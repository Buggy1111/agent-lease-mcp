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
        _line(OK, f"identita agenta: {settings.agent}")
    else:
        problems += 1
        _line(FAIL, f"AGENT_NAME není nastavené (jsi {settings.agent}) — hook a server se rozejdou")

    db = settings.db_path
    try:
        db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=db.parent, prefix=".doctor-"):
            pass
        _line(OK, f"zápis do {db.parent} funguje")
    except OSError as exc:
        problems += 1
        _line(FAIL, f"nelze zapisovat do {db.parent}: {exc} "
                    "(Codex sandbox? viz README → writable_roots, nebo použij broker socket)")

    if db.exists():
        mode = stat.S_IMODE(db.stat().st_mode)
        if mode & 0o077:
            problems += 1
            _line(FAIL, f"DB má práva {oct(mode)}, má být 0600 (chmod 600 {db})")
        else:
            _line(OK, "práva DB 0600")

    ver = sqlite3.sqlite_version_info
    if (3, 7, 0) <= ver < (3, 44, 6) or (3, 45, 0) <= ver < (3, 50, 7) or (3, 51, 0) <= ver < (3, 51, 3):
        _line(WARN, f"SQLite {sqlite3.sqlite_version} má známou WAL-reset chybu (opraveno 3.51.3 / 3.50.7 / 3.44.6)")
    else:
        _line(OK, f"SQLite {sqlite3.sqlite_version}")

    home = Path.home()
    for label, path, needle in (
        ("Claude Code", home / ".claude" / "settings.json", "agent-lease-guard"),
        ("Codex", home / ".codex" / "hooks.json", "agent-lease-guard"),
    ):
        found = _hooks_mention(path, needle)
        if found is None:
            _line(WARN, f"{label}: {path} nenalezen / nečitelný — guard hook neověřen")
        elif found:
            _line(OK, f"{label}: guard hook nastaven")
        else:
            problems += 1
            _line(FAIL, f"{label}: v {path} chybí agent-lease-guard (bez něj je to jen nástěnka)")
        if found:
            ctx = _hooks_mention(path, "agent-lease-context")
            rewake = _hooks_mention(path, "--rewake")
            ctx_msg = "nastaveno" if ctx else "CHYBÍ → nedojde k potvrzení 'viděl'"
            rw_msg = "nastaveno" if rewake else "nenastaveno (zpráva počká do dalšího promptu)"
            _line(OK if ctx else WARN, f"{label}: vkládání vzkazů (agent-lease-context) {ctx_msg}")
            _line(OK if rewake else WARN, f"{label}: probuzení spící session (wait --rewake) {rw_msg}")

    for binary in ("codex", "claude"):
        found_bin = shutil.which(binary)
        _line(OK if found_bin else WARN,
              f"{binary} v PATH" if found_bin else f"{binary} není v PATH (bridge pro něj nepůjde)")

    if platform.system() == "Linux" and "microsoft" in platform.release().lower():
        _line(WARN, "WSL: systemd služby instanci neudrží naživu po restartu Windows — "
                    "viz deploy/windows/start-bridge.ps1")
    if shutil.which("systemctl") and os.path.exists("/run/systemd/system"):
        _line(OK, "systemd dostupný (deploy/systemd/*.service)")

    print()
    if problems:
        print(f"{problems} problém(ů) k opravě.")
        return 1
    print("Vypadá to dobře.")
    return 0


if __name__ == "__main__":
    sys.exit(run_doctor())
