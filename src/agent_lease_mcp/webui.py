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
            if parts.path in ("/api/review-grant", "/api/review-close"):
                return self._handle_review(parts.path.endswith("grant"))
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
                "reviews": store.reviews(limit=30),
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

        def _handle_review(self, grant: bool) -> None:
            """Člověk u review (B4): povolit ještě jedno kolo po eskalaci, nebo uzavřít."""
            data = self._read_json()
            if data is None:
                return
            try:
                review_id = int(data["id"])
                reason = str(data.get("reason", "")).strip()
            except (KeyError, TypeError, ValueError):
                return self._reject(400, "review action needs a valid 'id'")
            try:
                if grant:
                    result = store.review_grant_cycle(web_agent, review_id, reason)
                else:
                    result = store.review_human_close(web_agent, review_id, reason)
            except KeyError:
                return self._reject(404, "no such review")
            except ValueError as exc:
                return self._reject(409, str(exc))
            self._json_response(result)

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
                            "reviews": store.reviews(limit=30),
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
<title>Agent Lease · řídicí přehled</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {
  color-scheme: dark;
  --bg:#080c15; --surface:#101622; --surface-2:#131c2b; --raised:#192337;
  --border:#273248; --border-soft:#1d293c; --text:#e9eef8; --text-dim:#a8b4c9; --text-faint:#8695ae;
  --accent-1:#ac8bff; --accent-2:#647eff; --blue:#58b5ff; --ok:#40dba4; --warn:#f3bf65; --bad:#ff7e96;
  --grad:linear-gradient(120deg,#824de3,#6468ed);
  --shadow:0 6px 24px #0003, inset 0 1px 0 #ffffff04;
}
* { box-sizing:border-box; }
body { margin:0; background:radial-gradient(ellipse at 50% 0,#17163066,transparent 60%),var(--bg); color:var(--text); font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; -webkit-font-smoothing:antialiased; }
button,input,select,textarea { font:inherit; }
button { cursor:pointer; }
button,input,select,textarea { color:var(--text); }
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible,a:focus-visible { outline:2px solid var(--accent-1); outline-offset:3px; }
button { transition:background .15s,border-color .15s; }
button:disabled { opacity:.55; cursor:wait; }
button:hover { border-color:var(--accent-1); }
button,select { min-height:34px; }
::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-thumb { background:#35425a; border-radius:8px; }
.skip { position:fixed; top:-80px; left:16px; z-index:20; background:var(--raised); padding:12px; color:var(--text); }
.skip:focus { top:8px; }
#shell { max-width:1920px; margin:auto; padding:0 24px 20px; }
#masthead { display:flex; align-items:center; gap:20px; min-height:84px; border-bottom:1px solid var(--border-soft); }
#brand { display:flex; align-items:center; gap:12px; flex-shrink:0; }
.mark { width:40px; height:42px; display:grid; place-items:center; color:#cbb5ff; font-size:28px; background:linear-gradient(135deg,#282a63,#19172e); border:1px solid #705baf; border-radius:12px; box-shadow:0 0 25px #8663e31a; }
#brand .name { margin:0; font-size:22px; font-weight:740; letter-spacing:-.7px; line-height:1.2; }
#brand .sub { color:var(--text-faint); font-size:11px; margin-top:3px; }
.workspace-name { border-left:1px solid var(--border); padding-left:20px; color:var(--text-dim); font-size:13px; }
.workspace-name span { color:var(--accent-1); margin-right:6px; }
#pause { margin-left:auto; min-width:0; }
.pausebar { display:flex; align-items:center; justify-content:space-between; gap:14px; }
.ptext { min-width:0; }
.big { font-size:12px; font-weight:650; color:var(--ok); }
.why { color:var(--text-dim); font-size:12px; overflow-wrap:anywhere; max-width:420px; }
.pausebar button { border:1px solid #773746; background:#2b1521; color:#ff9db0; padding:8px 14px; border-radius:9px; font-size:12px; font-weight:650; white-space:nowrap; }
.pausebar.stopped { background:#351924; border:1px solid #a0465b; border-radius:10px; padding:10px 14px; }
.pausebar.stopped .big { color:#ffb4c4; }
.pausebar.stopped button { background:#f9d9e1; color:#70243a; border-color:#f9d9e1; }
#overview { padding:22px 0 20px; }
.section-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }
.section-heading h2 { margin:0; font-size:16px; font-weight:650; letter-spacing:-.2px; }
.eyebrow { text-transform:uppercase; letter-spacing:.11em; font-size:10px; font-weight:650; color:var(--text-faint); }
#scope { color:var(--text-faint); font-size:11px; }
#metrics { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:12px; }
.metric { position:relative; overflow:hidden; display:flex; align-items:center; gap:12px; padding:17px 14px; border:1px solid var(--border); border-radius:12px; background:linear-gradient(135deg,#131c2b,#0e1420); box-shadow:var(--shadow); }
.metric::after { content:""; position:absolute; width:70px; height:70px; right:-28px; top:-30px; background:var(--tint,var(--blue)); opacity:.06; filter:blur(12px); border-radius:50%; pointer-events:none; }
.metric-icon { flex:none; width:34px; height:34px; display:grid; place-items:center; color:var(--tint,var(--blue)); background:color-mix(in srgb,var(--tint,var(--blue)) 10%,transparent); border:1px solid color-mix(in srgb,var(--tint,var(--blue)) 18%,transparent); border-radius:10px; font-size:18px; }
.metric-label { color:var(--text-dim); font-size:11px; white-space:nowrap; }
.metric-value { font-size:25px; line-height:1.3; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.8px; }
.metric-value.small { font-size:14px; letter-spacing:0; margin-top:4px; }
.mobile-nav { display:none; }
#app { display:grid; grid-template-columns:240px minmax(0,1fr) 310px; align-items:start; gap:16px; }
#side,#inspector { min-width:0; display:flex; flex-direction:column; gap:16px; }
.panel { border:1px solid var(--border); border-radius:13px; background:linear-gradient(145deg,#111a28,#0d1420); box-shadow:var(--shadow); min-width:0; }
.panel-title { padding:14px 16px; display:flex; align-items:center; gap:8px; border-bottom:1px solid var(--border-soft); }
.panel-title h2 { font-size:13px; font-weight:650; margin:0; }
.panel-icon { color:var(--blue); font-size:16px; }
.count { margin-left:auto; color:var(--text-dim); background:var(--raised); border:1px solid var(--border); border-radius:6px; padding:0 6px; font-size:10px; font-variant-numeric:tabular-nums; }
.panel-body { padding:12px; }
#agents,#offline,#claims,#reviews,#projects { display:flex; flex-direction:column; gap:8px; }
.card { padding:11px; border-radius:9px; background:#111a28; border:1px solid var(--border-soft); min-width:0; }
.card .head { display:flex; align-items:center; gap:9px; }
.avatar { width:32px; height:32px; border-radius:10px; display:grid; place-items:center; flex:none; font-size:13px; font-weight:750; position:relative; color:white; }
.avatar .dot { position:absolute; right:-2px; bottom:-2px; width:9px; height:9px; border:2px solid var(--surface); border-radius:50%; background:var(--text-faint); }
.avatar .dot.on { background:var(--ok); }
.card .who { min-width:0; }
.card .name { font-size:13px; font-weight:650; overflow-wrap:anywhere; }
.card .id { font-size:10px; color:var(--text-faint); overflow-wrap:anywhere; }
.card .line { font-size:12px; color:var(--text-dim); overflow-wrap:anywhere; margin-top:7px; }
.card .tags { margin-top:8px; display:flex; flex-wrap:wrap; gap:4px; }
.card.offline { background:#0d1420; }
.card.bridge { border-style:dashed; }
.card.alert { border-color:var(--bad); }
.role { color:var(--text-faint); font-size:9px; text-transform:uppercase; letter-spacing:.07em; margin-left:auto; }
#offlineBox { margin-top:12px; }
summary { cursor:pointer; color:var(--text-dim); font-size:12px; }
#offline { padding-top:8px; }
.empty { padding:8px 2px; color:var(--text-faint); font-size:12px; }
.path { font:11px/1.5 ui-monospace,"SF Mono",monospace; overflow-wrap:anywhere; color:var(--text-dim); }
.tag { display:inline-block; font-size:10px; padding:2px 7px; border-radius:5px; color:var(--text-dim); background:#25314980; max-width:100%; overflow-wrap:anywhere; }
.tag.ok { background:#173f354f; color:var(--ok); }
.tag.warn { background:#5541205c; color:var(--warn); }
.tag.bad { background:#5a27354f; color:var(--bad); }
.tag.work { background:#4332675c; color:#c1a5ff; }
.tag.gone { color:var(--text-faint); }
#main { display:flex; flex-direction:column; min-width:0; height:calc(100dvh - 256px); min-height:540px; max-height:1000px; border-color:#3b3359; }
#topbar { padding:14px 16px 12px; border-bottom:1px solid var(--border-soft); }
#topbar .title { display:flex; align-items:center; gap:8px; font-size:14px; font-weight:650; }
#topbar .title .panel-icon { color:var(--accent-1); }
#topbar .title #status { margin-left:auto; font-size:11px; font-weight:400; display:flex; gap:6px; align-items:center; color:var(--text-faint); }
#status .dot { width:6px; height:6px; background:var(--text-faint); border-radius:50%; flex:none; }
#status .dot.live { background:var(--ok); }
#status .dot.retry { background:var(--warn); }
.tools { display:flex; align-items:center; flex-wrap:wrap; gap:8px; margin-top:12px; }
.filters { display:flex; flex-wrap:wrap; gap:3px; }
.filter { padding:5px 9px; min-height:30px; border:1px solid transparent; background:transparent; color:var(--text-dim); font-size:11px; border-radius:7px; }
.filter.on { background:#342453; color:#d9c7ff; border-color:#60458c; }
.search { margin-left:auto; min-width:120px; flex:1; max-width:220px; }
#search { width:100%; min-height:32px; font-size:11px; border:1px solid var(--border); border-radius:7px; background:#0b121e; padding:6px 9px; }
.system-toggle { display:flex; align-items:center; gap:5px; font-size:11px; color:var(--text-faint); margin-left:auto; cursor:pointer; }
#feed { flex:1; min-height:200px; overflow-y:auto; padding:16px; display:flex; flex-direction:column; align-items:flex-start; gap:12px; overscroll-behavior:contain; }
.bubble { width:94%; padding:12px 13px; border-radius:10px; background:#141e2c; border:1px solid #2a384e; border-left:2px solid #497ab3; }
.bubble.mine { align-self:flex-end; background:linear-gradient(120deg,#241e3b,#192035); border-color:#45365f; border-left-color:#a47de9; }
.bubble.task { border-left-color:var(--warn); }
.bubble.result { border-left-color:var(--ok); }
.bubble.system { border-left-color:#7b86a2; background:#101824; }
.bubble.selected { border-color:var(--accent-1); box-shadow:0 0 0 1px #ac8bff26; }
.bubble.hidden { display:none; }
.bubble .meta { display:flex; align-items:center; flex-wrap:wrap; gap:5px 7px; color:var(--text-faint); font-size:10px; margin-bottom:7px; }
.bubble .meta .who { color:var(--text); font-size:11px; font-weight:650; }
.bubble .meta .num { margin-left:auto; font:10px ui-monospace,monospace; }
.bubble .text { font-size:13px; line-height:1.65; white-space:pre-wrap; overflow-wrap:anywhere; }
.bubble .text.clip { max-height:10em; overflow:hidden; mask-image:linear-gradient(#000 65%,transparent); }
.more { margin-top:8px; border:0; background:transparent; color:var(--accent-1); padding:0; font-size:11px; }
.kind { display:inline-block; font-size:9px; font-weight:700; padding:2px 6px; border-radius:4px; text-transform:uppercase; letter-spacing:.04em; }
.kind-chat { background:#23466c; color:#a1d5ff; }
.kind-task { background:#60451e; color:#ffcf7f; }
.kind-result { background:#1c4b3f; color:#78edbf; }
.kind-broadcast { background:#413659; color:#ceb2fc; }
.kind-control { background:#562c3e; color:#ffadc3; }
.actions,.btns { display:flex; flex-wrap:wrap; gap:6px; margin-top:9px; }
.btn { font-size:10px; padding:5px 9px; border:1px solid #34425a; border-radius:6px; background:#1b2639; color:var(--text-dim); }
.btn.detail { color:#d1b8ff; }
#compose { padding:14px 16px; border-top:1px solid #3b3359; background:linear-gradient(120deg,#181829,#111925); border-radius:0 0 13px 13px; display:flex; flex-direction:column; gap:9px; box-shadow:0 -6px 24px #6a42b90a; }
#compose .row { display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
.seg { display:inline-flex; padding:3px; border-radius:8px; background:#0d1421; border:1px solid var(--border); }
.seg button { min-height:29px; border:0; border-radius:5px; background:transparent; color:var(--text-dim); padding:4px 12px; font-size:11px; }
.seg button.on { background:var(--grad); color:white; box-shadow:0 2px 8px #824de326; }
#compose label { display:flex; align-items:center; gap:8px; color:var(--text-faint); font-size:11px; }
#to,#taskPicker { background:#0b121e; border:1px solid var(--border); border-radius:7px; padding:6px 9px; font-size:11px; max-width:100%; }
#to { width:185px; }
#hint { font-size:10px; line-height:1.6; color:var(--text-faint); }
#hint.task { color:var(--warn); }
#text { flex:1; width:100%; min-width:0; resize:vertical; min-height:54px; max-height:30vh; background:#0c1420; border:1px solid var(--border); border-radius:8px; padding:9px 11px; font-size:12px; }
#sendBtn { align-self:stretch; border:1px solid #9271e0; border-radius:8px; background:var(--grad); color:#fff; padding:8px 17px; font-size:12px; font-weight:650; }
#taskPicker { width:100%; margin-bottom:12px; }
.detail-title { font-size:12px; margin:0 0 8px; font-weight:650; }
.detail-section { margin-top:16px; padding-top:12px; border-top:1px solid var(--border-soft); }
.task-description .detail-text { margin-top:8px; max-height:280px; overflow:auto; }
.detail-section summary { font-size:11px; }
.detail-section h3 { font-size:11px; margin:0 0 8px; color:var(--text-dim); }
.detail-text { font-size:11px; line-height:1.65; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--text-dim); }
.detail-grid { display:grid; grid-template-columns:76px minmax(0,1fr); gap:8px; font-size:12px; margin:0; }
.detail-grid dt { color:var(--text-faint); }
.detail-grid dd { margin:0; overflow-wrap:anywhere; }
.note { font-size:10px; line-height:1.7; color:var(--text-faint); margin:8px 0 0; }
.event-list { list-style:none; margin:0; padding:0; }
.event-list li { border-left:1px solid #3d3658; padding:0 0 14px 12px; position:relative; font-size:11px; overflow-wrap:anywhere; }
.event-list li::before { content:""; position:absolute; left:-3px; top:6px; width:5px; height:5px; border-radius:50%; background:var(--accent-1); }
.event-list li:last-child { padding-bottom:0; }
.event-list .event-meta { color:var(--text-faint); font-size:10px; margin-bottom:4px; }
.event-list p { margin:0; white-space:pre-wrap; }
.project-name { display:flex; align-items:center; gap:8px; font-size:12px; font-weight:600; }
.project-name span { color:var(--accent-1); }
#inspector,#main,#side,#projectPanel,#historyPanel,#overview { scroll-margin-top:150px; }
.sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); border:0; white-space:nowrap; }
@media (min-width:1500px) { #app { grid-template-columns:260px minmax(0,1fr) 340px; gap:20px; } .bubble .text { font-size:13px; } }
@media (max-width:1199px) {
  #shell { padding:0 18px 20px; }
  #metrics { grid-template-columns:repeat(3,minmax(0,1fr)); }
  #app { grid-template-columns:220px minmax(0,1fr); }
  #side { grid-row:1 / span 2; }
  #inspector { grid-column:2; display:grid; grid-template-columns:1fr 1fr; align-items:start; }
  #taskPanel { grid-column:1 / -1; }
  #main { height:660px; min-height:540px; }
  .workspace-name { display:none; }
}
@media (max-width:760px) {
  #shell { padding:0 12px 16px; }
  #masthead { flex-wrap:wrap; gap:12px; padding:16px 0; }
  #brand .name { font-size:20px; }
  #pause { margin-left:0; width:100%; }
  .pausebar { gap:10px; }
  .pausebar button { white-space:normal; text-align:center; }
  #overview { padding:18px 0; }
  .section-heading { flex-wrap:wrap; gap:4px; }
  #metrics { gap:8px; }
  .metric { padding:12px 10px; gap:8px; }
  .metric-icon { display:none; }
  .metric-label { font-size:10px; white-space:normal; }
  .metric-value { font-size:23px; }
  .mobile-nav { display:flex; gap:8px; margin:0 0 14px; }
  .mobile-nav a { flex:1; padding:8px; text-align:center; color:var(--text-dim); border:1px solid var(--border); border-radius:8px; text-decoration:none; background:var(--surface); font-size:12px; }
  #main { order:0; }
  #inspector { order:1; }
  #side { order:2; }
  #app { display:flex; flex-direction:column; gap:14px; }
  #side,#main,#inspector { width:100%; }
  #side { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  #agentPanel { grid-column:1 / -1; }
  #agents { display:grid; grid-template-columns:1fr 1fr; }
  #main { height:740px; min-height:550px; max-height:none; }
  #inspector { display:flex; }
  #topbar,#feed,#compose { padding-left:12px; padding-right:12px; }
  .bubble { width:98%; }
  #statusText { max-width:120px; }
  #compose label { flex:1; }
  #to { flex:1; width:auto; min-width:0; }
  #text { flex-basis:100%; }
  #sendBtn { margin-left:auto; min-height:40px; }
}
@media (max-width:400px) { #agents { grid-template-columns:1fr; } #side { display:flex; } .filters { gap:0; } .filter { padding:5px 7px; } }
@media (prefers-reduced-motion:reduce) { *,*::before,*::after { transition:none!important; animation:none!important; scroll-behavior:auto!important; } }
/* Rozvržení a světlo podle Michalovy předlohy; navigace vede jen na existující části. */
#shell { padding:0 14px 16px; }
#masthead { min-height:62px; gap:24px; position:sticky; top:0; z-index:10; background:#080c15f5; }
#brand { gap:9px; }
#brand .name { font-size:21px; }
.mark { border:0; border-radius:0; background:none; box-shadow:none; width:40px; }
.mark svg { width:40px; height:40px; filter:drop-shadow(0 0 6px #8670ff66); }
.workspace-name { display:none; }
.primary-nav { display:flex; align-items:center; gap:4px; }
.primary-nav a,.primary-nav button { display:flex; align-items:center; gap:6px; border:1px solid transparent; background:transparent; text-decoration:none; color:var(--text-dim); padding:7px 11px; border-radius:9px; font-size:12px; white-space:nowrap; }
.primary-nav .current { background:linear-gradient(140deg,#4a2b70,#251c40); color:#e5d6ff; border-color:#62418e; box-shadow:0 0 16px #9448f329,inset 0 1px 8px #ae69ff24; }
.primary-nav span { color:#b6c5e0; }
#overview { padding:12px 0 14px; }
.section-heading { margin-bottom:7px; }
.section-heading h2 { font-size:12px; color:var(--text-dim); font-weight:500; }
#scope { font-size:10px; }
#metrics { gap:12px; }
.metric { padding:12px; border-radius:11px; border-color:color-mix(in srgb,var(--tint,var(--blue)) 23%,#1c263a); box-shadow:0 0 16px color-mix(in srgb,var(--tint,var(--blue)) 6%,transparent),inset 0 0 15px color-mix(in srgb,var(--tint,var(--blue)) 4%,transparent); }
.metric-icon { border-radius:50%; width:38px; height:38px; font-size:21px; box-shadow:0 0 16px color-mix(in srgb,var(--tint,var(--blue)) 12%,transparent); }
#app { grid-template-columns:230px minmax(0,1fr) 300px; gap:14px; }
.panel { border-color:#26374d; box-shadow:0 0 14px #366ab509,inset 0 0 12px #487cc908; background:linear-gradient(130deg,#111b2a,#0a111c 75%); }
#main { border-color:#46366d; box-shadow:0 0 20px #8b4af31c,inset 0 0 12px #7d4bcc0b; height:calc(100dvh - 196px); min-height:540px; }
#topbar { display:grid; grid-template-columns:1fr auto; gap:6px 10px; padding:10px 14px; }
#topbar .title { font-size:13px; min-width:0; }
#topbar .title #status { margin-left:10px; font-size:10px; }
.filter-tools { margin:0; }
.feed-tools { grid-column:1 / -1; margin:0; gap:10px; }
.feed-tools .note { margin:0; }
.feed-tools .search { max-width:180px; }
#search { min-height:27px; padding:4px 8px; }
#feed { padding:12px 14px; gap:10px; }
.bubble { width:88%; background:linear-gradient(145deg,#172130,#101825); border-color:#30415a; box-shadow:inset 0 1px 6px #6287b90a; }
.bubble.mine { width:78%; background:linear-gradient(140deg,#292244,#151e30); border-color:#504171; box-shadow:inset 0 2px 12px #9b61f514; }
.bubble.system { width:98%; }
.bubble .text { font-size:13px; }
.bubble .meta { font-size:11px; }
#compose { box-shadow:0 -2px 18px #9d4bfa1c,inset 0 0 12px #8843d90b; padding:10px 14px; }
#sendBtn { box-shadow:0 0 16px #9b5cff2b,inset 0 1px 6px #e4b5ff44; }
.panel-title { padding:12px; }
.panel-title h2 { font-size:13px; }
.panel-body { padding:10px; }
.card { background:linear-gradient(140deg,#142030,#101723); }
.card .agent-work { display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; }
.card.selected-agent { border-color:#8754cc; background:linear-gradient(130deg,#33254e,#151d2e); box-shadow:0 0 12px #9853f325,inset 0 0 12px #aa6df610; }
#timelineTitle { color:var(--text); }
#taskTimeline .event-list li::before { width:7px; height:7px; left:-4px; background:var(--accent-1); box-shadow:0 0 8px #a16af53b; }
#taskTimeline summary { font-size:11px; color:var(--text-dim); }
#taskTimeline .detail-text { margin-top:7px; }
#taskTimeline .event-list { max-height:170px; overflow-y:auto; padding-left:4px; }
#inspector { align-items:stretch; }
@media(min-width:1500px) { #app { grid-template-columns:250px minmax(0,1fr) 330px; gap:16px; } }
@media(max-width:1250px) { #masthead { gap:12px; } .primary-nav a,.primary-nav button { padding:7px; font-size:11px; } }
@media(max-width:1199px) {
  #app { grid-template-columns:220px minmax(0,1fr); }
  #inspector { grid-column:2; align-items:stretch; }
  #timelinePanel,#taskPanel { grid-column:1 / -1; }
  #main { height:700px; }
  .primary-nav { display:none; }
  #topbar { display:block; }
  .filter-tools { margin-top:8px; }
  .feed-tools { margin-top:8px; }
}
@media(max-width:760px) {
  #shell { padding:0 12px 16px; }
  #masthead { padding:12px 0; }
  #overview { padding:14px 0; }
  .metric { padding:10px; }
  .metric-icon { display:none; }
  #app { display:flex; }
  #main { height:740px; }
  .bubble,.bubble.mine { width:98%; }
  #inspector { display:flex; align-items:stretch; }
  #topbar,#feed,#compose { padding-left:12px; padding-right:12px; }
}
</style>
<a class="skip" href="#main">Přejít do místnosti</a>
<div id="shell">
  <header id="masthead">
    <div id="brand"><div class="mark" aria-hidden="true"><svg viewBox="0 0 48 48" fill="none"><path d="M23 5 4 40h22" stroke="#77bfff" stroke-width="4" stroke-linejoin="round"/><path d="M26 9 44 41H18" stroke="#bb8cff" stroke-width="4" stroke-linejoin="round"/><path d="m24 19-9 16h18z" stroke="#938aff" stroke-width="3" stroke-linejoin="round"/></svg></div><div><h1 class="name">Agent Lease</h1><div class="sub">Společný prostor pro tvoje agenty</div></div></div>
    <nav class="primary-nav" aria-label="Hlavní navigace"><a class="current" href="#overview"><span aria-hidden="true">✣</span>Přehled</a><button id="navTasks" type="button"><span aria-hidden="true">▣</span>Úkoly</button><a href="#side"><span aria-hidden="true">♧</span>Agenti</a><a href="#projectPanel"><span aria-hidden="true">▱</span>Repozitáře</a><a href="#historyPanel"><span aria-hidden="true">◷</span>Historie</a></nav>
    <div id="pause"><div class="pausebar"><div class="big">Načítám stav systému…</div></div></div>
  </header>
  <section id="overview" aria-labelledby="overviewTitle">
    <div class="section-heading"><h2 id="overviewTitle">Přehled místnosti</h2><span id="scope">Úkoly v načtené historii · nejvýše 200 posledních zpráv</span></div>
    <div id="metrics">
      <div class="metric" style="--tint:var(--blue)"><span class="metric-icon" aria-hidden="true">♧</span><div><div class="metric-label">Aktivní agenti</div><div class="metric-value" id="metricAgents">—</div></div></div>
      <div class="metric" style="--tint:var(--accent-1)"><span class="metric-icon" aria-hidden="true">▷</span><div><div class="metric-label">Běžící úkoly</div><div class="metric-value" id="metricRunning">—</div></div></div>
      <div class="metric" style="--tint:var(--warn)"><span class="metric-icon" aria-hidden="true">▱</span><div><div class="metric-label">Čekající úkoly</div><div class="metric-value" id="metricQueued">—</div></div></div>
      <div class="metric" style="--tint:var(--ok)"><span class="metric-icon" aria-hidden="true">✓</span><div><div class="metric-label">Dokončené úkoly</div><div class="metric-value" id="metricDone">—</div></div></div>
      <div class="metric" style="--tint:var(--bad)"><span class="metric-icon" aria-hidden="true">◇</span><div><div class="metric-label">Úkoly ke kontrole</div><div class="metric-value" id="metricReview">—</div></div></div>
      <div class="metric" style="--tint:var(--ok)"><span class="metric-icon" aria-hidden="true">⌁</span><div><div class="metric-label">Stav systému</div><div class="metric-value small" id="metricSystem">Načítání…</div></div></div>
    </div>
  </section>
  <nav class="mobile-nav" aria-label="Přejít na část přehledu"><a href="#main">Místnost</a><a href="#side">Agenti</a><a href="#inspector">Detail</a></nav>
  <div id="app">
    <aside id="side" aria-label="Účastníci a pracovní prostor">
      <section class="panel" id="agentPanel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">♧</span><h2>Agenti a mosty</h2><span id="agentCount" class="count">—</span></div><div class="panel-body"><div id="agents"></div><details id="offlineBox"><summary id="offlineSummary">Offline</summary><div id="offline"></div></details></div></section>
      <section id="projectPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">▣</span><h2>Repozitáře v review</h2></div><div class="panel-body" id="projects"><div class="empty">Data zatím nejsou načtená.</div></div></section>
      <section class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">⌑</span><h2>Zamčené cesty</h2></div><div class="panel-body" id="claims"></div></section>
    </aside>
    <main id="main" class="panel" tabindex="-1">
      <div id="topbar">
        <div class="title"><span class="panel-icon" aria-hidden="true">✣</span> Místnost agentů <div id="status" role="status"><span class="dot"></span><span id="statusText">připojuji…</span></div></div>
        <div class="tools filter-tools"><div class="filters" role="group" aria-label="Filtrovat zprávy">
          <button class="filter on" data-filter="all" aria-pressed="true">Vše</button><button class="filter" data-filter="task" aria-pressed="false">Úkoly</button><button class="filter" data-filter="result" aria-pressed="false">Výsledky</button><button class="filter" data-filter="system" aria-pressed="false">Systém</button>
        </div></div>
        <div class="tools feed-tools"><span id="messageCount" class="note">Načítám zprávy…</span><label class="search"><span class="sr-only">Hledat ve zprávách</span><input id="search" type="search" placeholder="Hledat text, agenta, #ID…"></label><label class="system-toggle"><input type="checkbox" id="showSystem"> systémové příjemky</label></div>
      </div>
      <div id="feed" aria-label="Zprávy místnosti"></div>
      <div id="compose">
        <div class="row"><div class="seg" id="kindSeg"><button type="button" data-kind="chat" class="on" aria-pressed="true">Zpráva</button><button type="button" data-kind="task" aria-pressed="false">Úkol</button></div><label>Komu <select id="to"></select></label></div>
        <div id="hint"></div>
        <div class="row"><label class="sr-only" for="text">Text zprávy nebo úkolu</label><textarea id="text" rows="2" placeholder="Napiš zprávu… (Enter pošle, Shift+Enter nový řádek)"></textarea><button id="sendBtn" type="button">Poslat ↗</button></div>
      </div>
    </main>
    <aside id="inspector" aria-label="Detail a zaznamenané události">
      <section id="timelinePanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◎</span><h2 id="timelineTitle">Záznamy úkolu</h2></div><div class="panel-body" id="taskTimeline"><div class="empty">Vyber úkol z místnosti.</div></div></section>
      <section id="taskPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◎</span><h2 id="detailTitle" tabindex="-1">Detail úkolu</h2></div><div class="panel-body"><label class="sr-only" for="taskPicker">Vybrat úkol z načtené historie</label><select id="taskPicker"><option value="">Vyber úkol z místnosti</option></select><div id="taskDetail"><div class="empty">Vyber úkol. Zobrazí se jeho skutečný stav a navázané zprávy.</div></div></div></section>
      <section class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◇</span><h2>Review commitů</h2><span id="reviewCount" class="count">—</span></div><div class="panel-body" id="reviews"></div></section>
      <section id="historyPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◷</span><h2>Poslední události</h2></div><div class="panel-body" id="recentEvents"><div class="empty">Čekám na zaznamenané zprávy.</div></div></section>
    </aside>
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
// Pouze prezentace existujících dat; žádné nové endpointy nebo odvozování fází.
const messageCache = new Map();
const expandedMessages = new Set();
let activeFilter = "all";
let selectedTask = null;
let renderingRoom = false;
let latestPeers = [];
let lastPaused; // undefined = stav dosud neznáme, null = provoz povolen
let connectionMode = "loading";

function historyMessages() { return [...messageCache.values()].sort((a, b) => a.id - b.id); }
function taskState(m) { const s = STATE[m.state] || [m.state || "stav není dostupný", ""]; return tag(s[0], s[1]); }
function roleOf(agent) { return agent === me ? "operátor" : agent === "broker" ? "systém" : String(agent).endsWith("-bridge") ? "most" : "agent"; }
function applyFilters() {
  const query = document.getElementById("search").value.trim().toLocaleLowerCase("cs-CZ");
  let visible = 0;
  for (const m of messageCache.values()) {
    const div = document.getElementById("msg-" + m.id);
    if (!div) continue;
    const system = isSystem(m);
    const matchesKind = activeFilter === "all" ? (!system || showSystemEl.checked)
      : activeFilter === "system" ? system : !system && m.kind === activeFilter;
    const haystack = ["#" + m.id, m.text, m.agent, m.recipient, nameOf(m.agent), nameOf(m.recipient)].join(" ").toLocaleLowerCase("cs-CZ");
    const show = matchesKind && (!query || haystack.includes(query));
    div.classList.toggle("hidden", !show);
    if (show) visible++;
  }
  document.getElementById("messageCount").textContent = `${visible} z ${messageCache.size} načtených zpráv`;
  let empty = document.getElementById("filterEmpty");
  if (!empty) { empty = document.createElement("div"); empty.id = "filterEmpty"; empty.className = "empty"; feed.appendChild(empty); }
  empty.textContent = messageCache.size ? "Tomuto filtru neodpovídají žádné zprávy." : "Místnost zatím nemá žádné zprávy.";
  empty.hidden = visible > 0;
}
function refreshDashboard() {
  const all = historyMessages();
  const tasks = all.filter(m => m.kind === "task");
  document.getElementById("metricAgents").textContent = latestPeers.filter(p => p.active && roleOf(p.agent) === "agent").length;
  document.getElementById("metricRunning").textContent = tasks.filter(m => m.state === "started").length;
  document.getElementById("metricQueued").textContent = tasks.filter(m => m.state === "pending").length;
  document.getElementById("metricDone").textContent = tasks.filter(m => m.state === "succeeded").length;
  document.getElementById("metricReview").textContent = tasks.filter(m => m.state === "needs_review").length;
  renderSystemMetric();
  const picker = document.getElementById("taskPicker");
  const optionsKey = JSON.stringify(tasks.map(m => [m.id, m.state, m.text.slice(0, 65)]));
  if (picker.dataset.optionsKey !== optionsKey) {
    picker.dataset.optionsKey = optionsKey;
    picker.replaceChildren(new Option("Vyber úkol z místnosti", ""));
    [...tasks].reverse().forEach(m => picker.add(new Option(`#${m.id} · ${(STATE[m.state] || [m.state || "?"])[0]} · ${m.text.slice(0, 65)}`, String(m.id))));
  }
  picker.value = selectedTask === null ? "" : String(selectedTask);
  renderTaskDetail();
  const events = all.filter(m => m.kind === "result" || m.kind === "control").slice(-5).reverse();
  document.getElementById("recentEvents").innerHTML = events.length ? `<ol class="event-list">${events.map(m => `<li><div class="event-meta">#${m.id} · ${esc(nameOf(m.agent))} · ${ago(m.seconds_ago)}</div><p>${esc(m.text.slice(0, 170))}${m.text.length > 170 ? "…" : ""}</p></li>`).join("")}</ol><p class="note">Původní záznamy z načtené historie. Celý text je v místnosti.</p>` : "<div class='empty'>V načtené historii nejsou žádné události.</div>";
  applyFilters();
}
function renderSystemMetric() {
  const el = document.getElementById("metricSystem");
  el.textContent = connectionMode !== "live" ? (connectionMode === "retry" ? "Spojení přerušeno" : "Připojování…")
    : lastPaused === undefined ? "Stav není dostupný" : lastPaused ? "Pozastaveno" : "Provoz povolen";
  el.style.color = connectionMode !== "live" ? "var(--warn)" : lastPaused === undefined ? "var(--text-dim)" : lastPaused ? "var(--bad)" : "var(--ok)";
}
function selectTask(id, focus = false) {
  selectedTask = id === "" || id === null ? null : Number(id);
  document.querySelectorAll(".bubble.selected").forEach(el => el.classList.remove("selected"));
  if (selectedTask !== null) document.getElementById("msg-" + selectedTask)?.classList.add("selected");
  document.getElementById("taskPicker").value = selectedTask === null ? "" : String(selectedTask);
  renderTaskDetail();
  highlightAgent();
  if (focus) { document.getElementById("detailTitle").focus({preventScroll:true}); if (matchMedia("(max-width:1199px)").matches) document.getElementById("inspector").scrollIntoView({block:"start"}); }
}
function highlightAgent() {
  const recipient = messageCache.get(selectedTask)?.recipient;
  document.querySelectorAll("[data-agent]").forEach(card => card.classList.toggle("selected-agent", card.dataset.agent === recipient));
}
function renderTaskDetail() {
  const el = document.getElementById("taskDetail");
  const m = messageCache.get(selectedTask);
  document.getElementById("detailTitle").textContent = m ? `Detail úkolu #${m.id}` : "Detail úkolu";
  document.getElementById("timelineTitle").textContent = m ? `Záznamy úkolu #${m.id}` : "Záznamy úkolu";
  const timeline = document.getElementById("taskTimeline");
  if (!m || m.kind !== "task") {
    timeline.innerHTML = `<div class="empty">Vyber úkol z načtené historie.</div>`;
    el.dataset.detailKey = "";
    el.innerHTML = `<div class="empty">${selectedTask === null ? "Vyber úkol. Zobrazí se jeho skutečný stav a navázané zprávy." : "Vybraný úkol už není v načtené historii."}</div>`;
    return;
  }
  const related = historyMessages().filter(r => r.reply_to === m.id);
  const detailKey = JSON.stringify([m.id, m.text, m.agent, m.recipient, m.state, m.attempts, m.accept_note, m.last_error, related.map(r => [r.id, r.kind, r.text])]);
  if (el.dataset.detailKey === detailKey) {
    [...el.querySelectorAll("[data-relative-id]"), ...timeline.querySelectorAll("[data-relative-id]")].forEach(span => {
      const record = messageCache.get(Number(span.dataset.relativeId));
      if (record) span.textContent = ago(record.seconds_ago);
    });
    return; // Otevřené zadání/výsledky a fokus přežijí pravidelnou SSE aktualizaci.
  }
  const openSections = el.dataset.taskId === String(m.id) ? [...el.querySelectorAll("details[open][data-section]"), ...timeline.querySelectorAll("details[open][data-section]")].map(d => d.dataset.section) : [];
  el.dataset.taskId = String(m.id);
  el.dataset.detailKey = detailKey;
  const fields = [["ID", esc("#" + m.id)], ["Zadavatel", esc(nameOf(m.agent))], ["Přiřazeno", esc(nameOf(m.recipient))], ["Stav", taskState(m)], ["Pokusy", esc(m.attempts ?? "není dostupné")], ["Zadáno", `<span data-relative-id="${m.id}">${ago(m.seconds_ago)}</span>`]];
  el.innerHTML = `<dl class="detail-grid">${fields.map(([key,value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("")}</dl>`
    + `<details class="detail-section task-description" data-section="assignment"><summary>Zadání úkolu v plném znění</summary><div class="detail-text">${esc(m.text)}</div></details>`
    + (m.accept_note ? `<div class="detail-section"><h3>Potvrzení převzetí</h3><div class="detail-text">${esc(m.accept_note)}</div></div>` : "")
    + (m.last_error ? `<div class="detail-section"><h3>Poslední hlášená chyba</h3><div class="detail-text">${esc(m.last_error)}</div></div>` : "");
  timeline.innerHTML = `<ol class="event-list"><li><div class="event-meta">#${m.id} · zadání úkolu · <span data-relative-id="${m.id}">${ago(m.seconds_ago)}</span></div></li>`
    + related.map(r => `<li><details data-section="reply-${r.id}"><summary><span class="event-meta">#${r.id} · ${esc(KIND[r.kind] || r.kind)} · <span data-relative-id="${r.id}">${ago(r.seconds_ago)}</span></span><br>${esc(r.text.slice(0,80))}${r.text.length > 80 ? "…" : ""}</summary><div class="detail-text">${esc(r.text)}</div></details></li>`).join("")
    + `</ol><p class="note">${related.length ? "Přímo navázané zprávy z načtené historie." : "Žádné navázané zprávy v načtené historii."} Strukturované kroky ani úplná historie přechodů nejsou v API dostupné.</p>`;
  [...el.querySelectorAll("details[data-section]"), ...timeline.querySelectorAll("details[data-section]")].forEach(d => { d.open = openSections.includes(d.dataset.section); });
}
function renderProjects(reviews) {
  const repos = [...new Set(reviews.map(r => r.repo).filter(Boolean))].sort();
  document.getElementById("projects").innerHTML = repos.length ? repos.map(repo => `<div class="card"><div class="project-name"><span aria-hidden="true">▣</span>${esc(repo.split("/").filter(Boolean).pop() || repo)}</div><div class="path">${esc(repo)}</div></div>`).join("") : `<div class="empty">V dostupných review nejsou uvedené repozitáře.</div>`;
}


const NAMES = {
  "claude-code": "Ada · Claude Code", "codex": "Nox · Codex",
  "claude-code-bridge": "Most Ady", "codex-bridge": "Most Noxe",
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
  connectionMode = mode;
  renderSystemMetric();
  statusDot.className = "dot" + (mode === "live" ? " live" : mode === "retry" ? " retry" : "");
  statusText.textContent = mode === "live" ? "živě" : mode === "retry" ? "spojení přerušeno, zkouším znovu…" : "připojuji…";
}

// Systémový šum: potvrzení od brokeru a automatické „✔ taking #…" příjemky.
function isSystem(m) {
  return m.agent === "broker" || (m.kind === "result" && /^✔ taking #/.test(m.text || ""));
}

function renderMessage(incoming) {
  const m = incoming;
  messageCache.set(m.id, m);
  if (messageCache.size > 200) {
    const oldest = Math.min(...messageCache.keys());
    messageCache.delete(oldest);
    expandedMessages.delete(oldest);
    document.getElementById("msg-" + oldest)?.remove();
    if (oldest === m.id) return;
  }
  let div = document.getElementById("msg-" + m.id);
  const fresh = !div;
  const renderKey = JSON.stringify([m.text, m.kind, m.state, m.agent, m.recipient, m.accepted, m.seen, m.accept_note, m.last_error, m.attempts]);
  if (!fresh && div.dataset.renderKey === renderKey) {
    const time = div.querySelector(".message-time");
    if (time) { time.textContent = "· " + ago(m.seconds_ago); time.title = "Stáří podle poslední aktualizace"; }
    if (!renderingRoom) refreshDashboard();
    return;
  }
  if (fresh) div = document.createElement("div");
  div.dataset.renderKey = renderKey;
  div.dataset.messageId = String(m.id);
  div.id = "msg-" + m.id;
  const system = isSystem(m);
  div.className = "bubble" + (m.agent === me ? " mine" : "") + (system ? " system" : "")
    + (system && !showSystemEl.checked ? " hidden" : "");
  const k = m.kind || "chat";
  div.classList.toggle("task", k === "task");
  div.classList.toggle("result", k === "result");
  div.classList.toggle("selected", selectedTask === m.id);
  const to = m.recipient && m.recipient !== "*" ? `→ <span class="who">${esc(nameOf(m.recipient))}</span>` : "";
  // Stav doručení má smysl jen u úkolu; u zprávy stačí, jestli ji příjemce viděl.
  const st = k !== "task" ? "" : m.state && STATE[m.state] ? tag(STATE[m.state][0], STATE[m.state][1]) : (m.state ? tag(m.state) : "");
  const rcpt = (m.recipient && m.recipient !== "*" && k !== "result" && m.agent !== "broker")
    ? (m.accepted ? tag("✓ převzal" + (m.accept_note ? ": " + m.accept_note : ""), "ok")
       : m.seen ? tag("viděl, zatím nepřevzal", "work")
       : tag("ještě nedoručeno")) : "";
  const long = (m.text || "").length > 600 || (m.text || "").split(String.fromCharCode(10)).length > 8;
  div.innerHTML =
    `<div class="meta"><span class="kind kind-${esc(k)}">${esc(KIND[k] || k)}</span>` +
    `<span class="who">${esc(nameOf(m.agent))}</span> ${to}<span class="message-time" title="Stáří podle poslední aktualizace">· ${ago(m.seconds_ago)}</span>` +
    st + rcpt + `<span class="num">#${m.id}</span></div>` +
    `<div class="text${long && !expandedMessages.has(m.id) ? " clip" : ""}">${esc(m.text)}</div>`;
  if (long) {
    const more = document.createElement("button");
    more.className = "more"; more.textContent = expandedMessages.has(m.id) ? "Sbalit" : "Zobrazit celé";
    more.onclick = () => {
      const t = div.querySelector(".text");
      t.classList.toggle("clip");
      if (t.classList.contains("clip")) expandedMessages.delete(m.id); else expandedMessages.add(m.id);
      more.textContent = t.classList.contains("clip") ? "Zobrazit celé" : "Sbalit";
    };
    div.appendChild(more);
  }
  if (k === "task") {
    const detail = document.createElement("button");
    detail.className = "btn detail"; detail.textContent = "Detail úkolu ↗";
    detail.setAttribute("aria-label", `Detail úkolu #${m.id}`);
    detail.onclick = () => selectTask(m.id, true);
    div.appendChild(detail);
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
    const next = [...feed.querySelectorAll(".bubble")].find(el => Number(el.dataset.messageId) > m.id);
    feed.insertBefore(div, next || document.getElementById("filterEmpty"));
    if (stick) feed.scrollTop = feed.scrollHeight;
  }
  if (!renderingRoom) refreshDashboard();
}

function renderPause(p) {
  if (p === undefined) return;  // zpráva bez stavu pauzy nesmí přepsat tlačítko
  lastPaused = p;
  renderSystemMetric();
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
    bar.innerHTML = `<div class="ptext"><div class="big">● Provoz povolen</div></div>`;
    b.textContent = "Zastavit všechny agenty";
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
  if (b && b.blocked_task) line += `<br>⌑ #${b.blocked_task.id} ${esc(b.blocked_task.path)} drží ${esc(nameOf(b.blocked_task.held_by))}`;
  return `<div class="card ${p.active ? "" : "offline"} ${roleOf(p.agent) === "most" ? "bridge" : ""}">
    <div class="head"><div class="avatar" style="${avatarStyle(p.agent)}">${esc(initial(p.agent))}<span class="dot ${p.active ? "on" : ""}"></span></div>
      <div class="who"><div class="name">${esc(nameOf(p.agent))}</div>${NAMES[p.agent] || p.agent === me ? `<div class="id">${esc(p.agent)}</div>` : ""}</div><span class="role">${roleOf(p.agent)}</span></div>
    ${line ? `<div class="line agent-work">${line}</div>` : ""}
    <div class="tags">${tags.join("")}</div></div>`;
}

const REVIEW = {
  working: ["rozpracováno", ""], awaiting_review: ["čeká na review", "work"],
  changes_requested: ["požadované změny", "warn"], approved: ["schváleno", "ok"],
  escalated: ["ČEKÁ NA TEBE", "bad"], closed: ["uzavřeno", "gone"],
};
function renderReviews(list) {
  if (list === undefined) return;
  renderProjects(list);
  document.getElementById("reviewCount").textContent = list.length;
  const el = document.getElementById("reviews");
  el.innerHTML = "";
  if (!list.length) { el.innerHTML = "<div class='empty'>žádné otevřené review</div>"; return; }
  // Co čeká na člověka, nahoru.
  [...list].sort((x, y) => (y.state === "escalated") - (x.state === "escalated")).forEach(v => {
    const st = REVIEW[v.state] || [v.state, ""];
    const card = document.createElement("div");
    card.className = "card" + (v.state === "escalated" ? " alert" : "");
    const who = `${esc(nameOf(v.author))}${v.reviewer ? " → " + esc(nameOf(v.reviewer)) : ""}`;
    const sha = v.head_commit ? ` · ${esc(v.head_commit.slice(0, 10))}` : "";
    const why = v.verdict_note && v.state !== "approved" ? `<div class="line">„${esc(v.verdict_note.slice(0, 240))}"</div>` : "";
    card.innerHTML = `<div class="name">#${v.id} ${esc(v.topic)}</div>`
      + `<div class="line">${who}${sha}</div><div class="path">${esc(v.repo || "")}</div>${why}`
      + `<div class="tags">${tag(st[0], st[1])}${tag(`kolo oprav ${v.fix_cycles}/${v.max_fix_cycles}`, v.cycles_left ? "" : "bad")}</div>`;
    if (v.state === "escalated") {
      const btns = document.createElement("div");
      btns.className = "btns";
      const grant = document.createElement("button");
      grant.className = "btn"; grant.textContent = "Povolit ještě 1 kolo";
      grant.onclick = () => {
        const reason = prompt("Proč povolit další kolo oprav? (zaznamená se)");
        if (reason) post("/api/review-grant", {id: v.id, reason});
      };
      const close = document.createElement("button");
      close.className = "btn"; close.textContent = "Uzavřít review";
      close.onclick = () => {
        const reason = prompt("Proč uzavřít? (zaznamená se)", "převezmu sám");
        if (reason !== null) post("/api/review-close", {id: v.id, reason});
      };
      btns.append(grant, close);
      card.appendChild(btns);
    }
    el.appendChild(card);
  });
}

function renderRoom(r) {
  if (r.me) me = r.me;
  renderReviews(r.reviews);
  renderingRoom = true;
  (r.messages || []).forEach(renderMessage);
  renderingRoom = false;
  renderPause(r.paused);
  const board = {};
  (r.board || []).forEach(b => { board[b.agent] = b; });
  const peers = (r.peers || []).filter(p => !String(p.agent).startsWith("untrusted-"));
  (r.board || []).forEach(b => { if (!peers.some(p => p.agent === b.agent)) peers.push({agent: b.agent, active: false, status: b.status}); });
  latestPeers = peers;
  const coreAgent = p => ["claude-code", "codex"].includes(p.agent);
  const online = peers.filter(p => p.active || coreAgent(p)), offline = peers.filter(p => !p.active && !coreAgent(p));
  document.getElementById("agentCount").textContent = peers.length;
  document.getElementById("agents").innerHTML = online.map(p => agentCard(p, board[p.agent])).join("") || "<div class='empty'>nikdo není online</div>";
  document.getElementById("offline").innerHTML = offline.map(p => agentCard(p, board[p.agent])).join("");
  [...document.querySelectorAll("#agents > .card"), ...document.querySelectorAll("#offline > .card")].forEach((card,i) => { card.dataset.agent = [...online,...offline][i].agent; const work = card.querySelector(".agent-work"); if (work) work.title = work.textContent; });
  highlightAgent();
  document.getElementById("offlineSummary").textContent = `Offline (${offline.length})`;
  document.getElementById("offlineBox").style.display = offline.length ? "" : "none";
  document.getElementById("claims").innerHTML = (r.claims || []).map(c => `
    <div class="card"><div class="path">⌑ ${esc(c.path)}</div>
      <div class="line">${esc(nameOf(c.held_by))}${c.purpose ? " · " + esc(c.purpose) : ""} · ještě ${ttl(c.expires_in_seconds)}</div></div>`).join("")
    || "<div class='empty'>nic není zamčené</div>";
  // Komu: skuteční agenti, bez mostů, systému a sebe.
  const names = new Set(["claude-code", "codex"]);
  peers.forEach(p => { const a = String(p.agent); if (a !== me && a !== "broker" && !a.endsWith("-bridge")) names.add(a); });
  const list = [...names].sort((a, b) => (NAMES[a] ? 0 : 1) - (NAMES[b] ? 0 : 1) || a.localeCompare(b));
  if (list.join() !== knownAgents.join()) { knownAgents = list; fillRecipients(); }
  refreshDashboard();
}

function fillRecipients() {
  const prev = toEl.value;
  toEl.innerHTML = "";
  knownAgents.forEach(a => toEl.add(new Option(nameOf(a), a)));
  const all = new Option("Všem v místnosti", "*");
  all.disabled = kind === "task";
  toEl.add(all);
  if ([...toEl.options].some(o => o.value === prev && !o.disabled)) toEl.value = prev;
  syncHint();
}
function setKind(k) {
  kind = k;
  document.querySelectorAll("#kindSeg button").forEach(b => { b.classList.toggle("on", b.dataset.kind === k); b.setAttribute("aria-pressed", b.dataset.kind === k); });
  fillRecipients();
}
function syncHint() {
  const to = toEl.value === "*" ? "všem v místnosti" : nameOf(toEl.value);
  if (kind === "task") {
    hintEl.className = "task";
    hintEl.textContent = `Úkol: ${to} ho převezme a udělá sám, klidně i na pozadí bez tebe. Posílej jen to, co má opravdu udělat.`;
  } else {
    hintEl.className = "";
    hintEl.textContent = `Zpráva pro: ${to}. Jen informace — přečte si ji, až se podívá do místnosti. Nic se samo nespustí.`;
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
    renderRoom(s);
    feed.scrollTop = feed.scrollHeight;
    connect(s.messages.length ? s.messages[s.messages.length - 1].id : 0);
  })
  .catch(() => { updateStatus("retry"); showLoginHelp(); });

function post(url, body) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(r => { if (!r.ok) r.text().then(t => alert(t)); return r.ok; })
    .catch(() => { alert("Spojení se nezdařilo. Zkontroluj stav v místnosti před opakováním akce."); return false; });
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
showSystemEl.addEventListener("change", applyFilters);
document.getElementById("search").addEventListener("input", applyFilters);
document.getElementById("taskPicker").addEventListener("change", e => selectTask(e.target.value));
document.querySelectorAll("[data-filter]").forEach(b => b.onclick = () => {
  activeFilter = b.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach(el => { el.classList.toggle("on", el === b); el.setAttribute("aria-pressed", el === b); });
  applyFilters();
});
document.getElementById("navTasks").onclick = () => {
  document.querySelector('[data-filter="task"]').click();
  document.getElementById("main").scrollIntoView({block:"start"});
};
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
