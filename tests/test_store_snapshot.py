"""
Charakterizační test pro refaktoring store.py (Nox, rozbor #516): pevný scénář a
přesný tvar i hodnoty všech čtecích výstupů. Přesun kódu do modulů nesmí změnit
ani jeden klíč, stav nebo hodnotu. Snímek se mění jen vědomou změnou chování.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store

SNAPSHOT = Path(__file__).parent / "fixtures" / "store_snapshot.json"


def _strip(x):
    if isinstance(x, list):
        return [_strip(i) for i in x]
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if k not in ("seconds_ago", "lease_until")}
    return x


def test_read_outputs_match_the_snapshot(tmp_path: Path):
    s = Store(tmp_path / "r.db", settings=Settings(tmp_path / "r.db", "codex", 1800))
    mid = s.send("claude-code", "codex", "úkol", kind="task")
    s.mark_seen("codex", [mid])
    delivery = s.lease_next("codex")
    s.accept(mid, "codex", "beru", lease_token=delivery.lease_token)
    s.send("claude-code", "codex", "chat")
    s.send("codex", "claude-code", "výsledek", kind="result", reply_to=mid)
    s.ack(mid, "codex", "succeeded", lease_token=delivery.lease_token)
    s.say("codex", "ahoj všem")

    actual = _strip({
        "inbox": s.inbox(), "latest": s.latest_messages(), "undelivered": s.undelivered("codex"),
        "addressed": s.addressed("codex"), "jobs": s.jobs("codex"),
        "awaiting": s.awaiting_accept("codex"), "outcome": s.outcome(mid, viewer="claude-code"),
        "outcome_ui": s.outcome(mid),
        "history": [{k: v for k, v in h.items() if k != "seconds_ago"} for h in s.history()],
    })
    assert actual == json.loads(SNAPSHOT.read_text(encoding="utf-8"))
