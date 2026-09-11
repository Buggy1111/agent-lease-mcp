"""Testy stavu místnosti. Zamykání bez testu na souběh je jen slib."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_lease_mcp.policy import normalize_path
from agent_lease_mcp.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "room.db")


def test_claim_grants_free_path(store: Store, tmp_path: Path):
    result = store.claim([str(tmp_path / "a.txt")], agent="claude")
    assert result.ok
    assert result.granted == [normalize_path(str(tmp_path / "a.txt"))]


def test_second_agent_is_refused(store: Store, tmp_path: Path):
    path = str(tmp_path / "a.txt")
    store.claim([path], agent="claude", purpose="oprava")

    result = store.claim([path], agent="codex")

    assert not result.ok
    assert result.conflicts[0].agent == "claude"
    assert result.conflicts[0].purpose == "oprava"


def test_same_agent_can_extend_own_claim(store: Store, tmp_path: Path):
    path = str(tmp_path / "a.txt")
    store.claim([path], agent="claude")
    assert store.claim([path], agent="claude").ok


def test_claim_is_all_or_nothing(store: Store, tmp_path: Path):
    """Půlka cest agentovi k ničemu není, ale druhého by blokovala."""
    free, taken = str(tmp_path / "free.txt"), str(tmp_path / "taken.txt")
    store.claim([taken], agent="codex")

    result = store.claim([free, taken], agent="claude")

    assert not result.ok
    assert store.holder_of(free) is None, "volná cesta se nesmí zabrat při neúspěchu"


def test_expired_claim_frees_path(store: Store, tmp_path: Path):
    path = str(tmp_path / "a.txt")
    store.claim([path], agent="codex", ttl_seconds=1)
    time.sleep(1.1)

    assert store.holder_of(path) is None
    assert store.claim([path], agent="claude").ok


def test_release_only_touches_own_claims(store: Store, tmp_path: Path):
    path = str(tmp_path / "a.txt")
    store.claim([path], agent="codex")

    assert store.release([path], agent="claude") == []
    assert store.holder_of(path).agent == "codex"


def test_relative_and_absolute_path_are_same_claim(store: Store, tmp_path: Path, monkeypatch):
    """Bez normalizace by si dva agenti mysleli, že drží různé soubory."""
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.chdir(tmp_path)
    store.claim(["a.txt"], agent="claude")

    result = store.claim([str(tmp_path / "a.txt")], agent="codex")

    assert not result.ok


def test_concurrent_claims_have_exactly_one_winner(store: Store, tmp_path: Path):
    """
    Jádro celé věci: dva agenti ve stejnou chvíli na stejný soubor.
    Bez BEGIN IMMEDIATE oba přečtou "volno" a oba si ho vezmou.
    """
    path = str(tmp_path / "contested.sh")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda a: store.claim([path], agent=a), ["claude", "codex"]))

    assert sum(r.ok for r in results) == 1, "nájem smí dostat právě jeden"


def test_messages_use_cursor(store: Store):
    first = store.say("claude", "beru si balicí skript")
    store.say("codex", "ok, nesahám")

    assert len(store.inbox(since_id=0)) == 2
    assert len(store.inbox(since_id=first)) == 1


def test_latest_messages_returns_tail_in_chronological_order(store: Store):
    for i in range(5):
        store.say("claude", str(i))

    assert [m["text"] for m in store.latest_messages(limit=3)] == ["2", "3", "4"]


def test_addressed_message_is_only_undelivered_to_recipient(store: Store):
    store.send("michal", "codex", "udělej test", kind="task")

    assert [m["text"] for m in store.undelivered("codex")] == ["udělej test"]
    assert store.undelivered("claude-code") == []
    assert [m["text"] for m in store.addressed("codex")] == ["udělej test"]
    assert store.addressed("codex")[0]["state"] == "pending"


def test_other_recipients_cannot_starve_undelivered_limit(store: Store):
    for i in range(25):
        store.send("michal", "claude-code", f"cizí {i}", kind="chat")
    store.send("michal", "codex", "pro mě", kind="chat")

    assert [m["text"] for m in store.undelivered("codex", limit=20)] == ["pro mě"]


def test_send_deduplicates_by_sender_key(store: Store):
    first = store.send("michal", "codex", "jednou", kind="task", dedupe_key="task-1")
    second = store.send("michal", "codex", "podruhé", kind="task", dedupe_key="task-1")

    assert second == first
    assert len(store.jobs("codex")) == 1


def test_task_lease_is_atomic_and_requires_token(store: Store):
    message_id = store.send("michal", "codex", "otestuj", kind="task")

    with ThreadPoolExecutor(max_workers=2) as pool:
        leased = list(pool.map(lambda _: store.lease_next("codex"), range(2)))

    deliveries = [item for item in leased if item is not None]
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery.message_id == message_id
    assert not store.ack(message_id, "codex", "succeeded", lease_token="špatně")
    assert store.ack(message_id, "codex", "succeeded", lease_token=delivery.lease_token)
    assert store.jobs("codex")[0]["state"] == "succeeded"
    assert store.inbox()[0]["state"] == "succeeded"


def test_expired_delivery_lease_can_be_taken_again(store: Store, monkeypatch):
    store.send("michal", "codex", "otestuj", kind="task")
    first = store.lease_next("codex", lease_seconds=1)
    assert first is not None
    monkeypatch.setattr(time, "time", lambda: first.lease_until + 1)

    second = store.lease_next("codex")

    assert second is not None
    assert second.lease_token != first.lease_token
    assert second.attempts == 2


def test_expired_started_task_needs_review(store: Store, monkeypatch):
    message_id = store.send("michal", "codex", "otestuj", kind="task")
    delivery = store.lease_next("codex", lease_seconds=1)
    assert delivery is not None
    assert store.ack(message_id, "codex", "started", lease_token=delivery.lease_token)
    monkeypatch.setattr(time, "time", lambda: delivery.lease_until + 1)

    assert store.lease_next("codex") is None
    job = store.jobs("codex")[0]
    assert job["state"] == "needs_review"
    assert job["last_error"] == "worker lease expired after start"


def test_failed_task_can_retry_then_cancel(store: Store):
    message_id = store.send("michal", "codex", "otestuj", kind="task")
    delivery = store.lease_next("codex")
    assert delivery is not None
    assert store.ack(message_id, "codex", "failed", lease_token=delivery.lease_token)

    assert store.retry(message_id, "codex")
    assert store.jobs("codex")[0]["state"] == "pending"
    assert store.cancel(message_id, "codex")
    assert store.jobs("codex")[0]["state"] == "cancelled"
    assert not store.retry(message_id, "codex")


def test_peer_goes_stale(store: Store):
    store.heartbeat("codex", status="kontrola")
    assert store.peers(stale_after=900)[0]["active"] is True
    assert store.peers(stale_after=0)[0]["active"] is False


def test_heartbeat_keeps_status_when_not_given(store: Store):
    """`room()` bez argumentu nesmí smazat popis práce ohlášený dřív."""
    store.heartbeat("codex", status="balím balíček", cwd="/repo")
    store.heartbeat("codex")

    peer = store.peers()[0]
    assert peer["status"] == "balím balíček"
    assert peer["cwd"] == "/repo"


def test_heartbeat_clears_status_when_asked(store: Store):
    """Prázdný řetězec je platná hodnota — na rozdíl od None status smaže."""
    store.heartbeat("codex", status="balím balíček")
    store.heartbeat("codex", status="")

    assert store.peers()[0]["status"] == ""


def test_directory_claim_blocks_file_inside(store: Store, tmp_path: Path):
    """Nikdo dopředu nevyjmenuje všechny soubory, kterých se dotkne."""
    (tmp_path / "deploy").mkdir()
    store.claim([str(tmp_path / "deploy")], agent="codex", purpose="balení")

    result = store.claim([str(tmp_path / "deploy" / "make-package.sh")], agent="claude")

    assert not result.ok
    assert result.conflicts[0].agent == "codex"


def test_file_claim_blocks_directory_above(store: Store, tmp_path: Path):
    """Opačný směr: nemůžu si vzít adresář, když uvnitř někdo pracuje."""
    (tmp_path / "deploy").mkdir()
    store.claim([str(tmp_path / "deploy" / "x.sh")], agent="codex")

    assert not store.claim([str(tmp_path / "deploy")], agent="claude").ok


def test_sibling_directory_is_not_blocked(store: Store, tmp_path: Path):
    """Prefix nestačí — `deploy2` není uvnitř `deploy`."""
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy2").mkdir()
    store.claim([str(tmp_path / "deploy")], agent="codex")

    assert store.claim([str(tmp_path / "deploy2")], agent="claude").ok


def test_audit_records_who_touched_what(store: Store, tmp_path: Path):
    """Kvůli tomuhle projekt vznikl: po kolizi musí jít dohledat, kdo tam sahal."""
    path = str(tmp_path / "a.txt")
    store.claim([path], agent="codex", purpose="balení")
    store.claim([path], agent="claude")

    actions = [e["action"] for e in store.history(path=path)]

    assert "claim" in actions
    assert "claim-refused" in actions
