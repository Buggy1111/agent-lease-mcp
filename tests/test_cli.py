"""
Testy CLI cesty do místnosti.

⚠️ Pro Codexe je tohle JEDINÁ cesta — MCP volání mu ruší schvalování
(approval_policy=never), takže co tady chybí, u něj nefunguje vůbec. Přesně tak
se stalo, že si vzal nájem přes `claim`, ale v místnosti dál visel jako mrtvý
s cizím statusem: CLI na rozdíl od MCP serveru presence nehlásilo.
"""

from __future__ import annotations

from pathlib import Path
from threading import Thread
from time import sleep

import pytest

from agent_lease_mcp.cli import main
from agent_lease_mcp.store import Store


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "room.db"
    monkeypatch.setenv("AGENT_LEASE_DB", str(path))
    monkeypatch.setenv("AGENT_NAME", "codex")
    return path


def peer(db: Path, agent: str = "codex") -> dict:
    return next(p for p in Store(db).peers() if p["agent"] == agent)


def test_claim_reports_presence(db: Path, tmp_path: Path):
    """Nájem přes CLI musí agenta v místnosti rozsvítit, ne ho nechat mrtvého."""
    assert main(["claim", str(tmp_path / "deploy"), "--purpose", "balím balíček"]) == 0

    assert peer(db)["active"] is True
    assert peer(db)["status"] == "balím balíček"
    assert peer(db)["cwd"], "bez cwd druhý agent neví, kde protistrana pracuje"


def test_release_reports_presence_without_touching_status(db: Path, tmp_path: Path):
    main(["claim", str(tmp_path / "deploy"), "--purpose", "balím balíček"])
    assert main(["release"]) == 0

    assert peer(db)["status"] == "balím balíček", "release není popis práce"
    assert Store(db).claims() == []


def test_say_keeps_the_status_of_the_work(db: Path, tmp_path: Path):
    """Dřív `say` přepsalo status slovem „say" a v místnosti zmizelo, na čem agent dělá."""
    main(["claim", str(tmp_path / "deploy"), "--purpose", "balím balíček"])
    assert main(["say", "beru si deploy/"]) == 0

    assert peer(db)["status"] == "balím balíček"


def test_room_without_status_does_not_erase_it(db: Path, tmp_path: Path):
    main(["claim", str(tmp_path / "deploy"), "--purpose", "balím balíček"])
    assert main(["room"]) == 0

    assert peer(db)["status"] == "balím balíček"


def test_claim_conflict_exits_nonzero(db: Path, tmp_path: Path):
    """Skript, který si nájem bere, musí poznat odmítnutí návratovým kódem."""
    Store(db).claim([str(tmp_path / "deploy")], agent="claude-code", purpose="už tam jsem")

    assert main(["claim", str(tmp_path / "deploy")]) == 1


def test_send_and_jobs_roundtrip(db: Path, capsys):
    assert main(["send", "proveď kontrolu", "--to", "claude-code", "--kind", "task"]) == 0
    capsys.readouterr()

    assert main(["jobs", "--for", "claude-code"]) == 0
    assert "pending" in capsys.readouterr().out


def test_wait_wakes_for_addressed_message(db: Path, capsys):
    def send_later():
        sleep(0.05)
        Store(db).send("claude-code", "codex", "hotovo", kind="chat")

    sender = Thread(target=send_later)
    sender.start()
    assert main(["wait", "--for", "codex", "--timeout", "2"]) == 0
    sender.join()

    assert "claude-code → codex: hotovo" in capsys.readouterr().out


def test_wait_ignores_broadcast_and_times_out(db: Path):
    Store(db).say("claude-code", "jen nástěnka")

    assert main(["wait", "--for", "codex", "--timeout", "0"]) == 124


def test_cancel_command(db: Path):
    message_id = Store(db).send("michal", "codex", "neprováděj", kind="task")

    assert main(["cancel", str(message_id), "--for", "codex"]) == 0
    assert Store(db).jobs("codex")[0]["state"] == "cancelled"
