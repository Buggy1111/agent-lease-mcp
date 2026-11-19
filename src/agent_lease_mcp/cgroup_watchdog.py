"""Independent cgroup cleanup when the bridge's lifetime pipe closes."""
from __future__ import annotations

import os
import signal
import sys


def main() -> int:
    read_fd, ready_fd, kill_fd, parent_pid = map(int, sys.argv[1:])

    def parent_died(_signum=None, _frame=None):
        try:
            os.write(kill_fd, b"1")
        except OSError:
            pass
        os._exit(1)

    try:
        # The death signal covers a leaked copy of the lifetime pipe in a
        # forked process. Install its handler before setting PR_SET_PDEATHSIG.
        import ctypes

        signal.signal(signal.SIGTERM, parent_died)
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(1, int(signal.SIGTERM)) != 0 or os.getppid() != parent_pid:
            parent_died()
        os.write(ready_fd, b"1")
        os.close(ready_fd)
        # Only the bridge owns the write end. A clean completion sends C;
        # EOF without C means that the bridge died or abandoned the attempt.
        while True:
            data = os.read(read_fd, 1)
            if data == b"C":
                return 0
            if not data:
                return 0 if os.write(kill_fd, b"1") == 1 else 1
    except OSError:
        return 1
    finally:
        os.close(read_fd)
        os.close(kill_fd)


if __name__ == "__main__":
    raise SystemExit(main())
