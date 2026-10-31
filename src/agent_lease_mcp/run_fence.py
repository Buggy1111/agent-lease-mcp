"""Persistent fail-closed marker for an unverified command attempt.

The marker is deliberately not a process lock: it survives SIGKILL and reboot.
It is removed only after command cleanup was confirmed. Recovery requires a
human to verify the old process is gone; this does not replace cgroup cleanup.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class RunFence:
    def __init__(self, db_path: Path, agent: str) -> None:
        db = db_path.resolve()
        suffix = hashlib.sha256(agent.encode("utf-8")).hexdigest()
        self.path = db.parent / f".{db.name}.run-fence-{suffix}"

    def present(self) -> bool:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return False
        return True

    def _sync_parent(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def acquire(self, message_id: int) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            data = f"Unverified command attempt for task {message_id}.\n".encode()
            if os.write(fd, data) != len(data):
                raise OSError("cannot fully write command fence")
            os.fsync(fd)
        finally:
            os.close(fd)
        self._sync_parent()

    def release(self) -> None:
        self.path.unlink()
        self._sync_parent()
