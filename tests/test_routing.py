"""B2: `--to auto` nikdy na most, budík, broker ani člověka; minimální popis schopností."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_lease_mcp import jev, routing
from agent_lease_mcp.cli import main
from agent_lease_mcp.store import Store


def peer(name, active=True):
    return {"agent": name, "active": active}


def test_candidates_exclude_service_and_human_identities(tmp_path: Path):
    (tmp_path / "human-name").write_text("michal")
    db = tmp_path / "room.db"
    peers = [peer(n) for n in ("codex", "claude-code", "codex-bridge", "claude-code-bridge",
                               "codex-wake", "broker", "michal", "human", "untrusted-x-1",
                               "gemini")]
    assert routing.candidates(peers, "claude-code", db) == ["codex", "gemini"]


def test_offline_agent_with_running_bridge_is_reachable(tmp_path: Path):
    db = tmp_path / "room.db"
    peers = [peer("codex", active=False), peer("codex-bridge"), peer("gemini", active=False)]
    assert routing.candidates(peers, "claude-code", db) == ["codex"]
    # most bez řádku agenta v peers stačí taky
    assert routing.candidates([peer("codex-bridge")], "claude-code", db) == ["codex"]


def test_capabilities_have_defaults_and_env_override(monkeypatch):
    assert "codex" in routing.capabilities() and "claude-code" in routing.capabilities()
    monkeypatch.setenv("AGENT_LEASE_CAPABILITIES", "codex=jen review;gemini=obrázky;bad")
    caps = routing.capabilities()
    assert caps["codex"] == "jen review" and caps["gemini"] == "obrázky" and "bad" not in caps


def test_route_sends_capabilities_to_jev(monkeypatch):
    seen = {}

    def fake_ask(state, questions, **_):
        seen["state"] = state
        return {}

    monkeypatch.setattr(jev, "ask", fake_ask)
    jev.route("oprav test", ["codex", "claude-code"], {"codex": "review", "claude-code": "návrh"})
    assert "- codex: review" in seen["state"] and "- claude-code: návrh" in seen["state"]


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "room.db"
    (tmp_path / "human-name").write_text("michal")
    monkeypatch.setenv("AGENT_LEASE_DB", str(path))
    monkeypatch.setenv("AGENT_NAME", "claude-code")
    monkeypatch.delenv("AGENT_LEASE_REQUIRE_BROKER", raising=False)
    monkeypatch.delenv("AGENT_LEASE_SOCKET", raising=False)
    return path


def test_cli_auto_never_picks_bridge_or_wake(db: Path, capsys, monkeypatch):
    store = Store(db)
    for name in ("codex-bridge", "codex-wake", "claude-code-bridge"):
        store.heartbeat(name, cwd="/")
    monkeypatch.setattr(jev, "enabled", lambda: False)
    assert main(["send", "zkontroluj", "--to", "auto", "--kind", "task"]) == 0
    assert "auto → codex" in capsys.readouterr().out
    assert [j["recipient"] for j in Store(db).jobs("codex")] == ["codex"]
    assert Store(db).jobs("codex-bridge") == [] and Store(db).jobs("codex-wake") == []


def test_cli_auto_refuses_when_only_services_are_around(db: Path, capsys, monkeypatch):
    Store(db).heartbeat("codex-wake", cwd="/")
    monkeypatch.setattr(jev, "enabled", lambda: False)
    assert main(["send", "x", "--to", "auto"]) == 1
    assert "cannot decide" in capsys.readouterr().err
