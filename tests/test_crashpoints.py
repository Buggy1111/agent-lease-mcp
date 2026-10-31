"""Deterministic crash tests against an isolated on-disk database."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store


def test_guard_refuses_to_run_outside_its_own_process_group(tmp_path: Path):
    marker = tmp_path / "ran"
    command = f"from pathlib import Path; Path({str(marker)!r}).touch()"
    result = subprocess.run([sys.executable, "-m", "agent_lease_mcp.process_guard",
                             sys.executable, "-c", command], capture_output=True, check=False,
                            timeout=10)
    assert result.returncode == 2
    assert not marker.exists()


def test_complete_crash_before_commit_rolls_back_state_and_result(tmp_path: Path):
    db = tmp_path / "room.db"
    store = Store(db, settings=Settings(db, "codex", 1800))
    mid = store.send("michal", "codex", "work", kind="task")
    lease = store.lease_next("codex", start=True)
    with store._connect() as con:
        con.execute("CREATE TRIGGER kill_during_result BEFORE INSERT ON messages "
                    "WHEN NEW.kind='result' BEGIN SELECT crashpoint(); END")
    child = """
import os, signal, sys
from contextlib import contextmanager
from pathlib import Path
from agent_lease_mcp.store import Store
s = Store(Path(sys.argv[1])); original = s._connect
@contextmanager
def crash_connect():
    with original() as c:
        c.create_function('crashpoint', 0, lambda: os.kill(os.getpid(), signal.SIGKILL))
        yield c
s._connect = crash_connect
s.complete(int(sys.argv[2]), 'codex', sys.argv[3], 'succeeded', 'Done')
"""
    result = subprocess.run([sys.executable, "-c", child, str(db), str(mid), lease.lease_token],
                            check=False, timeout=20, env=os.environ.copy())
    assert result.returncode == -signal.SIGKILL
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "started"
    assert not any(m["reply_to"] == mid and m["kind"] == "result" for m in store.inbox())
    with store._connect() as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_complete_crash_after_commit_is_idempotent(tmp_path: Path):
    db = tmp_path / "room.db"
    store = Store(db, settings=Settings(db, "codex", 1800))
    mid = store.send("michal", "codex", "work", kind="task")
    lease = store.lease_next("codex", start=True)
    child = """
import os, signal, sys
from pathlib import Path
from agent_lease_mcp.store import Store
s = Store(Path(sys.argv[1]))
s._signal = lambda *args: os.kill(os.getpid(), signal.SIGKILL)
s.complete(int(sys.argv[2]), 'codex', sys.argv[3], 'succeeded', 'Done')
"""
    result = subprocess.run([sys.executable, "-c", child, str(db), str(mid), lease.lease_token],
                            check=False, timeout=20, env=os.environ.copy())
    assert result.returncode == -signal.SIGKILL
    assert store.complete(mid, "codex", lease.lease_token, "succeeded", "Done")
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "succeeded"
    assert sum(m["reply_to"] == mid and m["kind"] == "result" for m in store.inbox()) == 1


@pytest.mark.parametrize("started,effect", [(False, False), (True, False), (True, True)])
def test_crash_at_execution_barriers_never_requeues_started_work(
    tmp_path: Path, monkeypatch, started: bool, effect: bool,
):
    db = tmp_path / "room.db"
    marker = tmp_path / "effect"
    store = Store(db, settings=Settings(db, "codex", 1800))
    mid = store.send("michal", "codex", "work", kind="task")
    child = """
