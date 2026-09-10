"""
PreToolUse hook: vynutí nájem dřív, než agent smí soubor přepsat.

⚠️ Tohle je jediný důvod, proč projekt existuje. Hotová řešení mají zámky pouze
doporučující — vynucují se „na úrovni promptu", takže je agent v zápalu práce
obejde; AgentRoom si to sám dokumentuje. Claude Code i Codex ale mají PreToolUse
hooky se stejnou sémantikou (exit 2 = zablokovat, důvod na stderr), takže se to
dá vynutit deterministicky na úrovni volání nástroje. Jeden skript obslouží oba.

Slupka je schválně tenká: rozhoduje `policy.decide()`, tady se jen čte stdin,
zapisuje důsledek a vrací exit kód.
"""

from __future__ import annotations

import json
import sys

from .config import Settings
from .models import Verdict
from .policy import decide, extract_target
from .store import Store

EXIT_BLOCK = 2  # blokuje volání v Claude Code i v Codexu


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # nerozumím vstupu → pustit dál, ne blokovat naslepo

    settings = Settings.from_env()
    tool, raw_path = extract_target(payload)

    try:
        store = Store(settings=settings)
        holder = store.holder_of(raw_path) if raw_path else None
        decision = decide(tool, raw_path, holder, settings.agent)

        if decision.verdict is Verdict.AUTO_CLAIMED and decision.path:
            # Volný soubor si bereme sami — na explicitní `claim` se pak nedá
            # zapomenout a protokol nejde nechtěně obejít.
            store.claim([decision.path], agent=settings.agent, purpose="auto (edit)")

        if decision.blocks:
            store.record(settings.agent, "blocked", decision.path or "", tool)
            print(decision.reason, file=sys.stderr)
            return EXIT_BLOCK
    except Exception as exc:  # noqa: BLE001
        # ⚠️ Fail-open schválně. Rozbitá koordinace nesmí zastavit práci — z
        # pomocníka by se stala překážka a první, co by kdokoli udělal, je vypnout
        # hook. Selhání je vidět na stderr.
        print(f"[agent-lease] hook selhal, pouštím dál: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
