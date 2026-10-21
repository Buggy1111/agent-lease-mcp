from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from agent_lease_mcp.bridge import Bridge, parse_retry_after
from agent_lease_mcp.config import Settings
from agent_lease_mcp.models import DeliveryState
from agent_lease_mcp.rate_limits import ProviderState
from agent_lease_mcp.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "room.db", settings=Settings(tmp_path / "room.db", "codex", 1800))


def fake(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake.py"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {script} {{prompt}}"


def test_success_accepts_first_then_returns_result(store: Store, tmp_path: Path):
    mid = store.send("michal", "codex", "spočítej 2+2", kind="task")
    cmd = fake(tmp_path, "import sys; print('výsledek: 4')")
    assert Bridge(store, "codex", cmd, lease_seconds=5).step()
    states = {j["id"]: j["state"] for j in store.jobs("codex")}
    assert states[mid] == "succeeded"
    texts = [m["text"] for m in store.inbox() if m["recipient"] == "michal"]
    assert any("codex saw" in t for t in texts)           # automatický příjem
    assert any(t.startswith("✔ taking") for t in texts)        # vědomé potvrzení dřív než výsledek
    assert any("výsledek: 4" in t for t in texts)
    peers = {p["agent"] for p in store.peers()}
    assert "codex-bridge" in peers and "codex" not in peers


def test_prompt_is_one_argument_not_shell(store: Store, tmp_path: Path):
    marker = tmp_path / "pwned"
    store.send("michal", "codex", f"x; touch {marker}", kind="task")
    cmd = fake(tmp_path, "import sys; print(len(sys.argv))")
    Bridge(store, "codex", cmd).step()
    assert not marker.exists()
    assert any(m["text"].endswith("2") for m in store.inbox() if "Done #" in m["text"])


def test_failure_goes_to_needs_review_and_reports(store: Store, tmp_path: Path):
    mid = store.send("michal", "codex", "rozbij to", kind="task")
    Bridge(store, "codex", fake(tmp_path, "import sys; print('boom'); sys.exit(3)")).step()
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "needs_review"
    assert any("selhal" in m["text"] for m in store.inbox())


def test_rate_limit_defers_instead_of_retry_storm(store: Store, tmp_path: Path):
    mid = store.send("michal", "codex", "limit", kind="task")
    cmd = fake(tmp_path, "import sys; print('Usage limit reached, try again in 2 hours'); sys.exit(1)")
    Bridge(store, "codex", cmd).step()
    job = next(j for j in store.jobs("codex") if j["id"] == mid)
    assert job["state"] == "pending"
    assert store.lease_next("codex") is None  # not_before je v budoucnu
    assert parse_retry_after("try again in 2 hours") == 7200


def test_missing_reset_is_not_invented(store: Store, tmp_path: Path):
    mid = store.send("michal", "claude-code", "limit", kind="task")
    cmd = fake(tmp_path, "import sys; print('rate limit reached'); sys.exit(1)")
    Bridge(store, "claude-code", cmd).step()

    provider = store.provider_state("claude-code-bridge")
    assert provider["state"] == "limited" and provider["resets_at"] is None
    assert next(j for j in store.jobs("claude-code") if j["id"] == mid)["state"] == "pending"
    assert parse_retry_after("rate limit reached") is None
    assert store.limits() == []


def test_claude_weekly_limit_wording_persists_calendar_reset(
    store: Store, tmp_path: Path,
):
    mid = store.send("michal", "claude-code", "limit", kind="task")
    cmd = fake(
        tmp_path,
        "import sys; print(\"You've hit your weekly limit · resets Oct 20, 6pm "
        "(Europe/Prague)\"); sys.exit(1)",
    )
    Bridge(store, "claude-code", cmd).step()

    assert next(j for j in store.jobs("claude-code") if j["id"] == mid)["state"] == "pending"
    assert store.provider_state("claude-code-bridge")["resets_at"] is not None


def test_calendar_reset_is_parsed_in_provider_timezone():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 17, 18, 0, tzinfo=ZoneInfo("Europe/Prague")).timestamp()
    delay = parse_retry_after(
        "You've hit your weekly limit · resets Oct 20, 6pm (Europe/Prague)", now=now,
    )

    assert delay == 3 * 86400


