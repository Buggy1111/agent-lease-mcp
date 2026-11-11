"""Offline B7 supervisor. Operator policy and kernel limits are mandatory.

Project code sees only a disposable commit snapshot, /usr and explicitly pinned
read-only tool mounts. The broker itself collects process observations. No
project report is accepted as execution evidence; no shell interpolates args.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import replace
from pathlib import Path, PurePosixPath

from ._quality_runs import CommandObservation, MetricObservation
from .cgroup import current_cgroup
from .config import is_trusted_agent
from .launch_gate import launch_gate
from .quality import inspect_repository
from .workspace import _git_hardened

MAX_OUTPUT = 1024 * 1024
MAX_SNAPSHOT = 64 * 1024 * 1024
MAX_FILES = 2000
MAX_MEMORY = 2 * 1024**3
MAX_PIDS = 256
MAX_JOB_SECONDS = 720
MAX_TOOL_BYTES = 128 * 1024**2  # approved standalone CPython 3.13 is about 81 MB


class SetupRequired(ValueError):
    """Only fixed, source-free diagnostics cross the broker boundary."""


def tree_digest(root: Path) -> str:
    """Pin an operator's tool tree without following links outside that tree."""
    root = root.resolve(strict=True)
    digest = hashlib.sha256()
    size = 0
    for count, path in enumerate(sorted(root.rglob("*")), 1):
        if count > 30000:
            raise SetupRequired("Tool tree exceeds the supported file limit.")
        info = path.lstat()
        name = path.relative_to(root).as_posix().encode()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(str(stat.S_IMODE(info.st_mode)).encode() + b"\0")
        if path.is_symlink():
            path.resolve(strict=True).relative_to(root)
            data = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            size += info.st_size
            if size > 512 * 1024**2:
                raise SetupRequired("Tool tree exceeds the supported size limit.")
            data = path.read_bytes()
        elif stat.S_ISDIR(info.st_mode):
            data = b"directory"
        else:
            raise SetupRequired("Tool trees must contain only regular files and internal symlinks.")
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def verify_limits() -> dict:
    """Require aggregate limits in this cgroup or its ancestors; no soft fallback."""
    group = current_cgroup()
    root = Path("/sys/fs/cgroup")
    memory, pids, cpu = [], [], []
    for parent in (group, *group.parents):
        if not parent.is_relative_to(root):
            break
        for filename, values in (("memory.max", memory), ("pids.max", pids)):
            path = parent / filename
            if path.exists():
                value = path.read_text().strip()
                if value != "max":
                    values.append(int(value))
        path = parent / "cpu.max"
        if path.exists():
            quota, period = path.read_text().split()
            if quota != "max":
                cpu.append(int(quota) / int(period))
    if (not memory or not pids or not cpu or min(memory) > MAX_MEMORY
            or min(pids) > MAX_PIDS or min(cpu) > 1):
        raise SetupRequired("Runner requires cgroup limits: memory <= 2 GiB, CPU <= 100%, pids <= 256.")
    return {"memory_bytes": min(memory), "pids": min(pids), "cpu_ratio": min(cpu)}


def policy_bytes(db_path: Path) -> bytes:
    path = db_path.resolve().parent / "quality-runner.toml"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 65536):
            raise SetupRequired("Runner policy must be a private operator-owned regular file.")
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    return raw


