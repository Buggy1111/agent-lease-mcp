"""Test an independent synthetic cgroup watchdog after parent and guard die."""
from __future__ import annotations

import argparse
import ctypes
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from probe_cgroup import own_cgroup, populated

WORKER = """
import os,sys,time
from pathlib import Path
os.setsid(); Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)
"""

GUARD = """
import ctypes,os,signal,subprocess,sys
from pathlib import Path
group=Path(sys.argv[1]); marker=Path(sys.argv[2]); worker=sys.argv[3]; guard_marker=Path(sys.argv[4]); parent=int(sys.argv[5])
libc=ctypes.CDLL('libc.so.6',use_errno=True)
if libc.prctl(1,int(signal.SIGTERM)) != 0 or os.getppid() != parent: os._exit(125)
def stop(*_):
    (group/'cgroup.kill').write_text('1')
    os._exit(126)
signal.signal(signal.SIGTERM,stop)
(group/'cgroup.procs').write_text(str(os.getpid()))
guard_marker.write_text(str(os.getpid()))
child=subprocess.Popen([sys.executable,'-c',worker,str(marker)])
child.wait()
"""

WATCHDOG = """
import os,sys
from pathlib import Path
read_fd=int(sys.argv[1]); group=Path(sys.argv[2]); marker=Path(sys.argv[3])
marker.write_text(str(os.getpid()))
while os.read(read_fd,1): pass
(group/'cgroup.kill').write_text('1')
"""

STARTER = """
import os,subprocess,sys
guard,watchdog,worker,group,marker,guard_marker,watch_marker=sys.argv[1:]
r,w=os.pipe()
watch=subprocess.Popen([sys.executable,'-c',watchdog,str(r),group,watch_marker],pass_fds=(r,))
os.close(r)
child=subprocess.Popen([sys.executable,'-c',guard,group,marker,worker,guard_marker,str(os.getpid())])
child.wait(); os.close(w); watch.wait()
"""


def kill_group_member(pid: int, group: Path) -> None:
    """Use a pidfd so a recycled numeric PID can never target another process."""
    if platform.machine() not in ("x86_64", "aarch64"):
        raise RuntimeError("pidfd syscall numbers are not known for this architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(434, pid, 0)  # pidfd_open
    if fd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    try:
        if str(pid) not in (group / "cgroup.procs").read_text().split():
            raise RuntimeError("synthetic guard is no longer in the test group")
        if libc.syscall(424, fd, int(signal.SIGKILL), 0, 0) != 0:  # pidfd_send_signal
            raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kill", choices=("guard", "watchdog", "both"), default="guard")
    args = parser.parse_args()
    parent = own_cgroup()
    if not os.access(parent, os.W_OK):
        print("No delegated writable cgroup; no test started.", file=sys.stderr)
        return 2
    group = parent / f"agent-lease-watchdog-{os.getpid()}"
    group.mkdir(mode=0o700)
    starter = None
    try:
        for name, permission in (("cgroup.procs", os.W_OK), ("cgroup.kill", os.W_OK),
                                 ("cgroup.events", os.R_OK)):
            if not os.access(group / name, permission):
                raise RuntimeError(f"required delegated cgroup control is unavailable: {name}")
        with tempfile.TemporaryDirectory(prefix="lease-cgroup-watchdog-") as tmp:
            root = Path(tmp)
            marker, guard_marker, watch_marker = (root / name for name in ("worker", "guard", "watch"))
            starter = subprocess.Popen([sys.executable, "-c", STARTER, GUARD, WATCHDOG, WORKER,
                                        str(group), str(marker), str(guard_marker), str(watch_marker)])
            deadline = time.monotonic() + 10
            while not all(path.exists() for path in (marker, guard_marker, watch_marker)):
                if time.monotonic() >= deadline:
                    raise RuntimeError("synthetic worker, guard, or watchdog did not start")
                time.sleep(0.05)
            # Freeze the parent so it cannot reap the guard and exit on its own.
            starter.send_signal(signal.SIGSTOP)
            _, status = os.waitpid(starter.pid, os.WUNTRACED)
            if not os.WIFSTOPPED(status):
                raise RuntimeError("synthetic parent did not stop as expected")
            if args.kill in ("guard", "both"):
                kill_group_member(int(guard_marker.read_text()), group)
            if args.kill in ("watchdog", "both"):
                kill_group_member(int(watch_marker.read_text()), parent)
            starter.kill()
            starter.wait(timeout=5)
            if args.kill == "both":
                if not populated(group):
                    raise RuntimeError("test did not leave a worker to demonstrate the limitation")
                print("worker survives if both independent guards and parent are killed")
                return 0  # finally cleans the synthetic cgroup
            deadline = time.monotonic() + 5
            while populated(group) and time.monotonic() < deadline:
                time.sleep(0.05)
            if populated(group):
                raise RuntimeError(f"cleanup failed after parent and {args.kill} died")
            print(f"cgroup cleaned up after both parent and {args.kill} were killed")
            return 0
    finally:
        if starter is not None:
            if populated(group):
                (group / "cgroup.kill").write_text("1")
            if starter.poll() is None:
                starter.kill()
                starter.wait(timeout=5)
            deadline = time.monotonic() + 5
            while populated(group) and time.monotonic() < deadline:
                time.sleep(0.05)
            if populated(group):
                raise RuntimeError(f"test group still populated: {group}")
        group.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
