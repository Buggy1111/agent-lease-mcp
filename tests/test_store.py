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


def test_provider_state_is_typed_and_unknown_is_not_available(store: Store):
    store.heartbeat("codex")
    store.report_provider_state("codex", "unknown", "test", reason="signal absent")
    assert not store.is_available("codex")

    store.report_provider_state("codex", "available", "test")
    assert store.is_available("codex")


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

    assert not store.ack(message_id, "codex", "succeeded")

    with ThreadPoolExecutor(max_workers=2) as pool:
        leased = list(pool.map(lambda _: store.lease_next("codex"), range(2)))

    deliveries = [item for item in leased if item is not None]
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery.message_id == message_id
    assert not store.ack(message_id, "codex", "succeeded", lease_token="špatně")
    assert store.ack(message_id, "codex", "succeeded", lease_token=delivery.lease_token)
    assert store.ack(message_id, "codex", "succeeded", lease_token=delivery.lease_token)
    assert not store.ack(message_id, "codex", "failed", lease_token=delivery.lease_token)
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


def test_lease_next_can_ignore_tasks_before_activation(store: Store):
    old = store.send("michal", "codex", "old", kind="task")
    new = store.send("michal", "codex", "new", kind="task")

    delivery = store.lease_next("codex", after_message_id=old)

    assert delivery is not None and delivery.message_id == new
    assert next(j for j in store.jobs("codex") if j["id"] == old)["state"] == "pending"


def test_task_summary_separates_receipt_from_execution(store: Store):
    unaccepted = store.send("michal", "codex", "one", kind="task")
    accepted = store.send("michal", "codex", "two", kind="task")
    store.accept(accepted, "codex")

    assert store.task_summary("codex") == {
        "pending": 2,
        "unaccepted": 1,
        "accepted_without_start": 1,
        "leased": 0,
        "started": 0,
        "needs_review": 0,
    }
    assert unaccepted != accepted


def test_activation_status_is_read_only(store: Store):
    assert store.activation_status("codex") == {
        "worker_after_message_id": None,
        "failover_after_message_id": None,
    }
    store.worker_activation("codex")
    store.failover_activation("codex")
    assert store.activation_status("codex") == {
        "worker_after_message_id": 0,
        "failover_after_message_id": 0,
    }


def test_peer_goes_stale(store: Store):
    store.heartbeat("codex", status="kontrola")
    assert store.peers(stale_after=900)[0]["active"] is True
    assert store.peers(stale_after=0)[0]["active"] is False


def test_presence_distinguishes_busy_stalled_and_offline(store: Store, monkeypatch):
    store.heartbeat("codex", status="spouštím testy")
    seen = time.time()
    assert store.peers(stale_after=10)[0]["presence"] == "busy"

    monkeypatch.setattr(time, "time", lambda: seen + 11)
    assert store.peers(stale_after=10)[0]["presence"] == "stalled"

    monkeypatch.setattr(time, "time", lambda: seen + 21)
    assert store.peers(stale_after=10)[0]["presence"] == "offline"


def test_presence_reports_rate_limit(store: Store):
    store.heartbeat("claude-code", status="rate-limited do 20:00")

    assert store.peers()[0]["presence"] == "rate-limited"


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


# ── hardening (APP-001, APP-009, APP-012) ───────────────────────────────────


def test_database_files_are_owner_only(tmp_path):
    """APP-001: i s povolnou umask je databáze jen pro vlastníka."""
    import os
    import stat

    old = os.umask(0o000)
    try:
        db = tmp_path / "fresh" / "room.db"
        store = Store(db)
        store.say("a", "ahoj")  # vytvoří i -wal/-shm
    finally:
        os.umask(old)
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    for suffix in ("", "-wal", "-shm"):
        f = db.with_name(db.name + suffix)
        if f.exists():
            assert stat.S_IMODE(f.stat().st_mode) & 0o077 == 0, f"{f.name} je čitelná pro ostatní"


