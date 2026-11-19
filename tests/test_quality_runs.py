"""Internal runner evidence, authority boundaries and live read-only projection."""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from test_quality import commit, git, python_project
from test_webui import authenticated_handler, call_handler

from agent_lease_mcp._quality_runs import CommandObservation
from agent_lease_mcp.broker import METHODS, RemoteStore, serve
from agent_lease_mcp.config import Settings
from agent_lease_mcp.quality import inspect_repository, policy_path
from agent_lease_mcp.store import Store


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project with spaces"
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(root)], check=True)
    git(root, "config", "user.name", "Quality test")
    git(root, "config", "user.email", "quality@example.invalid")
    commit(root, {"README.md": "synthetic fixture\n"})
    return root


@pytest.fixture
def store(tmp_path):
    return Store(settings=Settings(tmp_path / "quality.db", "codex", 1800))


@pytest.fixture
def plan(repo):
    python_project(repo)
    # A source-only follow-up: policy changes are tested separately.
    head = commit(repo, {"src/app.py": "answer = 42\n"})
    return inspect_repository(repo, head)


def observation(name="tests", *, exit_code=0, timed_out=False):
    return CommandObservation(name, exit_code, timed_out, 20, 0, hashlib.sha256(b"").hexdigest())


def record(store, plan, *, outcomes=None, execution_id=None):
    return store._quality_record_run("codex", plan, outcomes or tuple(
        observation(check.name) for check in plan.checks if check.command
    ), execution_id=execution_id or uuid.uuid4().hex)


def review(store, plan, **overrides):
    # Synthetic DB authority fixtures only, never the user's room. Exercise
    # projections independently of Git scans/notifications in ReviewsMixin.
    fields = {"repo": plan.repository, "topic": "fixture", "author": "codex", "reviewer": "claude-code",
                  "state": "approved", "base_commit": plan.base_commit, "head_commit": plan.commit_sha,
                  "max_fix_cycles": 3, "verdict": "approved", "verdict_by": "claude-code",
                  "verdict_commit": plan.commit_sha, "created_at": 1, "updated_at": 1,
                  "security": "pass", "security_by": "claude-code", "scan_commit": plan.commit_sha,
                  "scan_findings": "[]"}
    fields.update(overrides)
    with store._connect() as con:
        names = ",".join(fields)
        return con.execute(f"INSERT INTO reviews({names}) VALUES({','.join('?' for _ in fields)})",
                           tuple(fields.values())).lastrowid


def test_inspection_is_not_a_run_and_no_public_result_import(store, plan):
    assert store.quality_runs() == []
    assert not {"quality_record_run", "_quality_record_run", "quality_submit"} & METHODS
    with pytest.raises(TypeError):
        store._quality_record_run("codex", plan.public(), (observation(),), execution_id=uuid.uuid4().hex)
    with pytest.raises(ValueError):
        store._quality_record_run("codex", plan, (), execution_id=uuid.uuid4().hex)
    assert store.quality_runs() == []


def test_evidence_survives_restart_and_never_contains_arguments_or_output(store, plan):
    result = record(store, plan)
    assert result["kind"] == "quality_run" and result["executed"] is True
    assert result["overall_status"] == "REVIEW" and result["review_status"] == "pending"
    assert result["checks"]["tests"]["status"] == "PASS"
    assert result["checks"]["security"]["status"] == "REVIEW"
    assert result["checks"]["coverage"]["status"] == "NA"
    assert len(result["logs"]) == 3 and result["branch"] is None
    with store._connect() as con:
        raw = con.execute("SELECT evidence_json FROM quality_runs").fetchone()[0]
    assert '"command":' not in raw and '"-m"' not in raw
    assert "stdout" not in raw and "stderr" not in raw
    restarted = Store(settings=store.settings)
    assert restarted.quality_get(result["run_id"]) == result
    assert restarted.quality_get(9999) is None


def test_idempotent_retry_and_conflicting_evidence(store, plan):
    key = uuid.uuid4().hex
    first = record(store, plan, execution_id=key)
    assert record(store, plan, execution_id=key) == first
    with pytest.raises(ValueError, match="different evidence"):
        record(store, plan, execution_id=key, outcomes=(observation(exit_code=1),))
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM quality_runs").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM audit WHERE action='quality-run'").fetchone()[0] == 1


