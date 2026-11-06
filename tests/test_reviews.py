"""B3 (review stavový automat, verdikt vázaný na commit) a B4 (max 3 opravná kola)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_lease_mcp import broker
from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store

A, B, C, D, E = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
AUTHOR, REVIEWER = "claude-code", "codex"


def make(tmp_path: Path) -> Store:
    db = tmp_path / "room.db"
    (tmp_path / "human-name").write_text("michal")
    return Store(db, settings=Settings(db, AUTHOR, 1800))


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return make(tmp_path)


def opened(store: Store, tmp_path: Path, head: str = A, reviewer: str = REVIEWER) -> dict:
    return store.review_open(AUTHOR, str(tmp_path), "fix: run_fence", reviewer=reviewer, head_commit=head,
                             secret_findings=[])


def test_open_with_commit_waits_for_reviewer_and_notifies(store, tmp_path):
    review = opened(store, tmp_path)
    assert review["state"] == "awaiting_review" and review["head_commit"] == A
    assert review["fix_cycles"] == 0 and review["cycles_left"] == 3
    inbox = store.inbox(since_id=0)
    assert any(m["recipient"] == REVIEWER and f"#{review['id']}" in m["text"] for m in inbox)


def test_open_without_commit_is_working(store, tmp_path):
    review = store.review_open(AUTHOR, str(tmp_path), "wip")
    assert review["state"] == "working"
    review = store.review_submit(AUTHOR, review["id"], A, secret_findings=[])
    assert review["state"] == "awaiting_review"


def test_author_cannot_approve_own_work(store, tmp_path):
    review = opened(store, tmp_path, reviewer="")
    with pytest.raises(PermissionError):
        store.review_verdict(AUTHOR, review["id"], "approved", A, security="pass", reviewer_findings=[])
    with pytest.raises(PermissionError):
        store.review_open(AUTHOR, str(tmp_path), "other", reviewer=AUTHOR)


def test_only_assigned_reviewer_gives_verdict(store, tmp_path):
    review = opened(store, tmp_path)
    with pytest.raises(PermissionError):
        store.review_verdict("gemini", review["id"], "approved", A, security="pass", reviewer_findings=[])
    assert store.review_verdict(REVIEWER, review["id"], "approved", A, security="pass", reviewer_findings=[])["state"] == "approved"


def test_verdict_on_other_commit_is_rejected(store, tmp_path):
    review = opened(store, tmp_path)
    store.review_submit(AUTHOR, review["id"], B, secret_findings=[])  # autor mezitím předložil nový commit
    with pytest.raises(ValueError, match="stale verdict"):
        store.review_verdict(REVIEWER, review["id"], "approved", A, security="pass", reviewer_findings=[])
    assert store.review_get(review["id"])["state"] == "awaiting_review"


def test_new_commit_after_approval_voids_the_approval(store, tmp_path):
    review = opened(store, tmp_path)
    store.review_verdict(REVIEWER, review["id"], "approved", A, security="pass", reviewer_findings=[])
    assert store.review_approved(str(tmp_path), A)
    again = store.review_submit(AUTHOR, review["id"], B, secret_findings=[])
    assert again["state"] == "awaiting_review" and again["verdict"] is None
    assert store.review_approved(str(tmp_path), A) is None
    assert store.review_approved(str(tmp_path), B) is None
    assert again["fix_cycles"] == 0  # nová práce po schválení není opravné kolo


def test_same_commit_resubmitted_is_a_no_op(store, tmp_path):
    review = opened(store, tmp_path)
    store.review_verdict(REVIEWER, review["id"], "approved", A, security="pass", reviewer_findings=[])
    assert store.review_submit(AUTHOR, review["id"], A, secret_findings=[])["state"] == "approved"


def test_only_author_submits(store, tmp_path):
    review = opened(store, tmp_path)
    with pytest.raises(PermissionError):
        store.review_submit(REVIEWER, review["id"], B)


def test_changes_requested_needs_a_note_and_full_hashes(store, tmp_path):
    review = opened(store, tmp_path)
    with pytest.raises(ValueError):
        store.review_verdict(REVIEWER, review["id"], "changes_requested", A, "")
    with pytest.raises(ValueError):
        store.review_verdict(REVIEWER, review["id"], "approved", "HEAD")
    with pytest.raises(ValueError):
        store.review_verdict(REVIEWER, review["id"], "approved", A[:12])
    with pytest.raises(ValueError):
        store.review_verdict(REVIEWER, review["id"], "maybe", A)


def test_three_fix_cycles_then_human_even_after_restart(tmp_path):
    store = make(tmp_path)
    review = opened(store, tmp_path)
    rid = review["id"]
    heads = [B, C, D]
    current = A
    for cycle, head in enumerate(heads, start=1):
        store.review_verdict(REVIEWER, rid, "changes_requested", current, f"oprav {cycle}")
        state = store.review_submit(AUTHOR, rid, head)
        assert state["fix_cycles"] == cycle
        current = head
    final = store.review_verdict(REVIEWER, rid, "changes_requested", current, "pořád špatně")
    assert final["state"] == "escalated" and final["cycles_left"] == 0
    human_inbox = [m for m in store.inbox(since_id=0) if m["recipient"] == "michal"]
    assert any("ESKALOVÁNO" in m["text"] for m in human_inbox)

    restarted = make(tmp_path)  # nový proces, stejná DB
    with pytest.raises(PermissionError, match="fix cycles"):
        restarted.review_submit(AUTHOR, rid, E, secret_findings=[])
    with pytest.raises(PermissionError):
        restarted.review_close(AUTHOR, rid, "vzdávám")
    assert restarted.review_get(rid)["head_commit"] == D


def test_human_grants_one_more_cycle(store, tmp_path):
    review = opened(store, tmp_path)
    rid, current = review["id"], A
    for head in (B, C, D):
        store.review_verdict(REVIEWER, rid, "changes_requested", current, "x")
        store.review_submit(AUTHOR, rid, head)
        current = head
    store.review_verdict(REVIEWER, rid, "changes_requested", current, "x")
    with pytest.raises(ValueError):
        store.review_grant_cycle("michal", rid, "")
    granted = store.review_grant_cycle("michal", rid, "poslední pokus")
    assert granted["state"] == "changes_requested" and granted["max_fix_cycles"] == 4
    assert store.review_submit(AUTHOR, rid, E, secret_findings=[])["fix_cycles"] == 4
    escalated = store.review_verdict(REVIEWER, rid, "changes_requested", E, "ne")
    assert escalated["state"] == "escalated"


def test_agents_cannot_act_as_the_human(store, tmp_path):
    review = opened(store, tmp_path)
    for name in ("michal", "human", "broker"):
        with pytest.raises(PermissionError):
            store.review_verdict(name, review["id"], "approved", A, security="pass", reviewer_findings=[])
        with pytest.raises(PermissionError):
            store.review_open(name, str(tmp_path), "t")
    # Lidské metody nejsou dostupné přes broker.
    assert "review_grant_cycle" not in broker.METHODS
    assert "review_human_close" not in broker.METHODS
    assert {"review_open", "review_submit", "review_verdict", "review_close"} <= broker.METHODS


def test_close_rules_and_one_open_review_per_topic(store, tmp_path):
    review = opened(store, tmp_path)
    with pytest.raises(ValueError, match="still open"):
        opened(store, tmp_path, head=B)
    with pytest.raises(PermissionError):
        store.review_close(REVIEWER, review["id"])
    store.review_verdict(REVIEWER, review["id"], "approved", A, security="pass", reviewer_findings=[])
    closed = store.review_close(AUTHOR, review["id"], "sloučeno")
    assert closed["state"] == "closed"
    assert store.review_approved(str(tmp_path), A)  # sloučený schválený commit zůstává schválený
    with pytest.raises(ValueError):
        store.review_submit(AUTHOR, review["id"], B, secret_findings=[])
    assert opened(store, tmp_path, head=B)["state"] == "awaiting_review"  # téma je zase volné
    assert store.review_human_close("michal", review["id"])["state"] == "closed"


def test_history_records_every_transition(store, tmp_path):
    review = opened(store, tmp_path)
    store.review_verdict(REVIEWER, review["id"], "changes_requested", A, "chybí test")
    store.review_submit(AUTHOR, review["id"], B, secret_findings=[])
    store.review_verdict(REVIEWER, review["id"], "approved", B, security="pass", reviewer_findings=[])
    events = store.review_get(review["id"])["events"]
    assert [e["event"] for e in events] == ["open", "changes_requested", "fix", "approved"]
    assert [e["commit_sha"] for e in events] == [A, A, B, B]
    assert [r["id"] for r in store.reviews()] == [review["id"]]
    assert store.reviews(state="closed") == []


def test_author_cannot_reset_the_cycle_budget_by_close_and_reopen(store, tmp_path):
    """Nox #655/P1: close před eskalací + nové review téhož tématu vynulovalo kola."""
    review = opened(store, tmp_path)
    rid, current = review["id"], A
    for head in (B, C, D):
        store.review_verdict(REVIEWER, rid, "changes_requested", current, "x")
        store.review_submit(AUTHOR, rid, head)
        current = head
    assert store.review_get(rid)["state"] == "awaiting_review"
    with pytest.raises(PermissionError):
        store.review_close(AUTHOR, rid, "zkusím znovu")
    with pytest.raises(ValueError, match="still open"):
        opened(store, tmp_path, head=D)
    store.review_verdict(REVIEWER, rid, "changes_requested", current, "x")
    assert store.review_get(rid)["state"] == "escalated"

    second = store.review_open(AUTHOR, str(tmp_path), "jiné téma", reviewer=REVIEWER, head_commit=A)
    store.review_verdict(REVIEWER, second["id"], "changes_requested", A, "x")
    with pytest.raises(PermissionError):
        store.review_close(AUTHOR, second["id"])  # ani z changes_requested
    wip = store.review_open(AUTHOR, str(tmp_path), "rozpracované")
    assert store.review_close(AUTHOR, wip["id"])["state"] == "closed"  # nic nepředloženo


