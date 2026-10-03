from __future__ import annotations

import json
import threading
from pathlib import Path

from agent_lease_mcp.config import Settings
from agent_lease_mcp.init_cmd import merge, run_init
from agent_lease_mcp.store import Store


def test_init_writes_all_hooks_idempotently_and_keeps_foreign_ones(tmp_path: Path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}
    path.write_text(json.dumps({"theme": "dark", "hooks": {"PreToolUse": [foreign]}}))
    assert run_init("claude-code", write=True, rewake=True, home=tmp_path) == 0
    assert run_init("claude-code", write=True, rewake=True, home=tmp_path) == 0  # podruhé beze změny
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and foreign in data["hooks"]["PreToolUse"]
    pre = data["hooks"]["PreToolUse"]
    assert sum("agent-lease-guard" in json.dumps(e) for e in pre) == 1
    assert "agent-lease-context" in json.dumps(data["hooks"]["UserPromptSubmit"])
    assert "--rewake" in json.dumps(data["hooks"]["SessionStart"])
    assert "agent-lease-release" in json.dumps(data["hooks"]["Stop"])
    assert path.with_suffix(".json.bak").exists()


def test_init_dry_run_and_bad_json(tmp_path: Path, capsys):
    assert run_init("codex", write=False, rewake=False, home=tmp_path) == 0
    assert not (tmp_path / ".codex").exists()
    bad = tmp_path / ".codex" / "hooks.json"
    bad.parent.mkdir()
    bad.write_text("{nope")
    assert run_init("codex", write=True, rewake=False, home=tmp_path) == 1
    assert bad.read_text() == "{nope"  # nepoškozeno


def test_merge_does_not_duplicate():
    ours = {"Stop": [{"hooks": [{"type": "command", "command": "AGENT_NAME=x agent-lease-release"}]}]}
    once = merge({}, ours)
    assert merge(once, ours) == once


def test_every_task_is_leased_exactly_once_under_contention(tmp_path: Path):
    db = tmp_path / "r.db"
    store = Store(db, settings=Settings(db, "x", 1800))
    ids = {store.send("michal", "worker", f"t{i}", kind="task") for i in range(40)}
    got: list[int] = []
    lock = threading.Lock()

    def worker():
        s = Store(db, settings=Settings(db, "x", 1800))
        while (d := s.lease_next("worker", lease_seconds=60)):
            with lock:
                got.append(d.message_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(got) == sorted(ids)  # nic dvakrát, nic nechybí


def test_concurrent_claims_have_one_winner(tmp_path: Path):
    db = tmp_path / "r.db"
    results: list[bool] = []

    def go(agent):
        s = Store(db, settings=Settings(db, "x", 1800))
        results.append(s.claim([str(tmp_path / "f.txt")], agent=agent).ok)

    ts = [threading.Thread(target=go, args=(f"a{i}",)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1
