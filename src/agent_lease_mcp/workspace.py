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
import stat
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
