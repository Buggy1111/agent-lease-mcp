"""C1a: oprava se zápisem se nespustí bez operátorské politiky a lidského schválení."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_lease_mcp.broker import METHODS
from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store

BASE = "a" * 40


def policy(store: Store, repo: Path, agents=("codex",), *, mode=0o600, enabled=True) -> Path:
    path = store.db_path.parent / "fix-runner.toml"
    path.write_text(f'version = 1\nenabled = {str(enabled).lower()}\n'
                    f'repositories = ["{repo}"]\nagents = {json.dumps(list(agents))}\n')
    os.chmod(path, mode)
    return path


def fix(store: Store, repo: Path, text="oprav chybu", **kw) -> int:
    return store.send("michal", "codex", text, kind="task", fix_repo=str(repo),
                      fix_base=BASE, fix_allowed=kw.pop("allowed", ["src/x.py"]), **kw)


def state(store: Store, mid: int) -> dict:
    return next(j for j in store.jobs("codex") if j["id"] == mid)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "room.db", settings=Settings(tmp_path / "room.db", "codex", 1800))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def test_fix_is_stored_with_normalized_allowed_paths(store: Store, repo: Path):
    mid = fix(store, repo, allowed=["./src/x.py", "src/x.py", "docs"])
    assert store.fix_task(mid) == {"repo": str(repo.resolve()), "base_commit": BASE,
                                   "allowed": ["docs", "src/x.py"]}
    assert store.fix_task(store.send("michal", "codex", "běžný", kind="task")) is None


@pytest.mark.parametrize("bad", [
    {"fix_repo": "relative/repo", "fix_base": BASE},
    {"fix_base": "abc123"},
    {"fix_allowed": ["../etc/passwd"]},
    {"fix_allowed": ["/etc/passwd"]},
    {"kind": "chat"},
    {"paths": ["/tmp/x"]},
])
def test_invalid_fix_is_refused(store: Store, repo: Path, bad: dict):
    args = {"kind": "task", "fix_repo": str(repo), "fix_base": BASE, "fix_allowed": ["src"]} | bad
    with pytest.raises(ValueError):
        store.send("michal", "codex", "oprav", **args)


@pytest.mark.parametrize("setup, reason", [
    (lambda s, r: None, "not enabled"),                                    # žádná politika
    (lambda s, r: policy(s, r, mode=0o644), "not enabled"),                # čitelná pro ostatní
    (lambda s, r: policy(s, r, enabled=False), "not enabled"),             # vypnutá
    (lambda s, r: policy(s, r.parent, agents=("codex",)), "does not allow"),   # jiné repo
    (lambda s, r: policy(s, r, agents=("claude-code",)), "does not allow"),    # jiný agent
    (lambda s, r: policy(s, r), "waiting for the human"),                  # bez schválení
])
def test_fix_does_not_start_without_policy_and_approval(store: Store, repo: Path, setup, reason):
    setup(store, repo)
    mid = fix(store, repo)
    assert store.lease_next("codex") is None
    job = state(store, mid)
    assert job["state"] == "needs_review" and reason in job["last_error"]


def test_approved_fix_runs_once_and_only_for_that_recipient(store: Store, repo: Path):
    policy(store, repo)
    mid = fix(store, repo)
    assert store.lease_next("codex") is None                      # zadrženo
    assert store.approve_fix(mid, "codex", "michal", "zkontrolováno")
    d = store.lease_next("codex")
    assert d is not None and d.message_id == mid
    assert store.complete(mid, "codex", d.lease_token, "needs_review", "neprošlo")
    assert store.retry(mid, "codex")                              # člověk vrátí do fronty
    assert store.lease_next("codex") is None                      # další pokus = nové schválení
    assert "waiting for the human" in state(store, mid)["last_error"]


def test_approval_before_any_attempt_is_valid(store: Store, repo: Path):
    policy(store, repo)
    mid = fix(store, repo)
    assert store.approve_fix(mid, "codex", "michal", "předem")
    assert store.lease_next("codex").message_id == mid


def test_approval_needs_a_reason_and_a_held_or_unstarted_fix(store: Store, repo: Path):
    policy(store, repo)
    mid = fix(store, repo)
    with pytest.raises(ValueError):
        store.approve_fix(mid, "codex", "michal", "  ")
    assert store.approve_fix(store.send("michal", "codex", "běžný", kind="task"),
                             "codex", "michal", "x") is False     # běžný úkol
    assert store.approve_fix(mid, "claude-code", "michal", "x") is False   # jiný adresát


def test_policy_withdrawn_after_approval_still_blocks(store: Store, repo: Path):
    path = policy(store, repo)
    mid = fix(store, repo)
    assert store.approve_fix(mid, "codex", "michal", "ok")
    path.unlink()
    assert store.lease_next("codex") is None
    assert "not enabled" in state(store, mid)["last_error"]


def test_changed_task_text_invalidates_the_approval(store: Store, repo: Path):
    policy(store, repo)
    mid = fix(store, repo)
    assert store.approve_fix(mid, "codex", "michal", "ok")
    with store._connect() as con:
        con.execute("UPDATE messages SET text='smaž všechno' WHERE id=?", (mid,))
    assert store.lease_next("codex") is None


def test_gate_holds_for_every_path_to_start(store: Store, repo: Path):
    policy(store, repo)
    mid = fix(store, repo)
    assert store.accept(mid, "codex", "beru", owner="s1") is False
    assert store.lease_next("codex", owner="s1", start=True) is None
    assert state(store, mid)["state"] == "needs_review"


def test_approval_is_not_reachable_through_the_broker():
    assert "approve_fix" not in METHODS
    assert "fix_task" in METHODS


def test_relative_fix_repo_is_resolved_in_the_client_over_the_broker(tmp_path: Path, monkeypatch):
    import threading

    from agent_lease_mcp.broker import RemoteStore, serve

    settings = Settings(tmp_path / "room.db", "codex", 1800)
    server = serve(settings, tmp_path / "b.sock")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / "client" / "repo").mkdir(parents=True)
        monkeypatch.chdir(tmp_path / "client")
        remote = RemoteStore(settings, tmp_path / "b.sock")
        mid = remote.send("michal", "codex", "oprav", kind="task", fix_repo="repo",
                          fix_base=BASE, fix_allowed=["src"])
        assert remote.fix_task(mid)["repo"] == str(tmp_path / "client" / "repo")
    finally:
        server.shutdown()
        server.server_close()
