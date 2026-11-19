"""Synthetic cgroup guard experiment; run only in an approved delegated scope."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from probe_cgroup import own_cgroup, populated

WORKER = """
import os,sys,time
from pathlib import Path
os.setsid()
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
"""

GUARD = """
import ctypes,os,signal,subprocess,sys
from pathlib import Path
parent=int(sys.argv[1]); group=Path(sys.argv[2]); marker=sys.argv[3]; worker=sys.argv[4]
libc=ctypes.CDLL('libc.so.6',use_errno=True)
if libc.prctl(1,int(signal.SIGTERM)) != 0 or os.getppid() != parent: os._exit(125)
def stop(*_):
    (group/'cgroup.kill').write_text('1')
    os._exit(126)
signal.signal(signal.SIGTERM,stop)
(group/'cgroup.procs').write_text(str(os.getpid()))
child=subprocess.Popen([sys.executable,'-c',worker,marker])
child.wait()
"""

STARTER = """
import os,subprocess,sys
guard,worker,group,marker=sys.argv[1:]
child=subprocess.Popen([sys.executable,'-c',guard,str(os.getpid()),group,marker,worker])
child.wait()
"""


def main() -> int:
    parent = own_cgroup()
    if not os.access(parent, os.W_OK):
        print("No delegated writable cgroup; no test started.", file=sys.stderr)
        return 2
    group = parent / f"agent-lease-parent-death-{os.getpid()}"
    group.mkdir(mode=0o700)
    starter = None
    try:
        for name, permission in (("cgroup.procs", os.W_OK), ("cgroup.kill", os.W_OK),
                                 ("cgroup.events", os.R_OK)):
            if not os.access(group / name, permission):
                raise RuntimeError(f"required delegated cgroup control is unavailable: {name}")
        with tempfile.TemporaryDirectory(prefix="lease-cgroup-parent-") as tmp:
            marker = Path(tmp) / "detached.pid"
            starter = subprocess.Popen([sys.executable, "-c", STARTER, GUARD, WORKER,
                                        str(group), str(marker)])
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not marker.exists():
                raise RuntimeError("detached worker did not start")
            starter.kill()
            starter.wait(timeout=5)
            deadline = time.monotonic() + 5
            while populated(group) and time.monotonic() < deadline:
                time.sleep(0.05)
            if populated(group):
                raise RuntimeError("guard did not clean up after parent death")
            print("guard cleaned up detached worker after its parent was killed")
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
