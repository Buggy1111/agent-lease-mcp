"""Verify parent death cleanup even if a synthetic child keeps the pipe open."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_lease_mcp.cgroup import current_cgroup


def main() -> int:
    root = current_cgroup()
    if not os.access(root, os.W_OK):
        print("No delegated cgroup; no test started.", file=sys.stderr)
        return 2
    before = set(root.glob("agent-lease-attempt-*"))
    with tempfile.TemporaryDirectory(prefix="lease-watchdog-pipe-") as tmp:
        path = Path(tmp)
        group_name, worker_ready, leaker_ready, done = (path / x for x in
                                                       ("group", "worker", "leaker", "done"))
        leaker_exit = path / "leaker-exit"
        worker_code = (f"from pathlib import Path; import time; Path({str(worker_ready)!r}).touch(); "
                       "time.sleep(60)")
        leaker_code = (
            f"from pathlib import Path; import time; Path({str(leaker_ready)!r}).touch(); "
            f"p=Path({str(done)!r}); end=time.monotonic()+5; "
            "exec(\"while not p.exists() and time.monotonic()<end: time.sleep(0.05)\"); "
            f"Path({str(leaker_exit)!r}).touch()"
        )
        program = (
            "import os,subprocess,sys,time\nfrom pathlib import Path\n"
            "from agent_lease_mcp.cgroup import CommandCgroup,CgroupWatchdog\n"
            "group=CommandCgroup(); watchdog=CgroupWatchdog(group)\n"
            f"Path({str(group_name)!r}).write_text(group.path.name)\n"
            "def enter(): os.write(group.procs_fd,b'0')\n"
            f"worker={worker_code!r}\n"
            "subprocess.Popen([sys.executable,'-c',worker],pass_fds=(group.procs_fd,),preexec_fn=enter)\n"
            f"leaker={leaker_code!r}\n"
            "subprocess.Popen([sys.executable,'-c',leaker],pass_fds=(watchdog.write_fd,))\n"
            "time.sleep(60)\n"
        )
        parent = subprocess.Popen([sys.executable, "-c", program])
        try:
            deadline = time.monotonic() + 10
            while not all(p.exists() for p in (group_name, worker_ready, leaker_ready)):
                if parent.poll() is not None:
                    raise RuntimeError("synthetic parent exited before startup")
                if time.monotonic() >= deadline:
                    raise RuntimeError("synthetic worker and pipe holder did not start")
                time.sleep(0.05)
            group = root / group_name.read_text()
            if group not in set(root.glob("agent-lease-attempt-*")) - before:
                raise RuntimeError("group is outside this test")
            parent.kill()
            parent.wait(timeout=5)
            deadline = time.monotonic() + 3
            while "populated 1" in (group / "cgroup.events").read_text():
                if time.monotonic() >= deadline:
                    raise RuntimeError("parent death did not stop worker with the pipe held open")
                time.sleep(0.05)
            if done.exists() or leaker_exit.exists():
                raise RuntimeError("pipe holder was released before cleanup")
            print("parent-death signal cleaned the group while another process held the pipe")
            return 0
        finally:
            done.touch()
            if parent.poll() is None:
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
            deadline = time.monotonic() + 6
            while leaker_ready.exists() and not leaker_exit.exists() and time.monotonic() < deadline:
                time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
