"""
`agent-lease simulate` — přehraje celý scénář v dočasné databázi bez skutečných agentů.

První věc po instalaci: za dvě vteřiny ukáže, že potvrzení příjmu, kolize souborů
a failover při limitu opravdu fungují na tomhle stroji, a vypíše to jako časovou osu.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .config import Settings, parse_fallbacks
from .store import Store


def run_simulation(out=print) -> bool:
    ok = True

    def step(text: str, cond: bool = True) -> None:
        nonlocal ok
        ok &= bool(cond)
        out(f"  {'✔' if cond else '✘'} {text}")

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sim.db"
        fb = parse_fallbacks("codex=claude-code;claude-code=codex")
        st = Store(db, settings=Settings(db, "sim", 1800, fallbacks=fb, failover_grace=0,
                                         offline_grace=0))
        out("agent-lease simulace (dočasná DB, nic se nezapisuje do tvého stavu)\n")
        for a in ("codex", "claude-code"):
            st.heartbeat(a)
        out("1) Michal zadá úkol")
        mid = st.send("michal", "codex", "Oprav padající test", kind="task")
        step(f"úkol #{mid} čeká u codex", any(j["id"] == mid for j in st.jobs("codex")))
        out("2) Codex dostane prompt — hook zapíše 'viděl'")
        st.mark_seen("codex", [mid])
        step("Michal vidí „codex viděl“",
             any("viděl" in m["text"] for m in st.inbox() if m["recipient"] == "michal"))
        step("výzva k potvrzení se připomíná", bool(st.awaiting_accept("codex")))
        out("3) Codex potvrdí")
        st.accept(mid, "codex", "opravím test")
        step("Michal vidí „beru“",
             any(m["text"].startswith("✔ beru") for m in st.inbox() if m["recipient"] == "michal"))
        step("připomínání skončilo", not st.awaiting_accept("codex"))
        out("4) Dva agenti na jeden soubor")
        f = str(Path(tmp) / "deploy.sh")
        step("codex získá soubor", st.edit_gate(f, "codex") is None)
        step("claude-code je zablokován", st.edit_gate(f, "claude-code") is not None)
        out("5) Codexu dojde limit")
        busy = st.send("michal", "codex", "Rozdělaný refaktor", kind="task")
        lease = st.lease_next("codex")
        st.ack(busy, "codex", "started", lease_token=lease.lease_token)
        mid2 = st.send("michal", "codex", "Napiš dokumentaci", kind="task")
        st.report_limit("codex", time.time() + 7200, "simulovaný limit")
        step("nezahájený úkol přešel na claude-code",
             any(j["id"] == mid2 for j in st.jobs("claude-code")))
        step("claude-code ho uvidí v kontextu",
             any("Napiš dokumentaci" in m["text"] for m in st.undelivered("claude-code")))
        step("rozdělaná práce zůstala u codex", any(j["id"] == busy for j in st.jobs("codex")))
        out("6) Přehled")
        for row in st.board():
            out(f"    {row['agent']:<12} {row['presence']:<13} fronta {row['queued']}")
    out("\n" + ("Vše funguje." if ok else "Něco selhalo — viz ✘ výše."))
    return ok
