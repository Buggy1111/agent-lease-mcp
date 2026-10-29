"""`wait` (čekání na výsledek bez ručního dotazování) a štíhlý `room`."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "room.db", settings=Settings(tmp_path / "room.db", "codex", 1800))


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.setenv("AGENT_NAME", "claude-code")
    import importlib

    from agent_lease_mcp import server

    importlib.reload(server)
    return server


# ── outcome ──────────────────────────────────────────────────────────────────

def test_outcome_separates_answers_from_automatic_receipts(store: Store):
    mid = store.send("claude-code", "codex", "zrevidni to", kind="task")
    store.mark_seen("codex", [mid])          # „✓ codex saw #…“ od brokeru
    delivery = store.lease_next("codex")
    assert store.accept(mid, "codex", "beru", lease_token=delivery.lease_token)  # „✔ taking #…“

    result = store.outcome(mid, viewer="claude-code")
    assert result["answers"] == []
    assert len(result["receipts"]) == 2
    assert result["final"] is False

    store.send("codex", "claude-code", "2 nálezy", kind="result", reply_to=mid)
    store.ack(mid, "codex", "succeeded", lease_token=delivery.lease_token)
    result = store.outcome(mid, viewer="claude-code")
    assert [a["text"] for a in result["answers"]] == ["2 nálezy"]
    assert result["state"] == "succeeded" and result["final"] is True


def test_outcome_is_only_for_sender_and_recipient(store: Store):
    mid = store.send("claude-code", "codex", "tajné", kind="task")
    assert store.outcome(mid, viewer="codex")["id"] == mid
    with pytest.raises(PermissionError):
        store.outcome(mid, viewer="cizinec")
    with pytest.raises(KeyError):
        store.outcome(9999, viewer="claude-code")


# ── wait ─────────────────────────────────────────────────────────────────────

def test_wait_returns_when_the_answer_arrives(server):
    mid = server.send("codex", "zrevidni", kind="task")["id"]
    store = server._store

    def answer():
        time.sleep(0.5)
        store.send("codex", "claude-code", "hotovo, bez nálezů", kind="result", reply_to=mid)

    threading.Thread(target=answer).start()
    started = time.monotonic()
    result = server.wait(mid, timeout_seconds=20)
    assert result["timed_out"] is False
    assert result["answers"][0]["text"] == "hotovo, bez nálezů"
    assert time.monotonic() - started < 10


def test_wait_ignores_receipts_and_times_out(server):
    mid = server.send("codex", "zrevidni", kind="task")["id"]
    server._store.mark_seen("codex", [mid])
    result = server.wait(mid, timeout_seconds=1)
    assert result["timed_out"] is True and result["answers"] == []
    assert len(result["receipts"]) == 1


def test_wait_returns_on_final_state_without_answer(server):
    mid = server.send("codex", "zrevidni", kind="task")["id"]
    server._store.cancel(mid, "codex", actor="claude-code")
    result = server.wait(mid, timeout_seconds=5)
    assert result["timed_out"] is False and result["state"] == "cancelled"


# ── room ─────────────────────────────────────────────────────────────────────

def test_room_shows_newest_messages_not_oldest(server):
    for i in range(15):
        server.say(f"zpráva {i}")
    texts = [m["text"] for m in server.room()["recent_messages"]]
    assert texts[-1] == "zpráva 14"
    assert "zpráva 0" not in texts
    assert len(texts) == server.ROOM_MESSAGES


def test_room_shortens_long_messages(server):
    server.say("x" * 5000)
    message = server.room()["recent_messages"][-1]
    assert message["truncated"] is True
    assert len(message["text"]) == server.ROOM_TEXT_CHARS + 1


def test_room_hides_long_gone_agents(server, monkeypatch):
    store = server._store
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3 * 86400)
    store.heartbeat("stary-agent", status="dávno")
    monkeypatch.setattr(time, "time", real)
    store.heartbeat("codex", status="tady")

    state = server.room()
    names = {p["agent"] for p in state["peers"]}
    assert "codex" in names and "stary-agent" not in names
    assert state["hidden_offline_peers"] == 1
    assert "stary-agent" in {p["agent"] for p in server.room(all_peers=True)["peers"]}