def test_successful_retry_confirms_provider_recovery(store: Store, tmp_path: Path, monkeypatch):
    store.report_provider_state(
        "claude-code-bridge", "limited", "cli_error", observed_at=100, resets_at=1000,
    )
    mid = store.send("michal", "claude-code", "try again", kind="task")
    monkeypatch.setattr(time, "time", lambda: 1001)

    assert Bridge(store, "claude-code", fake(tmp_path, "print('ok')")).step()
    assert next(j for j in store.jobs("claude-code") if j["id"] == mid)["state"] == "succeeded"
    provider = store.provider_state("claude-code-bridge")
    assert provider["state"] == "available" and provider["source"] == "cli_success"


def test_hourly_cap(store: Store, tmp_path: Path):
    for i in range(3):
        store.send("michal", "codex", f"t{i}", kind="task")
    b = Bridge(store, "codex", fake(tmp_path, "print('ok')"), max_per_hour=2)
    assert b.step() and b.step()
    assert not b.step()  # třetí čeká na další hodinu
    assert sum(j["state"] == "pending" for j in store.jobs("codex")) == 1


def test_explicit_provider_limit_prevents_task_lease(store: Store, tmp_path: Path):
    mid = store.send("michal", "codex", "must wait", kind="task")

    def limited():
        return ProviderState("limited", "test", 100, "rate_limit_reached")

    bridge = Bridge(store, "codex", fake(tmp_path, "print('must not run')"),
                    provider_probe=limited, provider_probe_interval=60)

    assert not bridge.step()
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "pending"
    assert store.provider_state("codex-bridge")["state"] == "limited"


def test_first_worker_activation_skips_old_backlog_but_survives_restart(
    store: Store, tmp_path: Path
):
    old = store.send("michal", "codex", "old task", kind="task")
    after = store.worker_activation("codex")
    assert after == old
    assert store.worker_activation("codex", include_existing=True) == after

    new = store.send("michal", "codex", "new task", kind="task")
    bridge = Bridge(
        store, "codex", fake(tmp_path, "print('ok')"), after_message_id=after,
    )
    assert bridge.step()

    states = {j["id"]: j["state"] for j in store.jobs("codex")}
    assert states[old] == "pending"
    assert states[new] == "succeeded"


def test_unauthorized_sender_cannot_enqueue(store: Store):
    with pytest.raises(PermissionError):
        store.send("unconfigured-x-1", "codex", "rm", kind="task")


def test_overdue_lists_unaccepted(store: Store):
    mid = store.send("michal", "codex", "ahoj", kind="chat")
    assert store.overdue(0)[0]["problem"] == "not delivered"
    store.mark_seen("codex", [mid])
    assert store.overdue(0)[0]["problem"] == "seen, not acknowledged"
    store.accept(mid, "codex")
    assert store.overdue(0) == []


def test_doctor_runs(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "d" / "room.db"))
    monkeypatch.setenv("AGENT_NAME", "codex")
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_lease_mcp.doctor import run_doctor
    code = run_doctor()
    out = capsys.readouterr().out
    assert "agent identity: codex" in out and code in (0, 1)
    assert os.path.isdir(tmp_path / "d")


