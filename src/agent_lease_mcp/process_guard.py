"""Supervise one command inside the bridge's private process group.

The bridge starts this process as a session leader with Linux PDEATHSIG=SIGTERM.
If the bridge dies, kill the group, including ordinary grandchildren. Escaped
sessions and a forcibly killed guard still require cgroup-based supervision.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

_cgroup_kill_fd = None

def _stop_group(_signum, _frame) -> None:
    if _cgroup_kill_fd is not None:
        try:
            os.write(_cgroup_kill_fd, b"1")
        except OSError:
            pass  # fall back to the process group if cgroup cleanup fails
    os.killpg(os.getpgrp(), signal.SIGKILL)


def main() -> int:
    global _cgroup_kill_fd
    args = sys.argv[1:]
    ready_fd = None
    if args and args[0].startswith("--ready-fd="):
        ready_fd = int(args.pop(0).split("=", 1)[1])
    if args and args[0].startswith("--cgroup-kill-fd="):
        _cgroup_kill_fd = int(args.pop(0).split("=", 1)[1])
    if not args:
        print("process guard requires a command", file=sys.stderr)
        return 2
    if os.getpgrp() != os.getpid():
        print("process guard must lead its own process group", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, _stop_group)
    try:
        child = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        if ready_fd is not None:
            os.write(ready_fd, b"1")
            os.close(ready_fd)
        code = child.wait()
    except OSError as exc:
        print(f"cannot start {args[0]}: {exc}", file=sys.stderr)
        return 127
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    raise SystemExit(main())
