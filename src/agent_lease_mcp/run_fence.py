"""Persistent fail-closed marker for an unverified command attempt.

The marker survives SIGKILL and reboot; a lock distinguishes a live owner.
It is removed only after command cleanup was confirmed. In cgroup mode a new
bridge can recover a recorded group in the same delegated scope; all other
uncertain markers require manual investigation.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import stat
from pathlib import Path

from .cgroup import recover_cgroup


class RunFence:
    def __init__(self, db_path: Path, agent: str) -> None:
        db = db_path.resolve()
        suffix = hashlib.sha256(agent.encode("utf-8")).hexdigest()
        self.path = db.parent / f".{db.name}.run-fence-{suffix}"
        self._file = None
        self._message_id = None

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
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data = f"Unverified command attempt for task {message_id}.\n".encode()
            if os.write(fd, data) != len(data):
                raise OSError("cannot fully write command fence")
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        self._file = os.fdopen(fd, "r+b", buffering=0)
        self._message_id = message_id
        self._sync_parent()

    def record_cgroup(self, group) -> None:
        if self._file is None:
            raise OSError("command fence is not owned")
        info = group.path.stat(follow_symlinks=False)
        record = {"version": 1, "message_id": self._message_id,
                  "root": str(group.path.parent), "name": group.path.name,
                  "dev": info.st_dev, "ino": info.st_ino}
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        data = json.dumps({"record": record, "sha256": hashlib.sha256(raw).hexdigest()}).encode()
        self._file.seek(0)
        self._file.truncate()
        if self._file.write(data) != len(data):
            raise OSError("cannot persist cgroup identity")
        os.fsync(self._file.fileno())

    def try_recover(self) -> bool:
        """Recover only a dead owner's recorded cgroup within our current scope."""
        flags = os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError:
            return False
        with os.fdopen(fd, "r+b", buffering=0) as marker:
            try:
                fcntl.flock(marker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                own = os.fstat(marker.fileno())
                current = self.path.stat(follow_symlinks=False)
                if (not stat.S_ISREG(own.st_mode)
                        or (own.st_dev, own.st_ino) != (current.st_dev, current.st_ino)):
                    return False
                raw = marker.read(4097)
                if len(raw) > 4096:
                    return False
                data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(data.get("record"), dict):
                    return False
                record, digest = data["record"], data.get("sha256")
                encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
                if (not isinstance(digest, str)
                        or not hmac.compare_digest(digest, hashlib.sha256(encoded).hexdigest())
                        or not recover_cgroup(record)):
                    return False
                self.path.unlink()
                self._sync_parent()
                return True
            except (OSError, ValueError, TypeError):
                return False

    def release(self) -> None:
        self.path.unlink()
        self._sync_parent()
        if self._file is not None:
            self._file.close()
            self._file = None
