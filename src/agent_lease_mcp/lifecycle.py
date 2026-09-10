"""
Hooky, které drží koordinaci naživu BEZ uživatele u klávesnice.

- `context_main`  (SessionStart, UserPromptSubmit) — vstříkne stav místnosti
  a nedoručené vzkazy do kontextu. Agent se tedy nemusí ptát; dozví se to sám.
- `release_main`  (Stop, SessionEnd) — vrátí všechny nájmy, jakmile agent dotáhne
  tah. Bez tohohle by soubory zůstaly zamčené až do vypršení TTL, i když je nikdo
  nedrží, a druhý agent by zbytečně čekal.

Obojí je fail-open a mlčí, když není co říct.
"""

from __future__ import annotations

import json
import sys

from .briefing import prompt_update, session_briefing
from .config import Settings
from .store import Store


def _emit(event: str, text: str) -> None:
    if not text:
        return  # prázdno = nic nevstřikovat, ať se z toho nestane šum
    json.dump(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
        sys.stdout,
        ensure_ascii=False,
    )


def context_main() -> int:
    """SessionStart / UserPromptSubmit."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    event = payload.get("hook_event_name") or payload.get("hookEventName") or "SessionStart"
    settings = Settings.from_env()

    try:
        store = Store(settings=settings)
        me = settings.agent
        messages = store.undelivered(me)

        if event == "SessionStart":
            store.heartbeat(me, status="start session")
            text = session_briefing(me, store.peers(), store.claims(), messages)
        else:
            text = prompt_update(me, store.claims(), messages)

        if messages:
            store.set_cursor(me, messages[-1]["id"])
        _emit(event, text)
    except Exception as exc:  # noqa: BLE001
        print(f"[agent-lease] kontext se nepodařilo načíst: {exc}", file=sys.stderr)
    return 0


def release_main() -> int:
    """
    Stop / SessionEnd — vrátit všechno, co agent držel.

    ⚠️ Tohle je hlavní důvod, proč nájmy nemusí mít dlouhé TTL. Agent, který
    dokončil tah, už soubory nepotřebuje; držet je „pro jistotu" jen blokuje
    druhého. TTL zůstává jako pojistka pro případ, že se tenhle hook nespustí
    (tvrdý pád, kill -9).
    """
    settings = Settings.from_env()
    try:
        released = Store(settings=settings).release_all(settings.agent)
        if released:
            print(f"[agent-lease] uvolněno {len(released)} nájmů", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[agent-lease] uvolnění selhalo: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(context_main())
