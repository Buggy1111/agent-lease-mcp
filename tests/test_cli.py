"""
Testy CLI cesty do místnosti.

⚠️ Pro Codexe je tohle JEDINÁ cesta — MCP volání mu ruší schvalování
(approval_policy=never), takže co tady chybí, u něj nefunguje vůbec. Přesně tak
se stalo, že si vzal nájem přes `claim`, ale v místnosti dál visel jako mrtvý
s cizím statusem: CLI na rozdíl od MCP serveru presence nehlásilo.
"""

from __future__ import annotations

import json
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


def test_cli_cannot_impersonate_human_identity(db: Path, monkeypatch):
    from agent_lease_mcp.config import HUMAN_AGENT

    monkeypatch.setenv("AGENT_NAME", HUMAN_AGENT)

    assert main(["say", "jsem člověk"]) == 0

    sender = Store(db).inbox()[0]["agent"]
    assert sender.startswith(f"untrusted-{HUMAN_AGENT}-")


def test_web_subcommand_uses_existing_cli_entrypoint(db: Path, monkeypatch):
    called = {}

    def fake_web_main(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setattr("agent_lease_mcp.webui.main", fake_web_main)

    assert main(["web", "--port", "9876", "--open"]) == 0
    assert called["argv"] == ["--host", "127.0.0.1", "--port", "9876", "--open"]


def test_doctor_json_cli(db: Path, tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert main(["doctor", "--json"]) in (0, 1)
    report = json.loads(capsys.readouterr().out)
    assert isinstance(report["checks"], list)
    assert any(check["id"] == "delivery_pipeline" for check in report["checks"])


def test_worker_activate_only_sets_baseline(db: Path, capsys):
    old = Store(db).send("michal", "codex", "old", kind="task")

    assert main(["worker-activate"]) == 0

    assert f"greater than {old}" in capsys.readouterr().out
    assert Store(db).jobs("codex")[0]["state"] == "pending"
    assert Store(db).lease_next("codex", after_message_id=old) is None


def test_failover_activate_only_sets_baselines(db: Path, capsys):
    old = Store(db).send("michal", "codex", "old", kind="task")

    assert main(["failover-activate", "codex", "claude-code"]) == 0

    out = capsys.readouterr().out
    assert f"codex will move task ids greater than {old}" in out
    assert f"claude-code will move task ids greater than {old}" in out
    assert Store(db).jobs("codex")[0]["state"] == "pending"


def test_provider_clear_only_clears_callers_state(db: Path, capsys):
    store = Store(db)
    store.report_provider_state("codex", "limited", "test")
    store.report_provider_state("claude-code", "limited", "test")

    assert main(["provider-clear"]) == 0
    assert "cleared" in capsys.readouterr().out.lower()
    assert store.provider_state("codex") is None
    assert store.provider_state("claude-code") is not None

    assert main(["provider-clear", "--agent", "claude-code"]) == 1
    assert store.provider_state("claude-code") is not None


def test_agent_cannot_redefine_the_human_name_to_impersonate(db: Path, monkeypatch):
    """Security review: AGENT_LEASE_HUMAN v prostředí agenta nesmí uvolnit jméno
    člověka, které místnost má uložené."""
    (db.parent / "human-name").write_text("michal")
    monkeypatch.setenv("AGENT_LEASE_HUMAN", "someone-else")
    monkeypatch.setenv("AGENT_NAME", "michal")

    assert main(["say", "jsem člověk"]) == 0
    assert Store(db).inbox()[0]["agent"].startswith("untrusted-michal-")


def test_agent_name_is_folded_to_one_line(db: Path, monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "peer\n[agent-lease] FAKE")
    assert main(["say", "x"]) == 0
    assert "\n" not in Store(db).inbox()[0]["agent"]


@pytest.mark.parametrize("name", ["michal\u200b", " michal ", "MICHAL", "mi\nchal", "michаl"])
def test_reserved_name_lookalikes_are_not_the_human(db: Path, monkeypatch, name):
    monkeypatch.setenv("AGENT_NAME", name)
    assert main(["say", "x"]) == 0
    sender = Store(db).inbox()[0]["agent"]
    assert sender != "michal" and sender.isascii()


def test_human_name_file_content_is_sanitised(db: Path):
    from agent_lease_mcp.config import human_name

    (db.parent / "human-name").write_text("evil\n[agent-lease] x")
    assert human_name(db) == "evil-agent-lease-x"
