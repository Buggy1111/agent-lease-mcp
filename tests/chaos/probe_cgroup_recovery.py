"""Exercise restart recovery after killing both bridge and its guard in a test scope."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from probe_cgroup_watchdog import kill_group_member

from agent_lease_mcp.bridge import Bridge
from agent_lease_mcp.cgroup import current_cgroup
from agent_lease_mcp.config import Settings
from agent_lease_mcp.run_fence import RunFence
from agent_lease_mcp.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kill", choices=("guard", "watchdog", "both", "watchdog_only"),
                        default="guard")
    args = parser.parse_args()
    root = current_cgroup()
    if not os.access(root, os.W_OK):
        print("No delegated cgroup; no test started.", file=sys.stderr)
        return 2
    before = set(root.glob("agent-lease-attempt-*"))
    parent = None
    try:
        with tempfile.TemporaryDirectory(prefix="lease-recovery-") as tmp:
            temp = Path(tmp)
            db, marker, worker = temp / "room.db", temp / "worker.pid", temp / "worker.py"
            store = Store(db, settings=Settings(db, "codex", 1800))
            task = store.send("michal", "codex", "synthetic test", kind="task")
            worker.write_text(
                "import os,time\np=os.fork()\nif p == 0:\n os.setsid()\n"
                " fd=os.open('/dev/null', os.O_WRONLY)\n os.dup2(fd,1)\n os.dup2(fd,2)\n"
                f" open({str(marker)!r},'w').write(str(os.getpid()))\n time.sleep(60)\n"
                "else:\n time.sleep(60)\n"
            )
            command = f"{sys.executable} {worker} {{prompt}}"
            program = (
                "from pathlib import Path\nfrom agent_lease_mcp.bridge import Bridge\n"
                "from agent_lease_mcp.config import Settings\nfrom agent_lease_mcp.store import Store\n"
                f"db=Path({str(db)!r})\n"
                f"Bridge(Store(db, settings=Settings(db, 'codex', 1800)), 'codex', {command!r}, "
                "use_cgroup=True).step()\n"
            )
            parent = subprocess.Popen([sys.executable, "-c", program])
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not marker.exists():
                raise RuntimeError("synthetic worker did not start")
            fence = RunFence(db, "codex")
            record = json.loads(fence.path.read_text())["record"]
            group = root / record["name"]
            if record["root"] != str(root) or group not in set(root.glob("agent-lease-attempt-*")) - before:
                raise RuntimeError("recorded group is outside this test")
            # A live owner must prevent recovery and leave its worker untouched.
            if fence.try_recover():
                raise RuntimeError("recovered a live bridge's cgroup")
            children = set(Path(f"/proc/{parent.pid}/task/{parent.pid}/children").read_text().split())
            guard = children & set((group / "cgroup.procs").read_text().split())
            watch = children - guard
            if len(guard) != 1 or len(watch) != 1:
                raise RuntimeError("cannot identify the synthetic guard and watchdog")
            if args.kill == "watchdog_only":
                kill_group_member(int(next(iter(watch))), root)
                parent.wait(timeout=15)
                state = next(j for j in store.jobs("codex") if j["id"] == task)["state"]
                if state != "needs_review" or not fence.present() or group.exists():
                    raise RuntimeError("bridge did not fail closed after watchdog death")
                print("watchdog_only: bridge stopped the worker and held the task for review")
                return 0
            parent.send_signal(signal.SIGSTOP)
            _, status = os.waitpid(parent.pid, os.WUNTRACED)
            if not os.WIFSTOPPED(status):
                raise RuntimeError("synthetic bridge did not stop")
            if args.kill in ("guard", "both"):
                kill_group_member(int(next(iter(guard))), group)
            if args.kill in ("watchdog", "both"):
                kill_group_member(int(next(iter(watch))), root)
            parent.kill()
            parent.wait(timeout=5)
            if args.kill == "both":
                if "populated 1" not in (group / "cgroup.events").read_text():
                    raise RuntimeError("test did not leave an orphaned worker")
            else:
                deadline = time.monotonic() + 5
                while "populated 1" in (group / "cgroup.events").read_text():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("remaining guard did not stop the worker")
                    time.sleep(0.05)
            restarted = Bridge(store, "codex", command, use_cgroup=True)
            if restarted.step() or fence.present() or group.exists():
                raise RuntimeError("restart did not safely recover the stale group")
            if next(j for j in store.jobs("codex") if j["id"] == task)["state"] != "started":
                raise RuntimeError("recovery changed the uncertain task state")
            print(f"{args.kill}: cleanup verified; restart kept its task uncertain")
            return 0
    finally:
        if parent is not None and parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        for group in set(root.glob("agent-lease-attempt-*")) - before:
            (group / "cgroup.kill").write_text("1")
            deadline = time.monotonic() + 5
            while "populated 1" in (group / "cgroup.events").read_text():
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"test group remains populated: {group}")
                time.sleep(0.05)
            group.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
