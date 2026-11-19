"""Exercise real bridge containment with synthetic workers in a delegated scope."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_lease_mcp.bridge import run_command
from agent_lease_mcp.cgroup import current_cgroup


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[0] != "Z"
    except FileNotFoundError:
        return False


def run_probe() -> int:
    root = current_cgroup()
    if not os.access(root, os.W_OK):
        print("No delegated cgroup; no synthetic worker started.", file=sys.stderr)
        return 2
    before = set(root.glob("agent-lease-attempt-*"))
    with tempfile.TemporaryDirectory(prefix="lease-cgroup-bridge-") as tmp:
        marker = Path(tmp) / "child.pid"
        worker = Path(tmp) / "worker.py"
        worker.write_text(
            "import os,time\np=os.fork()\nif p == 0:\n os.setsid()\n"
            " fd=os.open('/dev/null', os.O_WRONLY)\n os.dup2(fd,1)\n os.dup2(fd,2)\n"
            f" open({str(marker)!r},'w').write(str(os.getpid()))\n time.sleep(30)\n"
            "else:\n time.sleep(0.5)\n"
        )
        result = run_command(f"{sys.executable} {worker} {{prompt}}", "x", timeout=15,
                             use_cgroup=True)
        if not marker.exists() or not result.ok or result.cleanup_failed:
            raise RuntimeError(f"contained command failed: {result}")
        pid = int(marker.read_text())
        if alive(pid):
            raise RuntimeError("detached synthetic worker survived normal cleanup")
        print("real bridge cleaned detached worker after normal completion", flush=True)

        marker.unlink()
        worker.write_text(worker.read_text().replace("else:\n time.sleep(0.5)",
                                                     "else:\n time.sleep(30)"))
        result = run_command(f"{sys.executable} {worker} {{prompt}}", "x", timeout=1,
                             use_cgroup=True)
        if not marker.exists() or result.ok or result.cleanup_failed:
            raise RuntimeError(f"contained timeout failed: {result}")
        if alive(int(marker.read_text())):
            raise RuntimeError("detached synthetic worker survived timeout")
        print("real bridge cleaned detached worker after timeout", flush=True)

        marker.unlink()
        runner = Path(tmp) / "runner.py"
        runner.write_text(
            "from agent_lease_mcp.bridge import run_command\n"
            f"run_command({f'{sys.executable} {worker} {{prompt}}'!r}, 'x', "
            "timeout=30, use_cgroup=True)\n"
        )
        parent = subprocess.Popen([sys.executable, str(runner)])
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not marker.exists():
                raise RuntimeError("detached synthetic worker did not start")
            pid = int(marker.read_text())
            parent.kill()
            parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            if alive(pid):
                raise RuntimeError("detached synthetic worker survived bridge death")
            print("real guard cleaned detached worker after bridge SIGKILL", flush=True)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            # Only remove groups newly created inside this isolated scope.
            for path in set(root.glob("agent-lease-attempt-*")) - before:
                (path / "cgroup.kill").write_text("1")
                deadline = time.monotonic() + 5
                while "populated 1" in (path / "cgroup.events").read_text():
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"test group remains populated: {path}")
                    time.sleep(0.05)
                path.rmdir()
    return 0


def main() -> int:
    root = current_cgroup()
    before = set(root.glob("agent-lease-attempt-*"))
    try:
        return run_probe()
    finally:
        # The delegated scope is dedicated to this test. Never touch prior groups.
        for path in set(root.glob("agent-lease-attempt-*")) - before:
            (path / "cgroup.kill").write_text("1")
            deadline = time.monotonic() + 5
            while "populated 1" in (path / "cgroup.events").read_text():
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"test group remains populated: {path}")
                time.sleep(0.05)
            path.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
