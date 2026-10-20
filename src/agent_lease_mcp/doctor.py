"""`agent-lease doctor` — ověří, že koordinace opravdu funguje, ne že je jen nainstalovaná.

Tichá porucha je u tohoto projektu nejhorší (viz README: Codex bez `writable_roots`
se do místnosti vůbec nedostane a nikdo si toho nevšimne). Doctor proto zkouší
skutečný zápis, ne jen existenci souborů.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from .config import Settings, is_trusted_agent, parse_fallbacks

OK, WARN, FAIL = "✔", "⚠", "✘"


def _hooks_mention(path: Path, needle: str) -> bool | None:
    if not path.exists():
        return None
    try:
        return needle in json.dumps(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None


def _hook_agent_names(path: Path) -> set[str] | None:
    """Return explicit AGENT_NAME values from hook commands, without executing them."""
    if not path.exists():
        return None
    try:
        raw = json.dumps(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None
    return set(re.findall(r"\bAGENT_NAME=([A-Za-z0-9_.-]+)", raw))


def _codex_mcp_agent(path: Path) -> str | None:
    """Read only the agent-lease MCP identity from Codex TOML; never return secrets."""
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text())
        server = data.get("mcp_servers", {}).get("agent-lease", {})
        return server.get("env", {}).get("AGENT_NAME")
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return None


def _codex_queue_supported(binary: str | None) -> bool:
    if not binary:
        return False
    try:
        probe = subprocess.run(
            [binary, "queue", "--help"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _service_env_value(path: Path, key: str) -> str:
    """Read one non-secret value from a systemd EnvironmentFile ("" if absent)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _service_fallbacks(path: Path) -> dict[str, tuple[str, ...]]:
    """Read only the non-secret fallback setting from a systemd EnvironmentFile."""
    raw = _service_env_value(path, "AGENT_LEASE_FALLBACKS")
    return parse_fallbacks(raw) if raw else {}