def test_doctor_json_separates_delivery_from_worker(tmp_path: Path, monkeypatch, capsys):
    import json

    db = tmp_path / "room.db"
    monkeypatch.setenv("AGENT_LEASE_DB", str(db))
    monkeypatch.setenv("AGENT_NAME", "codex")
    monkeypatch.setenv("HOME", str(tmp_path))
    store = Store(db)
    mid = store.send("michal", "codex", "diagnostic task", kind="task")
    store.mark_seen("codex", [mid])
    store.accept(mid, "codex", "queued manually")

    from agent_lease_mcp.doctor import run_doctor
    code = run_doctor(json_output=True)
    report = json.loads(capsys.readouterr().out)
    checks = {item["id"]: item for item in report["checks"]}

    assert code in (0, 1)
    assert checks["delivery_pipeline"]["pending"] == 1
    assert checks["delivery_pipeline"]["unaccepted"] == 0
    assert checks["delivery_pipeline"]["accepted_without_start"] == 1
    assert checks["worker"]["observed"] is False
    assert "diagnostic task" not in json.dumps(report)


def test_doctor_reads_hook_identity_without_exposing_commands(tmp_path: Path):
    import json

    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{
        "type": "command", "command": "AGENT_NAME=codex agent-lease-context --secret nope"
    }]}]}}))

    from agent_lease_mcp.doctor import _hook_agent_names

    assert _hook_agent_names(hooks) == {"codex"}


def test_doctor_reads_service_fallbacks_without_loading_other_values(tmp_path: Path):
    env = tmp_path / "environment"
    env.write_text(
        "SECRET=do-not-read\n"
        "AGENT_LEASE_FALLBACKS=codex=claude-code;claude-code=codex\n"
    )

    from agent_lease_mcp.doctor import _service_fallbacks

    assert _service_fallbacks(env) == {
        "codex": ("claude-code",), "claude-code": ("codex",),
    }


def _fake_openrouter(status=200, body=None, headers=None):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.dumps(body or {"choices": [{"message": {"content": "ahoj z modelu"}}]})
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_openrouter_runner_success_and_request_shape():
    from agent_lease_mcp.bridge import run_openrouter
    srv, seen = _fake_openrouter()
    r = run_openrouter("otázka", model="x/y", api_key="sk-test",
                       base_url=f"http://127.0.0.1:{srv.server_port}/v1")
    srv.shutdown()
    assert r.ok and r.output == "ahoj z modelu"
    assert seen["auth"] == "Bearer sk-test" and seen["body"]["model"] == "x/y"


def test_openrouter_429_is_deferred_not_failed(store: Store):
    from agent_lease_mcp.bridge import run_openrouter
    srv, _ = _fake_openrouter(429, {"error": "slow"}, {"Retry-After": "120"})
    mid = store.send("michal", "kimi", "úkol", kind="task")

    def runner(prompt):
        return run_openrouter(prompt, model="m", api_key="k",
                              base_url=f"http://127.0.0.1:{srv.server_port}/v1")

    Bridge(store, "kimi", "", runner=runner).step()
    srv.shutdown()
    job = next(j for j in store.jobs("kimi") if j["id"] == mid)
    assert job["state"] == "pending"  # odloženo, ne ztraceno
    assert store.lease_next("kimi") is None


def test_provider_is_probed_on_the_first_step_right_after_boot(store: Store, tmp_path: Path, monkeypatch):
    """CI odhalilo: monotonic() běží od bootu; s výchozí 0.0 by se na čerstvém
    stroji (autostart po zapnutí PC) první kontrola limitu přeskočila."""
    import agent_lease_mcp.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod.time, "monotonic", lambda: 5.0)  # 5 s po bootu
    store.send("michal", "codex", "must wait", kind="task")
    bridge = Bridge(store, "codex", fake(tmp_path, "print('must not run')"),
                    provider_probe=lambda: ProviderState("limited", "test", 100, "rate_limit_reached"),
                    provider_probe_interval=60)
    assert not bridge.step()


def test_last_message_file_replaces_the_noisy_transcript(tmp_path: Path):
    """Živý test 20. 10.: bez --output-last-message vracel worker celý přepis běhu."""
    from agent_lease_mcp.bridge import run_command

    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import sys\nprint('tool output ' * 500)\n"
        "open(sys.argv[1], 'w').write('THE ANSWER')\n"
    )
    res = run_command(f"{sys.executable} {script} {{last_message}} {{prompt}}", "q", timeout=30)
    assert res.ok and res.output == "THE ANSWER"


