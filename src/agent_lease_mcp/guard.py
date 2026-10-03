"""
PreToolUse hook: vynutí nájem dřív, než agent smí soubor přepsat.

⚠️ Tohle je jediný důvod, proč projekt existuje. Hotová řešení mají zámky pouze
doporučující — vynucují se „na úrovni promptu", takže je agent v zápalu práce
obejde; AgentRoom si to sám dokumentuje. Claude Code i Codex ale mají PreToolUse
hooky se stejnou sémantikou (exit 2 = zablokovat, důvod na stderr), takže se to
dá vynutit deterministicky na úrovni volání nástroje. Jeden skript obslouží oba.

Slupka je schválně tenká: kontrola i převzetí nájmu je jedna transakce
(`Store.edit_gate`), tady se jen čte stdin a vrací exit kód.

Selhání: výchozí je fail-open (rozbitá koordinace nesmí zastavit práci a z hooku
by se stala věc, kterou první vypnou). Kdo chce opak, nastaví
`AGENT_LEASE_FAIL_CLOSED=1` — pak neplatný vstup i interní chyba volání zablokují.
"""

from __future__ import annotations

import json
import sys

from .config import Settings
from .policy import decide, extract_targets, is_write_tool, script_target
from .store import Store

EXIT_BLOCK = 2  # blokuje volání v Claude Code i v Codexu


def main() -> int:
    settings = Settings.from_env()
    closed = settings.fail_closed
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload není objekt")
    except (json.JSONDecodeError, ValueError) as exc:
        if closed:
            print(f"[agent-lease] nečitelný vstup hooku, blokuji (fail-closed): {exc}",
                  file=sys.stderr)
            return EXIT_BLOCK
        return 0  # nerozumím vstupu → pustit dál, ne blokovat naslepo

    tool, paths = extract_targets(payload)

    # Spouštěný skript se chová jako zápis: po dobu běhu ho nikdo nesmí editovat.
    # Přesně tahle kombinace (jeden spouští, druhý edituje) 9.9.2026 rozbila balíček.
    if not paths:
        raw_input = payload.get("tool_input") or payload.get("toolInput") or {}
        command = raw_input.get("command") if isinstance(raw_input, dict) else None
        if isinstance(command, str) and (found := script_target(command)):
            paths, tool = [found], "Edit"  # posuzuj jako zápis

    if not paths or not is_write_tool(tool):
        return 0

    try:
        store = Store(settings=settings)
        for raw_path in paths:
            blocker = store.edit_gate(raw_path, settings.agent)
            if blocker is not None:
                decision = decide(tool, raw_path, blocker, settings.agent)
                print(decision.reason, file=sys.stderr)
                return EXIT_BLOCK
    except Exception as exc:  # noqa: BLE001
        if closed:
            print(f"[agent-lease] hook selhal, blokuji (fail-closed): {exc}", file=sys.stderr)
            return EXIT_BLOCK
        print(f"[agent-lease] hook selhal, pouštím dál: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
