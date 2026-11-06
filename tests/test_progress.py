"""B5: dlouhý, ale aktivní příkaz není zaseknutý; nečinný se ukončí; strop platí vždy."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from agent_lease_mcp import bridge as br
from agent_lease_mcp.bridge import Bridge, RunResult, run_command
from agent_lease_mcp.store import Store


def script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "cmd.py"
    path.write_text("import sys, time\n" + body)
    return f"{sys.executable} {path} {{prompt}}"


@pytest.fixture(autouse=True)
def fast_sampling(monkeypatch):
    monkeypatch.setattr(br, "PROGRESS_SAMPLE_SECONDS", 0.2)


def test_active_command_outlives_the_idle_limit(tmp_path):
    cmd = script(tmp_path, "for i in range(12):\n    print(i, flush=True)\n    time.sleep(0.25)\nprint('done')\n")
    started = time.monotonic()
    result = run_command(cmd, "x", timeout=10, idle_timeout=1.0)
    assert result.ok and result.output.endswith("done")
    assert time.monotonic() - started > 2.5  # běžel déle než limit nečinnosti


def test_silent_command_is_stopped_as_stalled(tmp_path):
    cmd = script(tmp_path, "time.sleep(20)\n")
    started = time.monotonic()
    result = run_command(cmd, "x", timeout=30, idle_timeout=1.0)
    assert not result.ok and result.output.startswith("stalled: no progress for 1 s")
    assert time.monotonic() - started < 5


def test_absolute_ceiling_stops_even_an_active_command(tmp_path):
    cmd = script(tmp_path, "while True:\n    print('x', flush=True)\n    time.sleep(0.1)\n")
    result = run_command(cmd, "x", timeout=1.5, idle_timeout=1.0)
    assert not result.ok and result.output == "timed out after 1 s"


def test_external_progress_keeps_a_silent_command_alive(tmp_path):
    cmd = script(tmp_path, "time.sleep(2.5)\nprint('ok')\n")
    result = run_command(cmd, "x", timeout=10, idle_timeout=1.0, progress=lambda: True)
    assert result.ok and result.output == "ok"


def test_without_idle_timeout_behaviour_is_unchanged(tmp_path):
    cmd = script(tmp_path, "time.sleep(1.5)\nprint('ok')\n")
    assert run_command(cmd, "x", timeout=10).output == "ok"  # žádný limit nečinnosti


def test_bridge_uses_timeout_as_idle_limit_only_with_max_runtime(tmp_path, monkeypatch):
    store = Store(tmp_path / "room.db")
    seen = []

    def fake_run(template, prompt, **kw):
        seen.append((kw["timeout"], kw["idle_timeout"]))
        return RunResult(True, "ok")

    monkeypatch.setattr(br, "run_command", fake_run)
    for max_runtime in (None, 3600):
        store.send("michal", "codex", f"task {max_runtime}", kind="task")
        Bridge(store, "codex", "echo {prompt}", timeout=600, max_runtime=max_runtime,
               interactive_grace=0, after_message_id=0).step()
    assert seen == [(600, None), (3600, 600)]
    assert Bridge(store, "codex", "echo", timeout=900, max_runtime=60).max_runtime == 900


def test_checkpoint_progress_counts_only_new_checkpoints(tmp_path):
    store = Store(tmp_path / "room.db")
    mid = store.send("michal", "codex", "work", kind="task")
    d = store.lease_next("codex", lease_seconds=600, start=True)
    store.checkpoint(mid, "codex", d.lease_token, "starý krok")
    changed = Bridge(store, "codex", "echo")._checkpoint_progress(mid)
    assert changed() is False  # checkpoint z doby před během se nepočítá
    time.sleep(0.01)
    store.checkpoint(mid, "codex", d.lease_token, "nový krok")
    assert changed() is True and changed() is False


def test_cgroup_cpu_reader(tmp_path):
    class Group:
        path = tmp_path

    (tmp_path / "cpu.stat").write_text("usage_usec 1234\nuser_usec 1000\nsystem_usec 234\n")
    assert br._cgroup_cpu_usec(Group()) == 1234
    assert br._cgroup_cpu_usec(None) is None
    (tmp_path / "cpu.stat").write_text("garbage")
    assert br._cgroup_cpu_usec(Group()) is None


def test_cpu_use_counts_as_progress(tmp_path, monkeypatch):
    usage = iter(range(0, 10**9, 2_000_000))  # každý vzorek +2 s CPU
    monkeypatch.setattr(br, "_cgroup_cpu_usec", lambda group: next(usage))
    cmd = script(tmp_path, "time.sleep(2.5)\nprint('ok')\n")
    assert run_command(cmd, "x", timeout=10, idle_timeout=1.0).output == "ok"
    monkeypatch.setattr(br, "_cgroup_cpu_usec", lambda group: 5)  # žádný přírůstek
    assert run_command(cmd, "x", timeout=10, idle_timeout=1.0).output.startswith("stalled")
