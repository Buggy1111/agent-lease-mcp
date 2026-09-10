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


def test_peer_goes_stale(store: Store):
    store.heartbeat("codex", status="kontrola")
    assert store.peers(stale_after=900)[0]["active"] is True
    assert store.peers(stale_after=0)[0]["active"] is False


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
