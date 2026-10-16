"""HTTP kontrakt lokálního live chatu."""

from __future__ import annotations

import json
import socket
import stat
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Thread

import pytest

from agent_lease_mcp import webui
from agent_lease_mcp.config import Settings, human_name
from agent_lease_mcp.store import Store
from agent_lease_mcp.webui import (
    MAX_SSE_CONNECTIONS,
    PAGE_HTML,
    REQUEST_SOCKET_TIMEOUT,
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    _sse_heartbeat,
    load_or_create_token,
    make_handler,
)


def call_handler(store: Store, settings: Settings, token: str, method: str, path: str, *,
                 headers: dict | None = None, body: dict | None = None, handler_type=None):
    """Prožene request skutečným handlerem bez socketu (Codex sandbox ho zakazuje)."""
    handler_type = handler_type or make_handler(store, settings, token, 8765)
    handler = object.__new__(handler_type)
    raw = json.dumps(body).encode() if body is not None else b""
    handler.path = path
    handler.headers = dict(headers or {})
    if raw:
        handler.headers.setdefault("Content-Length", str(len(raw)))
        handler.headers.setdefault("Content-Type", "application/json")
    handler.rfile = BytesIO(raw)
    handler.wfile = BytesIO()
    handler.response_code = None
    handler.send_response = lambda code: setattr(handler, "response_code", code)
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    getattr(handler, f"do_{method}")()
    return handler.response_code, handler.wfile.getvalue()


def authenticated_handler(store: Store, settings: Settings, token: str, session_id: str = "session"):
    handler_type = make_handler(store, settings, token, 8765)
    handler_type.sessions[session_id] = time.monotonic() + SESSION_MAX_AGE_SECONDS
    return handler_type


def exchange_session(port: int, token: str) -> tuple[int, str | None]:
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(
        "POST",
        "/session",
        headers={
            "X-Agent-Lease-Token": token,
            "Origin": f"http://127.0.0.1:{port}",
            "Content-Length": "0",
        },
    )
    response = connection.getresponse()
    cookie = response.getheader("Set-Cookie")
    response.read()
    status = response.status
    connection.close()
    return status, cookie


@pytest.fixture
def live_chat(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    token = "test-token"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, settings, token, 0))
        server.RequestHandlerClass = make_handler(store, settings, token, server.server_address[1])
    except PermissionError:
        pytest.skip("sandbox této relace zakazuje i loopback socket")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, cookie = exchange_session(server.server_address[1], token)
        assert status == 204 and cookie is not None
        session_id = cookie.split(";", 1)[0].split("=", 1)[1]
        yield server.server_address[1], token, session_id, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(port: int, method: str, path: str, *, token: str | None = None, body=None):
    headers = {}
    payload = None
    if token:
        headers["Cookie"] = f"{SESSION_COOKIE}={token}"
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def test_index_is_public_but_snapshot_requires_session_cookie(live_chat):
    port, token, session_id, _ = live_chat

    assert request(port, "GET", "/")[0] == 200
    assert request(port, "GET", "/api/snapshot")[0] == 403
    assert request(port, "GET", f"/api/snapshot?token={token}")[0] == 403
    assert request(port, "GET", "/api/snapshot", token=session_id)[0] == 200


def test_fragment_token_is_exchanged_once_for_httponly_cookie(live_chat):
    port, token, _, _ = live_chat
    status, cookie = exchange_session(port, token)

    assert status == 204
    assert cookie is not None
    assert token not in cookie
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert cookie.endswith(
        f"; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_MAX_AGE_SECONDS}"
    )
    assert "location.hash" in PAGE_HTML
    assert "history.replaceState" in PAGE_HTML
    assert "/api/events?token=" not in PAGE_HTML


