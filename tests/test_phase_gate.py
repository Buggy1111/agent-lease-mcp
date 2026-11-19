"""Regression tests for scripts/phase_gate.py; no external services."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "phase_gate.py"
spec = importlib.util.spec_from_file_location("phase_gate", SCRIPT)
phase_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phase_gate)


def test_default_blocks_progress(tmp_path):
    state = tmp_path / "phase.json"
    assert phase_gate.main(["--state", str(state), "init"]) == 0
    assert phase_gate.main(["--state", str(state), "advance", "--confirm"]) == 2
    assert phase_gate.load(state)["phase"] == "A"


def test_unverified_evidence_does_not_count(tmp_path):
    state = tmp_path / "phase.json"
    phase_gate.main(["--state", str(state), "init"])
    for gate in phase_gate.GATES["A"]:
        phase_gate.main(["--state", str(state), "record", gate, "https://example.test/evidence"])
    assert len(phase_gate.remaining(phase_gate.load(state))) == len(phase_gate.GATES["A"])


def test_advance_needs_verified_evidence_and_confirmation(tmp_path):
    state = tmp_path / "phase.json"
    phase_gate.main(["--state", str(state), "init"])
    for gate in phase_gate.GATES["A"]:
        phase_gate.main(["--state", str(state), "record", gate, "review-or-test-id", "--verified"])
    assert phase_gate.main(["--state", str(state), "advance"]) == 0
    assert phase_gate.load(state)["phase"] == "A"
    assert phase_gate.main(["--state", str(state), "advance", "--confirm"]) == 0
    assert phase_gate.load(state)["phase"] == "B"
    assert phase_gate.main(["--state", str(state), "advance", "--confirm"]) == 2


def test_invalid_or_missing_state_is_safe(tmp_path):
    state = tmp_path / "phase.json"
    phase_gate.main(["--state", str(state), "init"])
    import pytest
    with pytest.raises(SystemExit):
        phase_gate.main(["--state", str(state), "init"])
    state.write_text('{"phase": "D"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        phase_gate.main(["--state", str(state), "advance", "--confirm"])
