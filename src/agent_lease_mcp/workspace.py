"""
Otisk pracovní kopie pro checkpoint (fáze A6, review Noxe #550/4).

`dirty` je hash OBSAHU rozpracovaných změn (staged i unstaged diff vůči HEAD)
a obsahu nesledovaných souborů — ne jen `git status`, který je pro dvě různé
verze téhož souboru stejný. Když stav nejde zjistit (mimo git, chyba), vrací
`UNKNOWN`, a shoda se pak nikdy nehlásí.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

UNKNOWN = "unknown"


def _git(cwd: Path, *argv: str, env: dict | None = None) -> bytes | None:
    try:
        out = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, timeout=20,
                             check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def workspace_state(cwd: str | Path) -> tuple[str, str]:
    cwd = Path(cwd)
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if root is None:
        return UNKNOWN, UNKNOWN
    try:
        cwd = Path(os.fsdecode(root.rstrip(b"\n"))).resolve(strict=True)
    except OSError:
        return UNKNOWN, UNKNOWN
    head = _git(cwd, "rev-parse", "HEAD")
    staged = _git(cwd, "diff", "--cached", "--binary")
    diff = _git(cwd, "diff", "--binary")
    index = _git(cwd, "ls-files", "--stage", "-z")
    untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    if any(value is None for value in (root, head, staged, diff, index, untracked)):
        return UNKNOWN, UNKNOWN
    # A submodule can contain arbitrary changes invisible to the parent diff.
    if any(entry.startswith(b"160000 ") for entry in index.split(b"\0")):
        return UNKNOWN, UNKNOWN
    h = hashlib.sha256()
    for part in (os.fsencode(cwd), staged, diff):
        h.update(len(part).to_bytes(8, "big"))
        h.update(part)
    for name in sorted(p for p in untracked.split(b"\0") if p):
        h.update(b"\0" + name + b"\0")
        try:
            path = cwd / os.fsdecode(name)
            info = path.lstat()
            h.update(str(info.st_mode).encode())
            if stat.S_ISLNK(info.st_mode):
                h.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(info.st_mode):
                h.update(path.read_bytes())
            else:
                return UNKNOWN, UNKNOWN
        except OSError:
            return UNKNOWN, UNKNOWN
    return head.decode().strip(), h.hexdigest()[:32]


def matches(cp: dict, head: str, dirty: str) -> bool:
    """Shoda jen při plně známém stavu na obou stranách."""
    return (UNKNOWN not in (head, dirty, cp.get("git_head"), cp.get("dirty"))
            and cp.get("git_head") == head and cp.get("dirty") == dirty)


# Klíče, přes které by konfigurace repa spustila program (git status volá clean filtry).
# core.fsmonitor tu není: přebíjí ho `-c core.fsmonitor=false` a `git config` by jinak
# vypsal i ten přepínač z příkazové řádky (každé repo by vypadalo podezřele).
_EXEC_CONFIG = r"^(filter\..*\.(clean|smudge|process)|diff\..*\.(command|textconv))$"


def _hardened_env() -> dict:
    """Jen lokální konfigurace repa: globální a systémová (a jejich filtry) se nečtou."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
               GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="")
    return env


def _git_hardened(cwd: Path, *argv: str) -> bytes | None:
    """git bez kódu z konfigurace (fsmonitor, hooky, globální config) a bez zámků indexu."""
    return _git(cwd, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                "--no-optional-locks", "--no-replace-objects", *argv, env=_hardened_env())


def _repo_runs_programs(top: Path) -> bool:
    """Definuje repo filtr nebo diff program? Pak `status` nespouštět (mohl by ho zavolat)."""
    # Účinná konfigurace repa včetně include/includeIf (bez --includes by filtr ve
    # vloženém souboru prošel). Globální a systémovou vypíná _hardened_env.
    # --list rozliší prázdnou konfiguraci od chyby čtení (get-regexp vrací 1 i bez shod).
    keys = _git_hardened(top, "config", "--includes", "--name-only", "--null", "--list")
    return keys is None or any(
        re.fullmatch(_EXEC_CONFIG, os.fsdecode(key), re.IGNORECASE)
        for key in keys.split(b"\0") if key
    )


def resolve_commit(cwd: str | Path, rev: str = "HEAD") -> tuple[str, str, bool]:
    """
    Kořen repa, plný hash commitu `rev` a jestli jsou v kopii necommitnuté změny.
    Review (B3) se váže na commit, ne na rozpracované soubory — proto to varování.
    Volat jen s vlastním pracovním adresářem, nikdy s cestou ze sdílené DB.
    """
    cwd = Path(cwd)
    root = _git_hardened(cwd, "rev-parse", "--show-toplevel")
    if root is None:
        raise ValueError(f"{cwd} is not inside a git repository")
    top = Path(os.fsdecode(root.rstrip(b"\n"))).resolve()
    if rev.startswith("-"):
        raise ValueError(f"invalid revision {rev!r}")
    sha = _git_hardened(top, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}")
    if sha is None:
        raise ValueError(f"{rev!r} is not a commit in {top}")
    if _repo_runs_programs(top):
        # Necommitnuté změny bez spuštění cizích filtrů zjistit nejde; raději varovat.
        return str(top), sha.decode().strip(), True
    index = _git_hardened(top, "ls-files", "--stage", "-z")
    if index is None or any(entry.startswith(b"160000 ") for entry in index.split(b"\0")):
        # Status by vstoupil do submodulu, jehož vlastní config může spustit filtr.
        # Ignorování submodulů by naopak skrylo jejich rozpracované změny.
        return str(top), sha.decode().strip(), True
    status = _git_hardened(top, "status", "--porcelain", "--untracked-files=normal")
    return str(top), sha.decode().strip(), status is None or bool(status.strip())