def test_session_exchange_rejects_mismatched_host(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    status, _ = call_handler(
        store,
        settings,
        "secret",
        "POST",
        "/session",
        headers={
            "X-Agent-Lease-Token": "secret",
            "Origin": "http://localhost:8765",
            "Host": "evil.example",
        },
    )
    assert status == 403


def test_master_token_and_expired_sessions_are_not_valid_cookies(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    handler_type = make_handler(store, settings, "master-secret", 8765)
    handler_type.sessions["expired"] = time.monotonic() - 1

    for cookie_value in ("master-secret", "expired"):
        status, _ = call_handler(
            store,
            settings,
            "master-secret",
            "GET",
            "/api/snapshot",
            headers={"Cookie": f"{SESSION_COOKIE}={cookie_value}"},
            handler_type=handler_type,
        )
        assert status == 403


def test_sse_connection_limit_returns_service_unavailable(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    handler_type = authenticated_handler(store, settings, "secret")
    slots = handler_type.sse_slots
    acquired = 0
    try:
        for _ in range(MAX_SSE_CONNECTIONS):
            assert slots.acquire(blocking=False)
            acquired += 1
        status, body = call_handler(
            store,
            settings,
            "secret",
            "GET",
            "/api/events?since=0",
            headers={"Cookie": f"{SESSION_COOKIE}=session"},
            handler_type=handler_type,
        )
        assert status == 503
        assert body == b"too many event streams"
    finally:
        for _ in range(acquired):
            slots.release()


def test_handler_sets_socket_timeout_and_sse_heartbeat(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    handler = object.__new__(make_handler(store, settings, "secret", 8765))
    server_socket, client_socket = socket.socketpair()
    handler.request = server_socket
    try:
        handler.setup()
        assert server_socket.gettimeout() == REQUEST_SOCKET_TIMEOUT
        assert _sse_heartbeat() == b": heartbeat\n\n"
    finally:
        handler.rfile.close()
        handler.wfile.close()
        server_socket.close()
        client_socket.close()


def test_send_is_authenticated_and_sender_is_not_client_controlled(live_chat):
    port, _, session_id, store = live_chat
    body = {"text": "zkontroluj", "kind": "task", "to": "codex", "agent": "attacker"}

    assert request(port, "POST", "/api/send", body=body)[0] == 403
    status, raw = request(port, "POST", "/api/send", token=session_id, body=body)

    assert status == 200
    message_id = json.loads(raw)["id"]
    message = next(item for item in store.inbox() if item["id"] == message_id)
    assert message["agent"] == human_name(store.db_path)
    assert message["recipient"] == "codex"
    assert message["kind"] == "task"


def test_post_rejects_foreign_origin(live_chat):
    port, _, session_id, _ = live_chat
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    payload = json.dumps({"text": "x", "kind": "chat", "to": "codex"})
    connection.request(
        "POST", "/api/send", body=payload,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "Cookie": f"{SESSION_COOKIE}={session_id}",
            "Origin": "https://evil.example",
        },
    )
    assert connection.getresponse().status == 403
    connection.close()


def test_token_file_is_private(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)

    first = load_or_create_token(settings)
    second = load_or_create_token(settings)

    assert first == second
    mode = stat.S_IMODE((tmp_path / "webui.token").stat().st_mode)
    assert mode == 0o600


def test_existing_token_permissions_are_repaired(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    path = tmp_path / "webui.token"
    path.write_text("existing")
    path.chmod(0o644)

    assert load_or_create_token(settings) == "existing"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_startup_output_never_contains_token(tmp_path: Path, monkeypatch, capsys):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    (tmp_path / "webui.token").write_text("stdout-secret")

    class FakeServer:
        daemon_threads = False

        def __init__(self, *_args):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(webui.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(webui, "ThreadingHTTPServer", FakeServer)

    assert webui.run(port=9876) == 0
    output = capsys.readouterr().out
    assert "stdout-secret" not in output
    assert "http://127.0.0.1:9876/" in output
    assert "--open" in output


def test_open_passes_fragment_to_browser_without_printing_it(tmp_path: Path, monkeypatch, capsys):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    (tmp_path / "webui.token").write_text("browser-secret")
    opened = []

    class FakeServer:
        daemon_threads = False

        def __init__(self, *_args):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(webui.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(webui, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(webui.webbrowser, "open", lambda url: opened.append(url) or True)

    assert webui.run(port=9876, open_browser=True) == 0
    captured = capsys.readouterr()
    assert opened == ["http://127.0.0.1:9876/#token=browser-secret"]
    assert "browser-secret" not in captured.out
    assert "browser-secret" not in captured.err


def test_open_failure_prints_one_explicit_secret_warning(tmp_path: Path, monkeypatch, capsys):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    (tmp_path / "webui.token").write_text("fallback-secret")

    class FakeServer:
        daemon_threads = False

        def __init__(self, *_args):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(webui.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(webui, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(webui.webbrowser, "open", lambda _url: False)

    assert webui.run(port=9876, open_browser=True) == 0
    captured = capsys.readouterr()
    assert "fallback-secret" not in captured.out
    assert captured.err.count("fallback-secret") == 1
    assert "WARNING" in captured.err


def test_handlers_enforce_token_origin_and_human_sender_without_socket(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    token = "secret"
    handler_type = authenticated_handler(store, settings, token)
    body = {"text": "proveď kontrolu", "kind": "task", "to": "codex", "agent": "fake"}

    assert call_handler(store, settings, token, "POST", "/api/send", body=body)[0] == 403
    assert call_handler(
        store, settings, token, "POST", "/api/send",
        headers={"Cookie": f"{SESSION_COOKIE}=session", "Origin": "https://evil.example"},
        body=body,
        handler_type=handler_type,
    )[0] == 403
    status, raw = call_handler(
        store, settings, token, "POST", "/api/send",
        headers={
            "Cookie": f"{SESSION_COOKIE}=session",
            "Origin": "http://localhost:8765",
        },
        body=body,
        handler_type=handler_type,
    )

    assert status == 200
    assert json.loads(raw)["id"]
    message = store.latest_messages(1)[0]
    assert message["agent"] == human_name(store.db_path)
    assert message["recipient"] == "codex"
    assert message["kind"] == "task"


def test_snapshot_handler_returns_structured_room_without_socket(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    store.send("claude-code", "codex", "ahoj", kind="task")
    handler_type = authenticated_handler(store, settings, "secret")

    status, raw = call_handler(
        store, settings, "secret", "GET", "/api/snapshot",
        headers={"Cookie": f"{SESSION_COOKIE}=session"},
        handler_type=handler_type,
    )

    assert status == 200
    payload = json.loads(raw)
    assert payload["me"] == human_name(store.db_path)
    assert payload["messages"][0]["text"] == "ahoj"
    assert payload["messages"][0]["state"] == "pending"


def test_initial_snapshot_returns_newest_200_messages(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    for i in range(205):
        store.say("claude-code", str(i))
    handler_type = authenticated_handler(store, settings, "secret")

    status, raw = call_handler(
        store, settings, "secret", "GET", "/api/snapshot?since=0",
        headers={"Cookie": f"{SESSION_COOKIE}=session"},
        handler_type=handler_type,
    )

    assert status == 200
    messages = json.loads(raw)["messages"]
    assert len(messages) == 200
    assert messages[0]["text"] == "5"
    assert messages[-1]["text"] == "204"


def test_authenticated_human_can_cancel_and_retry_task(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    message_id = store.send("michal", "codex", "práce", kind="task")
    handler_type = authenticated_handler(store, settings, "secret")
    headers = {
        "Cookie": f"{SESSION_COOKIE}=session",
        "Origin": "http://localhost:8765",
    }

    status, raw = call_handler(
        store, settings, "secret", "POST", "/api/cancel",
        headers=headers, body={"id": message_id, "to": "codex"}, handler_type=handler_type,
    )
    assert status == 200
    assert json.loads(raw)["state"] == "cancelled"
    assert store.jobs("codex")[0]["state"] == "cancelled"

    failed_id = store.send("michal", "codex", "znovu", kind="task")
    delivery = store.lease_next("codex")
    assert delivery is not None
    assert store.ack(failed_id, "codex", "failed", lease_token=delivery.lease_token)
    status, raw = call_handler(
        store, settings, "secret", "POST", "/api/retry",
        headers=headers, body={"id": failed_id, "to": "codex"}, handler_type=handler_type,
    )
    assert status == 200
    assert json.loads(raw)["state"] == "pending"
    assert store.jobs("codex")[0]["state"] == "pending"


def test_web_ui_creates_owner_only_human_name_file(tmp_path, monkeypatch):
    """Jméno člověka zakládá web UI vedle DB (0600); agent ho přes env nezmění."""
    import stat

    from agent_lease_mcp.config import Settings
    from agent_lease_mcp.webui import ensure_human_name

    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    name = ensure_human_name(Settings.from_env())
    f = tmp_path / "human-name"
    assert f.read_text() == name
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
