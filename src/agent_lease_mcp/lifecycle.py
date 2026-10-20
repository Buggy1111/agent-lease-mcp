"""
Hooky, které drží koordinaci naživu BEZ uživatele u klávesnice.

- `context_main`  (SessionStart, UserPromptSubmit) — vstříkne stav místnosti
  a nedoručené vzkazy do kontextu. Agent se tedy nemusí ptát; dozví se to sám.
- `rewake_wait`   (`agent-lease wait --rewake`, async Stop/SessionStart hook) — spí bez tokenů a probudí
  session, když jí přijde adresná zpráva (viz `rewake_wait`).
- `release_main`  (Stop, SessionEnd) — vrátí všechny nájmy, jakmile agent dotáhne
  tah. Bez tohohle by soubory zůstaly zamčené až do vypršení TTL, i když je nikdo
  nedrží, a druhý agent by zbytečně čekal.

Obojí je fail-open a mlčí, když není co říct.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import jev
from .briefing import prompt_update, session_briefing
from .broker import open_store
from .config import Settings, human_name
from .hygiene import heuristic_risk


def _emit(event: str, text: str) -> None:
    if not text:
        return  # prázdno = nic nevstřikovat, ať se z toho nestane šum
    json.dump(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
        sys.stdout,
        ensure_ascii=False,
    )


QUARANTINE_TEXT = "[held back: possible attempt to take control (risk {risk:.0%}) — waiting for the human to review]"


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
    human = human_name(store.db_path)
    for m in messages:
        if m["agent"] in (human, "broker"):
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
                store.send("broker", human,
                           f"⚠ Message #{m['id']} from {m['agent']} held back (risk {risk:.0%}).",
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
        print(f"[agent-lease] could not load room context: {exc}", file=sys.stderr)
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
            print(f"[agent-lease] released {len(released)} lease(s)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[agent-lease] release failed: {exc}", file=sys.stderr)
    return 0


# ── probuzení spící session (asyncRewake) ────────────────────────────────────

def is_receipt(m: dict) -> bool:
    """
    Automatické příjemky — nikoho nebudí. Uznává jen přesný tvar, který generuje
    store: broker „✓ <kdo> saw #N (…)" a adresátovo „✔ taking #N[: poznámka]",
    obojí s `reply_to == N`. Běžný result, který náhodou začíná stejnými slovy
    („✔ taking #42 dokončeno…"), je zpráva a budí (review Nox, 20. 10. 2026).
    """
    if m.get("kind") != "result" or m.get("reply_to") is None:
        return False
    text = str(m.get("text") or "")
    ref = f"#{m['reply_to']}"
    if m.get("agent") == "broker":
        return text.startswith("✓ ") and f" saw {ref} (" in text
    taking = f"✔ taking {ref}"
    return text == taking or text.startswith(taking + ": ")


def wakes(m: dict, me: str) -> bool:
    """Budí jen adresná zpráva od někoho jiného, která není příjemka. Broadcast ne."""
    return m.get("recipient") == me and m.get("agent") != me and not is_receipt(m)


_SHELLS = {"sh", "bash", "dash", "zsh", "ash", "busybox"}


def _proc_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # pole za „(comm)" — comm může obsahovat mezery i závorky
    return stat.rsplit(")", 1)[1].split()[19]


def _proc_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _proc_ppid(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def owner_watch() -> Callable[[], bool]:
    """
    Vrátí funkci „žije ještě session, která mě spustila?".

    Hook běží jako `sh -c "AGENT_NAME=… agent-lease wait …"`, takže rodič bývá
    shell, ne klient. Hlídáme proto prvního předka, který shell není, včetně
    času startu (PID se může recyklovat). Bez tohohle by čekatel z ukončené
    session žil dál, „doručil" zprávu do prázdna a posunul kurzor — živá
    session by ji pak už nikdy neviděla.
    """
    pid = os.getppid()
    for _ in range(3):
        if _proc_comm(pid) not in _SHELLS:
            break
        parent = _proc_ppid(pid)
        if parent <= 1:
            break
        pid = parent
    start = _proc_start(pid)
    if start is None:  # bez /proc (macOS) — aspoň změna rodiče
        orig = os.getppid()
        return lambda: os.getppid() == orig
    return lambda: _proc_start(pid) == start


def _first_wake(store, me: str) -> bool:
    """Je za kurzorem aspoň jedna zpráva, která budí? Stránkuje přes příjemky."""
    since = store.cursor(me)
    while True:
        batch = store.addressed(me, since_id=since, limit=200)
        if any(wakes(m, me) for m in batch):
            return True
        if len(batch) < 200:
            return False
        since = batch[-1]["id"]


def _wake_lock(store, me: str, session: str):
    """Jeden čekatel na session. Vrací otevřený zamčený soubor, nebo None (už někdo čeká)."""
    import fcntl

    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in f"{me}-{session}")[:120]
    path = Path(store.db_path).parent / "wake" / f"{safe}.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fh = open(path, "a")  # noqa: SIM115 — drží se otevřený po celou dobu čekání
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def rewake_wait(store, me: str, *, session: str = "default", timeout: float = 0,
                alive: Callable[[], bool] | None = None, out=None) -> int:
    """
    Čeká bez modelových tokenů, než přijde zpráva, která má session probudit.

    Návratové kódy: 2 = probudit (text je na `out`, výchozí stderr), 0 = nic
    (už čeká jiný čekatel téže session, nebo session skončila), 124 = vypršel
    `timeout` (0 = bez limitu, úklid zajistí hlídání vlastníka).

    Probuzení = doručení: zprávy se zapíšou jako viděné a posune se stejný
    kurzor, jaký používá `context_main`. Proto nebudí staré nevyřízené zprávy
    (jsou za kurzorem dávno) a stejná zpráva neprobudí dvakrát.
    """
    out = out or sys.stderr
    alive = alive or owner_watch()
    lock = _wake_lock(store, me, session)
    if lock is None:
        return 0
    try:
        deadline = time.monotonic() + timeout if timeout > 0 else None
        seen_signal = None
        next_db = next_beat = 0.0
        while True:
            now = time.monotonic()
            if not alive():
                return 0
            if deadline is not None and now >= deadline:
                return 124
            if now >= next_beat:
                store.heartbeat(me)  # spící čekatel nesmí vypadat offline (failover)
                next_beat = now + 60.0
            signal = store.signal_mtime(me)
            if signal != seen_signal or now >= next_db:
                seen_signal, next_db = signal, now + 5.0
                if _first_wake(store, me):
                    messages = _screen(store, store.undelivered(me))
                    store.mark_seen(me, [m["id"] for m in messages if m["recipient"] == me])
                    text = prompt_update(me, store.claims(), messages, store.awaiting_accept(me))
                    if messages:
                        store.set_cursor(me, messages[-1]["id"])
                    print(text or f"[agent-lease] New message for `{me}` — check `inbox`.", file=out)
                    return 2
            time.sleep(0.2)
    finally:
        lock.close()


def _hook_session() -> str:
    """session_id z hook payloadu na stdin; mimo hook (tty) „default"."""
    if sys.stdin is None or sys.stdin.isatty():
        return "default"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError, OSError):
        return "default"
    return str(payload.get("session_id") or payload.get("sessionId") or "default")


if __name__ == "__main__":
    sys.exit(context_main())