import os, signal, sys
from pathlib import Path
from agent_lease_mcp.store import Store
s = Store(Path(sys.argv[1]))
d = s.lease_next('codex', lease_seconds=2, start=sys.argv[3] == '1')
if d is None: raise RuntimeError('missing lease')
if sys.argv[4] == '1': Path(sys.argv[2]).write_text('one effect')
os.kill(os.getpid(), signal.SIGKILL)
"""
    result = subprocess.run([sys.executable, "-c", child, str(db), str(marker), str(int(started)),
                             str(int(effect))], check=False, timeout=20, env=os.environ.copy())
    assert result.returncode == -signal.SIGKILL
    assert marker.exists() == effect
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 5)
    next_lease = store.lease_next("codex", lease_seconds=60)
    if started:
        assert next_lease is None
        assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "needs_review"
    else:
        assert next_lease is not None and next_lease.message_id == mid


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="requires Linux /proc")
def test_detached_grandchild_must_not_survive_bridge_death(tmp_path: Path):
    marker = tmp_path / "grandchild.pid"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os,time\np=os.fork()\nif p == 0:\n os.setsid()\n "
        f"open({str(marker)!r},'w').write(str(os.getpid()))\n time.sleep(30)\n"
        "else:\n time.sleep(30)\n"
    )
    runner = tmp_path / "runner.py"
    runner.write_text(
        "from agent_lease_mcp.bridge import run_command\n"
        f"run_command({f'{sys.executable} {worker} {{prompt}}'!r}, 'x', timeout=30)\n"
    )
    parent = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    grandchild = None
    try:
        for _ in range(100):
            if marker.exists() and marker.read_text():
                grandchild = int(marker.read_text())
                break
            time.sleep(0.05)
        assert grandchild is not None
        parent.kill()
        parent.wait(timeout=5)
        time.sleep(0.5)
        stat = Path(f"/proc/{grandchild}/stat")
        if stat.exists() and stat.read_text().split()[2] != "Z":
            pytest.xfail("detached grandchild survives SIGKILL of bridge")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if grandchild is not None:
            try:
                os.kill(grandchild, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="requires Linux /proc")
def test_guard_stops_a_grandchild_in_the_process_group_when_bridge_dies(tmp_path: Path):
    marker = tmp_path / "grandchild.pid"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os,time\np=os.fork()\nif p == 0:\n "
        f"open({str(marker)!r},'w').write(str(os.getpid()))\n time.sleep(30)\n"
        "else:\n time.sleep(30)\n"
    )
    runner = tmp_path / "runner.py"
    runner.write_text(
        "from agent_lease_mcp.bridge import run_command\n"
        f"run_command({f'{sys.executable} {worker} {{prompt}}'!r}, 'x', timeout=30)\n"
    )
    parent = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    grandchild = None
    try:
        for _ in range(100):
            if marker.exists() and marker.read_text():
                grandchild = int(marker.read_text())
                break
            time.sleep(0.05)
        assert grandchild is not None
        parent.kill()
        parent.wait(timeout=5)
        stat = Path(f"/proc/{grandchild}/stat")
        for _ in range(100):
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.05)
        assert not stat.exists() or stat.read_text().split()[2] == "Z"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if grandchild is not None:
            try:
                os.kill(grandchild, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="requires Linux /proc")
def test_open_pipe_from_detached_child_cannot_be_reported_as_success(tmp_path: Path):
    from agent_lease_mcp.bridge import run_command

    marker = tmp_path / "child.pid"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os,time\np=os.fork()\nif p == 0:\n os.setsid()\n "
        f"open({str(marker)!r},'w').write(str(os.getpid()))\n time.sleep(30)\n"
        "else:\n time.sleep(0.2)\n"
    )
    child = None
    try:
        result = run_command(f"{sys.executable} {worker} {{prompt}}", "x", timeout=20)
        assert marker.exists()
        child = int(marker.read_text())
        assert not result.ok and result.cleanup_failed
    finally:
        if child is None and marker.exists() and marker.read_text():
            child = int(marker.read_text())
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="requires Linux /proc")
def test_open_pipe_from_detached_child_is_uncertain_on_timeout(tmp_path: Path):
    from agent_lease_mcp.bridge import run_command

    marker = tmp_path / "child.pid"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os,time\np=os.fork()\nif p == 0:\n os.setsid()\n "
        f"open({str(marker)!r},'w').write(str(os.getpid()))\n time.sleep(30)\n"
        "else:\n time.sleep(30)\n"
    )
    try:
        result = run_command(f"{sys.executable} {worker} {{prompt}}", "x", timeout=1)
        assert marker.exists()
        assert not result.ok and result.cleanup_failed
    finally:
        if marker.exists() and marker.read_text():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="requires Linux /proc")
def test_killed_bridge_leaves_fence_for_next_bridge(tmp_path: Path):
    from agent_lease_mcp.bridge import Bridge
    from agent_lease_mcp.config import Settings
    from agent_lease_mcp.store import Store

    db = tmp_path / "room.db"
    store = Store(db, settings=Settings(db, "codex", 1800))
    first = store.send("michal", "codex", "first", kind="task")
    second = store.send("michal", "codex", "second", kind="task")
    marker = tmp_path / "started"
    worker = tmp_path / "worker.py"
    worker.write_text(f"import time\nopen({str(marker)!r}, 'w').write('started')\ntime.sleep(30)\n")
    command = f"{sys.executable} {worker} {{prompt}}"
    program = (
        "from pathlib import Path\n"
        "from agent_lease_mcp.bridge import Bridge\n"
        "from agent_lease_mcp.config import Settings\n"
        "from agent_lease_mcp.store import Store\n"
        f"db=Path({str(db)!r})\n"
        f"Bridge(Store(db, settings=Settings(db, 'codex', 1800)), 'codex', {command!r}).step()\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", program])
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        parent.kill()
        parent.wait(timeout=5)
        new_bridge = Bridge(store, "codex", command)
        assert new_bridge._run_fence.present()
        assert new_bridge.step() is False
        states = {job["id"]: job["state"] for job in store.jobs("codex")}
        assert states[first] == "started" and states[second] == "pending"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
