"""
Otisk pracovní kopie pro checkpoint (fáze A6, review Noxe #550/4).

`dirty` je hash OBSAHU rozpracovaných změn (staged i unstaged diff vůči HEAD)
a obsahu nesledovaných souborů — ne jen `git status`, který je pro dvě různé
verze téhož souboru stejný. Když stav nejde zjistit (mimo git, chyba), vrací
`UNKNOWN`, a shoda se pak nikdy nehlásí.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

UNKNOWN = "unknown"


def _git(cwd: Path, *argv: str) -> bytes | None:
    try:
        out = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, timeout=20,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def workspace_state(cwd: str | Path) -> tuple[str, str]:
    cwd = Path(cwd)
    head = _git(cwd, "rev-parse", "HEAD")
    diff = _git(cwd, "diff", "HEAD", "--binary")
    untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    if head is None or diff is None or untracked is None:
        return UNKNOWN, UNKNOWN
    h = hashlib.sha256(diff)
    for name in sorted(p for p in untracked.split(b"\0") if p):
        h.update(b"\0" + name + b"\0")
        try:
            h.update((cwd / name.decode("utf-8", "surrogateescape")).read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return head.decode().strip(), h.hexdigest()[:32]


def matches(cp: dict, head: str, dirty: str) -> bool:
    """Shoda jen při plně známém stavu na obou stranách."""
    return (UNKNOWN not in (head, dirty, cp.get("git_head"), cp.get("dirty"))
            and cp.get("git_head") == head and cp.get("dirty") == dirty)
