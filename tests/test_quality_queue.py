"""Durability, deduplication and opt-in hook behavior without project execution."""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from test_quality_runner import make_repo, write_policy

from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store


@pytest.fixture
def fixture(tmp_path):
    repo = make_repo(tmp_path)
    store = Store(settings=Settings(tmp_path / "room" / "room.db", "codex", 1800))
    policy = write_policy(store, repo)
    return repo, store, policy


def test_queue_requires_policy_and_deduplicates_by_commit_and_policy(fixture):
    repo, store, policy = fixture
    first = store.quality_request("codex", str(repo))
    assert first["state"] == "queued" and first["run_id"] is None
    assert store.quality_request("codex", str(repo))["id"] == first["id"]
    assert len(store.quality_requests()) == 1
    policy.write_text(policy.read_text().replace("timeout_seconds=10", "timeout_seconds=9"))
    assert store.quality_request("codex", str(repo))["id"] != first["id"]
    policy.unlink()
    with pytest.raises(OSError):
        store.quality_request("codex", str(repo))


def test_queued_survives_restart_but_started_is_never_replayed(fixture):
    repo, store, _ = fixture
    request = store.quality_request("codex", str(repo))
    restarted = Store(settings=store.settings)
    assert restarted.quality_requests()[0]["state"] == "queued"
    with store._connect() as con:
        con.execute("UPDATE quality_requests SET state='running' WHERE id=?", (request["id"],))
    restarted._quality_recover_requests()
    assert restarted.quality_requests()[0]["state"] == "interrupted"
    assert not restarted._quality_drain_one()


def test_pause_and_changed_operator_policy_prevent_queued_execution(fixture, monkeypatch):
    repo, store, policy = fixture
    store.quality_request("codex", str(repo))
    runner = Mock(side_effect=AssertionError("project must not run"))
    monkeypatch.setattr(store, "quality_check", runner)
    store.pause("michal", "fixture")
    assert not store._quality_drain_one()
    # Synthetic room only: remove pause as a human test action.
    store.resume("michal", "fixture")
    policy.write_text(policy.read_text().replace("timeout_seconds=10", "timeout_seconds=9"))
    assert store._quality_drain_one()
    assert store.quality_requests()[0]["state"] == "refused"
    runner.assert_not_called()


def test_stop_hook_only_enqueues_clean_commits_when_explicitly_enabled(fixture, monkeypatch):
    from agent_lease_mcp import lifecycle
    repo, store, _ = fixture
    remote = Mock()
    remote.release_all.return_value = []
    remote.quality_autorun_enabled.return_value = False
    monkeypatch.setattr(lifecycle.Settings, "from_env", lambda: store.settings)
    monkeypatch.setattr(lifecycle, "open_store", lambda _settings: remote)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("AGENT_LEASE_QUALITY_AUTORUN", raising=False)
    assert lifecycle.release_main() == 0
    remote.quality_request.assert_not_called()
    monkeypatch.setenv("AGENT_LEASE_QUALITY_AUTORUN", "1")
    remote.release_all.return_value = []
    assert lifecycle.release_main() == 0
    remote.quality_request.assert_called_once()
    (repo / "README.md").write_text("dirty")
    assert lifecycle.release_main() == 0
    assert remote.quality_request.call_count == 1


def test_review_submission_only_enqueues_after_explicit_opt_in(fixture, monkeypatch):
    repo, store, _ = fixture
    from test_quality import git
    head = git(repo, "rev-parse", "HEAD")
    base = git(repo, "rev-parse", "HEAD^")
    monkeypatch.setenv("AGENT_LEASE_QUALITY_AUTORUN", "1")
    store.review_open("codex", str(repo), "fixture-auto", "claude-code", base, head, secret_findings=[])
    assert len(store.quality_requests()) == 1


def test_one_time_operator_activation_needs_no_agent_config_edit(fixture):
    _repo, store, policy = fixture
    assert not store.quality_autorun_enabled()
    policy.write_text(policy.read_text().replace("enabled=true", "enabled=true\nautomatic=true"))
    assert store.quality_autorun_enabled()
    policy.chmod(0o644)
    assert not store.quality_autorun_enabled()


@pytest.mark.parametrize("state", ["refused", "interrupted", "busy"])
def test_explicit_retry_after_unsuccessful_request(fixture, state):
    repo, store, _ = fixture
    first = store.quality_request("codex", str(repo))
    with store._connect() as con:
        con.execute("UPDATE quality_requests SET state=? WHERE id=?", (state, first["id"]))
    retry = store.quality_request("codex", str(repo))
    assert retry["state"] == "queued" and retry["id"] == first["id"]


def test_busy_room_is_distinct_and_can_be_requeued(fixture, monkeypatch):
    import fcntl
    repo, store, _ = fixture
    monkeypatch.setattr("agent_lease_mcp.quality_runner.verify_limits", dict)
    request = store.quality_request("codex", str(repo))
    with open(str(store.db_path.resolve()) + ".quality-lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert store._quality_drain_one()
    assert store.quality_requests()[0]["state"] == "busy"
    retry = store.quality_request("codex", str(repo))
    assert retry["id"] == request["id"] and retry["state"] == "queued"

