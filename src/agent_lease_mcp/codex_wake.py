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
from .config import STALE_PEER_SECONDS, Settings
from .lifecycle import wakes

RECIPIENT = "codex"
RECOVERY_WINDOW_SECONDS = 86400


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


def _queue_text(thread: str, notice: str, *, runner=subprocess.run) -> tuple[bool, str]:
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


def queue_nudge(thread: str, message_ids: list[int], *, runner=subprocess.run) -> tuple[bool, str]:
    ids = ", ".join(f"#{mid}" for mid in message_ids)
    noun = "zprávu" if len(message_ids) == 1 else "zprávy"
    notice = (
        f"[agent-lease] Máš nové adresné {noun} {ids} v místnosti. "
        "Zavolej inbox/room a odpověz přes send."
    )
    return _queue_text(thread, notice, runner=runner)


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
        self.recovery_path = cursor_path.with_name(f"{cursor_path.name}.recovery")
        saved_recovery = _read_cursor(self.recovery_path)
        self.recovery_episode = saved_recovery or 0
        self._recovery_baselined = saved_recovery is not None
        self._last_error = ""
        self._retry_at = 0.0
        self._provider_audit_since = 0
        self._provider_was_limited = {RECIPIENT: False, f"{RECIPIENT}-bridge": False}
        self._latest_recovery = None

    def step(self) -> bool:
        """Process one snapshot; return true only when a Codex nudge was queued."""
        if self.cursor is None:
            self.cursor = _high_water(self.store)
            _atomic_write(self.cursor_path, self.cursor)
            self._current_recovery()  # první start baselinuje i staré recovery epizody
            return False
        if self.clock() < self._retry_at:
            return False

        messages = _addressed_after(self.store, RECIPIENT, self.cursor)
        if not messages:
            return self._wake_recovered_work()
        high_water = int(messages[-1]["id"])
        wake_ids = [
            int(message["id"])
            for message in messages
            if message.get("kind") in {"chat", "result", "task"}
            and wakes(message, RECIPIENT)
        ]
        if not wake_ids:
            self.cursor = high_water
            _atomic_write(self.cursor_path, self.cursor)
            return self._wake_recovered_work()

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
        self._mark_current_recovery()
        return True

    def _current_recovery(self) -> dict | None:
        self._scan_provider_audit()
        episode = self._latest_recovery
        if not self._recovery_baselined:
            self.recovery_episode = int(episode["id"]) if episode else 0
            _atomic_write(self.recovery_path, self.recovery_episode)
            self._recovery_baselined = True
            return None
        if not episode or int(episode["id"]) <= self.recovery_episode:
            return None
        provider = self.store.provider_state(episode["agent"])
        if not provider or provider["state"] != "available":
            return None
        if time.time() - float(episode["at"]) > RECOVERY_WINDOW_SECONDS:
            return None
        return episode

    def _scan_provider_audit(self) -> None:
        """Page once through history, then inspect only new provider events."""
        while True:
            batch = self.store.recent_audit(
                ("provider-state",), since_id=self._provider_audit_since, limit=200,
            )
            if not batch:
                return
            for event in batch:
                self._provider_audit_since = max(self._provider_audit_since, int(event["id"]))
                agent = event["agent"]
                if agent not in self._provider_was_limited:
                    continue
                detail = str(event.get("detail") or "")
                if detail.startswith("limited via "):
                    self._provider_was_limited[agent] = True
                elif detail.startswith("available via ") and self._provider_was_limited[agent]:
                    self._latest_recovery = event
                    self._provider_was_limited[agent] = False
            if len(batch) < 200:
                return

    def _mark_current_recovery(self) -> None:
        episode = self._current_recovery()
        if episode:
            self.recovery_episode = int(episode["id"])
            _atomic_write(self.recovery_path, self.recovery_episode)

    def _wake_recovered_work(self) -> bool:
        episode = self._current_recovery()
        if episode is None:
            return False
        peer = next((p for p in self.store.peers() if p["agent"] == RECIPIENT), None)
        inactive = peer is None or peer["seen_seconds_ago"] >= STALE_PEER_SECONDS
        status = str(peer.get("status") or "") if peer else ""
        meaningful_status = bool(status and status != "start session")
        accepted_work = self.store.task_summary(RECIPIENT)["accepted_without_start"] > 0
        if not inactive or not (accepted_work or meaningful_status):
            return False
        notice = (
            "[agent-lease] Limit Codexu skončil a zůstala rozdělaná práce. "
            "Zkontroluj room/jobs a bezpečně pokračuj tam, kde ses zastavil."
        )
        ok, error = _queue_text(self.thread, notice, runner=self.runner)
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
        self.recovery_episode = int(episode["id"])
        _atomic_write(self.recovery_path, self.recovery_episode)
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-lease-codex-wake")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--poll", type=float, default=0.2, help="signal poll interval in seconds")
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

    seen_signal = None
    next_db = 0.0
    while True:
        now = time.monotonic()
        signal = wake.store.signal_mtime(RECIPIENT)
        if signal != seen_signal or now >= next_db:
            seen_signal, next_db = signal, now + 5.0
            wake.step()
        if args.once:
            return 0
        time.sleep(max(0.1, args.poll))


if __name__ == "__main__":
    raise SystemExit(main())
