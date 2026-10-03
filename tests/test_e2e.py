"""
End-to-end: skutečné podprocesy (hooky, CLI) přes skutečný broker.
Simuluje večer, který projekt řeší: Michal zadá práci, agenti ji musí vidět,
potvrdit, nešlapat si po souborech a při vyčerpaném limitu ji předat dál.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_lease_mcp.broker import RemoteStore, serve
from agent_lease_mcp.config import Settings, parse_fallbacks

SRC = str(Path(__file__).resolve().parent.parent / "src")


@pytest.fixture
def room(tmp_path: Path):
    db, sock = tmp_path / "room.db", tmp_path / "b.sock"
    fb = "codex=claude-code;claude-code=codex"
    settings = Settings(db, "michal", 1800, fallbacks=parse_fallbacks(fb), failover_grace=0)
    server = serve(settings, sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env_base = {"PATH": os.environ["PATH"], "PYTHONPATH": SRC, "AGENT_LEASE_DB": str(db),
                "AGENT_LEASE_SOCKET": str(sock), "AGENT_LEASE_FALLBACKS": fb,
                "AGENT_LEASE_FAILOVER_GRACE": "0", "HOME": str(tmp_path)}

    def run(module: str, agent: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", module, *args], input=stdin, text=True,
                              capture_output=True, env={**env_base, "AGENT_NAME": agent},
                              timeout=30, check=False)

    yield RemoteStore(settings, sock), run, tmp_path
    server.shutdown()
    server.server_close()


def hook(run, agent, event="UserPromptSubmit"):
    return run("agent_lease_mcp.lifecycle", agent, stdin=json.dumps({"hook_event_name": event}))


def test_whole_evening(room):
    store, run, tmp = room
    for a in ("codex", "claude-code"):  # oba agenti se hlásí (SessionStart)
        assert hook(run, a, "SessionStart").returncode == 0

    # 1) Michal zadá task. Codex ho uvidí při dalším promptu — včetně výzvy k potvrzení.
    mid = store.send("michal", "codex", "Oprav padající test v deploy/", kind="task")
    out = hook(run, "codex").stdout
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "ČEKÁ NA TVOJE POTVRZENÍ" in ctx and "nedůvěryhodná citace" in ctx

    # 2) „Viděl" se zapsalo samo, Michal ho vidí v místnosti, i když agent nic neudělal.
    assert any("codex viděl" in m["text"] for m in store.inbox() if m["recipient"] == "michal")
    # a výzva se opakuje, dokud agent nepotvrdí
    assert "ČEKÁ NA TVOJE POTVRZENÍ" in hook(run, "codex").stdout

    # 3) Potvrzení → výzva mizí, Michal dostane „beru".
    acc = run("agent_lease_mcp.cli", "codex", "accept", str(mid), "--note", "opravím test")
    assert acc.returncode == 0, acc.stderr
    assert "ČEKÁ" not in hook(run, "codex").stdout
    assert any("beru" in m["text"] and "opravím test" in m["text"]
               for m in store.inbox() if m["recipient"] == "michal")

    # 4) Codex edituje soubor → nájem; Claude Code na něj nesmí (exit 2), uvolnění pomůže.
    target = str(tmp / "deploy" / "x.sh")
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": target}})
    assert run("agent_lease_mcp.guard", "codex", stdin=payload).returncode == 0
    blocked = run("agent_lease_mcp.guard", "claude-code", stdin=payload)
    assert blocked.returncode == 2 and "codex" in blocked.stderr
    run("agent_lease_mcp.lifecycle", "codex", stdin="{}")  # (kontext) beze změny
    assert subprocess.run([sys.executable, "-c",
                           "from agent_lease_mcp.lifecycle import release_main; release_main()"],
                          env={"PATH": os.environ["PATH"], "PYTHONPATH": SRC,
                               "AGENT_LEASE_DB": str(tmp / "room.db"), "AGENT_NAME": "codex",
                               "AGENT_LEASE_SOCKET": str(tmp / "b.sock")},
                          check=False).returncode == 0
    assert run("agent_lease_mcp.guard", "claude-code", stdin=payload).returncode == 0

    # 5) Další task pro codex a codexu dojde limit → úkol putuje ke claude-code.
    mid2 = store.send("michal", "codex", "Napiš dokumentaci", kind="task")
    lim = run("agent_lease_mcp.cli", "codex", "limit", "--until", "+2h", "--reason", "týdenní limit")
    assert lim.returncode == 0, lim.stderr
    assert any(j["id"] == mid2 for j in store.jobs("claude-code"))
    ctx2 = json.loads(hook(run, "claude-code").stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Napiš dokumentaci" in ctx2 and "Převzal jsi úkol" in ctx2

    # 6) overdue/limits fungují
    assert "týdenní limit" in run("agent_lease_mcp.cli", "claude-code", "limits").stdout


def test_hook_does_not_leave_broker_waiting(room):
    """Hook musí být rychlý — běží před každým promptem."""
    _, run, _ = room
    start = time.monotonic()
    for _ in range(5):
        hook(run, "codex")
    assert (time.monotonic() - start) / 5 < 1.5
