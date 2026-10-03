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