def load_policy(db_path: Path, repository: str, *, verify_pins: bool = True) -> dict:
    path = db_path.resolve().parent / "quality-runner.toml"
    if path.is_relative_to(Path(repository)):
        raise SetupRequired("Operator runner policy must be outside the project.")
    raw = policy_bytes(db_path)
    try:
        policy = tomllib.loads(raw.decode())
        if (set(policy) - {"version", "enabled", "automatic", "repositories", "policy_bases", "mounts", "tools", "timeout_seconds"}
                or policy.get("version") != 1 or type(policy.get("version")) is not int
                or policy.get("enabled") is not True):
            raise ValueError
        if "automatic" in policy and type(policy["automatic"]) is not bool:
            raise ValueError
        repositories = policy["repositories"]
        if (not isinstance(repositories, list) or not repositories
                or any(not isinstance(p, str) or not Path(p).is_absolute() for p in repositories)
                or repository not in [str(Path(p).resolve(strict=True)) for p in repositories]):
            raise ValueError
        timeout = policy.get("timeout_seconds", 60)
        policy_base = policy["policy_bases"][repository]
        if not isinstance(policy_base, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", policy_base):
            raise ValueError
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError
        if len(policy.get("mounts", {})) > 8 or len(policy["tools"]) > 32:
            raise ValueError
        mounts = {}
        for name, spec in policy.get("mounts", {}).items():
            if (not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) or name == "bin"
                    or set(spec) != {"path", "tree_sha256"}
                    or not re.fullmatch(r"[0-9a-f]{64}", spec["tree_sha256"])):
                raise ValueError
            source = Path(spec["path"])
            if not source.is_absolute():
                raise ValueError
            source = source.resolve(strict=True)
            if (not source.is_dir() or len(source.parts) < 4
                    or path.is_relative_to(source) or Path(repository).is_relative_to(source)
                    or source.is_relative_to(Path(repository))
                    or (verify_pins and tree_digest(source) != spec["tree_sha256"])):
                raise ValueError
            mounts[name] = str(source)
        tools = {}
        for name, spec in policy["tools"].items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or set(spec) != {"path", "sha256"}:
                raise ValueError
            if not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"]):
                raise ValueError
            source = Path(spec["path"])
            if not source.is_absolute():
                raise ValueError
            source = source.resolve(strict=True)
            if (not source.is_file() or source.stat().st_size > MAX_TOOL_BYTES
                    or not os.access(source, os.X_OK)
                    or (verify_pins and hashlib.sha256(source.read_bytes()).hexdigest() != spec["sha256"])):
                raise ValueError
            target = str(source) if source.is_relative_to(Path("/usr")) else None
            for mount_name, mount_source in mounts.items():
                if source.is_relative_to(Path(mount_source)):
                    target = "/tools/" + mount_name + "/" + source.relative_to(mount_source).as_posix()
            if target is None:
                raise ValueError
            tools[name] = target
        if not tools:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise SetupRequired("Runner policy is invalid, project is not allowed, or a tool pin changed.") from None
    return {"mounts": mounts, "tools": tools, "timeout": timeout,
            "policy_base": policy_base,
            "sha256": hashlib.sha256(raw).hexdigest()}


def export_snapshot(repository: str, commit: str, destination: Path) -> None:
    deadline = time.monotonic() + 30
    tree = _git_hardened(Path(repository), "ls-tree", "-r", "-l", "-z", commit)
    if tree is None or len(tree) > 1024 * 1024:
        raise SetupRequired("Cannot enumerate the committed snapshot.")
    entries = tree.rstrip(b"\0").split(b"\0") if tree else []
    total = 0
    if len(entries) > MAX_FILES:
        raise SetupRequired("Snapshot exceeds the supported file limit.")
    for entry in entries:
        if time.monotonic() >= deadline:
            raise SetupRequired("Snapshot preparation exceeded its time limit.")
        info, raw_name = entry.split(b"\t", 1)
        mode, kind, oid, size = info.split()
        name = PurePosixPath(os.fsdecode(raw_name))
        if (mode not in (b"100644", b"100755") or kind != b"blob"
                or name.is_absolute() or any(p in {"..", ".git"} for p in name.parts)):
            raise SetupRequired("Snapshot contains unsupported links, submodules or special paths.")
        total += int(size)
        if total > MAX_SNAPSHOT:
            raise SetupRequired("Snapshot exceeds the supported size limit.")
        data = _git_hardened(Path(repository), "cat-file", "blob", oid.decode())
        if data is None or len(data) != int(size):
            raise SetupRequired("Cannot read committed snapshot content.")
        target = destination.joinpath(*name.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if mode == b"100755" else 0o644)


