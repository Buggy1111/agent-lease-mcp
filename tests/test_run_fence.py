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
