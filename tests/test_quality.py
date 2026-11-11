"""B7.1: immutable inputs, no execution/room writes, conservative result policy."""
from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_lease_mcp.quality import (
    CHECK_NAMES,
    REQUIRED_CHECKS,
    Check,
    Inspection,
    Status,
    aggregate,
    inspect_repository,
    policy_path,
)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "add", "--all")
    git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project with spaces"
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(root)], check=True)
    git(root, "config", "user.name", "Quality test")
    git(root, "config", "user.email", "quality@example.invalid")
    commit(root, {"README.md": "synthetic fixture\n"})
    return root


CONFIG = '''version = 1
language = "python"
[checks.tests]
command = ["python", "-m", "pytest"]
[checks.lint]
command = ["python", "-m", "ruff", "check", "src"]
[checks.build]
command = ["python", "-m", "build"]
'''


def python_project(repo: Path, config: str = CONFIG) -> str:
    return commit(repo, {"pyproject.toml": '[project]\nname="fixture"\n',
                         ".agent-lease/quality.toml": config})


def passing_checks() -> tuple[Check, ...]:
    return tuple(Check(name, Status.PASS if name in REQUIRED_CHECKS else Status.NA,
                       name in REQUIRED_CHECKS, "fixture") for name in CHECK_NAMES)


def test_pass_requires_complete_checks_and_independent_review():
    checks = passing_checks()
    assert aggregate(checks) is Status.REVIEW
    assert aggregate(checks, peer_review="approved") is Status.PASS
    assert aggregate(checks[:-1], peer_review="approved") is Status.SETUP_REQUIRED
    assert aggregate((), peer_review="approved") is Status.SETUP_REQUIRED
    optional = tuple(replace(check, required=False) for check in checks)
    assert aggregate(optional, peer_review="approved") is Status.SETUP_REQUIRED
    weakened = tuple(replace(check, required=False, status=Status.NA) if check.name == "tests"
                     else check for check in checks)
    assert aggregate(weakened, peer_review="approved") is Status.SETUP_REQUIRED


@pytest.mark.parametrize(("status", "expected"), [
    (Status.FAIL, Status.FAIL), (Status.NA, Status.SETUP_REQUIRED),
    (Status.SETUP_REQUIRED, Status.SETUP_REQUIRED), (Status.NOT_RUN, Status.NOT_RUN),
    (Status.REVIEW, Status.REVIEW),
])
def test_required_status_cannot_become_pass(status, expected):
    checks = tuple(replace(check, status=status) if check.name == "tests" else check
                   for check in passing_checks())
    assert aggregate(checks, peer_review="approved") is expected


def test_policy_changes_and_optional_failure_require_review():
    assert policy_path("src/agent_lease_mcp/quality.py")  # The gate's own policy is protected.
    assert aggregate(passing_checks(), peer_review="approved", policy_review_required=True) is Status.REVIEW
    checks = tuple(replace(check, status=Status.FAIL) if check.name == "coverage" else check
                   for check in passing_checks())
    assert aggregate(checks, peer_review="approved") is Status.REVIEW
    with pytest.raises(ValueError, match="Duplicate"):
        aggregate((*passing_checks(), passing_checks()[0]))


def test_check_and_commit_identity_are_validated():
    with pytest.raises(ValueError):
        Check("tests", "PASS", True, "fixture")
    with pytest.raises(ValueError):
        Check("invented", Status.PASS, True, "fixture")
    with pytest.raises(ValueError):
        Inspection("/repo", "abcd1234", "", "python", None, False, True,
                   passing_checks(), (), (), (), None)


def test_python_plan_uses_selected_commit_and_never_runs_commands(repo: Path, tmp_path: Path):
    marker = tmp_path / "MUST_NOT_EXIST"
    config = CONFIG.replace('["python", "-m", "pytest"]',
                            json.dumps(["touch", str(marker)]))
    head = python_project(repo, config)
    first = inspect_repository(repo, head)
    (repo / ".agent-lease/quality.toml").write_text("this dirty config is invalid")
    (repo / "package.json").write_text('{"scripts":{"test":"touch should-not-run"}}')
    second = inspect_repository(repo, head)
    assert second.language == "python"
    assert second.commit_sha == head
    assert second.dirty is True
    assert first.config_digest == second.config_digest
    assert first.public()["inspection_id"] == second.public()["inspection_id"]
    assert not marker.exists()
    report = second.public()
    assert report["executed"] is False and report["execution_allowed"] is False
    assert report["review_status"] == "pending" and report["reviewer"] is None
    assert report["logs"] == []
    assert all(check["status"] in {"NOT_RUN", "NA"} for check in report["checks"].values())
    assert report["overall_status"] == "REVIEW"  # Newly committed policy needs review.


