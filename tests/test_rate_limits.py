from __future__ import annotations

import subprocess

from agent_lease_mcp import rate_limits
from agent_lease_mcp.rate_limits import parse_codex_rate_limits, probe_codex_rate_limits


def test_explicit_allowed_is_available():
    state = parse_codex_rate_limits({
        "ordinaryUsageAllowed": True,
        "rateLimits": {"primary": {"usedPercent": 99, "resetsAt": 200}},
    }, now=100)

    assert state.state == "available"
    assert state.resets_at == 200


def test_null_permission_never_infers_recovery_from_percent_or_reset():
    state = parse_codex_rate_limits({
        "ordinaryUsageAllowed": None,
        "rateLimits": {"primary": {"usedPercent": 0, "resetsAt": 200}},
    }, now=100)

    assert state.state == "unknown"


def test_explicit_reached_reason_is_limited_even_when_permission_missing():
    state = parse_codex_rate_limits({
        "ordinaryUsageAllowed": None,
        "rateLimits": {"rateLimitReachedType": "workspace_member_usage_limit_reached"},
    }, now=100)

    assert state.state == "limited"
    assert state.reason == "workspace_member_usage_limit_reached"


def test_probe_reads_only_matching_response(monkeypatch):
    monkeypatch.setattr(rate_limits, "_read_codex_response", lambda *a: {
        "ordinaryUsageAllowed": False,
        "rateLimits": {"rateLimitReachedType": "rate_limit_reached"},
    })

    assert probe_codex_rate_limits().state == "limited"


def test_probe_failure_is_unknown(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("codex", 1)

    monkeypatch.setattr(rate_limits, "_read_codex_response", fail)
    assert probe_codex_rate_limits().state == "unknown"
