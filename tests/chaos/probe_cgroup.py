"""Manual Linux cgroup-v2 probe; run only inside an approved delegated test service.

Creates one temporary child cgroup, runs synthetic workers, and checks that
`cgroup.kill` also stops a grandchild which called setsid(). Never targets the
live broker, bridge, or room database.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def own_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise RuntimeError("cgroup v2 is unavailable")


def populated(group: Path) -> bool:
    fields = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
    return fields.get("populated") != "0"


def main() -> int:
    parent = own_cgroup()
    if not os.access(parent, os.W_OK):
        print(f"No delegated writable cgroup at {parent}; no test started.", file=sys.stderr)
        return 2
    group = parent / f"agent-lease-probe-{os.getpid()}"
    group.mkdir(mode=0o700)  # never use an existing group
    child = None
    try:
        for name, permission in (("cgroup.procs", os.W_OK), ("cgroup.kill", os.W_OK),
                                 ("cgroup.events", os.R_OK)):
            if not os.access(group / name, permission):
                raise RuntimeError(f"required delegated cgroup control is unavailable: {name}")
        with tempfile.TemporaryDirectory(prefix="lease-cgroup-probe-") as tmp:
            marker = Path(tmp) / "grandchild.pid"
            worker = (
                "import os,sys,time\n"
                "pid=os.fork()\n"
                "if pid == 0:\n"
                " os.setsid()\n"
                " open(sys.argv[1],'w').write(str(os.getpid()))\n"
                " time.sleep(30)\n"
                "else:\n time.sleep(30)\n"
            )

            def enter_group() -> None:
                (group / "cgroup.procs").write_text(str(os.getpid()))

            child = subprocess.Popen([sys.executable, "-c", worker, str(marker)],
                                     preexec_fn=enter_group)  # noqa: PLW1509
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not marker.exists():
                raise RuntimeError("synthetic grandchild did not start")
            grandchild = int(marker.read_text())
            (group / "cgroup.kill").write_text("1")
            child.wait(timeout=5)
            deadline = time.monotonic() + 5
            while populated(group) and time.monotonic() < deadline:
                time.sleep(0.05)
            if populated(group):
                raise RuntimeError("cgroup remains populated after cgroup.kill")
            stat = Path(f"/proc/{grandchild}/stat")
            if stat.exists() and stat.read_text().rpartition(") ")[2].split()[0] != "Z":
                raise RuntimeError("detached grandchild survived cgroup.kill")
            print("cgroup.kill stopped the synthetic worker and detached grandchild")
            return 0
    finally:
        if child is not None:
            if populated(group):
                (group / "cgroup.kill").write_text("1")
            if child.poll() is None:
                child.wait(timeout=5)
            deadline = time.monotonic() + 5
            while populated(group) and time.monotonic() < deadline:
                time.sleep(0.05)
            if populated(group):
                raise RuntimeError(f"test group still populated: {group}")
        group.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