def test_concurrent_verdicts_and_submits_serialize(tmp_path):
    import threading

    stores = [make(tmp_path) for _ in range(4)]
    review = opened(stores[0], tmp_path)
    rid = review["id"]
    results: list[str] = []
    barrier = threading.Barrier(2)

    def verdict(s, kind):
        barrier.wait()
        try:
            s.review_verdict(REVIEWER, rid, kind, A, "x", security="pass", reviewer_findings=[])
            results.append(kind)
        except ValueError:
            results.append("rejected")

    threads = [threading.Thread(target=verdict, args=(stores[0], "approved")),  # security níže
               threading.Thread(target=verdict, args=(stores[1], "changes_requested"))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results).count("rejected") == 1

    final = stores[2].review_get(rid)
    if final["state"] == "approved":
        stores[2].review_submit(AUTHOR, rid, B, secret_findings=[])
        stores[2].review_verdict(REVIEWER, rid, "changes_requested", B, "x")
    barrier2 = threading.Barrier(2)

    def submit(s, head):
        barrier2.wait()
        s.review_submit(AUTHOR, rid, head, secret_findings=[])

    threads = [threading.Thread(target=submit, args=(stores[2], C)),
               threading.Thread(target=submit, args=(stores[3], D))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    after = stores[0].review_get(rid)
    assert after["fix_cycles"] == 1 and after["state"] == "awaiting_review"
