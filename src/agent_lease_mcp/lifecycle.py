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

from . import jev
from .briefing import prompt_update, session_briefing
from .broker import open_store
from .config import Settings
from .hygiene import heuristic_risk


def _emit(event: str, text: str) -> None:
    if not text:
        return  # prázdno = nic nevstřikovat, ať se z toho nestane šum
    json.dump(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
        sys.stdout,
        ensure_ascii=False,
    )


QUARANTINE_TEXT = "[zadrženo: možný pokus o ovládnutí (riziko {risk:.0%}) — čeká na posouzení člověka]"


def _screen(store, messages: list[dict]) -> list[dict]:
    """
    Screening cizích zpráv před vložením do kontextu agenta.

    1. Deterministický předfiltr (`heuristic_risk`) běží VŽDY — nezávisí na modelu.
    2. Je-li zapnutý Jev, přidá riziko z atomických otázek a odhad „vyžaduje akci"
       (informativní chat se pak nepřipomíná).
    Podezřelý text se v kontextu nahradí upozorněním a člověk dostane zprávu;
    originál zůstává v místnosti. Zprávy od člověka se neposuzují. Nízké riziko
    NENÍ záruka bezpečí — text je dál označený jako nedůvěryhodná citace.
    """
    out = []
    for m in messages:
        if m["agent"] in ("michal", "broker"):
            out.append(m)
            continue
        cached = store.get_screen(m["id"])
        if cached is None:
            risk = heuristic_risk(m["text"])
            actionable = None
            if jev.enabled():
                t = jev.triage(m["text"], m["agent"])
                if t.risk is not None:
                    risk = max(risk, t.risk)
                actionable = t.actionable
            store.set_screen(m["id"], risk, actionable)
            cached = {"risk": risk, "actionable": actionable}
            if risk >= jev.RISK_THRESHOLD:
                store.send("broker", "michal",
                           f"⚠ Zpráva #{m['id']} od {m['agent']} zadržena (riziko {risk:.0%}).",
                           kind="result", reply_to=m["id"])
        if cached["risk"] >= jev.RISK_THRESHOLD:
            m = {**m, "text": QUARANTINE_TEXT.format(risk=cached["risk"])}
        out.append(m)
    return out


def context_main() -> int:
    """SessionStart / UserPromptSubmit."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    event = payload.get("hook_event_name") or payload.get("hookEventName") or "SessionStart"
    settings = Settings.from_env()

    try:
        store = open_store(settings)
        me = settings.agent
        store.failover_sweep()
        messages = _screen(store, store.undelivered(me))
        # Příjem se zapíše AŽ po úspěšném sestavení textu (viz níže), ale `awaiting`
        # musí zahrnout i zprávy, které přišly právě teď — proto ho počítáme po
        # `mark_seen`, ne před ním.
        store.mark_seen(me, [m["id"] for m in messages if m["recipient"] == me])
        awaiting = store.awaiting_accept(me)

        if event == "SessionStart":
            store.heartbeat(me, status="start session")
            text = session_briefing(me, store.peers(), store.claims(), messages, awaiting)
        else:
            # Ohlásit, že žiju, ale NEpřepsat status — ten patří `room(status=…)`.
            # Bez tohohle agent, který jede jen přes hooky (Codex nemá MCP), po
            # 15 minutách práce zmizí z místnosti jako neaktivní, i když maká;
            # druhý ho pak přestane brát v potaz. Přesně to se stalo 10.9.2026.
            store.heartbeat(me)
            text = prompt_update(me, store.claims(), messages, awaiting)

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
        released = open_store(settings).release_all(settings.agent)
        if released:
            print(f"[agent-lease] uvolněno {len(released)} nájmů", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[agent-lease] uvolnění selhalo: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(context_main())
