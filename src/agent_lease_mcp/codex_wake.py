"""Wake an interactive Codex session when agent-lease receives a direct message.

Codex does not implement Claude Code's ``asyncRewake`` hook.  This small daemon
therefore watches the shared room and submits a short, content-free nudge via
``codex queue``.  It deliberately does not heartbeat: the service identity
(``codex-wake``) must never appear as another agent in the room.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from .broker import open_store
from .config import Settings
from .lifecycle import is_receipt, wakes

RECIPIENT = "codex"


def _atomic_write(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"{value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_cursor(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid Codex wake cursor in {path}") from exc
    if value < 0:
        raise ValueError(f"invalid Codex wake cursor in {path}")
    return value


def _high_water(store) -> int:
    latest = store.latest_messages(limit=1)
    return int(latest[-1]["id"]) if latest else 0


def _addressed_after(store, recipient: str, cursor: int) -> list[dict]:
    messages: list[dict] = []
    since = cursor
    while True:
        batch = store.addressed(recipient, since_id=since, limit=200)
        messages.extend(batch)
        if len(batch) < 200:
            return messages
        since = int(batch[-1]["id"])


def queue_nudge(thread: str, message_ids: list[int], *, runner=subprocess.run) -> tuple[bool, str]:
    ids = ", ".join(f"#{mid}" for mid in message_ids)
    noun = "zprávu" if len(message_ids) == 1 else "zprávy"
    notice = (
        f"[agent-lease] Máš nové adresné {noun} {ids} v místnosti. "
        "Zavolej inbox/room a odpověz přes send."
    )
    try:
        completed = runner(
            ["codex", "queue", "--thread", thread, "--message", notice],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if completed.returncode == 0:
        return True, ""
    detail = (completed.stderr or completed.stdout or "codex queue failed").strip()
    return False, detail[-500:]


class CodexWake:
    def __init__(
        self,
        store,
        thread: str,
        cursor_path: Path,
        *,
        runner=subprocess.run,
        clock=time.monotonic,
        retry_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.thread = thread
        self.cursor_path = cursor_path
        self.runner = runner
        self.clock = clock
        self.retry_seconds = retry_seconds
        self.cursor = _read_cursor(cursor_path)
        self._last_error = ""
        self._retry_at = 0.0

    def step(self) -> bool:
        """Process one snapshot; return true only when a Codex nudge was queued."""
        if self.cursor is None:
            self.cursor = _high_water(self.store)
            _atomic_write(self.cursor_path, self.cursor)
            return False
        if self.clock() < self._retry_at:
            return False

        messages = _addressed_after(self.store, RECIPIENT, self.cursor)
        if not messages:
            return False
        high_water = int(messages[-1]["id"])
        wake_ids = [
            int(message["id"])
            for message in messages
            if message.get("kind") in {"chat", "result"}
            and wakes(message, RECIPIENT)
            and not is_receipt(message)
        ]
        if not wake_ids:
            self.cursor = high_water
            _atomic_write(self.cursor_path, self.cursor)
            return False

        ok, error = queue_nudge(self.thread, wake_ids, runner=self.runner)
        if not ok:
            if error != self._last_error:
                print(
                    f"[agent-lease-codex-wake] Codex session {self.thread!r} is not active: "
                    f"{error}",
                    file=sys.stderr,
                )
                self._last_error = error
            self._retry_at = self.clock() + self.retry_seconds
            return False

        self._last_error = ""
        self._retry_at = 0.0
        self.cursor = high_water
        _atomic_write(self.cursor_path, self.cursor)
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-lease-codex-wake")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--poll", type=float, default=1.0, help="fallback poll interval in seconds")
    args = parser.parse_args(argv)

    thread = os.environ.get("AGENT_LEASE_CODEX_THREAD", "").strip()
    if not thread:
        print(
            "AGENT_LEASE_CODEX_THREAD is required; name the target session with /rename.",
            file=sys.stderr,
        )
        return 2

    settings = Settings.from_env()
    cursor_path = Path(
        os.environ.get(
            "AGENT_LEASE_CODEX_WAKE_CURSOR",
            settings.db_path.parent / "codex-wake.cursor",
        )
    ).expanduser()
    try:
        wake = CodexWake(open_store(settings), thread, cursor_path)
    except ValueError as exc:
        print(f"[agent-lease-codex-wake] {exc}", file=sys.stderr)
        return 2

    while True:
        wake.step()
        if args.once:
            return 0
        time.sleep(max(0.1, args.poll))


if __name__ == "__main__":
    raise SystemExit(main())
