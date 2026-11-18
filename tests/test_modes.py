"""D2: provozní režimy Základní / Inteligentní / Autonomní."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_webui import authenticated_handler, call_handler

from agent_lease_mcp.broker import METHODS
from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store
from agent_lease_mcp.webui import SESSION_COOKIE


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "room.db")


def test_default_mode_keeps_pre_d2_behaviour(store: Store):
    m = store.mode()
    assert m["mode"] == "intelligent" and m["memory"] is True
    store.send("michal", "codex", "otestuj", kind="task")
    assert store.lease_next("codex", worker=True) is not None


def test_basic_mode_stops_bridge_pickup_but_not_interactive(store: Store):
    store.set_mode("michal", "basic", "šetřím tokeny")
    store.send("michal", "codex", "otestuj", kind="task")
    assert store.lease_next("codex", worker=True) is None
    assert store.lease_next("codex", start=True, owner="session-1") is not None


def test_downgrade_returns_unstarted_bridge_work_and_refuses_its_start(store: Store):
    mid = store.send("michal", "codex", "otestuj", kind="task")
    leased = store.lease_next("codex", worker=True)
    store.set_mode("michal", "basic")
    assert not store.holds(mid, "codex", leased.lease_token)  # spouštěč s launch gate nic nespustí
    assert store.ack(mid, "codex", "started", lease_token=leased.lease_token) is False
    store.set_mode("michal", "intelligent")
    again = store.lease_next("codex", worker=True)
    assert again is not None and again.message_id == mid


def test_ack_started_refused_in_basic_even_with_a_valid_lease(store: Store):
    mid = store.send("michal", "codex", "otestuj", kind="task")
    leased = store.lease_next("codex", worker=True)
    with store._connect() as con:  # režim přepnutý mimo set_mode (např. starší web): start stejně neprojde
        con.execute("INSERT INTO control(key, value, changed_by, changed_at) VALUES('mode','basic','michal',0)")
    assert store.ack(mid, "codex", "started", lease_token=leased.lease_token) is False


def test_running_work_is_left_to_finish_on_downgrade(store: Store):
    mid = store.send("michal", "codex", "otestuj", kind="task")
    leased = store.lease_next("codex", worker=True)
    assert store.ack(mid, "codex", "started", lease_token=leased.lease_token)
    store.set_mode("michal", "basic")
    assert store.holds(mid, "codex", leased.lease_token)


def test_basic_mode_blocks_agent_memory_but_not_human_reads(store: Store):
    e = store.knowledge_add("codex", "disk full")
    store.set_mode("michal", "basic")
    for call in (lambda: store.knowledge_add("codex", "x"), lambda: store.knowledge_search("disk"),
                 lambda: store.knowledge_get(e["id"])):
        with pytest.raises(PermissionError):
            call()
    assert store.knowledge_human_search("disk")[0]["id"] == e["id"]
    assert store.knowledge_list()[0]["id"] == e["id"]
    assert "knowledge_list" not in METHODS and "knowledge_human_search" not in METHODS


def test_mode_survives_restart_and_is_audited(tmp_path: Path):
    Store(tmp_path / "room.db").set_mode("michal", "basic", "test")
    again = Store(tmp_path / "room.db")
    assert again.mode()["mode"] == "basic" and again.mode()["by"] == "michal"
    assert any(e["action"] == "mode" for e in again.history(limit=5))


def test_autonomous_is_locked_and_unknown_mode_rejected(store: Store):
    with pytest.raises(PermissionError):
        store.set_mode("michal", "autonomous")
    with pytest.raises(ValueError):
        store.set_mode("michal", "yolo")
    assert store.mode()["mode"] == "intelligent"


def test_agents_can_read_but_not_change_mode():
    assert "mode" in METHODS and "set_mode" not in METHODS


def test_web_switches_mode_only_with_session(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    status, _ = call_handler(store, settings, "secret", "POST", "/api/mode", body={"mode": "basic"})
    assert status == 403
    handler = authenticated_handler(store, settings, "secret")
    cookie = {"Cookie": f"{SESSION_COOKIE}=session"}
    status, raw = call_handler(store, settings, "secret", "POST", "/api/mode", headers=cookie,
                               body={"mode": "basic", "reason": "noc"}, handler_type=handler)
    assert status == 200 and json.loads(raw)["mode"] == "basic"
    status, _ = call_handler(store, settings, "secret", "POST", "/api/mode", headers=cookie,
                             body={"mode": "autonomous"}, handler_type=handler)
    assert status == 409 and store.mode()["mode"] == "basic"
