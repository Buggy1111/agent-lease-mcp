"""
`agent-lease simulate` — přehraje celý scénář v dočasné databázi bez skutečných agentů.

První věc po instalaci: za dvě vteřiny ukáže, že potvrzení příjmu, kolize souborů
a failover při limitu opravdu fungují na tomhle stroji, a vypíše to jako časovou osu.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .config import Settings, human_name, parse_fallbacks
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
        out("agent-lease simulation (temporary DB, nothing is written to your state)\n")
        human = human_name(db)
        for a in ("codex", "claude-code"):
            st.heartbeat(a)
        out(f"1) {human} assigns a task")
        mid = st.send(human, "codex", "Fix the failing test", kind="task")
        step(f"task #{mid} waits for codex", any(j["id"] == mid for j in st.jobs("codex")))
        out("2) Codex gets a prompt — the hook records 'seen'")
        st.mark_seen("codex", [mid])
        step(f"{human} sees “codex saw”",
             any("saw" in m["text"] for m in st.inbox() if m["recipient"] == human))
        step("the acknowledgement reminder is shown", bool(st.awaiting_accept("codex")))
        out("3) Codex acknowledges")
        st.accept(mid, "codex", "will fix the test")
        step(f"{human} sees “taking”",
             any(m["text"].startswith("✔ taking") for m in st.inbox() if m["recipient"] == human))
        step("the reminder stopped", not st.awaiting_accept("codex"))
        out("4) Two agents, one file")
        f = str(Path(tmp) / "deploy.sh")
        step("codex gets the file", st.edit_gate(f, "codex") is None)
        step("claude-code is blocked", st.edit_gate(f, "claude-code") is not None)
        out("5) Codex runs out of quota")
        lease = st.lease_next("codex")  # nejstarší čekající = úkol z kroku 1, který právě rozdělá
        busy = lease.message_id
        st.ack(busy, "codex", "started", lease_token=lease.lease_token)
        mid2 = st.send(human, "codex", "Write the docs", kind="task")
        st.report_limit("codex", time.time() + 7200, "simulated limit")
        step("the unstarted task moved to claude-code",
             any(j["id"] == mid2 for j in st.jobs("claude-code")))
        step("claude-code sees it in context",
             any("Write the docs" in m["text"] for m in st.undelivered("claude-code")))
        step("work in progress stayed with codex", any(j["id"] == busy for j in st.jobs("codex")))
        out("6) Overview")
        for row in st.board():
            out(f"    {row['agent']:<12} {row['presence']:<13} queue {row['queued']}")
    out("\n" + ("Everything works." if ok else "Something failed — see ✘ above."))
    return ok
