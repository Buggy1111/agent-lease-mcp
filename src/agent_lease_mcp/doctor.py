"""`agent-lease doctor` — ověří, že koordinace opravdu funguje, ne že je jen nainstalovaná.

Tichá porucha je u tohoto projektu nejhorší (viz README: Codex bez `writable_roots`
se do místnosti vůbec nedostane a nikdo si toho nevšimne). Doctor proto zkouší
skutečný zápis, ne jen existenci souborů.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

from .config import Settings, is_trusted_agent

OK, WARN, FAIL = "✔", "⚠", "✘"


def _line(level: str, text: str) -> None:
    print(f" {level} {text}")


def _hooks_mention(path: Path, needle: str) -> bool | None:
    if not path.exists():
        return None
    try:
        return needle in json.dumps(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None


def run_doctor() -> int:
    settings = Settings.from_env()
    problems = 0
    print("agent-lease doctor\n")

    if is_trusted_agent(settings.agent):
        _line(OK, f"agent identity: {settings.agent}")
    else:
        problems += 1
        _line(FAIL, f"AGENT_NAME is not set (you are {settings.agent}) — hook and server will disagree")

    db = settings.db_path
    try:
        db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=db.parent, prefix=".doctor-"):
            pass
        _line(OK, f"writing to {db.parent} works")
    except OSError as exc:
        problems += 1
        _line(FAIL, f"cannot write to {db.parent}: {exc} "
                    "(Codex sandbox? see README → writable_roots, or use the broker socket)")

    if db.exists():
        mode = stat.S_IMODE(db.stat().st_mode)
        if mode & 0o077:
            problems += 1
            _line(FAIL, f"DB mode is {oct(mode)}, should be 0600 (chmod 600 {db})")
        else:
            _line(OK, "DB mode 0600")

    ver = sqlite3.sqlite_version_info
    if (3, 7, 0) <= ver < (3, 44, 6) or (3, 45, 0) <= ver < (3, 50, 7) or (3, 51, 0) <= ver < (3, 51, 3):
        _line(WARN, f"SQLite {sqlite3.sqlite_version} has a known WAL-reset bug (fixed in 3.51.3 / 3.50.7 / 3.44.6)")
    else:
        _line(OK, f"SQLite {sqlite3.sqlite_version}")

    home = Path.home()
    for label, path, needle in (
        ("Claude Code", home / ".claude" / "settings.json", "agent-lease-guard"),
        ("Codex", home / ".codex" / "hooks.json", "agent-lease-guard"),
    ):
        found = _hooks_mention(path, needle)
        if found is None:
            _line(WARN, f"{label}: {path} not found / unreadable — guard hook not verified")
        elif found:
            _line(OK, f"{label}: guard hook set")
        else:
            problems += 1
            _line(FAIL, f"{label}: agent-lease-guard missing in {path} (without it this is only a notice board)")
        if found:
            ctx = _hooks_mention(path, "agent-lease-context")
            rewake = _hooks_mention(path, "--rewake")
            ctx_msg = "set" if ctx else "MISSING → no 'seen' receipts"
            rw_msg = "set" if rewake else "not set (a message waits for the next prompt)"
            _line(OK if ctx else WARN, f"{label}: message injection (agent-lease-context) {ctx_msg}")
            _line(OK if rewake else WARN, f"{label}: waking a sleeping session (wait --rewake) {rw_msg}")

    for binary in ("codex", "claude"):
        found_bin = shutil.which(binary)
        _line(OK if found_bin else WARN,
              f"{binary} on PATH" if found_bin else f"{binary} not on PATH (no bridge for it)")

    if platform.system() == "Linux" and "microsoft" in platform.release().lower():
        _line(WARN, "WSL: systemd services do not survive a Windows restart — "
                    "see deploy/windows/start-bridge.ps1")
    if shutil.which("systemctl") and os.path.exists("/run/systemd/system"):
        _line(OK, "systemd available (deploy/systemd/*.service)")

    from .broker import default_socket_path, ping
    from .store import Store

    sock = default_socket_path(settings)
    _line(OK if ping(sock) else WARN,
          f"broker running ({sock})" if ping(sock) else "broker not running (tools write to SQLite directly)")
    if settings.fallbacks:
        _line(OK, f"failover chains: {settings.fallbacks}")
    else:
        _line(WARN, "AGENT_LEASE_FALLBACKS not set — when an agent hits its limit, its queue waits")
    try:
        for lim in Store(settings=settings).limits():
            _line(WARN, f"{lim['agent']} is limited for {lim['seconds_left'] // 60} more min")
    except Exception:  # noqa: BLE001, S110
        pass
    jev_on = os.environ.get("AGENT_LEASE_JEV", "") in ("1", "true", "yes")
    _line(OK if jev_on else WARN, "Jev on" if jev_on else "Jev off (optional, docs/JEV.md)")

    print()
    if problems:
        print(f"{problems} problem(s) to fix.")
        return 1
    print("Looks good.")
    return 0


if __name__ == "__main__":
    sys.exit(run_doctor())
