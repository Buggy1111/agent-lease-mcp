"""Zápisový běh opravy v izolaci (fáze C1b) — DŮVĚRYHODNÁ strana, žádná logika od agenta.

Odpověď na NO-GO z C0 (docs/research/2026-11-12-c0-codex-worktree.md): přepínač
`--sandbox workspace-write` klienta nestačil (zápis do /tmp mimo worktree, čtení mimo
repo, závislost na sdíleném `.git`). Proto vlastní obal z OS prostředků:

1. **Samostatný klon** (`prepare`): vlastní `.git` s vlastními metadaty, nikdy sdílený
   git common dir ani worktree hlavního repa. Hooky vypnuté, klon na přesném základu.
2. **bwrap** (`sandbox_argv`): celý systém jen pro čtení, zapisovat jde JEN do
   pracovních souborů klonu. `.git` klonu je pro agenta jen pro čtení (žádná změna
   config/hooks/refs → žádné spuštění kódu v důvěryhodném procesu přes git config),
   `/tmp` je soukromý tmpfs, stav agent-lease (`webui.token`, `room.db`) a běžná
   úložiště tajemství jsou překrytá prázdným tmpfs. Zápis do vlastního stavu CLI
   (přihlášení, relace) je jediná výslovná výjimka (`cli_state`).
3. **Vyhodnocení** (`collect`): důvěryhodný proces po skončení agenta zkontroluje
   změny (jen povolené cesty, žádné symlinky ani speciální soubory, limity velikosti,
   kritické soubory CI vždy zamítne), projde diff secret scanem a teprve pak sám
   commitne pevnou identitou s `--no-verify`.
4. **Předání** (`publish`): hlavní repo si commit jen stáhne (`fetch`) do větve
   `agent-fix/<id>`; nic se neslučuje, nemění se pracovní kopie ani jiné větve.

Síť zůstává (CLI potřebuje API modelu). Sloučení větve schvaluje člověk.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath

from .secret_scan import scan_diff

BWRAP = "/usr/bin/bwrap"
MAX_FILES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DIFF_BYTES = 8 * 1024 * 1024
# Soubory, které mění chování CI, gitu nebo dodavatelského řetězce — oprava je nesmí měnit.
CRITICAL = (".github/", ".gitlab-ci.yml", ".gitmodules", ".gitattributes", ".pre-commit-config.yaml",
            ".git/", "deploy/")
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


def sandbox_argv(clone: Path, argv: list[str], *, home: Path | None = None,
                 cli_state: tuple[str, ...] = (), hide: tuple[str, ...] = DEFAULT_HIDE,
                 extra_rw: tuple[str, ...] = ()) -> list[str]:
    """Příkaz, který spustí `argv` v izolaci: zapisovat smí jen do pracovních souborů klonu."""
    clone = Path(clone).resolve()
    home = Path(home or Path.home())
    args = [BWRAP, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
            "--unshare-uts", "--unshare-cgroup-try",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for rel in hide:
        target = home / rel
        if target.is_dir():
            args += ["--tmpfs", str(target)]
        elif target.exists():
            args += ["--ro-bind", "/dev/null", str(target)]
    for rel in cli_state:  # výslovná výjimka: CLI si musí zapsat vlastní relaci/přihlášení
        target = home / rel
        if target.exists():
            args += ["--bind", str(target), str(target)]
    for path in extra_rw:  # např. soubor pro poslední odpověď CLI, který čte bridge
        args += ["--bind", str(path), str(path)]
    args += ["--bind", str(clone), str(clone), "--ro-bind", str(clone / ".git"), str(clone / ".git"),
             "--chdir", str(clone), "--setenv", "HOME", str(home), "--"]
    return args + list(argv)


def run_sandboxed(clone: Path, argv: list[str], *, timeout: int = 1800, **kw) -> subprocess.CompletedProcess:
    if not Path(BWRAP).exists():
        raise RuntimeError("bubblewrap is required for write-mode fixes (fail closed)")
    return subprocess.run(sandbox_argv(clone, argv, **kw), capture_output=True, timeout=timeout, check=False)


def _allowed(path: str, allowed: list[str]) -> bool:
    return not allowed or any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed)


def _critical(path: str) -> bool:
    return any(path == c.rstrip("/") or path.startswith(c) for c in CRITICAL)


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
# Vlastní stav CLI (přihlášení, relace) — jediný povolený zápis mimo klon.
CLI_STATE = {"codex": (".codex",), "claude-code": (".claude", ".claude.json")}


def cli_state_for(agent: str) -> tuple[str, ...]:
    import os
    env = os.environ.get("AGENT_LEASE_FIX_CLI_STATE")
    if env is not None:
        return tuple(p for p in env.split(":") if p)
    return CLI_STATE.get(agent, ())


__all__ = ["CRITICAL", "DEFAULT_HIDE", "cleanup", "collect", "prepare", "publish", "run_sandboxed", "sandbox_argv"]
