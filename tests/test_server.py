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
    "claim", "release", "owner", "say", "send", "jobs", "next_task", "ack", "retry",
    "cancel",
    "inbox", "room", "history",
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
