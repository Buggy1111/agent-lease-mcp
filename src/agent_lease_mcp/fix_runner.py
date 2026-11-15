"""Zápisový běh opravy v izolaci (fáze C1b) — DŮVĚRYHODNÁ strana, žádná logika od agenta.

Odpověď na NO-GO z C0 (docs/research/2026-11-12-c0-codex-worktree.md): přepínač
`--sandbox workspace-write` klienta nestačil (zápis do /tmp mimo worktree, čtení mimo
repo, závislost na sdíleném `.git`). Proto vlastní obal z OS prostředků:

1. **Samostatný klon** (`prepare`): vlastní `.git` s vlastními metadaty, nikdy sdílený
   git common dir ani worktree hlavního repa. Hooky vypnuté, klon na přesném základu.
2. **bwrap** (`sandbox_argv`): celý systém jen pro čtení, zapisovat jde JEN do
   pracovních souborů klonu. `.git` klonu je pro agenta jen pro čtení (žádná změna
   config/hooks/refs → žádné spuštění kódu v důvěryhodném procesu přes git config),
   `/tmp` je soukromý tmpfs, stav agent-lease (adresář DB s `webui.token`, `room.db`)
   a běžná úložiště tajemství jsou překrytá, sokety hostitele (session bus, docker)
   schované a prostředí je prázdné kromě krátkého seznamu (`ENV_PASS`). HOME je
   jednorázová kopie jen přihlašovacích souborů CLI; zpět se vrací jen obnovené
   přihlášení, nikdy konfigurace (agent tak nezmění MCP servery ani hooky CLI).
3. **Vyhodnocení** (`collect`): důvěryhodný proces po skončení agenta zkontroluje
   změny (jen povolené cesty, žádné symlinky ani speciální soubory, limity velikosti,
   kritické soubory CI vždy zamítne), projde diff secret scanem a teprve pak sám
   commitne pevnou identitou s `--no-verify`.
4. **Předání** (`publish`): hlavní repo si commit jen stáhne (`fetch`) do větve
   `agent-fix/<id>`; nic se neslučuje, nemění se pracovní kopie ani jiné větve.

Síť zůstává (CLI potřebuje API modelu). Sloučení větve schvaluje člověk.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath

from .secret_scan import scan_diff

BWRAP = "/usr/bin/bwrap"
MAX_FILES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DIFF_BYTES = 8 * 1024 * 1024
# Co se agentovi vždy schová (stav místnosti, klíče, přihlášení k jiným službám).
DEFAULT_HIDE = (".agent-lease", ".ssh", ".gnupg", ".config/gh", ".aws", ".docker", ".kube",
                ".git-credentials", ".netrc", ".pypirc")

_GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "protocol.file.allow=always", "-c", "commit.gpgsign=false")


def _git(*args: str, cwd: Path | str, check: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "HOME": "/nonexistent", "LC_ALL": "C"}
    return subprocess.run(["git", *_GIT_SAFE, *args], cwd=cwd, env=env, input=input,
                          capture_output=True, check=check, timeout=120)


def prepare(repo: Path, base_commit: str, workdir: Path) -> Path:
    """Samostatný klon `repo` na `base_commit` v `workdir/clone` (workdir patří jen tomuto běhu)."""
    repo, workdir = Path(repo).resolve(), Path(workdir)
    workdir.mkdir(mode=0o700, parents=True, exist_ok=False)
    clone = workdir / "clone"
    _git("clone", "--quiet", "--no-hardlinks", "--no-checkout", str(repo), str(clone), cwd=workdir)
    _git("config", "core.hooksPath", "/dev/null", cwd=clone)
    _git("checkout", "--quiet", "--detach", base_commit, cwd=clone)
    head = _git("rev-parse", "HEAD", cwd=clone).stdout.decode().strip()
    if head != base_commit and not head.startswith(base_commit):
        raise ValueError(f"clone is at {head}, expected {base_commit}")
    return clone


# Proměnné, které smí projít do běhu (přihlášení CLI přes API klíč, jazyk); nic z AGENT_LEASE_*.
ENV_PASS = ("LANG", "LC_ALL", "TERM", "USER", "LOGNAME", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "CODEX_API_KEY", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")


def prepare_home(workdir: Path, real_home: Path, files: tuple[str, ...]) -> Path:
    """Jednorázový HOME běhu: jen kopie přihlašovacích souborů CLI (skutečná konfigurace zůstane netknutá)."""
    run_home = Path(workdir) / "home"
    run_home.mkdir(mode=0o700)
    for rel in files:
        src = Path(real_home) / rel
        if src.is_file() and not src.is_symlink():
            dst = run_home / rel
            dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            dst.chmod(0o600)
    return run_home


def sync_credentials(run_home: Path, real_home: Path, files: tuple[str, ...]) -> list[str]:
    """Vrátí zpět JEN obnovené přihlašovací soubory (rotující refresh token), a jen platný JSON objekt."""
    import json
    synced = []
    for rel in files:
        src, dst = Path(run_home) / rel, Path(real_home) / rel
        if not src.is_file() or src.is_symlink() or src.stat().st_size > 256 * 1024:
            continue
        data = src.read_bytes()
        try:
            if not isinstance(json.loads(data), dict):
                continue
        except ValueError:
            continue
        if dst.exists() and dst.read_bytes() == data:
            continue
        tmp = dst.with_name(dst.name + ".agent-lease-tmp")
        tmp.write_bytes(data)
        tmp.chmod(0o600)
        tmp.replace(dst)
        synced.append(rel)
    return synced


# Allowlist: jen systémové adresáře (programy, knihovny, /etc kvůli certifikátům a DNS).
# HOME hostitele, jiné projekty, /run (sokety, broker) ani /mnt se vůbec nepřipojí (review #20 F1).
SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/etc", "/opt")


def runtime_paths(argv: list[str]) -> tuple[list[str], list[str]]:
    """Spustitelný soubor jako absolutní cesta + co z hostitele musí být čitelné, aby šel spustit.

    Program mimo systémové adresáře (CLI v ~/.local, interpret z uv/nvm) se připojí jen pro čtení:
    instalace `…/bin/x` celým kořenem instalace, samostatný binární soubor sám. Stejně tak
    interpret ze shebangu a absolutní cesty v argumentech (např. skript).
    """
    if not argv:
        return [], []
    exe = shutil.which(argv[0]) or argv[0]
    real = Path(exe).resolve()
    ro: list[str] = []

    def add(path: Path) -> None:
        if not any(str(path).startswith(root + "/") or str(path) == root for root in SYSTEM_RO):
            ro.append(str(path.parent.parent if path.parent.name == "bin" else path))
    add(real)
    try:
        head = real.read_bytes()[:256]
        if head.startswith(b"#!"):
            interp = head[2:].split(b"\n", 1)[0].split()
            if interp and interp[0].endswith(b"/env") and len(interp) > 1:
                found = shutil.which(interp[1].decode())
                if found:
                    add(Path(found).resolve())
            elif interp:
                add(Path(interp[0].decode()).resolve())
    except OSError:
        pass
    for arg in argv[1:]:
        if arg.startswith("/") and len(arg) < 4096 and Path(arg).exists():
            add(Path(arg).resolve())
    return [str(real), *argv[1:]], sorted(set(ro))


def sandbox_argv(clone: Path, argv: list[str], *, home: Path | None = None, run_home: Path | None = None,
                 ro_paths: tuple[str, ...] | list[str] = (), hide_abs: tuple[str, ...] = (),
                 extra_rw: tuple[str, ...] = (), env: dict[str, str] | None = None,
                 hide: tuple[str, ...] = DEFAULT_HIDE) -> list[str]:
    """Příkaz, který spustí `argv` v izolaci: vidí jen systémové adresáře, program, klon a HOME běhu."""
    clone = Path(clone).resolve()
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    argv, needed = runtime_paths(list(argv))
    args = [BWRAP, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
            "--unshare-uts", "--unshare-cgroup-try", "--clearenv"]
    for root in SYSTEM_RO:
        p = Path(root)
        if p.is_symlink():
            args += ["--symlink", os.readlink(p), root]
        elif p.is_dir():
            args += ["--ro-bind", root, root]
    resolv = Path("/etc/resolv.conf").resolve()   # WSL / systemd-resolved: cíl mimo /etc
    if resolv.exists() and not str(resolv).startswith("/etc/"):
        args += ["--ro-bind", str(resolv), str(resolv)]
    args += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for path in [*needed, *ro_paths]:
        args += ["--ro-bind", str(path), str(path)]
    # Pojistka: kdyby připojená cesta obsahovala stav místnosti nebo tajemství, překrýt je.
    bound = [*needed, *[str(p) for p in ro_paths]]
    for target in [home / rel for rel in hide] + [Path(p) for p in hide_abs]:
        if any(str(target).startswith(b.rstrip("/") + "/") or str(target) == b for b in bound):
            args += ["--tmpfs", str(target)] if target.is_dir() else ["--ro-bind", "/dev/null", str(target)]
    for path in extra_rw:  # např. adresář s poslední odpovědí CLI, který čte bridge
        args += ["--bind", str(path), str(path)]
    if run_home is not None:
        args += ["--bind", str(run_home), str(run_home)]
    args += ["--bind", str(clone), str(clone), "--ro-bind", str(clone / ".git"), str(clone / ".git"),
             "--chdir", str(clone)]
    for key in ENV_PASS:
        if env.get(key):
            args += ["--setenv", key, env[key]]
    args += ["--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "TMPDIR", "/tmp",
             "--setenv", "HOME", str(run_home or home), "--"]
    return args + argv


def run_sandboxed(clone: Path, argv: list[str], *, timeout: int = 1800, **kw) -> subprocess.CompletedProcess:
    if not Path(BWRAP).exists():
        raise RuntimeError("bubblewrap is required for write-mode fixes (fail closed)")
    return subprocess.run(sandbox_argv(clone, argv, **kw), capture_output=True, timeout=timeout, check=False)


def _allowed(path: str, allowed: list[str]) -> bool:
    return not allowed or any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed)


_CRITICAL_DIRS = {".git", ".github", ".gitlab", ".circleci", "deploy"}
_CRITICAL_NAMES = {".gitattributes", ".gitmodules", ".gitlab-ci.yml", ".pre-commit-config.yaml",
                   ".gitconfig", ".mailmap"}


def _critical(path: str) -> bool:
    """Kritický soubor kdekoli ve stromu (i vnořený .gitattributes), nezávisle na velikosti písmen."""
    parts = [p.lower() for p in PurePosixPath(path).parts]
    return bool(set(parts[:-1]) & _CRITICAL_DIRS) or (parts and (parts[-1] in _CRITICAL_NAMES
                                                                 or parts[-1] in _CRITICAL_DIRS))


def collect(clone: Path, base_commit: str, allowed: list[str], message: str) -> dict:
    """Ověří změny agenta a commitne je důvěryhodnou identitou. Vrací {ok, commit, files, rejected, findings}."""
    clone = Path(clone)
    out = _git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", cwd=clone).stdout
    entries = [e for e in out.split(b"\0") if e]
    files, rejected = [], []
    for entry in entries:
        path = entry[3:].decode("utf-8", "replace")
        posix = PurePosixPath(path)
        if posix.is_absolute() or ".." in posix.parts:
            rejected.append({"path": path, "reason": "path escapes the repository"})
            continue
        full = clone / path
        if _critical(path):
            rejected.append({"path": path, "reason": "critical file (CI, git, deploy) — human only"})
        elif not _allowed(path, allowed):
            rejected.append({"path": path, "reason": "outside allowed paths"})
        elif full.is_symlink():
            rejected.append({"path": path, "reason": "symlink"})
        elif full.exists() and not stat.S_ISREG(full.lstat().st_mode):
            rejected.append({"path": path, "reason": "not a regular file"})
        elif full.exists() and full.stat().st_size > MAX_FILE_BYTES:
            rejected.append({"path": path, "reason": f"larger than {MAX_FILE_BYTES} bytes"})
        else:
            files.append(path)
    if len(files) > MAX_FILES:
        rejected.append({"path": "*", "reason": f"more than {MAX_FILES} files changed"})
    result = {"ok": False, "commit": None, "files": files, "rejected": rejected, "findings": []}
    if rejected or not files:
        if not files and not rejected:
            result["rejected"] = [{"path": "*", "reason": "no changes"}]
        return result
    _git("add", "--all", "--", *files, cwd=clone)
    numstat = _git("diff", "--cached", "--numstat", "--no-renames", "-z", base_commit, cwd=clone).stdout
    binary = [e.split(b"\t", 2)[2].decode("utf-8", "replace") for e in numstat.split(b"\0")
              if e.startswith(b"-\t-\t")]
    if binary:
        result["rejected"] = [{"path": b, "reason": "binary file (cannot be secret-scanned)"} for b in binary]
        return result
    diff = _git("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", base_commit, cwd=clone).stdout
    if len(diff) > MAX_DIFF_BYTES:
        result["rejected"] = [{"path": "*", "reason": f"diff larger than {MAX_DIFF_BYTES} bytes"}]
        return result
    findings = scan_diff(diff.decode("utf-8", "replace"))
    if findings:
        result["findings"] = findings
        return result
    _git("-c", "user.name=agent-lease", "-c", "user.email=agent-lease@localhost",
         "commit", "--quiet", "--no-verify", "-m", message, cwd=clone)
    result["commit"] = _git("rev-parse", "HEAD", cwd=clone).stdout.decode().strip()
    result["ok"] = True
    return result


_TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.(py|go|ts|js|rs)$|[^/]*\.(test|spec)\.[cm]?[jt]sx?$)")


def verify_regression(clone: Path, base_commit: str, files: list[str], test_argv: list[str], workdir: Path,
                      *, timeout: int = 900, **sandbox_kw) -> dict:
    """
    Důvěryhodné ověření C2a: oprava musí přinést regresní test, který na ZÁKLADU selže
    (nový test bez opravy) a s opravou projde i s celou sadou. Testy běží ve stejném bwrap.
    Vrací {ok, reason, tests}.
    """
    tests = [f for f in files if _TEST_FILE.search(f)]
    if not tests:
        return {"ok": False, "reason": "no regression test in the change", "tests": []}
    head = run_sandboxed(clone, test_argv, timeout=timeout, **sandbox_kw)
    if head.returncode != 0:
        return {"ok": False, "reason": f"tests fail with the fix (exit {head.returncode})", "tests": tests}
    base = prepare(clone, base_commit, Path(workdir) / "base-check")
    for rel in tests:  # jen nové/změněné testy na původní kód
        src = Path(clone) / rel
        if src.is_file():
            dst = base / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    old = run_sandboxed(base, test_argv, timeout=timeout, **sandbox_kw)
    if old.returncode == 0:
        return {"ok": False, "reason": "the new test passes without the fix (not a regression test)", "tests": tests}
    return {"ok": True, "reason": "", "tests": tests}


def publish(repo: Path, clone: Path, commit: str, branch: str) -> str:
    """Stáhne ověřený commit z klonu do větve `branch` hlavního repa; nic neslučuje."""
    if not branch.startswith("agent-fix/") or ".." in branch or any(c in branch for c in " ~^:?*[\\"):
        raise ValueError("branch must be agent-fix/<name>")
    _git("fetch", "--quiet", "--no-tags", str(Path(clone).resolve()), f"{commit}:refs/heads/{branch}", cwd=repo)
    return _git("rev-parse", f"refs/heads/{branch}", cwd=repo).stdout.decode().strip()


def cleanup(workdir: Path) -> None:
    shutil.rmtree(workdir, ignore_errors=True)


# Příkazy pro zápisový běh: izolaci dělá bwrap, vnitřní sandbox klienta smí psát do cwd (klonu).
FIX_COMMANDS = {
    "codex": "codex exec --sandbox workspace-write --skip-git-repo-check "
             "--output-last-message {last_message} {prompt}",
    "claude-code": "claude -p {prompt} --output-format text --permission-mode acceptEdits",
}
# Přihlašovací soubory CLI, které se zkopírují do jednorázového HOME běhu; zpět se vrací jen
# obnovené přihlášení (CRED_SYNC), nikdy konfigurace — agent tak nezmění config/MCP/hooky CLI.
CLI_FILES = {"codex": (".codex/auth.json", ".codex/config.toml"),
             "claude-code": (".claude/.credentials.json", ".claude.json", ".claude/settings.json")}
CRED_SYNC = {"codex": (".codex/auth.json",), "claude-code": (".claude/.credentials.json",)}


def cli_files_for(agent: str) -> tuple[str, ...]:
    env = os.environ.get("AGENT_LEASE_FIX_CLI_FILES")
    if env is not None:
        return tuple(p for p in env.split(":") if p)
    return CLI_FILES.get(agent, ())


__all__ = ["DEFAULT_HIDE", "cleanup", "collect", "prepare", "prepare_home", "publish", "run_sandboxed",
           "runtime_paths", "sandbox_argv", "sync_credentials", "verify_regression"]
