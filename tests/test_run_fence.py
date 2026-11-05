from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from agent_lease_mcp.run_fence import RunFence


def test_only_one_concurrent_launcher_acquires_fence(tmp_path):
    barrier = Barrier(2)

    def attempt():
        fence = RunFence(tmp_path / "room.db", "codex")
        barrier.wait()
        try:
            fence.acquire(1)
        except FileExistsError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == [False, True]
    assert RunFence(tmp_path / "room.db", "codex").present()


def test_existing_symlink_blocks_acquire(tmp_path):
    fence = RunFence(tmp_path / "room.db", "codex")
    target = tmp_path / "target"
    target.write_text("unchanged")
    fence.path.symlink_to(target)
    try:
        fence.acquire(1)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing fence must block the launcher")
    assert target.read_text() == "unchanged"


def test_live_fence_owner_cannot_be_recovered(tmp_path, monkeypatch):
    import agent_lease_mcp.run_fence as rf

    owner = RunFence(tmp_path / "room.db", "codex")
    owner.acquire(123)
    monkeypatch.setattr(rf, "recover_cgroup", lambda _: (_ for _ in ()).throw(
        AssertionError("must not touch live owner's group")
    ))
    assert not RunFence(tmp_path / "room.db", "codex").try_recover()
    assert owner.present()
    owner.release()


def test_legacy_marker_is_not_automatically_cleared(tmp_path):
    fence = RunFence(tmp_path / "room.db", "codex")
    fence.path.write_text("Unverified command attempt for task 123.\n")
    assert not fence.try_recover()
    assert fence.present()


def test_corrupted_cgroup_record_is_not_recovered(tmp_path, monkeypatch):
    import json

    import agent_lease_mcp.run_fence as rf

    fence = RunFence(tmp_path / "room.db", "codex")
    fence.path.write_text(json.dumps({"record": {"version": 1, "name": "wrong"}, "sha256": "0" * 64}))
    monkeypatch.setattr(rf, "recover_cgroup", lambda _: (_ for _ in ()).throw(
        AssertionError("corrupt record must never trigger recovery")
    ))
    assert not fence.try_recover()
    assert fence.present()


def test_recovery_rejects_changed_cgroup_identity(tmp_path, monkeypatch):
    import agent_lease_mcp.cgroup as cg

    monkeypatch.setattr(cg, "current_cgroup", lambda: tmp_path)
    name = "agent-lease-attempt-" + "a" * 32
    group = tmp_path / name
    group.mkdir()
    control = group / "cgroup.kill"
    control.write_text("untouched")
    st = group.stat()
    record = {"version": 1, "root": str(tmp_path), "name": name,
              "dev": st.st_dev, "ino": st.st_ino + 1}
    assert not cg.recover_cgroup(record)
    assert control.read_text() == "untouched"
    record["root"] = str(tmp_path / "other-scope")
    record["ino"] = st.st_ino
    assert not cg.recover_cgroup(record)
    assert control.read_text() == "untouched"
    record["root"] = str(tmp_path)
    record["name"] = "../elsewhere"
    assert not cg.recover_cgroup(record)


# ── 8h chaos 5. 11.: pád mezi acquire a record_cgroup nesmí bridge zablokovat ──

def test_cgroup_pending_marker_of_dead_owner_is_cleared(tmp_path, monkeypatch):
    import agent_lease_mcp.run_fence as rf

    owner = RunFence(tmp_path / "room.db", "codex")
    owner.acquire(16832, cgroup=True)
    owner._file.close()          # owner killed before record_cgroup: nothing was spawned
    owner._file = None
    monkeypatch.setattr(rf, "recover_cgroup", lambda _: (_ for _ in ()).throw(
        AssertionError("no recorded group to recover")))
    fence = RunFence(tmp_path / "room.db", "codex")
    assert fence.try_recover()
    assert not fence.present()


def test_live_cgroup_pending_owner_is_not_cleared(tmp_path):
    owner = RunFence(tmp_path / "room.db", "codex")
    owner.acquire(1, cgroup=True)
    assert not RunFence(tmp_path / "room.db", "codex").try_recover()
    assert owner.present()
    owner.release()


def test_forged_or_non_cgroup_pending_marker_stays(tmp_path):
    import json

    fence = RunFence(tmp_path / "room.db", "codex")
    fence.path.write_text(json.dumps({"pending": {"version": 1, "mode": "cgroup"},
                                      "sha256": "0" * 64}))
    assert not fence.try_recover() and fence.present()
    fence.path.unlink()
    fence.path.write_bytes(__import__("agent_lease_mcp.run_fence", fromlist=["_sealed"])
                           ._sealed("pending", {"version": 1, "mode": "plain"}))
    assert not fence.try_recover() and fence.present()


def test_empty_marker_is_cleared_only_after_grace(tmp_path):
    import os

    fence = RunFence(tmp_path / "room.db", "codex")
    fence.path.write_bytes(b"")
    assert not fence.try_recover() and fence.present()       # možná živý acquire
    old = fence.path.stat().st_mtime - 120
    os.utime(fence.path, (old, old))
    assert fence.try_recover() and not fence.present()


def test_recovery_cannot_remove_marker_while_live_owner_is_before_flock(tmp_path, monkeypatch):
    """A slow creator still owns its empty marker, even beyond the age grace."""
    import os
    import time
    from threading import Event

    owner = RunFence(tmp_path / "room.db", "codex")
    created, resume = Event(), Event()
    real_open = os.open

    def pause_after_create(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == owner.path and flags & os.O_EXCL:
            created.set()
            if not resume.wait(5):
                os.close(fd)
                raise TimeoutError("test did not resume the creator")
        return fd

    monkeypatch.setattr(os, "open", pause_after_create)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner.acquire, 1, cgroup=True)
        try:
            assert created.wait(5)
            old = time.time() - 120
            os.utime(owner.path, (old, old))
            recovered = RunFence(tmp_path / "room.db", "codex").try_recover()
        finally:
            resume.set()
            future.result(timeout=5)
            if owner.present():
                owner.release()
            elif owner._file is not None:
                owner._file.close()
        assert not recovered


def test_recovery_after_creator_dies_before_flock_allows_next_launcher(tmp_path):
    import os
    import subprocess
    import sys
    import time

    code = """
import os
import sys
from pathlib import Path
from agent_lease_mcp.run_fence import RunFence
real_open = os.open
def die_after_create(path, flags, *args, **kwargs):
    fd = real_open(path, flags, *args, **kwargs)
    if flags & os.O_EXCL:
        os._exit(23)
    return fd
os.open = die_after_create
RunFence(Path(sys.argv[1]), 'codex').acquire(1, cgroup=True)
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "room.db")],
                            check=False, timeout=5)
    assert result.returncode == 23
    fence = RunFence(tmp_path / "room.db", "codex")
    assert fence.path.read_bytes() == b""
    old = time.time() - 120
    os.utime(fence.path, (old, old))
    assert fence.try_recover()
    fence.acquire(2, cgroup=True)
    fence.release()


def test_record_replaces_pending_marker_completely(tmp_path, monkeypatch):
    import json

    import agent_lease_mcp.cgroup as cg

    monkeypatch.setattr(cg, "current_cgroup", lambda: tmp_path)
    group_dir = tmp_path / ("agent-lease-attempt-" + "b" * 32)
    group_dir.mkdir()
    owner = RunFence(tmp_path / "room.db", "codex")
    owner.acquire(7, cgroup=True)
    owner.record_cgroup(type("G", (), {"path": group_dir})())
    data = json.loads(owner.path.read_bytes())
    assert "record" in data and "pending" not in data
    owner.release()