def _user_service_active(name: str) -> bool:
    if not (shutil.which("systemctl") and os.path.exists("/run/systemd/system")):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", name],
                              timeout=5, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_doctor(*, json_output: bool = False) -> int:
    settings = Settings.from_env()
    problems = 0
    checks: list[dict] = []

    def line(check_id: str, level: str, text: str, **details) -> None:
        status = {OK: "ok", WARN: "warning", FAIL: "failure"}[level]
        checks.append({"id": check_id, "status": status, "level": level,
                       "message": text, **details})

    if is_trusted_agent(settings.agent):
        line("agent_identity", OK, f"agent identity: {settings.agent}", agent=settings.agent)
    else:
        problems += 1
        line("agent_identity", FAIL,
             f"AGENT_NAME is not set (you are {settings.agent}) — hook and server will disagree",
             agent=settings.agent)

    db = settings.db_path
    try:
        db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=db.parent, prefix=".doctor-"):
            pass
        line("room_write", OK, f"writing to {db.parent} works", path=str(db.parent))
    except OSError as exc:
        problems += 1
        line("room_write", FAIL, f"cannot write to {db.parent}: {exc} "
             "(Codex sandbox? see README → writable_roots, or use the broker socket)",
             path=str(db.parent))

    if db.exists():
        mode = stat.S_IMODE(db.stat().st_mode)
        if mode & 0o077:
            problems += 1
            line("db_mode", FAIL, f"DB mode is {oct(mode)}, should be 0600 (chmod 600 {db})",
                 mode=oct(mode))
        else:
            line("db_mode", OK, "DB mode 0600", mode="0o600")

    ver = sqlite3.sqlite_version_info
    if (3, 7, 0) <= ver < (3, 44, 6) or (3, 45, 0) <= ver < (3, 50, 7) or (3, 51, 0) <= ver < (3, 51, 3):
        line("sqlite_version", WARN,
             f"SQLite {sqlite3.sqlite_version} has a known WAL-reset bug "
             "(fixed in 3.51.3 / 3.50.7 / 3.44.6)", version=sqlite3.sqlite_version)
    else:
        line("sqlite_version", OK, f"SQLite {sqlite3.sqlite_version}",
             version=sqlite3.sqlite_version)

    home = Path.home()
    codex_config = home / ".codex" / "config.toml"
    mcp_agent = _codex_mcp_agent(codex_config)
    if mcp_agent is None:
        line("codex_mcp_identity", WARN,
             "Codex: agent-lease MCP identity not verified", configured_agent=None)
    elif mcp_agent != "codex":
        problems += 1
        line("codex_mcp_identity", FAIL,
             f"Codex MCP uses AGENT_NAME={mcp_agent!r}, expected 'codex'",
             configured_agent=mcp_agent)
    else:
        line("codex_mcp_identity", OK, "Codex MCP identity: codex", configured_agent=mcp_agent)

    for label, path, needle in (
        ("Claude Code", home / ".claude" / "settings.json", "agent-lease-guard"),
        ("Codex", home / ".codex" / "hooks.json", "agent-lease-guard"),
    ):
        slug = "claude" if label == "Claude Code" else "codex"
        found = _hooks_mention(path, needle)
        if found is None:
            line(f"{slug}_guard", WARN,
                 f"{label}: {path} not found / unreadable — guard hook not verified")
        elif found:
            line(f"{slug}_guard", OK, f"{label}: guard hook set")
        else:
            problems += 1
            line(f"{slug}_guard", FAIL,
                 f"{label}: agent-lease-guard missing in {path} "
                 "(without it this is only a notice board)")
        if found:
            ctx = _hooks_mention(path, "agent-lease-context")
            rewake = _hooks_mention(path, "--rewake")
            ctx_msg = "set" if ctx else "MISSING → no 'seen' receipts"
            rw_msg = "set" if rewake else "not set (a message waits for the next prompt)"
            line(f"{slug}_context", OK if ctx else WARN,
                 f"{label}: message injection (agent-lease-context) {ctx_msg}")
            if slug == "claude":  # Codex asyncRewake nemá — jeho buzení viz codex_rewake níž
                line(f"{slug}_rewake", OK if rewake else WARN,
                     f"{label}: waking a sleeping session (wait --rewake) {rw_msg}",
                     configured=bool(rewake))
            hook_agents = _hook_agent_names(path)
            expected = "claude-code" if slug == "claude" else "codex"
            if hook_agents is not None and hook_agents and hook_agents != {expected}:
                problems += 1
                line(f"{slug}_hook_identity", FAIL,
                     f"{label}: hook identities {sorted(hook_agents)!r}, expected only {expected!r}",
                     configured_agents=sorted(hook_agents))
            elif hook_agents:
                line(f"{slug}_hook_identity", OK,
                     f"{label}: hook identity {expected}", configured_agents=[expected])

    binaries: dict[str, str | None] = {}
    for binary in ("codex", "claude"):
        found_bin = shutil.which(binary)
        binaries[binary] = found_bin
        line(f"{binary}_binary", OK if found_bin else WARN,
             f"{binary} on PATH" if found_bin else f"{binary} not on PATH (no bridge for it)",
             available=bool(found_bin))

    queue_supported = _codex_queue_supported(binaries["codex"])
    line("codex_queue", OK if queue_supported else WARN,
         "Codex queue command is available for an existing named session"
         if queue_supported else "Codex queue command not available; use a bridge-owned worker",
         supported=queue_supported)

    env_file = home / ".config" / "agent-lease" / "environment"
    thread = (os.environ.get("AGENT_LEASE_CODEX_THREAD", "").strip()
              or _service_env_value(env_file, "AGENT_LEASE_CODEX_THREAD"))
    wake_active = _user_service_active("agent-lease-codex-wake.service")
    if queue_supported and thread and wake_active:
        line("codex_rewake", OK, f"Codex wake service active for named session {thread!r}",
             configured_thread=thread, service_active=True)
    else:
        missing = []
        if not thread:
            missing.append("name the session with /rename and set AGENT_LEASE_CODEX_THREAD "
                           f"in {env_file}")
        if not wake_active:
            missing.append("systemctl --user enable --now agent-lease-codex-wake.service")
        if not queue_supported:
            missing.append("a Codex CLI with `codex queue`")
        line("codex_rewake", WARN,
             "Codex: a sleeping session is not woken (a message waits for the next prompt) — "
             + "; ".join(missing),
             configured_thread=thread or None, service_active=wake_active)

    if platform.system() == "Linux" and "microsoft" in platform.release().lower():
        line("wsl_lifetime", WARN, "WSL: systemd services do not survive a Windows restart — "
             "see deploy/windows/start-bridge.ps1")
    if shutil.which("systemctl") and os.path.exists("/run/systemd/system"):
        line("systemd", OK, "systemd available (deploy/systemd/*.service)")

    from .broker import default_socket_path, ping
    from .store import Store

    sock = default_socket_path(settings)
    broker_running = ping(sock)
    line("broker", OK if broker_running else WARN,
         f"broker running ({sock})" if broker_running
         else "broker not running (tools write to SQLite directly)",
         running=broker_running, socket=str(sock))
    service_fallbacks = _service_fallbacks(home / ".config" / "agent-lease" / "environment")
    effective_fallbacks = settings.fallbacks or service_fallbacks
    if effective_fallbacks:
        source = "process environment" if settings.fallbacks else "systemd environment file"
        line("fallbacks", OK, f"failover chains ({source}): {effective_fallbacks}",
             configured=True, source=source)
    else:
        line("fallbacks", WARN,
             "AGENT_LEASE_FALLBACKS not set — when an agent hits its limit, its queue waits",
             configured=False)
    try:
        store = Store(settings=settings)
        limits = store.limits()
        for lim in limits:
            line("active_limit", WARN,
                 f"{lim['agent']} is limited for {lim['seconds_left'] // 60} more min",
                 agent=lim["agent"], seconds_left=lim["seconds_left"])
        provider = store.provider_state("codex-bridge") or store.provider_state("codex")
        if provider:
            provider_level = OK if provider["state"] == "available" else WARN
            line("codex_provider_state", provider_level,
                 f"Codex provider: {provider['state']} via {provider['source']}",
                 agent="codex", state=provider["state"], source=provider["source"],
                 observed_at=provider["observed_at"], resets_at=provider["resets_at"],
                 reason=provider["reason"])
        else:
            line("codex_provider_state", WARN,
                 "Codex provider state has not been observed yet",
                 agent="codex", state=None)

        summary = store.task_summary(settings.agent)
        queued = summary["pending"]
        unaccepted = summary["unaccepted"]
        accepted_without_start = summary["accepted_without_start"]
        level = WARN if queued or summary["needs_review"] else OK
        line("delivery_pipeline", level,
             f"{settings.agent}: {queued} pending ({unaccepted} unaccepted, "
             f"{accepted_without_start} accepted but not started), "
             f"{summary['leased']} leased, {summary['started']} started, "
             f"{summary['needs_review']} needs review",
             recipient=settings.agent, pending=queued, unaccepted=unaccepted,
             accepted_without_start=accepted_without_start,
             leased=summary["leased"], started=summary["started"],
             needs_review=summary["needs_review"])

        activations = store.activation_status(settings.agent)
        activation_level = OK if activations["worker_after_message_id"] is not None else WARN
        line("activation_baselines", activation_level,
             f"{settings.agent}: worker after #{activations['worker_after_message_id']}; "
             f"failover after #{activations['failover_after_message_id']}",
             **activations)

        worker_name = f"{settings.agent}-bridge"
        peer = next((p for p in store.peers() if p["agent"] == worker_name), None)
        worker_observed = bool(peer and str(peer.get("status", "")).startswith("bridge:"))
        line("worker", OK if worker_observed else WARN,
             f"autonomous worker observed for {settings.agent}"
             if worker_observed else
             f"no autonomous worker observed for {settings.agent}; hooks need an existing turn",
             observed=worker_observed, worker_identity=worker_name)
    except Exception as exc:  # noqa: BLE001
        line("delivery_pipeline", WARN,
             f"delivery state could not be inspected: {type(exc).__name__}")
    jev_on = os.environ.get("AGENT_LEASE_JEV", "") in ("1", "true", "yes")
    line("jev", OK if jev_on else WARN,
         "Jev on" if jev_on else "Jev off (optional, docs/JEV.md)", enabled=jev_on)

    if json_output:
        print(json.dumps({"ok": problems == 0, "problems": problems, "checks": checks},
                         ensure_ascii=False, sort_keys=True))
    else:
        print("agent-lease doctor\n")
        for check in checks:
            print(f" {check['level']} {check['message']}")
        print()
        print(f"{problems} problem(s) to fix." if problems else "Looks good.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(run_doctor())
