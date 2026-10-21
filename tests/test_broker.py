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


def test_dropped_reply_is_a_connection_error_not_a_json_error(tmp_path: Path):
    import socket as socketlib

    sock = tmp_path / "drop.sock"
    server = socketlib.socket(socketlib.AF_UNIX)
    server.bind(str(sock))
    server.listen(1)

    def drop():
        conn, _ = server.accept()
        conn.recv(65536)
        conn.close()  # broker spadl po přijetí požadavku

    threading.Thread(target=drop, daemon=True).start()
    remote = RemoteStore(Settings(tmp_path / "room.db", "codex", 1800), sock)
    with pytest.raises(ConnectionResetError):
        remote.peers()
    server.close()


def test_every_store_method_used_through_the_broker_is_proxied():
    """Regrese 21. 10.: codex-wake volal task_summary, broker ho neznal → pád ve smyčce."""
    import re

    import agent_lease_mcp
    from agent_lease_mcp.broker import METHODS
    from agent_lease_mcp.store import Store

    store_methods = {m for m in dir(Store) if not m.startswith("_")}
    package = Path(agent_lease_mcp.__file__).parent
    missing = {}
    for module in package.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        if "open_store" not in source or module.name == "broker.py":
            continue
        used = set(re.findall(r"\b_?store\.(\w+)\(", source))
        gaps = sorted(m for m in used if m in store_methods and m not in METHODS)
        if gaps:
            missing[module.name] = gaps
    assert missing == {}


def test_codex_wake_recovery_path_works_over_the_broker(broker, tmp_path, monkeypatch):
    import os
    import stat

    from agent_lease_mcp.codex_wake import CodexWake

    settings, sock = broker
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "codex"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    remote = RemoteStore(settings, sock)
    wake = CodexWake(remote, "nox", tmp_path / "cursor")
    wake.step()  # baseline
    remote.report_provider_state("codex-bridge", "limited", "test")
    remote.report_provider_state("codex-bridge", "available", "test")
    wake.step()  # dřív AttributeError: task_summary


def test_open_store_with_explicit_socket_never_falls_back(tmp_path: Path, monkeypatch):
    """Review Nox #438: vyžádaný broker (sandbox) nesmí potichu přejít na přímé SQLite."""
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("AGENT_LEASE_SOCKET", str(tmp_path / "missing.sock"))
    assert isinstance(open_store(), RemoteStore)


def test_open_store_waits_for_a_restarting_broker(tmp_path: Path, monkeypatch):
    """Socket existuje, broker zrovna neodpovídá (restart) → počkat, ne přejít na SQLite."""
    import time

    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.delenv("AGENT_LEASE_SOCKET", raising=False)
    settings = Settings.from_env()
    from agent_lease_mcp.broker import default_socket_path
    sock = default_socket_path(settings)
    first = serve(settings, sock)
    first.server_close()  # zastaralý socket: soubor je, nikdo neposlouchá
    servers = []

    def restart():
        time.sleep(0.5)
        servers.append(serve(settings, sock))
        servers[0].serve_forever()

    threading.Thread(target=restart, daemon=True).start()
    try:
        assert isinstance(open_store(settings), RemoteStore)
    finally:
        if servers:
            servers[0].shutdown()
            servers[0].server_close()


def test_open_store_falls_back_when_a_stale_socket_never_answers(tmp_path: Path, monkeypatch):
    from agent_lease_mcp.broker import default_socket_path
    from agent_lease_mcp.store import Store

    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.delenv("AGENT_LEASE_SOCKET", raising=False)
    settings = Settings.from_env()
    server = serve(settings, default_socket_path(settings))
    server.server_close()
    assert default_socket_path(settings).exists()
    assert isinstance(open_store(settings, restart_wait=0.3), Store)


def test_doctor_reads_the_room_through_the_broker(broker, monkeypatch, capsys):
    """Review Nox #448: doctor šel přímým Store() (migrace + retence = zápis) mimo broker."""
    import json

    from agent_lease_mcp import doctor

    settings, sock = broker
    monkeypatch.setenv("AGENT_LEASE_DB", str(settings.db_path))
    monkeypatch.setenv("AGENT_LEASE_SOCKET", str(sock))
    monkeypatch.setenv("AGENT_NAME", "codex")
    import agent_lease_mcp.broker as broker_mod

    real_open = broker_mod.open_store
    opened = []

    def spy(s=None, **kw):
        store = real_open(s, **kw)
        opened.append(type(store).__name__)
        return store

    monkeypatch.setattr(broker_mod, "open_store", spy)
    doctor.run_doctor(json_output=True)
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert opened == ["RemoteStore"]
    assert not [c for c in checks if "could not be inspected" in c["message"]]
    assert any(c["id"] == "activation_baselines" for c in checks)
