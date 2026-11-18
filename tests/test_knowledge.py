"""D1: sdílená paměť příznak → příčina → řešení."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_webui import authenticated_handler, call_handler

from agent_lease_mcp.broker import METHODS
from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store
from agent_lease_mcp.webui import SESSION_COOKIE


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "room.db")


def test_new_entry_is_unverified_and_not_a_fact(store: Store):
    e = store.knowledge_add("claude-code", "pytest hangs on import", cause="WSL OOM", solution="run -x")
    assert e["status"] == "unverified" and e["fact"] is False


def test_author_cannot_verify_own_entry_and_evidence_is_required(store: Store):
    e = store.knowledge_add("claude-code", "build fails")
    with pytest.raises(PermissionError):
        store.knowledge_verdict("claude-code", e["id"], "verified", "commit abc")
    with pytest.raises(ValueError):
        store.knowledge_verdict("codex", e["id"], "verified", "  ")
    with pytest.raises(ValueError):
        store.knowledge_verdict("codex", e["id"], "approved", "commit abc")
    v = store.knowledge_verdict("codex", e["id"], "verified", "commit abc")
    assert v["fact"] is True and v["verified_by"] == "codex"


def test_unnamed_session_and_human_name_cannot_judge_over_agent_api(store: Store):
    e = store.knowledge_add("codex", "x fails")
    with pytest.raises(PermissionError):
        store.knowledge_verdict("unconfigured-codex-1", e["id"], "verified", "proof")
    with pytest.raises(PermissionError):
        store.knowledge_verdict("michal", e["id"], "verified", "proof")
    with pytest.raises(PermissionError):
        store.knowledge_add("michal", "pretend to be human")


def test_human_methods_are_not_reachable_through_broker():
    assert not {m for m in METHODS if m.startswith("knowledge_human")}
    assert {"knowledge_add", "knowledge_search", "knowledge_verdict"} <= METHODS


def test_search_ranks_verified_first_hides_retired_and_ignores_diacritics(store: Store):
    a = store.knowledge_add("claude-code", "Pomalé spuštění serveru", solution="cache")
    b = store.knowledge_add("claude-code", "pomale spusteni po restartu", solution="warmup")
    c = store.knowledge_add("claude-code", "pomalé spuštění, zastaralá rada")
    store.knowledge_verdict("codex", b["id"], "verified", "test_x PASS")
    store.knowledge_verdict("codex", c["id"], "stale", "v2 changed it")
    ids = [r["id"] for r in store.knowledge_search("pomale spusteni")]
    assert ids[0] == b["id"] and a["id"] in ids and c["id"] not in ids
    assert c["id"] in [r["id"] for r in store.knowledge_search("spuštění", include_retired=True)]


def test_search_is_safe_against_fts_syntax_and_redacts_secrets(store: Store):
    store.knowledge_add("claude-code", "token leak", solution="key sk-ant-api03-" + "a" * 40)
    assert store.knowledge_search('") OR * NEAR(') == []  # syntaxe FTS se nesmí dostat do dotazu
    entry = store.knowledge_search("token leak")[0]
    assert "a" * 40 not in entry["solution"]


def test_web_add_and_verdict_as_human(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    handler = authenticated_handler(store, settings, "secret")
    cookie = {"Cookie": f"{SESSION_COOKIE}=session"}
    agent_entry = store.knowledge_add("codex", "flaky e2e", solution="wait for idle")

    status, raw = call_handler(store, settings, "secret", "POST", "/api/knowledge-verdict", headers=cookie,
                               body={"id": agent_entry["id"], "verdict": "verified", "evidence": "CI #12"},
                               handler_type=handler)
    assert status == 200 and json.loads(raw)["fact"] is True

    status, raw = call_handler(store, settings, "secret", "POST", "/api/knowledge-add", headers=cookie,
                               body={"symptom": "web add", "project": "demo"}, handler_type=handler)
    own = json.loads(raw)
    assert status == 200 and own["status"] == "unverified"
    status, _ = call_handler(store, settings, "secret", "POST", "/api/knowledge-verdict", headers=cookie,
                             body={"id": own["id"], "verdict": "verified", "evidence": "me"},
                             handler_type=handler)
    assert status == 409  # ani člověk nepotvrzuje sám sebe

    status, raw = call_handler(store, settings, "secret", "GET", "/api/knowledge?q=flaky", headers=cookie,
                               handler_type=handler)
    assert status == 200 and json.loads(raw)["entries"][0]["id"] == agent_entry["id"]


def test_web_knowledge_needs_session(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    status, _ = call_handler(store, settings, "secret", "GET", "/api/knowledge")
    assert status == 403
    status, _ = call_handler(store, settings, "secret", "POST", "/api/knowledge-add",
                             body={"symptom": "x"})
    assert status == 403


def test_agent_cannot_override_human_verdict(store: Store):
    e = store.knowledge_add("codex", "wrong fix")
    store.knowledge_human_verdict("michal", e["id"], "rejected", "tried it, still broken")
    with pytest.raises(PermissionError):
        store.knowledge_verdict("claude-code", e["id"], "verified", "trust me")
    assert store.knowledge_get(e["id"])["status"] == "rejected"
