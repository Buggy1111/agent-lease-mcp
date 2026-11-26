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


@pytest.fixture(autouse=True)
def _not_wsl(monkeypatch):
    """Testy nesmí ve WSL otevírat skutečný prohlížeč ve Windows (open_url → cmd.exe)."""
    monkeypatch.setattr(webui, "_is_wsl", lambda: False)


def call_handler(store: Store, settings: Settings, token: str, method: str, path: str, *,
                 headers: dict | None = None, body: dict | None = None, handler_type=None):
    """Prožene request skutečným handlerem bez socketu (Codex sandbox ho zakazuje)."""
    handler_type = handler_type or make_handler(store, settings, token, 8765)
    handler = object.__new__(handler_type)
    raw = json.dumps(body).encode() if body is not None else b""
    handler.path = path
    handler.headers = dict(headers or {})
    handler.headers.setdefault("Host", "localhost:8765")
    handler.headers.setdefault("X-Agent-Lease-Key", "key")   # klíč z localStorage, jako skutečný prohlížeč
    if method == "POST":
        handler.headers.setdefault("Origin", "http://localhost:8765")
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
    handler_type.session_keys[session_id] = "key"
    return handler_type


def test_unconfirmed_pause_returns_an_error(tmp_path: Path, monkeypatch):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    handler = authenticated_handler(store, settings, "secret")

    def cannot_confirm(*_args):
        raise TimeoutError("launcher stuck")

    monkeypatch.setattr(store, "pause", cannot_confirm)
    status, body = call_handler(store, settings, "secret", "POST", "/api/pause",
                                headers={"Cookie": f"{SESSION_COOKIE}=session"},
                                body={"reason": "stop"}, handler_type=handler)
    assert status == 503 and b"could not be confirmed" in body


SESSION_KEYS: dict[int, str] = {}


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
    SESSION_KEYS[port] = response.getheader("X-Agent-Lease-Key") or ""
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
        headers["Cookie"] = f"{webui.session_cookie_name(port)}={token}"
        headers["X-Agent-Lease-Key"] = SESSION_KEYS.get(port, "")
    if method == "POST":
        headers["Origin"] = f"http://127.0.0.1:{port}"   # jako prohlížeč na stejném původu
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
    assert cookie.startswith(f"{webui.session_cookie_name(port)}=")
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
    code = (tmp_path / "webui.login").read_text().split()[0]
    assert opened == [f"http://127.0.0.1:9876/#token={code}"]   # jednorázový kód, ne hlavní token
    assert "browser-secret" not in captured.out + captured.err + opened[0]


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
    code = (tmp_path / "webui.login").read_text().split()[0]
    out = captured.out + captured.err                      # audit 26. 11.: nic tajného na výstup
    assert "fallback-secret" not in out and code not in out and "web-login" in captured.err


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