def committed_branch(repository: str, commit: str) -> str | None:
    """Only label the branch when the selected commit is its current HEAD."""
    head = _git_hardened(Path(repository), "rev-parse", "--verify", "HEAD^{commit}")
    if head is None or head.strip().decode("ascii", "replace") != commit:
        return None
    branch = _git_hardened(Path(repository), "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch is None:
        return None
    try:
        name = branch.decode().strip()
    except UnicodeError:
        return None
    return name if 0 < len(name) <= 256 and all(ord(c) >= 32 and ord(c) != 127 for c in name) else None


def sandbox_argv(work: Path, bins: Path, policy: dict, command: tuple[str, ...], *, probe=False,
                 collect_metrics=False) -> list[str]:
    if command[0] not in policy["tools"] and not (probe and command == ("/usr/bin/true",)):
        raise SetupRequired("A committed command has no explicitly pinned operator tool.")
    argv = ["/usr/bin/bwrap", "--unshare-all", "--share-net", "--as-pid-1", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL", "--clearenv", "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
            "--tmpfs", "/tmp", "--dir", "/tmp/home", "--dir", "/etc",
            "--ro-bind", str(bins.parent / "passwd"), "/etc/passwd",
            "--ro-bind", str(bins.parent / "group"), "/etc/group",
            "--ro-bind", str(work), "/snapshot", "--tmpfs", "/work",
            "--chmod", "0777", "/work",
            "--ro-bind", str(bins), "/tools/bin",
            "--ro-bind", str(Path(__file__).with_name("quality_collect.py")), "/tools/collector.py",
            "--chdir", "/work", "--setenv", "PATH", "/tools/bin:/usr/bin:/bin",
            "--setenv", "HOME", "/tmp/home", "--setenv", "TMPDIR", "/tmp",
            "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--setenv", "PYTHONSAFEPATH", "1",
            "--setenv", "npm_config_offline", "true", "--setenv", "npm_config_cache", "/tmp/npm"]
    for name, source in policy["mounts"].items():
        argv += ["--ro-bind", source, "/tools/" + name]
    if "python" in policy["mounts"]:
        argv += ["--setenv", "PYTHONHOME", "/tools/python"]
    if "site" in policy["mounts"]:
        argv += ["--setenv", "PYTHONPATH", "/tools/site"]
    if "node_modules" in policy["mounts"]:
        argv += ["--ro-bind", policy["mounts"]["node_modules"], "/work/node_modules"]
    # No host-writable project mount. cp runs in the same bounded namespace;
    # quoted "$@" preserves each committed argument without shell expansion.
    return [sys.executable, "-I", "-S", "-B", str(Path(__file__).with_name("quality_namespace.py")),
            str(os.getpid()), *argv,
            "--", "/bin/sh", "-c", '/usr/bin/cp -R --no-preserve=ownership,mode /snapshot/. /work/ && exec "$@"',
            "quality", "/usr/bin/python3", "-I", "-S", "-B", "/tools/collector.py", "__QUALITY_METRICS_FD__",
            "coverage" if collect_metrics else "none", *command]


def supervise(argv: list[str], *, name: str, timeout: int, can_continue, store) -> CommandObservation:
    """Bounded binary capture. Killing the namespace init also kills setsid descendants."""
    started = time.monotonic()
    digest = hashlib.sha256()
    size = 0
    interrupted = timed_out = limited = False
    killed_at = None
    metadata = os.memfd_create("agent-lease-quality-metrics", os.MFD_CLOEXEC)
    proc = None
    metric = None
    try:
        argv = list(argv)
        argv[argv.index("__QUALITY_METRICS_FD__")] = str(metadata)
        with launch_gate(store.db_path):
            if not can_continue():
                raise SetupRequired("Runner was paused or task ownership was lost before launch.")
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env={"PATH": "/usr/bin:/bin"},
                                    pass_fds=(metadata,), start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map() or proc.poll() is None:
                if not can_continue():
                    interrupted = True
                timed_out = time.monotonic() - started >= timeout
                if interrupted or timed_out or limited:
                    if proc.poll() is None:
                        proc.kill()
                    killed_at = killed_at or time.monotonic()
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif not limited:
                        captured = chunk[:MAX_OUTPUT + 1 - size]
                        digest.update(captured)
                        size += len(captured)
                        limited = size > MAX_OUTPUT
                if killed_at is not None and time.monotonic() - killed_at >= 5:
                    raise RuntimeError("Cannot verify sandbox output cleanup.")
        code = proc.wait(timeout=5)
        if name == "coverage" and code == 0 and os.fstat(metadata).st_size <= 4096:
            try:
                os.lseek(metadata, 0, os.SEEK_SET)
                values = json.loads(os.read(metadata, 4097))
                if values:
                    metric = MetricObservation(**values)
            except (ValueError, TypeError):
                metric = None  # absent/invalid metrics never become a passing coverage check
    finally:
        os.close(metadata)
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close()
    return CommandObservation(name, code, timed_out, int((time.monotonic() - started) * 1000),
                              size, digest.hexdigest(), interrupted, limited, metric)


def _task_valid(store, agent: str, repository: str, task_id: int | None, lease_token: str | None) -> bool:
    shutdown = getattr(store, "_quality_shutdown", None)
    if shutdown is not None and shutdown.is_set():
        return False
    if store.paused():
        return False
    if task_id is None:
        return lease_token is None
    if type(task_id) is not int or task_id <= 0 or not isinstance(lease_token, str):
        return False
    if not store.holds(task_id, agent, lease_token):
        return False
    with store._connect() as con:
        row = con.execute("SELECT m.kind,m.project,d.state FROM messages m JOIN deliveries d "
                          "ON d.message_id=m.id WHERE m.id=? AND d.recipient=?",
                          (task_id, agent)).fetchone()
    return bool(row and row["kind"] == "task" and row["state"] == "started"
                and row["project"] == repository)


def run_quality(store, agent: str, repo: str, commit: str = "HEAD", base: str | None = None,
                *, task_id: int | None = None, lease_token: str | None = None) -> dict:
    """Broker-owned execution, never accepts statuses/logs/commands from its caller."""
    store._check_review_agent(agent)
    if not is_trusted_agent(agent):
        raise PermissionError("Quality execution requires a configured agent identity")
    attempted = False
    job_started = time.monotonic()
    try:
        plan = inspect_repository(repo, commit, base)
        policy = load_policy(store.db_path, plan.repository)
        plan = inspect_repository(plan.repository, plan.commit_sha, base,
                                  policy_base=policy["policy_base"])
        branch = committed_branch(plan.repository, plan.commit_sha)
        if not plan.enabled or plan.problems:
            return plan.public()
        can_continue = lambda: (time.monotonic() - job_started < MAX_JOB_SECONDS
                                and _task_valid(store, agent, plan.repository, task_id, lease_token))
        if not can_continue():
            raise SetupRequired("Runner is paused or the task binding is invalid.")
        limits = verify_limits()
        commands = [c for c in plan.checks if c.command]
        if not commands:
            return plan.public()
        if any(c.command[0] not in policy["tools"] for c in commands):
            raise SetupRequired("A committed command has no explicitly pinned operator tool.")
        # One live run per room. This avoids multiplying aggregate resource budgets.
        lock = os.open(str(store.db_path.resolve()) + ".quality-lock",
                       os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with tempfile.TemporaryDirectory(prefix="agent-lease-quality-") as tmp:
                root = Path(tmp)
                original, bins = root / "snapshot", root / "bin"
                original.mkdir()
                bins.mkdir()
                (root / "passwd").write_text(f"runner:x:0:0:runner:/tmp/home:/bin/sh\nhostrunner:x:{os.getuid()}:{os.getgid()}:runner:/tmp/home:/bin/sh\n")
                (root / "group").write_text(f"runner:x:0:\nhostrunner:x:{os.getgid()}:\n")
                export_snapshot(plan.repository, plan.commit_sha, original)
                for name, target in policy["tools"].items():
                    (bins / name).symlink_to(target)
                probe_work = root / "probe"
                probe_work.mkdir()
                probe = supervise(sandbox_argv(probe_work, bins, policy, ("/usr/bin/true",), probe=True),
                                  name="tests", timeout=5, can_continue=can_continue, store=store)
                if probe.exit_code != 0 or probe.timed_out or probe.interrupted:
                    raise SetupRequired(f"The required sandbox namespace could not start (bootstrap exit {probe.exit_code}).")
                observations = []
                # Verify required Python modules in an empty work namespace,
                # before importing any project code or accepting a test failure.
                modules = {(c.command[0], c.command[2]) for c in commands
                           if len(c.command) >= 3 and c.command[0].startswith("python")
                           and c.command[1] == "-m"}
                for tool, module in sorted(modules):
                    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", module):
                        raise SetupRequired("Unsupported Python module command.")
                    script = "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec(" + repr(module) + ") else 2)"
                    tested = supervise(sandbox_argv(probe_work, bins, policy, (tool, "-c", script)),
                                        name="tests", timeout=5, can_continue=can_continue, store=store)
                    if tested.exit_code != 0 or tested.timed_out or tested.interrupted:
                        raise SetupRequired("A declared Python check module is missing from the pinned tool environment.")
                for check in commands:
                    if observations and not can_continue():
                        observations[-1] = replace(observations[-1], interrupted=True)
                        break
                    attempted = True
                    result = supervise(sandbox_argv(original, bins, policy, check.command,
                                                    collect_metrics=check.name == "coverage"), name=check.name,
                                       timeout=policy["timeout"], can_continue=can_continue, store=store)
                    observations.append(result)
                    if result.interrupted or result.output_limited or result.timed_out:
                        break
                return store._quality_record_run(agent, plan, tuple(observations),
                                                 execution_id=uuid.uuid4().hex, task_id=task_id,
                                                 branch=branch,
                                                 sandbox={"policy_sha256": policy["sha256"],
                                                          "limits": limits, "network": "loopback_only"})
        finally:
            os.close(lock)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        # Exceptions from parsers/process launch can include source or secret args.
        return {"schema_version": 1, "kind": "quality_refusal", "executed": attempted,
                "execution_status": "unverified" if attempted else "not_started",
                "overall_status": "REVIEW" if attempted else "SETUP_REQUIRED",
                "error": str(error) if type(error) is SetupRequired else
                "Runner setup, task authorization, snapshot, policy pins or isolation could not be verified."}


def check_cli(repo: str, commit: str, base: str | None, task_id: int | None,
              lease_token: str | None, *, enqueue: bool = False) -> int:
    from .broker import open_store
    from .config import UNTRUSTED_PREFIXES, Settings

    settings = Settings.from_env()
    if settings.agent.startswith(UNTRUSTED_PREFIXES):
        print(json.dumps({"overall_status": "SETUP_REQUIRED", "error": "An explicit agent identity is required."}))
        return 2
    try:
        store = open_store(settings)
        if enqueue:
            result = store.quality_request(settings.agent, str(Path(repo).resolve()), commit, base)
            print(json.dumps({"kind": "quality_request", "executed": False, **result}, indent=2))
            return 0 if result["state"] in {"queued", "running", "completed"} else 2
        result = store.quality_check(settings.agent, str(Path(repo).resolve()), commit, base,
                                                   task_id=task_id, lease_token=lease_token)
    except (OSError, ValueError, RuntimeError, KeyError):
        result = {"kind": "quality_request_error", "executed": None, "overall_status": "REVIEW",
                  "execution_status": "unknown",
                  "error": "Quality runner reply unavailable; check stored runs before retrying."}
    print(json.dumps(result, indent=2))
    return 0 if result["overall_status"] == "PASS" else 1 if result["overall_status"] == "FAIL" else 2