@pytest.mark.parametrize("outcomes", [(observation(exit_code=1),),
                                       (observation(exit_code=None, timed_out=True),),
                                       (observation(exit_code=-9),)])
def test_real_required_failure_overrides_approval(store, plan, outcomes):
    review(store, plan)
    result = record(store, plan, outcomes=outcomes)
    assert result["overall_status"] == "FAIL"


def test_missing_observations_remain_not_run(store, plan):
    review(store, plan)
    result = record(store, plan, outcomes=(observation(),))
    assert result["checks"]["build"]["status"] == "NOT_RUN"
    assert result["overall_status"] == "NOT_RUN"


def test_pass_comes_only_from_matching_b6_authority_and_is_recomputed(store, plan):
    run = record(store, plan)
    assert run["overall_status"] == "REVIEW"
    rid = review(store, plan)
    assert store.quality_get(run["run_id"])["overall_status"] == "PASS"
    with store._connect() as con:
        con.execute("UPDATE reviews SET state='awaiting_review', verdict=NULL, security=NULL WHERE id=?",
                    (rid,))
    assert store.quality_get(run["run_id"])["overall_status"] == "REVIEW"


@pytest.mark.parametrize("overrides", [
    {"repo": "/different"}, {"head_commit": "f" * 40}, {"base_commit": "f" * 40},
    {"verdict_commit": "f" * 40}, {"scan_commit": "f" * 40}, {"scan_findings": None},
    {"scan_findings": '[{"kind":"fixture"}]'}, {"security_by": "codex"},
    {"verdict_by": "codex"}, {"state": "awaiting_review"}, {"verdict": "changes_requested"},
    {"security": None}, {"author": "claude-code"},
])
def test_wrong_stale_incomplete_and_self_approval_cannot_pass(store, plan, overrides):
    review(store, plan, **overrides)
    assert record(store, plan)["overall_status"] == "REVIEW"


def test_closed_valid_approval_and_newer_review_invalidation(store, plan):
    review(store, plan, state="closed")
    run = record(store, plan)
    assert run["overall_status"] == "PASS"
    review(store, plan, state="awaiting_review", verdict=None, security=None, updated_at=2)
    assert store.quality_get(run["run_id"])["overall_status"] == "REVIEW"


def test_confirmed_security_failure_is_fail(store, plan):
    review(store, plan, state="changes_requested", verdict="changes_requested", security="fail")
    assert record(store, plan)["overall_status"] == "FAIL"


def test_policy_change_cannot_be_waived_by_peer_approval(store, repo):
    head = python_project(repo)
    plan = inspect_repository(repo, head)
    review(store, plan)
    assert record(store, plan)["overall_status"] == "REVIEW"
    assert policy_path("src/agent_lease_mcp/_quality_runs.py")


def test_unconfigured_disabled_and_forged_plans_rejected(store, plan, repo):
    for forged in (replace(plan, enabled=False), replace(plan, policy_paths=()),
                   replace(plan, config_digest="f" * 64)):
        if forged == plan:
            continue
        with pytest.raises(ValueError):
            record(store, forged)
    with pytest.raises(ValueError, match="committed command"):
        record(store, plan, outcomes=(observation("typecheck"),))
    with pytest.raises(ValueError, match="Duplicate"):
        record(store, plan, outcomes=(observation(), observation()))
    head = commit(repo, {".agent-lease/quality.toml": "version=1\nenabled=false\nlanguage='python'\n"})
    with pytest.raises(ValueError, match="opted-out"):
        record(store, inspect_repository(repo, head))
    assert store.quality_runs() == []


@pytest.mark.parametrize("change", [
    {"check": "security"}, {"exit_code": True}, {"exit_code": 256},
    {"exit_code": None}, {"timed_out": 1}, {"duration_ms": -1},
    {"output_bytes": 2**54}, {"output_sha256": "private text"},
])
def test_invalid_observation_rejected(change):
    values = {"check": "tests", "exit_code": 0, "timed_out": False, "duration_ms": 1,
                  "output_bytes": 0, "output_sha256": "a" * 64}
    with pytest.raises(ValueError):
        CommandObservation(**(values | change))


