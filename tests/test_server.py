"""
Smoke testy MCP plochy. Nekontrolují logiku (od toho jsou testy policy a store),
ale to, že nástroje existují, jmenují se jak mají a vrací tvar, na který se agent
spolehne. Přejmenovaný nebo rozbitý nástroj je tichá porucha — server naběhne,
jen ho agent nenajde.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

EXPECTED_TOOLS = {"claim", "release", "owner", "say", "inbox", "room", "history"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.setenv("AGENT_NAME", "test-agent")
    # Import až po nastavení prostředí — server si settings čte při načtení modulu.
    import importlib

    from agent_lease_mcp import server

    importlib.reload(server)
    return Client(server.mcp)


@pytest.mark.asyncio
async def test_tool_surface_is_stable(client):
    async with client as c:
        assert {t.name for t in await c.list_tools()} == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_claim_then_owner_roundtrip(client, tmp_path):
    target = str(tmp_path / "a.txt")
    async with client as c:
        assert (await c.call_tool("claim", {"paths": [target]})).data["ok"] is True
        assert (await c.call_tool("owner", {"path": target})).data["held_by"] == "test-agent"


@pytest.mark.asyncio
async def test_release_frees_the_path(client, tmp_path):
    target = str(tmp_path / "a.txt")
    async with client as c:
        await c.call_tool("claim", {"paths": [target]})
        await c.call_tool("release", {})
        assert (await c.call_tool("owner", {"path": target})).data["held_by"] is None


@pytest.mark.asyncio
async def test_room_reports_me_claims_and_messages(client, tmp_path):
    async with client as c:
        await c.call_tool("claim", {"paths": [str(tmp_path / "a.txt")], "purpose": "oprava"})
        await c.call_tool("say", {"text": "beru si a.txt"})

        room = (await c.call_tool("room", {"status": "pracuju"})).data

        assert room["me"] == "test-agent"
        assert room["claims"][0]["purpose"] == "oprava"
        assert room["recent_messages"][0]["text"] == "beru si a.txt"


@pytest.mark.asyncio
async def test_history_answers_who_touched_the_path(client, tmp_path):
    target = str(tmp_path / "a.txt")
    async with client as c:
        await c.call_tool("claim", {"paths": [target], "purpose": "balení"})

        events = (await c.call_tool("history", {"path": target})).data["events"]

        assert events[0]["action"] == "claim"
        assert events[0]["agent"] == "test-agent"
