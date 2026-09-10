"""
Testy automatizace. Tohle je ta půlka, která má fungovat BEZ uživatele —
když mlčky selže, nikdo si toho nevšimne, dokud si dva agenti zase nešlápnou
do souboru.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_lease_mcp.briefing import prompt_update, session_briefing
from agent_lease_mcp.models import Claim
from agent_lease_mcp.store import Store


def _env(agent: str, db: Path) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "AGENT_NAME": agent,
        "AGENT_LEASE_DB": str(db),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
    }


def run(module: str, payload: dict, agent: str, db: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=_env(agent, db),
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "room.db"


def claim_of(agent: str, path: str = "/repo/x.sh", purpose: str = "balení") -> Claim:
    return Claim(path=path, agent=agent, purpose=purpose, claimed_at=0, expires_at=9e12)


class TestBriefing:
    def test_silent_when_alone_and_nothing_new(self):
        """Vstřikovat stav pokaždé by byl šum a agent by to začal přeskakovat."""
        assert session_briefing("claude-code", peers=[], claims=[], messages=[]) == ""

    def test_names_the_other_agent_and_what_it_holds(self):
        peers = [{"agent": "codex", "status": "balí balíček", "active": True, "seen_seconds_ago": 5}]
        text = session_briefing("claude-code", peers, [claim_of("codex")], [])
        assert "codex" in text
        assert "balí balíček" in text
        assert "/repo/x.sh" in text

    def test_own_claims_are_not_warned_about(self):
        peers = [{"agent": "claude-code", "status": "", "active": True, "seen_seconds_ago": 1}]
        text = session_briefing("claude-code", peers, [claim_of("claude-code")], [])
        assert "NESAHAT" not in text

    def test_prompt_update_is_silent_without_messages(self):
        assert prompt_update("claude-code", [claim_of("codex")], []) == ""

    def test_prompt_update_delivers_messages(self):
        msgs = [{"agent": "codex", "text": "beru si deploy/", "seconds_ago": 3}]
        assert "beru si deploy/" in prompt_update("claude-code", [], msgs)


class TestContextHook:
    def test_session_start_emits_additional_context(self, db: Path):
        Store(db).claim(["/repo/x.sh"], agent="codex", purpose="balení")
        Store(db).heartbeat("codex", status="balí")

        out = run("agent_lease_mcp.lifecycle", {"hook_event_name": "SessionStart"}, "claude-code", db)

        payload = json.loads(out.stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "codex" in payload["hookSpecificOutput"]["additionalContext"]

    def test_messages_are_delivered_once(self, db: Path):
        """Bez kurzoru by se stejné zprávy vstřikovaly dokola při každém promptu."""
        Store(db).say("codex", "nesahej na deploy/")
        event = {"hook_event_name": "UserPromptSubmit"}

        first = run("agent_lease_mcp.lifecycle", event, "claude-code", db)
        second = run("agent_lease_mcp.lifecycle", event, "claude-code", db)

        assert "nesahej na deploy/" in first.stdout
        assert second.stdout == "", "podruhé už se doručovat nesmí"

    def test_prompt_hook_refreshes_presence(self, db: Path):
        """
        Agent, který jede jen přes hooky (Codex nemá MCP), musí v místnosti
        zůstat vidět jako živý. Než se tohle doplnilo, vypadal po 15 minutách
        práce jako mrtvý — a druhý agent ho přestal brát v potaz (10.9.2026).
        """
        store = Store(db)
        store.heartbeat("codex", status="balí balíček")
        with sqlite3.connect(db) as con:  # posunout ho do minulosti = „vyčichlý"
            con.execute("UPDATE agents SET seen_at = seen_at - 1800 WHERE agent = 'codex'")
        assert next(p for p in store.peers() if p["agent"] == "codex")["active"] is False

        run("agent_lease_mcp.lifecycle", {"hook_event_name": "UserPromptSubmit"}, "codex", db)

        peer = next(p for p in store.peers() if p["agent"] == "codex")
        assert peer["active"] is True
        assert peer["status"] == "balí balíček", "hook nesmí přepsat, na čem agent dělá"

    def test_own_messages_are_not_echoed_back(self, db: Path):
        Store(db).say("claude-code", "beru si to")
        out = run("agent_lease_mcp.lifecycle", {"hook_event_name": "UserPromptSubmit"}, "claude-code", db)
        assert out.stdout == ""

    def test_broken_db_does_not_break_the_session(self, tmp_path: Path):
        out = run("agent_lease_mcp.lifecycle", {}, "claude-code", tmp_path / "nested" / "room.db")
        assert out.returncode == 0


class TestReleaseHook:
    def test_stop_releases_everything_the_agent_held(self, db: Path):
        """Bez tohohle by soubory zůstaly zamčené až do vypršení TTL."""
        Store(db).claim(["/repo/a.sh", "/repo/b.sh"], agent="claude-code")

        out = subprocess.run(
            [sys.executable, "-c",
             "from agent_lease_mcp.lifecycle import release_main; raise SystemExit(release_main())"],
            input="{}", capture_output=True, text=True, check=False, env=_env("claude-code", db),
        )

        assert out.returncode == 0
        assert Store(db).claims() == []

    def test_other_agents_claims_survive(self, db: Path):
        Store(db).claim(["/repo/a.sh"], agent="codex")

        subprocess.run(
            [sys.executable, "-c",
             "from agent_lease_mcp.lifecycle import release_main; raise SystemExit(release_main())"],
            input="{}", capture_output=True, text=True, check=False, env=_env("claude-code", db),
        )

        assert [c.agent for c in Store(db).claims()] == ["codex"]