def test_latest_per_repository_history_and_read_without_git(store, plan, monkeypatch):
    runs = [record(store, plan) for _ in range(8)]
    def no_inspection(*_args, **_kwargs):
        raise AssertionError("read API must not run Git or project code")
    monkeypatch.setattr("agent_lease_mcp._quality_runs.inspect_repository", no_inspection)
    latest = store.quality_runs(repository=plan.repository, limit=1)
    assert len(latest) == 1 and latest[0]["run_id"] == runs[-1]["run_id"]
    assert len(latest[0]["history"]) == 5
    assert latest[0]["history"][0]["run_id"] == runs[-2]["run_id"]
    assert store.quality_runs(repository="/other") == []


def test_multiple_repos_with_same_basename_do_not_collapse(store, plan, tmp_path):
    first = record(store, plan)
    second_root = tmp_path / "other" / Path(plan.repository).name
    second_root.parent.mkdir()
    subprocess.run(["git", "clone", "-q", plan.repository, str(second_root)], check=True)
    second = record(store, inspect_repository(second_root))
    rows = store.quality_runs()
    assert [r["run_id"] for r in rows] == [second["run_id"], first["run_id"]]


def test_broker_can_read_but_cannot_import_evidence(store, plan, tmp_path):
    run = record(store, plan)
    sock = tmp_path / "quality.sock"
    server = serve(store.settings, sock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        remote = RemoteStore(store.settings, sock)
        assert remote.quality_get(run["run_id"]) == run
        assert remote.quality_runs()[0]["run_id"] == run["run_id"]
        with pytest.raises(AttributeError):
            remote.__getattr__("_quality_record_run")
        # Exercise the wire whitelist too, not only RemoteStore.__getattr__.
        with pytest.raises(PermissionError):
            remote._call("_quality_record_run", [], {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_snapshot_requires_session_and_returns_server_run(store, plan):
    run = record(store, plan)
    handler = authenticated_handler(store, store.settings, "fixture-not-a-real-token")
    status, _ = call_handler(store, store.settings, "fixture-not-a-real-token", "GET",
                             "/api/snapshot", handler_type=handler)
    assert status == 403
    status, body = call_handler(store, store.settings, "fixture-not-a-real-token", "GET",
                               "/api/snapshot", headers={"Cookie": "agent_lease_session=session"},
                               handler_type=handler)
    assert status == 200
    assert json.loads(body)["quality_runs"][0]["run_id"] == run["run_id"]


def test_sse_room_uses_the_same_quality_projection(store, plan, monkeypatch):
    run = record(store, plan)
    handler = authenticated_handler(store, store.settings, "fixture-only")

    def disconnect(_seconds):
        raise BrokenPipeError

    monkeypatch.setattr("agent_lease_mcp.webui.time.sleep", disconnect)
    status, body = call_handler(store, store.settings, "fixture-only", "GET", "/api/events",
                               headers={"Cookie": "agent_lease_session=session"},
                               handler_type=handler)
    assert status == 200
    room = next(chunk for chunk in body.decode().split("\n\n") if "event: room\n" in chunk)
    payload = json.loads(next(line[6:] for line in room.splitlines() if line.startswith("data: ")))
    assert payload["quality_runs"][0]["run_id"] == run["run_id"]


def test_coverage_command_success_is_not_a_numeric_metric(store, repo):
    from test_quality import CONFIG
    config = CONFIG + '\n[checks.coverage]\ncommand=["python", "-m", "coverage", "run"]\n'
    python_project(repo, config)
    head = commit(repo, {"src/app.py": "answer = 42\n"})
    plan = inspect_repository(repo, head)
    review(store, plan)
    run = record(store, plan)
    assert run["checks"]["coverage"]["status"] == "REVIEW"
    assert run["checks"]["coverage"]["value"] is None
    assert run["overall_status"] == "REVIEW"


@pytest.mark.parametrize("known,unknown,high,score,expected", [
    (1,0,0,1000,"PASS"), (1,0,1,72000,"REVIEW"), (1,1,0,1000,"NA"),
    (0,1,0,None,"NA")])
def test_numeric_crap_is_report_only(store, repo, known, unknown, high, score, expected):
    from test_quality import CONFIG

    from agent_lease_mcp._quality_runs import MetricObservation
    python_project(repo, CONFIG + '\n[checks.coverage]\ncommand=["python","coverage.py"]\n')
    plan = inspect_repository(repo, commit(repo, {"src/app.py":"answer = 42\n"}))
    review(store, plan)
    metric = MetricObservation("coverage.py", 90, 100, known, unknown, high, score)
    outcomes = tuple(replace(observation(check.name), metrics=metric if check.name=="coverage" else None)
                     for check in plan.checks if check.command)
    run = record(store, plan, outcomes=outcomes)
    assert run["checks"]["coverage"]["status"] == "PASS"
    assert run["checks"]["crap"]["status"] == expected
    assert run["overall_status"] == ("REVIEW" if high else "PASS")


def test_typescript_typecheck_failure_is_required(store, repo):
    commit(repo, {"package.json": json.dumps({"packageManager": "npm@11",
                 "devDependencies": {"typescript": "5"},
                 "scripts": {"test": "node test.js", "lint": "eslint .", "build": "tsc",
                             "typecheck": "tsc --noEmit"}})})
    head = commit(repo, {"src/app.ts": "const answer = 42;\n"})
    plan = inspect_repository(repo, head)
    review(store, plan)
    outcomes = tuple(observation(check.name, exit_code=1 if check.name == "typecheck" else 0)
                     for check in plan.checks if check.command)
    run = record(store, plan, outcomes=outcomes)
    assert run["checks"]["typecheck"]["required"] is True
    assert run["overall_status"] == "FAIL"


@pytest.mark.parametrize("bad", [True, 0, -1, "1", 101])
def test_read_limits_reject_invalid_values(store, bad):
    with pytest.raises(ValueError):
        store.quality_runs(limit=bad)


def test_critical_policy_approval_requires_human_and_exact_run(store, repo):
    plan = inspect_repository(repo, python_project(repo))
    run = record(store, plan)
    with pytest.raises(PermissionError):
        store.quality_human_approve_policy("codex", run["run_id"], plan.commit_sha, "fixture approval reason")
    with pytest.raises(PermissionError):
        store.quality_human_approve_policy("michal", run["run_id"], plan.commit_sha, "fixture approval reason")
    review(store, plan)
    with pytest.raises(ValueError):
        store.quality_human_approve_policy("michal", run["run_id"], "f" * 40, "fixture approval reason")
    approved = store.quality_human_approve_policy("michal", run["run_id"], plan.commit_sha, "fixture approval reason")
    assert approved["overall_status"] == "PASS"
    assert approved["policy_review_status"] == "approved"
    assert record(store, plan)["overall_status"] == "REVIEW"  # new execution is not approved
    assert "quality_human_approve_policy" not in METHODS


def test_human_policy_approval_never_waives_failure(store, repo):
    plan = inspect_repository(repo, python_project(repo))
    review(store, plan)
    run = record(store, plan, outcomes=(observation(exit_code=1),))
    with pytest.raises(PermissionError):
        store.quality_human_approve_policy("michal", run["run_id"], plan.commit_sha, "fixture approval reason")
    assert store.quality_get(run["run_id"])["overall_status"] == "FAIL"


@pytest.mark.parametrize("bad_id", [True, 1.5, "1"])
def test_human_http_approval_never_coerces_an_identity(store, repo, bad_id):
    plan = inspect_repository(repo, python_project(repo))
    review(store, plan)
    run = record(store, plan)
    handler = authenticated_handler(store, store.settings, "fixture-only")
    status, _ = call_handler(store, store.settings, "fixture-only", "POST", "/api/quality-policy-approve",
        headers={"Cookie": "agent_lease_session=session"},
        body={"run_id":bad_id,"commit_sha":plan.commit_sha,"reason":"fixture approval reason"},
        handler_type=handler)
    assert status == 400
    assert store.quality_get(run["run_id"])["policy_review_status"] == "pending"
