from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from agent_lease_mcp.bridge import Bridge, parse_retry_after
from agent_lease_mcp.config import Settings
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


def test_prompt_is_one_argument_not_shell(store: Store, tmp_path: Path):
    marker = tmp_path / "pwned"
    store.send("michal", "codex", f"x; touch {marker}", kind="task")
    cmd = fake(tmp_path, "import sys; print(len(sys.argv))")
    Bridge(store, "codex", cmd).step()
    assert not marker.exists()
    assert any(m["text"].endswith("2") for m in store.inbox() if "Hotovo" in m["text"])


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


def test_hourly_cap(store: Store, tmp_path: Path):
    for i in range(3):
        store.send("michal", "codex", f"t{i}", kind="task")
    b = Bridge(store, "codex", fake(tmp_path, "print('ok')"), max_per_hour=2)
    assert b.step() and b.step()
    assert not b.step()  # třetí čeká na další hodinu
    assert sum(j["state"] == "pending" for j in store.jobs("codex")) == 1


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
