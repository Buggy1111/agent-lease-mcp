from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent_lease_mcp.broker import RemoteStore, open_store, ping, serve
from agent_lease_mcp.config import Settings


@pytest.fixture
def broker(tmp_path: Path):
    settings = Settings(tmp_path / "room.db", "codex", 1800)
    sock = tmp_path / "b.sock"
    server = serve(settings, sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield settings, sock
    server.shutdown()
    server.server_close()


def test_socket_is_private_and_alive(broker):
    _, sock = broker
    assert oct(sock.stat().st_mode & 0o777) == "0o600"
    assert ping(sock)


def test_remote_store_mirrors_store_including_dataclasses(broker):
    settings, sock = broker
    remote = RemoteStore(settings, sock)
    res = remote.claim(["/tmp/x.txt"], agent="codex")
    assert res.ok and res.granted
    other = remote.claim(["/tmp/x.txt"], agent="claude-code")
    assert not other.ok and other.conflicts[0].agent == "codex"
    mid = remote.send("michal", "codex", "task", kind="task")
    d = remote.lease_next("codex")
    assert d.message_id == mid and d.kind.value == "task" and d.lease_token
    assert remote.ack(mid, "codex", "succeeded", lease_token=d.lease_token)


def test_remote_store_resolves_relative_paths_in_client_cwd(broker, tmp_path, monkeypatch):
    settings, sock = broker
    client_cwd = tmp_path / "client"
    client_cwd.mkdir()
    monkeypatch.chdir(client_cwd)
    remote = RemoteStore(settings, sock)

    assert remote.claim(["src/a.py"], agent="codex").granted == [
        str(client_cwd / "src/a.py")
    ]
    mid = remote.send("michal", "codex", "task", kind="task", paths=["src/b.py"])

    from agent_lease_mcp.store import Store
    with Store(settings=settings)._connect() as con:
        assert con.execute(
            "SELECT path FROM task_paths WHERE message_id=?", (mid,)
        ).fetchone()[0] == str(client_cwd / "src/b.py")


def test_worker_activation_roundtrips_over_broker(broker):
    settings, sock = broker
    remote = RemoteStore(settings, sock)
    from agent_lease_mcp.store import Store

    direct = Store(settings=settings)
    old = direct.send("michal", "codex", "old", kind="task")

    assert remote.worker_activation("codex") == old
    assert remote.worker_activation("codex", include_existing=True) == old


def test_provider_state_roundtrips_over_broker(broker):
    settings, sock = broker
    remote = RemoteStore(settings, sock)
    remote.report_provider_state("codex", "available", "codex_app_server", observed_at=123)

    assert remote.provider_state("codex") == {
        "agent": "codex", "state": "available", "source": "codex_app_server",
        "observed_at": 123.0, "resets_at": None, "reason": "",
    }
    assert remote.clear_provider_state("codex")
    assert remote.provider_state("codex") is None


def test_errors_cross_the_socket(broker):
    settings, sock = broker
    remote = RemoteStore(settings, sock)
    with pytest.raises(PermissionError):
        remote.send("unconfigured-x-1", "codex", "rm", kind="task")
    with pytest.raises(AttributeError):
        remote.__getattr__("_connect")  # jen povolené metody


def test_unknown_method_is_refused(broker):
    import json
    import socket
    _, sock = broker
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(sock))
        s.sendall(b'{"method":"_connect","args":[]}\n')
        reply = json.loads(s.makefile().readline())
    assert reply["ok"] is False and reply["error"] == "PermissionError"


def test_open_store_falls_back_to_sqlite_without_broker(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "x.db"))
    monkeypatch.delenv("AGENT_LEASE_SOCKET", raising=False)
    from agent_lease_mcp.store import Store
    assert isinstance(open_store(), Store)


def test_second_broker_refuses_and_stale_socket_is_cleaned(tmp_path: Path):
    settings = Settings(tmp_path / "room.db", "codex", 1800)
    sock = tmp_path / "b.sock"
    first = serve(settings, sock)
    threading.Thread(target=first.serve_forever, daemon=True).start()
    with pytest.raises(RuntimeError):
        serve(settings, sock)
    first.shutdown()
    first.server_close()
    again = serve(settings, sock)  # soubor zůstal, nikdo neposlouchá → uklidit
    again.server_close()


def test_broker_refuses_human_privileges_from_socket_clients(tmp_path):
    """Security review: přes broker chodí jen agenti — actor/viewer=None (práva
    člověka) se odmítne."""
    from agent_lease_mcp.broker import _NEEDS_ACTOR, _NEEDS_VIEWER

    assert {"retry", "cancel"} <= _NEEDS_ACTOR and "jobs" in _NEEDS_VIEWER


def test_remote_store_waits_for_a_restarting_broker(tmp_path: Path):
    """Restart brokeru nesmí shodit bridge ani budík (ConnectionRefusedError)."""
    import time

    settings = Settings(tmp_path / "room.db", "codex", 1800)
    sock = tmp_path / "b.sock"
    first = serve(settings, sock)
    threading.Thread(target=first.serve_forever, daemon=True).start()
    remote = RemoteStore(settings, sock, connect_retry=5)
    assert remote.peers() == []
    first.shutdown()
    first.server_close()
    servers = []

    def restart():
        time.sleep(0.5)
        servers.append(serve(settings, sock))
        servers[0].serve_forever()

    threading.Thread(target=restart, daemon=True).start()
    try:
        assert remote.peers() == []  # počká na nový broker místo pádu
    finally:
        if servers:
            servers[0].shutdown()
            servers[0].server_close()


def test_remote_store_gives_up_when_broker_stays_down(tmp_path: Path):
    import time

    settings = Settings(tmp_path / "room.db", "codex", 1800)
    remote = RemoteStore(settings, tmp_path / "missing.sock", connect_retry=0.3)
    started = time.monotonic()
    with pytest.raises(FileNotFoundError):
        remote.peers()
    assert time.monotonic() - started < 2