def test_reported_limit_blocks_even_when_provider_says_available(store: Store, tmp_path: Path):
    """Živý test 20. 10. (#296): ohlášený limit worker ignoroval, protože
    poskytovatel hlásil „available"."""
    import time

    mid = store.send("michal", "codex", "must wait", kind="task")
    store.report_limit("codex-bridge", time.time() + 300, "artificial")
    bridge = Bridge(store, "codex", fake(tmp_path, "print('must not run')"),
                    provider_probe=lambda: ProviderState("available", "test", None, ""),
                    provider_probe_interval=60)
    assert not bridge.step()
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "pending"
    store.clear_limit("codex-bridge")
    assert bridge.step(), "po zrušení limitu se úkol zpracuje"
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "succeeded"


# ── bridge ustoupí interaktivní session stejného jména ──────────────────────

def test_bridge_never_takes_a_task_the_interactive_agent_accepted(store: Store, tmp_path: Path):
    """20. 10. 2026: Nox přijal task v okně a bridge ho mohl spustit podruhé."""
    mid = store.send("michal", "codex", "udělej X", kind="task")
    store.mark_seen("codex", [mid])
    store.accept(mid, "codex", "beru v okně")

    bridge = Bridge(store, "codex", fake(tmp_path, "print('ok')"), interactive_grace=0)
    assert bridge.step() is False
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "pending"


def test_bridge_waits_for_a_present_interactive_agent_then_takes_over(
    store: Store, tmp_path: Path, monkeypatch,
):
    real = time.time()
    store.heartbeat("codex")  # interaktivní Nox je u počítače
    mid = store.send("michal", "codex", "úkol", kind="task")
    bridge = Bridge(store, "codex", fake(tmp_path, "print('ok')"), interactive_grace=300)

    assert bridge.step() is False                      # čerstvý → nechá ho Noxovi
    monkeypatch.setattr(time, "time", lambda: real + 301)
    store.heartbeat("codex")
    assert bridge.step() is True                       # po grace převezme
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "succeeded"


def test_bridge_takes_a_task_at_once_when_nobody_is_present(store: Store, tmp_path: Path):
    mid = store.send("michal", "codex", "úkol", kind="task")
    bridge = Bridge(store, "codex", fake(tmp_path, "print('ok')"), interactive_grace=300)
    assert bridge.step() is True
    assert next(j for j in store.jobs("codex") if j["id"] == mid)["state"] == "succeeded"


def test_interactive_lease_still_gets_its_own_accepted_task(store: Store):
    """MCP `next_task` interaktivní session nesmí přijatý task přeskočit."""
    mid = store.send("michal", "codex", "úkol", kind="task")
    store.accept(mid, "codex", "beru")
    assert store.lease_next("codex").message_id == mid


def test_interactive_accept_after_bridge_lease_is_refused(store: Store, tmp_path: Path):
    """Review Nox #378: lease_next → interaktivní accept nesměl projít (dvojí provedení)."""
    mid = store.send("michal", "codex", "úkol", kind="task")
    lease = store.lease_next("codex", skip_accepted=True)
    assert lease.message_id == mid
    assert store.accept(mid, "codex", "beru v okně") is False          # drží ho worker
    assert store.accept(mid, "codex", "bridge", lease_token=lease.lease_token) is True
    assert store.ack(mid, "codex", DeliveryState.STARTED, lease_token=lease.lease_token)
    assert store.accept(mid, "codex", "beru v okně") is False          # pořád ne


def test_bridge_may_retake_its_own_task_after_lease_expiry(store: Store, monkeypatch):
    real = time.time()
    mid = store.send("michal", "codex", "úkol", kind="task")
    lease = store.lease_next("codex", lease_seconds=1, skip_accepted=True)
    store.accept(mid, "codex", "bridge", lease_token=lease.lease_token)
    monkeypatch.setattr(time, "time", lambda: real + 5)     # lease vypršel před startem
    again = store.lease_next("codex", skip_accepted=True)
    assert again is not None and again.message_id == mid


