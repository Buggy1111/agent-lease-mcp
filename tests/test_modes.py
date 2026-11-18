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
    assert store.lease_next("codex") is not None


def test_basic_mode_stops_bridge_pickup_and_agent_memory(store: Store):
    store.set_mode("michal", "basic", "šetřím tokeny")
    store.send("michal", "codex", "otestuj", kind="task")
    assert store.lease_next("codex") is None
    with pytest.raises(PermissionError):
        store.knowledge_add("codex", "x")
    store.set_mode("michal", "intelligent")
    assert store.lease_next("codex") is not None


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
