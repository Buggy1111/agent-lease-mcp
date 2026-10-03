from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_lease_mcp.cli import parse_until
from agent_lease_mcp.config import Settings, parse_fallbacks
from agent_lease_mcp.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    fb = parse_fallbacks("codex=claude-code,openrouter;claude-code=codex,openrouter")
    s = Store(tmp_path / "r.db", settings=Settings(tmp_path / "r.db", "x", 1800, fallbacks=fb,
                                                   failover_grace=0))
    for a in ("codex", "claude-code", "openrouter"):
        s.heartbeat(a, status="")
    return s


def test_parse_fallbacks():
    assert parse_fallbacks("a=b,c;d=e") == {"a": ("b", "c"), "d": ("e",)}
    assert parse_fallbacks("blbost") == {}


def test_limit_moves_pending_task_to_first_available_fallback(store: Store):
    mid = store.send("michal", "codex", "oprav build", kind="task")
    store.report_limit("codex", time.time() + 3600, "weekly limit")
    job = next(j for j in store.jobs("claude-code") if j["id"] == mid)
    assert job["state"] == "pending" and job["recipient"] == "claude-code"
    notes = [m["text"] for m in store.inbox() if m["agent"] == "broker" and "přesunut" in m["text"]]
    assert len(notes) == 1 and "codex → claude-code" in notes[0]  # odesílatel je člověk → jedna zpráva
    assert store.lease_next("codex") is None
    assert store.lease_next("claude-code").message_id == mid


def test_skips_limited_fallback_and_uses_next(store: Store):
    store.report_limit("claude-code", time.time() + 3600)
    mid = store.send("michal", "codex", "t", kind="task")
    store.report_limit("codex", time.time() + 3600)
    assert any(j["id"] == mid for j in store.jobs("openrouter"))


def test_started_work_is_never_moved(store: Store):
    mid = store.send("michal", "codex", "t", kind="task")
    d = store.lease_next("codex")
    store.ack(mid, "codex", "started", lease_token=d.lease_token)
    store.report_limit("codex", time.time() + 3600)
    assert any(j["id"] == mid for j in store.jobs("codex"))
    assert not store.jobs("claude-code")


def test_grace_period_prevents_bouncing(tmp_path: Path):
    fb = parse_fallbacks("codex=claude-code")
    s = Store(tmp_path / "r.db", settings=Settings(tmp_path / "r.db", "x", 1800,
                                                   fallbacks=fb, failover_grace=300))
    s.heartbeat("codex")
    s.heartbeat("claude-code")
    s.send("michal", "codex", "t", kind="task")
    assert s.failover_sweep() == []  # čerstvý úkol, ještě se čeká


def test_no_move_when_nobody_available(store: Store):
    mid = store.send("michal", "codex", "t", kind="task")
    for a in ("claude-code", "openrouter"):
        store.report_limit(a, time.time() + 3600)
    store.report_limit("codex", time.time() + 3600)
    assert any(j["id"] == mid for j in store.jobs("codex"))  # zůstane čekat


def test_limit_expires_and_presence_shows_it(store: Store):
    store.report_limit("codex", time.time() + 2)
    assert next(p for p in store.peers() if p["agent"] == "codex")["presence"] == "rate-limited"
    assert not store.is_available("codex")
    assert store.clear_limit("codex") and store.is_available("codex")


def test_sweep_is_idempotent(store: Store):
    store.send("michal", "codex", "t", kind="task")
    store.report_limit("codex", time.time() + 3600)
    assert store.failover_sweep() == []


def test_parse_until():
    assert 7100 < parse_until("+2h") - time.time() < 7300
    assert parse_until("+30m") - time.time() < 1900
    assert parse_until("1900000000") == 1900000000.0


def test_agent_sender_and_human_are_both_told(store: Store):
    store.heartbeat("helper")
    store.send("helper", "codex", "t", kind="task")
    store.report_limit("codex", time.time() + 3600)
    to = {m["recipient"] for m in store.inbox() if m["agent"] == "broker"}
    assert {"helper", "michal"} <= to