def test_interactive_accept_of_a_returned_task_blocks_the_bridge(store: Store):
    import sqlite3

    mid = store.send("michal", "codex", "úkol", kind="task")
    lease = store.lease_next("codex", skip_accepted=True)
    store.accept(mid, "codex", "bridge", lease_token=lease.lease_token)
    with sqlite3.connect(store.db_path) as con:  # lease vypršel → task zpět ve frontě
        con.execute("UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL "
                    "WHERE message_id=?", (mid,))
    assert store.accept(mid, "codex", "beru v okně") is True     # teď ho bere Nox
    assert store.lease_next("codex", skip_accepted=True) is None  # bridge už ne


def test_migration_marks_old_bridge_receipts_as_worker_owned(store: Store, tmp_path: Path):
    """Review Nox #391: starý receipt bridge (bez by_lease) nesmí po retry zablokovat bridge."""
    import sqlite3

    from agent_lease_mcp.store import BRIDGE_ACCEPT_NOTE

    old = store.send("michal", "codex", "starý úkol bridge", kind="task")
    lease = store.lease_next("codex", skip_accepted=True)
    store.accept(old, "codex", BRIDGE_ACCEPT_NOTE, lease_token=lease.lease_token)
    store.ack(old, "codex", DeliveryState.STARTED, lease_token=lease.lease_token)
    store.ack(old, "codex", DeliveryState.FAILED, lease_token=lease.lease_token)
    mine = store.send("michal", "codex", "úkol pro okno", kind="task")
    store.accept(mine, "codex", BRIDGE_ACCEPT_NOTE)  # stejný text, ale bez pokusu workeru
    with sqlite3.connect(store.db_path) as con:  # stará DB před sloupcem by_lease
        con.execute("ALTER TABLE receipts DROP COLUMN by_lease")

    migrated = Store(store.db_path, settings=Settings(store.db_path, "codex", 1800))
    assert migrated.retry(old, "codex")
    again = migrated.lease_next("codex", skip_accepted=True)
    assert again is not None and again.message_id == old
    with sqlite3.connect(store.db_path) as con:
        flags = dict(con.execute("SELECT message_id, by_lease FROM receipts"))
    assert flags == {old: 1, mine: 0}


def test_concurrent_open_of_old_schema_migrates_once(store: Store):
    """Review Nox #395: dva Store() nad starou DB nesmí oba zkusit ALTER."""
    import sqlite3
    import threading

    with sqlite3.connect(store.db_path) as con:
        con.execute("ALTER TABLE receipts DROP COLUMN by_lease")
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def open_store():
        barrier.wait()
        try:
            Store(store.db_path, settings=Settings(store.db_path, "codex", 1800))
        except BaseException as exc:  # noqa: BLE001 - test sbírá jakoukoli chybu
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_presence_stops_showing_limited_after_provider_reset(store: Store):
    """Zaseklé „rate-limited“: reset od poskytovatele prošel, worker čeká na další úkol."""
    store.heartbeat("claude-code-bridge", status="bridge: waiting")
    store.report_provider_state("claude-code-bridge", "limited", "cli_error",
                                resets_at=time.time() + 3600, reason="rate_limit_error")
    peer = next(p for p in store.peers() if p["agent"] == "claude-code-bridge")
    assert peer["presence"] == "rate-limited"
    store.report_provider_state("claude-code-bridge", "limited", "cli_error",
                                resets_at=time.time() - 60, reason="rate_limit_error")
    peer = next(p for p in store.peers() if p["agent"] == "claude-code-bridge")
    assert peer["presence"] == "recovering"
    store.report_provider_state("claude-code-bridge", "limited", "cli_error",
                                reason="unknown reset")  # bez resetu se nic nevymýšlí
    peer = next(p for p in store.peers() if p["agent"] == "claude-code-bridge")
    assert peer["presence"] == "rate-limited"