def test_only_authenticated_human_approves_checkpoint_resume(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    mid = store.send("michal", "codex", "práce", kind="task")
    d = store.lease_next("codex", start=True)
    store.checkpoint(mid, "codex", d.lease_token, "krok", "", "h", "f")
    assert store.complete(mid, "codex", d.lease_token, "needs_review", "zadrženo",
                          error="checkpoint: workspace changed, not started")
    handler_type = authenticated_handler(store, settings, "secret")
    body = {"id": mid, "to": "codex", "reason": "diff zkontrolován"}
    status, _ = call_handler(store, settings, "secret", "POST", "/api/approve-resume",
                             headers={"Origin": "http://localhost:8765"}, body=body,
                             handler_type=handler_type)
    assert status in (401, 403)
    headers = {"Cookie": f"{SESSION_COOKIE}=session", "Origin": "http://localhost:8765"}
    status, raw = call_handler(store, settings, "secret", "POST", "/api/approve-resume",
                               headers=headers, body={"id": mid, "to": "codex"},
                               handler_type=handler_type)
    assert status == 400                                       # bez důvodu ne
    status, raw = call_handler(store, settings, "secret", "POST", "/api/approve-resume",
                               headers=headers, body=body, handler_type=handler_type)
    assert status == 200 and json.loads(raw)["resume_approved"] is True
    assert store.jobs("codex")[0]["state"] == "pending"


def test_live_room_event_carries_pause_state(live_chat):
    """Živá aktualizace musí nést stav pauzy, jinak web ukáže „Zastavit" místo „Pustit"."""
    port, _, session_id, store = live_chat
    store.pause("michal", "test")
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request("GET", f"/api/events?since=0&k={SESSION_KEYS[port]}",
                       headers={"Cookie": f"{webui.session_cookie_name(port)}={session_id}"})
    response = connection.getresponse()
    assert response.status == 200
    event = None
    data = b""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        line = response.fp.readline()
        if line.startswith(b"event:"):
            event = line.split(b":", 1)[1].strip()
        elif line.startswith(b"data:") and event == b"room":
            data = line.split(b":", 1)[1].strip()
            break
    connection.close()
    room = json.loads(data)
    assert room["paused"]["by"] == "michal"


def test_pause_banner_sits_on_top_and_ignores_missing_state():
    assert PAGE_HTML.index('id="pause"') < PAGE_HTML.index('id="topbar"')
    assert "if (p === undefined) return;" in PAGE_HTML
    assert "Pustit agenty znovu" in PAGE_HTML
    assert '<select id="to">' in PAGE_HTML  # komu se vybírá, nepíše


def test_human_review_actions_only_in_web_ui(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    head = "a" * 40
    review = store.review_open("claude-code", str(tmp_path), "B4", "codex", "", head)
    for _ in range(3):
        store.review_verdict("codex", review["id"], "changes_requested", head, "x")
        head = format(int(head, 16) + 1, "040x")
        store.review_submit("claude-code", review["id"], head)
    store.review_verdict("codex", review["id"], "changes_requested", head, "x")
    handler_type = authenticated_handler(store, settings, "secret")
    cookie = {"Cookie": f"{SESSION_COOKIE}=session", "Origin": "http://127.0.0.1:8765",
              "Host": "127.0.0.1:8765"}
    status, _ = call_handler(store, settings, "secret", "POST", "/api/review-grant",
                             headers=cookie, body={"id": review["id"]}, handler_type=handler_type)
    assert status == 409  # bez důvodu ne
    status, raw = call_handler(store, settings, "secret", "POST", "/api/review-grant",
                               headers=cookie, body={"id": review["id"], "reason": "ještě jednou"},
                               handler_type=handler_type)
    assert status == 200 and json.loads(raw)["state"] == "changes_requested"
    status, _ = call_handler(store, settings, "secret", "POST", "/api/review-close",
                             headers={}, body={"id": review["id"]})
    assert status == 403
    assert "renderReviews(r.reviews)" in PAGE_HTML


def test_review_card_shows_secret_scan_without_values():
    assert "function scanTag(v)" in PAGE_HTML and "${scanTag(v)}" in PAGE_HTML
    assert "tajné údaje" in PAGE_HTML


def test_only_authenticated_human_approves_a_fix(tmp_path: Path):
    """C1a: zápisový běh opravy povolí jen přihlášený člověk, s důvodem."""
    import os
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = tmp_path / "fix-runner.toml"
    policy.write_text(f'version = 1\nenabled = true\nrepositories = ["{repo}"]\nagents = ["codex"]\n')
    os.chmod(policy, 0o600)
    mid = store.send("michal", "codex", "oprav", kind="task", fix_repo=str(repo),
                     fix_base="a" * 40, fix_allowed=["src"])
    assert store.lease_next("codex") is None                    # brána ho zadrží
    handler_type = authenticated_handler(store, settings, "secret")
    body = {"id": mid, "to": "codex", "reason": "zadání zkontrolováno"}
    status, _ = call_handler(store, settings, "secret", "POST", "/api/approve-fix",
                             headers={"Origin": "http://localhost:8765"}, body=body,
                             handler_type=handler_type)
    assert status in (401, 403)
    headers = {"Cookie": f"{SESSION_COOKIE}=session", "Origin": "http://localhost:8765"}
    status, _ = call_handler(store, settings, "secret", "POST", "/api/approve-fix",
                             headers=headers, body={"id": mid, "to": "codex"},
                             handler_type=handler_type)
    assert status == 400                                       # bez důvodu ne
    status, raw = call_handler(store, settings, "secret", "POST", "/api/approve-fix",
                               headers=headers, body=body, handler_type=handler_type)
    assert status == 200 and json.loads(raw)["fix_approved"] is True
    assert store.lease_next("codex").message_id == mid


def test_ui_assets_are_a_fixed_list_without_path_traversal(tmp_path: Path):
    """Neonové UI (21. 11.): portréty a ikona z pevného seznamu, nic jiného ze souborového systému."""
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    for path, magic in [("/assets/ada.webp", b"RIFF"), ("/assets/nox.webp", b"RIFF"),
                        ("/favicon.svg", b"<svg"), ("/favicon.ico", b"<svg")]:
        status, body = call_handler(store, settings, "secret", "GET", path)
        assert status == 200 and body.startswith(magic), path
    for path in ("/assets/../webui.py", "/assets/ada.png", "/assets/", "/assets/%2e%2e/store.py"):
        status, _ = call_handler(store, settings, "secret", "GET", path)
        assert status != 200, path
    assert 'href="/favicon.svg"' in PAGE_HTML and 'src="/assets/ada.webp"' in PAGE_HTML


def test_room_stays_reachable_and_agent_state_comes_from_live_data():
    """Místnost agentů zůstává (přehled + vlastní položka) a stav Ady/Noxe se počítá z peers/board."""
    for needle in ('data-view="room"', 'id="feed"', 'id="compose"', '<select id="to">', 'id="sendBtn"',
                   "function agentStatus(key)", 'connectionMode !== "live"'):
        assert needle in PAGE_HTML, needle
    assert PAGE_HTML.count('class="agent-hero"') == 2


def test_session_cookie_is_per_port_so_two_webs_do_not_log_each_other_out(tmp_path: Path):
    """Regrese 21. 11.: web na 8765 a náhled na 8766 si přepisovaly přihlašovací cookie (403)."""
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    preview = make_handler(store, settings, "secret", 8766)
    preview.sessions["s"] = time.monotonic() + SESSION_MAX_AGE_SECONDS
    preview.session_keys["s"] = "key"
    status, _ = call_handler(store, settings, "secret", "GET", "/api/snapshot?since=0",
                             headers={"Cookie": f"{SESSION_COOKIE}=s", "Host": "localhost:8766"}, handler_type=preview)
    assert status == 403                                   # cookie webu 8765 na náhledu neplatí
    status, _ = call_handler(store, settings, "secret", "GET", "/api/snapshot?since=0",
                             headers={"Cookie": f"{SESSION_COOKIE}_8766=s", "Host": "localhost:8766"}, handler_type=preview)
    assert status == 200


def test_js_esc_escapes_quotes_for_attributes():
    """Bezpečnostní kontrola 21. 11.: esc() se používá i v atributech (title="…"), musí escapovat uvozovky."""
    assert '.replace(/"/g, "&quot;")' in PAGE_HTML and ".replace(/'/g, \"&#39;\")" in PAGE_HTML
    assert 'd.textContent = s ?? ""; return d.innerHTML' not in PAGE_HTML


def test_web_login_opens_running_web_logged_in_without_printing_token(tmp_path: Path, monkeypatch, capsys):
    """Michal 26. 11.: web běží jako služba; `agent-lease web-login` ho otevře přihlášený, token nevypisuje."""
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.setenv("AGENT_NAME", "web")
    opened = []
    monkeypatch.setattr(webui.socket, "create_connection", lambda *a, **k: socket.socket())
    monkeypatch.setattr(webui, "listener_owned_by_me", lambda port: True)
    monkeypatch.setattr(webui, "open_url", lambda url: opened.append(url) or True)
    assert webui.login(8765) == 0
    token = (tmp_path / "webui.token").read_text().strip()
    code = (tmp_path / "webui.login").read_text().split()[0]
    assert opened == [f"http://localhost:8765/#token={code}"] and code != token
    out = capsys.readouterr()
    assert token not in out.out + out.err


def test_web_login_says_when_service_is_down(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.setenv("AGENT_NAME", "web")

    def refused(*_a, **_k):
        raise ConnectionRefusedError

    opened = []
    monkeypatch.setattr(webui.socket, "create_connection", refused)
    monkeypatch.setattr(webui, "listener_owned_by_me", lambda port: None)
    monkeypatch.setattr(webui, "open_url", lambda url: opened.append(url) or True)
    assert webui.login(8765) == 1
    assert opened == [] and "agent-lease-web" in capsys.readouterr().err


def test_open_url_uses_windows_browser_in_wsl(tmp_path: Path, monkeypatch):
    """Ve WSL `webbrowser` otevřel linuxový prohlížeč a přihlášení skončilo 403 (26. 11.)."""
    cmd = tmp_path / "cmd.exe"
    cmd.write_text("")
    calls = []
    monkeypatch.setattr(webui, "_is_wsl", lambda: True)
    monkeypatch.setattr(webui.shutil, "which", lambda name: str(cmd))
    monkeypatch.setattr(webui.subprocess, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(webui.webbrowser, "open", lambda url: pytest.fail("linuxový prohlížeč"))
    assert webui.open_url("http://localhost:8765/#token=abc") is True
    assert calls == [[str(cmd), "/c", "start", "", "http://localhost:8765/#token=abc"]]


def test_session_lasts_a_week_and_pages_have_human_controls():
    """Web jako trvalá služba: přihlášení jednou týdně; člověk zavře review z webu v každém stavu."""
    assert SESSION_MAX_AGE_SECONDS == 7 * 86400
    for needle in ('data-view-panel="quality"', 'data-view-panel="research"', 'data-view-panel="memory"',
                   'data-view-panel="settings"', "data-rv-close", "/api/review-close", 'id="rsNew"',
                   'id="kbDialog"', 'id="setStopBody"', 'id="modeGrid"'):
        assert needle in PAGE_HTML, needle
    assert "QG_DEMO" not in PAGE_HTML  # jen skutečná data


def test_web_login_refuses_port_held_by_another_user(tmp_path: Path, monkeypatch, capsys):
    """Bezpečnostní kontrola 26. 11.: cizí proces na 8765 by přes prohlížeč dostal token z fragmentu."""
    import os
    table = tmp_path / "tcp"
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
    row = "   0: 0100007F:223D 00000000:0000 0A 00000000:00000000 00:00000000 00000000  {uid}        0 1 1"
    table.write_text(header + "\n" + row.format(uid=os.getuid() + 1) + "\n")
    assert webui.listener_owned_by_me(8765, (str(table),)) is False
    table.write_text(header + "\n" + row.format(uid=os.getuid()) + "\n")
    assert webui.listener_owned_by_me(8765, (str(table),)) is True
    assert webui.listener_owned_by_me(8766, (str(table),)) is None
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.setattr(webui, "listener_owned_by_me", lambda port: False)
    monkeypatch.setattr(webui, "open_url", lambda url: pytest.fail("token nesmí odejít"))
    assert webui.login(8765) == 2


def test_pages_refresh_without_new_messages_and_never_keep_stale_bridges_online():
    """Review #54: Nastavení po výpadku nesmí ukazovat most online; Výzkum se obnovuje na otevřené stránce."""
    assert 'const stale = connectionMode !== "live" || !lastRoomAt || Date.now() - lastRoomAt > STALE_MS;' in PAGE_HTML
    assert "return !stale && !!(p && p.active" in PAGE_HTML
    assert 'if (currentView === "research") loadResearch();' in PAGE_HTML
    assert "setTimeout(renderPages);" in PAGE_HTML


def test_login_code_is_single_use_and_expires(tmp_path: Path, monkeypatch):
    """Bezpečnostní audit 26. 11.: do URL a historie prohlížeče jde jen jednorázový kód na 2 min."""
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    code = webui.issue_login_code(settings)
    assert stat.S_IMODE((tmp_path / "webui.login").stat().st_mode) == 0o600
    hdr = {"Origin": "http://localhost:8765", "Host": "localhost:8765", "X-Agent-Lease-Token": code}
    assert call_handler(store, settings, "secret", "POST", "/session", headers=hdr)[0] == 204
    assert call_handler(store, settings, "secret", "POST", "/session", headers=hdr)[0] == 403   # podruhé ne
    code = webui.issue_login_code(settings)
    monkeypatch.setattr(webui.time, "time", lambda: 10**12)
    assert call_handler(store, settings, "secret", "POST", "/session",
                        headers={**hdr, "X-Agent-Lease-Token": code})[0] == 403                 # prošlý


def test_foreign_host_and_missing_origin_are_rejected(tmp_path: Path):
    """DNS rebinding (cizí Host) a POST bez Origin neprojdou ani s platnou session."""
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    handler = authenticated_handler(store, settings, "secret")
    cookie = {"Cookie": f"{SESSION_COOKIE}=session"}
    assert call_handler(store, settings, "secret", "GET", "/api/snapshot?since=0",
                        headers={**cookie, "Host": "evil.example:8765"}, handler_type=handler)[0] == 403
    assert call_handler(store, settings, "secret", "GET", "/",
                        headers={"Host": "evil.example:8765"}, handler_type=handler)[0] == 403
    assert call_handler(store, settings, "secret", "POST", "/api/pause", body={"reason": "x"},
                        headers={**cookie, "Origin": ""}, handler_type=handler)[0] == 403
    status, _ = call_handler(store, settings, "secret", "POST", "/api/pause", body={"reason": "x"},
                             headers={**cookie, "Content-Length": "abc"}, handler_type=handler)
    assert status == 400


def test_index_csp_uses_per_response_nonce_instead_of_unsafe_inline_script(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    sent = {}
    handler_type = make_handler(store, settings, "secret", 8765)
    handler = object.__new__(handler_type)
    handler.path, handler.headers = "/", {"Host": "localhost:8765"}
    handler.wfile, handler.rfile = BytesIO(), BytesIO()
    handler.send_response = lambda code: None
    handler.send_header = lambda k, v: sent.__setitem__(k, v)
    handler.end_headers = lambda: None
    handler.do_GET()
    csp = sent["Content-Security-Policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert "script-src 'unsafe-inline'" not in csp and "object-src 'none'" in csp
    assert f'<script nonce="{nonce}">'.encode() in handler.wfile.getvalue()


def test_cookie_alone_is_not_enough_session_key_is_origin_bound(live_chat):
    """Audit #2098 P2: cookie jde i na jiný port localhostu; bez klíče z localStorage nic nepřečte ani nezmění."""
    port, _, session_id, _ = live_chat
    cookie = {"Cookie": f"{webui.session_cookie_name(port)}={session_id}"}
    for headers in (cookie, {**cookie, "X-Agent-Lease-Key": "wrong"}):
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/api/snapshot?since=0", headers=headers)
        assert connection.getresponse().status == 403
        connection.close()
    assert request(port, "GET", "/api/snapshot?since=0", token=session_id)[0] == 200


def test_event_stream_ends_when_session_expires(tmp_path: Path, monkeypatch):
    """Audit #2098 P2: otevřený stream nesmí po vypršení přihlášení dál posílat data."""
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    store = Store(settings=settings)
    handler_type = authenticated_handler(store, settings, "secret")
    monkeypatch.setattr(webui.time, "sleep", lambda _s: handler_type.sessions.__setitem__("session", 0))
    status, raw = call_handler(store, settings, "secret", "GET", "/api/events?since=0",
                               headers={"Cookie": f"{SESSION_COOKIE}=session"}, handler_type=handler_type)
    assert raw.count(b"event: room") == 1            # jedna dávka, pak konec místo nekonečné smyčky
    assert handler_type.sse_slots.acquire(blocking=False)   # slot se vrátil


def test_slow_request_hits_absolute_read_deadline():
    """Audit #2098 P2: kapání po bajtu neobejde limit; čtení po termínu skončí."""
    reader = webui._DeadlineReader(BytesIO(b"GET / HTTP/1.1"))
    reader.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        reader.readline()


def test_connection_cap_refuses_extra_connections():
    closed = []

    class Base:
        def __init__(self, *a, **k):
            pass

        def process_request(self, request, addr):
            pass

        def shutdown_request(self, request):
            closed.append(request)

    capped = type("Capped", (webui._ConnectionCap, Base), {"max_connections": 2})()
    for i in range(3):
        capped.process_request(i, None)
    assert closed == [2]


def test_web_login_sends_code_only_to_verified_port(tmp_path: Path, monkeypatch):
    """Audit #2098 P2: neověřitelný vlastník portu nebo cizí program ve Windows = kód se neposílá."""
    monkeypatch.setenv("AGENT_LEASE_DB", str(tmp_path / "room.db"))
    monkeypatch.setattr(webui, "open_url", lambda url: pytest.fail("kód nesmí odejít"))
    monkeypatch.setattr(webui.socket, "create_connection", lambda *a, **k: socket.socket())
    monkeypatch.setattr(webui, "listener_owned_by_me", lambda port: None)
    assert webui.login(8765) == 1
    monkeypatch.setattr(webui, "listener_owned_by_me", lambda port: True)
    monkeypatch.setattr(webui, "_is_wsl", lambda: True)
    monkeypatch.setattr(webui, "windows_port_forwards_to_wsl", lambda port: False)
    assert webui.login(8765) == 2


def test_windows_listener_check_accepts_only_wsl_forwarding(monkeypatch):
    out = {"v": "wslrelay\n"}
    monkeypatch.setattr(webui.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": out["v"]})())
    assert webui.windows_port_forwards_to_wsl(8765) is True
    out["v"] = ""
    assert webui.windows_port_forwards_to_wsl(8765) is True
    out["v"] = "evil\n"
    assert webui.windows_port_forwards_to_wsl(8765) is False
