"""Runner policy and refusal tests that do not need privileged namespaces."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from test_quality import commit, git, python_project

from agent_lease_mcp.config import Settings
from agent_lease_mcp.quality import inspect_repository, policy_path
from agent_lease_mcp.quality_runner import (
    SetupRequired,
    committed_branch,
    export_snapshot,
    load_policy,
    run_quality,
    tree_digest,
)
from agent_lease_mcp.store import Store


def make_repo(tmp_path):
    repo = tmp_path / "project"
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)
    git(repo, "config", "user.name", "Runner test")
    git(repo, "config", "user.email", "runner@example.invalid")
    commit(repo, {"README.md": "fixture"})
    python_project(repo)
    return repo


def write_policy(store, repo, *, mounts=None, tools=None, timeout=10):
    tools = tools or {"python": str(Path("/usr/bin/python3").resolve())}
    text = 'version=1\nenabled=true\nrepositories=' + json.dumps([str(repo)])
    text += f"\ntimeout_seconds={timeout}\n"
    text += '\n[policy_bases]\n' + json.dumps(str(repo)) + '=' + json.dumps(git(repo, "rev-parse", "HEAD^")) + '\n'
    for name, root in (mounts or {}).items():
        text += f'\n[mounts.{name}]\npath={json.dumps(str(root))}\ntree_sha256="{tree_digest(root)}"\n'
    for name, path in tools.items():
        text += f'\n[tools.{name}]\npath={json.dumps(path)}\nsha256="{hashlib.sha256(Path(path).read_bytes()).hexdigest()}"\n'
    path = store.db_path.parent / "quality-runner.toml"
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def fixture(tmp_path):
    repo = make_repo(tmp_path)
    room = tmp_path / "room"
    store = Store(settings=Settings(room / "room.db", "codex", 1800))
    return repo, store


def test_default_is_disabled_and_no_project_execution(fixture, monkeypatch):
    repo, store = fixture
    def must_not_launch(*_args, **_kwargs):
        raise AssertionError("must not execute without operator policy")
    monkeypatch.setattr("agent_lease_mcp.quality_runner.supervise", must_not_launch)
    result = run_quality(store, "codex", str(repo))
    assert result["overall_status"] == "SETUP_REQUIRED" and result["executed"] is False
    assert store.quality_runs() == []


def test_policy_is_independent_of_project_and_pins_tools(fixture):
    repo, store = fixture
    path = write_policy(store, repo)
    assert load_policy(store.db_path, str(repo))["tools"]["python"].startswith("/usr/")
    path.write_text(path.read_text().replace('enabled=true', 'enabled=false'))
    with pytest.raises(SetupRequired):
        load_policy(store.db_path, str(repo))
    path = write_policy(store, repo)
    path.chmod(0o644)
    with pytest.raises(SetupRequired):
        load_policy(store.db_path, str(repo))


def test_policy_symlink_and_changed_executable_pin_rejected(fixture):
    repo, store = fixture
    path = write_policy(store, repo)
    source = path.with_suffix(".original")
    path.rename(source)
    path.symlink_to(source)
    with pytest.raises(OSError):
        load_policy(store.db_path, str(repo))
    path.unlink()
    source.rename(path)
    text = path.read_text()
    pin = hashlib.sha256(Path("/usr/bin/python3").resolve().read_bytes()).hexdigest()
    path.write_text(text.replace(pin, "f" * 64))
    with pytest.raises(SetupRequired):
        load_policy(store.db_path, str(repo))


def test_tool_tree_digest_detects_edits_and_external_links(tmp_path):
    root = tmp_path / "tools"
    root.mkdir()
    file = root / "tool"
    file.write_text("before")
    before = tree_digest(root)
    file.write_text("after")
    assert tree_digest(root) != before
    (root / "outside").symlink_to("/etc/passwd")
    with pytest.raises(ValueError):
        tree_digest(root)


def test_export_uses_git_blobs_not_dirty_checkout_or_attributes(fixture, tmp_path):
    repo, _ = fixture
    head = commit(repo, {"value.txt": "committed", ".gitattributes": "value.txt export-ignore\n"})
    (repo / "value.txt").write_text("DIRTY")
    out = tmp_path / "snapshot"
    out.mkdir()
    export_snapshot(str(repo), head, out)
    assert (out / "value.txt").read_text() == "committed"
    assert not (out / ".git").exists()


def test_snapshot_links_are_rejected(fixture, tmp_path):
    repo, _ = fixture
    (repo / "link").symlink_to("/etc/passwd")
    git(repo, "add", "link")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "link")
    out = tmp_path / "snapshot"
    out.mkdir()
    with pytest.raises(SetupRequired):
        export_snapshot(str(repo), git(repo, "rev-parse", "HEAD"), out)


def test_pause_and_missing_limits_prevent_launch(fixture, monkeypatch):
    repo, store = fixture
    write_policy(store, repo)
    def no_limits():
        raise SetupRequired("Missing limits.")
    monkeypatch.setattr("agent_lease_mcp.quality_runner.verify_limits", no_limits)
    result = run_quality(store, "codex", str(repo))
    assert not result["executed"] and result["overall_status"] == "SETUP_REQUIRED"
    store.pause("michal", "fixture stop")
    assert run_quality(store, "codex", str(repo))["executed"] is False


def test_no_arbitrary_commands_or_results_can_be_imported(fixture):
    repo, store = fixture
    with pytest.raises(TypeError):
        store.quality_check("codex", str(repo), commands=["true"], overall_status="PASS")
    assert store.quality_runs() == []


def test_bad_task_binding_refuses_before_isolation(fixture):
    repo, store = fixture
    result = store.quality_check("codex", str(repo), task_id=9, lease_token="fixture-invalid")
    assert not result["executed"]


def test_runner_cli_identity_required_without_writing_room(tmp_path, monkeypatch, capsys):
    from agent_lease_mcp.cli import main
    monkeypatch.delenv("AGENT_NAME", raising=False)
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "absent" / "room.db"))
    assert main(["quality", "check", "--repo", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)["overall_status"] == "SETUP_REQUIRED"
    assert not (tmp_path / "absent").exists()


def test_process_metadata_log_is_private_and_has_no_raw_output(fixture):
    from test_webui import authenticated_handler, call_handler

    from agent_lease_mcp._quality_runs import CommandObservation
    repo, store = fixture
    plan = inspect_repository(repo)
    run = store._quality_record_run("codex", plan,
        (CommandObservation("tests", 1, False, 20, 32, "b" * 64),), execution_id="a" * 32)
    handler = authenticated_handler(store, store.settings, "fixture-only")
    path = f'/api/quality-log?run_id={run["run_id"]}&check=tests'
    assert call_handler(store, store.settings, "fixture-only", "GET", path, handler_type=handler)[0] == 403
    status, body = call_handler(store, store.settings, "fixture-only", "GET", path,
        headers={"Cookie": "agent_lease_session=session"}, handler_type=handler)
    assert status == 200 and json.loads(body)["observation"]["exit_code"] == 1
    assert "fully withheld" in body.decode()
    assert store.quality_log(run["run_id"], "../../etc/passwd") is None


def test_command_interruption_cannot_pass(fixture):
    from agent_lease_mcp._quality_runs import CommandObservation
    repo, store = fixture
    plan = inspect_repository(repo)
    run = store._quality_record_run("codex", plan,
        (CommandObservation("tests", -9, False, 20, 0, "b" * 64, True),), execution_id="c" * 32)
    assert run["overall_status"] == "REVIEW" and run["execution_status"] == "interrupted"


@pytest.mark.parametrize("name", ["_reviews.py", "store.py", "broker.py", "webui.py", "lifecycle.py",
                                  "config.py", "launch_gate.py", "process_guard.py", "cgroup.py", "run_fence.py"])
def test_gate_authority_changes_also_require_human_review(name):
    assert policy_path("src/agent_lease_mcp/"+name)
    assert not policy_path("src/another_project/"+name)


def test_branch_label_is_only_for_exact_current_head(fixture):
    repo, _ = fixture
    head = git(repo, "rev-parse", "HEAD")
    assert committed_branch(str(repo), head) == "main"
    commit(repo, {"src/app.py": "answer=42\n"})
    assert committed_branch(str(repo), head) is None
    git(repo, "checkout", "--detach", "-q", head)
    assert committed_branch(str(repo), head) is None


def test_check_timeout_is_bounded_by_fixed_room_job_budget(fixture):
    repo, store = fixture
    write_policy(store, repo, timeout=600)
    assert load_policy(store.db_path, str(repo))["timeout"] == 600
    write_policy(store, repo, timeout=601)
    with pytest.raises(SetupRequired):
        load_policy(store.db_path, str(repo))
