"""
Smoke testy MCP plochy. Nekontrolují logiku (od toho jsou testy policy a store),
ale to, že nástroje existují, jmenují se jak mají a vrací tvar, na který se agent
spolehne. Přejmenovaný nebo rozbitý nástroj je tichá porucha — server naběhne,
jen ho agent nenajde.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

EXPECTED_TOOLS = {
    "report_limit",
    "accept",
    "claim", "release", "owner", "say", "send", "jobs", "next_task", "ack", "retry",
    "cancel",
    "inbox", "room", "history", "wait", "checkpoint", "emergency_stop",
    "review", "verdict",
}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.setenv("AGENT_NAME", "test-agent")
    # Import až po nastavení prostředí — server si settings čte při načtení modulu.
    import importlib

    from agent_lease_mcp import server

    importlib.reload(server)
    return server


@pytest.fixture
def client(server):
    return Client(server.mcp)


@pytest.mark.asyncio
async def test_tool_surface_is_stable(client):
    async with client as c:
        assert {t.name for t in await c.list_tools()} == EXPECTED_TOOLS


def test_claim_then_owner_roundtrip(server, tmp_path):
    target = str(tmp_path / "a.txt")
    assert server.claim([target])["ok"] is True
    assert server.owner(target)["held_by"] == "test-agent"


def test_release_frees_the_path(server, tmp_path):
    target = str(tmp_path / "a.txt")
    server.claim([target])
    server.release()
    assert server.owner(target)["held_by"] is None


def test_room_reports_me_claims_and_messages(server, tmp_path):
    server.claim([str(tmp_path / "a.txt")], purpose="oprava")
    server.say("beru si a.txt")

    state = server.room(status="pracuju")

    assert state["me"] == "test-agent"
    assert state["claims"][0]["purpose"] == "oprava"
    assert state["recent_messages"][0]["text"] == "beru si a.txt"


def test_history_answers_who_touched_the_path(server, tmp_path):
    target = str(tmp_path / "a.txt")
    server.claim([target], purpose="balení")

    events = server.history(path=target)["events"]

    assert events[0]["action"] == "claim"
    assert events[0]["agent"] == "test-agent"


def test_addressed_task_roundtrip(server):
    sent = server.send("test-agent", "zkontroluj", kind="task")
    task = server.next_task()["task"]

    assert task["id"] == sent["id"]
    assert task["text"] == "zkontroluj"
    acked = server.ack(task["id"], "succeeded", task["lease_token"])
    assert acked["ok"] is True


def test_next_task_holds_a_checkpointed_task_when_the_workspace_changed(server, monkeypatch):
    """A6 přísně i v interaktivní cestě: nevydat úkol, předat člověku."""
    store = server._store
    mid = store.send("michal", "test-agent", "úkol", kind="task")
    d = store.lease_next("test-agent", start=True)
    assert store.checkpoint(mid, "test-agent", d.lease_token, "krok 1", "krok 2", "h", "f")
    assert store.complete(mid, "test-agent", d.lease_token, "needs_review", "přerušeno")
    assert store.retry(mid, "test-agent")
    monkeypatch.setattr(server, "_workspace_state", lambda: ("h", "jiný"))
    result = server.next_task()
    assert result["task"] is None    # úložiště samo úkol zadrželo, nic nevydalo (A6, #566/P1)
    job = next(j for j in store.jobs("test-agent") if j["id"] == mid)
    assert job["state"] == "needs_review" and job["last_error"].startswith("checkpoint:")
    assert store.approve_checkpoint_resume(mid, "test-agent", "michal", "ověřeno")
    result = server.next_task()
    assert result["task"]["id"] == mid and result["checkpoint"]["workspace_matches"] is False


def test_next_task_resumes_a_checkpoint_on_a_matching_workspace(server, monkeypatch):
    store = server._store
    mid = store.send("michal", "test-agent", "úkol", kind="task")
    d = store.lease_next("test-agent", start=True)
    store.checkpoint(mid, "test-agent", d.lease_token, "krok 1", "krok 2", "h", "f")
    store.complete(mid, "test-agent", d.lease_token, "needs_review", "přerušeno")
    store.retry(mid, "test-agent")
    monkeypatch.setattr(server, "_workspace_state", lambda: ("h", "f"))
    result = server.next_task()
    assert result["task"]["id"] == mid and result["checkpoint"]["workspace_matches"] is True


def _git_repo(path):
    import subprocess

    def git(*argv):
        return subprocess.run(["git", *argv], cwd=path, check=True, capture_output=True,
                              text=True).stdout.strip()

    path.mkdir()
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (path / "f.txt").write_text("1")
    git("add", "f.txt")
    git("commit", "-qm", "one")
    return git


def test_review_tools_bind_to_commits(server, tmp_path, monkeypatch):
    git = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(tmp_path / "repo")
    first = git("rev-parse", "HEAD")
    opened = server.review("request", topic="B3 smoke", reviewer="codex")
    assert opened["head_commit"] == first and opened["state"] == "awaiting_review"
    assert "warning" not in opened
    with pytest.raises(PermissionError):
        server.verdict(opened["id"], "approved", first, security="pass")  # autor sám ne
    server._store.review_verdict("codex", opened["id"], "changes_requested", first, "doplň test")

    (tmp_path / "repo" / "f.txt").write_text("2")
    git("commit", "-qam", "two")
    (tmp_path / "repo" / "dirty.txt").write_text("x")
    monkeypatch.chdir(tmp_path)  # mimo repo review: git se v cestě z DB nespouští
    with pytest.raises(ValueError):
        server.review("submit", review_id=opened["id"])
    monkeypatch.chdir(tmp_path / "repo")
    submitted = server.review("submit", review_id=opened["id"])
    second = git("rev-parse", "HEAD")
    assert submitted["head_commit"] == second and submitted["fix_cycles"] == 1
    assert "uncommitted" in submitted["warning"]

    server._store.review_verdict("codex", opened["id"], "approved", second, security="pass", reviewer_findings=[])
    assert server.review("check")["approved"] is True
    assert server.review("check", commit=first)["approved"] is False
    assert [e["event"] for e in server.review("show", review_id=opened["id"])["events"]] == [
        "open", "changes_requested", "fix", "approved"]
    assert server.review("close", review_id=opened["id"], note="merged")["state"] == "closed"
    with pytest.raises(ValueError):
        server.review("bogus")


def test_review_submit_checks_author_before_running_git(server, tmp_path, monkeypatch):
    import subprocess

    from agent_lease_mcp import workspace
    other = server._store.review_open("codex", str(tmp_path), "cizí", "", "", "a" * 40)
    calls = []
    monkeypatch.setattr(workspace, "_git", lambda *a: calls.append(a) or subprocess.CalledProcessError)
    with pytest.raises(PermissionError):
        server.review("submit", review_id=other["id"])
    assert calls == []


def test_review_takes_repo_path_instead_of_session_cwd(server, tmp_path, monkeypatch):
    git = _git_repo(tmp_path / "work")
    monkeypatch.chdir(tmp_path)  # relace začala mimo repo (jako /home/…/dev)
    with pytest.raises(ValueError, match="not inside a git repository"):
        server.review("request", topic="t", reviewer="codex")
    opened = server.review("request", topic="t", reviewer="codex", repo=str(tmp_path / "work"))
    assert opened["head_commit"] == git("rev-parse", "HEAD")
    assert server.review("check", repo="work")["approved"] is False  # relativně k cwd
    server._store.review_verdict("codex", opened["id"], "changes_requested", opened["head_commit"], "x")
    (tmp_path / "work" / "f.txt").write_text("3")
    git("commit", "-qam", "three")
    with pytest.raises(ValueError, match="pass repo="):
        server.review("submit", review_id=opened["id"], repo=str(_git_repo(tmp_path / "other") and tmp_path / "other"))
    assert server.review("submit", review_id=opened["id"], repo=str(tmp_path / "work"))["fix_cycles"] == 1


def test_resolve_commit_never_runs_repo_filters(tmp_path):
    """Kontrola rozpracovaných změn nesmí spustit clean filtr z konfigurace repa."""
    import subprocess
    import time

    from agent_lease_mcp import workspace

    def scenario(name):
        repo = tmp_path / name
        git = _git_repo(repo)
        (repo / ".gitattributes").write_text("*.txt filter=x\n")
        git("add", ".gitattributes")
        git("commit", "-qm", "attrs")
        marker = tmp_path / f"{name}-pwned"
        git("config", "filter.x.clean", f"sh -c 'touch {marker}; cat'")
        time.sleep(1.1)
        (repo / "f.txt").write_text("2")  # stejná velikost → git musí obsah prohnat filtrem
        return repo, git, marker

    control, _, control_marker = scenario("control")
    subprocess.run(["git", "status", "--porcelain"], cwd=control, capture_output=True, check=True)
    assert control_marker.exists()  # obyčejný status filtr opravdu spustí

    repo, git, marker = scenario("evil")
    _top, sha, dirty = workspace.resolve_commit(repo)
    assert sha == git("rev-parse", "HEAD") and dirty is True
    assert not marker.exists()

    # Filtr schovaný ve vloženém konfiguračním souboru (include.path) taky ne.
    hidden, git2, hidden_marker = scenario("hidden")
    git2("config", "--unset", "filter.x.clean")
    included = tmp_path / "included.cfg"
    # V souboru config je `;` komentář, proto `&&`.
    included.write_text(f"[filter \"x\"]\n\tclean = sh -c 'touch {hidden_marker} && cat'\n")
    control2, git3, control2_marker = scenario("control2")
    git3("config", "--unset", "filter.x.clean")
    included2 = tmp_path / "included2.cfg"
    included2.write_text(f"[filter \"x\"]\n\tclean = sh -c 'touch {control2_marker} && cat'\n")
    git3("config", "include.path", str(included2))
    subprocess.run(["git", "status", "--porcelain"], cwd=control2, capture_output=True, check=True)
    assert control2_marker.exists()  # vložený filtr obyčejný status spustí
    git2("config", "include.path", str(included))
    assert workspace.resolve_commit(hidden)[2] is True
    assert not hidden_marker.exists()


def test_resolve_commit_never_runs_submodule_filters(tmp_path):
    import subprocess
    import time

    from agent_lease_mcp import workspace

    for name in ("control", "hardened"):
        parent = tmp_path / name
        git = _git_repo(parent)
        sub = parent / "sub"
        subgit = _git_repo(sub)
        (sub / ".gitattributes").write_text("*.txt filter=x\n")
        subgit("add", ".gitattributes")
        subgit("commit", "-qm", "attrs")
        git("add", "sub")
        git("commit", "-qm", "gitlink")
        marker = tmp_path / f"{name}-sub-filter"
        subgit("config", "filter.x.clean", f"sh -c 'touch {marker}; cat'")
        time.sleep(1.1)
        (sub / "f.txt").write_text("2")
        if name == "control":
            subprocess.run(["git", "status", "--porcelain"], cwd=parent,
                           capture_output=True, check=True)
            assert marker.exists()
        else:
            assert workspace.resolve_commit(parent) == (
                str(parent), git("rev-parse", "HEAD"), True)
            assert not marker.exists()


def test_resolve_commit_never_runs_promisor_helper(tmp_path):
    import subprocess

    from agent_lease_mcp import workspace

    for name in ("control", "hardened"):
        repo = tmp_path / name
        git = _git_repo(repo)
        first = git("rev-parse", "HEAD")
        (repo / "f.txt").write_text("2")
        git("commit", "-qam", "two")
        missing = git("rev-parse", "HEAD")
        git("reset", "--hard", first)
        (repo / ".git" / "objects" / missing[:2] / missing[2:]).unlink()
        marker = tmp_path / f"{name}-promisor"
        helper = tmp_path / f"{name}-helper"
        helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        helper.chmod(0o700)
        git("config", "remote.evil.promisor", "true")
        git("config", "remote.evil.url", f"ext::{helper}")
        git("config", "protocol.ext.allow", "always")
        if name == "control":
            result = subprocess.run(["git", "rev-parse", "--verify", f"{missing}^{{commit}}"],
                                    cwd=repo, capture_output=True, timeout=20, check=False)
            assert result.returncode != 0
            assert marker.exists()
        else:
            with pytest.raises(ValueError, match="not a commit"):
                workspace.resolve_commit(repo, missing)
            assert not marker.exists()


@pytest.mark.parametrize("failed_command", ["config", "ls-files", "status"])
def test_resolve_commit_warns_on_inspection_failure(tmp_path, monkeypatch, failed_command):
    from agent_lease_mcp import workspace

    repo = tmp_path / "repo"
    git = _git_repo(repo)
    original = workspace._git_hardened

    def fail_inspection(cwd, *args):
        return None if args[0] == failed_command else original(cwd, *args)

    monkeypatch.setattr(workspace, "_git_hardened", fail_inspection)
    assert workspace.resolve_commit(repo) == (str(repo), git("rev-parse", "HEAD"), True)


def test_resolve_commit_distinguishes_clean_and_dirty_repo(tmp_path):
    from agent_lease_mcp import workspace

    repo = tmp_path / "repo"
    _git_repo(repo)
    assert workspace.resolve_commit(repo)[2] is False
    (repo / "f.txt").write_text("changed")
    assert workspace.resolve_commit(repo)[2] is True


def test_review_blocks_a_committed_secret_end_to_end(server, tmp_path, monkeypatch):
    git = _git_repo(tmp_path / "leaky")
    secret = "sk-or-v1-" + "9f8e7d6c" * 6
    (tmp_path / "leaky" / "config.py").write_text(f'OPENROUTER_API_KEY = "{secret}"\n')
    git("add", "config.py")
    git("commit", "-qm", "add config")
    monkeypatch.chdir(tmp_path / "leaky")
    opened = server.review("request", topic="config", reviewer="codex")
    assert opened["secret_findings"] == [{"file": "config.py", "line": 1, "kind": "api-key-sk"}]
    assert "blocked" in opened and secret not in str(opened)
    with pytest.raises(PermissionError):
        server._store.review_verdict("codex", opened["id"], "approved", opened["head_commit"],
                                     security="pass", reviewer_findings=[])
    server._store.review_verdict("codex", opened["id"], "changes_requested", opened["head_commit"],
                                 "klíč do proměnné prostředí")
    (tmp_path / "leaky" / "config.py").write_text('import os\nKEY = os.environ["OPENROUTER_API_KEY"]\n')
    git("commit", "-qam", "use env")
    fixed = server.review("submit", review_id=opened["id"])
    assert fixed["secret_findings"] == [] and "blocked" not in fixed
    server._store.review_verdict("codex", opened["id"], "approved", fixed["head_commit"],
                                 security="pass", reviewer_findings=[])
    assert server.review("check")["approved"] is True


def test_fix_round_scans_the_whole_range_not_only_the_last_commit(server, tmp_path, monkeypatch):
    git = _git_repo(tmp_path / "range")
    monkeypatch.chdir(tmp_path / "range")
    (tmp_path / "range" / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("commit", "-qm", "clean start")
    opened = server.review("request", topic="range", reviewer="codex")
    assert opened["base_commit"] and opened["secret_findings"] == []
    server._store.review_verdict("codex", opened["id"], "changes_requested", opened["head_commit"], "x")
    (tmp_path / "range" / "b.py").write_text('TOKEN = "ghp_' + "A1b2C3d4E5" * 4 + '"\n')
    git("add", "b.py")
    git("commit", "-qm", "leak")
    (tmp_path / "range" / "c.py").write_text("y = 2\n")
    git("add", "c.py")
    git("commit", "-qm", "unrelated")
    again = server.review("submit", review_id=opened["id"])
    assert [f["file"] for f in again["secret_findings"]] == ["b.py"]


def test_reviewer_tool_rescans_and_ignores_a_faked_author_scan(server, tmp_path, monkeypatch):
    import dataclasses

    git = _git_repo(tmp_path / "fake")
    (tmp_path / "fake" / "k.py").write_text('KEY = "sk-' + "q1w2e3r4" * 4 + '"\n')
    git("add", "k.py")
    git("commit", "-qm", "key")
    head = git("rev-parse", "HEAD")
    base = git("rev-parse", "HEAD^")
    # autor obejde svůj nástroj a do úložiště rovnou zapíše „čistý“ scan
    review = server._store.review_open("claude-code", str(tmp_path / "fake"), "fake", "test-agent",
                                       base, head, [])
    assert review["secret_findings"] == []
    monkeypatch.chdir(tmp_path / "fake")
    with pytest.raises(ValueError):
        server.verdict(review["id"], "approved", "HEAD", security="pass")  # jen plný hash
    with pytest.raises(PermissionError, match="your secret scan found 1"):
        server.verdict(review["id"], "approved", head, security="pass")
    monkeypatch.setattr(server, "_settings", dataclasses.replace(server._settings, agent="gemini"))
    with pytest.raises(PermissionError):  # jiný než přidělený recenzent
        server.verdict(review["id"], "approved", head, security="pass")


def test_base_must_be_a_strict_ancestor_and_cannot_move(server, tmp_path, monkeypatch):
    git = _git_repo(tmp_path / "anc")
    monkeypatch.chdir(tmp_path / "anc")
    head = git("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="ancestor"):
        server.review("request", topic="t", reviewer="codex", base=head)  # base == head
    (tmp_path / "anc" / "g.txt").write_text("2")
    git("add", "g.txt")
    git("commit", "-qm", "two")
    opened = server.review("request", topic="t", reviewer="codex", base=head)
    server._store.review_verdict("codex", opened["id"], "changes_requested", opened["head_commit"], "x")
    (tmp_path / "anc" / "h.txt").write_text("3")
    git("add", "h.txt")
    git("commit", "-qm", "three")
    with pytest.raises(ValueError, match="cannot change"):
        server.review("submit", review_id=opened["id"], base="HEAD^")


def test_reviewer_refuses_a_base_that_is_not_an_ancestor(server, tmp_path, monkeypatch):
    git = _git_repo(tmp_path / "side")
    root = git("rev-parse", "HEAD")
    git("checkout", "-qb", "side")
    (tmp_path / "side" / "s.txt").write_text("s")
    git("add", "s.txt")
    git("commit", "-qm", "side")
    side = git("rev-parse", "HEAD")
    git("checkout", "-q", "-")
    (tmp_path / "side" / "m.txt").write_text("m")
    git("add", "m.txt")
    git("commit", "-qm", "main")
    head = git("rev-parse", "HEAD")
    assert root
    # base zapsaný mimo nástroj (přes broker) není předek head
    review = server._store.review_open("claude-code", str(tmp_path / "side"), "t", "test-agent",
                                       side, head, [])
    monkeypatch.chdir(tmp_path / "side")
    with pytest.raises(PermissionError, match="not an ancestor"):
        server.verdict(review["id"], "approved", head, security="pass")
    with pytest.raises(ValueError, match="cannot change"):
        server._store.review_submit("claude-code", review["id"], head, root)
