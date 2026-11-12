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
    store.complete(mid, "codex", d.lease_token, "succeeded", "ok")
    assert store.record_usage(mid, "codex", d.lease_token, real) is False    # po konci lease
    with store._connect() as con:
        row = con.execute("SELECT * FROM task_usage").fetchone()
    assert (row["input_tokens"], row["output_tokens"]) == (50000, 100)
    assert row["cost_usd"] is None             # podvržená cena 0 nepřebije neznámou (Nox)


def test_forged_numbers_cannot_hide_an_unreported_attempt(store: Store):
    mid = store.send("michal", "codex", "ukol", kind="task")
    d = store.lease_next("codex")
    assert store.record_usage(mid, "codex", d.lease_token, {"source": "unreported"})
    assert store.record_usage(mid, "codex", d.lease_token,
                              {"source": "codex-jsonl", "input_tokens": 0, "output_tokens": 0})
    assert store.record_usage(mid, "codex", d.lease_token, {"source": "unreported"})
    assert store.usage_summary()["input_tokens"] is None


def test_cancelled_attempt_still_leaves_a_record(store: Store):
    """Nox: zrušení během běhu smaže lease token, záznam ale vznikl už před startem."""
    mid = store.send("michal", "codex", "ukol", kind="task")

    def run(_prompt):
        store.cancel(mid, "codex")
        return RunResult(False, "stopped: the task lease was lost or cancelled", aborted=True)
    assert Bridge(store, "codex", "unused {prompt}", runner=run, read_only=True,
                  lease_seconds=10).step()
    assert store.usage_summary()["attempts"] == 1


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


# ── C3b: stropy ──────────────────────────────────────────────────────────────

def capped(tmp_path: Path, **caps) -> Store:
    return Store(tmp_path / "room.db",
                 settings=Settings(tmp_path / "room.db", "codex", 1800, **caps))


def use(store: Store, mid: int, token: str, tokens: int, usd: float | None = None):
    assert store.record_usage(mid, "codex", token, {"source": "claude-json",
                                                    "input_tokens": tokens, "cost_usd": usd})


def test_task_over_its_token_cap_is_not_started_again(tmp_path: Path):
    store = capped(tmp_path, task_budget_tokens=1000)
    mid = store.send("michal", "codex", "ukol", kind="task")
    d = store.lease_next("codex")
    use(store, mid, d.lease_token, 1200)
    assert store.complete(mid, "codex", d.lease_token, "needs_review", "neprošlo")
    assert store.retry(mid, "codex")                     # člověk vrátí do fronty
    assert store.lease_next("codex") is None             # strop: další pokus ne
    job = next(j for j in store.jobs("codex") if j["id"] == mid)
    assert job["state"] == "needs_review" and job["last_error"].startswith("budget: task used 1200")


def test_daily_cap_stops_new_starts_but_keeps_tasks_queued(tmp_path: Path):
    store = capped(tmp_path, daily_budget_usd=1.0)
    first = store.send("michal", "codex", "prvni", kind="task")
    d = store.lease_next("codex")
    use(store, first, d.lease_token, 10, usd=1.5)
    store.complete(first, "codex", d.lease_token, "succeeded", "hotovo")
    second = store.send("michal", "codex", "druhy", kind="task")
    assert store.lease_next("codex") is None
    assert next(j for j in store.jobs("codex") if j["id"] == second)["state"] == "pending"
    with store._connect() as con:                        # starší než 24 h se nepočítá
        con.execute("UPDATE task_usage SET at = at - 90000")
    assert store.lease_next("codex").message_id == second


def test_no_caps_and_unknown_usage_never_block(tmp_path: Path):
    store = capped(tmp_path)
    mid = store.send("michal", "codex", "ukol", kind="task")
    d = store.lease_next("codex")
    use(store, mid, d.lease_token, 10**9, usd=10**5)
    store.complete(mid, "codex", d.lease_token, "needs_review", "x")
    assert store.retry(mid, "codex")
    assert store.lease_next("codex").message_id == mid
    capped_store = capped(tmp_path / "b", daily_budget_usd=1.0)
    other = capped_store.send("michal", "codex", "ukol", kind="task")
    d = capped_store.lease_next("codex")
    use(capped_store, other, d.lease_token, 5)              # cena neznámá → do USD stropu se nepočítá
    capped_store.complete(other, "codex", d.lease_token, "succeeded", "ok")
    nxt = capped_store.send("michal", "codex", "dalsi", kind="task")
    assert capped_store.lease_next("codex").message_id == nxt


def test_budget_env_is_read_by_the_broker_process(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.setenv("AGENT_LEASE_TASK_BUDGET_TOKENS", "5000")
    monkeypatch.setenv("AGENT_LEASE_DAILY_BUDGET_USD", "2.5")
    monkeypatch.setenv("AGENT_LEASE_DAILY_BUDGET_TOKENS", "-3")      # neplatné → bez stropu
    s = Settings.from_env()
    assert (s.task_budget_tokens, s.daily_budget_usd, s.daily_budget_tokens) == (5000, 2.5, 0)


def test_cap_reached_after_lease_blocks_the_start(tmp_path: Path):
    """Úkol převzatý před dosažením stropu se už nespustí (repro Noxe k #15/#16)."""
    store = capped(tmp_path, daily_budget_tokens=100)
    first = store.send("michal", "codex", "prvni", kind="task")
    d = store.lease_next("codex")
    store.send("michal", "codex", "druhy", kind="task")
    e = store.lease_next("codex")
    use(store, first, d.lease_token, 120)
    store.complete(first, "codex", d.lease_token, "succeeded", "ok")
    assert not store.ack(e.message_id, "codex", "started", lease_token=e.lease_token)
