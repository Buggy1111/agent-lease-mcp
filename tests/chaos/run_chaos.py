"""
Chaos test fáze A (izolovaná DB, nic neběží proti živé místnosti).

Běží broker + několik bridge workerů jednoho agenta naráz. Driver posílá úkoly,
náhodně zabíjí (SIGKILL) workery i broker a startuje je znovu, ruší úkoly,
zkouší převzetí ze „dvou session“ a jednou dá nouzové zastavení.

Na konci ověří:
  1. žádný úkol nemá víc než jeden EFEKT (přesně jednou),
  2. žádný proces falešného agenta nepřežil,
  3. každý úkol skončil jasným stavem (succeeded / needs_review / cancelled /
     pending kvůli limitu) — nic nevisí v leased/started,
  4. „Done“ nepřišlo k úkolu, který nebyl succeeded,
  5. databáze prošla integrity_check.

Spuštění: python tests/chaos/run_chaos.py --minutes 30
"""
from __future__ import annotations

import argparse
import os
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="agent-lease-chaos-"))
    db = tmp / "room.db"
    log = tmp / "effects.log"
    log.touch()
    env = {**os.environ, "AGENT_LEASE_DB": str(db), "AGENT_NAME": "michal",
           "CHAOS_LOG": str(log), "AGENT_LEASE_JEV": "0"}
    env.pop("AGENT_LEASE_SOCKET", None)
    py = sys.executable
    cmd = f"{py} {HERE / 'fake_worker.py'} {{prompt}}"

    def start_broker():
        return subprocess.Popen([py, "-m", "agent_lease_mcp.broker"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start_bridge():
        return subprocess.Popen(
            [py, "-m", "agent_lease_mcp.bridge", "--agent", "codex", "--command", cmd,
             "--max-per-hour", "100000", "--interactive-grace", "0", "--include-existing",
             "--lease-seconds", "6", "--timeout", "60"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    sys.path.insert(0, str(HERE.parents[1] / "src"))
    from agent_lease_mcp.config import Settings
    from agent_lease_mcp.store import Store

    settings = Settings(db, "michal", 1800)
    store = Store(db, settings=settings)       # driver píše přímo (jako člověk/web)
    broker = start_broker()
    time.sleep(1.5)
    workers = [start_bridge() for _ in range(args.workers)]
    sent: list[int] = []
    stats = {"sent": 0, "worker_kills": 0, "broker_kills": 0, "cancels": 0,
             "double_accept_refused": 0, "pauses": 0}
    end = time.monotonic() + args.minutes * 60
    paused_once = False
    rnd = random.Random(42)

    try:
        while time.monotonic() < end:
            # nové úkoly
            for _ in range(rnd.randint(0, 3)):
                sent.append(store.send("michal", "codex", f"chaos úkol {len(sent)}", kind="task"))
                stats["sent"] += 1
            roll = rnd.random()
            if roll < 0.20:                                  # zabít náhodného workera
                i = rnd.randrange(len(workers))
                workers[i].send_signal(signal.SIGKILL)
                workers[i].wait()
                workers[i] = start_bridge()
                stats["worker_kills"] += 1
            elif roll < 0.25:                                # zabít broker
                broker.send_signal(signal.SIGKILL)
                broker.wait()
                time.sleep(rnd.uniform(0.5, 3))
                broker = start_broker()
                stats["broker_kills"] += 1
            elif roll < 0.32 and sent:                       # zrušit úkol
                if store.cancel(rnd.choice(sent), "codex", actor="michal"):
                    stats["cancels"] += 1
            elif roll < 0.36 and sent:                       # dvě session na jeden úkol
                mid = rnd.choice(sent)
                a = store.accept(mid, "codex", "A", owner="session-A")
                b = store.accept(mid, "codex", "B", owner="session-B")
                if a and b:
                    print(f"CHYBA: dvě session převzaly #{mid}")
                    return 1
                if a or b:
                    stats["double_accept_refused"] += 1
                    # session úkol nevykoná → vrátit ho, ať neblokuje
                    owner = "session-A" if a else "session-B"
                    d = store.lease_next("codex", owner=owner)
                    if d:
                        store.complete(d.message_id, "codex", d.lease_token, "cancelled",
                                       "session to vzdala")
            elif not paused_once and time.monotonic() > end - args.minutes * 30:
                store.pause("michal", "chaos: nouzové zastavení")
                stats["pauses"] += 1
                time.sleep(8)
                store.resume("michal", "chaos: pokračovat")
                paused_once = True
            time.sleep(rnd.uniform(0.5, 2.0))

        # dojezd: nové úkoly už ne, nechat frontu doběhnout
        drain_end = time.monotonic() + 90
        while time.monotonic() < drain_end:
            busy = sum(1 for j in store.jobs("codex", limit=500)
                       if j["state"] in ("leased", "started"))
            if not busy:
                break
            time.sleep(2)
    finally:
        for w in workers:
            w.send_signal(signal.SIGTERM)
        for w in workers:
            try:
                w.wait(10)
            except subprocess.TimeoutExpired:
                w.kill()
        broker.send_signal(signal.SIGTERM)
        broker.wait(10)
        time.sleep(1)

    # ── vyhodnocení ──
    effects: dict[str, int] = {}
    pids: set[int] = set()
    for line in log.read_text().splitlines():
        kind, mid, pid = line.split()
        pids.add(int(pid))
        if kind == "effect":
            effects[mid] = effects.get(mid, 0) + 1
    doubles = {m: n for m, n in effects.items() if n > 1}
    alive = [p for p in pids if Path(f"/proc/{p}").exists()
             and Path(f"/proc/{p}/stat").read_text().split()[2] != "Z"]
    states: dict[str, int] = {}
    stuck = []
    for j in store.jobs("codex", limit=10000):
        states[j["state"]] = states.get(j["state"], 0) + 1
        if j["state"] in ("leased", "started"):
            stuck.append(j["id"])
    done_ids = {m["reply_to"] for m in store.inbox(limit=100000)
                if m["kind"] == "result" and (m["text"] or "").startswith("Done #")}
    not_succeeded = {j["id"] for j in store.jobs("codex", limit=10000) if j["state"] != "succeeded"}
    false_done = sorted(done_ids & not_succeeded)
    con = sqlite3.connect(db)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()

    print(f"statistika: {stats}")
    print(f"stavy úkolů: {states}")
    print(f"efekty: {len(effects)} úkolů, dvojité: {doubles or 'žádné'}")
    print(f"přeživší procesy: {alive or 'žádné'}")
    print(f"visící leased/started: {stuck or 'žádné'}")
    print(f"„Done“ u nedokončeného: {false_done or 'žádné'}")
    print(f"integrita DB: {integrity}")
    ok = not doubles and not alive and not stuck and not false_done and integrity == "ok"
    print("VÝSLEDEK:", "PROŠLO" if ok else "SELHALO", f"(data: {tmp})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
