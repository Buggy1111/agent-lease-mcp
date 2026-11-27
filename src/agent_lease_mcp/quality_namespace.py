"""Trusted stdlib-only namespace bootstrap; never imports a project.

Keep Linux parent-death protection across unshare/exec. The outer network
namespace gets only its loopback interface; bwrap retains that network while
isolating PID/mount/user/etc namespaces and dropping all capabilities.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys


def main() -> int:
    try:
        args = sys.argv[1:]
        if args[:1] == ["--cgroup-fd"]:
            fd = int(args[1])
            try:
                if os.write(fd, b"0") != 1:
                    return 107
            finally:
                os.close(fd)
            del args[:2]
        inner = bool(args and args[0] == "--inner")
        if inner:
            args.pop(0)
        expected_parent = int(args.pop(0))
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(1, int(signal.SIGKILL)) != 0:
            return 101
        if os.getppid() != expected_parent:
            return 102
        if not args or args[0] != "/usr/bin/bwrap":
            return 103
        env = {"PATH": "/usr/bin:/bin"}
        if inner:
            ready = subprocess.run(["/usr/sbin/ip", "link", "set", "lo", "up"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=env, timeout=3, check=False)
            if ready.returncode != 0:
                return 104
            if os.getppid() != expected_parent:
                return 105
            os.execve(args[0], args, env)
        else:
            os.execve("/usr/bin/unshare", ["/usr/bin/unshare", "--user", "--map-root-user", "--net",
                      sys.executable, "-I", "-S", "-B", os.path.abspath(__file__), "--inner",
                      str(expected_parent), *args], env)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 106
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
