"""Typed provider-availability probes.

Codex exposes an explicit availability bit through its local App Server.  This
module deliberately does not derive availability from usage percentages or a
reset timestamp: the protocol says that ``ordinaryUsageAllowed = null`` means
the answer is unavailable, not that the account recovered.
"""

from __future__ import annotations

import json
import selectors
import subprocess
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderState:
    state: str
    source: str
    observed_at: float
    reason: str = ""
    resets_at: float | None = None


def _snapshots(payload: dict[str, Any]) -> list[dict[str, Any]]:
    snapshots = []
    primary = payload.get("rateLimits")
    if isinstance(primary, dict):
        snapshots.append(primary)
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        snapshots.extend(value for value in by_id.values() if isinstance(value, dict))
    return snapshots


def parse_codex_rate_limits(payload: dict[str, Any], *, now: float | None = None) -> ProviderState:
    """Convert a Codex response to available/limited/unknown without guessing."""
    observed_at = time.time() if now is None else now
    snapshots = _snapshots(payload)
    reached = next(
        (str(s["rateLimitReachedType"]) for s in snapshots if s.get("rateLimitReachedType")),
        "",
    )
    spend_reached = any(s.get("spendControlReached") is True for s in snapshots)
    allowed = payload.get("ordinaryUsageAllowed")

    reset_values: list[float] = []
    for snapshot in snapshots:
        for name in ("primary", "secondary", "individualLimit"):
            window = snapshot.get(name)
            if isinstance(window, dict) and isinstance(window.get("resetsAt"), (int, float)):
                reset_values.append(float(window["resetsAt"]))
    resets_at = min((value for value in reset_values if value > observed_at), default=None)

    if allowed is False or reached or spend_reached:
        reason = reached or ("spend_control_reached" if spend_reached else "ordinary_usage_denied")
        return ProviderState("limited", "codex_app_server", observed_at, reason, resets_at)
    if allowed is True:
        return ProviderState("available", "codex_app_server", observed_at, resets_at=resets_at)
    return ProviderState(
        "unknown", "codex_app_server", observed_at,
        "ordinaryUsageAllowed unavailable", resets_at,
    )


def _read_codex_response(binary: str, requests: tuple[dict, ...], timeout: float) -> dict | None:
    """Keep stdin open while the App Server asynchronously answers request id 2."""
    proc = subprocess.Popen(
        [binary, "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        for item in requests:
            proc.stdin.write(json.dumps(item, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready = selector.select(max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                continue
            if message.get("id") == 2 and isinstance(message.get("result"), dict):
                return message["result"]
        return None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def probe_codex_rate_limits(*, binary: str = "codex", timeout: float = 15) -> ProviderState:
    """Read the authenticated local Codex App Server snapshot over JSONL stdio."""
    requests = (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "agent-lease", "version": "0.2.0"},
            "capabilities": {"experimentalApi": True},
        }},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
    )
    observed_at = time.time()
    try:
        result = _read_codex_response(binary, requests, timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProviderState("unknown", "codex_app_server", observed_at, type(exc).__name__)
    if result is not None:
        return parse_codex_rate_limits(result, now=observed_at)
    return ProviderState("unknown", "codex_app_server", observed_at, "rate_limit_response_missing")
