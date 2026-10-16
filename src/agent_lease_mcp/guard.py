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
import os
import sys

from .config import Settings
from .models import Verdict
from .policy import decide, extract_target, script_target, shell_write_targets
from .store import Store

EXIT_BLOCK = 2  # blokuje volání v Claude Code i v Codexu


def _fail_closed() -> bool:
    """
    `AGENT_LEASE_FAIL_CLOSED=1` (APP-008): při chybě hooku blokovat místo
    pouštění. Výchozí zůstává fail-open — pro běžnou práci je rozbitá
    koordinace menší zlo než zastavená práce. Kdo pouští agenty bez dozoru
    na sdílené věci, ať si zapne tohle.
    """
    return os.environ.get("AGENT_LEASE_FAIL_CLOSED", "").lower() in ("1", "true", "yes")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        if _fail_closed():
            print("[agent-lease] unreadable hook input, blocking (fail-closed)", file=sys.stderr)
            return EXIT_BLOCK
        return 0  # nerozumím vstupu → pustit dál, ne blokovat naslepo
    if not isinstance(payload, dict):
        payload = {}

    settings = Settings.from_env()
    tool, raw_path = extract_target(payload)

    # Spouštěný skript se chová jako zápis: po dobu běhu ho nikdo nesmí editovat.
    # Přesně tahle kombinace (jeden spouští, druhý edituje) 9.9.2026 rozbila balíček.
    write_targets: list[str] = []
    if raw_path is None:
        raw_input = payload.get("tool_input") or payload.get("toolInput") or {}
        command = raw_input.get("command") if isinstance(raw_input, dict) else None
        if isinstance(command, str):
            if found := script_target(command):
                raw_path, tool = found, "Edit"  # posuzuj jako zápis
            write_targets = _in_cwd(shell_write_targets(command), payload.get("cwd"))

    try:
        store = Store(settings=settings)
        # Zápis z příkazové řádky do cizího rozpracovaného souboru (APP-006).
        # Jen kolize se blokuje, volné cíle se do nájmu neberou.
        for target in write_targets:
            holder = store.holder_of(target)
            if holder and holder.agent != settings.agent:
                denied = decide("Edit", target, holder, settings.agent)
                store.record(settings.agent, "blocked", denied.path or "", "shell write")
                print(denied.reason, file=sys.stderr)
                return EXIT_BLOCK

        holder = store.holder_of(raw_path) if raw_path else None
        decision = decide(tool, raw_path, holder, settings.agent)

        if decision.verdict is Verdict.ALLOW and holder and holder.agent == settings.agent:
            # Vlastní nájem při každém zápisu atomicky obnovit. Jinak by mohl mezi
            # kontrolou a editací vypršet a převzít ho druhý (Nox, review 16. 10.).
            renewed = store.claim([holder.path], agent=settings.agent, purpose=holder.purpose)
            if not renewed.ok:
                decision = decide(tool, raw_path, renewed.conflicts[0], settings.agent)

        if decision.verdict is Verdict.AUTO_CLAIMED and decision.path:
            # Volný soubor si bereme sami — na explicitní `claim` se pak nedá
            # zapomenout a protokol nejde nechtěně obejít.
            result = store.claim([decision.path], agent=settings.agent, purpose="auto (edit)")
            if not result.ok:
                # Mezi `holder_of` a `claim` byl rychlejší někdo jiný. Rozhoduje
                # atomický claim, ne dřívější čtení — jinak projdou oba (APP-007).
                decision = decide(tool, raw_path, result.conflicts[0], settings.agent)

        if decision.blocks:
            store.record(settings.agent, "blocked", decision.path or "", tool)
            print(decision.reason, file=sys.stderr)
            return EXIT_BLOCK
    except Exception as exc:  # noqa: BLE001
        # ⚠️ Fail-open schválně. Rozbitá koordinace nesmí zastavit práci — z
        # pomocníka by se stala překážka a první, co by kdokoli udělal, je vypnout
        # hook. Selhání je vidět na stderr.
        if _fail_closed():
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
