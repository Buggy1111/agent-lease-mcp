"""Integrace fáze C: oprava (C1b/C2) + měření a stropy (C3) + připnutý model (#21)."""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent_lease_mcp import bridge as bridge_mod
from agent_lease_mcp.bridge import DEFAULT_COMMANDS, Bridge, RunResult
from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store

pytestmark = pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="bubblewrap není k dispozici")


@pytest.fixture
def env(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*g, "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run([*g, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*g, "commit", "-qm", "base"], cwd=repo, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    db = tmp_path / "state" / "room.db"
    db.parent.mkdir()
    store = Store(db, settings=Settings(db, "codex", 1800))
    pol = db.parent / "fix-runner.toml"
    pol.write_text(f'version = 1\nenabled = true\nrepositories = ["{repo}"]\nagents = ["codex"]\n')
    os.chmod(pol, 0o600)
    mid = store.send("michal", "codex", "oprav", kind="task", fix_repo=str(repo), fix_base=base, fix_allowed=[])
    assert store.approve_fix(mid, "codex", "michal", "test")
    return store, mid, tmp_path


def test_fix_run_uses_write_command_with_pinned_model_and_records_usage(env, monkeypatch):
    store, mid, tmp = env
    monkeypatch.setenv("AGENT_LEASE_FIX_DIR", str(tmp / "fixes"))
    monkeypatch.delenv("AGENT_LEASE_FIX_COMMAND", raising=False)
    with store._connect() as con:  # opravy běží jen v Autonomním (D2); v testu obejdeme zámek režimu
        con.execute("INSERT INTO control(key, value, changed_by, changed_at) VALUES('mode','autonomous','michal',0)")
    seen = {}

    def fake_run(command, prompt, **kw):
        seen["command"] = command
        return RunResult(True, "hotovo", usage={"source": "codex-jsonl", "model": "gpt-5.5", "input_tokens": 1200,
                                               "output_tokens": 30, "cache_read_tokens": 0,
                                               "cache_creation_tokens": 0, "cost_usd": None})
    monkeypatch.setattr(bridge_mod, "run_command", fake_run)
    Bridge(store, "codex", DEFAULT_COMMANDS["codex"], requested_model="gpt-5.5").step()
    assert "workspace-write" in seen["command"] and "--json" in seen["command"] and "-m gpt-5.5" in seen["command"]
    row = sqlite3.connect(store.db_path).execute(
        "SELECT requested_model, model, input_tokens FROM task_usage WHERE message_id=?", (mid,)).fetchone()
    assert row == ("gpt-5.5", "gpt-5.5", 1200)          # oprava je v evidenci i se stropy (C3)


def test_fix_run_is_blocked_by_daily_budget(env, monkeypatch):
    store, _mid, _tmp = env
    with sqlite3.connect(store.db_path) as con:            # minulý pokus vyčerpal denní strop
        con.execute("INSERT INTO task_usage(message_id, recipient, attempt, at, source, input_tokens, output_tokens,"
                    " cache_read_tokens, cache_creation_tokens, cost_usd) VALUES(9999,'codex',0,strftime('%s','now'),"
                    "'codex-jsonl',500,0,0,0,NULL)")
    store.settings = Settings(store.db_path, "codex", 1800, daily_budget_tokens=100)
    called = []
    monkeypatch.setattr(bridge_mod, "run_command", lambda *a, **k: called.append(1))
    Bridge(store, "codex", DEFAULT_COMMANDS["codex"]).step()
    assert not called                                      # strop platí i pro opravy
