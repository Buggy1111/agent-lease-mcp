"""
PreToolUse hook: vynutí nájem dřív, než agent smí soubor přepsat.

⚠️ Tohle je jediný důvod, proč projekt existuje. Hotová řešení mají zámky pouze
doporučující — vynucují se „na úrovni promptu", takže je agent v zápalu práce
obejde; AgentRoom si to sám dokumentuje. Claude Code i Codex ale mají PreToolUse
hooky se stejnou sémantikou (exit 2 = zablokovat, důvod na stderr), takže se to
dá vynutit deterministicky na úrovni volání nástroje. Jeden skript obslouží oba.

Slupka je schválně tenká: kontrola i převzetí nájmu je jedna transakce
(`Store.edit_gate`, APP-007), tady se jen čte stdin a vrací exit kód.

Selhání: výchozí je fail-open (rozbitá koordinace nesmí zastavit práci a z hooku
by se stala věc, kterou první vypnou). Kdo chce opak, nastaví
`AGENT_LEASE_FAIL_CLOSED=1` — pak neplatný vstup i interní chyba volání zablokují.
"""

from __future__ import annotations

import json
import os
import sys

from .broker import open_store
from .config import Settings
from .policy import decide, extract_targets, is_write_tool, script_target, shell_write_targets

EXIT_BLOCK = 2  # blokuje volání v Claude Code i v Codexu


def main() -> int:
    settings = Settings.from_env()
    closed = settings.fail_closed
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")  # noqa: TRY004
    except (json.JSONDecodeError, ValueError) as exc:
        if closed:
            print(f"[agent-lease] unreadable hook input, blocking (fail-closed): {exc}",
                  file=sys.stderr)
            return EXIT_BLOCK
        return 0  # nerozumím vstupu → pustit dál, ne blokovat naslepo

    tool, paths = extract_targets(payload)
    purpose = "auto (edit)"
    write_targets: list[str] = []

    # Spouštěný skript se chová jako zápis: po dobu běhu ho nikdo nesmí editovat.
    # Přesně tahle kombinace (jeden spouští, druhý edituje) 9.9.2026 rozbila balíček.
    if not paths:
        raw_input = payload.get("tool_input") or payload.get("toolInput") or {}
        command = raw_input.get("command") if isinstance(raw_input, dict) else None
        if isinstance(command, str):
            if found := script_target(command):
                paths, tool, purpose = [found], "Edit", "auto (running script)"
            write_targets = _in_cwd(shell_write_targets(command), payload.get("cwd"))

    if not write_targets and (not paths or not is_write_tool(tool)):
        return 0

    try:
        store = open_store(settings)
        # Zjevný zápis z příkazové řádky do cizího rozpracovaného souboru (APP-006).
        # Jen kolize se blokuje, volné cíle se do nájmu neberou (logy v /tmp apod.).
        for target in write_targets:
            holder = store.holder_of(target)
            if holder is not None and holder.agent != settings.agent:
                print(decide("Edit", target, holder, settings.agent).reason, file=sys.stderr)
                store.record(settings.agent, "blocked", target, "shell write")
                return EXIT_BLOCK
        if paths and is_write_tool(tool):
            for raw_path in paths:
                blocker = store.edit_gate(raw_path, settings.agent, purpose=purpose)
                if blocker is not None:
                    print(decide(tool, raw_path, blocker, settings.agent).reason, file=sys.stderr)
                    return EXIT_BLOCK
    except Exception as exc:  # noqa: BLE001
        if closed:
            print(f"[agent-lease] hook failed, blocking (fail-closed): {exc}", file=sys.stderr)
            return EXIT_BLOCK
        print(f"[agent-lease] hook failed, letting the call through: {exc}", file=sys.stderr)

    return 0


def _in_cwd(paths: list[str], cwd: object) -> list[str]:
    """Relativní cesty z příkazu patří ke cwd agenta, ne ke cwd hooku."""
    if not isinstance(cwd, str) or not cwd:
        return paths
    return [p if p.startswith(("/", "~")) else os.path.join(cwd, p) for p in paths]


if __name__ == "__main__":
    sys.exit(main())
