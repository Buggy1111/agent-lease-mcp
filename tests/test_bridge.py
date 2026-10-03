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
    assert any("codex viděl" in t for t in texts)           # automatický příjem
    assert any(t.startswith("✔ beru") for t in texts)        # vědomé potvrzení dřív než výsledek
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


def test_unauthorized_sender_cannot_enqueue(store: Store):
    with pytest.raises(PermissionError):
        store.send("unconfigured-x-1", "codex", "rm", kind="task")


def test_overdue_lists_unaccepted(store: Store):
    mid = store.send("michal", "codex", "ahoj", kind="chat")
    assert store.overdue(0)[0]["problem"] == "nedoručeno"
    store.mark_seen("codex", [mid])
    assert store.overdue(0)[0]["problem"] == "viděno, nepotvrzeno"
    store.accept(mid, "codex")
    assert store.overdue(0) == []


def test_doctor_runs(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "d" / "room.db"))
    monkeypatch.setenv("AGENT_NAME", "codex")
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_lease_mcp.doctor import run_doctor
    code = run_doctor()
    out = capsys.readouterr().out
    assert "identita agenta: codex" in out and code in (0, 1)
    assert os.path.isdir(tmp_path / "d")
