"""
Testy vynucení. Tohle je ta část, kvůli které projekt existuje — kdyby hook
mlčky pouštěl dál, je celý server jen nástěnka.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_lease_mcp.store import Store


def _env(agent: str, db: Path) -> dict:
    """Hook je podproces — potřebuje PYTHONPATH na src, jinak modul nenajde."""
    return {
        "PATH": "/usr/bin:/bin",
        "AGENT_NAME": agent,
        "AGENT_LEASE_DB": str(db),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
    }


def run_hook(payload: dict, agent: str, db: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent_lease_mcp.guard"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,  # nenulový kód je testovaný stav, ne chyba běhu
        env=_env(agent, db),
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "room.db"


def edit(path: Path) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": str(path)}}


def test_blocks_edit_of_foreign_claim(db: Path, tmp_path: Path):
    target = tmp_path / "make-work-package.sh"
    Store(db).claim([str(target)], agent="codex", purpose="balicí skript")

    result = run_hook(edit(target), agent="claude-code", db=db)

    assert result.returncode == 2, "cizí rozpracovaný soubor se musí zablokovat"
    assert "codex" in result.stderr
    assert "balicí skript" in result.stderr, "důvod má říct, co druhý dělá"


def test_allows_own_claim(db: Path, tmp_path: Path):
    target = tmp_path / "a.txt"
    Store(db).claim([str(target)], agent="codex")

    assert run_hook(edit(target), agent="codex", db=db).returncode == 0


def test_free_path_is_auto_claimed(db: Path, tmp_path: Path):
    """Na explicitní claim se nesmí dát zapomenout."""
    target = tmp_path / "volny.txt"

    assert run_hook(edit(target), agent="claude-code", db=db).returncode == 0
    assert Store(db).holder_of(str(target)).agent == "claude-code"


def test_bash_is_not_guarded(db: Path, tmp_path: Path):
    """Z příkazové řádky nejde spolehlivě zjistit, co skript zapíše."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo ahoj"}}

    assert run_hook(payload, agent="claude-code", db=db).returncode == 0


def test_codex_payload_shape_is_understood(db: Path, tmp_path: Path):
    """Codex posílá jiné jméno nástroje i klíč cesty než Claude Code."""
    target = tmp_path / "a.txt"
    Store(db).claim([str(target)], agent="claude-code", purpose="oprava")

    payload = {"toolName": "apply_patch", "toolInput": {"path": str(target)}}

    assert run_hook(payload, agent="codex", db=db).returncode == 2


def test_broken_input_does_not_block(db: Path):
    """Fail-open: rozbitá koordinace nesmí zastavit práci."""
    result = subprocess.run(
        [sys.executable, "-m", "agent_lease_mcp.guard"],
        input="tohle není JSON",
        capture_output=True,
        text=True,
        check=False,  # nenulový kód je testovaný stav, ne chyba běhu
        env=_env("claude-code", db),
    )

    assert result.returncode == 0


def test_race_loser_is_blocked_when_claim_fails(db: Path, tmp_path: Path, monkeypatch, capsys):
    """APP-007: čtení „volno" a claim nejsou jedno rozhodnutí. Když mezi nimi
    vyhraje druhý agent, musí rozhodnout výsledek atomického claimu, ne čtení."""
    import io

    from agent_lease_mcp import guard

    target = tmp_path / "shared.sh"
    Store(db).claim([str(target)], agent="codex", purpose="vyhrál závod")
    # Simulace okna: guard ještě viděl volno, ale codex mezitím claim zapsal.
    monkeypatch.setattr(Store, "holder_of", lambda self, path: None)
    monkeypatch.setenv("AGENT_NAME", "claude-code")
    monkeypatch.setenv("AGENT_LEASE_DB", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(edit(target))))

    assert guard.main() == 2, "poražený v závodu nesmí projít"
    assert "codex" in capsys.readouterr().err
    held = Store(db).claims()
    assert [c.agent for c in held] == ["codex"], "nájem zůstává vítězi"


@pytest.mark.parametrize(
    "command",
    ["echo x > {t}", "echo x >> {t}", "cat a | tee {t}", "sed -i 's/a/b/' {t}", "cp other.txt {t}"],
)
def test_shell_write_into_foreign_claim_is_blocked(db: Path, tmp_path: Path, command: str):
    """APP-006: zjevný zápis z příkazové řádky do cizího souboru se zablokuje."""
    target = tmp_path / "shared.txt"
    Store(db).claim([str(target)], agent="codex", purpose="rozpracováno")
    payload = {"tool_name": "Bash", "tool_input": {"command": command.format(t=target)}}

    assert run_hook(payload, agent="claude-code", db=db).returncode == 2


def test_shell_write_to_free_file_passes_without_claim(db: Path, tmp_path: Path):
    target = tmp_path / "out.log"
    payload = {"tool_name": "Bash", "tool_input": {"command": f"echo x > {target}"}}

    assert run_hook(payload, agent="claude-code", db=db).returncode == 0
    assert Store(db).holder_of(str(target)) is None, "volný cíl se do nájmu nebere"


def test_shell_relative_target_uses_agent_cwd(db: Path, tmp_path: Path):
    target = tmp_path / "rel.txt"
    Store(db).claim([str(target)], agent="codex")
    payload = {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": "echo x > rel.txt"}}

    assert run_hook(payload, agent="claude-code", db=db).returncode == 2
