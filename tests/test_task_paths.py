"""B1: rozběhnutý úkol rezervuje své task_paths; prošlé nájmy úkoly neblokují."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agent_lease_mcp.models import DeliveryState
from agent_lease_mcp.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "room.db")


def test_running_task_blocks_overlapping_task_of_another_agent(store, tmp_path):
    folder = tmp_path / "src"
    first = store.send("michal", "codex", "refactor", kind="task", paths=[str(folder)])
    second = store.send("michal", "claude-code", "fix", kind="task", paths=[str(folder / "a.py")])
    free = store.send("michal", "claude-code", "docs", kind="task", paths=[str(tmp_path / "README")])

    running = store.lease_next("codex", lease_seconds=600)
    assert running.message_id == first
    got = store.lease_next("claude-code", lease_seconds=600)
    assert got.message_id == free  # překrývající se #second čeká, nezávislý jde

    assert store.complete(free, "claude-code", got.lease_token, DeliveryState.SUCCEEDED, "ok")
    assert store.lease_next("claude-code") is None
    assert store.complete(first, "codex", running.lease_token, DeliveryState.SUCCEEDED, "ok")
    assert store.lease_next("claude-code").message_id == second


def test_only_one_of_concurrent_overlapping_leases_wins(tmp_path):
    db = tmp_path / "room.db"
    Store(db)
    path = str(tmp_path / "shared.py")
    for agent in ("codex", "claude-code", "gemini", "worker"):
        Store(db).send("michal", agent, "edit shared", kind="task", paths=[path])
    barrier = threading.Barrier(4)
    won = []

    def take(agent):
        s = Store(db)
        barrier.wait()
        if s.lease_next(agent, lease_seconds=600):
            won.append(agent)

    threads = [threading.Thread(target=take, args=(a,))
               for a in ("codex", "claude-code", "gemini", "worker")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(won) == 1


def test_second_session_of_same_agent_waits_for_overlapping_task(store, tmp_path):
    path = str(tmp_path / "x.py")
    first = store.send("michal", "codex", "one", kind="task", paths=[path])
    store.send("michal", "codex", "two", kind="task", paths=[path])
    other = store.send("michal", "codex", "three", kind="task", paths=[str(tmp_path / "y.py")])
    assert store.lease_next("codex", lease_seconds=600).message_id == first
    assert store.lease_next("codex", lease_seconds=600, start=True).message_id == other


def test_task_reservation_blocks_claims_and_edits_of_others(store, tmp_path):
    path = tmp_path / "pkg"
    mid = store.send("michal", "codex", "work", kind="task", paths=[str(path)])
    store.lease_next("codex", lease_seconds=600)
    result = store.claim([str(path / "f.py")], agent="claude-code", purpose="edit")
    assert not result.ok and result.conflicts[0].purpose == f"task #{mid}"
    assert result.conflicts[0].agent == "codex"
    blocker = store.edit_gate(str(path / "g.py"), "claude-code")
    assert blocker is not None and blocker.purpose == f"task #{mid}"
    assert store.edit_gate(str(path / "g.py"), "codex") is None  # adresát sám může
    holder = store.holder_of(str(path / "h.py"))
    assert holder.agent == "codex" and holder.purpose == f"task #{mid}"


def test_reservation_ends_with_the_lease(store, tmp_path):
    path = str(tmp_path / "f.py")
    first = store.send("michal", "codex", "short", kind="task", paths=[path])
    second = store.send("michal", "claude-code", "next", kind="task", paths=[path])
    assert store.lease_next("codex", lease_seconds=1).message_id == first
    assert store.lease_next("claude-code") is None
    time.sleep(1.2)  # lease prošel, rezervace s ním (bez úklidu)
    assert store.claim([path], agent="gemini", purpose="x").ok
    store.release([path], agent="gemini")
    assert store.lease_next("claude-code").message_id == second


def test_expired_foreign_claim_does_not_block_and_is_pruned(store, tmp_path):
    path = str(tmp_path / "f.py")
    store.claim([path], agent="claude-code", purpose="old", ttl_seconds=1)
    mid = store.send("michal", "codex", "work", kind="task", paths=[path])
    time.sleep(1.2)
    assert store.lease_next("codex").message_id == mid
    with store._connect() as con:  # prošlý nájem úklid v lease_next smazal
        assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_reconcile_prunes_expired_claims(store, tmp_path):
    store.claim([str(tmp_path / "f.py")], agent="claude-code", purpose="old", ttl_seconds=1)
    time.sleep(1.2)
    store.reconcile()
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_lease_next_loads_reservations_once(store, tmp_path, monkeypatch):
    """Nox #2: snímek rezervací jednou na lease_next, ne pro každého kandidáta."""
    blocker = store.send("michal", "codex", "hold", kind="task", paths=[str(tmp_path)])
    store.lease_next("codex", lease_seconds=600)
    for i in range(20):
        store.send("michal", "claude-code", f"t{i}", kind="task", paths=[str(tmp_path / f"f{i}")])
    calls = []
    real = type(store)._held
    monkeypatch.setattr(type(store), "_held", staticmethod(lambda con: calls.append(1) or real(con)))
    assert store.lease_next("claude-code") is None
    assert len(calls) == 1 and blocker
