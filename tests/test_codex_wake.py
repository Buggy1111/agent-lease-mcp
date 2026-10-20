from __future__ import annotations

import os
import stat
from pathlib import Path

from agent_lease_mcp.codex_wake import CodexWake, main
from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store


def _store(tmp_path: Path) -> Store:
    db = tmp_path / "room.db"
    return Store(db, settings=Settings(db, "codex-wake", 1800))


def _fake_codex(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "codex-args"
    executable = bindir / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" >> {log}\n"
        f"exit {exit_code}\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    return bindir, log


def test_first_start_baselines_existing_messages(tmp_path: Path, monkeypatch):
    store = _store(tmp_path)
    store.send("claude-code", "codex", "old", kind="chat")
    bindir, log = _fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    wake = CodexWake(store, "nox", tmp_path / "cursor")
    assert wake.step() is False
    assert not log.exists()
    assert (tmp_path / "cursor").read_text().strip() == "1"


def test_chat_and_real_result_are_batched_but_tasks_and_receipts_are_skipped(
    tmp_path: Path, monkeypatch,
):
    store = _store(tmp_path)
    bindir, log = _fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    wake = CodexWake(store, "nox", tmp_path / "cursor")
    wake.step()

    chat = store.send("claude-code", "codex", "ping", kind="chat")
    task = store.send("claude-code", "codex", "headless work", kind="task")
    receipt = store.send("broker", "codex", "✓ claude-code saw #1", kind="result")
    result = store.send("claude-code", "codex", "review hotov", kind="result")

    assert wake.step() is True
    args = log.read_text()
    assert "queue\n--thread\nnox\n--message\n" in args
    assert f"#{chat}" in args and f"#{result}" in args
    assert f"#{task}" not in args and f"#{receipt}" not in args
    assert int((tmp_path / "cursor").read_text()) == result


def test_inactive_session_does_not_advance_cursor_or_spam_error(tmp_path: Path, monkeypatch, capsys):
    store = _store(tmp_path)
    bindir, log = _fake_codex(tmp_path, exit_code=1)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    cursor = tmp_path / "cursor"
    wake = CodexWake(store, "missing", cursor)
    wake.step()
    store.send("claude-code", "codex", "ping", kind="chat")

    assert wake.step() is False
    first_cursor = cursor.read_text()
    assert wake.step() is False
    assert cursor.read_text() == first_cursor == "0\n"
    assert capsys.readouterr().err.count("not active") == 1
    assert log.read_text().count("queue\n") == 1


def test_receipts_and_tasks_advance_without_calling_codex(tmp_path: Path, monkeypatch):
    store = _store(tmp_path)
    bindir, log = _fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    cursor = tmp_path / "cursor"
    wake = CodexWake(store, "nox", cursor)
    wake.step()
    store.send("claude-code", "codex", "work", kind="task")
    last = store.send("broker", "codex", "✓ claude-code saw #1", kind="result")

    assert wake.step() is False
    assert int(cursor.read_text()) == last
    assert not log.exists()


def test_daemon_never_heartbeats_as_codex_wake(tmp_path: Path, monkeypatch):
    store = _store(tmp_path)
    bindir, _ = _fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    wake = CodexWake(store, "nox", tmp_path / "cursor")
    wake.step()
    store.send("claude-code", "codex", "ping", kind="chat")
    wake.step()
    assert store.peers() == []


def test_main_requires_named_thread(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_LEASE_CODEX_THREAD", raising=False)
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    assert main(["--once"]) == 2
    assert "/rename" in capsys.readouterr().err
