"""C3a: spotřeba tokenů z výstupu klienta a připnutý model."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from agent_lease_mcp.bridge import DEFAULT_COMMANDS, Bridge, RunResult, extract_usage, with_model
from agent_lease_mcp.broker import METHODS
from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store

# Tvar ověřený 11. 11. 2026 skutečným `claude -p … --output-format json` (zkrácený).
CLAUDE = {"type": "result", "is_error": False, "result": "OK", "total_cost_usd": 0.1325501,
          "usage": {"input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 9021,
                    "cache_creation_input_tokens": 32901},
          "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 2}}}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "room.db", settings=Settings(tmp_path / "room.db", "codex", 1800))


def test_claude_json_gives_result_text_usage_and_actual_model():
    text, usage = extract_usage(DEFAULT_COMMANDS["claude-code"], json.dumps(CLAUDE))
    assert text == "OK"
    assert usage == {"source": "claude-json", "model": "claude-sonnet-5-5", "input_tokens": 2,
                     "output_tokens": 4, "cache_read_tokens": 9021,
                     "cache_creation_tokens": 32901, "cost_usd": 0.1325501}


def test_codex_jsonl_takes_the_last_usage_event_and_keeps_the_text():
    lines = "\n".join([json.dumps({"type": "thread.started"}), "not json",
                       json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10,
                                   "cached_input_tokens": 3, "output_tokens": 5}})])
    text, usage = extract_usage(DEFAULT_COMMANDS["codex"], lines)
    assert text == lines
    # Codex počítá cache do vstupu; uložený vstup je jen ten nový (jako u Claude)
    assert usage["source"] == "codex-jsonl" and usage["input_tokens"] == 7
    assert usage["cache_read_tokens"] == 3 and usage["cost_usd"] is None


@pytest.mark.parametrize("template, output", [
    ("claude -p {prompt} --output-format text", "plain answer"),
    (DEFAULT_COMMANDS["claude-code"], "not json at all"),
    (DEFAULT_COMMANDS["claude-code"], "[1, 2]"),
    (DEFAULT_COMMANDS["codex"], "no events"),
])
def test_unknown_usage_is_none_not_zero(template, output):
    assert extract_usage(template, output) == (output, None)


def test_garbage_numbers_become_unknown():
    bad = dict(CLAUDE, total_cost_usd="free", usage={"input_tokens": -1, "output_tokens": True})
    _, usage = extract_usage(DEFAULT_COMMANDS["claude-code"], json.dumps(bad))
    assert usage["input_tokens"] is None and usage["output_tokens"] is None
    assert usage["cost_usd"] is None


def test_bridge_records_usage_of_the_attempt(store: Store, tmp_path: Path):
    script = tmp_path / "fake.py"
    script.write_text(f"print({json.dumps(json.dumps(CLAUDE))})")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    cmd = f"{sys.executable} {script} {{prompt}} --output-format json"
    mid = store.send("michal", "codex", "ukol", kind="task")
    assert Bridge(store, "codex", cmd, lease_seconds=5).step()
    with store._connect() as con:
        row = dict(con.execute("SELECT * FROM task_usage WHERE message_id=?", (mid,)).fetchone())
    assert row["attempt"] == 1 and row["model"] == "claude-sonnet-5-5" and row["cost_usd"] > 0
    done = next(m for m in store.latest_messages() if m["text"].startswith(f"Done #{mid}"))
    assert done["text"].endswith("OK")                       # výsledek je `result`, ne JSON
    summary = store.usage_summary(recipient="codex")
    assert summary["attempts"] == 1 and summary["cache_creation_tokens"] == 32901


def test_invalid_usage_is_refused(store: Store):
    with pytest.raises(ValueError):
        store.record_usage(1, "codex", "t", {"source": "made-up"})
    with pytest.raises(ValueError):
        store.record_usage(1, "codex", "t", {"source": "claude-json", "input_tokens": -5})


def test_usage_needs_the_live_lease_and_only_grows(store: Store):
    """Review #15 F1: podvržený první zápis (nuly) nesmí zablokovat pravdivý zápis bridge."""
    mid = store.send("michal", "codex", "ukol", kind="task")
    d = store.lease_next("codex")
    forged = {"source": "codex-jsonl", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}
    real = {"source": "codex-jsonl", "input_tokens": 50000, "output_tokens": 100}
    assert store.record_usage(mid, "codex", "wrong-token", real) is False
    assert store.record_usage(mid, "codex", d.lease_token, forged) is True
    assert store.record_usage(mid, "codex", d.lease_token, real) is True
    assert store.record_usage(mid, "codex", d.lease_token, forged) is True   # nesníží
    assert store.record_usage(mid, "codex", d.lease_token, {"source": "unreported"})
    store.complete(mid, "codex", d.lease_token, "succeeded", "ok")
    assert store.record_usage(mid, "codex", d.lease_token, real) is False    # po konci lease
    with store._connect() as con:
        row = con.execute("SELECT * FROM task_usage").fetchone()
    assert (row["input_tokens"], row["output_tokens"], row["source"]) == (50000, 100, "codex-jsonl")


@pytest.mark.parametrize("result", [
    RunResult(True, "plain text"),                          # klient bez strojového výstupu
    RunResult(False, "timed out after 60 s"),               # timeout / zaseknutí
])
def test_every_attempt_leaves_a_record_even_without_numbers(store: Store, result):
    """Review #15 F2: proběhlý pokus musí být vidět i s neznámou spotřebou."""
    store.send("michal", "codex", "ukol", kind="task")
    assert Bridge(store, "codex", "unused {prompt}", runner=lambda _p: result,
                  read_only=True, lease_seconds=10).step()
    summary = store.usage_summary()
    assert summary["attempts"] == 1 and summary["cost_unknown"] == 1
    assert summary["input_tokens"] is None


def test_usage_methods_are_on_the_broker():
    assert {"record_usage", "usage_summary"} <= METHODS


@pytest.mark.parametrize("agent, flag", [("claude-code", "--model"), ("codex", "-m")])
def test_model_is_pinned_before_the_prompt(agent, flag):
    pinned = with_model(agent, DEFAULT_COMMANDS[agent], "some model")
    assert f"{flag} 'some model' {{prompt}}" in pinned
    assert with_model(agent, DEFAULT_COMMANDS[agent], "") == DEFAULT_COMMANDS[agent]
    assert with_model("kimi", "x {prompt}", "m") == "x {prompt}"
