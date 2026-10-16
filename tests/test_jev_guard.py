"""
Testy orchestrace. `evaluate()` je čistá až na `classify` (injektovatelné),
takže Jev cesty jdou otestovat bez sítě a bez API klíče. Podprocesové testy
na konci ověřují jen skutečné zapojení hooku (stdin → exit kód).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agent_lease_mcp.jev_client import JevAnswer, JevError
from agent_lease_mcp.jev_guard import evaluate


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def fail_if_called(_command: str) -> JevAnswer:
    raise AssertionError("Jev se v tomhle případě nesměl volat")


class TestNonBashPassesThrough:
    def test_edit_tool_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x"}}

        code, msg = evaluate(payload, classify=fail_if_called)

        assert code == 0 and msg is None

    def test_missing_command_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        code, msg = evaluate({"tool_name": "Bash", "tool_input": {}}, classify=fail_if_called)

        assert code == 0 and msg is None


class TestHardBlockAlwaysActive:
    """Natvrdo blokované vzory platí bez ohledu na JEV_GUARD_ENABLED."""

    def test_blocks_without_calling_jev(self, monkeypatch, tmp_path):
        monkeypatch.delenv("JEV_GUARD_ENABLED", raising=False)
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        code, msg = evaluate(bash("rm -rf /"), classify=fail_if_called)

        assert code == 2
        assert msg is not None and "hard rule" in msg

    def test_blocks_even_when_jev_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JEV_GUARD_ENABLED", "true")
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        code, _ = evaluate(bash("git reset --hard"), classify=fail_if_called)

        assert code == 2


class TestDisabledByDefault:
    def test_safe_command_passes_without_key_or_network(self, monkeypatch, tmp_path):
        monkeypatch.delenv("JEV_GUARD_ENABLED", raising=False)
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        code, msg = evaluate(bash("git push origin feature-x"), classify=fail_if_called)

        assert code == 0 and msg is None


class TestShadowMode:
    def test_never_blocks_even_on_destructive_answer(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JEV_GUARD_ENABLED", "true")
        monkeypatch.delenv("JEV_GUARD_MODE", raising=False)  # výchozí = shadow
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        code, msg = evaluate(
            bash("nejasny prikaz"), classify=lambda c: JevAnswer(p_destructive=0.99)
        )

        assert code == 0 and msg is None

    def test_still_logs_the_decision(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JEV_GUARD_ENABLED", "true")
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        evaluate(bash("nejasny prikaz"), classify=lambda c: JevAnswer(p_destructive=0.9))

        log = (tmp_path / "jev-guard.log").read_text(encoding="utf-8")
        entry = json.loads(log.strip().splitlines()[-1])
        assert entry["source"] == "shadow"
        assert entry["p_destructive"] == 0.9

    def test_log_and_classifier_never_see_secrets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JEV_GUARD_ENABLED", "true")
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
        seen = []

        def classify(command):
            seen.append(command)
            return JevAnswer(p_destructive=0.1)

        evaluate(bash("deploy --token=supersecret123 && curl -H 'Authorization: Bearer abc999'"), classify=classify)

        log_path = tmp_path / "jev-guard.log"
        log = log_path.read_text(encoding="utf-8")
        for secret in ("supersecret123", "abc999"):
            assert secret not in log
            assert secret not in seen[0]
        assert log_path.stat().st_mode & 0o077 == 0, "log jen pro vlastníka"


class TestEnforceMode:
    def test_blocks_above_threshold(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JEV_GUARD_ENABLED", "true")
        monkeypatch.setenv("JEV_GUARD_MODE", "enforce")
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        code, msg = evaluate(bash("neco riskantniho"), classify=lambda c: JevAnswer(p_destructive=0.9))

        assert code == 2
        assert "0.90" in msg

    def test_allows_below_threshold(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JEV_GUARD_ENABLED", "true")
        monkeypatch.setenv("JEV_GUARD_MODE", "enforce")
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        code, msg = evaluate(bash("cat soubor.txt"), classify=lambda c: JevAnswer(p_destructive=0.1))

        assert code == 0 and msg is None

    def test_custom_threshold_is_respected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JEV_GUARD_ENABLED", "true")
        monkeypatch.setenv("JEV_GUARD_MODE", "enforce")
        monkeypatch.setenv("JEV_GUARD_THRESHOLD", "0.9")
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        code, _ = evaluate(bash("neco"), classify=lambda c: JevAnswer(p_destructive=0.7))

        assert code == 0, "0.7 < práh 0.9, nemá blokovat"

    def test_fails_open_on_jev_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JEV_GUARD_ENABLED", "true")
        monkeypatch.setenv("JEV_GUARD_MODE", "enforce")
        monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))

        def broken(_command: str) -> JevAnswer:
            raise JevError("timeout")

        code, msg = evaluate(bash("neco"), classify=broken)

        assert code == 0
        assert msg is not None and "unavailable" in msg


def _hook_env(db: Path) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "AGENT_LEASE_DB": str(db),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
    }


def test_entrypoint_blocks_hard_rule_via_stdin(tmp_path: Path):
    """Smoke test na skutečné zapojení hooku (stdin → exit kód), bez sítě."""
    result = subprocess.run(
        [sys.executable, "-m", "agent_lease_mcp.jev_guard"],
        input=json.dumps(bash("git reset --hard")),
        capture_output=True,
        text=True,
        check=False,
        env=_hook_env(tmp_path / "room.db"),
    )

    assert result.returncode == 2
    assert "hard rule" in result.stderr


def test_entrypoint_safe_command_passes_via_stdin(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-m", "agent_lease_mcp.jev_guard"],
        input=json.dumps(bash("echo ahoj")),
        capture_output=True,
        text=True,
        check=False,
        env=_hook_env(tmp_path / "room.db"),
    )

    assert result.returncode == 0


def test_entrypoint_broken_stdin_does_not_block(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-m", "agent_lease_mcp.jev_guard"],
        input="tohle není JSON",
        capture_output=True,
        text=True,
        check=False,
        env=_hook_env(tmp_path / "room.db"),
    )

    assert result.returncode == 0