def test_project_tool_configuration_is_not_an_invented_command(repo: Path):
    head = commit(repo, {"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths=["tests"]\n[tool.ruff]\nline-length=100\n'})
    plan = inspect_repository(repo, head)
    assert plan.declared_tools == ("pytest", "ruff")
    assert plan.public()["overall_status"] == "SETUP_REQUIRED"
    assert all(not check.command for check in plan.checks)


@pytest.mark.parametrize(("lock", "manager"), [("package-lock.json", "npm"),
                                                ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn")])
def test_typescript_uses_declared_scripts_and_manager(repo: Path, lock, manager):
    head = commit(repo, {"package.json": json.dumps({"devDependencies": {"typescript": "5"},
                         "scripts": {"test": "node tests.js", "lint": "eslint .", "build": "tsc",
                                     "typecheck": "tsc --noEmit", "coverage": "vitest --coverage"}}), lock: "fixture"})
    plan = inspect_repository(repo, head)
    assert plan.language == "typescript" and plan.package_manager == manager
    checks = {check.name: check for check in plan.checks}
    assert checks["typecheck"].required is True
    assert checks["tests"].command == (manager, "run", "test")
    assert checks["tests"].source == "package.json:scripts.test"
    assert checks["coverage"].status is Status.NOT_RUN
    assert checks["crap"].status is Status.NA


def test_node_scripts_are_not_executed_and_script_bodies_are_not_exposed(repo: Path, tmp_path: Path):
    marker = tmp_path / "NO_EXECUTION"
    private = "this embedded command body must stay private"
    package = {"scripts": {"test": f"touch '{marker}'; echo '{private}'",
                            "lint": "lint", "build": "build"}}
    head = commit(repo, {"package.json": json.dumps(package), "package-lock.json": "fixture"})
    plan = inspect_repository(repo, head)
    assert plan.language == "javascript"
    assert private not in json.dumps(plan.public())
    assert not marker.exists()


def test_explicit_arguments_are_not_leaked_in_report(repo: Path):
    private = "do not print this argument"
    config = CONFIG.replace('["python", "-m", "pytest"]', json.dumps(["echo", private]))
    plan = inspect_repository(repo, python_project(repo, config))
    assert any(private in check.command for check in plan.checks)
    assert private not in json.dumps(plan.public())


@pytest.mark.parametrize("files", [
    {"package.json": "{}"},
    {"package.json": '{"scripts":{"test":"node test.js"}}', "package-lock.json": "x", "yarn.lock": "x"},
    {"package.json": '{"packageManager":"pnpm@9","scripts":{"test":"test"}}', "package-lock.json": "x"},
    {"package.json": '{"packageManager":"bun@1"}'},
    {"package.json": '{"scripts":[]}'},
    {"package.json": '{"scripts":{"test":17}}'},
    {"package.json": '{"scripts":{},"scripts":{"test":"test"}}'},
    {"package.json": '[]'},
    {"package.json": "invalid json"},
    {"package.json": '{"scripts":' + "[" * 1200 + "0" + "]" * 1200 + "}"},
    {"pyproject.toml": "[unclosed"},
    {"project.godot": "unsupported engine"},
])
def test_unknown_ambiguous_and_invalid_projects_require_setup(repo: Path, files):
    plan = inspect_repository(repo, commit(repo, files))
    assert plan.public()["overall_status"] == "SETUP_REQUIRED"
    assert not plan.public()["executed"]


@pytest.mark.parametrize("config", [
    "version = 2", "version = true", "enabled = true", 'version=1\nenabled="false"',
    'version=1\nlanguage=["python"]', 'version=1\nlanguage="godot"',
    'version=1\nthreshold=0', 'version=1\n[checks.security]\ncommand=["echo","PASS"]',
    'version=1\n[checks.tests]\nrequired=false\ncommand=["true"]',
    'version=1\n[checks.tests]\ncommand="pytest"', 'version=1\n[checks.tests]\ncommand=[]',
    'version=1\n[checks.tests]\ncommand=["npx","vitest"]',
    'version=1\n[checks.tests]\ncommand=["npm","exec","vitest"]',
    'version=1\n[checks.tests]\ncommand=["pnpm","dlx","vitest"]',
])
def test_config_cannot_override_policy_or_download_packages(repo: Path, config):
    plan = inspect_repository(repo, python_project(repo, config))
    assert plan.public()["overall_status"] == "SETUP_REQUIRED"
    assert plan.problems


def test_no_manifests_is_setup_and_explicit_opt_out_is_never_pass(repo: Path):
    assert inspect_repository(repo).public()["overall_status"] == "SETUP_REQUIRED"
    head = commit(repo, {".agent-lease/quality.toml": "version=1\nenabled=false\n"})
    report = inspect_repository(repo, head).public()
    assert report["enabled"] is False and report["overall_status"] == "NOT_RUN"
    assert report["policy_review_required"] is True


def test_mixed_project_requires_explicit_language_selection(repo: Path):
    commit(repo, {"package.json": "{}"})
    head = commit(repo, {"pyproject.toml": "[tool.pytest]\n"})
    assert inspect_repository(repo, head).public()["overall_status"] == "SETUP_REQUIRED"
    selected = commit(repo, {".agent-lease/quality.toml": CONFIG})
    assert inspect_repository(repo, selected).language == "python"


def test_typescript_runtime_dependency_also_requires_typecheck(repo: Path):
    head = commit(repo, {"package.json": '{"dependencies":{"typescript":"5"}}'})
    plan = inspect_repository(repo, head)
    assert plan.language == "typescript"
    assert next(check for check in plan.checks if check.name == "typecheck").required


@pytest.mark.parametrize("target", ["pyproject.toml", ".agent-lease/quality.toml", ".agent-lease", "package-lock.json"])
def test_committed_manifest_symlinks_are_rejected_without_reading_target(repo: Path, tmp_path: Path, target):
    secret = tmp_path / "outside"
    secret.write_text("outside data must not enter the report")
    commit(repo, {"package.json": '{"scripts":{"test":"test","lint":"lint","build":"build"}}'})
    link = repo / target
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)
    head = commit(repo, {})
    report = inspect_repository(repo, head).public()
    assert report["overall_status"] == "SETUP_REQUIRED"
    assert "outside data" not in json.dumps(report)


def test_oversized_manifest_is_not_read(repo: Path, monkeypatch):
    from agent_lease_mcp import quality
    head = commit(repo, {"package.json": json.dumps({"description": "x" * 1000})})
    monkeypatch.setattr(quality, "MAX_MANIFEST_BYTES", 128)
    assert inspect_repository(repo, head).public()["overall_status"] == "SETUP_REQUIRED"


@pytest.mark.parametrize("path", ["tests/new_test.py", "src/test_guard.py", "src/widget.spec.ts",
                                  ".github/workflows/ci.yml", "pyproject.toml", "package.json",
                                  "src/secret_scan.py", ".agent-lease/quality.toml", ".coveragerc",
                                  "src/widget.test.tsx", "jest.config.cjs", "vitest.config.ts"])
def test_changed_test_ci_and_security_names_are_guarded(path):
    assert policy_path(path)


@pytest.mark.parametrize("path", [".pytest.ini", "pytest.toml", ".pytest.toml", ".mypy.ini",
                                  "node_modules/.bin/test", "nested/node_modules/pkg/index.js"])
def test_tool_configuration_is_policy(path):
    assert policy_path(path)


@pytest.mark.parametrize("path", ["pytest.py", "pytest/__main__.py", "ruff.py", "build/__init__.py"])
def test_command_module_shadow_is_policy(repo, path):
    python_project(repo)
    head = commit(repo, {path: "raise SystemExit(0)\n"})
    assert path in inspect_repository(repo, head).policy_paths


def test_base_must_be_real_ancestor_and_deleted_tests_remain_guarded(repo: Path):
    base = python_project(repo)
    with pytest.raises(ValueError):
        inspect_repository(repo, base, base)
    with pytest.raises(ValueError):
        inspect_repository(repo, base, "--help")
    first = commit(repo, {"tests/test_example.py": "def test_example(): assert True"})
    (repo / "tests/test_example.py").unlink()
    head = commit(repo, {"README.md": "updated"})
    plan = inspect_repository(repo, head, first)
    assert plan.base_commit == first and "tests/test_example.py" in plan.policy_paths
    # A file introduced and removed within the range is not a net policy change.
    assert inspect_repository(repo, head, base).policy_paths == ()
    assert first != head
    doc_only = commit(repo, {"README.md": "only docs again"})
    assert inspect_repository(repo, doc_only).policy_paths == ()


def test_git_configuration_cannot_execute_filters_or_hooks(repo: Path, tmp_path: Path):
    marker = tmp_path / "FILTER_MUST_NOT_RUN"
    head = python_project(repo)
    commit(repo, {".gitattributes": "*.toml filter=trip diff=trip\n"})
    git(repo, "config", "filter.trip.clean", f"touch '{marker}'; cat")
    git(repo, "config", "diff.trip.textconv", f"touch '{marker}'")
    git(repo, "config", "core.fsmonitor", f"touch '{marker}'")
    (repo / "pyproject.toml").write_text("modified worktree")
    plan = inspect_repository(repo, head)
    assert plan.dirty is True and plan.commit_sha == head
    assert not marker.exists()


def test_cli_inspection_does_not_open_room_or_require_identity(repo: Path, tmp_path: Path, monkeypatch, capsys):
    from agent_lease_mcp import cli
    python_project(repo)
    def forbid(*args, **kwargs):
        pytest.fail("Read-only quality inspection must not open the room")
    monkeypatch.setattr(cli, "open_store", forbid)
    monkeypatch.setattr(cli.Settings, "from_env", forbid)
    monkeypatch.delenv("AGENT_NAME", raising=False)
    database = tmp_path / "MUST_NOT_CREATE.db"
    monkeypatch.setenv("AGENT_LEASE_DB", str(database))
    result = cli.main(["quality", "inspect", "--repo", str(repo)])
    report = json.loads(capsys.readouterr().out)
    assert result == 0 and report["executed"] is False
    assert report["overall_status"] == "REVIEW"
    assert not database.exists()


def test_invalid_cli_target_reports_setup_without_dumping_exception(tmp_path: Path, capsys):
    from agent_lease_mcp import cli
    assert cli.main(["quality", "inspect", "--repo", str(tmp_path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["overall_status"] == "SETUP_REQUIRED" and report["executed"] is False
