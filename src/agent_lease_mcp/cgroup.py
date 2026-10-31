"""Linux cgroup-v2 containment for one command in a delegated scope."""
from __future__ import annotations

import os
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
