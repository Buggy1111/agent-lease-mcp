"""Linux cgroup-v2 containment for one command in a delegated scope."""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path


def current_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            path = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
            resolved = path.resolve(strict=True)
            resolved.relative_to(Path("/sys/fs/cgroup"))
            return resolved
    raise OSError("cgroup v2 is unavailable")


def recover_cgroup(record: object, timeout: float = 5) -> bool:
    """Kill only the recorded cgroup, under this process's delegated root."""
    if not isinstance(record, dict) or record.get("version") != 1:
        return False
    root = current_cgroup()
    name = record.get("name")
    dev, ino = record.get("dev"), record.get("ino")
    if (record.get("root") != str(root) or not isinstance(name, str)
            or not re.fullmatch(r"agent-lease-attempt-[0-9a-f]{32}", name)
            or type(dev) is not int or type(ino) is not int):
        return False
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            group_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=root_fd)
        except FileNotFoundError:
            return True  # original group was already removed by the kernel/manager
        try:
            st = os.fstat(group_fd)
            if (st.st_dev, st.st_ino) != (dev, ino):
                return False
            kill_fd = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=group_fd)
            try:
                if os.write(kill_fd, b"1") != 1:
                    return False
            finally:
                os.close(kill_fd)
            deadline = time.monotonic() + timeout
            while True:
                events_fd = os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=group_fd)
                try:
                    fields = dict(line.split() for line in os.read(events_fd, 4096).decode().splitlines())
                finally:
                    os.close(events_fd)
                if fields.get("populated") == "0":
                    break
                if fields.get("populated") != "1" or time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (dev, ino):
                return False
            os.rmdir(name, dir_fd=root_fd)
            return True
        finally:
            os.close(group_fd)
    finally:
        os.close(root_fd)


class CommandCgroup:
    def __init__(self) -> None:
        root = current_cgroup()
        self.path = root / f"agent-lease-attempt-{uuid.uuid4().hex}"
        self.path.mkdir(mode=0o700)
        try:
            self.procs_fd = os.open(self.path / "cgroup.procs", os.O_WRONLY)
            try:
                self.kill_fd = os.open(self.path / "cgroup.kill", os.O_WRONLY)
            except BaseException:
                os.close(self.procs_fd)
                raise
            self.guard_kill_fd = os.open(self.path / "cgroup.kill", os.O_WRONLY)
            # Verify the events file before any command is allowed to start.
            self._populated()
        except BaseException:
            if hasattr(self, "kill_fd"):
                os.close(self.kill_fd)
                os.close(self.procs_fd)
            if hasattr(self, "guard_kill_fd"):
                os.close(self.guard_kill_fd)
            self.path.rmdir()
            raise

    def _populated(self) -> bool:
        fields = dict(line.split() for line in (self.path / "cgroup.events").read_text().splitlines())
        if fields.get("populated") not in {"0", "1"}:
            raise OSError("cannot verify cgroup population")
        return fields["populated"] == "1"

    def cleanup(self, timeout: float = 5) -> bool:
        try:
            if os.write(self.kill_fd, b"1") != 1:
                return False
            deadline = time.monotonic() + timeout
            while self._populated():
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
            self.path.rmdir()
            return True
        except OSError:
            return False
        finally:
            os.close(self.procs_fd)
            os.close(self.kill_fd)
            os.close(self.guard_kill_fd)
