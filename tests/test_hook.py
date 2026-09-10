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
