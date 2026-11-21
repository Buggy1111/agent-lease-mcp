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

# Statické soubory UI (portréty Ady a Noxe, ikona). Pevný seznam: žádné cesty z požadavku.
_ASSET_DIR = Path(__file__).with_name("assets")
ASSETS = {"/assets/ada.webp": ("ada.webp", "image/webp"), "/assets/nox.webp": ("nox.webp", "image/webp"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml"), "/favicon.ico": ("favicon.svg", "image/svg+xml")}

POLL_SECONDS = 0.7
ROOM_SECONDS = 2.0
SESSION_COOKIE = "agent_lease_session"


def session_cookie_name(port: int) -> str:
    """Cookies prohlížeč neodděluje podle portu: dva weby na jednom stroji (8765 a náhled) by si přihlášení
    přepisovaly (21. 11.). Výchozí port si jméno ponechá, ať stávající přihlášení dál platí."""
    return SESSION_COOKIE if port == 8765 else f"{SESSION_COOKIE}_{port}"
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
    cookie_name = session_cookie_name(port)
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

        def _handle_asset(self, name: str, content_type: str) -> None:
            try:
                body = (_ASSET_DIR / name).read_bytes()
            except OSError:
                return self._reject(404, "not found")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ── autentizace ─────────────────────────────────────────────────
        def _bearer_ok(self) -> bool:
            header = self.headers.get("X-Agent-Lease-Token", "")
            return bool(header) and secrets.compare_digest(header, token)

        def _session_ok(self) -> bool:
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                value = cookie[cookie_name].value
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
            if parts.path in ASSETS:
                return self._handle_asset(*ASSETS[parts.path])
            if parts.path == "/api/events":
                return self._handle_events(qs)
            if parts.path == "/api/snapshot":
                return self._handle_snapshot(qs)
            if parts.path == "/api/knowledge":
                return self._handle_knowledge_list(qs)
            if parts.path == "/api/research":
                if not self._session_ok():
                    return self._reject(403, "bad token")
                runs = [store.research_get(r["id"]) for r in store.research_list(20)]
                return self._json_response({"research": [r for r in runs if r]})
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
            if parts.path == "/api/mode":
                data = self._read_json()
                if data is None:
                    return
                try:
                    result = store.set_mode(web_agent, str(data.get("mode", "")), str(data.get("reason", ""))[:500])
                except PermissionError as exc:
                    return self._reject(409, str(exc))
                except ValueError as exc:
                    return self._reject(400, str(exc))
                return self._json_response(result)
            if parts.path in ("/api/knowledge-add", "/api/knowledge-verdict"):
                return self._handle_knowledge_write(parts.path.endswith("add"))
            if parts.path == "/api/research-cancel":
                data = self._read_json()
                if data is None:
                    return
                try:  # člověk (actor=None) smí zrušit kterýkoli výzkum
                    return self._json_response({"cancelled": store.research_cancel(int(data["id"]))})
                except (KeyError, TypeError, ValueError):
                    return self._reject(400, "id required")
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
                f"{cookie_name}={session_id}; HttpOnly; SameSite=Strict; Path=/; "
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
                "mode": store.mode(),
                "reviews": store.reviews(limit=30),
                "reviews_all": store.reviews(limit=200, include_closed=True),   # Přehled: postup i u uzavřených
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

        def _handle_knowledge_list(self, qs: dict[str, list[str]]) -> None:
            """D1: hledání (`q`) nebo nejnovější záznamy sdílené paměti."""
            if not self._session_ok():
                return self._reject(403, "bad token")
            q, project, status = (qs.get(k, [""])[0] for k in ("q", "project", "status"))
            try:
                if q:
                    entries = store.knowledge_human_search(q, project=project, include_retired=True, limit=50,
                                                     status=status)
                else:
                    entries = store.knowledge_list(project=project, status=status, limit=100)
            except ValueError as exc:
                return self._reject(400, str(exc))
            self._json_response({"entries": entries})

        def _handle_knowledge_write(self, add: bool) -> None:
            """D1: člověk zapíše záznam, nebo ho ověří / zamítne / označí za zastaralý."""
            data = self._read_json()
            if data is None:
                return
            text = {k: str(data.get(k, "")) for k in
                    ("symptom", "cause", "solution", "project", "evidence", "verdict", "note")}
            try:
                if add:
                    result = store.knowledge_human_add(
                        web_agent, text["symptom"], text["cause"], text["solution"],
                        text["project"], text["evidence"])
                else:
                    result = store.knowledge_human_verdict(
                        web_agent, int(data["id"]), text["verdict"], text["evidence"], text["note"])
            except KeyError:
                return self._reject(404, "no such entry")
            except PermissionError as exc:
                return self._reject(409, str(exc))
            except (TypeError, ValueError) as exc:
                return self._reject(400, str(exc))
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
                "reviews_all": store.reviews(limit=200, include_closed=True),   # Přehled: postup i u uzavřených
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
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root {
  color-scheme:dark;
  --bg:#070D20; --surface:#101B38; --surface-2:#16244A;
  --accent-1:#923BFF; --blue:#04CFFF; --ok:#00E779; --warn:#FF8A18; --bad:#FF5A7A;
  --ada:#FF8A18; --nox:#00E779; --cyan:#04CFFF; --purple:#923BFF;
  --text:#F5F7FF; --text-dim:#BBC6E1; --ink:#000;
  --border:rgba(120,150,255,.22);
  --border-soft:rgba(120,150,255,.12);
  --text-faint:var(--text-dim); --raised:var(--surface-2);
  --accent-text:#C9A2FF;
  --bad-text:#FF8FA6;
  --blue-text:#7FE3FF;
  --grad:linear-gradient(135deg,#7E30E8,#6A24D8);
  --panel-grad:linear-gradient(160deg,rgba(22,36,74,.92),rgba(13,22,48,.92));
  --shadow:0 0 0 1px rgba(120,150,255,.06),0 10px 30px rgba(0,0,0,.35);
  --shadow-hover:0 0 0 1px rgba(146,59,255,.35),0 0 22px rgba(146,59,255,.18);
  --ease:cubic-bezier(.22,1,.36,1); --panel-time:200ms; --control-time:160ms;
  --avatar-ada:var(--ada); --avatar-nox:var(--nox); --avatar-bridge:#5B6B95; --avatar-user:var(--purple);
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
.mode-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-bottom:12px; }
.mode-card { text-align:left; border:1px solid var(--border); border-radius:12px; padding:14px; background:var(--surface-2); display:flex; flex-direction:column; gap:8px; }
.mode-card[aria-checked="true"] { border-color:var(--accent-text); box-shadow:0 0 0 1px var(--accent-text); }
.mode-card[aria-disabled="true"] { opacity:.6; cursor:not-allowed; }
.mode-card h3 { margin:0; font-size:16px; }
.mode-card ul { margin:0; padding-left:18px; font-size:13px; color:var(--text-dim); }
.kb-grid { display:grid; grid-template-columns:minmax(0,2fr) minmax(0,1fr); gap:16px; }
@media (max-width:900px) { .kb-grid { grid-template-columns:1fr; } }
.kb-tools { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
.kb-tools .search { flex:1; min-width:180px; }
.kb-form { display:flex; flex-direction:column; gap:10px; }
.kb-form label { display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--text-dim); }
.kb-entry { border:1px solid var(--border); border-radius:10px; padding:10px 12px; margin-bottom:10px; overflow-wrap:anywhere; }
.kb-entry dl { display:grid; grid-template-columns:max-content 1fr; gap:4px 10px; margin:8px 0; font-size:13px; }
.kb-entry dt { color:var(--text-dim); }
.kb-entry dd { margin:0; white-space:pre-wrap; }
.kb-state { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--border); }
.kb-state.verified { color:var(--ok); border-color:currentColor; }
.kb-state.rejected, .kb-state.stale { color:var(--text-dim); text-decoration:line-through; }
.kb-actions { display:flex; gap:6px; flex-wrap:wrap; }
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
/* ── Neonové UI podle Michalových předloh (21. 11. 2026, docs/ui/NEON_UI_PROPOSAL_2026-11-21.md) ── */
body { font:14px/1.5 "Inter","Segoe UI Variable Text","Segoe UI",ui-sans-serif,system-ui,sans-serif; background:radial-gradient(1200px 600px at 50% -10%,rgba(146,59,255,.16),transparent 60%),var(--bg); background-attachment:fixed; }
#shell { max-width:1680px; padding:0 20px 40px; }
/* hlavička: logo · Ada · ∞ · Nox · nástroje; pod ní navigace */
#masthead { position:relative; display:grid; grid-template-columns:auto 1fr auto; grid-template-areas:"brand heroes tools" "nav nav pause"; align-items:center; gap:8px 20px; min-height:0; padding:14px 0 10px; background:transparent; border-bottom:0; backdrop-filter:none; }
#brand { grid-area:brand; } #agentHeroes { grid-area:heroes; } #headTools { grid-area:tools; } .primary-nav { grid-area:nav; } #pause { grid-area:pause; justify-self:end; }
#brand .mark,#brand .mark svg { width:58px; height:30px; filter:drop-shadow(0 0 8px rgba(4,207,255,.35)); }
#brand .name { font-size:28px; font-weight:700; letter-spacing:-.5px; line-height:1.1; }
#brand .sub { display:block; font-size:13px; color:var(--text-dim); }
#agentHeroes { display:flex; align-items:center; justify-content:center; gap:18px; min-width:0; }
.hub { width:72px; height:72px; flex:none; display:grid; place-items:center; border-radius:50%; border:1px solid rgba(146,59,255,.45); background:radial-gradient(circle,rgba(146,59,255,.18),rgba(7,13,32,.9) 70%); box-shadow:0 0 24px rgba(146,59,255,.25),inset 0 0 18px rgba(4,207,255,.12); }
.hub svg { width:44px; height:22px; }
.agent-hero { --c:var(--ada); position:relative; display:flex; align-items:center; gap:12px; min-width:0; width:min(360px,100%); padding:8px 16px 8px 8px; border-radius:16px; border:1px solid color-mix(in srgb,var(--c) 55%,transparent); background:linear-gradient(120deg,color-mix(in srgb,var(--c) 16%,var(--surface)),rgba(10,16,38,.95) 70%); box-shadow:0 0 0 1px rgba(0,0,0,.2),0 0 18px color-mix(in srgb,var(--c) 14%,transparent); }
.agent-hero[data-agent="nox"] { --c:var(--nox); flex-direction:row-reverse; padding:8px 8px 8px 16px; }
.agent-hero::after { content:""; position:absolute; inset:-1px; border-radius:inherit; border:1px solid var(--c); opacity:0; pointer-events:none; box-shadow:0 0 22px color-mix(in srgb,var(--c) 55%,transparent); }
.portrait-wrap { position:relative; flex:none; width:84px; height:78px; }
.portrait { position:relative; z-index:1; width:84px; height:78px; object-fit:cover; object-position:top; border-radius:12px; }
.halo { position:absolute; inset:-6px; border-radius:16px; background:radial-gradient(circle at 50% 40%,color-mix(in srgb,var(--c) 55%,transparent),transparent 70%); opacity:.25; }
.agent-info { min-width:0; flex:1; }
.agent-title { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; }
.agent-name { font-size:21px; font-weight:700; letter-spacing:-.3px; }
.agent-vendor,.agent-role { font-size:12px; color:var(--text-dim); }
.agent-state { display:flex; align-items:center; gap:7px; margin-top:4px; font-size:13px; font-weight:600; color:var(--c); }
.state-dot { width:9px; height:9px; border-radius:50%; background:var(--c); box-shadow:0 0 8px var(--c); }
.pulse-line { width:70px; height:18px; margin-left:auto; }
.pulse-line path { fill:none; stroke:var(--c); stroke-width:1.6; stroke-linejoin:round; opacity:.55; stroke-dasharray:120; stroke-dashoffset:0; }
.agent-task { font-size:11px; color:var(--text-dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:240px; min-height:16px; }
/* stavy: pulzuje jen „working“, každý agent zvlášť */
.agent-hero[data-state="working"]::after { animation:hero-breathe 2.2s ease-in-out infinite; }
.agent-hero[data-state="working"] .halo { animation:halo-breathe 2.2s ease-in-out infinite; }
.agent-hero[data-state="working"] .pulse-line path { opacity:1; stroke-dasharray:6 4; animation:pulse-run 1.6s linear infinite; }
.agent-hero[data-state="idle"] .halo { opacity:.22; }
.agent-hero[data-state="limited"],.agent-hero[data-state="offline"],.agent-hero[data-state="unknown"] { filter:saturate(.35); }
.agent-hero[data-state="offline"] .portrait,.agent-hero[data-state="unknown"] .portrait { opacity:.55; }
.agent-hero[data-state="offline"] .state-dot,.agent-hero[data-state="unknown"] .state-dot { background:#7D88A8; box-shadow:none; }
.agent-hero[data-state="offline"] .agent-state,.agent-hero[data-state="unknown"] .agent-state { color:var(--text-dim); }
.agent-hero[data-state="error"] .agent-state { color:var(--bad-text); }
.agent-hero[data-state="error"] .state-dot { background:var(--bad); box-shadow:none; border-radius:2px; transform:rotate(45deg); }
@keyframes hero-breathe { 0%,100% { opacity:.15; } 50% { opacity:.9; } }
@keyframes halo-breathe { 0%,100% { opacity:.25; transform:scale(.97); } 50% { opacity:.75; transform:scale(1.03); } }
@keyframes pulse-run { to { stroke-dashoffset:-20; } }
#headTools { display:flex; align-items:center; gap:10px; }
.mode-badge { padding:7px 14px; border-radius:999px; border:1px solid rgba(146,59,255,.55); background:rgba(146,59,255,.12); color:var(--text); font-size:13px; white-space:nowrap; }
.human-badge { width:34px; height:34px; border-radius:50%; display:grid; place-items:center; background:var(--grad); font-weight:700; }
/* navigace */
.primary-nav { display:flex; gap:6px; flex-wrap:wrap; padding:6px 0 4px; border-bottom:1px solid transparent; border-image:linear-gradient(90deg,transparent,rgba(146,59,255,.6),rgba(4,207,255,.4),transparent) 1; }
.primary-nav button,.primary-nav a { display:flex; align-items:center; gap:8px; min-height:42px; padding:8px 18px; border-radius:12px; font-size:15px; color:var(--text); background:transparent; border:1px solid transparent; }
.primary-nav button:hover,.primary-nav [data-view]:hover { background:rgba(146,59,255,.12); border-color:rgba(146,59,255,.35); box-shadow:none; }
.primary-nav .current,.primary-nav .current:hover,.primary-nav [data-view].current:hover { background:var(--grad); border-color:transparent; color:#fff; box-shadow:0 0 18px rgba(146,59,255,.45); }
.nav-ic { width:19px; height:19px; flex:none; }
.big { font-size:13px; } .pausebar button { border-radius:10px; }
/* metriky */
#overview { padding:16px 0 14px; } .section-heading { display:none; }
#metrics { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:14px; border:0; background:none; overflow:visible; }
.metric { --t:var(--cyan); display:flex; align-items:center; gap:14px; padding:16px; border-radius:14px; border:1px solid color-mix(in srgb,var(--t) 55%,transparent)!important; background:linear-gradient(135deg,color-mix(in srgb,var(--t) 14%,var(--surface)),rgba(10,16,38,.95)); box-shadow:0 0 16px color-mix(in srgb,var(--t) 12%,transparent); }
.metric[data-tone-card="purple"] { --t:var(--purple); } .metric[data-tone-card="orange"] { --t:var(--ada); } .metric[data-tone-card="green"] { --t:var(--nox); }
.metric-icon { display:grid!important; width:42px; height:42px; place-items:center; color:var(--t); border:0; background:none; box-shadow:none; }
.metric-icon svg { width:34px; height:34px; filter:drop-shadow(0 0 6px color-mix(in srgb,var(--t) 60%,transparent)); }
.metric-label { font-size:14px; color:var(--text); }
.metric-value { font-size:28px; font-weight:700; color:var(--text); }
.metric-system { display:none!important; }
/* plocha: projekty+agenti | místnost | aktivita */
#app { grid-template-columns:270px minmax(0,1fr) 300px; gap:16px; }
#inspector { display:none; } #openDetail { display:inline-flex!important; align-items:center; }
.panel { border:1px solid var(--border); border-radius:16px; background:var(--panel-grad); box-shadow:var(--shadow); }
.panel-title { padding:14px 16px; border-bottom:1px solid var(--border-soft); gap:10px; }
.panel-title h2 { font-size:16px; font-weight:650; }
.panel-icon { display:inline; color:var(--cyan); }
.panel-body { padding:12px 16px; }
.count { background:rgba(146,59,255,.15); border:1px solid rgba(146,59,255,.35); color:var(--text); border-radius:999px; padding:0 8px; }
.card { background:rgba(16,27,56,.7); border:1px solid var(--border-soft); border-radius:12px; }
.card.selected-agent { border-color:var(--purple); box-shadow:0 0 0 1px var(--purple),0 0 16px rgba(146,59,255,.3); }
.card.bridge { border-style:dashed; }
.avatar { border-radius:50%; border:1px solid color-mix(in srgb,var(--avatar-color,var(--purple)) 70%,transparent); background:color-mix(in srgb,var(--avatar-color,var(--purple)) 22%,var(--surface)); color:var(--text); }
.tag { border-radius:999px; padding:2px 9px; background:rgba(120,150,255,.1); color:var(--text-dim); }
.tag.ok { color:#7CF5B6; background:rgba(0,231,121,.1); } .tag.warn { color:#FFB36B; background:rgba(255,138,24,.12); }
.tag.bad { color:var(--bad-text); background:rgba(255,90,122,.12); } .tag.work { color:var(--accent-text); background:rgba(146,59,255,.16); }
/* místnost */
#main { border-color:rgba(146,59,255,.45); box-shadow:0 0 0 1px rgba(146,59,255,.12),0 0 26px rgba(146,59,255,.14); height:calc(100dvh - 330px); min-height:560px; }
#topbar { padding:14px 16px; }
#topbar .title { font-size:16px; }
.filters,.seg { background:rgba(7,13,32,.6); border:1px solid var(--border-soft); border-radius:10px; padding:3px; }
.filter,.seg button { color:var(--text-dim); border-radius:8px; }
.filter.on,.seg button.on { background:var(--grad); color:#fff; border-color:transparent; box-shadow:0 0 12px rgba(146,59,255,.35); }
#search,#to,#taskPicker,#text,#kbQuery,select,.kb-form input,.kb-form textarea { background:rgba(7,13,32,.7); border:1px solid var(--border); border-radius:10px; color:var(--text); }
#text:focus,#search:focus,#to:focus { border-color:var(--purple); box-shadow:0 0 0 3px rgba(146,59,255,.25); }
#feed { padding:16px; gap:12px; }
.bubble { --c:var(--cyan); width:auto; max-width:min(760px,92%); background:rgba(16,27,56,.85); border:1px solid var(--border-soft); border-left:3px solid var(--c); border-radius:14px; }
.bubble[data-agent="ada"] { --c:var(--ada); } .bubble[data-agent="nox"] { --c:var(--nox); } .bubble[data-agent="me"] { --c:var(--purple); }
.bubble.mine { align-self:flex-end; background:rgba(146,59,255,.14); }
.bubble.system { background:transparent; border-color:transparent; border-left-color:var(--border); }
.bubble.selected { box-shadow:0 0 0 1px var(--purple),0 0 14px rgba(146,59,255,.3); }
.bubble .meta .who { color:var(--text); }
.bubble[data-agent="ada"] .meta > .who:first-of-type { color:#FFB36B; } .bubble[data-agent="nox"] .meta > .who:first-of-type { color:#7CF5B6; }
.bubble .text { font-size:14px; }
.kind { text-transform:none; letter-spacing:0; border-radius:999px; }
#compose { background:rgba(7,13,32,.35); border-top:1px solid var(--border-soft); }
#sendBtn,.btn.detail { background:var(--grad); color:#fff; border:0; border-radius:10px; box-shadow:0 0 14px rgba(146,59,255,.35); }
#sendBtn { align-self:flex-end; min-height:42px; padding:8px 22px; font-weight:650; }
#sendBtn:hover,.btn.detail:hover { background:linear-gradient(135deg,#6E26D6,#5A1CC0); color:#fff; }
.btn { border:1px solid var(--border); background:rgba(16,27,56,.8); color:var(--text); border-radius:10px; }
.btn:hover:not(.detail) { border-color:var(--purple); background:rgba(146,59,255,.14); }
.more { color:var(--accent-text); }
/* aktivita agentů */
#activity { min-width:0; align-self:stretch; }
.activity { list-style:none; margin:0; padding:12px 16px 16px; display:flex; flex-direction:column; gap:14px; }
.act { --c:var(--ada); position:relative; display:flex; gap:12px; padding-left:16px; }
.act[data-agent="nox"] { --c:var(--nox); }
.act::before { content:""; position:absolute; left:0; top:8px; width:9px; height:9px; border-radius:50%; background:var(--c); box-shadow:0 0 8px var(--c); }
.act:not(:last-child)::after { content:""; position:absolute; left:4px; top:20px; bottom:-14px; width:1px; background:var(--border); }
.act-portrait { width:40px; height:40px; border-radius:50%; object-fit:cover; object-position:top; border:1px solid var(--c); flex:none; }
.act-body { min-width:0; } .act-head { display:flex; justify-content:space-between; gap:8px; } .act-head b { color:var(--text); }
.act-head time { color:var(--text-dim); font-size:12px; } .act p { margin:2px 0 0; color:var(--text-dim); font-size:13px; overflow-wrap:anywhere; }
/* Místnost agentů přes celou plochu */
body.room-focus #overview,body.room-focus #side,body.room-focus #activity,body.room-focus .mobile-nav { display:none!important; }
body.room-focus #app { grid-template-columns:minmax(0,1fr); padding-top:16px; }
body.room-focus #main { height:calc(100dvh - 210px); }
/* vysouvací panel, stránky */
#workspaceDrawer { background:var(--bg); border:1px solid var(--border); box-shadow:0 0 40px rgba(146,59,255,.25); }
#workspaceDrawer #inspector { display:flex; }
.view-title { font-size:30px; font-weight:700; letter-spacing:-.4px; }
.view-subtitle { color:var(--text-dim); }
.mode-card,.kb-entry { background:var(--panel-grad); border:1px solid var(--border); border-radius:14px; }
.mode-card[aria-checked="true"] { border-color:var(--purple); box-shadow:0 0 0 1px var(--purple),0 0 18px rgba(146,59,255,.3); }
.qg-stat { border-radius:999px; flex-direction:row; align-items:center; gap:6px; padding:4px 10px; }
#inspector .qg-summary,.qg-summary { display:flex; flex-wrap:wrap; gap:6px; }
.concept-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }
.concept-badge { margin-left:auto; padding:2px 10px; border-radius:999px; border:1px dashed rgba(255,138,24,.7); color:#FFB36B; font-size:12px; }
.concept-list { margin:0; padding-left:18px; color:var(--text-dim); line-height:1.8; }
.concept-list.mono { font:12px/1.8 ui-monospace,monospace; }
@media(hover:hover) and (pointer:fine) {
  .panel:hover { border-color:var(--border); box-shadow:var(--shadow); }
  .card:hover { border-color:rgba(146,59,255,.4); box-shadow:none; }
  .metric:hover { transform:none; }
}
@media(max-width:1500px) { #metrics { grid-template-columns:repeat(3,minmax(0,1fr)); } #app { grid-template-columns:240px minmax(0,1fr) 260px; } }
@media(max-width:1280px) {
  #masthead { grid-template-columns:minmax(0,1fr) auto; grid-template-areas:"brand tools" "heroes heroes" "nav nav" "pause pause"; }
  #masthead > * { min-width:0; }
  #pause { justify-self:start; }
  #app { grid-template-columns:220px minmax(0,1fr); } #activity { grid-column:1 / -1; }
}
@media(max-width:899px) {
  #brand .name { font-size:22px; } #brand .sub { font-size:12px; } #brand .mark,#brand .mark svg { width:44px; height:24px; }
  .metric { padding:12px; gap:10px; } .metric-icon { width:30px; height:30px; } .metric-icon svg { width:26px; height:26px; }
  .metric-label { font-size:13px; overflow-wrap:normal; word-break:normal; hyphens:auto; } .metric-value { font-size:24px; }
  .pausebar { flex-wrap:wrap; } .agent-task { max-width:100%; }
  #agentHeroes { flex-direction:column; } .hub { display:none; } .agent-hero { width:100%; }
  #metrics { grid-template-columns:repeat(2,minmax(0,1fr)); }
  #app { display:block; } #activity { margin-top:16px; }
  .primary-nav { flex-wrap:nowrap; overflow-x:auto; }
  .primary-nav button { padding:8px 12px; font-size:14px; }
}
@media(prefers-reduced-motion:reduce) {
  .agent-hero[data-state="working"]::after { animation:none; opacity:.8; }
  .agent-hero[data-state="working"] .halo { animation:none; opacity:.6; }
  .agent-hero[data-state="working"] .pulse-line path { animation:none; }
}
body.page-hidden .agent-hero::after,body.page-hidden .halo,body.page-hidden .pulse-line path { animation-play-state:paused!important; }
/* ── Vizuální prvky z předlohy 02 (Michal 21. 11.) ── */
.panel-title .nav-ic { width:22px; height:22px; color:var(--cyan); filter:drop-shadow(0 0 6px rgba(4,207,255,.6)); }
#topbar .title .nav-ic { color:var(--cyan); }
.mode-badge { display:inline-flex; align-items:center; gap:8px; } .mode-badge .nav-ic { width:18px; height:18px; color:var(--cyan); }
.icon-btn { position:relative; width:40px; height:40px; min-height:0; display:grid; place-items:center; border-radius:50%; border:1px solid var(--border); background:rgba(16,27,56,.7); color:var(--text); cursor:pointer; list-style:none; }
.icon-btn::-webkit-details-marker { display:none; } .icon-btn .nav-ic { width:20px; height:20px; }
.bell-count { position:absolute; top:-4px; right:-4px; min-width:18px; height:18px; padding:0 5px; border-radius:9px; background:var(--purple); color:#fff; font-size:11px; font-weight:700; display:grid; place-items:center; box-shadow:0 0 10px rgba(146,59,255,.8); }
.help { position:relative; } .help summary { padding:0; }
.help-pop { position:absolute; right:0; top:48px; z-index:40; width:320px; padding:14px 16px; border-radius:14px; border:1px solid var(--border); background:#0C1533; box-shadow:0 0 30px rgba(0,0,0,.5); font-size:13px; color:var(--text-dim); }
.help-pop b { color:var(--text); } .help-pop p { margin:8px 0 0; }
.lg { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; vertical-align:-1px; } .lg-ada { background:var(--ada); } .lg-nox { background:var(--nox); }
/* Stav práce */
.work-list { list-style:none; margin:0; padding:6px 16px 12px; }
.work { display:flex; align-items:center; gap:12px; padding:6px 0; border-bottom:1px solid var(--border-soft); font-size:15px; }
.work:last-child { border-bottom:0; } .work b { margin-left:auto; font-size:18px; }
.work-ic { width:20px; height:20px; border-radius:50%; flex:none; border:2px solid var(--c,#8b96b8); display:grid; place-items:center; }
.work-ok { --c:var(--nox); } .work-ok .work-ic { background:var(--nox); box-shadow:0 0 10px rgba(0,231,121,.6); }
.work-ok .work-ic::after { content:""; width:8px; height:4px; border:2px solid #06130C; border-top:0; border-right:0; transform:rotate(-45deg) translate(1px,-1px); }
.work-work { --c:var(--purple); } .work-work .work-ic { box-shadow:0 0 10px rgba(146,59,255,.6); }
.work-wait { --c:#8B96B8; }
.work-warn { --c:var(--ada); } .work-warn .work-ic { border-radius:50%; box-shadow:0 0 10px rgba(255,138,24,.5); }
.work-warn .work-ic::after { content:"!"; color:var(--ada); font-weight:800; font-size:12px; line-height:1; }
/* Projekty */
.chips { display:flex; flex-wrap:wrap; gap:6px; padding:10px 16px 0; }
.chip { min-height:30px; padding:4px 12px; border-radius:999px; border:1px solid var(--border); background:rgba(16,27,56,.7); color:var(--text-dim); font-size:13px; }
.chip.on { background:var(--grad); color:#fff; border-color:transparent; box-shadow:0 0 12px rgba(146,59,255,.45); }
#projects { display:flex; flex-direction:column; gap:8px; }
.proj { display:flex; gap:12px; padding:10px 12px; border-radius:12px; border:1px solid var(--border-soft); background:rgba(16,27,56,.6); }
.proj-hot { border-color:rgba(255,138,24,.7); background:linear-gradient(120deg,rgba(255,138,24,.18),rgba(16,27,56,.7) 70%); box-shadow:0 0 16px rgba(255,138,24,.18); }
.proj-ic { color:var(--text-dim); } .proj-hot .proj-ic { color:var(--ada); } .proj-ic svg { width:26px; height:26px; }
.proj-body { min-width:0; flex:1; } .proj-head { display:flex; justify-content:space-between; gap:8px; } .proj-head b { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.proj-sub { font-size:12px; color:var(--text-dim); }
.proj-head span { white-space:nowrap; flex:none; color:var(--text); font-weight:600; }
@media(min-width:1600px) { #app { grid-template-columns:310px minmax(0,1fr) 320px; } }
.bar { height:6px; margin-top:6px; border-radius:3px; background:rgba(120,150,255,.15); overflow:hidden; }
.bar i { display:block; height:100%; border-radius:3px; background:linear-gradient(90deg,#00C96A,#00E779); box-shadow:0 0 8px rgba(0,231,121,.6); }
.proj-hot .bar i { background:linear-gradient(90deg,#FF7A00,#FF8A18); box-shadow:0 0 8px rgba(255,138,24,.6); }
/* Rychlé akce */
.quick { display:flex; flex-direction:column; gap:8px; padding:12px 16px 16px; }
.qa { display:flex; align-items:center; gap:10px; min-height:42px; padding:8px 14px; border-radius:12px; border:1px solid rgba(146,59,255,.45); background:rgba(146,59,255,.1); color:var(--text); font-size:14px; text-align:left; }
.qa .nav-ic { width:20px; height:20px; color:var(--cyan); }
.qa-ada { border-color:rgba(255,138,24,.65); background:rgba(255,138,24,.1); } .qa-ada .nav-ic { color:var(--ada); }
.qa-nox { border-color:rgba(0,231,121,.6); background:rgba(0,231,121,.08); } .qa-nox .nav-ic { color:var(--nox); }
.qa:hover { box-shadow:0 0 14px rgba(146,59,255,.3); }
/* odesílatel zprávy jako barevný štítek */
.bubble[data-agent="ada"] .meta > .who:first-of-type,.bubble[data-agent="nox"] .meta > .who:first-of-type { padding:1px 10px; border-radius:999px; font-size:12px; }
.bubble[data-agent="ada"] .meta > .who:first-of-type { color:#FFC08A; background:rgba(255,138,24,.16); border:1px solid rgba(255,138,24,.55); }
.bubble[data-agent="nox"] .meta > .who:first-of-type { color:#8AF7C2; background:rgba(0,231,121,.12); border:1px solid rgba(0,231,121,.5); }
/* ── Přehled 1:1 podle předlohy 02 (Michal 21. 11.) ── */
#dash { display:grid; gap:14px; grid-template-columns:minmax(0,1.05fr) minmax(0,1.55fr) minmax(0,1.05fr) minmax(0,.95fr);
  grid-template-areas:"proj chart work act" "proj tasks sources act" "ins reco quick act"; grid-template-rows:minmax(0,1.15fr) minmax(0,1fr) minmax(0,.85fr); }
.dash-projects { grid-area:proj; } .dash-chart { grid-area:chart; } .dash-work { grid-area:work; } .dash-activity { grid-area:act; }
.dash-tasks { grid-area:tasks; } .dash-sources { grid-area:sources; } .dash-insights { grid-area:ins; } .dash-reco { grid-area:reco; } .dash-quick { grid-area:quick; }
#dash > .panel { min-height:0; overflow:hidden; display:flex; flex-direction:column; }
#dash #projects,#dash .activity,#dash .dash-list,#dash .dot-list,#dash .check-list,#dash .work-list,#dash .quick { overflow:auto; min-height:0; }
#dash .chart-body { overflow:hidden; }
body:not(.room-focus) #app,body:not(.room-focus) .mobile-nav { display:none!important; }
body.room-focus #dash { display:none; }
.h-count { color:var(--cyan); font-weight:500; }
.link-btn { margin-left:auto; background:none; border:0; color:var(--cyan); font-size:14px; min-height:0; padding:0; cursor:pointer; }
.chart-body { flex:1; min-height:0; display:grid; grid-template-columns:auto 1fr; align-items:center; gap:12px; padding:8px 16px 12px; }
.chart-big { display:flex; flex-direction:column; gap:6px; } .chart-big span:first-child { font-size:44px; font-weight:700; text-shadow:0 0 18px rgba(146,59,255,.5); }
.chart-delta { font-size:13px; } .chart-delta[data-tone="ok"] { color:#7CF5B6; } .chart-delta[data-tone="bad"] { color:var(--bad-text); }
.chart-svg { width:100%; height:100%; min-height:110px; }
.chart-svg .grid { stroke:rgba(120,150,255,.18); stroke-width:1; } .chart-svg .ax { fill:#BBC6E1; font-size:10px; }
.chart-svg .pl { fill:none; stroke:#B784FF; stroke-width:2.5; filter:drop-shadow(0 0 4px #923BFF); }
.chart-svg .pt { fill:#C9A2FF; } .chart-svg .pt.last { fill:#fff; stroke:#923BFF; stroke-width:3; filter:drop-shadow(0 0 6px #923BFF); }
.dash-list { list-style:none; margin:0; padding:6px 16px 12px; }
.task-row,.src-row { display:flex; align-items:center; gap:12px; padding:8px 0; border-bottom:1px solid var(--border-soft); font-size:14px; }
.task-row:last-child,.src-row:last-child { border-bottom:0; }
.task-text { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.box { width:16px; height:16px; flex:none; border:1.5px solid #8B96B8; border-radius:4px; } .box.run { border-color:var(--purple); box-shadow:0 0 8px rgba(146,59,255,.6); }
.who-badge { flex:none; padding:2px 10px; border-radius:8px; font-size:12px; border:1px solid; }
.who-badge.ada { color:#FFC08A; border-color:rgba(255,138,24,.7); background:rgba(255,138,24,.14); }
.who-badge.nox { color:#8AF7C2; border-color:rgba(0,231,121,.6); background:rgba(0,231,121,.12); }
.who-badge.other { color:var(--text-dim); border-color:var(--border); }
.src-ic { color:var(--text-dim); } .src-st { flex:none; width:18px; height:18px; border-radius:50%; display:grid; place-items:center; font-size:11px; color:#8B96B8; border:1.5px solid #8B96B8; }
.src-st.ok { color:#06130C; background:var(--nox); border-color:var(--nox); box-shadow:0 0 8px rgba(0,231,121,.6); }
.dot-list,.check-list { list-style:none; margin:0; padding:8px 16px 14px; display:flex; flex-direction:column; gap:8px; font-size:14px; }
.dot-list li:not(.empty)::before { content:""; display:inline-block; width:9px; height:9px; margin-right:12px; border-radius:50%; background:var(--purple); box-shadow:0 0 8px var(--purple); vertical-align:1px; }
.new-badge { margin-left:6px; padding:2px 10px; border-radius:999px; background:rgba(146,59,255,.25); border:1px solid rgba(146,59,255,.6); font-size:12px; }
.dash-reco { border-color:rgba(0,231,121,.6)!important; box-shadow:0 0 24px rgba(0,231,121,.2),inset 0 0 18px rgba(0,231,121,.06)!important; }
.dash-reco .panel-title .nav-ic { color:var(--nox); filter:drop-shadow(0 0 6px rgba(0,231,121,.7)); }
.check-list li:not(.empty)::before { content:"✓"; display:inline-grid; place-items:center; width:18px; height:18px; margin-right:10px; border-radius:50%; background:var(--nox); color:#06130C; font-size:11px; font-weight:800; box-shadow:0 0 8px rgba(0,231,121,.6); }
.qa { justify-content:flex-start; } .qa span:nth-child(2) { flex:1; } .qa-arrow { font-size:18px; }
.qa-research { border-color:rgba(146,59,255,.7); background:rgba(146,59,255,.14); }
@media(min-width:1100px) and (min-height:680px) { #dash { flex:1; min-height:0; } }
@media(max-width:1280px) { #dash { grid-template-columns:repeat(2,minmax(0,1fr)); grid-template-areas:"proj chart" "work tasks" "sources reco" "ins quick" "act act"; grid-template-rows:none; } }
@media(max-width:899px) { #dash { grid-template-columns:1fr; grid-template-areas:"chart" "work" "proj" "tasks" "sources" "reco" "ins" "quick" "act"; } }
.session-lost { position:fixed; top:12px; left:50%; transform:translateX(-50%); z-index:50; max-width:min(720px,calc(100vw - 32px)); padding:12px 18px; border-radius:12px; border:1px solid #FF8A18; background:#2A1608; color:#FFE2C4; box-shadow:0 0 24px rgba(255,138,24,.35); font-size:14px; }
/* ── Výraznější neon + Přehled na jednu obrazovku (Michal 21. 11.: „výrazné jako na obrázku, žádný scroll“) ── */
body { background:radial-gradient(900px 420px at 50% -6%,rgba(146,59,255,.30),transparent 62%),radial-gradient(700px 400px at 0% 100%,rgba(4,207,255,.10),transparent 60%),radial-gradient(700px 400px at 100% 100%,rgba(146,59,255,.12),transparent 60%),#060B1C; }
.panel { border:1px solid rgba(120,150,255,.38); background:linear-gradient(165deg,rgba(24,40,86,.92),rgba(12,20,46,.94) 60%); box-shadow:0 0 0 1px rgba(4,207,255,.05),0 0 22px rgba(70,100,255,.14),inset 0 1px 0 rgba(255,255,255,.05); }
.panel-title h2 { font-size:17px; } .panel-icon,.panel-title .nav-ic { color:var(--cyan); filter:drop-shadow(0 0 6px rgba(4,207,255,.7)); }
/* metriky jako na předloze */
.metric { border-width:1.5px!important; padding:14px 18px; background:linear-gradient(135deg,color-mix(in srgb,var(--t) 26%,#0C1533),rgba(9,15,36,.96) 75%); box-shadow:0 0 26px color-mix(in srgb,var(--t) 32%,transparent),inset 0 0 18px color-mix(in srgb,var(--t) 12%,transparent); }
.metric-icon { width:52px; height:52px; } .metric-icon svg { width:44px; height:44px; filter:drop-shadow(0 0 10px var(--t)); }
.metric-label { font-size:15px; font-weight:500; } .metric-value { font-size:32px; line-height:1.1; text-shadow:0 0 14px color-mix(in srgb,var(--t) 40%,transparent); }
/* Ada a Nox: větší portrét přesahující kartu, trvalá identita barvou */
#masthead { padding-top:6px; }
.agent-hero { width:min(400px,100%); min-height:104px; margin-top:18px; border-width:1.5px; background:linear-gradient(110deg,color-mix(in srgb,var(--c) 30%,#101A3A),rgba(9,15,36,.96) 62%); box-shadow:0 0 28px color-mix(in srgb,var(--c) 34%,transparent),inset 0 0 22px color-mix(in srgb,var(--c) 12%,transparent); overflow:visible; }
.portrait-wrap { width:112px; height:104px; align-self:flex-end; margin-top:-26px; }
.portrait { width:112px; height:130px; margin-top:-26px; object-fit:cover; object-position:top; border-radius:0 0 12px 12px; filter:drop-shadow(0 0 12px color-mix(in srgb,var(--c) 60%,transparent)); }
.agent-hero[data-agent="ada"] .portrait { border-radius:0 0 12px 12px; } 
.halo { inset:-10px; border-radius:50%; opacity:.45; }
.agent-name { font-size:26px; } .agent-vendor { font-size:13px; color:#D9E0F5; }
.agent-state { font-size:15px; } .state-dot { width:11px; height:11px; }
.pulse-line { width:90px; height:22px; } .pulse-line path { stroke-width:2; opacity:.85; filter:drop-shadow(0 0 4px var(--c)); }
.agent-hero[data-state="working"]::after { box-shadow:0 0 34px color-mix(in srgb,var(--c) 75%,transparent); }
.hub { width:92px; height:92px; margin-top:14px; border-width:1.5px; box-shadow:0 0 34px rgba(146,59,255,.45),inset 0 0 24px rgba(4,207,255,.18); } .hub svg { width:58px; height:29px; filter:drop-shadow(0 0 8px rgba(4,207,255,.6)); }
#brand .name { font-size:34px; text-shadow:0 0 18px rgba(146,59,255,.35); } #brand .sub { font-size:15px; } #brand .mark,#brand .mark svg { width:72px; height:36px; }
.mode-badge { border-width:1.5px; box-shadow:0 0 16px rgba(146,59,255,.35); padding:8px 16px; font-size:14px; }
.human-badge { width:40px; height:40px; box-shadow:0 0 16px rgba(146,59,255,.55); }
/* navigace: větší, svítící linka */
.primary-nav { position:relative; padding:4px 0 10px; border-bottom:0; }
.primary-nav::after { content:""; position:absolute; left:0; right:0; bottom:0; height:2px; background:linear-gradient(90deg,transparent,#923BFF 20%,#04CFFF 55%,#923BFF 80%,transparent); box-shadow:0 0 14px rgba(146,59,255,.8); }
.primary-nav button,.primary-nav a { font-size:17px; padding:10px 22px; min-height:48px; }
.nav-ic { width:22px; height:22px; }
.primary-nav .current,.primary-nav .current:hover,.primary-nav [data-view].current:hover { box-shadow:0 0 24px rgba(146,59,255,.75),inset 0 0 10px rgba(255,255,255,.12); }
/* místnost a aktivita výrazněji */
#main { border-color:rgba(146,59,255,.6); box-shadow:0 0 30px rgba(146,59,255,.25),inset 0 0 30px rgba(146,59,255,.06); }
.bubble { background:linear-gradient(165deg,rgba(26,42,90,.9),rgba(16,27,60,.9)); border-color:rgba(120,150,255,.22); box-shadow:inset 3px 0 12px -6px var(--c); }
.act-portrait { width:46px; height:46px; box-shadow:0 0 12px color-mix(in srgb,var(--c) 60%,transparent); }
/* ── Přehled a Místnost na jednu obrazovku: posouvá se jen obsah panelů ── */
@media(min-width:1100px) and (min-height:680px) {
  html,body { height:100%; }
  body:not(.drawer-open) { overflow:hidden; }
  #shell { height:100dvh; display:flex; flex-direction:column; padding-bottom:16px; max-width:none; padding-left:24px; padding-right:24px; }
  #masthead { flex:none; }
  #overviewView:not([hidden]) { flex:1; min-height:0; display:flex; flex-direction:column; }
  #overview { flex:none; padding:12px 0 12px; }
  #app { flex:1; min-height:0; align-items:stretch; }
  #side,#activity { min-height:0; overflow:auto; }
  #side { padding-right:2px; }
  #main { height:auto!important; min-height:0!important; max-height:none; }
  .view-page:not([hidden]) { flex:1; min-height:0; overflow:auto; padding-bottom:8px; }
  body.room-focus #app { padding-top:12px; }
}
@media(min-width:1100px) and (max-width:1500px) and (min-height:680px) {
  #metrics { grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; }
  .metric { padding:10px 12px; gap:10px; } .metric-label { font-size:13px; } .metric-value { font-size:24px; }
  .metric-icon { width:34px; height:34px; } .metric-icon svg { width:28px; height:28px; }
  #app { grid-template-columns:230px minmax(0,1fr) 250px; }
}
@media(min-width:1100px) and (max-height:800px) {
  #brand .sub,.agent-role { display:none; }
  .agent-hero { min-height:72px; margin-top:10px; padding-top:4px; padding-bottom:4px; } .portrait-wrap { width:80px; height:72px; margin-top:-18px; } .portrait { width:80px; height:96px; margin-top:-18px; }
  .hub { width:62px; height:62px; margin-top:8px; } .hub svg { width:40px; height:20px; }
  .primary-nav button { min-height:38px; padding:6px 14px; font-size:15px; } .primary-nav { padding-bottom:8px; }
  #overview { padding:8px 0; }
  #hint { display:none; } #compose { padding-top:10px; padding-bottom:10px; gap:6px; } #topbar { padding-top:10px; padding-bottom:8px; }
}
@media(min-width:1100px) and (max-height:900px) {
  #dash { gap:10px; } #dash .panel-title { padding:9px 14px; } #dash .panel-title h2 { font-size:15px; }
  .chart-big span:first-child { font-size:34px; } .dot-list,.check-list { font-size:13px; gap:5px; padding-top:6px; }
  .task-row,.src-row,.work { padding:4px 0; font-size:14px; } .qa { min-height:36px; } .quick { gap:6px; padding:8px 14px; }
  .agent-hero { min-height:88px; margin-top:14px; } .portrait-wrap { width:96px; height:88px; } .portrait { width:96px; height:112px; }
  .agent-name { font-size:22px; } .hub { width:76px; height:76px; } #brand .name { font-size:28px; }
  .metric { padding:10px 14px; } .metric-value { font-size:26px; } .metric-icon { width:40px; height:40px; } .metric-icon svg { width:34px; height:34px; }
  .primary-nav button { min-height:42px; padding:8px 18px; font-size:16px; }
}
</style>
<a class="skip" href="#main">Přejít do místnosti</a>
<div id="shell">
  <header id="masthead">
    <div id="brand"><div class="mark"><svg viewBox="0 0 64 32" fill="none" aria-hidden="true"><defs><linearGradient id="infg" x1="0" x2="1"><stop offset="0" stop-color="#04CFFF"/><stop offset="1" stop-color="#923BFF"/></linearGradient></defs><path d="M6 16c0-6 4-10 10-10 11 0 21 20 32 20 6 0 10-4 10-10S54 6 48 6C37 6 27 26 16 26 10 26 6 22 6 16z" stroke="url(#infg)" stroke-width="5" stroke-linecap="round"/></svg></div><div><h1 class="name">Agent Lease</h1><div class="sub">Společný prostor pro tvoje agenty</div></div></div>
    <div id="agentHeroes"><article class="agent-hero" id="heroAda" data-agent="ada" data-state="unknown" aria-live="polite"><div class="portrait-wrap"><span class="halo" aria-hidden="true"></span><img class="portrait" src="/assets/ada.webp" alt="Portrét Ada" width="140" height="129"></div><div class="agent-info"><div class="agent-title"><span class="agent-name">Ada</span><span class="agent-vendor">Anthropic (Claude)</span></div><div class="agent-role">Analýza · Výzkum · Návrhy</div><div class="agent-state"><span class="state-dot" aria-hidden="true"></span><span class="state-text">Neznámý stav</span><svg class="pulse-line" viewBox="0 0 80 20" aria-hidden="true"><path d="M0 10h22l4-7 5 14 5-12 4 9 4-4h36"/></svg></div><div class="agent-task"></div></div></article><div class="hub" aria-hidden="true"><svg viewBox="0 0 64 32" fill="none" aria-hidden="true"><defs><linearGradient id="infg2" x1="0" x2="1"><stop offset="0" stop-color="#04CFFF"/><stop offset="1" stop-color="#923BFF"/></linearGradient></defs><path d="M6 16c0-6 4-10 10-10 11 0 21 20 32 20 6 0 10-4 10-10S54 6 48 6C37 6 27 26 16 26 10 26 6 22 6 16z" stroke="url(#infg2)" stroke-width="5" stroke-linecap="round"/></svg></div><article class="agent-hero" id="heroNox" data-agent="nox" data-state="unknown" aria-live="polite"><div class="portrait-wrap"><span class="halo" aria-hidden="true"></span><img class="portrait" src="/assets/nox.webp" alt="Portrét Nox" width="140" height="129"></div><div class="agent-info"><div class="agent-title"><span class="agent-name">Nox</span><span class="agent-vendor">OpenAI (GPT)</span></div><div class="agent-role">Implementace · Testy · Ověření</div><div class="agent-state"><span class="state-dot" aria-hidden="true"></span><span class="state-text">Neznámý stav</span><svg class="pulse-line" viewBox="0 0 80 20" aria-hidden="true"><path d="M0 10h22l4-7 5 14 5-12 4 9 4-4h36"/></svg></div><div class="agent-task"></div></div></article></div>
    <div id="headTools"><span id="modeBadge" class="mode-badge" title="Provozní režim místnosti"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/></svg><span id="modeText">Režim: …</span></span>
      <button class="icon-btn" id="bellBtn" type="button" aria-label="Co čeká na tebe" title="Úkoly ke kontrole a review čekající na tebe"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/></svg><span id="bellCount" class="bell-count" hidden>0</span></button>
      <details class="help"><summary class="icon-btn" aria-label="Nápověda"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 1-1 1.7M12 17v.5"/></svg></summary><div class="help-pop"><b>Barvy a stavy</b><p><span class="lg lg-ada"></span>Ada (Claude) · <span class="lg lg-nox"></span>Nox (Codex)</p><p>Karta agenta pulzuje jen tehdy, když agent opravdu pracuje. Čeká = online bez úkolu. Limit = vyčerpaný limit poskytovatele. Vyžaduje pozornost = úkol ke kontrole nebo zaseknutý most. Neznámý stav = web nemá čerstvá data.</p><p>Zvonek = úkoly ke kontrole a review, které čekají na tebe.</p></div></details><span id="humanBadge" class="human-badge" title="Přihlášený člověk">M</span></div>
    <nav class="primary-nav" aria-label="Hlavní navigace"><button class="current" aria-current="page" data-view="overview"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11 12 4l9 7v9h-6v-6H9v6H3z"/></svg>Přehled</button><button data-view="room"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg>Místnost agentů</button><button data-view="quality"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/></svg>Kvalita</button><button data-view="research"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/></svg>Výzkum</button><button data-view="memory"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/></svg>Paměť</button><button data-view="catalog"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>Katalog</button><button data-view="settings"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/></svg>Nastavení</button><button id="navTasks" type="button" hidden>Úkoly</button><button type="button" id="navAgents" hidden>Agenti</button><a href="#projectPanel" id="navProjects" hidden>Projekty</a><a href="#historyPanel" id="navHistory" hidden>Historie</a><button data-view="analytics" hidden>Analytics</button></nav>
    <div id="pause"><div class="pausebar"><div class="big">Načítám stav systému…</div></div></div>
  </header>
  <div id="overviewView" data-view-panel="overview">
  <section id="overview" aria-labelledby="overviewTitle">
    <div class="section-heading"><h2 id="overviewTitle" class="view-title" tabindex="-1">Přehled místnosti</h2><span id="scope">Fronta a review: celá místnost · běžící: aktivní úkol dle přehledu každého agenta</span></div>
    <div id="metrics">
      <div class="metric" data-tone-card="cyan"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></span><div><div class="metric-label">Aktivní projekty</div><div class="metric-value" id="metricAgents">—</div></div></div>
      <div class="metric" data-tone-card="purple"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/></svg></span><div><div class="metric-label">Otevřené úkoly</div><div class="metric-value" id="metricRunning">—</div></div></div>
      <div class="metric" data-tone-card="orange"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"/><path d="M7.5 15h9"/></svg></span><div><div class="metric-label">Probíhající analýzy</div><div class="metric-value" id="metricQueued">—</div></div></div>
      <div class="metric" data-tone-card="green"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg></span><div><div class="metric-label" title="Pouze úkoly v nejvýše 200 posledních načtených zprávách">Dokončené výstupy</div><div class="metric-value" id="metricDone">—</div></div></div>
      <div class="metric" data-tone-card="purple"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12v18l-6-4-6 4z"/></svg></span><div><div class="metric-label">Nalezené insighty</div><div class="metric-value" id="metricReview">—</div></div></div>
      <div class="metric" data-tone-card="cyan"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 20V12M10 20V8M15 20v-6M20 20V4"/></svg></span><div><div class="metric-label">Celkový postup</div><div class="metric-value" id="metricReviews">—</div></div></div>
      <div class="metric metric-system" data-tone-card="cyan"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg></span><div><div class="metric-label">Stav systému</div><div class="metric-value small" id="metricSystem">Načítání…</div></div></div>
    </div>
  </section>

  <div id="dash">
    <section id="projectPanel" class="panel dash-projects"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><h2>Projekty</h2></div><div class="chips" role="group" aria-label="Filtr projektů"><button class="chip on" data-proj="all" aria-pressed="true">Vše</button><button class="chip" data-proj="open" aria-pressed="false">V review</button><button class="chip" data-proj="done" aria-pressed="false">Schválené</button></div><div class="panel-body" id="projects"><div class="empty">Data zatím nejsou načtená.</div></div></section>
    <section class="panel dash-chart" id="chartPanel"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/></svg><h2>Celkový postup napříč projekty</h2></div><div class="chart-body"><div class="chart-big"><span id="chartPct">—</span><span id="chartDelta" class="chart-delta"></span></div><svg id="chartSvg" class="chart-svg" viewBox="0 0 360 150" preserveAspectRatio="none" role="img" aria-label="Podíl schválených review za posledních 7 dní"></svg></div></section>
    <section class="panel dash-work" id="workPanel"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/></svg><h2>Stav práce</h2></div><ul class="work-list" id="workList"><li class="empty">Načítám…</li></ul></section>
    <section class="panel dash-tasks" id="tasksPanel"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg><h2>Aktivní úkoly <span id="tasksCount" class="h-count"></span></h2><button class="link-btn" data-goto="room-tasks">Zobrazit vše →</button></div><ul id="tasksList" class="dash-list"><li class="empty">Načítám…</li></ul></section>
    <section class="panel dash-sources" id="sourcesPanel"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h9l4 4v14H6z"/><path d="M9 12h7M9 16h5"/></svg><h2>Zdroje znalostí <span id="sourcesCount" class="h-count"></span></h2><button class="link-btn" data-goto="memory">Zobrazit vše →</button></div><ul id="sourcesList" class="dash-list"><li class="empty">Načítám…</li></ul></section>
    <section class="panel dash-insights" id="insightsPanel"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.4 1 1.1 1 1.8V16h5v-.3c0-.7.4-1.4 1-1.8A6 6 0 0 0 12 3z"/></svg><h2>Klíčové insighty</h2><span id="insightsNew" class="new-badge" hidden></span></div><ul id="insightsList" class="dot-list"><li class="empty">Načítám…</li></ul></section>
    <section class="panel dash-reco" id="recoPanel"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 21V4"/><path d="M5 4h12l-2 4 2 4H5"/></svg><h2>Doporučení dalšího postupu</h2></div><ul id="recoList" class="check-list"><li class="empty">Načítám…</li></ul></section>
    <section class="panel dash-quick" id="quickPanel"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg><h2>Rychlé akce</h2></div><div class="quick">
        <button class="qa qa-research" data-qa="research"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/></svg><span>Pokračovat ve výzkumu</span><span class="qa-arrow" aria-hidden="true">→</span></button>
        <button class="qa qa-ada" data-qa="task-ada"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"/></svg><span>Zadat novou analýzu Adě</span><span class="qa-arrow" aria-hidden="true">→</span></button></div></section>
    <aside id="activity" class="panel dash-activity" aria-label="Aktivita agentů"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg><h2>Aktivita agentů</h2></div><ol id="activityList" class="activity"><li class="empty">Čekám na zprávy Ady a Noxe.</li></ol></aside>
  </div>
  <nav class="mobile-nav" aria-label="Přejít na část přehledu"><a href="#main">Místnost</a><button type="button" data-panel="side" aria-haspopup="dialog" aria-expanded="false">Agenti</button><button type="button" data-panel="inspector" aria-haspopup="dialog" aria-expanded="false">Detail</button></nav>
  <div id="app">
    <aside id="side" aria-label="Účastníci a pracovní prostor">
      
      
      
      <section class="panel" id="agentPanel"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3.5"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M16 4.5a3.5 3.5 0 0 1 0 7M18 14c2 .8 3 2.7 3 6"/></svg><h2>Agenti a mosty</h2><span id="agentCount" class="count">—</span></div><div class="panel-body"><div id="agents"></div><details id="offlineBox"><summary id="offlineSummary">Offline</summary><div id="offline"></div></details></div></section>
      <section class="panel"><div class="panel-title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg><h2>Zamčené cesty</h2></div><div class="panel-body" id="claims"></div></section>
    </aside>
    <main id="main" class="panel" tabindex="-1">
      <div id="topbar">
        <div class="title"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg> Místnost agentů <button class="btn panel-trigger" id="openAgents" type="button" data-panel="side" aria-haspopup="dialog" aria-expanded="false">Agenti</button><button class="btn panel-trigger" id="openDetail" type="button" data-panel="inspector" aria-haspopup="dialog" aria-expanded="false">Detail</button><div id="status" role="status"><span class="dot"></span><span id="statusText">připojuji…</span></div></div>
        <div class="tools filter-tools"><div class="filters" role="group" aria-label="Filtrovat zprávy">
          <button class="filter on" data-filter="all" aria-pressed="true">Vše</button><button class="filter" data-filter="task" aria-pressed="false">Úkoly</button><button class="filter" data-filter="result" aria-pressed="false">Výsledky</button><button class="filter" data-filter="system" aria-pressed="false">Systém</button>
        </div></div>
        <div class="tools feed-tools"><span id="messageCount" class="note">Načítám zprávy…</span><label class="search"><span class="sr-only">Hledat ve zprávách</span><input id="search" type="search" placeholder="Hledat text, agenta, #ID…"></label><label class="system-toggle"><input type="checkbox" id="showSystem"> systémové příjemky</label></div>
      </div>
      <div id="feed" aria-label="Zprávy místnosti"></div>
      <div id="compose">
        <div class="row"><div class="seg" id="kindSeg"><button type="button" data-kind="chat" class="on" aria-pressed="true">Zpráva</button><button type="button" data-kind="task" aria-pressed="false">Úkol</button></div><label>Komu <select id="to"></select></label></div>
        <div id="hint"></div>
        <div class="row"><label class="sr-only" for="text">Text zprávy nebo úkolu</label><textarea id="text" rows="2" placeholder="Napiš zprávu… (Enter pošle, Shift+Enter nový řádek)"></textarea><button id="sendBtn" type="button">Poslat</button></div>
      </div>
    </main>
    
    <aside id="inspector" aria-label="Detail a zaznamenané události">
      <section id="qgOverviewPanel" class="panel qg-overview"><div class="panel-title"><svg class="qg-shield" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m8 12 3 3 5-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg><h2>Quality Gate</h2><button class="btn" data-view="quality">Všechny repozitáře</button></div><div class="panel-body"><span class="qg-mode" data-qg-mode></span><div class="qg-summary" id="qgCompactSummary"></div><div id="qgCompactTable"></div><p class="note">Zobrazují se pouze zaznamenané běhy. Chybějící kontroly nejsou PASS.</p></div></section>
      <section id="timelinePanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◎</span><h2 id="timelineTitle">Záznamy úkolu</h2></div><div class="panel-body" id="taskTimeline"><div class="empty">Vyber úkol z místnosti.</div></div></section>
      <section id="taskPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◎</span><h2 id="detailTitle" tabindex="-1">Detail úkolu</h2></div><div class="panel-body"><label class="sr-only" for="taskPicker">Vybrat úkol z načtené historie</label><select id="taskPicker"><option value="">Vyber úkol z místnosti</option></select><div id="taskDetail"><div class="empty">Vyber úkol. Zobrazí se jeho skutečný stav a navázané zprávy.</div></div></div></section>
      <section id="reviewPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◇</span><h2>Review commitů</h2><span id="reviewCount" class="count">—</span></div><div class="panel-body" id="reviews"></div></section>
      <section id="historyPanel" class="panel"><div class="panel-title"><span class="panel-icon" aria-hidden="true">◷</span><h2>Poslední události</h2></div><div class="panel-body" id="recentEvents"><div class="empty">Čekám na zaznamenané zprávy.</div></div></section>
    </aside>
  </div>
</div>
  <section id="qualityPage" class="view-page" data-view-panel="quality" hidden aria-labelledby="qualityTitle"><div class="view-header"><div><h2 id="qualityTitle" class="view-title" tabindex="-1"><svg class="qg-shield" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m8 12 3 3 5-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg> Quality Gate</h2><p class="view-subtitle">Kvalita práce napříč repozitáři</p></div><div class="qg-actions"><span class="qg-mode" data-qg-mode></span><button class="btn" data-qg-demo aria-pressed="false">Zobrazit demo</button><button class="btn" data-view="overview">Zpět do místnosti</button></div></div><p class="qg-demo-note">Přehled kontrol pro konkrétní commity. Demo obsahuje pouze ukázková data.</p><div class="qg-page-stats"><p>Kontrolované repozitáře: <strong id="qgRepoCount">—</strong><br>Poslední kontrola: <strong id="qgLastCheck">Není známá</strong></p><div class="qg-summary" id="qgPageSummary"></div></div><div id="qgRequests" role="status"></div><div class="qg-page-grid"><section class="panel"><div class="panel-title"><h2>Výsledky projektů</h2></div><div class="panel-body" id="qgProjectTable"></div></section><section class="panel"><div class="panel-title"><h2 id="qgProjectTitle" tabindex="-1">Detail repozitáře</h2></div><div class="panel-body" id="qgProjectDetail"></div></section></div></section>
  <section class="view-page" data-view-panel="catalog" hidden aria-labelledby="catalogTitle"><div class="view-header"><div><h2 id="catalogTitle" class="view-title" tabindex="-1">Katalog schopností</h2><p class="view-subtitle">Fáze D. Koncept: aplikace zatím nemá data ani API katalogu, proto zde nic není živé ani klikací.</p></div><button class="btn" data-view="overview">Zpět na přehled</button></div>
    <div class="concept-grid"><section class="panel concept"><div class="panel-title"><h2>Co katalog bude umět</h2><span class="concept-badge">koncept</span></div><div class="panel-body"><ul class="concept-list"><li>Seznam schopností Ady a Noxe: nástroje, skilly, MCP servery.</li><li>U každé položky účel, vstupy a výstupy, závislosti, testy a dokumentace.</li><li>Kompatibilita s Adou a Noxem jen ověřená, nikdy automaticky „oba“.</li><li>Výběr krátkého seznamu nástrojů k úkolu, aby agent nemusel číst celý inventář.</li></ul></div></section>
    <section class="panel concept"><div class="panel-title"><h2>Kde jsou data dnes</h2><span class="concept-badge">jen dokument</span></div><div class="panel-body"><p class="note">Inventura z 19. 11. 2026 je v repozitáři jako dokument:</p><ul class="concept-list mono"><li>docs/research/2026-11-19-d-capabilities-ada.md</li><li>docs/research/2026-11-19-d-capabilities-nox.md</li></ul><p class="note">Napojení na aplikaci je samostatná změna s vlastním schválením.</p></div></section></div></section>
  <section class="view-page" data-view-panel="analytics" hidden><div class="panel placeholder-page"><h2 class="view-title" tabindex="-1">Analytics · připravuje se</h2><p>Výkon za 24 hodin ani skutečná úspěšnost nejsou v dostupném API definované. Metriky a grafy zatím nejsou připojené.</p><button class="btn" data-view="overview">Zpět do místnosti</button></div></section>
  <section class="view-page" data-view-panel="research" hidden aria-labelledby="researchTitle"><div class="view-header"><div><h2 id="researchTitle" class="view-title" tabindex="-1">Výzkumný tým</h2><p class="view-subtitle">Části běží izolovaně jen pro čtení, pak jedna syntéza. Zjištění jsou neověřená, dokud je někdo nepotvrdí v paměti.</p></div><button class="btn" data-view="overview">Zpět do místnosti</button></div>
    <section class="panel"><div class="panel-body"><div id="rsList" role="status">Načítám…</div></div></section></section>
  <section class="view-page" data-view-panel="memory" hidden aria-labelledby="memoryTitle"><div class="view-header"><div><h2 id="memoryTitle" class="view-title" tabindex="-1">Sdílená paměť</h2><p class="view-subtitle">Příznak → příčina → řešení. Za fakt platí jen ověřené záznamy, ostatní jsou tipy ke kontrole.</p></div><button class="btn" data-view="overview">Zpět do místnosti</button></div>
    <div class="kb-grid"><section class="panel"><div class="panel-title"><h2>Záznamy</h2></div><div class="panel-body"><div class="kb-tools"><label class="search"><span class="sr-only">Hledat v paměti</span><input id="kbQuery" type="search" placeholder="Hledat příznak, chybu, příčinu…"></label><select id="kbStatus" aria-label="Stav"><option value="">Všechny stavy</option><option value="verified">Ověřené</option><option value="unverified">Neověřené</option><option value="rejected">Zamítnuté</option><option value="stale">Zastaralé</option></select></div><div id="kbList" role="status">Načítám…</div></div></section>
    <section class="panel"><div class="panel-title"><h2>Nový záznam</h2></div><div class="panel-body kb-form"><label>Příznak *<textarea id="kbSymptom" rows="2" maxlength="4000"></textarea></label><label>Příčina<textarea id="kbCause" rows="2" maxlength="4000"></textarea></label><label>Řešení<textarea id="kbSolution" rows="3" maxlength="4000"></textarea></label><label>Projekt<input id="kbProject" maxlength="200"></label><label>Důkaz (commit, test, odkaz)<input id="kbEvidence" maxlength="1000"></label><button class="btn detail" id="kbAdd" type="button">Uložit jako neověřené</button><p class="note">Ověřit záznam smí jen někdo jiný než autor, vždy s důkazem.</p></div></section></div></section>
  <section class="view-page" data-view-panel="settings" hidden aria-labelledby="settingsTitle"><div class="view-header"><div><h2 id="settingsTitle" class="view-title" tabindex="-1">Provozní režim</h2><p class="view-subtitle">Jak samostatně Ada a Nox pracují. Přepnout smí jen člověk tady na webu. Základní bezpečnost a 🛑 nouzové zastavení platí vždy.</p></div><button class="btn" data-view="overview">Zpět do místnosti</button></div>
    <div class="mode-grid" id="modeGrid" role="radiogroup" aria-label="Provozní režim"></div><p class="note" id="modeInfo"></p></section>
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
let latestReviews = null;
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
  const narrow = id === "side" ? matchMedia("(max-width:899px)").matches : true;   // detail úkolu vždy ve vysouvacím panelu
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
  // Board pokrývá celou frontu; historie zpráv je omezena na 200 položek.
  // API poskytuje nejvýše jeden aktivní úkol na agenta (leased nebo started).
  setMetric("metricDone", tasks.filter(m => m.state === "succeeded").length);
  renderSystemMetric();
  renderHeroes();
  renderActivity();
  renderWork();
  renderDash();
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
// Stav Ady a Noxe jen ze skutečných dat (peers + board), každý zvlášť; nikdy nezůstane „Pracuje“ při výpadku spojení.
const HERO = { ada: ["claude-code", "claude-code-bridge"], nox: ["codex", "codex-bridge"] };
const ONLINE_S = 120, STALE_MS = 60000;   // stav místnosti chodí po SSE každé 2 s; 60 s bez něj = data neplatí
let lastRoomAt = 0;
function agentStatus(key) {
  if (connectionMode !== "live") return {state: "unknown", label: "Spojení přerušeno", detail: ""};
  if (!lastRoomAt || Date.now() - lastRoomAt > STALE_MS) return {state: "unknown", label: "Neznámý stav", detail: "data nejsou aktuální"};
  const [main, bridge] = HERO[key];
  const peer = a => latestPeers.find(p => p.agent === a);
  const card = a => (latestBoard || []).find(b => b.agent === a);
  const pm = peer(main), pb = peer(bridge), bm = card(main), bb = card(bridge);
  const fresh = p => p && p.active && p.seen_seconds_ago != null && p.seen_seconds_ago < ONLINE_S;
  // limit hlášený poskytovatelem i ručně (`limit` → presence rate-limited + limited_until)
  const nowS = Date.now() / 1000;
  const limited = [pm, pb, bm, bb].find(x => x && (x.provider_state === "limited" || x.presence === "rate-limited"
    || (x.limited_until && x.limited_until > nowS)));
  const active = (bb && bb.active) || (bm && bm.active);
  if (active) return {state: "working", label: "Pracuje", detail: `#${active.id} ${active.text || ""}`};
  if (limited) { const until = limited.provider_resets_at || limited.limited_until;
    return {state: "limited", label: "Limit" + (until ? " do " + clock(until) : ""), detail: limited.provider_reason || ""}; }
  if (fresh(pm) && pm.presence === "busy" && pm.status && pm.status !== "start session") return {state: "working", label: "Pracuje", detail: pm.status};
  const problem = (bm && bm.needs_review) ? `ke kontrole ${bm.needs_review}`
    : (bm && bm.blocked_reason && fresh(pm)) ? bm.blocked_reason
    : (bb && bb.blocked_reason && fresh(pb)) ? "most: " + bb.blocked_reason : "";
  if (problem) return {state: "error", label: "Vyžaduje pozornost", detail: problem};
  const unknownLimit = [pm, pb, bm, bb].some(x => x && x.provider_state === "unknown");
  if (fresh(pm) || fresh(pb)) return {state: "idle", label: "Čeká", detail: unknownLimit ? "stav limitu poskytovatele neznámý"
    : fresh(pm) ? (pm.status || "") : "most připraven převzít úkol"};
  return {state: "offline", label: "Offline", detail: pm && pm.seen_seconds_ago != null ? "naposledy " + ago(pm.seen_seconds_ago) : ""};
}
function renderHeroes() {
  for (const key of ["ada", "nox"]) {
    const el = document.querySelector(`.agent-hero[data-agent="${key}"]`); if (!el) continue;
    const s = agentStatus(key);
    if (el.dataset.state !== s.state) el.dataset.state = s.state;
    el.querySelector(".state-text").textContent = s.label;
    const task = el.querySelector(".agent-task"); task.textContent = s.detail.slice(0, 90); task.title = s.detail;
  }
}
setInterval(() => { if (!document.hidden) renderHeroes(); }, 5000);
function renderWork() {
  const tasks = historyMessages().filter(m => m.kind === "task");
  const board = latestBoard || [];
  const escalated = (latestReviews || []).filter(r => r.state === "escalated").length;
  const attention = board.reduce((s, b) => s + (b.needs_review || 0), 0) + escalated;
  const rows = [["ok", "Dokončeno", tasks.filter(m => m.state === "succeeded").length, "úkoly v načtené historii"],
                ["work", "Probíhá", board.filter(b => b.active).length, "právě běžící úkoly"],
                ["wait", "Ve frontě", board.reduce((s, b) => s + (b.queued || 0), 0), "nepřevzaté doručenky ve frontě agentů (zprávy i úkoly)"],
                ["warn", "Vyžaduje pozornost", attention, "ke kontrole a review čekající na tebe"]];
  setHTMLIfChanged(document.getElementById("workList"), rows.map(([cls, label, n, hint]) =>
    `<li class="work work-${cls}" title="${esc(hint)}"><span class="work-ic" aria-hidden="true"></span><span>${label}</span><b>${latestBoard === null && cls !== "ok" ? "—" : n}</b></li>`).join(""));
  const bell = document.getElementById("bellCount");
  bell.hidden = !attention; bell.textContent = attention;
}
function openRoomTasks() { showView("room", {focus: false}); document.querySelector('[data-filter="task"]').click(); }
document.getElementById("bellBtn").onclick = openRoomTasks;
document.querySelectorAll("[data-goto]").forEach(b => b.onclick = () => b.dataset.goto === "room-tasks" ? openRoomTasks() : showView(b.dataset.goto));
document.querySelectorAll("[data-qa]").forEach(b => b.onclick = () => {
  const qa = b.dataset.qa;
  if (qa === "research") return showView("research");
  showView("room", {focus: false});
  setKind("task"); toEl.value = "claude-code"; syncHint(); textEl.focus();
});
// ── Přehled podle předlohy 02: jen skutečná data ──
let latestReviewsAll = null, dashResearch = null, dashKnowledge = null;
const DONE_REVIEW = r => r.verdict === "approved" && r.verdict_at;
function loadDashData() {
  fetch("/api/research").then(r => r.ok ? r.json() : null).then(d => { if (d) { dashResearch = d.research || []; renderDash(); } }).catch(() => {});
  fetch("/api/knowledge").then(r => r.ok ? r.json() : null).then(d => { if (d) { dashKnowledge = d.entries || []; renderDash(); } }).catch(() => {});
}
setInterval(() => { if (!document.hidden && currentView === "overview") loadDashData(); }, 60000);
function renderChart() {
  const all = latestReviewsAll || [], svg = document.getElementById("chartSvg");
  if (!all.length) { document.getElementById("chartPct").textContent = "—"; svg.innerHTML = ""; return; }
  const day = 86400, end = new Date(); end.setHours(24, 0, 0, 0);
  const points = [...Array(7)].map((_, i) => {
    const t = end.getTime() / 1000 - (6 - i) * day;
    const created = all.filter(r => r.created_at <= t), done = created.filter(r => DONE_REVIEW(r) && r.verdict_at <= t);
    return {t, pct: created.length ? Math.round(100 * done.length / created.length) : 0};
  });
  const now = points[6].pct, delta = now - points[0].pct;
  document.getElementById("chartPct").textContent = now + " %";
  const d = document.getElementById("chartDelta"); d.textContent = (delta >= 0 ? "▲ +" : "▼ ") + delta + " p. b. (7 dní)"; d.dataset.tone = delta >= 0 ? "ok" : "bad";
  const W = 360, H = 150, L = 42, B = 20, x = i => L + i * (W - L - 8) / 6, y = p => 8 + (H - B - 8) * (1 - p / 100);
  const line = points.map((p, i) => `${x(i)},${y(p.pct)}`).join(" ");
  const days = ["Ne","Po","Út","St","Čt","Pá","So"];
  svg.innerHTML = `<defs><linearGradient id="cg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#923BFF" stop-opacity=".55"/><stop offset="1" stop-color="#923BFF" stop-opacity="0"/></linearGradient></defs>`
    + [0, 25, 50, 75, 100].map(p => `<line x1="${L}" x2="${W - 8}" y1="${y(p)}" y2="${y(p)}" class="grid"/><text x="${L - 6}" y="${y(p) + 4}" class="ax" text-anchor="end">${p} %</text>`).join("")
    + `<polygon points="${x(0)},${y(0)} ${line} ${x(6)},${y(0)}" fill="url(#cg)"/><polyline points="${line}" class="pl"/>`
    + points.map((p, i) => `<circle cx="${x(i)}" cy="${y(p.pct)}" r="${i === 6 ? 5 : 3.5}" class="${i === 6 ? "pt last" : "pt"}"><title>${p.pct} % schváleno</title></circle><text x="${x(i)}" y="${H - 4}" class="ax" text-anchor="middle">${days[new Date((p.t - day) * 1000).getDay()]}</text>`).join("");
}
function renderDash() {
  const all = latestReviewsAll || [], board = latestBoard || [];
  const tasks = historyMessages().filter(m => m.kind === "task");
  const openRepos = new Set(all.filter(r => !["approved", "closed"].includes(r.state)).map(r => r.repo));
  setMetric("metricAgents", latestReviewsAll === null ? "—" : openRepos.size);
  const OPEN = ["pending", "leased", "started", "accepted", "unread"];
  setMetric("metricRunning", tasks.filter(m => OPEN.includes(m.state || "pending")).length);
  setMetric("metricQueued", dashResearch === null ? "—" : dashResearch.filter(r => ["running", "synthesizing"].includes(r.state)).length);
  setMetric("metricReview", dashResearch === null ? "—" : dashResearch.filter(r => r.state === "done" || r.state === "partial").length);
  setMetric("metricReviews", all.length ? Math.round(100 * all.filter(DONE_REVIEW).length / all.length) + " %" : "—");
  renderChart();
  // aktivní úkoly: otevřené úkoly v načtené historii, se štítkem adresáta
  const open = tasks.filter(m => ["pending", "leased", "started", "accepted", "unread"].includes(m.state || "pending")).slice(-6).reverse();
  document.getElementById("tasksCount").textContent = `(${tasks.filter(m => ["pending", "leased", "started", "accepted", "unread"].includes(m.state || "pending")).length})`;
  setHTMLIfChanged(document.getElementById("tasksList"), open.length ? open.map(m => {
    const who = m.recipient === "claude-code" ? ["Ada", "ada"] : m.recipient === "codex" ? ["Nox", "nox"] : [nameOf(m.recipient), "other"];
    return `<li class="task-row"><span class="box ${m.state === "started" ? "run" : ""}" aria-hidden="true"></span><span class="task-text" title="${esc(m.text)}">${esc((m.text || "").split(String.fromCharCode(10))[0].slice(0, 90))}</span><span class="who-badge ${who[1]}">${esc(who[0])}</span></li>`;
  }).join("") : "<li class='empty'>Žádné otevřené úkoly.</li>");
  // zdroje znalostí: sdílená paměť
  const kb = dashKnowledge || [];
  document.getElementById("sourcesCount").textContent = dashKnowledge === null ? "" : `(${kb.length})`;
  setHTMLIfChanged(document.getElementById("sourcesList"), kb.length ? kb.slice(0, 5).map(e => `<li class="src-row"><span class="src-ic" aria-hidden="true">▤</span><span class="task-text" title="${esc(e.symptom)}">${esc((e.project ? e.project + " – " : "") + e.symptom.slice(0, 70))}</span><span class="src-st ${e.status === "verified" ? "ok" : ""}" title="${esc(KB_LABEL[e.status] || e.status)}">${e.status === "verified" ? "✓" : "○"}</span></li>`).join("")
    : `<li class="empty">${dashKnowledge === null ? "Načítám…" : "Paměť je zatím prázdná."}</li>`);
  // klíčové insighty: dokončené výzkumy
  const done = (dashResearch || []).filter(r => r.state === "done" || r.state === "partial");
  const fresh = done.filter(r => r.finished_at && Date.now() / 1000 - r.finished_at < 86400).length;
  const nb = document.getElementById("insightsNew"); nb.hidden = !fresh; nb.textContent = `${fresh} nové`;
  setHTMLIfChanged(document.getElementById("insightsList"), done.length ? done.slice(0, 3).map(r => `<li title="${esc(r.question)}">${esc(r.question.slice(0, 95))}</li>`).join("")
    : `<li class="empty">${dashResearch === null ? "Načítám…" : "Zatím žádný dokončený výzkum."}</li>`);
  // doporučení: co čeká na člověka (review a úkoly ke kontrole)
  const reco = [];
  all.filter(r => r.state === "escalated").forEach(r => reco.push(`Rozhodnout review #${r.id} – ${r.topic.slice(0, 60)}`));
  all.filter(r => r.state === "approved").forEach(r => reco.push(`Sloučit schválené review #${r.id} – ${r.topic.slice(0, 55)}`));
  board.filter(b => b.needs_review).forEach(b => reco.push(`Zkontrolovat ${b.needs_review} úkol(y) u ${nameOf(b.agent)}`));
  all.filter(r => r.state === "changes_requested" && r.author === me).forEach(r => reco.push(`Opravit review #${r.id} podle připomínek`));
  setHTMLIfChanged(document.getElementById("recoList"), reco.length ? reco.slice(0, 4).map(x => `<li>${esc(x)}</li>`).join("") : "<li class='empty'>Nic nečeká na tvé rozhodnutí.</li>");
}   // stáří dat se projeví i bez nové zprávy
function renderActivity() {
  const items = historyMessages().filter(m => (m.agent === "claude-code" || m.agent === "codex") && !isSystem(m)).slice(-8).reverse();
  setHTMLIfChanged(document.getElementById("activityList"), items.length ? items.map(m => {
    const key = m.agent === "claude-code" ? "ada" : "nox";
    return `<li class="act" data-agent="${key}"><img class="act-portrait" src="/assets/${key}.webp" alt=""><div class="act-body"><div class="act-head"><b>${key === "ada" ? "Ada" : "Nox"}</b><time>${clock(Date.now() / 1000 - (m.seconds_ago || 0))}</time></div><p>${esc((m.text || "").slice(0, 120))}${(m.text || "").length > 120 ? "…" : ""}</p></div></li>`;
  }).join("") : "<li class='empty'>Čekám na zprávy Ady a Noxe.</li>");
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
let projFilter = "all", lastProjReviews = [];
function renderProjects(reviews) {
  lastProjReviews = reviews || [];
  const by = new Map();
  lastProjReviews.forEach(r => { if (!r.repo) return; const s = by.get(r.repo) || {total: 0, done: 0, open: 0}; s.total++;
    if (["approved", "closed"].includes(r.state)) s.done++; else s.open++; by.set(r.repo, s); });
  const rows = [...by.entries()].sort((a, b) => b[1].open - a[1].open || a[0].localeCompare(b[0]))
    .filter(([, s]) => projFilter === "all" || (projFilter === "open" ? s.open > 0 : s.open === 0));
  const folder = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
  setHTMLIfChanged(document.getElementById("projects"), rows.length ? rows.map(([repo, s], i) => {
    const pct = Math.round(100 * s.done / s.total);
    return `<div class="proj ${i === 0 && s.open ? "proj-hot" : ""}" title="${esc(repo)}"><span class="proj-ic">${folder}</span><div class="proj-body"><div class="proj-head"><b>${esc(repo.split("/").filter(Boolean).pop() || repo)}</b><span>${pct} %</span></div><div class="proj-sub">${s.done}/${s.total} review schváleno${s.open ? ` · ${s.open} otevřených` : ""}</div><div class="bar"><i style="width:${pct}%"></i></div></div></div>`;
  }).join("") : `<div class="empty">Pro tento filtr nejsou žádné projekty.</div>`);
}
document.querySelectorAll("[data-proj]").forEach(b => b.onclick = () => {
  projFilter = b.dataset.proj;
  document.querySelectorAll("[data-proj]").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
  renderProjects(lastProjReviews);
});


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

// escapuje i uvozovky: výstup se vkládá do textu i do atributů (title="…") — bezpečnostní kontrola 21. 11.
function esc(s) { return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }
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
  renderHeroes();
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
      const detail = document.createElement("button"); detail.className = "btn detail"; detail.textContent = "Detail úkolu";
      detail.setAttribute("aria-label",`Detail úkolu #${m.id}`); detail.onclick = () => selectTask(m.id,true); div.appendChild(detail);
    }
    div._actionsKey = null;
  }
  div.dataset.agent = m.agent === "claude-code" ? "ada" : m.agent === "codex" ? "nox" : m.agent === me ? "me" : "other";
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
    if (m.fix) {
      const info = document.createElement("div");
      info.className = "fix-scope";
      info.textContent = `Oprava se zápisem · repozitář ${m.fix.repo} · základ ${m.fix.base_commit.slice(0, 12)} · cesty: ${m.fix.allowed.length ? m.fix.allowed.join(", ") : "celé repo"}`;
      actions.appendChild(info);
    }
    if (m.fix && ["pending", "needs_review"].includes(m.state) && (m.state === "pending" || (m.last_error || "").startsWith("fix:"))) {
      add("Povolit zápis jednou", () => {
        const scope = `Repozitář: ${m.fix.repo}\nZáklad: ${m.fix.base_commit}\nSmí měnit: ${m.fix.allowed.length ? m.fix.allowed.join(", ") : "celé repo"}`;
        const reason = prompt(`Tato oprava smí zapisovat do repozitáře.\n${scope}\n\nProč ji povolit? (zaznamená se)`);
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
  lastRoomAt = Date.now();
  if (r.me) { me = r.me; document.getElementById("humanBadge").textContent = String(me).slice(0, 1).toUpperCase(); }
  if (Object.prototype.hasOwnProperty.call(r, "quality_runs")) {
    const records = Array.isArray(r.quality_runs) ? r.quality_runs : [];
    qgRecords = records.filter(record => record && record.kind === "quality_run" && record.executed === true);
  }
  if (Array.isArray(r.quality_requests)) qgRequests = r.quality_requests;
  if (Object.prototype.hasOwnProperty.call(r, "quality_runs") || Object.prototype.hasOwnProperty.call(r, "quality_requests")) renderQualityGate();
  latestBoard = Array.isArray(r.board) ? r.board : null;
  renderReviews(r.reviews);
  if (Array.isArray(r.reviews)) latestReviews = r.reviews;
  if (Array.isArray(r.reviews_all)) { latestReviewsAll = r.reviews_all; renderProjects(r.reviews_all); }
  if (r.mode && r.mode.mode) document.getElementById("modeText").textContent = "Režim: " + ({basic: "Základní", intelligent: "Inteligentní", autonomous: "Autonomní"}[r.mode.mode] || r.mode.mode);
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

// 403 = přihlášení na tomto serveru neplatí (např. po jeho restartu): říct to jasně, ne jen chybou v konzoli.
const rawFetch = window.fetch.bind(window);
let sessionLostShown = false;
window.fetch = (...args) => rawFetch(...args).then(r => {
  if (r.status === 403 && !String(args[0]).startsWith("/session") && !sessionLostShown) { sessionLostShown = true; showSessionLost(); }
  return r;
});
function showSessionLost() {
  updateStatus("retry");
  const bar = document.createElement("div"); bar.className = "session-lost"; bar.setAttribute("role", "alert");
  bar.innerHTML = "Přihlášení k webu vypršelo (server se mezitím restartoval). Spusť v terminálu <b>agent-lease web --open</b> a pokračuj v nově otevřeném okně.";
  document.body.prepend(bar);
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
    loadDashData();
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
const KB_LABEL = {verified:"ověřeno",unverified:"neověřeno — jen tip",rejected:"zamítnuto",stale:"zastaralé"};
let kbTimer = null;
function loadKnowledge() {
  const q = document.getElementById("kbQuery").value.trim(), st = document.getElementById("kbStatus").value;
  const params = new URLSearchParams({...(q ? {q} : {}), ...(st ? {status:st} : {})});
  fetch("/api/knowledge?" + params).then(r => r.ok ? r.json() : Promise.reject(r))
    .then(d => renderKnowledge(d.entries))
    .catch(() => { document.getElementById("kbList").textContent = "Paměť se nepodařilo načíst."; });
}
function renderKnowledge(entries) {
  const el = document.getElementById("kbList");
  if (!entries.length) { el.innerHTML = '<p class="note">Žádné záznamy.</p>'; return; }
  el.innerHTML = entries.map(e => `<article class="kb-entry" data-id="${e.id}"><div class="kb-actions"><strong>#${e.id}</strong><span class="kb-state ${esc(e.status)}">${esc(KB_LABEL[e.status] || e.status)}</span><span class="note">${esc(e.project || "obecné")} · autor ${esc(e.author)}${e.verified_by ? " · posoudil " + esc(e.verified_by) : ""} · ${new Date(e.updated_at * 1000).toLocaleString("cs-CZ")}</span></div>
    <dl><dt>Příznak</dt><dd>${esc(e.symptom)}</dd>${e.cause ? `<dt>Příčina</dt><dd>${esc(e.cause)}</dd>` : ""}${e.solution ? `<dt>Řešení</dt><dd>${esc(e.solution)}</dd>` : ""}${e.evidence ? `<dt>Důkaz</dt><dd>${esc(e.evidence)}</dd>` : ""}${e.verdict_evidence ? `<dt>Důkaz posudku</dt><dd>${esc(e.verdict_evidence)}${e.verdict_note ? " — " + esc(e.verdict_note) : ""}</dd>` : ""}</dl>
    <div class="kb-actions"><button class="btn" data-kb="verified">✓ Ověřit</button><button class="btn" data-kb="rejected">✕ Nefunguje</button><button class="btn" data-kb="stale">◷ Zastaralé</button></div></article>`).join("");
  el.querySelectorAll("[data-kb]").forEach(b => b.onclick = () => {
    const id = Number(b.closest(".kb-entry").dataset.id);
    const evidence = prompt("Důkaz (commit, test, odkaz) — povinné:");
    if (!evidence || !evidence.trim()) return;
    b.disabled = true;
    post("/api/knowledge-verdict", {id, verdict: b.dataset.kb, evidence}).then(ok => { b.disabled = false; if (ok) loadKnowledge(); });
  });
}
document.getElementById("kbQuery").addEventListener("input", () => { clearTimeout(kbTimer); kbTimer = setTimeout(loadKnowledge, 250); });
document.getElementById("kbStatus").addEventListener("change", loadKnowledge);
document.getElementById("kbAdd").onclick = () => {
  const val = id => document.getElementById(id).value;
  if (!val("kbSymptom").trim()) { alert("Vyplň příznak."); return; }
  const btn = document.getElementById("kbAdd"); btn.disabled = true;
  post("/api/knowledge-add", {symptom: val("kbSymptom"), cause: val("kbCause"), solution: val("kbSolution"), project: val("kbProject"), evidence: val("kbEvidence")})
    .then(ok => { btn.disabled = false; if (ok) { ["kbSymptom","kbCause","kbSolution","kbEvidence"].forEach(i => document.getElementById(i).value = ""); loadKnowledge(); } });
};
const MODES = [
  {id:"basic", name:"🟢 Základní", items:["Bridge sám nepřebírá žádné úkoly","Pracuje jen session, kterou spustíš","Sdílená paměť vypnutá pro agenty","Minimální spotřeba tokenů"]},
  {id:"intelligent", name:"🔵 Inteligentní", items:["Bridge přebírá read-only úkoly","Review mezi agenty (max 3 kola)","Sdílená paměť zapnutá","Dnešní chování"]},
  {id:"autonomous", name:"🟣 Autonomní", items:["Opravy v izolovaném worktree","Povinné review, rozpočtové stropy","Eskalace na tebe po 3 pokusech"]},
];
function renderMode(m) {
  if (!m) return;
  const grid = document.getElementById("modeGrid");
  grid.innerHTML = MODES.map(x => { const lock = m.locked && m.locked[x.id]; const on = m.mode === x.id;
    return `<button type="button" class="mode-card" role="radio" data-mode="${x.id}" aria-checked="${on}" aria-disabled="${!!lock}"><h3>${x.name}${on ? " · aktivní" : ""}</h3><ul>${x.items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>${lock ? `<p class="note">🔒 ${esc(lock)}</p>` : ""}</button>`; }).join("");
  document.getElementById("modeInfo").textContent = m.by ? `Naposledy přepnul ${m.by} · ${new Date(m.at * 1000).toLocaleString("cs-CZ")}${m.reason ? " · " + m.reason : ""}` : "Výchozí režim (zatím nikdo nepřepínal).";
  grid.querySelectorAll("[data-mode]").forEach(b => b.onclick = () => {
    if (b.getAttribute("aria-disabled") === "true" || b.getAttribute("aria-checked") === "true") return;
    const reason = prompt("Proč přepínáš režim? (zapíše se do historie)");
    if (reason === null) return;
    fetch("/api/mode", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({mode:b.dataset.mode, reason})})
      .then(r => r.ok ? r.json().then(renderMode) : r.text().then(t => alert(t)))
      .catch(() => alert("Spojení se nezdařilo. Zkontroluj režim před opakováním."));
  });
}
function loadMode() { fetch("/api/snapshot?since=" + Number.MAX_SAFE_INTEGER).then(r => r.json()).then(s => renderMode(s.mode)).catch(() => {}); }
const RS_LABEL = {running:"běží",synthesizing:"syntéza",done:"hotovo",partial:"částečně",failed:"selhalo",cancelled:"zrušeno"};
function loadResearch() {
  fetch("/api/research").then(r => r.ok ? r.json() : Promise.reject(r)).then(d => renderResearch(d.research))
    .catch(() => { document.getElementById("rsList").textContent = "Výzkum se nepodařilo načíst."; });
}
function renderResearch(runs) {
  const el = document.getElementById("rsList");
  if (!runs.length) { el.innerHTML = '<p class="note">Zatím žádný výzkum. Zadává ho agent nástrojem <code>research</code>; bez <code>research.toml</code> s mezí spotřeby se nespustí.</p>'; return; }
  el.innerHTML = runs.map(r => `<article class="kb-entry" data-id="${r.id}"><div class="kb-actions"><strong>#${r.id}</strong><span class="kb-state ${r.state === "done" ? "verified" : r.state === "running" || r.state === "synthesizing" ? "unverified" : "stale"}">${esc(RS_LABEL[r.state] || r.state)}</span><span class="note">${esc(r.kind)} · zadal ${esc(r.requester)} · ${esc(String(r.spend_tokens))}/${esc(String(r.budget_tokens))} tokenů${r.reason ? " · " + esc(r.reason) : ""}</span></div>
    <dl><dt>Otázka</dt><dd>${esc(r.question)}</dd><dt>Části</dt><dd>${r.parts.filter(p => p.role === "part").map(p => `${esc(p.topic)} (${esc(p.state || "?")})`).join(" · ")}</dd>${r.summary ? `<dt>Souhrn (neověřený)</dt><dd style="white-space:pre-wrap">${esc(r.summary)}</dd>` : ""}</dl>
    ${r.state === "running" || r.state === "synthesizing" ? '<div class="kb-actions"><button class="btn" data-rs-cancel>✕ Zrušit</button></div>' : ""}</article>`).join("");
  el.querySelectorAll("[data-rs-cancel]").forEach(b => b.onclick = () => {
    if (!confirm("Zrušit výzkum včetně běžících částí a syntézy?")) return;
    b.disabled = true;
    post("/api/research-cancel", {id: Number(b.closest(".kb-entry").dataset.id)}).then(() => loadResearch());
  });
}
function showView(view,{focus=true}={}) {
  if(view==="settings")loadMode();
  if(view==="research")loadResearch();
  if(view==="memory")loadKnowledge();
  if(view==="overview")loadDashData();
  if(drawer.open){drawer.close();restorePanels();}
  currentView=view;
  const panelView=view==="room"?"overview":view;   // Místnost agentů = stejná místnost přes celou plochu
  document.body.classList.toggle("room-focus",view==="room");
  document.querySelectorAll("[data-view-panel]").forEach(el=>{el.hidden=el.dataset.viewPanel!==panelView;});
  document.querySelectorAll(".primary-nav [data-view]").forEach(b=>{const on=b.dataset.view===view;b.classList.toggle("current",on);if(on)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current");});
  if(view==="room"){if(focus)document.getElementById("text").focus({preventScroll:true});feed.scrollTop=feed.scrollHeight;}
  else if(focus)document.querySelector(`[data-view-panel="${panelView}"] .view-title`)?.focus({preventScroll:true});
  motion(document.querySelector(`[data-view-panel="${panelView}"]`),[{opacity:.5},{opacity:1}],180);
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
