"""
Live chat: lokální loopback místnost nad `Store`.

Fáze 2b z docs/AGENT-BRIDGE-DEEP-RESEARCH.md. Nejmenší verze, která splní
akceptační kritéria: poslouchá jen na loopbacku, vyžaduje token, obnoví
event stream bez ztráty/zdvojení zpráv (`Last-Event-ID`), a jasně rozlišuje
`chat` (bez oprávnění) od `task` (převzetí práce).

Čte a zapisuje přímo do stejné SQLite databáze jako CLI a MCP nástroje —
broker jako jediný writer je pozdější krok (fáze 2), tohle je most k němu,
ne jeho náhrada. Proto žádné nové závislosti: jen stdlib `http.server`.

Identita `michal`: tenhle proces je jediné místo, které smí zprávy posílat
pod jménem `michal` (viz doc řádek 95). Klient token pošle, ale odesílatele
si nevybírá — je to jméno z `human-name` vedle DB (viz `ensure_human_name`).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock
from urllib.parse import parse_qs, urlsplit

from .config import HUMAN_AGENT, HUMAN_NAME_FILE, Settings, human_name
from .models import MessageKind
from .store import Store

POLL_SECONDS = 0.7
ROOM_SECONDS = 2.0
SESSION_COOKIE = "agent_lease_session"
SESSION_MAX_AGE_SECONDS = 3600
MAX_SSE_CONNECTIONS = 16
REQUEST_SOCKET_TIMEOUT = 10.0
SSE_HEARTBEAT_SECONDS = 10.0


def _token_path(settings: Settings) -> Path:
    return settings.db_path.parent / "webui.token"


def load_or_create_token(settings: Settings) -> str:
    """
    Token žije vedle DB, ne v env — přežije restart procesu i terminálu.

    Vytváří se atomicky rovnou na 0600 (`O_EXCL`), ne `write_text` + dodatečný
    `chmod` — mezi těma dvěma kroky (nebo když proces spadne přesně mezi nimi)
    by soubor chvíli ležel na default umasku. Adresář dostává 0700 ze stejného
    důvodu; existující volnější práva na obou opravíme, ne jen na nových.
    """
    path = _token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)  # mkdir mode neplatí, když adresář už existoval
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            path.chmod(0o600)
            return existing
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Závod: mezi `exists()` a `open()` token vytvořil jiný proces (druhý
        # start webui skoro současně) — použij, co napsal on, ne přepisuj.
        existing = path.read_text().strip()
        if existing:
            path.chmod(0o600)
            return existing
        raise
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    return token


def _sse(event: str, event_id: int | None, payload: dict) -> bytes:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _sse_heartbeat() -> bytes:
    return b": heartbeat\n\n"


def ensure_human_name(settings: Settings) -> str:
    """
    Jméno člověka pro místnost, uložené vedle DB (0600). Zakládá ho jen web UI —
    agenti ho čtou, ale přes vlastní prostředí ho změnit nemohou.
    """
    path = settings.db_path.parent / HUMAN_NAME_FILE
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(HUMAN_AGENT)
    return human_name(settings.db_path)


def make_handler(store: Store, settings: Settings, token: str, port: int):
    web_agent = ensure_human_name(settings)
    allowed_origins = {
        f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"
    }
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    sse_slots = BoundedSemaphore(MAX_SSE_CONNECTIONS)
    sessions: dict[str, float] = {}
    sessions_lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "agent-lease-webui/0.1"

        def setup(self) -> None:
            super().setup()
            # Platí už při parsování requestu, takže pomalý lokální klient
            # nemůže držet vlákno navždy ještě před vstupem do handleru.
            self.connection.settimeout(REQUEST_SOCKET_TIMEOUT)

        def log_message(self, fmt: str, *args) -> None:
            pass  # ticho; kdo chce logy, spustí to z terminálu a uvidí je jinde

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")

        # ── autentizace ─────────────────────────────────────────────────
        def _bearer_ok(self) -> bool:
            header = self.headers.get("X-Agent-Lease-Token", "")
            return bool(header) and secrets.compare_digest(header, token)

        def _session_ok(self) -> bool:
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                value = cookie[SESSION_COOKIE].value
            except (CookieError, KeyError):
                return False
            now = time.monotonic()
            with sessions_lock:
                expired = [session_id for session_id, deadline in sessions.items() if deadline <= now]
                for session_id in expired:
                    del sessions[session_id]
                return bool(value) and sessions.get(value, 0) > now

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin in allowed_origins

        def _reject(self, code: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ── routování ───────────────────────────────────────────────────
        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            qs = parse_qs(parts.query)
            if parts.path == "/":
                return self._handle_index()
            if parts.path == "/api/events":
                return self._handle_events(qs)
            if parts.path == "/api/snapshot":
                return self._handle_snapshot(qs)
            self._reject(404, "not found")

        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            if parts.path == "/session":
                return self._handle_session()
            if not self._origin_ok():
                return self._reject(403, "bad origin")
            if not self._session_ok():
                return self._reject(403, "bad token")
            if parts.path == "/api/send":
                return self._handle_send()
            if parts.path == "/api/retry":
                return self._handle_retry()
            if parts.path == "/api/cancel":
                return self._handle_cancel()
            if parts.path == "/api/approve":
                return self._handle_approve()
            if parts.path == "/api/approve-resume":
                return self._handle_approve_resume()
            if parts.path in ("/api/pause", "/api/resume"):
                return self._handle_pause(parts.path.endswith("pause"))
            self._reject(404, "not found")

        # ── handlery ────────────────────────────────────────────────────
        def _handle_index(self) -> None:
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._security_headers()
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_session(self) -> None:
            # Browser načte token z URL fragmentu (ten se neposílá serveru),
            # ihned jej odstraní a jednou vymění za skriptu nepřístupnou cookie.
            if (
                self.headers.get("Origin") not in allowed_origins
                or self.headers.get("Host") not in allowed_hosts
            ):
                return self._reject(403, "bad origin")
            if not self._bearer_ok():
                return self._reject(403, "bad token")
            session_id = secrets.token_urlsafe(32)
            with sessions_lock:
                sessions[session_id] = time.monotonic() + SESSION_MAX_AGE_SECONDS
            self.send_response(204)
            self._security_headers()
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/; "
                f"Max-Age={SESSION_MAX_AGE_SECONDS}",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _handle_snapshot(self, qs: dict[str, list[str]]) -> None:
            if not self._session_ok():
                return self._reject(403, "bad token")
            try:
                since = max(0, int(qs.get("since", ["0"])[0]))
            except ValueError:
                return self._reject(400, "since must be an integer")
            payload = {
                "me": web_agent,
                "messages": (
                    store.inbox(since_id=since, limit=200)
                    if since else store.latest_messages(limit=200)
                ),
                "peers": store.peers(),
                "claims": [c.as_dict() for c in store.claims()],
                "board": store.board(),
                "paused": store.paused(),
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict | None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0 or length > 8192:
                self._reject(400, "empty or too long body")
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                self._reject(400, "invalid JSON")
                return None
            if not isinstance(data, dict):
                self._reject(400, "JSON body must be an object")
                return None
            return data

        def _json_response(self, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_send(self) -> None:
            data = self._read_json()
            if data is None:
                return
            text = str(data.get("text", "")).strip()
            kind = str(data.get("kind", "chat"))
            recipient = str(data.get("to", "") or "").strip()
            if not text:
                return self._reject(400, "empty text")
            try:
                kind_enum = MessageKind(kind)
            except ValueError:
                return self._reject(400, f"unknown kind: {kind}")
            if kind_enum == MessageKind.BROADCAST:
                recipient = "*"
            elif not recipient:
                return self._reject(400, "chat/task/control needs 'to'")
            message_id = store.send(web_agent, recipient, text, kind=kind_enum)
            store.heartbeat(web_agent, cwd=os.getcwd())
            self._json_response({"id": message_id})

        def _handle_retry(self) -> None:
            data = self._read_json()
            if data is None:
                return
            try:
                message_id = int(data["id"])
                recipient = str(data["to"]).strip()
                not_before = float(data.get("not_before", 0))
            except (KeyError, TypeError, ValueError):
                return self._reject(400, "retry needs a valid 'id' and 'to'")
            if not recipient:
                return self._reject(400, "retry needs a valid 'id' and 'to'")
            if not store.retry(message_id, recipient, not_before=not_before):
                return self._reject(409, "task is not in a state that can be retried")
            self._json_response({"id": message_id, "state": "pending"})

        def _handle_cancel(self) -> None:
            data = self._read_json()
            if data is None:
                return
            try:
                message_id = int(data["id"])
                recipient = str(data["to"]).strip()
            except (KeyError, TypeError, ValueError):
                return self._reject(400, "cancel needs a valid 'id' and 'to'")
            if not recipient:
                return self._reject(400, "cancel needs a valid 'id' and 'to'")
            try:
                cancelled = store.cancel(message_id, recipient)
            except (OSError, RuntimeError):
                return self._reject(503, "Cancellation requested, but stopping could not be "
                                    "confirmed. Check the running worker before continuing.")
            if not cancelled:
                return self._reject(409, "task can no longer be cancelled or does not exist")
            self._json_response({"id": message_id, "state": "cancelled"})

        def _handle_approve(self) -> None:
            """Člověk schválí úkol z karantény (jen přihlášená lidská session)."""
            data = self._read_json()
            if data is None:
                return
            try:
                message_id = int(data["id"])
                recipient = str(data["to"]).strip()
                reason = str(data.get("reason", "")).strip()
            except (KeyError, TypeError, ValueError):
                return self._reject(400, "approve needs a valid 'id' and 'to'")
            if not recipient or not reason:
                return self._reject(400, "approve needs 'id', 'to' and a reason")
            if not store.approve_quarantine(message_id, recipient, web_agent, reason):
                return self._reject(409, "only a task held back by quarantine can be approved")
            self._json_response({"id": message_id, "state": "pending", "approved": True})

        def _handle_approve_resume(self) -> None:
            """Člověk povolí jeden pokus navázat na checkpoint přes změněnou kopii (A6)."""
            data = self._read_json()
            if data is None:
                return
            try:
                message_id = int(data["id"])
                recipient = str(data["to"]).strip()
                reason = str(data.get("reason", "")).strip()
            except (KeyError, TypeError, ValueError):
                return self._reject(400, "approve-resume needs a valid 'id' and 'to'")
            if not recipient or not reason:
                return self._reject(400, "approve-resume needs 'id', 'to' and a reason")
            if not store.approve_checkpoint_resume(message_id, recipient, web_agent, reason):
                return self._reject(409, "only a task held back by its checkpoint can be resumed")
            self._json_response({"id": message_id, "state": "pending", "resume_approved": True})

        def _handle_pause(self, stop: bool) -> None:
            """Nouzové zastavení / obnovení. Obnovit smí jen člověk – proto jen tady."""
            data = self._read_json()
            if data is None:
                return
            reason = str(data.get("reason", "")).strip()[:500]
            if stop:
                try:
                    store.pause(web_agent, reason)
                except (OSError, RuntimeError):
                    return self._reject(503, "Pause requested, but stopping could not be confirmed. "
                                        "Check the running workers before continuing.")
            else:
                store.resume(web_agent, reason)
            self._json_response({"paused": stop})

        def _handle_events(self, qs: dict[str, list[str]]) -> None:
            if not self._session_ok():
                return self._reject(403, "bad token")
            # Last-Event-ID má přednost před ?since= — to je to, co prohlížeč
            # pošle sám při reconnectu, a je to spolehlivější než náš JS.
            last_event_id = self.headers.get("Last-Event-ID")
            try:
                since = max(
                    0, int(last_event_id) if last_event_id else int(qs.get("since", ["0"])[0])
                )
            except ValueError:
                return self._reject(400, "since/Last-Event-ID must be an integer")

            if not sse_slots.acquire(blocking=False):
                return self._reject(503, "too many event streams")

            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self._security_headers()
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                next_room_at = 0.0
                next_heartbeat_at = 0.0
                while True:
                    messages = store.inbox(since_id=since, limit=200)
                    for message in messages:
                        self.wfile.write(_sse("message", message["id"], message))
                        since = max(since, message["id"])
                    if messages:
                        self.wfile.flush()

                    now = time.monotonic()
                    if now >= next_room_at:
                        room = {
                            "peers": store.peers(),
                            "claims": [c.as_dict() for c in store.claims()],
                            "board": store.board(),
                            # Bez stavu pauzy by živá aktualizace přepsala tlačítko
                            # „Obnovit" na „Zastavit" a člověk by stop zapnul znovu.
                            "paused": store.paused(),
                            "messages": store.latest_messages(limit=200),
                        }
                        self.wfile.write(_sse("room", None, room))
                        self.wfile.flush()
                        next_room_at = now + ROOM_SECONDS

                    if now >= next_heartbeat_at:
                        self.wfile.write(_sse_heartbeat())
                        self.wfile.flush()
                        next_heartbeat_at = now + SSE_HEARTBEAT_SECONDS

                    time.sleep(POLL_SECONDS)
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                return  # klient zavřel kartu nebo se prohlížeč odpojil
            finally:
                sse_slots.release()

    Handler.sse_slots = sse_slots
    Handler.sessions = sessions
    Handler.sessions_lock = sessions_lock
    return Handler


PAGE_HTML = """<!doctype html>
<html lang="cs">
<title>agent-lease · místnost</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    color-scheme: dark light;
    --bg: #0a0b10; --surface: #12141c; --surface-2: #171a24; --raised: #1d212c;
    --border: rgba(255,255,255,.08); --border-soft: rgba(255,255,255,.05);
    --text: #eef0f6; --text-dim: #9aa0b4; --text-faint: #676d82;
    --accent-1: #8b7bff; --accent-2: #ff6fae;
    --grad: linear-gradient(135deg, var(--accent-1), var(--accent-2));
    --ok: #38d996; --warn: #ffb648; --bad: #ff5c7a;
    --shadow: 0 8px 28px rgba(0,0,0,.35);
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f4f5f9; --surface: #ffffff; --surface-2: #f0f1f7; --raised: #ffffff;
      --border: rgba(15,17,30,.10); --border-soft: rgba(15,17,30,.06);
      --text: #14162a; --text-dim: #565c74; --text-faint: #8a8ea3;
      --ok: #12a86a; --warn: #c27a00; --bad: #e0244a;
      --shadow: 0 8px 24px rgba(30,20,60,.08);
    }
  }
  * { box-sizing: border-box; }
  body {
    font: 14px/1.5 "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    margin: 0; height: 100vh; display: flex; flex-direction: column;
    background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased;
  }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 8px; }
  button { font: inherit; }

  /* ── pruh pauzy: vždy nahoře přes celou šířku ── */
  #pause { flex: none; }
  .pausebar {
    display: flex; align-items: center; justify-content: space-between; gap: 14px;
    flex-wrap: wrap; padding: 12px 22px; border-bottom: 1px solid var(--border);
  }
  .pausebar .ptext { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .pausebar .big { font-weight: 750; font-size: 16px; }
  .pausebar .why { font-size: 12.5px; opacity: .9; overflow-wrap: anywhere; }
  .pausebar.stopped { background: var(--bad); color: #fff; }
  .pausebar.running { background: color-mix(in srgb, var(--ok) 12%, var(--surface)); }
  .pausebar.running .big { color: var(--ok); font-size: 14px; }
  .pausebar button { border-radius: 10px; font-weight: 700; cursor: pointer; white-space: nowrap; }
  .pausebar.stopped button {
    border: none; padding: 12px 24px; font-size: 15.5px; background: #fff; color: #b3123a;
    box-shadow: 0 4px 14px rgba(0,0,0,.25);
  }
  .pausebar.running button {
    padding: 7px 14px; font-size: 12.5px; background: transparent; color: var(--bad);
    border: 1px solid var(--bad);
  }

  #app { flex: 1; display: flex; min-height: 0; }

  /* ── levý sloupec ── */
  #side {
    width: 300px; flex: none; padding: 18px 14px; overflow-y: auto;
    background: var(--surface); border-right: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 20px;
  }
  #brand { display: flex; align-items: center; gap: 10px; padding: 0 2px; }
  #brand .mark { width: 30px; height: 30px; border-radius: 9px; background: var(--grad); flex: none; }
  #brand .name { font-weight: 650; font-size: 15px; }
  #brand .sub { font-size: 11.5px; color: var(--text-faint); }
  h3 {
    font-size: 11.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .07em;
    color: var(--text-faint); margin: 0 0 8px 2px;
  }
  .section { display: flex; flex-direction: column; gap: 8px; }
  details.section > summary {
    cursor: pointer; list-style: none; font-size: 12px; color: var(--text-dim); padding: 2px;
  }
  details.section > summary::-webkit-details-marker { display: none; }
  details.section > summary::before { content: "▸ "; }
  details.section[open] > summary::before { content: "▾ "; }

  .card {
    padding: 10px 12px; border-radius: 12px; background: var(--surface-2);
    border: 1px solid var(--border-soft); display: flex; flex-direction: column; gap: 5px;
  }
  .card .head { display: flex; align-items: center; gap: 10px; }
  .avatar {
    width: 30px; height: 30px; border-radius: 50%; flex: none; display: grid; place-items: center;
    color: #fff; font-weight: 700; font-size: 12px; position: relative;
  }
  .avatar .dot {
    position: absolute; right: -1px; bottom: -1px; width: 10px; height: 10px; border-radius: 50%;
    border: 2px solid var(--surface-2); background: var(--text-faint);
  }
  .avatar .dot.on { background: var(--ok); }
  .card .who { min-width: 0; flex: 1; }
  .card .name { font-weight: 650; font-size: 13px; }
  .card .id { font-size: 11px; color: var(--text-faint); }
  .card .line { font-size: 12px; color: var(--text-dim); overflow-wrap: anywhere; }
  .card .tags { display: flex; flex-wrap: wrap; gap: 5px; }
  .card.offline { opacity: .65; }
  #agents, #offline, #claims { display: flex; flex-direction: column; gap: 8px; }
  .empty { font-size: 12.5px; color: var(--text-faint); padding: 4px 2px; }
  .path { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11.5px; overflow-wrap: anywhere; }

  .tag {
    font-size: 11px; font-weight: 650; padding: 2px 8px; border-radius: 999px;
    background: var(--border-soft); color: var(--text-dim); white-space: nowrap;
  }
  .tag.ok { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
  .tag.warn { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); }
  .tag.bad { background: color-mix(in srgb, var(--bad) 16%, transparent); color: var(--bad); }
  .tag.work { background: color-mix(in srgb, var(--accent-1) 18%, transparent); color: var(--accent-1); }
  .tag.gone { text-decoration: line-through; }

  /* ── hlavní část ── */
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #topbar {
    display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
    padding: 12px 22px; border-bottom: 1px solid var(--border);
  }
  #topbar .title { font-weight: 700; font-size: 15px; }
  #topbar .tools { display: flex; align-items: center; gap: 14px; font-size: 12.5px; color: var(--text-dim); }
  #topbar label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
  #status { display: flex; align-items: center; gap: 7px; }
  #status .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-faint); }
  #status .dot.live { background: var(--ok); }
  #status .dot.retry { background: var(--warn); }

  #feed { flex: 1; overflow-y: auto; padding: 18px 22px; display: flex; flex-direction: column; gap: 10px; }
  .bubble {
    max-width: 760px; padding: 10px 14px; border-radius: 14px; background: var(--surface);
    border: 1px solid var(--border);
  }
  .bubble.mine { align-self: flex-end; background: color-mix(in srgb, var(--accent-1) 10%, var(--surface)); }
  .bubble.system { opacity: .7; font-size: 12.5px; padding: 6px 12px; }
  .bubble.hidden { display: none; }
  .bubble .meta { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 8px; font-size: 12px; color: var(--text-faint); margin-bottom: 4px; }
  .bubble .meta .who { color: var(--text); font-weight: 650; }
  .bubble .meta .num { margin-left: auto; font-variant-numeric: tabular-nums; }
  .bubble .text { font-size: 13.5px; white-space: pre-wrap; word-break: break-word; }
  .bubble .text.clip { max-height: 9.5em; overflow: hidden; mask-image: linear-gradient(#000 70%, transparent); }
  .more { margin-top: 4px; border: none; background: none; color: var(--accent-1); cursor: pointer; padding: 0; font-size: 12.5px; font-weight: 600; }
  .kind { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px; color: #fff; }
  .kind-chat { background: #4d7dff; }
  .kind-task { background: #ff9d42; color: #2a1600; }
  .kind-broadcast { background: #7d84a3; }
  .kind-control { background: #ff5c7a; }
  .kind-result { background: #2fc79c; color: #00291f; }
  .actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .btn {
    font-size: 12px; font-weight: 650; padding: 5px 11px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--surface-2); color: var(--text); cursor: pointer;
  }
  .btn:hover { border-color: var(--accent-1); }

  /* ── psaní zprávy ── */
  #compose {
    display: flex; flex-direction: column; gap: 8px; padding: 12px 22px 14px;
    border-top: 1px solid var(--border); background: var(--surface);
  }
  #compose .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .seg { display: inline-flex; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  .seg button {
    border: none; background: transparent; color: var(--text-dim); padding: 7px 14px;
    font-weight: 650; cursor: pointer; font-size: 13px;
  }
  .seg button.on { background: var(--grad); color: #fff; }
  #compose label { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-dim); }
  #to, #text {
    font: inherit; color: var(--text); background: var(--surface-2);
    border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px;
  }
  #to { min-width: 210px; cursor: pointer; }
  #text { flex: 1; min-width: 0; resize: vertical; min-height: 42px; max-height: 40vh; }
  #to:focus, #text:focus { outline: 2px solid var(--accent-1); outline-offset: -1px; }
  #hint { font-size: 12px; color: var(--text-faint); }
  #hint.task { color: var(--warn); }
  #sendBtn {
    align-self: stretch; padding: 0 22px; border-radius: 10px; border: none; background: var(--grad);
    color: #fff; font-weight: 700; cursor: pointer;
  }
  #sendBtn:disabled { opacity: .5; cursor: default; }

  @media (max-width: 760px) {
    #app { flex-direction: column; }
    #side { width: auto; max-height: 38vh; border-right: none; border-bottom: 1px solid var(--border); padding: 12px 16px; }
    #topbar, #feed, #compose { padding-left: 16px; padding-right: 16px; }
    .pausebar { padding: 12px 16px; }
    #to { min-width: 0; flex: 1; }
  }
</style>
<div id="pause"></div>
<div id="app">
<div id="side">
  <div id="brand"><div class="mark"></div><div><div class="name">agent-lease</div><div class="sub">místnost agentů</div></div></div>
  <div class="section"><h3>Agenti</h3><div id="agents"></div></div>
  <div class="section"><h3>Zamčené cesty</h3><div id="claims"></div></div>
  <details class="section" id="offlineBox"><summary id="offlineSummary">Offline</summary><div id="offline"></div></details>
</div>
<div id="main">
  <div id="topbar">
    <div class="title">Zprávy</div>
    <div class="tools">
      <label><input type="checkbox" id="showSystem"> systémové zprávy</label>
      <div id="status"><span class="dot"></span><span id="statusText">připojuji…</span></div>
    </div>
  </div>
  <div id="feed"></div>
  <div id="compose">
    <div class="row">
      <div class="seg" id="kindSeg">
        <button type="button" data-kind="chat" class="on">💬 Zpráva</button>
        <button type="button" data-kind="task">🛠 Úkol</button>
      </div>
      <label>Komu <select id="to"></select></label>
    </div>
    <div id="hint"></div>
    <div class="row">
      <textarea id="text" rows="2" placeholder="Napiš zprávu… (Enter pošle, Shift+Enter nový řádek)"></textarea>
      <button id="sendBtn" type="button">Poslat</button>
    </div>
  </div>
</div>
</div>
<script>
const feed = document.getElementById("feed");
const statusDot = document.querySelector("#status .dot");
const statusText = document.getElementById("statusText");
const toEl = document.getElementById("to");
const textEl = document.getElementById("text");
const hintEl = document.getElementById("hint");
const showSystemEl = document.getElementById("showSystem");
let me = "";
let kind = "chat";
let knownAgents = ["claude-code", "codex"];

const NAMES = {
  "claude-code": "Ada · Claude Code", "codex": "Nox · Codex",
  "claude-code-bridge": "Most Ady (práce na pozadí)", "codex-bridge": "Most Noxe (práce na pozadí)",
  "broker": "systém",
};
const KIND = { chat: "zpráva", task: "úkol", broadcast: "všem", control: "řízení", result: "výsledek" };
const STATE = {
  pending: ["čeká", ""], leased: ["přiděleno", "work"], started: ["pracuje se", "work"],
  succeeded: ["hotovo", "ok"], failed: ["selhalo", "bad"], needs_review: ["ke kontrole", "warn"],
  dead_letter: ["nedoručitelné", "bad"], cancelled: ["zrušeno", "gone"], expired: ["propadlo", "gone"],
  accepted: ["převzato", "ok"], unread: ["nepřečteno", ""],
};
const PRESENCE = { busy: ["pracuje", "work"], idle: ["volný", "ok"], offline: ["offline", ""], stalled: ["neodpovídá", "warn"] };

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function nameOf(a) { return a === me ? "Ty" : (NAMES[a] || a); }
function hueOf(name) { let h = 0; for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function avatarStyle(name) {
  const h = hueOf(name);
  return `background: linear-gradient(135deg, hsl(${h} 70% 52%), hsl(${(h + 45) % 360} 70% 52%))`;
}
function initial(name) { return String(nameOf(name) || "?").trim().slice(0, 1).toUpperCase(); }
function ago(seconds) {
  const s = Math.max(0, seconds | 0);
  if (s < 60) return "právě teď";
  if (s < 3600) return `před ${Math.floor(s / 60)} min`;
  if (s < 86400) return `před ${Math.floor(s / 3600)} h`;
  return `před ${Math.floor(s / 86400)} d`;
}
function ttl(seconds) {
  const s = Math.max(0, seconds | 0), m = Math.floor(s / 60);
  return m > 0 ? `${m} min` : `${s} s`;
}
function clock(t) { return new Date(t * 1000).toLocaleTimeString("cs-CZ", {hour: "2-digit", minute: "2-digit"}); }
function tag(text, cls) { return `<span class="tag ${cls || ""}">${esc(text)}</span>`; }

function updateStatus(mode) {
  statusDot.className = "dot" + (mode === "live" ? " live" : mode === "retry" ? " retry" : "");
  statusText.textContent = mode === "live" ? "živě" : mode === "retry" ? "spojení přerušeno, zkouším znovu…" : "připojuji…";
}

// Systémový šum: potvrzení od brokeru a automatické „✔ taking #…" příjemky.
function isSystem(m) {
  return m.agent === "broker" || (m.kind === "result" && /^✔ taking #/.test(m.text || ""));
}

function renderMessage(m) {
  let div = document.getElementById("msg-" + m.id);
  const fresh = !div;
  if (fresh) div = document.createElement("div");
  div.id = "msg-" + m.id;
  const system = isSystem(m);
  div.className = "bubble" + (m.agent === me ? " mine" : "") + (system ? " system" : "")
    + (system && !showSystemEl.checked ? " hidden" : "");
  const k = m.kind || "chat";
  const to = m.recipient && m.recipient !== "*" ? `→ <span class="who">${esc(nameOf(m.recipient))}</span>` : "";
  // Stav doručení má smysl jen u úkolu; u zprávy stačí, jestli ji příjemce viděl.
  const st = k !== "task" ? "" : m.state && STATE[m.state] ? tag(STATE[m.state][0], STATE[m.state][1]) : (m.state ? tag(m.state) : "");
  const rcpt = (m.recipient && m.recipient !== "*" && k !== "result" && m.agent !== "broker")
    ? (m.accepted ? tag("✔ převzal" + (m.accept_note ? ": " + m.accept_note : ""), "ok")
       : m.seen ? tag("👁 viděl, zatím nepřevzal", "work")
       : tag("⏳ ještě nedoručeno")) : "";
  const long = (m.text || "").length > 600 || (m.text || "").split(String.fromCharCode(10)).length > 8;
  div.innerHTML =
    `<div class="meta"><span class="kind kind-${esc(k)}">${esc(KIND[k] || k)}</span>` +
    `<span class="who">${esc(nameOf(m.agent))}</span> ${to}<span>· ${ago(m.seconds_ago)}</span>` +
    st + rcpt + `<span class="num">#${m.id}</span></div>` +
    `<div class="text${long ? " clip" : ""}">${esc(m.text)}</div>`;
  if (long) {
    const more = document.createElement("button");
    more.className = "more"; more.textContent = "Zobrazit celé";
    more.onclick = () => {
      const t = div.querySelector(".text");
      t.classList.toggle("clip");
      more.textContent = t.classList.contains("clip") ? "Zobrazit celé" : "Sbalit";
    };
    div.appendChild(more);
  }
  if (k === "task" && m.recipient && m.state) {
    const actions = document.createElement("div");
    actions.className = "actions";
    const add = (label, fn) => { const b = document.createElement("button"); b.className = "btn"; b.textContent = label; b.onclick = fn; actions.appendChild(b); };
    if (["pending", "leased", "started", "failed", "needs_review", "dead_letter"].includes(m.state)) {
      add("✕ Zrušit úkol", () => { if (confirm(`Zrušit úkol #${m.id}?`)) control("cancel", m.id, m.recipient); });
    }
    if (m.state === "needs_review" && (m.last_error || "").startsWith("quarantine")) {
      add("Schválit a spustit jednou", () => {
        const reason = prompt("Proč je bezpečné tento zadržený úkol spustit? (zaznamená se)");
        if (reason) post("/api/approve", {id: m.id, to: m.recipient, reason});
      });
    }
    if (m.state === "needs_review" && (m.last_error || "").startsWith("checkpoint:")) {
      add("Přesto navázat (jednou)", () => {
        const reason = prompt("Pracovní složka se od uložení změnila. Proč je bezpečné pokračovat? (zaznamená se)");
        if (reason) post("/api/approve-resume", {id: m.id, to: m.recipient, reason});
      });
    }
    if (["failed", "needs_review", "dead_letter"].includes(m.state)) {
      add("↻ Zkusit znovu", () => control("retry", m.id, m.recipient));
    }
    if (actions.childElementCount) div.appendChild(actions);
  }
  if (fresh) {
    const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
    feed.appendChild(div);
    if (stick) feed.scrollTop = feed.scrollHeight;
  }
}

function renderPause(p) {
  if (p === undefined) return;  // zpráva bez stavu pauzy nesmí přepsat tlačítko
  const el = document.getElementById("pause");
  el.innerHTML = "";
  const bar = document.createElement("div");
  const b = document.createElement("button");
  if (p) {
    bar.className = "pausebar stopped";
    bar.innerHTML = `<div class="ptext"><div class="big">⏸ Agenti jsou ZASTAVENÍ — na pozadí nic nepracuje</div>`
      + `<div class="why">Zastavil: ${esc(nameOf(p.by))}${p.reason ? " · důvod: " + esc(p.reason) : ""}${p.at ? " · v " + clock(p.at) : ""}</div></div>`;
    b.textContent = "▶ Pustit agenty znovu";
    b.onclick = () => { if (confirm("Pustit agenty znovu do samostatné práce?")) pauseCall("resume", ""); };
  } else {
    bar.className = "pausebar running";
    bar.innerHTML = `<div class="ptext"><div class="big">▶ Agenti běží</div></div>`;
    b.textContent = "🛑 Zastavit všechny agenty";
    b.onclick = () => { const r = prompt("Opravdu zastavit všechny agenty? Napiš důvod (zaznamená se):", "nouzové zastavení"); if (r !== null) pauseCall("pause", r); };
  }
  bar.appendChild(b);
  el.appendChild(bar);
}
function pauseCall(action, reason) {
  fetch("/api/" + action, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({reason})})
    .then(r => {
      if (!r.ok) return r.text().then(t => alert(t));
      // Odpověď nese jen ano/ne; skutečný stav (kdo, proč) dočteme ze snímku.
      return fetch("/api/snapshot?since=" + Number.MAX_SAFE_INTEGER).then(s => s.json()).then(s => renderPause(s.paused));
    });
}

function agentCard(p, b) {
  const pres = PRESENCE[(b && b.presence) || p.presence] || [p.active ? "online" : "offline", p.active ? "ok" : ""];
  const tags = [tag(pres[0], pres[1])];
  const src = b || p;
  if (src.provider_state === "limited") tags.push(tag("vyčerpaný limit" + (src.provider_resets_at ? " do " + clock(src.provider_resets_at) : ""), "bad"));
  else if (src.provider_state === "unknown") tags.push(tag("stav limitu neznámý", "warn"));
  if (src.limited_until) tags.push(tag("limit do " + clock(src.limited_until), "bad"));
  if (b && b.queued) tags.push(tag(`ve frontě ${b.queued}` + (b.unaccepted ? `, nepřevzato ${b.unaccepted}` : ""), ""));
  if (b && b.needs_review) tags.push(tag(`ke kontrole ${b.needs_review}`, "warn"));
  let line = "";
  if (b && b.active) line = `▶ #${b.active.id}: ${esc(b.active.text)}`;
  else if (p.status) line = esc(p.status);
  else if (!p.active) line = "naposledy " + ago(p.seen_seconds_ago);
  if (b && b.blocked_reason) line += `<br>čeká: ${esc(b.blocked_reason)}`;
  if (b && b.blocked_task) line += `<br>🔒 #${b.blocked_task.id} ${esc(b.blocked_task.path)} drží ${esc(nameOf(b.blocked_task.held_by))}`;
  return `<div class="card ${p.active ? "" : "offline"}">
    <div class="head"><div class="avatar" style="${avatarStyle(p.agent)}">${esc(initial(p.agent))}<span class="dot ${p.active ? "on" : ""}"></span></div>
      <div class="who"><div class="name">${esc(nameOf(p.agent))}</div>${NAMES[p.agent] || p.agent === me ? `<div class="id">${esc(p.agent)}</div>` : ""}</div></div>
    ${line ? `<div class="line">${line}</div>` : ""}
    <div class="tags">${tags.join("")}</div></div>`;
}

function renderRoom(r) {
  if (r.me) me = r.me;
  (r.messages || []).forEach(renderMessage);
  renderPause(r.paused);
  const board = {};
  (r.board || []).forEach(b => { board[b.agent] = b; });
  const peers = (r.peers || []).filter(p => !String(p.agent).startsWith("untrusted-"));
  (r.board || []).forEach(b => { if (!peers.some(p => p.agent === b.agent)) peers.push({agent: b.agent, active: false, status: b.status}); });
  const online = peers.filter(p => p.active), offline = peers.filter(p => !p.active);
  document.getElementById("agents").innerHTML = online.map(p => agentCard(p, board[p.agent])).join("") || "<div class='empty'>nikdo není online</div>";
  document.getElementById("offline").innerHTML = offline.map(p => agentCard(p, board[p.agent])).join("");
  document.getElementById("offlineSummary").textContent = `Offline (${offline.length})`;
  document.getElementById("offlineBox").style.display = offline.length ? "" : "none";
  document.getElementById("claims").innerHTML = (r.claims || []).map(c => `
    <div class="card"><div class="path">🔒 ${esc(c.path)}</div>
      <div class="line">${esc(nameOf(c.held_by))}${c.purpose ? " · " + esc(c.purpose) : ""} · ještě ${ttl(c.expires_in_seconds)}</div></div>`).join("")
    || "<div class='empty'>nic není zamčené</div>";
  // Komu: skuteční agenti, bez mostů, systému a sebe.
  const names = new Set(["claude-code", "codex"]);
  peers.forEach(p => { const a = String(p.agent); if (a !== me && a !== "broker" && !a.endsWith("-bridge")) names.add(a); });
  const list = [...names].sort((a, b) => (NAMES[a] ? 0 : 1) - (NAMES[b] ? 0 : 1) || a.localeCompare(b));
  if (list.join() !== knownAgents.join()) { knownAgents = list; fillRecipients(); }
}

function fillRecipients() {
  const prev = toEl.value;
  toEl.innerHTML = "";
  knownAgents.forEach(a => toEl.add(new Option(nameOf(a), a)));
  const all = new Option("📢 Všem v místnosti", "*");
  all.disabled = kind === "task";
  toEl.add(all);
  if ([...toEl.options].some(o => o.value === prev && !o.disabled)) toEl.value = prev;
  syncHint();
}
function setKind(k) {
  kind = k;
  document.querySelectorAll("#kindSeg button").forEach(b => b.classList.toggle("on", b.dataset.kind === k));
  fillRecipients();
}
function syncHint() {
  const to = toEl.value === "*" ? "všem v místnosti" : nameOf(toEl.value);
  if (kind === "task") {
    hintEl.className = "task";
    hintEl.textContent = `🛠 Úkol: ${to} ho převezme a udělá sám, klidně i na pozadí bez tebe. Posílej jen to, co má opravdu udělat.`;
  } else {
    hintEl.className = "";
    hintEl.textContent = `💬 Zpráva pro: ${to}. Jen informace — přečte si ji, až se podívá do místnosti. Nic se samo nespustí.`;
  }
  textEl.placeholder = kind === "task" ? "Popiš, co má udělat…" : "Napiš zprávu… (Enter pošle, Shift+Enter nový řádek)";
}

function connect(since) {
  const es = new EventSource("/api/events?since=" + since);
  es.addEventListener("message", e => renderMessage(JSON.parse(e.data)));
  es.addEventListener("room", e => renderRoom(JSON.parse(e.data)));
  es.onopen = () => updateStatus("live");
  es.onerror = () => updateStatus("retry");
}

// Nejdřív snapshot, ať karta nezačíná prázdná, pak SSE od jeho posledního ID —
// `Last-Event-ID`/`since` na serveru zajistí navazující stream bez díry i bez duplicit.
async function establishSession() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
  history.replaceState(null, "", location.pathname + location.search);
  if (!token) return;  // bez tokenu zkusíme existující cookie
  const response = await fetch("/session", { method: "POST", headers: { "X-Agent-Lease-Token": token } });
  if (!response.ok) throw new Error("Login failed");
}

function showLoginHelp() {
  feed.innerHTML = `<div class="bubble"><div class="text">Nejsi přihlášený. Spusť v terminálu <b>agent-lease web --open</b> — otevře se ti přihlášená stránka.</div></div>`;
}

establishSession()
  .then(() => fetch("/api/snapshot?since=0"))
  .then(r => { if (!r.ok) throw new Error("auth"); return r.json(); })
  .then(s => {
    if (s.me) me = s.me;
    s.messages.forEach(renderMessage);
    renderRoom(s);
    feed.scrollTop = feed.scrollHeight;
    connect(s.messages.length ? s.messages[s.messages.length - 1].id : 0);
  })
  .catch(() => { updateStatus("retry"); showLoginHelp(); });

function post(url, body) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(r => { if (!r.ok) r.text().then(t => alert(t)); return r.ok; });
}
function send() {
  const text = textEl.value;
  if (!text.trim()) return;
  if (!toEl.value) { alert("Vyber, komu to je."); return; }
  const broadcast = toEl.value === "*";
  if (broadcast && kind === "task") { alert("Úkol musí mít jednoho adresáta."); return; }
  const btn = document.getElementById("sendBtn");
  btn.disabled = true;
  post("/api/send", { text, kind: broadcast ? "broadcast" : kind, to: broadcast ? "" : toEl.value })
    .then(ok => { if (ok) { textEl.value = ""; setKind("chat"); } })
    .finally(() => { btn.disabled = false; textEl.focus(); });
}
function control(action, id, to) { post("/api/" + action, { id, to }); }

document.querySelectorAll("#kindSeg button").forEach(b => b.onclick = () => setKind(b.dataset.kind));
toEl.addEventListener("change", syncHint);
showSystemEl.addEventListener("change", () => {
  document.querySelectorAll(".bubble.system").forEach(d => d.classList.toggle("hidden", !showSystemEl.checked));
});
document.getElementById("sendBtn").onclick = send;
textEl.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
fillRecipients();
</script>
"""


def run(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False) -> int:
    settings = Settings.from_env()
    store = Store(settings=settings)
    token = load_or_create_token(settings)
    handler = make_handler(store, settings, token, port)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"Live room at {url}")
    if open_browser:
        login_url = f"{url}#token={token}"
        if not webbrowser.open(login_url):
            print(
                "Could not open a browser. WARNING: the following URL carries full "
                f"human authority for this room: {login_url}",
                file=sys.stderr,
            )
    else:
        print("To log in safely, run again with --open.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-lease-webui", description="Local live web room for agent-lease")
    parser.add_argument("--host", default="127.0.0.1", help="loopback only — never 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print("Refused: the live room may only listen on loopback.", file=sys.stderr)
        return 2
    return run(args.host, args.port, open_browser=args.open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
