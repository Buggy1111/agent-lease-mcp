"""B7.1: commit-bound inspection and result policy. Never executes project commands."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from .workspace import _git_hardened, is_strict_ancestor, parent_commit, resolve_commit

CHECK_NAMES = ("tests", "lint", "typecheck", "build", "coverage", "security", "crap")
COMMAND_CHECKS = frozenset({"tests", "lint", "typecheck", "build", "coverage"})
REQUIRED_CHECKS = frozenset({"tests", "lint", "build", "security"})
CONFIG_PATH = ".agent-lease/quality.toml"
MAX_MANIFEST_BYTES = 256 * 1024
LANGUAGES = frozenset({"python", "typescript", "javascript"})
LOCKS = {"package-lock.json": "npm", "npm-shrinkwrap.json": "npm",
         "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn"}


class Status(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"
    NA = "NA"
    SETUP_REQUIRED = "SETUP_REQUIRED"
    NOT_RUN = "NOT_RUN"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    required: bool
    detail: str
    command: tuple[str, ...] = ()
    source: str | None = None

    def __post_init__(self) -> None:
        if self.name not in CHECK_NAMES or not isinstance(self.status, Status):
            raise ValueError("Invalid quality check")
        if type(self.required) is not bool:
            raise ValueError("Check required must be boolean")

    def public(self) -> dict:
        # Commands may embed credentials. Expose provenance, never argument/script contents.
        return {"status": self.status.value, "required": self.required, "detail": self.detail,
                "command_configured": bool(self.command), "source": self.source, "value": None}


def aggregate(checks: tuple[Check, ...], *, peer_review: str = "pending",
              policy_review_required: bool = False) -> Status:
    """A pure result policy, not an approval API or a substitute for B6 review identity."""
    names = [check.name for check in checks]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate quality check")
    if set(names) != set(CHECK_NAMES) or not any(check.required for check in checks):
        return Status.SETUP_REQUIRED
    if any(check.name in REQUIRED_CHECKS and not check.required for check in checks):
        return Status.SETUP_REQUIRED
    if any(check.required and check.status is Status.FAIL for check in checks):
        return Status.FAIL
    if any(check.required and check.status in (Status.NA, Status.SETUP_REQUIRED)
           for check in checks):
        return Status.SETUP_REQUIRED
    if policy_review_required:
        return Status.REVIEW
    if any(check.required and check.status is Status.NOT_RUN for check in checks):
        return Status.NOT_RUN
    if peer_review != "approved" or any(check.status in (Status.REVIEW, Status.FAIL,
                                                        Status.SETUP_REQUIRED)
                                         for check in checks):
        return Status.REVIEW
    return Status.PASS


@dataclass(frozen=True)
class Inspection:
    repository: str
    commit_sha: str
    base_commit: str
    language: str | None
    package_manager: str | None
    dirty: bool
    enabled: bool
    checks: tuple[Check, ...]
    policy_paths: tuple[str, ...]
    problems: tuple[str, ...]
    declared_tools: tuple[str, ...]
    config_digest: str | None
    policy_base: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.commit_sha):
            raise ValueError("Quality results require a full commit hash")

    def public(self) -> dict:
        status = aggregate(self.checks, policy_review_required=bool(self.policy_paths))
        if not self.enabled and not self.policy_paths:
            status = Status.NOT_RUN
        identity = "\0".join((self.repository, self.commit_sha, self.base_commit,
                               self.config_digest or ""))
        return {"schema_version": 1, "kind": "inspection", "executed": False,
                "execution_allowed": False,
                "inspection_id": hashlib.sha256(identity.encode()).hexdigest(),
                "repository": self.repository, "commit_sha": self.commit_sha,
                "branch": None, "agent": None, "timestamp": None,
                "base_commit": self.base_commit or None, "policy_base": self.policy_base,
                "language": self.language,
                "package_manager": self.package_manager, "working_tree_dirty": self.dirty,
                "enabled": self.enabled, "overall_status": status.value,
                "checks": {check.name: check.public() for check in self.checks},
                "policy_review_required": bool(self.policy_paths),
                "policy_paths": list(self.policy_paths), "setup_reasons": list(self.problems),
                "declared_tools": list(self.declared_tools), "config_digest": self.config_digest,
                "reviewer": None, "review_status": "pending", "logs": [], "history": []}


class _Snapshot:
    """Read only fixed manifest names from Git objects; never follow checkout symlinks."""

    def __init__(self, root: str, sha: str):
        self.root, self.sha = Path(root), sha

    def entry(self, name: str) -> str | None:
        output = _git_hardened(self.root, "ls-tree", "-z", self.sha, "--", name)
        if output is None:
            raise ValueError("Cannot read committed project metadata")
        if not output:
            return None
        entries = output.rstrip(b"\0").split(b"\0")
        if len(entries) != 1:
            raise ValueError("Ambiguous committed manifest")
        info, path = entries[0].split(b"\t", 1)
        mode, kind, oid = info.split(b" ")
        if path.decode() != name or mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("Project manifests must be regular committed files")
        return oid.decode()

    def read(self, name: str) -> bytes | None:
        if "/" in name:
            parent = name.split("/", 1)[0]
            entry = _git_hardened(self.root, "ls-tree", "-z", self.sha, "--", parent)
            if entry is None or (entry and not entry.startswith(b"040000 tree ")):
                raise ValueError("Project manifest directories must not be symlinks or submodules")
        oid = self.entry(name)
        if oid is None:
            return None
        size = _git_hardened(self.root, "cat-file", "-s", oid)
        if size is None or not size.strip().isdigit() or int(size) > MAX_MANIFEST_BYTES:
            raise ValueError("Project manifest exceeds the inspection limit")
        content = _git_hardened(self.root, "cat-file", "blob", oid)
        if content is None or len(content) != int(size):
            raise ValueError("Cannot read committed project manifest")
        return content


def _json_object(pairs: list[tuple]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property in package.json")
        result[key] = value
    return result


def _manifest(raw: bytes | None, name: str) -> dict:
    if raw is None:
        return {}
    try:
        value = (json.loads(raw.decode(), object_pairs_hook=_json_object) if name.endswith(".json")
                 else tomllib.loads(raw.decode()))
    except (ValueError, UnicodeError, RecursionError):
        # Parser diagnostics can contain embedded source/credentials. Do not echo them.
        raise ValueError(f"Invalid committed {name}") from None
    if not isinstance(value, dict):
        raise TypeError(f"Invalid committed {name}")
    return value


def _configuration(raw: bytes | None) -> dict:
    if raw is None:
        return {}
    data = _manifest(raw, CONFIG_PATH)
    if set(data) - {"version", "language", "enabled", "checks"}:
        raise ValueError("Unsupported quality configuration key; policy overrides are not supported")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Quality configuration requires version = 1")
    if "enabled" in data and type(data["enabled"]) is not bool:
        raise ValueError("Quality enabled must be boolean")
    if "language" in data and (not isinstance(data["language"], str) or data["language"] not in LANGUAGES):
        raise ValueError("Unsupported configured project language")
    checks = data.get("checks", {})
    if not isinstance(checks, dict) or set(checks) - COMMAND_CHECKS:
        raise ValueError("Only project command checks can be configured; security and CRAP are reserved")
    for spec in checks.values():
        if not isinstance(spec, dict) or set(spec) != {"command"}:
            raise ValueError("Each configured check requires only a command argument array")
        command = spec["command"]
        if (not isinstance(command, list) or not 1 <= len(command) <= 128
                or any(not isinstance(arg, str) or not arg or len(arg) > 4096
                       or any(ord(char) < 32 or ord(char) == 127 for char in arg) for arg in command)
                or sum(map(len, command)) > 16384):
            raise ValueError("Invalid project command argument array")
        if Path(command[0]).name in {"npx", "bunx"} or (len(command) > 1 and
                (Path(command[0]).name, command[1]) in {("npm", "exec"), ("pnpm", "dlx")}):
            raise ValueError("Package download commands are not supported quality commands")
    return data


def policy_path(path: str) -> bool:
    """Conservative filename guard; semantic test weakening still needs independent review."""
    name = PurePosixPath(path.lower())
    if any(part in {"test", "tests", "__tests__", ".github", ".circleci", ".agent-lease",
                    "security", ".semgrep", "node_modules"} for part in name.parts):
        return True
    filename = name.name
    if ("agent_lease_mcp" in name.parts and filename in {
            "_reviews.py", "store.py", "broker.py", "webui.py", "lifecycle.py", "config.py",
            "launch_gate.py", "process_guard.py", "cgroup.py", "run_fence.py"}):
        return True
    return (filename.startswith(("test_", "test.", "tests.", ".env", ".eslintrc", "eslint.config.", "tsconfig",
                                 ".gitleaks", ".betterleaks", "vitest.config.", "jest.config.",
                                 "playwright.config.", "cypress.config.", ".nycrc"))
            or re.search(r"(?:\.test\.|\.spec\.|_test\.)[^/]+$", filename) is not None
            or filename in {"conftest.py", "pyproject.toml", "package.json", "uv.lock",
                            "requirements.txt", "requirements-dev.txt", "pytest.ini", ".pytest.ini",
                            "pytest.toml", ".pytest.toml", ".mypy.ini", "tox.ini",
                            "setup.cfg", ".coveragerc", "ruff.toml", ".ruff.toml", "mypy.ini",
                            ".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml", ".gitmodules",
                            ".gitattributes", ".npmrc", ".pre-commit-config.yaml",
                            "secret_scan.py", "hygiene.py", "jev_policy.py", "quality.py",
                            "_quality_runs.py", "_quality_queue.py", "quality_runner.py", "quality_namespace.py",
                            "quality_collect.py"}
            or filename in LOCKS)


def _changed_policy(root: str, head: str, base: str,
                    tool_modules: frozenset[str] = frozenset()) -> tuple[str, ...]:
    if base:
        output = _git_hardened(Path(root), "diff", "--name-only", "-z", "--no-ext-diff",
                               "--no-textconv", "--no-renames", base, head, "--")
    else:
        output = _git_hardened(Path(root), "diff-tree", "--root", "--no-commit-id", "-r",
                               "--name-only", "-z", "--no-renames", head, "--")
    if output is None or len(output) > 4 * 1024 * 1024:
        raise ValueError("Cannot establish the quality policy diff")
    paths = [path.decode("utf-8", "surrogateescape") for path in output.split(b"\0") if path]
    return tuple(sorted(path for path in paths if policy_path(path)
                        or PurePosixPath(path).parts[0].split(".")[0] in tool_modules))


def inspect_repository(repo: str | Path, commit: str = "HEAD", base: str | None = None,
                       *, policy_base: str | None = None) -> Inspection:
    """Plan only from a resolved commit. No room, configuration writes, installs or shell execution."""
    if not commit or len(commit) > 1024 or any(ord(c) < 32 for c in commit):
        raise ValueError("Invalid quality commit")
    root, head, dirty = resolve_commit(repo, commit)
    if base is None:
        base_sha = parent_commit(root, head)
    else:
        _, base_sha, _ = resolve_commit(root, base)
        if not is_strict_ancestor(root, base_sha, head):
            raise ValueError("Quality base must be a strict ancestor of the selected commit")
    if policy_base is not None:
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", policy_base):
            raise ValueError("Policy baseline must be an operator-pinned full commit hash")
        _, pinned, _ = resolve_commit(root, policy_base)
        if pinned != policy_base or (pinned != head and not is_strict_ancestor(root, pinned, head)):
            raise ValueError("Policy baseline must be an ancestor of the selected commit")
    snapshot = _Snapshot(root, head)
    problems: list[str] = []
    commands: dict[str, tuple[tuple[str, ...], str]] = {}
    language = manager = None
    tools: list[str] = []
    enabled = True
    config_digest = None
    try:
        config_raw = snapshot.read(CONFIG_PATH)
        config_digest = hashlib.sha256(config_raw).hexdigest() if config_raw is not None else None
        config = _configuration(config_raw)
        enabled = config.get("enabled", True)
        python_raw = snapshot.read("pyproject.toml")
        node_raw = snapshot.read("package.json")
        python = _manifest(python_raw, "pyproject.toml")
        node = _manifest(node_raw, "package.json")
        scripts = node.get("scripts", {})
        dependencies = node.get("devDependencies", {})
        runtime_dependencies = node.get("dependencies", {})
        if (not isinstance(scripts, dict) or any(not isinstance(v, str) for v in scripts.values())
                or not isinstance(dependencies, dict) or not isinstance(runtime_dependencies, dict)):
            raise ValueError("Invalid package.json scripts or development dependencies")
        available: list[str] = []
        if python_raw is not None:
            available.append("python")
            declared = python.get("tool", {})
            if not isinstance(declared, dict):
                raise ValueError("Invalid pyproject.toml tool configuration")
            tools = sorted(name for name in ("pytest", "ruff", "mypy", "pyright", "coverage")
                           if name in declared)
        if node_raw is not None:
            available.append("typescript" if snapshot.entry("tsconfig.json") is not None
                             or "typescript" in dependencies or "typescript" in runtime_dependencies
                             else "javascript")
        selected = config.get("language")
        if selected is not None and selected not in available:
            raise ValueError("Configured language does not match the committed manifests")
        if len(available) > 1 and selected is None:
            raise ValueError("Mixed projects require an explicit quality language selection")
        language = selected or (available[0] if available else None)
        if language is None:
            raise ValueError("No supported committed project manifest; setup is required")
        if language in {"javascript", "typescript"}:
            managers = {value for name, value in LOCKS.items() if snapshot.entry(name) is not None}
            declared_manager = node.get("packageManager")
            if declared_manager is not None:
                if not isinstance(declared_manager, str) or not re.fullmatch(
                        r"(?:npm|pnpm|yarn)@[^\s]+", declared_manager):
                    raise ValueError("Unsupported package manager declaration")
                chosen = declared_manager.split("@", 1)[0]
                if managers and managers != {chosen}:
                    raise ValueError("Package manager declaration conflicts with lockfiles")
                manager = chosen
            elif len(managers) == 1:
                manager = next(iter(managers))
            elif len(managers) > 1:
                raise ValueError("Conflicting package manager lockfiles")
            if manager:
                for check, script in {"tests": "test", "lint": "lint", "typecheck": "typecheck",
                                      "build": "build", "coverage": "coverage"}.items():
                    if scripts.get(script, "").strip():
                        commands[check] = ((manager, "run", script), f"package.json:scripts.{script}")
        for check, spec in config.get("checks", {}).items():
            commands[check] = (tuple(spec["command"]), f"{CONFIG_PATH}:checks.{check}")
    except (ValueError, TypeError) as error:
        problems.append(str(error))
    checks: list[Check] = []
    for name in CHECK_NAMES:
        required = name in REQUIRED_CHECKS or (name == "typecheck" and language == "typescript")
        command, source = commands.get(name, ((), None))
        if not enabled:
            status, detail = Status.NOT_RUN, "Repository quality checks are explicitly opted out."
        elif problems:
            status = Status.SETUP_REQUIRED if required else Status.NA
            detail = "Project configuration needs attention; no checks were executed."
        elif name == "security":
            status, detail = Status.NOT_RUN, "B6 diff security and independent review are not imported by inspection."
            source = "agent-lease:B6"
        elif command:
            status, detail = Status.NOT_RUN, "Committed command declared; environment and execution are unverified."
        elif required:
            status, detail = Status.SETUP_REQUIRED, "No unambiguous committed command; add project quality configuration."
        else:
            status, detail = Status.NA, "No applicable configured metric; not a passing result."
        checks.append(Check(name, status, required, detail, command, source))
    tool_modules = {Path(command[0]).stem for command, _ in commands.values()}
    for command, _ in commands.values():
        if "-m" in command and command.index("-m") + 1 < len(command):
            tool_modules.add(command[command.index("-m") + 1].split(".")[0])
    guarded = (() if policy_base == head else
               _changed_policy(root, head, policy_base or base_sha, frozenset(tool_modules)))
    return Inspection(root, head, base_sha, language, manager, dirty, enabled, tuple(checks),
                      guarded, tuple(problems), tuple(tools), config_digest, policy_base)


def inspect_cli(repo: str, commit: str, base: str | None) -> int:
    try:
        result = inspect_repository(repo, commit, base).public()
    except (ValueError, OSError):
        print(json.dumps({"schema_version": 1, "kind": "inspection", "executed": False,
                          "overall_status": Status.SETUP_REQUIRED.value,
                          "error": "Cannot inspect the requested repository, commit or comparison base."}))
        return 2
    print(json.dumps(result, indent=2))
    return 2 if result["overall_status"] == Status.SETUP_REQUIRED.value else 0