def test_existing_permissive_database_is_tightened(tmp_path):
    import os
    import stat

    db = tmp_path / "room.db"
    Store(db)
    os.chmod(db, 0o644)
    Store(db)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_input_bounds(tmp_path):
    """APP-009: příliš dlouhé vstupy se odmítnou, LIMIT a délka lease mají strop."""
    import time

    import pytest

    from agent_lease_mcp.config import MAX_SHORT_CHARS, MAX_TASK_LEASE_SECONDS, MAX_TEXT_CHARS

    store = Store(tmp_path / "room.db")
    with pytest.raises(ValueError):
        store.send("a", "b", "x" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(ValueError):
        store.heartbeat("a", status="s" * (MAX_SHORT_CHARS + 1))
    with pytest.raises(ValueError):
        store.claim([str(tmp_path / "f")], agent="a", purpose="p" * (MAX_SHORT_CHARS + 1))

    for i in range(6):
        store.say("a", f"m{i}")
    assert len(store.inbox(limit=-1)) == 1, "záporný LIMIT nesmí vrátit všechno"

    store.send("a", "worker", "úkol", kind="task")
    delivery = store.lease_next("worker", lease_seconds=1_000_000_000)
    assert delivery.lease_until - time.time() <= MAX_TASK_LEASE_SECONDS + 5


def test_retention_drops_old_messages_but_keeps_open_tasks(tmp_path):
    """APP-012: staré vzkazy a audit pryč, nedokončený úkol nikdy."""
    import sqlite3
    import time

    db = tmp_path / "room.db"
    store = Store(db)
    old_chat = store.say("a", "starý vzkaz")
    old_task = store.send("a", "worker", "starý nedokončený úkol", kind="task")
    done = store.send("a", "worker", "starý hotový úkol", kind="task")
    store.lease_next("worker")  # vezme old_task (nejstarší)
    d = store.lease_next("worker")
    store.ack(done, "worker", "succeeded", lease_token=d.lease_token)
    store.record("a", "claim", "/x")
    long_ago = time.time() - 40 * 86400
    with sqlite3.connect(db) as con:
        con.execute("UPDATE messages SET sent_at=?", (long_ago,))
        con.execute("UPDATE audit SET at=?", (long_ago,))
    fresh = store.say("a", "čerstvý")

    Store(db)  # retence běží při otevření

    ids = {m["id"] for m in Store(db).inbox(limit=500)}
    assert old_chat not in ids and done not in ids
    assert old_task in ids, "nedokončený úkol se nesmí smazat"
    assert fresh in ids
    assert Store(db).history() == []


def test_foreign_jobs_hide_lease_tokens_and_unrelated_items(tmp_path):
    """APP-003: cizí agent nevidí token ani úkoly, které se ho netýkají."""
    store = Store(tmp_path / "room.db")
    mine = store.send("boss", "worker", "úkol", kind="task")
    store.send("other", "worker", "cizí úkol", kind="task")
    store.lease_next("worker")

    as_worker = store.jobs("worker", viewer="worker")
    assert len(as_worker) == 2, "adresát vidí všechny svoje úkoly"
    assert all("lease_token" not in j for j in as_worker), "token se ve výpisu nevrací nikdy"

    as_boss = store.jobs("worker", viewer="boss")
    assert [j["id"] for j in as_boss] == [mine], "odesílatel vidí jen svoje"

    assert store.jobs("worker", viewer="stranger") == []


def test_only_sender_or_recipient_may_cancel(tmp_path):
    store = Store(tmp_path / "room.db")
    task = store.send("boss", "worker", "úkol", kind="task")
    assert not store.cancel(task, "worker", actor="stranger")
    assert store.cancel(task, "worker", actor="boss")


def test_retention_handles_addressed_chats_and_reply_chains(tmp_path):
    """Nox review: adresovaný chat s pending doručením nesmí zůstat navždy a
    řetěz odpovědí nesmí po retenci odkazovat na smazanou zprávu."""
    import sqlite3
    import time

    db = tmp_path / "room.db"
    store = Store(db)
    chat = store.send("a", "b", "adresovaný chat", kind="chat")
    root = store.say("a", "root")
    middle = store.send("a", "*", "middle", kind="broadcast", reply_to=root)
    long_ago = time.time() - 40 * 86400
    with sqlite3.connect(db) as con:
        con.execute("UPDATE messages SET sent_at=?", (long_ago,))
    fresh = store.send("a", "*", "fresh", kind="broadcast", reply_to=middle)

    Store(db)

    msgs = {m["id"]: m for m in Store(db).inbox(limit=500)}
    assert chat not in msgs, "netask s pending doručením se retenuje"
    assert root not in msgs and middle not in msgs
    assert msgs[fresh]["reply_to"] is None, "žádný visící odkaz"


def test_auto_lease_respects_the_ttl_cap(tmp_path):
    """Nox review: edit_gate musí mít stejný strop jako claim()."""
    import time

    from agent_lease_mcp.config import MAX_TTL_SECONDS, Settings

    db = tmp_path / "room.db"
    st = Store(db, settings=Settings(db, "a", 1_000_000_000))
    target = str(tmp_path / "f.txt")
    assert st.edit_gate(target, "a") is None  # nový auto-nájem
    assert st.holder_of(target).expires_at - time.time() <= MAX_TTL_SECONDS + 5
    assert st.edit_gate(target, "a") is None  # obnova
    assert st.holder_of(target).expires_at - time.time() <= MAX_TTL_SECONDS + 5


def test_prune_removes_receipts_and_screens_too(tmp_path):
    """Nox review: retence nesmí nechat osiřelé potvrzení a screening."""
    import sqlite3
    import time

    import pytest

    db = tmp_path / "room.db"
    st = Store(db)
    mid = st.send("michal", "codex", "starý chat", kind="chat")
    st.mark_seen("codex", [mid])
    st.set_screen(mid, 0.1, 0.2)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE messages SET sent_at=?", (time.time() - 40 * 86400,))
    st.prune()
    with sqlite3.connect(db) as con:
        for table in ("receipts", "screens", "deliveries"):
            assert con.execute(f"SELECT COUNT(*) FROM {table} WHERE message_id=?", (mid,)).fetchone()[0] == 0
    with pytest.raises(ValueError):
        st.prune(-1)
