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


# ── review Nox #516 ──────────────────────────────────────────────────────────

def test_foreign_or_own_reply_does_not_end_the_wait(store: Store):
    mid = store.send("claude-code", "codex", "zrevidni", kind="task")
    store.send("untrusted-outsider", "michal", "nesouvisí", reply_to=mid)  # pro někoho jiného
    store.send("claude-code", "codex", "ještě doplním zadání", reply_to=mid)  # vlastní doplnění
    assert store.outcome(mid, viewer="claude-code")["answers"] == []
    store.send("codex", "claude-code", "výsledek", kind="result", reply_to=mid)
    assert [a["text"] for a in store.outcome(mid, viewer="claude-code")["answers"]] == ["výsledek"]


def test_recipient_waits_for_the_senders_follow_up(store: Store):
    mid = store.send("claude-code", "codex", "zrevidni", kind="task")
    store.send("codex", "claude-code", "dotaz: celé, nebo jen store?", reply_to=mid)
    assert store.outcome(mid, viewer="codex")["answers"] == []
    store.send("claude-code", "codex", "jen store", reply_to=mid)
    assert [a["text"] for a in store.outcome(mid, viewer="codex")["answers"]] == ["jen store"]


def test_answer_is_found_behind_many_receipts(store: Store):
    mid = store.send("claude-code", "codex", "zrevidni", kind="task")
    store.mark_seen("codex", [mid])
    delivery = store.lease_next("codex")
    store.accept(mid, "codex", "výsledek: hotovo (jen poznámka)", lease_token=delivery.lease_token)
    for i in range(60):
        store.send("broker", "claude-code", f"příjem {i}", kind="result", reply_to=mid)
    store.send("codex", "claude-code", "skutečný výsledek", kind="result", reply_to=mid)
    result = store.outcome(mid, viewer="claude-code")
    assert [a["text"] for a in result["answers"]] == ["skutečný výsledek"]
    assert len(result["receipts"]) == 10


def test_accept_of_one_task_is_not_mistaken_for_another(store: Store):
    for _ in range(4):
        store.send("michal", "codex", "výplň", kind="chat")
    mid = store.send("claude-code", "codex", "malé id", kind="task")
    # ruční odpověď, která jen připomíná příjem jiného úkolu (#<mid>16)
    store.send("codex", "claude-code", f"✔ taking #{mid}16 – to je jiný úkol", reply_to=mid)
    assert len(store.outcome(mid, viewer="claude-code")["answers"]) == 1


# ── staré nevyřízené chaty ───────────────────────────────────────────────────

def test_old_unread_chat_expires_and_sender_is_told(store: Store, monkeypatch):
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3 * 86400)
    old = store.send("claude-code", "codex", "k webu Baller…")
    seen = store.send("claude-code", "codex", "už viděno")
    store.mark_seen("codex", [seen])
    task = store.send("claude-code", "codex", "úkol čeká na limit", kind="task")
    monkeypatch.setattr(time, "time", real)
    fresh = store.send("claude-code", "codex", "čerstvé")

    assert sorted(store.expire_stale_chats(48)) == sorted([old, seen])
    states = {j["id"]: j["state"] for j in store.jobs("codex")}
    assert states[old] == "expired" and states[seen] == "expired"
    assert states[task] == "pending" and states[fresh] == "unread"
    notices = [m for m in store.inbox() if m["text"].startswith("⌛")]
    assert len(notices) == 1 and notices[0]["recipient"] == "claude-code"
    assert store.expire_stale_chats(48) == []          # podruhé nic


def test_expired_chat_is_no_longer_offered_to_the_agent(store: Store, monkeypatch):
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3 * 86400)
    old = store.send("claude-code", "codex", "staré")
    monkeypatch.setattr(time, "time", real)
    store.expire_stale_chats(48)
    assert old not in [m["id"] for m in store.awaiting_accept("codex")]
    assert store.expire_stale_chats(0) == []           # 0 = vypnuto


def test_outsider_cannot_fake_an_answer_to_the_waiter(store: Store):
    mid = store.send("claude-code", "codex", "zrevidni", kind="task")
    store.send("untrusted-outsider", "claude-code", "hotovo (podvrh)", kind="result", reply_to=mid)
    store.send("untrusted-outsider", "claude-code", f"✔ taking #{mid}", reply_to=mid)
    result = store.outcome(mid, viewer="claude-code")
    assert result["answers"] == [] and result["receipts"] == []


# ── review Nox #524 ──────────────────────────────────────────────────────────

def test_scheduled_chat_does_not_expire_before_it_is_due(store: Store, monkeypatch):
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3 * 86400)
    mid = store.send("claude-code", "codex", "připomeň zítra", not_before=real() + 86400)
    monkeypatch.setattr(time, "time", real)
    assert store.expire_stale_chats(48) == []
    # až uplyne celá lhůta od termínu doručení, vyprší
    monkeypatch.setattr(time, "time", lambda: real() + 86400 + 49 * 3600)
    assert store.expire_stale_chats(48) == [mid]


def test_expired_chat_leaves_live_context_wake_and_accept(store: Store, monkeypatch):
    from agent_lease_mcp.lifecycle import wakes

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3 * 86400)
    old = store.send("claude-code", "codex", "staré k Balleru")
    monkeypatch.setattr(time, "time", real)
    task = store.send("claude-code", "codex", "živý úkol", kind="task")
    store.expire_stale_chats(48)

    assert old not in [m["id"] for m in store.undelivered("codex")]
    assert task in [m["id"] for m in store.undelivered("codex")]
    batch = {m["id"]: m for m in store.addressed("codex")}
    assert not wakes(batch[old], "codex")
    assert wakes(batch[task], "codex")
    assert store.accept(old, "codex", "beru") is False
