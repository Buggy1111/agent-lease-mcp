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
            if parts.path == "/api/quality-log":
                if not self._session_ok():
                    return self._reject(403, "bad token")
                try:
                    log = store.quality_log(int(qs.get("run_id", ["0"])[0]),
                                            qs.get("check", [""])[0])
                except (ValueError, TypeError):
                    return self._reject(400, "invalid run")
                if log is None:
                    return self._reject(404, "log not found")
                body = log.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self._security_headers()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
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
            if parts.path == "/api/approve-fix":
                return self._handle_approve_fix()
            if parts.path == "/api/quality-policy-approve":
                data = self._read_json()
                if data is None:
                    return
                try:
                    result = store.quality_human_approve_policy(web_agent, data["run_id"],
                                                                data["commit_sha"], data["reason"])
                except (ValueError, TypeError, KeyError, PermissionError):
                    return self._reject(400, "critical approval requires matching run/commit, reason and passing independent review/checks")
                return self._json_response(result)
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
                "quality_runs": store.quality_runs(limit=30),
                "quality_requests": store.quality_requests(limit=30),
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

        def _handle_approve_fix(self) -> None:
            """Člověk povolí jeden zápisový běh opravy (C1a); politiku nenahrazuje."""
            data = self._read_json()
            if data is None:
                return
            try:
                message_id = int(data["id"])
                recipient = str(data["to"]).strip()
                reason = str(data.get("reason", "")).strip()
            except (KeyError, TypeError, ValueError):
                return self._reject(400, "approve-fix needs a valid 'id' and 'to'")
            if not recipient or not reason:
                return self._reject(400, "approve-fix needs 'id', 'to' and a reason")
            if not store.approve_fix(message_id, recipient, web_agent, reason):
                return self._reject(409, "only a fix that has not run, or was held by its gate, can be approved")
            self._json_response({"id": message_id, "state": "pending", "fix_approved": True})

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
                            "quality_runs": store.quality_runs(limit=30),
                            "quality_requests": store.quality_requests(limit=30),
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
  color-scheme:dark;
  --bg:#070B14; --surface:#0D1423; --surface-2:#131D30;
  --accent-1:#9146FF; --blue:#38A8FF; --ok:#25DFA0; --warn:#F5B94F; --bad:#FF5978;
  --text:#F0F4FF; --text-dim:#94A6C2; --ink:#000;
  --border:rgba(130,160,220,.14);
  --border-soft:color-mix(in srgb,var(--border) 65%,transparent);
  --text-faint:var(--text-dim); --raised:var(--surface-2);
  --accent-text:color-mix(in srgb,var(--accent-1) 45%,var(--text));
  --bad-text:color-mix(in srgb,var(--bad) 65%,var(--text));
  --blue-text:color-mix(in srgb,var(--blue) 70%,var(--text));
  --grad:linear-gradient(120deg,color-mix(in srgb,var(--accent-1) 88%,var(--bg)),color-mix(in srgb,var(--accent-1) 78%,var(--bg)));
  --panel-grad:linear-gradient(135deg,color-mix(in srgb,var(--surface-2) 30%,var(--surface)),var(--surface));
  --shadow:0 6px 22px color-mix(in srgb,var(--ink) 20%,transparent),inset 0 1px 0 color-mix(in srgb,var(--text) 2%,transparent);
  --shadow-hover:0 8px 24px color-mix(in srgb,var(--ink) 28%,transparent),0 0 16px color-mix(in srgb,var(--accent-1) 7%,transparent);
  --ease:cubic-bezier(.22,1,.36,1); --panel-time:200ms; --control-time:160ms;
  --avatar-ada:var(--warn); --avatar-nox:var(--accent-1); --avatar-bridge:var(--blue); --avatar-user:var(--ok);
}
* { box-sizing:border-box; }
[hidden] { display:none!important; }
html { scroll-padding-top:140px; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; -webkit-font-smoothing:antialiased; }
button,input,select,textarea { font:inherit; color:var(--text); }
button,summary,select { cursor:pointer; }
button,select { min-height:36px; }
button { transition:transform var(--control-time) var(--ease),background var(--control-time) var(--ease),border-color var(--control-time) var(--ease),box-shadow var(--control-time) var(--ease); }
button:hover { border-color:var(--accent-text); }
button:active { transform:scale(.98); }
button:disabled { opacity:.6; cursor:wait; }
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible,a:focus-visible,[tabindex]:focus-visible { outline:2px solid var(--blue); outline-offset:3px; }
::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-thumb { background:color-mix(in srgb,var(--text-dim) 35%,var(--surface)); border-radius:8px; }
.skip { position:fixed; top:-80px; left:16px; z-index:30; background:var(--surface-2); padding:12px; color:var(--text); }
.skip:focus { top:8px; }
#shell { max-width:1920px; margin:auto; padding:0 16px 20px; }
#masthead { display:flex; align-items:center; gap:20px; min-height:64px; flex-wrap:wrap; position:sticky; top:0; z-index:10; background:color-mix(in srgb,var(--bg) 97%,transparent); border-bottom:1px solid var(--border); }
#brand { display:flex; align-items:center; gap:10px; flex-shrink:0; }
.mark { width:38px; height:40px; display:grid; place-items:center; }
.mark svg { width:38px; height:38px; filter:drop-shadow(0 0 5px color-mix(in srgb,var(--accent-1) 30%,transparent)); }
.mark .logo-blue { stroke:var(--blue); } .mark .logo-purple { stroke:var(--accent-text); } .mark .logo-inner { stroke:var(--accent-1); }
#brand .name { margin:0; font-size:21px; font-weight:720; letter-spacing:-.6px; line-height:1.25; }
#brand .sub { color:var(--text-dim); font-size:11px; margin-top:2px; }
.primary-nav { display:flex; align-items:center; gap:4px; }
.primary-nav a,.primary-nav button { display:flex; align-items:center; gap:6px; min-height:36px; border:1px solid transparent; background:transparent; text-decoration:none; color:var(--text-dim); padding:6px 9px; border-radius:9px; font-size:12px; white-space:nowrap; }
.primary-nav .current { background:color-mix(in srgb,var(--accent-1) 18%,var(--surface)); color:var(--accent-text); border-color:color-mix(in srgb,var(--accent-1) 40%,var(--border)); box-shadow:0 0 12px color-mix(in srgb,var(--accent-1) 10%,transparent); }
#pause { margin-left:auto; min-width:0; }
.pausebar { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.ptext { min-width:0; }
.big { font-size:12px; font-weight:650; color:var(--ok); }
.why { color:var(--text-dim); font-size:12px; overflow-wrap:anywhere; max-width:420px; }
.pausebar button { border:1px solid color-mix(in srgb,var(--bad) 40%,var(--surface)); background:color-mix(in srgb,var(--bad) 10%,var(--surface)); color:var(--bad-text); padding:8px 12px; border-radius:10px; font-size:12px; font-weight:650; white-space:nowrap; transition:none; animation:none; }
.pausebar button:active { transform:none; }
.pausebar.stopped { background:color-mix(in srgb,var(--bad) 9%,var(--surface)); border:1px solid var(--bad); border-radius:12px; padding:8px 12px; }
.pausebar.stopped .big { color:var(--bad-text); }
.pausebar.stopped button { background:var(--text); color:var(--bg); border-color:var(--text); }
#overview { padding:12px 0 14px; }
.section-heading { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:4px 12px; margin-bottom:8px; }
.section-heading h2 { margin:0; font-size:12px; color:var(--text-dim); font-weight:500; }
#scope { color:var(--text-dim); font-size:11px; }
#metrics { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:12px; }
.metric { position:relative; overflow:hidden; display:flex; align-items:center; gap:12px; padding:13px; border:1px solid var(--border); border-radius:14px; background:var(--panel-grad); box-shadow:var(--shadow); transition:border-color var(--panel-time) var(--ease),box-shadow var(--panel-time) var(--ease),transform var(--panel-time) var(--ease); }
.metric::after { content:""; position:absolute; inset:0; background:radial-gradient(ellipse at top right,color-mix(in srgb,var(--tint,var(--blue)) 7%,transparent),transparent 70%); pointer-events:none; }
.metric-icon { flex:none; width:38px; height:38px; display:grid; place-items:center; border-radius:50%; color:var(--tint,var(--blue)); background:color-mix(in srgb,var(--tint,var(--blue)) 8%,transparent); border:1px solid color-mix(in srgb,var(--tint,var(--blue)) 18%,transparent); box-shadow:0 0 14px color-mix(in srgb,var(--tint,var(--blue)) 8%,transparent); font-size:20px; }
.metric > div { min-width:0; }
.metric-label { color:var(--text-dim); font-size:11px; overflow-wrap:anywhere; }
.metric-value { display:inline-block; font-size:25px; line-height:1.3; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.5px; }
.metric-value.small { font-size:14px; letter-spacing:0; margin-top:4px; }
[data-tone="ok"] { color:var(--ok); } [data-tone="warn"] { color:var(--warn); } [data-tone="bad"] { color:var(--bad-text); } [data-tone="muted"] { color:var(--text-dim); }
.mobile-nav { display:none; }
#app { display:grid; grid-template-columns:230px minmax(0,1fr) 310px; align-items:start; gap:14px; }
#side,#inspector { min-width:0; display:flex; flex-direction:column; gap:14px; }
.panel { border:1px solid var(--border); border-radius:14px; background:var(--panel-grad); box-shadow:var(--shadow); min-width:0; transition:border-color var(--panel-time) var(--ease),box-shadow var(--panel-time) var(--ease); }
.panel-title { padding:12px; display:flex; align-items:center; gap:8px; border-bottom:1px solid var(--border); }
.panel-title h2 { font-size:13px; font-weight:650; margin:0; }
.panel-icon { color:var(--blue); font-size:16px; }
.count { margin-left:auto; color:var(--text-dim); background:var(--surface-2); border:1px solid var(--border); border-radius:6px; padding:0 6px; font-size:11px; font-variant-numeric:tabular-nums; }
.panel-body { padding:12px; }
#agents,#offline,#claims,#reviews,#projects { display:flex; flex-direction:column; gap:8px; }
.card { padding:10px; border-radius:12px; background:var(--panel-grad); border:1px solid var(--border); min-width:0; transition:border-color var(--panel-time) var(--ease),box-shadow var(--panel-time) var(--ease); }
.card .head { display:flex; align-items:center; gap:8px; }
.avatar { width:32px; height:32px; border-radius:10px; display:grid; place-items:center; flex:none; font-size:13px; font-weight:750; position:relative; color:var(--text); background:color-mix(in srgb,var(--avatar-color,var(--accent-1)) 36%,var(--surface)); border:1px solid color-mix(in srgb,var(--avatar-color,var(--accent-1)) 65%,var(--border)); }
.avatar .dot { position:absolute; right:-2px; bottom:-2px; width:9px; height:9px; border:2px solid var(--surface); border-radius:50%; background:var(--text-dim); }
.avatar .dot.on { background:var(--ok); }
.card.interactive:not(.offline) .dot.on { animation:online-presence 2.4s ease-in-out infinite; }
@keyframes online-presence { 0%,100% { opacity:1; } 50% { opacity:.65; } }
.card .who { min-width:0; }
.card .name { font-size:13px; font-weight:650; overflow-wrap:anywhere; }
.card .id { font-size:11px; color:var(--text-dim); overflow-wrap:anywhere; }
.card .line { font-size:12px; color:var(--text-dim); overflow-wrap:anywhere; margin-top:6px; }
.card .tags { margin-top:7px; display:flex; flex-wrap:wrap; gap:4px; }
.card .agent-work { display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; }
.card .last-active { font-size:11px; color:var(--text-dim); margin-top:5px; }
.card.offline { background:var(--surface); }
.card.offline .avatar { --avatar-color:var(--text-dim)!important; }
.card.bridge { border-style:dashed; }
.card.alert { border-color:var(--bad); }
.card.selected-agent { border-color:color-mix(in srgb,var(--accent-1) 70%,var(--border)); background:linear-gradient(130deg,color-mix(in srgb,var(--accent-1) 12%,var(--surface)),var(--surface)); box-shadow:0 0 12px color-mix(in srgb,var(--accent-1) 10%,transparent); }
.role { color:var(--text-dim); font-size:10px; margin-left:auto; }
#offlineBox { margin-top:12px; }
summary { color:var(--text-dim); font-size:12px; }
#offline { padding-top:8px; }
.empty { padding:8px 2px; color:var(--text-dim); font-size:12px; }
.path { font:11px/1.6 ui-monospace,"SF Mono",monospace; overflow-wrap:anywhere; color:var(--text-dim); }
.tag { display:inline-block; font-size:11px; padding:2px 7px; border-radius:6px; color:var(--text-dim); background:var(--surface-2); max-width:100%; overflow-wrap:anywhere; }
.tag.ok { background:color-mix(in srgb,var(--ok) 8%,var(--surface)); color:var(--ok); }
.tag.warn { background:color-mix(in srgb,var(--warn) 8%,var(--surface)); color:var(--warn); }
.tag.bad { background:color-mix(in srgb,var(--bad) 8%,var(--surface)); color:var(--bad-text); }
.tag.work { background:color-mix(in srgb,var(--accent-1) 12%,var(--surface)); color:var(--accent-text); }
.tag.gone { color:var(--text-dim); }
#main { display:flex; flex-direction:column; min-width:0; height:calc(100dvh - 196px); min-height:550px; max-height:1200px; border-color:color-mix(in srgb,var(--accent-1) 28%,var(--border)); }
#topbar { display:flex; flex-wrap:wrap; justify-content:space-between; gap:6px 10px; padding:10px 14px; border-bottom:1px solid var(--border); }
#topbar .title { display:flex; align-items:center; gap:8px; font-size:13px; font-weight:650; min-width:0; flex-wrap:wrap; }
#topbar .title .panel-icon { color:var(--accent-text); }
#status { margin-left:auto; font-size:11px; font-weight:400; display:flex; gap:6px; align-items:center; color:var(--text-dim); }
#status .dot { width:6px; height:6px; background:var(--text-dim); border-radius:50%; flex:none; }
#status .dot.live { background:var(--ok); } #status .dot.retry { background:var(--warn); }
.tools { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
.filters { display:flex; flex-wrap:wrap; gap:3px; }
.filter { padding:5px 9px; min-height:32px; border:1px solid transparent; background:transparent; color:var(--text-dim); font-size:12px; border-radius:8px; }
.filter.on { background:color-mix(in srgb,var(--accent-1) 16%,var(--surface)); color:var(--accent-text); border-color:color-mix(in srgb,var(--accent-1) 45%,var(--border)); }
.feed-tools { flex-basis:100%; }
.feed-tools .note { margin:0; }
.search { margin-left:auto; min-width:120px; flex:1; max-width:200px; }
#search { width:100%; min-height:30px; font-size:12px; border:1px solid var(--border); border-radius:8px; background:var(--bg); padding:5px 9px; }
.system-toggle { display:flex; align-items:center; gap:6px; font-size:11px; color:var(--text-dim); margin-left:auto; cursor:pointer; }
#showSystem { accent-color:var(--accent-1); }
#feed { flex:1; min-height:160px; overflow-y:auto; padding:12px 14px; display:flex; flex-direction:column; align-items:flex-start; gap:10px; overscroll-behavior:contain; }
.bubble { position:relative; flex-shrink:0; width:88%; padding:12px 13px; border-radius:12px; background:var(--panel-grad); border:1px solid var(--border); border-left:2px solid var(--blue); }
.bubble.mine { align-self:flex-end; width:78%; background:linear-gradient(125deg,color-mix(in srgb,var(--accent-1) 9%,var(--surface)),var(--surface)); border-left-color:var(--accent-text); }
.bubble.task { border-left-color:var(--warn); } .bubble.result { border-left-color:var(--ok); }
.bubble.system { width:98%; border-left-color:var(--text-dim); background:var(--surface); }
.bubble.selected { border-color:var(--accent-text); }
.bubble.hidden { display:none; }
.bubble .meta { display:flex; align-items:center; flex-wrap:wrap; gap:5px 7px; color:var(--text-dim); font-size:11px; margin-bottom:7px; }
.bubble .meta .who { color:var(--text); font-size:12px; font-weight:650; }
.bubble .meta .num { margin-left:auto; font:11px ui-monospace,monospace; }
.delivery-meta { display:contents; }
.bubble .text { font-size:13px; line-height:1.65; white-space:pre-wrap; overflow-wrap:anywhere; user-select:text; }
.bubble .text.clip { max-height:10em; overflow:hidden; }
.completion-flash { position:absolute; inset:0; border-radius:inherit; background:var(--ok); opacity:0; pointer-events:none; }
.more { margin:8px 8px 0 0; border:0; background:transparent; color:var(--accent-text); padding:0; font-size:12px; }
.kind { display:inline-block; font-size:10px; font-weight:700; padding:2px 6px; border-radius:5px; text-transform:uppercase; letter-spacing:.04em; background:var(--surface-2); color:var(--text-dim); }
.kind-chat { background:color-mix(in srgb,var(--blue) 12%,var(--surface)); color:var(--blue-text); }
.kind-task { background:color-mix(in srgb,var(--warn) 10%,var(--surface)); color:var(--warn); }
.kind-result { background:color-mix(in srgb,var(--ok) 10%,var(--surface)); color:var(--ok); }
.kind-broadcast { background:color-mix(in srgb,var(--accent-1) 12%,var(--surface)); color:var(--accent-text); }
.kind-control { background:color-mix(in srgb,var(--bad) 10%,var(--surface)); color:var(--bad-text); }
.actions,.btns { display:flex; flex-wrap:wrap; gap:6px; margin-top:9px; }
.btn { font-size:11px; padding:5px 9px; border:1px solid var(--border); border-radius:8px; background:var(--surface-2); color:var(--text-dim); }
.btn.detail { color:var(--accent-text); }
#compose { padding:12px 14px; border-top:1px solid var(--border); background:var(--panel-grad); border-radius:0 0 14px 14px; display:flex; flex-direction:column; gap:9px; }
#compose .row { display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
.seg { display:inline-flex; padding:3px; border-radius:10px; background:var(--bg); border:1px solid var(--border); }
.seg button { min-height:30px; border:0; border-radius:7px; background:transparent; color:var(--text-dim); padding:5px 12px; font-size:12px; }
.seg button.on,#sendBtn { background:var(--grad); color:var(--text); box-shadow:0 0 14px color-mix(in srgb,var(--accent-1) 12%,transparent); }
#compose label { display:flex; align-items:center; gap:8px; color:var(--text-dim); font-size:12px; }
#to,#taskPicker { background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:6px 9px; font-size:12px; max-width:100%; }
#to { width:185px; }
#hint { font-size:11px; line-height:1.6; color:var(--text-dim); }
#hint.task { color:var(--warn); }
#text { flex:1; width:100%; min-width:0; resize:vertical; min-height:54px; max-height:30vh; background:var(--bg); border:1px solid var(--border); border-radius:10px; padding:9px 11px; font-size:13px; }
#text:focus,#search:focus,#to:focus { border-color:var(--accent-text); box-shadow:0 0 0 2px color-mix(in srgb,var(--accent-1) 12%,transparent); }
#sendBtn { align-self:stretch; border:1px solid color-mix(in srgb,var(--accent-1) 60%,var(--text)); border-radius:10px; padding:8px 17px; font-size:13px; font-weight:650; }
#sendBtn:hover,.seg button.on:hover { background:linear-gradient(120deg,color-mix(in srgb,var(--accent-1) 91%,var(--bg)),color-mix(in srgb,var(--accent-1) 82%,var(--bg))); box-shadow:0 0 16px color-mix(in srgb,var(--accent-1) 22%,transparent); }
#taskPicker { width:100%; margin-bottom:12px; }
.detail-section { margin-top:14px; padding-top:12px; border-top:1px solid var(--border); }
.task-description .detail-text { margin-top:8px; max-height:280px; overflow:auto; }
.detail-section summary,.detail-section h3 { font-size:12px; color:var(--text-dim); }
.detail-section h3 { margin:0 0 8px; }
.detail-text { font-size:12px; line-height:1.65; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--text-dim); }
.detail-grid { display:grid; grid-template-columns:76px minmax(0,1fr); gap:8px; font-size:12px; margin:0; }
.detail-grid dt { color:var(--text-dim); } .detail-grid dd { margin:0; overflow-wrap:anywhere; }
.note { font-size:11px; line-height:1.7; color:var(--text-dim); margin:8px 0 0; }
.event-list { list-style:none; margin:0; padding:0 0 0 4px; }
.event-list li { border-left:1px solid var(--border); padding:0 0 14px 12px; position:relative; font-size:12px; overflow-wrap:anywhere; }
.event-list li::before { content:""; position:absolute; left:-4px; top:6px; width:7px; height:7px; border-radius:50%; background:var(--text-dim); }
.event-list li:last-child { padding-bottom:0; }
.event-list .event-meta { color:var(--text-dim); font-size:11px; margin-bottom:4px; }
.event-list p { margin:0; white-space:pre-wrap; }
.timeline-current { position:relative; display:flex; align-items:center; gap:9px; padding:0 0 12px 4px; margin-bottom:12px; border-bottom:1px solid var(--border); font-size:12px; }
.timeline-marker { position:relative; width:9px; height:9px; border-radius:50%; background:currentColor; flex:none; }
.timeline-current[data-tone="work"] { color:var(--accent-text); }
.timeline-current[data-tone="work"] .timeline-marker::after { content:""; position:absolute; inset:-4px; border:1px solid currentColor; border-radius:50%; animation:active-phase 2.4s ease-in-out infinite; }
@keyframes active-phase { 0%,100% { opacity:.4; transform:scale(.9); } 50% { opacity:.8; transform:scale(1.15); } }
#taskTimeline .event-list { max-height:180px; overflow-y:auto; }
#taskTimeline summary { font-size:12px; color:var(--text-dim); }
#taskTimeline .detail-text { margin-top:8px; }
.project-name { display:flex; align-items:center; gap:8px; font-size:12px; font-weight:600; }
.project-name span { color:var(--accent-text); }
#inspector,#main,#side,#projectPanel,#historyPanel,#overview { scroll-margin-top:140px; }
.sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); border:0; white-space:nowrap; }
.panel-trigger { display:none; }
#workspaceDrawer { width:min(560px,calc(100vw - 24px)); max-width:none; height:calc(100dvh - 24px); max-height:none; margin:12px 12px 12px auto; padding:0; color:var(--text); background:var(--bg); border:1px solid var(--border); border-radius:16px; box-shadow:var(--shadow-hover); }
#workspaceDrawer[open] { display:flex; flex-direction:column; }
#workspaceDrawer::backdrop { background:color-mix(in srgb,var(--ink) 65%,transparent); }
.drawer-header { padding:14px; flex:none; display:flex; align-items:center; flex-wrap:wrap; gap:10px; border-bottom:1px solid var(--border); }
.drawer-header h2 { margin:0; font-size:16px; }
#drawerClose { margin-left:auto; min-width:44px; min-height:44px; }
#drawerPause { width:100%; }
#drawerPause #pause { width:100%; margin:0; }
#drawerContent { min-height:0; overflow-y:auto; padding:14px; }
#workspaceDrawer #side,#workspaceDrawer #inspector { display:flex; width:100%; }
#workspaceDrawer #taskTimeline .event-list { max-height:240px; }
body.drawer-open { overflow:hidden; }
body.page-hidden .avatar .dot,body.page-hidden .timeline-marker::after { animation-play-state:paused!important; }
@media(hover:hover) and (pointer:fine) {
  .panel:hover,.card:hover,.metric:hover { border-color:color-mix(in srgb,var(--accent-1) 35%,var(--blue)); box-shadow:var(--shadow-hover); }
  .metric:hover { transform:translateY(-2px); }
}
@media(min-width:1600px) { #app { grid-template-columns:250px minmax(0,1fr) 340px; gap:18px; } }
@media(max-width:1280px) {
  #app { grid-template-columns:200px minmax(0,1fr); }
  #inspector { display:none; }
  #openDetail { display:inline-flex; align-items:center; }
  .primary-nav { display:none; }
  #metrics { grid-template-columns:repeat(3,minmax(0,1fr)); }
  #main { height:calc(100dvh - 260px); min-height:590px; }
  #topbar { display:block; }
  .filter-tools,.feed-tools { margin-top:8px; }
}
@media(max-width:899px) {
  #app { display:block; }
  #side { display:none; }
  #openAgents { display:inline-flex; align-items:center; }
  .mobile-nav { display:flex; gap:8px; margin-bottom:12px; }
  .mobile-nav a,.mobile-nav button { flex:1; display:grid; place-items:center; min-height:44px; padding:8px; text-align:center; color:var(--text-dim); border:1px solid var(--border); border-radius:10px; text-decoration:none; background:var(--surface); font-size:12px; }
  #masthead { gap:12px; flex-wrap:wrap; padding:10px 0; }
  #pause { margin-left:auto; }
  #main { min-height:600px; height:calc(100dvh - 200px); }
}
@media(max-width:599px) {
  #shell { padding:0 12px 16px; }
  #brand .name { font-size:20px; }
  #pause { width:100%; margin-left:0; }
  .pausebar { gap:8px; }
  .pausebar button { min-height:44px; padding:8px 10px; white-space:normal; text-align:center; }
  #overview { padding:12px 0; }
  #metrics { gap:8px; }
  .metric { padding:10px; min-height:76px; }
  .metric-icon { display:none; }
  .metric-label { font-size:11px; }
  .metric-value { font-size:24px; }
  #main { height:calc(100svh - 145px); min-height:650px; }
  .bubble,.bubble.mine { width:100%; }
  #topbar,#feed,#compose { padding-left:10px; padding-right:10px; }
  #status { margin-left:0; }
  #compose label { flex:1; }
  #to { flex:1; width:auto; min-width:0; }
  #text { flex-basis:100%; min-height:72px; }
  #sendBtn { margin-left:auto; }
  button,select,input[type="search"],summary { min-height:44px; }
  button,summary { min-width:44px; }
  summary { padding:10px 0; }
  .more { padding:8px 4px; }
  .seg button { min-height:44px; padding:8px 12px; }
  .filters { gap:4px; }
  .filter { padding:6px 10px; min-height:44px; }
  #search { min-height:44px; }
  .system-toggle { min-height:44px; min-width:44px; padding:6px 0; }
  #showSystem { width:24px; height:24px; }
  #workspaceDrawer { width:calc(100vw - 16px); margin:8px; height:calc(100dvh - 16px); }
  .drawer-header,#drawerContent { padding:12px; }
}
@media(prefers-reduced-motion:reduce) {
  *,*::before,*::after { animation:none!important; transition:none!important; scroll-behavior:auto!important; }
  .metric:hover,button:active { transform:none; }
}
.qg-shield { width:18px; height:18px; flex:none; vertical-align:middle; color:var(--accent-text); }
/* Quality Gate is presentation-only until the phase B integration is reviewed. */
.primary-nav { flex-wrap:wrap; }
.primary-nav [data-view] { transition:background 200ms var(--ease),border-color 200ms var(--ease),box-shadow 200ms var(--ease); }
.primary-nav [data-view]:hover { border-color:var(--accent-text); box-shadow:0 0 12px color-mix(in srgb,var(--accent-1) 12%,transparent); }
.view-page { padding-top:20px; }
.view-header { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-bottom:16px; }
.view-title { font-size:22px; margin:0 0 6px; overflow-wrap:anywhere; }
.view-subtitle { color:var(--text-dim); margin:0; font-size:13px; }
.qg-mode { display:inline-flex; align-items:center; color:var(--accent-text); background:color-mix(in srgb,var(--accent-1) 10%,var(--surface)); border:1px solid color-mix(in srgb,var(--accent-1) 35%,var(--border)); border-radius:8px; padding:5px 8px; font-size:12px; }
.qg-actions { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
.qg-overview { order:-1; border-color:color-mix(in srgb,var(--accent-1) 40%,var(--border)); }
.qg-overview .panel-title { flex-wrap:wrap; }
.qg-overview .panel-title button { margin-left:auto; }
.qg-summary { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:8px; margin:12px 0; }
.qg-stat { min-width:0; padding:10px 7px; border:1px solid color-mix(in srgb,currentColor 30%,var(--border)); background:var(--panel-grad); border-radius:12px; display:flex; flex-direction:column; align-items:flex-start; gap:3px; transition:border-color 200ms var(--ease),box-shadow 200ms var(--ease); }
.qg-stat-name { font-size:10px; overflow-wrap:anywhere; }
.qg-stat strong { font-size:22px; line-height:1.2; font-variant-numeric:tabular-nums; }
[data-tone="work"] { color:var(--accent-text); }
.qg-stat:hover { box-shadow:0 0 12px color-mix(in srgb,currentColor 8%,transparent); }
.qg-scroll { overflow-x:auto; max-width:100%; border:1px solid var(--border); border-radius:12px; }
.qg-table { width:100%; min-width:720px; border-collapse:collapse; font-size:12px; }
.qg-table th,.qg-table td { text-align:left; vertical-align:top; padding:10px; border-bottom:1px solid var(--border); }
.qg-table thead th { font-weight:600; color:var(--text-dim); background:var(--surface); white-space:nowrap; }
.qg-table tbody tr:last-child th,.qg-table tbody tr:last-child td { border-bottom:0; }
.qg-table tbody th { min-width:180px; max-width:270px; }
.qg-repo { padding:0; text-align:left; color:var(--text); background:none; border:0; font-size:12px; font-weight:600; overflow-wrap:anywhere; max-width:100%; }
.qg-repo:hover { color:var(--accent-text); }
.qg-branch { display:block; margin-top:3px; font-weight:400; color:var(--text-dim); overflow-wrap:anywhere; }
.qg-badge { white-space:nowrap; }
.qg-empty { color:var(--text-dim); height:76px; }
.qg-demo-note { color:var(--warn); font-size:12px; margin:10px 0; }
.qg-page-grid { display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:16px; align-items:start; }
.qg-page-grid > .panel { min-width:0; }
.qg-check-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:16px; }
.qg-check-grid h3 { font-size:12px; margin:0 0 8px; }
.qg-check-grid p,#qgProjectDetail p { color:var(--text-dim); font-size:12px; overflow-wrap:anywhere; }
.qg-page-stats { display:flex; flex-wrap:wrap; align-items:center; gap:18px; margin-bottom:16px; }
.qg-page-stats p { margin:0; color:var(--text-dim); font-size:12px; }
.qg-page-stats strong { color:var(--text); }
.qg-page-stats .qg-summary { flex:1; min-width:min(100%,460px); }
.task-quality { margin-top:14px; padding-top:12px; border-top:1px solid var(--border); }
.task-quality h3 { font-size:12px; margin:0 0 8px; }
.placeholder-page { max-width:760px; padding:24px; }
#qgOverviewPanel .qg-table { min-width:650px; }
#qgOverviewPanel .qg-mode { font-size:11px; }
#qgOverviewPanel .qg-stat { padding:8px 5px; }
#qgOverviewPanel .qg-stat strong { font-size:18px; }
@media(min-width:1600px) {
  #app { grid-template-columns:240px minmax(0,1fr) 520px; }
  #inspector { display:grid; grid-template-columns:1fr 1fr; grid-template-areas:"timeline detail" "quality quality" "history history" "reviews reviews"; }
  #timelinePanel { grid-area:timeline; } #taskPanel { grid-area:detail; } #qgOverviewPanel { grid-area:quality; } #historyPanel { grid-area:history; } #reviewPanel { grid-area:reviews; }
  #inspector .panel { min-width:0; }
  #inspector .detail-grid { grid-template-columns:64px minmax(0,1fr); gap:6px; }
}
@media(max-width:1280px) {
  .primary-nav { display:flex; flex-basis:100%; order:3; max-width:100%; overflow-x:auto; flex-wrap:nowrap; padding-bottom:6px; }
  .primary-nav a,.primary-nav button { flex:none; }
  .qg-page-grid { grid-template-columns:1fr; }
  #workspaceDrawer #inspector { display:flex; }
}
@media(max-width:599px) {
  .primary-nav a,.primary-nav button { min-height:44px; }
  .qg-summary { gap:6px; }
  .qg-page-stats .qg-summary { flex-basis:100%; }
  .qg-stat { padding:8px 5px; }
  .qg-stat-name { font-size:10px; }
  .qg-check-grid { grid-template-columns:1fr; }
  .qg-table .qg-repo { min-height:44px; }
}
</style>
<a class="skip" href="#main">Přejít do místnosti</a>
<div id="shell">
  <header id="masthead">
    <div id="brand"><div class="mark" aria-hidden="true"><svg viewBox="0 0 48 48" fill="none"><path d="M23 5 4 40h22" class="logo-blue" stroke-width="4" stroke-linejoin="round"/><path d="M26 9 44 41H18" class="logo-purple" stroke-width="4" stroke-linejoin="round"/><path d="m24 19-9 16h18z" class="logo-inner" stroke-width="3" stroke-linejoin="round"/></svg></div><div><h1 class="name">Agent Lease</h1><div class="sub">Společný prostor pro tvoje agenty</div></div></div>
    <nav class="primary-nav" aria-label="Hlavní navigace"><button class="current" data-view="overview" aria-current="page">✣ Přehled</button><button id="navTasks" type="button">▣ Úkoly</button><button type="button" id="navAgents">♧ Agenti</button><a href="#projectPanel" id="navProjects">▱ Projekty</a><a href="#historyPanel" id="navHistory">◷ Historie</a><button data-view="analytics">⌁ Analytics</button><button data-view="quality"><svg class="qg-shield" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m8 12 3 3 5-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg> Quality Gate</button><button data-view="settings">⚙ Nastavení</button></nav>
    <div id="pause"><div class="pausebar"><div class="big">Načítám stav systému…</div></div></div>
  </header>
  <div id="overviewView" data-view-panel="overview">
  <section id="overview" aria-labelledby="overviewTitle">
    <div class="section-heading"><h2 id="overviewTitle" class="view-title" tabindex="-1">Přehled místnosti</h2><span id="scope">Fronta a review: celá místnost · běžící: aktivní úkol dle přehledu každého agenta</span></div>
    <div id="metrics">
      <div class="metric" style="--tint:var(--blue)"><span class="metric-icon" aria-hidden="true">♧</span><div><div class="metric-label">Aktivní agenti</div><div class="metric-value" id="metricAgents">—</div></div></div>
      <div class="metric" style="--tint:var(--accent-1)"><span class="metric-icon" aria-hidden="true">▷</span><div><div class="metric-label">Běžící úkoly</div><div class="metric-value" id="metricRunning">—</div></div></div>
      <div class="metric" style="--tint:var(--warn)"><span class="metric-icon" aria-hidden="true">▱</span><div><div class="metric-label">Čekající úkoly</div><div class="metric-value" id="metricQueued">—</div></div></div>
      <div class="metric" style="--tint:var(--ok)"><span class="metric-icon" aria-hidden="true">✓</span><div><div class="metric-label" title="Pouze úkoly v nejvýše 200 posledních načtených zprávách">Dokončené v historii</div><div class="metric-value" id="metricDone">—</div></div></div>
      <div class="metric" style="--tint:var(--bad)"><span class="metric-icon" aria-hidden="true">◇</span><div><div class="metric-label">Úkoly ke kontrole</div><div class="metric-value" id="metricReview">—</div></div></div>
      <div class="metric" style="--tint:var(--ok)"><span class="metric-icon" aria-hidden="true">⌁</span><div><div class="metric-label">Stav systému</div><div class="metric-value small" id="metricSystem">Načítání…</div></div></div>
    </div>
  </section>
  <nav class="mobile-nav" aria-label="Přejít na část přehledu"><a href="#main">Místnost</a><button type="button" data-panel="side" aria-haspopup="dialog" aria-expanded="false">Agenti</button><button type="button" data-panel="inspector" aria-haspopup="dialog" aria-expanded="false">Detail</button></nav>
  <div id="app">
    <aside id="side" aria-label="Účastníci a pracovní prostor">
      <section class="panel" id="agentPanel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">♧</span><h2>Agenti a mosty</h2><span id="agentCount" class="count">—</span></div><div class="panel-body"><div id="agents"></div><details id="offlineBox"><summary id="offlineSummary">Offline</summary><div id="offline"></div></details></div></section>
      <section id="projectPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">▣</span><h2>Repozitáře v review</h2></div><div class="panel-body" id="projects"><div class="empty">Data zatím nejsou načtená.</div></div></section>
      <section class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">⌑</span><h2>Zamčené cesty</h2></div><div class="panel-body" id="claims"></div></section>
    </aside>
    <main id="main" class="panel" tabindex="-1">
      <div id="topbar">
        <div class="title"><span class="panel-icon" aria-hidden="true">✣</span> Místnost agentů <button class="btn panel-trigger" id="openAgents" type="button" data-panel="side" aria-haspopup="dialog" aria-expanded="false">Agenti</button><button class="btn panel-trigger" id="openDetail" type="button" data-panel="inspector" aria-haspopup="dialog" aria-expanded="false">Detail</button><div id="status" role="status"><span class="dot"></span><span id="statusText">připojuji…</span></div></div>
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
      <section id="qgOverviewPanel" class="panel qg-overview"><div class="panel-title"><svg class="qg-shield" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m8 12 3 3 5-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg><h2>Quality Gate</h2><button class="btn" data-view="quality">Přehled repozitářů ↗</button></div><div class="panel-body"><span class="qg-mode" data-qg-mode></span><div class="qg-summary" id="qgCompactSummary"></div><div id="qgCompactTable"></div><p class="note">Zobrazují se pouze zaznamenané běhy. Chybějící kontroly nejsou PASS.</p></div></section>
      <section id="timelinePanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◎</span><h2 id="timelineTitle">Záznamy úkolu</h2></div><div class="panel-body" id="taskTimeline"><div class="empty">Vyber úkol z místnosti.</div></div></section>
      <section id="taskPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◎</span><h2 id="detailTitle" tabindex="-1">Detail úkolu</h2></div><div class="panel-body"><label class="sr-only" for="taskPicker">Vybrat úkol z načtené historie</label><select id="taskPicker"><option value="">Vyber úkol z místnosti</option></select><div id="taskDetail"><div class="empty">Vyber úkol. Zobrazí se jeho skutečný stav a navázané zprávy.</div></div></div></section>
      <section id="reviewPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◇</span><h2>Review commitů</h2><span id="reviewCount" class="count">—</span></div><div class="panel-body" id="reviews"></div></section>
      <section id="historyPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◷</span><h2>Poslední události</h2></div><div class="panel-body" id="recentEvents"><div class="empty">Čekám na zaznamenané zprávy.</div></div></section>
    </aside>
  </div>
</div>
  <section id="qualityPage" class="view-page" data-view-panel="quality" hidden aria-labelledby="qualityTitle"><div class="view-header"><div><h2 id="qualityTitle" class="view-title" tabindex="-1"><svg class="qg-shield" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m8 12 3 3 5-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg> Quality Gate</h2><p class="view-subtitle">Kvalita práce napříč repozitáři</p></div><div class="qg-actions"><span class="qg-mode" data-qg-mode></span><button class="btn" data-qg-demo aria-pressed="false">Zobrazit demo</button><button class="btn" data-view="overview">Zpět do místnosti</button></div></div><p class="qg-demo-note">Přehled kontrol pro konkrétní commity. Demo obsahuje pouze ukázková data.</p><div class="qg-page-stats"><p>Kontrolované repozitáře: <strong id="qgRepoCount">—</strong><br>Poslední kontrola: <strong id="qgLastCheck">Není známá</strong></p><div class="qg-summary" id="qgPageSummary"></div></div><div id="qgRequests" role="status"></div><div class="qg-page-grid"><section class="panel"><div class="panel-title"><h2>Výsledky projektů</h2></div><div class="panel-body" id="qgProjectTable"></div></section><section class="panel"><div class="panel-title"><h2 id="qgProjectTitle" tabindex="-1">Detail repozitáře</h2></div><div class="panel-body" id="qgProjectDetail"></div></section></div></section>
  <section class="view-page" data-view-panel="analytics" hidden><div class="panel placeholder-page"><h2 class="view-title" tabindex="-1">Analytics · připravuje se</h2><p>Výkon za 24 hodin ani skutečná úspěšnost nejsou v dostupném API definované. Metriky a grafy zatím nejsou připojené.</p><button class="btn" data-view="overview">Zpět do místnosti</button></div></section>
  <section class="view-page" data-view-panel="settings" hidden><div class="panel placeholder-page"><h2 class="view-title" tabindex="-1">Nastavení · připravuje se</h2><p>Tato stránka je vizuální návrh. Nemění oprávnění, konfiguraci ani bezpečnostní pravidla Agent Lease.</p><button class="btn" data-view="overview">Zpět do místnosti</button></div></section>
</div>
<dialog id="workspaceDrawer" aria-labelledby="drawerTitle"><header class="drawer-header"><h2 id="drawerTitle">Pracovní prostor</h2><button id="drawerClose" class="btn" type="button" aria-label="Zavřít panel">✕</button><div id="drawerPause"></div></header><div id="drawerContent"></div></dialog>
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
let latestBoard = null; // null = autoritativní přehled není dostupný
let lastPaused; // undefined = stav dosud neznáme, null = provoz povolen
let connectionMode = "loading";

const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
let highestMessageId = 0;
let historyReady = false;
function motion(el, frames, duration) {
  if (!el || reducedMotion.matches || document.hidden || !el.getClientRects().length) return;
  el.animate(frames, {duration, easing:"cubic-bezier(0.22,1,0.36,1)"});
}
function setMetric(id, value) {
  const el = document.getElementById(id), next = String(value);
  if (el.textContent === next) return;
  const known = !["—", "Načítání…", "Připojování…"].includes(el.textContent);
  el.textContent = next;
  if (known) motion(el, [{opacity:.65,transform:"translateY(2px)"},{opacity:1,transform:"translateY(0)"}],240);
}
function setHTMLIfChanged(el, html) {
  if (el._renderHTML !== html) { el.innerHTML = html; el._renderHTML = html; }
}
function stateTone(state) {
  return state === "succeeded" ? "ok" : ["started","leased"].includes(state) ? "work"
    : ["failed","dead_letter"].includes(state) ? "bad" : state === "needs_review" ? "warn" : "muted";
}

// One native modal reuses the original panels and emergency control; no duplicate actions.
const drawer = document.getElementById("workspaceDrawer");
const panelPlacements = {};
for (const id of ["side","inspector","pause"]) {
  const node = document.getElementById(id), marker = document.createComment(id + " home");
  node.before(marker); panelPlacements[id] = {node, marker};
}
let drawerOpener = null;
function restorePanels() {
  for (const {node,marker} of Object.values(panelPlacements)) marker.after(node);
  document.body.classList.remove("drawer-open");
  document.querySelectorAll("[data-panel]").forEach(b => b.setAttribute("aria-expanded","false"));
  if (drawerOpener?.isConnected) drawerOpener.focus({preventScroll:true});
}
function openPanel(id, trigger = document.activeElement) {
  const narrow = id === "side" ? matchMedia("(max-width:899px)").matches : matchMedia("(max-width:1280px)").matches;
  if (!narrow) { document.getElementById(id).scrollIntoView({block:"start"}); return; }
  if (drawer.open) { drawer.close(); restorePanels(); }
  drawerOpener = trigger;
  document.getElementById("drawerContent").appendChild(panelPlacements[id].node);
  document.getElementById("drawerPause").appendChild(panelPlacements.pause.node);
  document.getElementById("drawerTitle").textContent = id === "side" ? "Agenti a pracovní prostor" : "Detail a záznamy úkolu";
  document.body.classList.add("drawer-open");
  drawer.showModal();
  document.querySelectorAll(`[data-panel="${id}"]`).forEach(b => b.setAttribute("aria-expanded","true"));
  motion(document.getElementById("drawerContent"),[{opacity:0},{opacity:1}],180);
}
drawer.addEventListener("close", () => { if (!drawer.open) restorePanels(); });
drawer.addEventListener("keydown", e => {
  if (e.key !== "Tab") return;
  const focusable = [...drawer.querySelectorAll('button,a[href],input,select,textarea,summary,[tabindex]')].filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length);
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (!first) { e.preventDefault(); return; }
  if (e.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (document.activeElement === last || !drawer.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
});
document.getElementById("drawerClose").onclick = () => drawer.close();
document.querySelectorAll("[data-panel]").forEach(b => b.onclick = () => openPanel(b.dataset.panel,b));
for (const query of ["(max-width:899px)","(max-width:1280px)"]) matchMedia(query).addEventListener("change",()=>{ if(drawer.open) drawer.close(); });
document.addEventListener("visibilitychange",()=>document.body.classList.toggle("page-hidden",document.hidden));
reducedMotion.addEventListener("change",()=>{ if(reducedMotion.matches) document.getAnimations().forEach(a=>a.cancel()); });

function renderAgentList(id, peers, board) {
  const el = document.getElementById(id);
  const old = new Map([...el.children].filter(c=>c.dataset.agent).map(c=>[c.dataset.agent,c]));
  el.querySelectorAll(":scope > .empty").forEach(n=>n.remove());
  peers.forEach((p,i)=>{
    const html = agentCard(p,board[p.agent]);
    let card = old.get(p.agent);
    if (!card || card._renderHTML !== html) {
      const template = document.createElement("template"); template.innerHTML = html;
      const next = template.content.firstElementChild;
      next._renderHTML = html; next.dataset.agent = p.agent;
      const work = next.querySelector(".agent-work"); if (work) work.title = work.textContent;
      if (card) card.replaceWith(next);
      card = next;
    }
    if (el.children[i] !== card) el.insertBefore(card,el.children[i] || null);
    old.delete(p.agent);
  });
  old.forEach(card=>card.remove());
  if (!peers.length && id === "agents") el.innerHTML = "<div class='empty'>nikdo není online</div>";
}

function historyMessages() { return [...messageCache.values()].sort((a, b) => a.id - b.id); }
function taskState(m) { const s = STATE[m.state] || [m.state || "stav není dostupný", ""]; return `<span class="tag task-state ${s[1]}">${esc(s[0])}</span>`; }
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
  setMetric("metricAgents", latestPeers.filter(p => p.active && roleOf(p.agent) === "agent").length);
  // Board pokrývá celou frontu; historie zpráv je omezena na 200 položek.
  // API poskytuje nejvýše jeden aktivní úkol na agenta (leased nebo started).
  setMetric("metricRunning", latestBoard === null ? "—" : latestBoard.filter(b => b.active).length);
  setMetric("metricQueued", latestBoard === null ? "—" : latestBoard.reduce((sum, b) => sum + b.queued, 0));
  setMetric("metricDone", tasks.filter(m => m.state === "succeeded").length);
  setMetric("metricReview", latestBoard === null ? "—" : latestBoard.reduce((sum, b) => sum + b.needs_review, 0));
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
  setHTMLIfChanged(document.getElementById("recentEvents"), events.length ? `<ol class="event-list">${events.map(m => `<li><div class="event-meta">#${m.id} · ${esc(nameOf(m.agent))} · ${ago(m.seconds_ago)}</div><p>${esc(m.text.slice(0, 170))}${m.text.length > 170 ? "…" : ""}</p></li>`).join("")}</ol><p class="note">Původní záznamy z načtené historie. Celý text je v místnosti.</p>` : "<div class='empty'>V načtené historii nejsou žádné události.</div>");
  applyFilters();
}
function renderSystemMetric() {
  const el = document.getElementById("metricSystem");
  setMetric("metricSystem", connectionMode !== "live" ? (connectionMode === "retry" ? "Spojení přerušeno" : "Připojování…")
    : lastPaused === undefined ? "Stav není dostupný" : lastPaused ? "Pozastaveno" : "Provoz povolen");
  el.dataset.tone = connectionMode !== "live" ? "warn" : lastPaused === undefined ? "muted" : lastPaused ? "bad" : "ok";
}
function selectTask(id, focus = false) {
  selectedTask = id === "" || id === null ? null : Number(id);
  document.querySelectorAll(".bubble.selected").forEach(el => el.classList.remove("selected"));
  if (selectedTask !== null) document.getElementById("msg-" + selectedTask)?.classList.add("selected");
  document.getElementById("taskPicker").value = selectedTask === null ? "" : String(selectedTask);
  renderTaskDetail();
  highlightAgent();
  if (focus) { openPanel("inspector"); document.getElementById("detailTitle").focus({preventScroll:true}); }
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
  const qualityRun = (qgRecords || []).find(r => r.task_id === m.id);
  const detailKey = JSON.stringify([m.id, m.text, m.agent, m.recipient, m.state, m.attempts, m.accept_note, m.last_error, qualityRun?.run_id, qualityRun?.overall_status, related.map(r => [r.id, r.kind, r.text])]);
  if (el.dataset.detailKey === detailKey) {
    [...el.querySelectorAll("[data-relative-id]"), ...timeline.querySelectorAll("[data-relative-id]")].forEach(span => {
      const record = messageCache.get(Number(span.dataset.relativeId));
      if (record) span.textContent = ago(record.seconds_ago);
    });
    return; // Otevřené zadání/výsledky a fokus přežijí pravidelnou SSE aktualizaci.
  }
  const openSections = el.dataset.taskId === String(m.id) ? [...el.querySelectorAll("details[open][data-section]"), ...timeline.querySelectorAll("details[open][data-section]")].map(d => d.dataset.section) : [];
  const switched = el.dataset.taskId !== String(m.id);
  el.dataset.taskId = String(m.id);
  el.dataset.detailKey = detailKey;
  const fields = [["ID", esc("#" + m.id)], ["Zadavatel", esc(nameOf(m.agent))], ["Přiřazeno", esc(nameOf(m.recipient))], ["Stav", taskState(m)], ["Pokusy", esc(m.attempts ?? "není dostupné")], ["Zadáno", `<span data-relative-id="${m.id}">${ago(m.seconds_ago)}</span>`]];
  el.innerHTML = `<dl class="detail-grid">${fields.map(([key,value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("")}</dl>`
    + `<details class="detail-section task-description" data-section="assignment"><summary>Zadání úkolu v plném znění</summary><div class="detail-text">${esc(m.text)}</div></details>`
    + (m.accept_note ? `<div class="detail-section"><h3>Potvrzení převzetí</h3><div class="detail-text">${esc(m.accept_note)}</div></div>` : "")
    + (m.last_error ? `<div class="detail-section"><h3>Poslední hlášená chyba</h3><div class="detail-text">${esc(m.last_error)}</div></div>` : "")
    + `<section class="task-quality"><h3><svg class="qg-shield" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m8 12 3 3 5-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg> Quality Gate</h3>${qualityRun ? qgBadge(qualityRun.overall_status) + `<p class="note">Commit: ${esc(qualityRun.commit_sha)}</p>` : '<span class="tag">NOT RUN</span><p class="note">Kontrola zatím není dostupná. Pro tento úkol není zaznamenaný běh.</p>'}</section>`;
  timeline.innerHTML = `<div class="timeline-current" data-tone="${stateTone(m.state)}"><span class="timeline-marker" aria-hidden="true"></span><span>Aktuální stav: ${esc((STATE[m.state] || [m.state || "nedostupný"])[0])}</span></div><ol class="event-list"><li><div class="event-meta">#${m.id} · zadání úkolu · <span data-relative-id="${m.id}">${ago(m.seconds_ago)}</span></div></li>`
    + related.map(r => `<li><details data-section="reply-${r.id}"><summary><span class="event-meta">#${r.id} · ${esc(KIND[r.kind] || r.kind)} · <span data-relative-id="${r.id}">${ago(r.seconds_ago)}</span></span><br>${esc(r.text.slice(0,80))}${r.text.length > 80 ? "…" : ""}</summary><div class="detail-text">${esc(r.text)}</div></details></li>`).join("")
    + `</ol><p class="note">${related.length ? "Přímo navázané zprávy z načtené historie." : "Žádné navázané zprávy v načtené historii."} Strukturované kroky ani úplná historie přechodů nejsou v API dostupné.</p>`;
  if (switched) { motion(el,[{opacity:.3},{opacity:1}],180); motion(timeline,[{opacity:.3},{opacity:1}],180); }
  [...el.querySelectorAll("details[data-section]"), ...timeline.querySelectorAll("details[data-section]")].forEach(d => { d.open = openSections.includes(d.dataset.section); });
}
function renderProjects(reviews) {
  const repos = [...new Set(reviews.map(r => r.repo).filter(Boolean))].sort();
  setHTMLIfChanged(document.getElementById("projects"), repos.length ? repos.map(repo => `<div class="card"><div class="project-name"><span aria-hidden="true">▣</span>${esc(repo.split("/").filter(Boolean).pop() || repo)}</div><div class="path">${esc(repo)}</div></div>`).join("") : `<div class="empty">V dostupných review nejsou uvedené repozitáře.</div>`);
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
  const known = {"claude-code":"--avatar-ada", "codex":"--avatar-nox", "claude-code-bridge":"--avatar-bridge", "codex-bridge":"--avatar-bridge"};
  const palette = ["--accent-1","--blue","--ok","--warn"];
  return `--avatar-color:var(${name === me ? "--avatar-user" : known[name] || palette[hueOf(name) % palette.length]})`;
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

function renderMessage(m, {live = false} = {}) {
  const previous = messageCache.get(m.id);
  const newLive = live && historyReady && !previous && m.id > highestMessageId;
  highestMessageId = Math.max(highestMessageId,m.id);
  messageCache.set(m.id,m);
  if (messageCache.size > 200) {
    const oldest = Math.min(...messageCache.keys());
    messageCache.delete(oldest); expandedMessages.delete(oldest);
    document.getElementById("msg-" + oldest)?.remove();
    if (oldest === m.id) return;
  }
  let div = document.getElementById("msg-" + m.id);
  const fresh = !div;
  if (fresh) { div = document.createElement("div"); div.className = "bubble"; div.id = "msg-" + m.id; div.dataset.messageId = String(m.id); }
  const k = m.kind || "chat";
  const structuralKey = JSON.stringify([m.text,m.kind,m.agent,m.recipient]);
  if (div.dataset.renderKey !== structuralKey) {
    div.dataset.renderKey = structuralKey;
    const to = m.recipient && m.recipient !== "*" ? `→ <span class="who">${esc(nameOf(m.recipient))}</span>` : "";
    const long = (m.text || "").length > 600 || (m.text || "").split(String.fromCharCode(10)).length > 8;
    div.innerHTML = `<div class="meta"><span class="kind kind-${esc(k)}">${esc(KIND[k] || k)}</span><span class="who">${esc(nameOf(m.agent))}</span> ${to}<span class="message-time" title="Stáří podle poslední aktualizace"></span><span class="delivery-meta"></span><span class="num">#${m.id}</span></div><div class="text${long && !expandedMessages.has(m.id) ? " clip" : ""}">${esc(m.text)}</div><span class="completion-flash" aria-hidden="true"></span>`;
    if (long) {
      const more = document.createElement("button"); more.className = "more";
      more.textContent = expandedMessages.has(m.id) ? "Sbalit" : "Zobrazit celé";
      more.onclick = () => {
        const clipped = div.querySelector(".text").classList.toggle("clip");
        if (clipped) expandedMessages.delete(m.id); else expandedMessages.add(m.id);
        more.textContent = clipped ? "Zobrazit celé" : "Sbalit";
      };
      div.appendChild(more);
    }
    if (k === "task") {
      const detail = document.createElement("button"); detail.className = "btn detail"; detail.textContent = "Detail úkolu ↗";
      detail.setAttribute("aria-label",`Detail úkolu #${m.id}`); detail.onclick = () => selectTask(m.id,true); div.appendChild(detail);
    }
    div._actionsKey = null;
  }
  div.classList.toggle("mine",m.agent === me);
  div.classList.toggle("system",isSystem(m));
  div.classList.toggle("task",k === "task");
  div.classList.toggle("result",k === "result");
  div.classList.toggle("selected",selectedTask === m.id);
  div.querySelector(".message-time").textContent = "· " + ago(m.seconds_ago);
  const state = k === "task" && m.state ? taskState(m) : "";
  const receipt = m.recipient && m.recipient !== "*" && k !== "result" && m.agent !== "broker"
    ? m.accepted ? tag("✓ převzal" + (m.accept_note ? ": " + m.accept_note : ""),"ok") : m.seen ? tag("viděl, zatím nepřevzal","work") : tag("ještě nedoručeno") : "";
  setHTMLIfChanged(div.querySelector(".delivery-meta"),state + receipt);
  const actionsKey = JSON.stringify([m.state,m.recipient,m.last_error]);
  if (div._actionsKey !== actionsKey) { div.querySelector(".actions")?.remove(); renderTaskActions(m,div); div._actionsKey = actionsKey; }
  if (fresh) {
    const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
    const next = [...feed.querySelectorAll(".bubble")].find(el=>Number(el.dataset.messageId)>m.id);
    feed.insertBefore(div,next || document.getElementById("filterEmpty"));
    if (stick) feed.scrollTop = feed.scrollHeight;
    if (newLive) motion(div,[{opacity:0,transform:"translateY(6px)"},{opacity:1,transform:"translateY(0)"}],210);
  } else if (previous?.state !== m.state) {
    if (m.state === "succeeded") motion(div.querySelector(".completion-flash"),[{opacity:.12},{opacity:0}],450);
    else if (stateTone(m.state) !== "bad") motion(div.querySelector(".task-state"),[{opacity:.65},{opacity:1}],180);
  } else if (previous?.accepted !== m.accepted) {
    motion(div.querySelector(".delivery-meta .tag:last-child"),[{opacity:.65},{opacity:1}],180);
  }
  if (!renderingRoom) refreshDashboard();
}

function renderTaskActions(m,div) {
  if (m.kind === "task" && m.recipient && m.state) {
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
    if (m.state === "needs_review" && (m.last_error || "").startsWith("fix:")) {
      add("Povolit zápis jednou", () => {
        const reason = prompt("Tato oprava smí zapisovat do repozitáře. Proč ji povolit? (zaznamená se)");
        if (reason) post("/api/approve-fix", {id: m.id, to: m.recipient, reason});
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
}

function renderPause(p) {
  if (p === undefined) return;  // zpráva bez stavu pauzy nesmí přepsat tlačítko
  lastPaused = p;
  renderSystemMetric();
  const el = document.getElementById("pause");
  const pauseKey = JSON.stringify(p);
  if (el._pauseKey === pauseKey) return;
  el._pauseKey = pauseKey;
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
  return `<div class="card ${p.active ? "" : "offline"} ${roleOf(p.agent) === "most" ? "bridge" : roleOf(p.agent) === "agent" ? "interactive" : ""}">
    <div class="head"><div class="avatar" style="${avatarStyle(p.agent)}">${esc(initial(p.agent))}<span class="dot ${p.active ? "on" : ""}"></span></div>
      <div class="who"><div class="name">${esc(nameOf(p.agent))}</div>${NAMES[p.agent] || p.agent === me ? `<div class="id">${esc(p.agent)}</div>` : ""}</div><span class="role">${roleOf(p.agent)}</span></div>
    ${line ? `<div class="line agent-work">${line}</div>` : ""}
    <div class="tags">${tags.join("")}</div>${p.seen_seconds_ago == null ? "" : `<div class="last-active">Aktivita ${ago(p.seen_seconds_ago)}</div>`}</div>`;
}

const REVIEW = {
  working: ["rozpracováno", ""], awaiting_review: ["čeká na review", "work"],
  changes_requested: ["požadované změny", "warn"], approved: ["schváleno", "ok"],
  escalated: ["ČEKÁ NA TEBE", "bad"], closed: ["uzavřeno", "gone"],
};
// B6: výsledek kontroly tajných údajů a bezpečnostní verdikt (bez hodnot tajemství)
function scanTag(v) {
  if (!v.head_commit || v.state === "closed") return "";
  const f = v.secret_findings;
  if (f === null || f === undefined) return tag("bez kontroly tajemství", "warn");
  if (f.length) return tag(`tajné údaje: ${f.length} (${[...new Set(f.map(x => x.file))].slice(0, 2).join(", ")})`, "bad");
  return v.security === "pass" ? tag("bezpečnost OK", "ok") : tag("bez tajemství", "ok");
}
function renderReviews(list) {
  if (list === undefined) return;
  renderProjects(list);
  document.getElementById("reviewCount").textContent = list.length;
  const el = document.getElementById("reviews");
  const reviewKey = JSON.stringify(list);
  if (el._reviewKey === reviewKey) return;
  el._reviewKey = reviewKey;
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
      + `<div class="tags">${tag(st[0], st[1])}${tag(`kolo oprav ${v.fix_cycles}/${v.max_fix_cycles}`, v.cycles_left ? "" : "bad")}${scanTag(v)}</div>`;
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
  if (Object.prototype.hasOwnProperty.call(r, "quality_runs")) {
    const records = Array.isArray(r.quality_runs) ? r.quality_runs : [];
    qgRecords = records.filter(record => record && record.kind === "quality_run" && record.executed === true);
  }
  if (Array.isArray(r.quality_requests)) qgRequests = r.quality_requests;
  if (Object.prototype.hasOwnProperty.call(r, "quality_runs") || Object.prototype.hasOwnProperty.call(r, "quality_requests")) renderQualityGate();
  latestBoard = Array.isArray(r.board) ? r.board : null;
  renderReviews(r.reviews);
  renderingRoom = true;
  (r.messages || []).forEach(m => renderMessage(m));
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
  renderAgentList("agents",online,board);
  renderAgentList("offline",offline,board);
  highlightAgent();
  document.getElementById("offlineSummary").textContent = `Offline (${offline.length})`;
  document.getElementById("offlineBox").style.display = offline.length ? "" : "none";
  setHTMLIfChanged(document.getElementById("claims"), (r.claims || []).map(c => `
    <div class="card"><div class="path">⌑ ${esc(c.path)}</div>
      <div class="line">${esc(nameOf(c.held_by))}${c.purpose ? " · " + esc(c.purpose) : ""} · ještě ${ttl(c.expires_in_seconds)}</div></div>`).join("")
    || "<div class='empty'>nic není zamčené</div>");
  // Komu: skuteční agenti, bez mostů, systému a sebe.
  const names = new Set(["claude-code", "codex"]);
  peers.forEach(p => { const a = String(p.agent); if (a !== me && a !== "broker" && !a.endsWith("-bridge")) names.add(a); });
  const list = [...names].sort((a, b) => (NAMES[a] ? 0 : 1) - (NAMES[b] ? 0 : 1) || a.localeCompare(b));
  if (list.join() !== knownAgents.join()) { knownAgents = list; fillRecipients(); }
  refreshDashboard();
  historyReady = true;
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
  es.addEventListener("message", e => renderMessage(JSON.parse(e.data), {live:true}));
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
  showView("overview",{focus:false});
  document.querySelector('[data-filter="task"]').click();
  document.getElementById("main").scrollIntoView({block:"start"});
};
document.getElementById("sendBtn").onclick = send;
textEl.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
fillRecipients();
// Read-only presentation of server-derived runs. Demo never enters the live data.
const QG_STATUS = {
  PASS:{label:"PASS",tone:"ok",icon:"✓",meaning:"Všechny povinné kontroly prošly."},
  REVIEW:{label:"REVIEW",tone:"warn",icon:"◇",meaning:"Vyžaduje kontrolu."},
  FAIL:{label:"FAIL",tone:"bad",icon:"✕",meaning:"Povinná kontrola selhala."},
  NA:{label:"N/A",tone:"muted",icon:"—",meaning:"Kontrola není použitelná nebo chybí metriky."},
  SETUP_REQUIRED:{label:"SETUP REQUIRED",tone:"work",icon:"⚙",meaning:"Projekt nemá nastavené povinné kontroly."},
  NOT_RUN:{label:"NOT RUN",tone:"muted",icon:"○",meaning:"Kontrola dosud neproběhla."},
};
const QG_CHECKS = [["tests","Testy"],["lint","Lint"],["typecheck","Type check"],["build","Build"],["coverage","Coverage"],["security","Security"],["crap","CRAP score"]];
const QG_SUMMARY = ["PASS","REVIEW","FAIL","NA","SETUP_REQUIRED"];
const demoChecks = (status,detail) => Object.fromEntries(QG_CHECKS.map(([key])=>[key,{status,detail}]));
const QG_DEMO = [
  {repository:"demo/agent-lease",branch:"demo/ui",commit_sha:"a".repeat(40),agent:"Nox (ukázka)",timestamp:"2026-11-06T12:00:00Z",overall_status:"PASS",checks:demoChecks("PASS","Ukázka: kontrola prošla. Žádný nástroj se nespustil."),logs:[],reviewer:"Ada (ukázka)",review_status:"Schváleno — ukázka",history:[{timestamp:"2026-11-05T12:00:00Z",status:"REVIEW",note:"Demonstrační starší záznam."}]},
  {repository:"demo/frontend-s-velmi-dlouhym-nazvem-repozitare-pro-overeni-responzivni-tabulky",branch:"demo/coverage",commit_sha:"b".repeat(40),agent:"Ada (ukázka)",timestamp:"2026-11-06T11:00:00Z",overall_status:"REVIEW",checks:{...demoChecks("PASS","Ukázkový výsledek."),coverage:{status:"REVIEW",value:"72 % (demo)",detail:"Ukázka: coverage vyžaduje kontrolu; nejde o skutečnou metriku."}},logs:[],reviewer:"Nox (ukázka)",review_status:"Čeká na nezávislé review — ukázka"},
  {repository:"demo/build-failure",branch:"demo/build",commit_sha:"c".repeat(40),agent:"Nox (ukázka)",timestamp:"2026-11-06T10:00:00Z",overall_status:"FAIL",checks:{...demoChecks("PASS","Ukázkový výsledek."),build:{status:"FAIL",detail:"Ukázka: build selhal."},security:{status:"FAIL",detail:"Ukázka bezpečnostního nálezu; neobsahuje skutečná tajemství."}},logs:[],reviewer:null,review_status:null},
  {repository:"demo/no-applicable-metrics",branch:null,commit_sha:null,agent:null,timestamp:null,overall_status:"NA",checks:demoChecks("NA","Ukázka: tato kontrola není použitelná."),logs:[]},
  {repository:"demo/setup-required",branch:"demo/setup",commit_sha:null,agent:null,timestamp:null,overall_status:"SETUP_REQUIRED",checks:demoChecks("NOT_RUN","Ukázka: nejprve je třeba nastavit kontroly."),logs:[]},
  {repository:"demo/not-run",branch:"demo/pending",commit_sha:null,agent:null,timestamp:null,overall_status:"NOT_RUN",checks:demoChecks("NOT_RUN","Ukázka: kontrola ještě neproběhla."),logs:[]},
];
let qgDemo = false;
let qgRecords = null;
let qgRequests = [];
let qgSelected = null;
let currentView = "overview";
function qgBadge(status) {
  const s = QG_STATUS[status] || QG_STATUS.NOT_RUN;
  return `<span class="tag qg-badge ${s.tone}"><span aria-hidden="true">${s.icon}</span> ${s.label}</span>`;
}
function qgSummary(records) {
  return QG_SUMMARY.map(status=>{const s=QG_STATUS[status];return `<div class="qg-stat" data-qg-state="${status}" data-tone="${s.tone}"><span aria-hidden="true">${s.icon}</span><span class="qg-stat-name">${s.label}</span><strong>${qgDemo || qgRecords !== null ? records.filter(r=>r.overall_status===status).length : "—"}</strong></div>`;}).join("");
}
function qgLogs(record) {
  const logs = Array.isArray(record.logs) ? record.logs : [];
  const safe = logs.filter(log => /^[/]api[/]quality-log[?]run_id=[1-9][0-9]*&check=(tests|lint|typecheck|build|coverage)$/.test(log.url || ""));
  return safe.length ? `<ul>${safe.map(log => `<li><a href="${esc(log.url)}" target="_blank" rel="noopener">${esc(log.check)} · procesní protokol</a></li>`).join("")}</ul>` : "";
}
function qgDate(value) { return value ? new Date(value).toLocaleString("cs-CZ") : "Není známá"; }
function qgTable(records,{compact=false}={}) {
  const headers = compact ? ["Repozitář",...QG_CHECKS.map(c=>c[1]),"Stav"] : ["Repozitář / větev","Commit","Agent","Kontrola",...QG_CHECKS.map(c=>c[1]),"Stav"];
  return `<div class="qg-scroll" tabindex="0" role="region" aria-label="Výsledky kontrol repozitářů; tabulku lze posouvat vodorovně"><table class="qg-table"><caption class="sr-only">${qgDemo ? "DEMO — ukázkové výsledky, ne skutečné kontroly" : records.length ? "Zaznamenané výsledky kontrol" : "Quality Gate dosud nemá výsledky"}</caption><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>`
    + (records.length ? records.map((r,i)=>`<tr><th scope="row"><button type="button" class="qg-repo" data-qg-project="${i}">${esc(r.repository)}</button>${compact ? "" : `<span class="qg-branch">${esc(r.branch || "Větev není uvedená")}</span>`}</th>${compact ? "" : `<td class="path">${esc(r.commit_sha ? r.commit_sha.slice(0,10) : "Neuveden")}</td><td>${esc(r.agent || "Neuveden")}</td><td>${esc(qgDate(r.timestamp))}</td>`}${QG_CHECKS.map(([key])=>`<td>${qgBadge(r.checks?.[key]?.status || "NOT_RUN")}</td>`).join("")}<td>${qgBadge(r.overall_status)}</td></tr>`).join("") : `<tr><td colspan="${headers.length}" class="qg-empty">Dosud bez výsledků. Pro tyto repozitáře není zaznamenaný běh kontrol.</td></tr>`)
    + `</tbody></table></div>`;
}
function renderQualityGate() {
  const records = qgDemo ? QG_DEMO : (qgRecords || []);
  document.querySelectorAll("[data-qg-mode]").forEach(el=>{el.textContent=qgDemo ? "DEMO · pouze ukázková data" : qgRecords === null ? "Výsledky nejsou dostupné" : "Zaznamenané běhy kontrol";});
  document.querySelectorAll("[data-qg-demo]").forEach(b=>{b.textContent=qgDemo ? "Vypnout demo" : "Zobrazit demo";b.setAttribute("aria-pressed",String(qgDemo));});
  document.getElementById("qgCompactSummary").innerHTML=qgSummary(records);
  document.getElementById("qgPageSummary").innerHTML=qgSummary(records);
  document.querySelectorAll("[data-qg-state]").forEach(el=>{el.title=QG_STATUS[el.dataset.qgState].meaning;});
  document.getElementById("qgRepoCount").textContent=qgDemo || qgRecords !== null ? records.length : "—";
  document.getElementById("qgLastCheck").textContent=records.length ? (qgDemo ? "Ukázka: " : "") + qgDate(records[0].timestamp) : "Není známá";
  document.getElementById("qgCompactTable").innerHTML=qgTable(records,{compact:true});
  document.getElementById("qgProjectTable").innerHTML=qgTable(records);
  document.querySelectorAll("[data-qg-project]").forEach(b=>b.onclick=()=>{const record=records[Number(b.dataset.qgProject)];qgSelected=qgDemo ? record.repository : record.run_id;showView("quality");renderQualityProject();document.getElementById("qgProjectTitle").focus({preventScroll:true});});
  const pending = qgDemo ? [] : qgRequests.filter(request=>request.state !== "completed").slice(0,10);
  const requestLabels = {queued:"Čeká ve frontě",running:"Kontroly běží",refused:"Je potřeba nastavení",interrupted:"Běh byl přerušen"};
  document.getElementById("qgRequests").innerHTML = pending.length ? `<div class="card"><h3>Požadavky na kontrolu</h3><ul>${pending.map(request=>`<li>${esc(request.repository)} · ${esc(requestLabels[request.state] || "Stav není známý")} ${request.detail ? "· " + esc(request.detail) : ""}</li>`).join("")}</ul></div>` : "";
  renderQualityProject();
}
function renderQualityProject() {
  const r=qgSelected === null ? null : qgDemo ? QG_DEMO.find(record=>record.repository===qgSelected) : (qgRecords || []).find(record=>record.run_id===qgSelected);
  const el=document.getElementById("qgProjectDetail");
  document.getElementById("qgProjectTitle").textContent=r ? r.repository : "Detail repozitáře";
  if(!r){el.innerHTML=`<div class="empty">${qgDemo ? "Vyber ukázkový repozitář z tabulky." : "Vyber zaznamenaný běh z tabulky repozitářů."}</div>`;return;}
  const checks=QG_CHECKS;
  el.innerHTML=`${qgDemo ? '<p class="qg-demo-note">DEMO — tyto výsledky nepatří vašim projektům.</p>' : ''}<dl class="detail-grid"><dt>Větev</dt><dd>${esc(r.branch || "Neuvedena")}</dd><dt>Commit</dt><dd class="path">${esc(r.commit_sha || "Neuveden")}</dd><dt>Agent</dt><dd>${esc(r.agent || "Neuveden")}</dd><dt>Kontrola</dt><dd>${esc(qgDate(r.timestamp))}</dd><dt>Stav</dt><dd>${qgBadge(r.overall_status)}</dd></dl><div class="qg-check-grid">`
    + checks.map(([key,label])=>{const c=r.checks?.[key];return `<section class="card"><h3>${label}</h3>${qgBadge(c?.status || "NOT_RUN")}<p>${esc(c?.value || c?.detail || "Výsledek není dostupný.")}</p></section>`;}).join("")
    + `</div><div class="detail-section"><h3>Změny testů a pravidel</h3><p>${r.policy_review_required ? r.policy_review_status === "approved" ? "Kritické změny schválil člověk pro tento běh." : "Kritické změny čekají na lidské schválení." : "Další schválení kritických změn není požadováno."}</p>${!qgDemo && r.policy_review_required && r.policy_review_status !== "approved" && r.review_status === "approved" && Object.values(r.checks || {}).every(c=>!c.required || c.status === "PASS") ? '<button class="btn" id="qgApprovePolicy">Schválit kritické změny</button>' : ""}</div><div class="detail-section"><h3>Nezávislé review</h3><p>${esc(r.review_status === 'approved' ? 'Schváleno pro tento commit.' : r.review_status === 'pending' ? 'Čeká na nezávislé review.' : r.review_status || "Stav review není dostupný.")}${r.reviewer ? `<br>Recenzent: ${esc(r.reviewer)}` : ""}</p></div><div class="detail-section"><h3>Protokoly</h3><p>${qgDemo ? 'Protokoly nejsou připojené. Ukázka neobsahuje odkazy na skutečné soubory.' : 'Úplné protokoly nejsou připojené; dostupná jsou pouze procesní metadata a otisky výstupu.'}</p>${qgDemo ? '' : qgLogs(r)}</div><div class="detail-section"><h3>Historie kontrol</h3>${r.history?.length ? `<ul>${r.history.map(h=>`<li>${esc(qgDate(h.timestamp))} · ${qgBadge(h.status)} · ${esc(h.note)}</li>`).join("")}</ul>` : "<p>Historie kontrol není dostupná.</p>"}</div>`;
  const approve = document.getElementById("qgApprovePolicy");
  if (approve) approve.onclick=()=>{
    const reason=prompt("Proč schvaluješ změny testů nebo pravidel pro tento commit?");
    if (!reason || reason.trim().length < 10 || !confirm("Schválit kritické změny pro commit " + r.commit_sha + "?")) return;
    approve.disabled=true;
    post("/api/quality-policy-approve",{run_id:r.run_id,commit_sha:r.commit_sha,reason}).then(ok=>{if(!ok)approve.disabled=false;});
  };
  motion(el,[{opacity:.4},{opacity:1}],180);
}
function showView(view,{focus=true}={}) {
  if(drawer.open){drawer.close();restorePanels();}
  currentView=view;
  document.querySelectorAll("[data-view-panel]").forEach(el=>{el.hidden=el.dataset.viewPanel!==view;});
  document.querySelectorAll(".primary-nav [data-view]").forEach(b=>{const on=b.dataset.view===view;b.classList.toggle("current",on);if(on)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current");});
  if(focus)document.querySelector(`[data-view-panel="${view}"] .view-title`)?.focus({preventScroll:true});
  motion(document.querySelector(`[data-view-panel="${view}"]`),[{opacity:.5},{opacity:1}],180);
}
document.querySelectorAll("[data-view]").forEach(b=>b.addEventListener("click",()=>showView(b.dataset.view)));
document.querySelectorAll("[data-qg-demo]").forEach(b=>b.onclick=()=>{qgDemo=!qgDemo;qgSelected=null;renderQualityGate();});
renderQualityGate();

document.getElementById("navAgents").onclick=()=>{showView("overview",{focus:false});openPanel("side");};
for(const [id,target,panel] of [["navProjects","projectPanel","side"],["navHistory","historyPanel","inspector"]])document.getElementById(id).onclick=e=>{e.preventDefault();showView("overview",{focus:false});openPanel(panel);document.getElementById(target).scrollIntoView({block:"start"});};
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
