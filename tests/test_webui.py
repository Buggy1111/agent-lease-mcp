"""HTTP kontrakt lokálního live chatu."""

from __future__ import annotations

import json
import stat
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from agent_lease_mcp.config import Settings
from agent_lease_mcp.store import Store
from agent_lease_mcp.webui import load_or_create_token, make_handler


@pytest.fixture
def live_chat(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    token = "test-token"
    try:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(store, settings, token, 0)
        )
    except PermissionError:
        pytest.skip("sandbox této relace zakazuje i loopback socket")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], token, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(port: int, method: str, path: str, *, token: str | None = None, body=None):
    headers = {}
    payload = None
    if token:
        headers["X-Agent-Lease-Token"] = token
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


def test_index_and_snapshot_require_token(live_chat):
    port, token, _ = live_chat

    assert request(port, "GET", "/")[0] == 403
    assert request(port, "GET", f"/?token={token}")[0] == 200
    assert request(port, "GET", "/api/snapshot")[0] == 403
    assert request(port, "GET", "/api/snapshot", token=token)[0] == 200


def test_send_is_authenticated_and_sender_is_not_client_controlled(live_chat):
    port, token, store = live_chat
    body = {"text": "zkontroluj", "kind": "task", "to": "codex", "agent": "attacker"}

    assert request(port, "POST", "/api/send", body=body)[0] == 403
    status, raw = request(port, "POST", "/api/send", token=token, body=body)

    assert status == 200
    message_id = json.loads(raw)["id"]
    message = next(item for item in store.inbox() if item["id"] == message_id)
    assert message["agent"] == "michal"
    assert message["recipient"] == "codex"
    assert message["kind"] == "task"


def test_post_rejects_foreign_origin(live_chat):
    port, token, _ = live_chat
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    payload = json.dumps({"text": "x", "kind": "chat", "to": "codex"})
    connection.request(
        "POST", "/api/send", body=payload,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-Agent-Lease-Token": token,
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
