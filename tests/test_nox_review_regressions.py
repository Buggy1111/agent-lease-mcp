"""Regressions for Nox review #4/#5. Expected RED on candidate 7aeb90f.

Only synthetic markers and isolated repositories/processes; never production state.
Keep this separate from test_progress.py: its fast_sampling fixture masks the B5 bug.
"""
from __future__ import annotations

import importlib
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_lease_mcp import bridge, secret_scan, workspace
from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store

FAKE_MARKER = "sk-or-v1-" + "a1b2c3d4" * 6


def git_repo(path: Path):
    path.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=path,
            env=workspace._hardened_env(), capture_output=True, text=True, check=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Regression test")
    git("config", "user.email", "test@example.invalid")
    return git


@pytest.fixture
def isolated_server(tmp_path, monkeypatch):
    db = tmp_path / "isolated.db"
    monkeypatch.setenv("AGENT_LEASE_DB", str(db))
    monkeypatch.setenv("AGENT_NAME", "claude-code")
    monkeypatch.delenv("AGENT_LEASE_SOCKET", raising=False)
    monkeypatch.delenv("AGENT_LEASE_REQUIRE_BROKER", raising=False)
    server = importlib.import_module("agent_lease_mcp.server")
    settings = Settings(db, "claude-code", 1800)
    monkeypatch.setattr(server, "_settings", settings)
    monkeypatch.setattr(server, "_store", Store(settings=settings))
    return server


def test_root_review_cannot_approve_a_marker_retained_in_a_benign_child(
    tmp_path, isolated_server, monkeypatch,
):
    repo = tmp_path / "root-review"
    git = git_repo(repo)
    (repo / "config.txt").write_text("value = " + FAKE_MARKER + "\n")
    git("add", ".")
    git("commit", "-qm", "root with synthetic marker")
    root_commit = git("rev-parse", "HEAD")
    server = isolated_server
    opened = server.review("request", topic="root scope regression", reviewer="codex", repo=str(repo))
    assert opened["secret_findings"]  # positive control: initial root is scanned
    server._store.review_verdict(
        "codex", opened["id"], "changes_requested", root_commit,
        "remove the synthetic marker", security="fail",
    )
    (repo / "notes.txt").write_text("A benign addition does not remove config.txt.\n")
    git("add", "notes.txt")
    git("commit", "-qm", "benign child")
    child = git("rev-parse", "HEAD")
    server.review("submit", review_id=opened["id"], repo=str(repo))
    monkeypatch.setattr(server, "_settings", replace(server._settings, agent="codex"))
    with pytest.raises(PermissionError, match="secret"):
        server.verdict(opened["id"], "approved", child, security="pass", repo=str(repo))


def test_text_marker_cannot_be_hidden_by_a_binary_diff_attribute(tmp_path):
    repo = tmp_path / "binary-attribute"
    git = git_repo(repo)
    (repo / "base.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "plain-control.txt").write_text(FAKE_MARKER + "\n")
    (repo / "config.txt").write_text(FAKE_MARKER + "\n")
    (repo / ".gitattributes").write_text("config.txt -diff\n")
    git("add", ".")
    git("commit", "-qm", "synthetic markers, one hidden by attributes")
    # Accept either scanning this text or explicitly refusing unsupported binary content.
    try:
        diff = workspace.commit_diff(repo, git("rev-parse", "HEAD"), base)
        found = secret_scan.scan_diff(diff)
    except ValueError as exc:
        assert any(word in str(exc).lower() for word in ("binary", "unsupported", "unscannable"))
        return
    assert any(f["file"] == "plain-control.txt" for f in found)
    assert any(f["file"] == "config.txt" for f in found)


@pytest.mark.parametrize("separator", ["\v", "\f", "\r", "\x85", "\u2028", "\u2029"],
                         ids=["vertical-tab", "form-feed", "carriage-return", "next-line",
                              "line-separator", "paragraph-separator"])
def test_git_line_with_embedded_separator_does_not_hide_marker(tmp_path, separator):
    repo = tmp_path / "git-line"
    git = git_repo(repo)
    (repo / "base.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "plain-control.txt").write_text(FAKE_MARKER + "\n")
    (repo / "case.txt").write_text("prefix" + separator + FAKE_MARKER + "\n")
    git("add", ".")
    git("commit", "-qm", "single Git LF line containing an embedded separator")
    found = secret_scan.scan_diff(workspace.commit_diff(repo, git("rev-parse", "HEAD"), base))
    assert any(f["file"] == "plain-control.txt" for f in found)  # positive control
    assert {"file": "case.txt", "line": 1, "kind": "api-key-sk"} in found


def test_short_idle_budget_samples_external_progress_with_production_defaults(tmp_path):
    script = tmp_path / "silent_work.py"
    script.write_text("import time\ntime.sleep(1.8)\nprint('ok')\n")
    calls = []

    def progress():
        calls.append(True)
        return True

    # No fast_sampling monkeypatch. The production sampling interval is part of this scenario.
    try:
        result = bridge.run_command(
            shlex.join([sys.executable, str(script), "{prompt}"]), "x",
            timeout=6, idle_timeout=0.8, progress=progress,
        )
    except ValueError as exc:
        # Explicit validation of too-short idle budgets is also a valid fail-closed policy.
        assert "idle" in str(exc).lower() or "timeout" in str(exc).lower()
        return
    assert result.ok, result.output
    assert result.output == "ok"
    assert calls
