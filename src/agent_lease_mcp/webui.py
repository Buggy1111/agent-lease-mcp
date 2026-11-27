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
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock, Thread
from urllib.parse import parse_qs, urlsplit

from .config import HUMAN_AGENT, HUMAN_NAME_FILE, Settings, human_name
from .models import MessageKind
from .passkey import PasskeyError, Passkeys
from .store import Store

# Statické soubory UI (portréty Ady a Noxe, ikona). Pevný seznam: žádné cesty z požadavku.
_ASSET_DIR = Path(__file__).with_name("assets")
# Šum v historii: stav poskytovatele se zapisuje každou minutu (desítky tisíc řádků).
HISTORY_NOISE = ("provider-state",)
_PROJECT_CACHE: dict[str, str] = {}


def project_of(repo: str) -> str:
    """Název projektu pro repo/worktree: pracovní kopie patří pod svůj hlavní repozitář."""
    if repo in _PROJECT_CACHE:
        return _PROJECT_CACHE[repo]
    path, name = Path(repo), Path(repo).name or repo
    try:
        common = subprocess.run(["git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        name = Path(common).parent.name if common.endswith("/.git") else name
    except (OSError, subprocess.SubprocessError):
        # Smazané/přesunuté pracovní kopie Agent Lease (dev/work/agent-lease-*, nox-*) patří pod hlavní repo.
        parts = path.parts
        if "work" in parts and parts.index("work") + 1 < len(parts):
            first = parts[parts.index("work") + 1]
            name = "agent-lease-mcp" if first.startswith(("agent-lease", "nox-")) else first
        elif ".claude" in parts:
            name = parts[parts.index(".claude") - 1]
    _PROJECT_CACHE[repo] = name
    return name


ASSETS = {"/assets/ada.webp": ("ada.webp", "image/webp"), "/assets/nox.webp": ("nox.webp", "image/webp"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml"), "/assets/wave.svg": ("wave.svg", "image/svg+xml"), "/favicon.ico": ("favicon.svg", "image/svg+xml")}

POLL_SECONDS = 0.7
LIVE_POLL_SECONDS = 0.25   # jak rychle poznáme zápis do DB (stat souboru, bez dotazu)
ROOM_SECONDS = 5.0         # i bez změny: obnoví „před N s“, stáří dat a limity
SESSION_COOKIE = "agent_lease_session"


def session_cookie_name(port: int) -> str:
    """Cookies prohlížeč neodděluje podle portu: dva weby na jednom stroji (8765 a náhled) by si přihlášení
    přepisovaly (21. 11.). Výchozí port si jméno ponechá, ať stávající přihlášení dál platí."""
    return SESSION_COOKIE if port == 8765 else f"{SESSION_COOKIE}_{port}"
SESSION_MAX_AGE_SECONDS = 7 * 86400   # web běží trvale jako služba; přihlášení stačí jednou týdně (Michal 26. 11.)
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


LOGIN_CODE_FILE = "webui.login"
LOGIN_CODE_SECONDS = 120


def issue_login_code(settings: Settings) -> str:
    """Jednorázový kód na 2 min pro odkaz do prohlížeče: hlavní token se tak nedostane do URL ani historie."""
    code = secrets.token_urlsafe(32)
    path = settings.db_path.parent / LOGIN_CODE_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{LOGIN_CODE_FILE}.{secrets.token_hex(8)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{code} {time.time() + LOGIN_CODE_SECONDS:.0f}")
    os.replace(tmp, path)   # platí jen poslední vydaný kód
    return code


def consume_login_code(settings: Settings, code: str) -> bool:
    path = settings.db_path.parent / LOGIN_CODE_FILE
    try:
        stored, expires = path.read_text().split()
    except (OSError, ValueError):
        return False
    if not code or not secrets.compare_digest(code, stored) or time.time() > float(expires):
        return False
    try:
        path.unlink()           # jednorázový: kdo kód smaže první, ten se přihlásí
    except FileNotFoundError:
        return False
    return True


REQUEST_READ_DEADLINE = 20.0
MAX_CONNECTIONS = 64


class _DeadlineReader:
    """Obal rfile: čtení po vypršení `deadline` skončí chybou (pomalý klient = slowloris)."""

    def __init__(self, raw) -> None:
        self._raw, self.deadline = raw, float("inf")

    def _check(self) -> None:
        if time.monotonic() > self.deadline:
            raise TimeoutError("request read deadline")

    def read(self, *a):
        self._check()
        return self._raw.read(*a)

    def readline(self, *a):
        self._check()
        return self._raw.readline(*a)

    def readinto(self, b):
        self._check()
        return self._raw.readinto(b)

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _Reaper:
    """Jediné vlákno hlídá termíny požadavků a po vypršení spojení zavře.

    Časový limit socketu platí na jedno `recv`; kdo kape po bajtu uvnitř jednoho řádku hlavičky,
    by ho obcházel donekonečna. Zavření socketu zvenku přeruší i blokující čtení.
    """

    def __init__(self, period: float = 1.0) -> None:
        self._deadlines: dict = {}
        self._lock = Lock()
        self._period = period
        self._thread = None

    def watch(self, conn, deadline: float) -> None:
        with self._lock:
            self._deadlines[conn] = deadline
            if self._thread is None:
                self._thread = Thread(target=self._run, name="agent-lease-web-reaper", daemon=True)
                self._thread.start()

    def release(self, conn) -> None:
        with self._lock:
            self._deadlines.pop(conn, None)

    def reap(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            late = [c for c, d in self._deadlines.items() if d <= now]
            for c in late:
                del self._deadlines[c]
        for c in late:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return len(late)

    def _run(self) -> None:
        while True:
            time.sleep(self._period)
            self.reap()


class _ConnectionCap:
    """Strop souběžných spojení před vytvořením vlákna (ThreadingHTTPServer jinak neomezuje)."""

    max_connections = MAX_CONNECTIONS

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._slots = BoundedSemaphore(self.max_connections)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            return self.shutdown_request(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def db_version(path: Path) -> int:
    try:
        st = os.stat(path)
    except OSError:
        return 0
    return st.st_mtime_ns ^ st.st_size


def usage_report(store: Store, settings: Settings) -> dict:
    """Spotřeba tokenů pro web: 24 h a 7 dní po agentech, stropy z nastavení, poslední běhy."""
    now = time.time()
    agents = {}
    for agent in ("claude-code", "codex"):
        agents[agent] = {"day": store.usage_summary(since=now - 86400, recipient=agent),
                         "week": store.usage_summary(since=now - 7 * 86400, recipient=agent)}
    return {"agents": agents, "recent": store.usage_recent(limit=30),
            "caps": {"daily_tokens": settings.daily_budget_tokens, "daily_usd": settings.daily_budget_usd,
                     "task_tokens": settings.task_budget_tokens, "task_usd": settings.task_budget_usd}}


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
    # Cookie prohlížeč pošle i na jiný port localhostu (audit 26. 11.). Druhý klíč žije v localStorage,
    # které je oddělené podle původu (včetně portu), a chodí v hlavičce / parametru `k`.
    session_keys: dict[str, str] = {}
    passkeys = Passkeys(settings.db_path.parent, allowed_origins)
    reaper = _Reaper()
    sessions_lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "agent-lease-webui/0.1"

        def setup(self) -> None:
            super().setup()
            # Platí už při parsování requestu, takže pomalý lokální klient
            # nemůže držet vlákno navždy ještě před vstupem do handleru.
            self.connection.settimeout(REQUEST_SOCKET_TIMEOUT)
            self.rfile = _DeadlineReader(self.rfile)

        def handle_one_request(self) -> None:
            # Absolutní limit na celý požadavek (hlavičky, tělo, odpověď); SSE se z hlídání vyjme sám.
            deadline = time.monotonic() + REQUEST_READ_DEADLINE
            self.rfile.deadline = deadline
            reaper.watch(self.connection, deadline)
            try:
                super().handle_one_request()
            finally:
                reaper.release(self.connection)

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

        def _session_id(self) -> str:
            try:
                return SimpleCookie(self.headers.get("Cookie", ""))[cookie_name].value
            except (CookieError, KeyError):
                return ""

        def _session_ok(self) -> bool:
            value = self._session_id()
            key = self.headers.get("X-Agent-Lease-Key") or parse_qs(urlsplit(self.path).query).get("k", [""])[0]
            now = time.monotonic()
            with sessions_lock:
                expired = [session_id for session_id, deadline in sessions.items() if deadline <= now]
                for session_id in expired:
                    del sessions[session_id]
                    session_keys.pop(session_id, None)
                stored = session_keys.get(value, "")
                return (bool(value) and sessions.get(value, 0) > now and bool(key) and bool(stored)
                        and secrets.compare_digest(key, stored))

        def _session_alive(self, session_id: str) -> bool:
            with sessions_lock:
                return sessions.get(session_id, 0) > time.monotonic()

        def _origin_ok(self) -> bool:
            # Prohlížeč posílá Origin u každého POST z fetch; bez něj nebo cizí = odmítnout.
            return self.headers.get("Origin") in allowed_origins

        def _host_ok(self) -> bool:
            # Obrana proti DNS rebindingu: cizí doména přesměrovaná na 127.0.0.1 má jiný Host.
            return self.headers.get("Host") in allowed_hosts

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
            if not self._host_ok():
                return self._reject(403, "bad host")
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
            if parts.path in ("/api/usage", "/api/history", "/api/catalog"):
                if not self._session_ok():
                    return self._reject(403, "bad token")
                if parts.path == "/api/catalog":
                    return self._handle_asset("catalog.json", "application/json")
                if parts.path == "/api/usage":
                    return self._json_response(usage_report(store, settings))
                try:
                    limit = max(1, min(int(qs.get("limit", ["200"])[0]), 500))
                    before = int(qs.get("before", ["0"])[0])
                except ValueError:
                    return self._reject(400, "limit/before must be integers")
                return self._json_response({"events": store.audit_log(
                    limit=limit, before_id=before, action=qs.get("action", [""])[0][:64],
                    agent=qs.get("agent", [""])[0][:64], q=qs.get("q", [""])[0][:200],
                    exclude=() if qs.get("all", [""])[0] == "1" else HISTORY_NOISE)})
            if parts.path == "/api/passkey":
                if not self._session_ok():
                    return self._reject(403, "bad token")
                return self._json_response({"keys": passkeys.public_list()})
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
            if not self._host_ok():
                return self._reject(403, "bad host")
            parts = urlsplit(self.path)
            if parts.path == "/session":
                return self._handle_session()
            if parts.path in ("/session/passkey-options", "/session/passkey"):
                return self._handle_passkey_login(parts.path.endswith("options"))
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
            if parts.path in ("/api/passkey/register-options", "/api/passkey/register", "/api/passkey/remove"):
                return self._handle_passkey_manage(parts.path.rsplit("/", 1)[1])
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
            nonce = secrets.token_urlsafe(16)
            body = PAGE_HTML.replace("<script>", f'<script nonce="{nonce}">', 1).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._security_headers()
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'self'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
                "connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'",
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
            if not (self._bearer_ok() or consume_login_code(settings, self.headers.get("X-Agent-Lease-Token", ""))):
                return self._reject(403, "bad token")
            self._issue_session()

        def _issue_session(self) -> None:
            session_id, session_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with sessions_lock:
                sessions[session_id] = time.monotonic() + SESSION_MAX_AGE_SECONDS
                session_keys[session_id] = session_key
            self.send_response(204)
            self._security_headers()
            self.send_header("X-Agent-Lease-Key", session_key)
            self.send_header(
                "Set-Cookie",
                f"{cookie_name}={session_id}; HttpOnly; SameSite=Strict; Path=/; "
                f"Max-Age={SESSION_MAX_AGE_SECONDS}",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _handle_passkey_login(self, options: bool) -> None:
            """Přihlášení Windows Hello (PIN): bez session, ale jen z povoleného původu a s omezením pokusů."""
            if (self.headers.get("Origin") not in allowed_origins
                    or self.headers.get("Host") not in allowed_hosts):
                return self._reject(403, "bad origin")
            if passkeys.blocked():
                return self._reject(429, "too many failed attempts; wait 5 minutes or use the desktop icon")
            data = self._read_json()
            if data is None:
                return
            try:
                if options:
                    return self._json_response(passkeys.login_options())
                key = passkeys.login(data)
            except PasskeyError as exc:
                passkeys.failed()
                store.record(web_agent, "web-login-passkey-failed", "", str(exc)[:200])
                return self._reject(403, "Windows Hello login failed")
            store.record(web_agent, "web-login-passkey", "", key["name"])
            self._issue_session()

        def _handle_passkey_manage(self, action: str) -> None:
            data = self._read_json()
            if data is None:
                return
            try:
                if action == "register-options":
                    return self._json_response(passkeys.register_options(web_agent))
                if action == "register":
                    key = passkeys.register(data)
                    store.record(web_agent, "web-passkey-added", "", key["name"])
                    return self._json_response(key)
                if not passkeys.remove(str(data.get("id", ""))):
                    return self._reject(404, "no such key")
                store.record(web_agent, "web-passkey-removed", "", str(data.get("id", ""))[:80])
                return self._json_response({"removed": True})
            except PasskeyError as exc:
                return self._reject(400, str(exc))

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
                "projects": {},
                "quality_runs": store.quality_runs(limit=30),
                "quality_requests": store.quality_requests(limit=30),
            }
            payload["projects"] = {r["repo"]: project_of(r["repo"]) for r in payload["reviews_all"] if r.get("repo")}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = -1
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
                stream_session = self._session_id()
                reaper.release(getattr(self, "connection", None))   # stream je dlouhý záměrně; hlídá ho platnost přihlášení
                last_version = None
                while self._session_alive(stream_session):   # po vypršení přihlášení stream skončí
                    # Skutečně živě (Michal 27. 11.): každý zápis do DB mění soubor (journal DELETE),
                    # takže změnu poznáme hned a pošleme ji; bez změny jen levná kontrola času souboru.
                    version = db_version(settings.db_path)
                    changed = version != last_version
                    last_version = version
                    messages = store.inbox(since_id=since, limit=200) if changed else []
                    for message in messages:
                        self.wfile.write(_sse("message", message["id"], message))
                        since = max(since, message["id"])
                    if messages:
                        self.wfile.flush()

                    now = time.monotonic()
                    if changed or now >= next_room_at:
                        room = {
                            "version": version,
                            "peers": store.peers(),
                            "claims": [c.as_dict() for c in store.claims()],
                            "board": store.board(),
                            # Bez stavu pauzy by živá aktualizace přepsala tlačítko
                            # „Obnovit" na „Zastavit" a člověk by stop zapnul znovu.
                            "paused": store.paused(),
                            "reviews": store.reviews(limit=30),
                            "mode": store.mode(),
                            "reviews_all": store.reviews(limit=200, include_closed=True),   # Přehled: postup i u uzavřených
                            "quality_runs": store.quality_runs(limit=30),
                            "quality_requests": store.quality_requests(limit=30),
                            "messages": store.latest_messages(limit=200),
                        }
                        room["projects"] = {r["repo"]: project_of(r["repo"]) for r in room["reviews_all"] if r.get("repo")}
                        self.wfile.write(_sse("room", None, room))
                        self.wfile.flush()
                        next_room_at = now + ROOM_SECONDS

                    if now >= next_heartbeat_at:
                        self.wfile.write(_sse_heartbeat())
                        self.wfile.flush()
                        next_heartbeat_at = now + SSE_HEARTBEAT_SECONDS

                    time.sleep(LIVE_POLL_SECONDS)
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                return  # klient zavřel kartu nebo se prohlížeč odpojil
            finally:
                sse_slots.release()

    Handler.sse_slots = sse_slots
    Handler.sessions = sessions
    Handler.session_keys = session_keys
    Handler.passkeys = passkeys
    Handler.reaper = reaper
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
/* ── Hlavička, navigace a metriky 1:1 podle Michalova výřezu (21. 11. 07:01) ── */
#masthead { grid-template-columns:auto 1fr auto; grid-template-areas:"brand heroes tools" "nav nav nav"; padding:10px 0 0; gap:6px 18px; }
#pause { grid-area:tools; align-self:end; justify-self:end; margin-top:46px; }
#brand .mark,#brand .mark svg { width:84px; height:44px; }
#brand .name { font-size:38px; letter-spacing:-.6px; } #brand .sub { font-size:16px; color:#C9D2EA; }
#agentHeroes { gap:24px; align-items:stretch; }
.agent-hero { position:relative; width:415px; height:120px; min-height:0; margin-top:34px; padding:8px 14px 8px 150px; border-radius:18px; overflow:visible; }
.agent-info > * { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%; }
.agent-hero[data-agent="nox"] { flex-direction:row; padding:8px 150px 8px 16px; }
.portrait-wrap { position:absolute; left:-8px; bottom:0; width:165px; height:168px; margin:0; }
.agent-hero[data-agent="nox"] .portrait-wrap { left:auto; right:-8px; }
.portrait { position:absolute; bottom:0; left:0; width:165px; height:168px; margin:0; object-fit:contain; object-position:bottom; border-radius:0; filter:drop-shadow(0 0 14px color-mix(in srgb,var(--c) 70%,transparent)); }
.halo { inset:auto; left:10px; bottom:-6px; width:140px; height:120px; border-radius:50%; background:radial-gradient(circle at 50% 60%,color-mix(in srgb,var(--c) 70%,transparent),transparent 68%); opacity:.55; }
.agent-info { display:flex; flex-direction:column; justify-content:center; }
.agent-title { flex-direction:column; align-items:flex-start; gap:0; }
.agent-name { font-size:30px; line-height:1.05; display:flex; align-items:center; gap:10px; }
.agent-hero .vendor-mark { display:inline-block; flex:none; width:26px; min-width:26px; height:22px; color:#F5F7FF; }
.agent-vendor { font-size:15px; color:#E3E8F7; margin-top:1px; } .agent-role { font-size:13.5px; color:#C9D2EA; letter-spacing:-.1px; }
.agent-name { font-size:30px; }
.agent-state { font-size:16px; margin-top:4px; } .pulse-line { width:78px; height:22px; margin-left:12px; flex:none; }
.agent-task { display:none; }
.hub { width:118px; height:118px; margin-top:6px; align-self:center; border:2px solid transparent; background:radial-gradient(circle,rgba(30,20,70,.95),rgba(7,13,32,.98) 66%) padding-box,linear-gradient(135deg,#04CFFF,#923BFF 60%,#C04BFF) border-box; box-shadow:0 0 34px rgba(146,59,255,.55),0 0 60px rgba(4,207,255,.18); }
.hub svg { width:74px; height:37px; }
#headTools { gap:12px; align-self:start; margin-top:20px; }
.mode-badge { padding:9px 18px; font-size:15px; border-radius:14px; background:rgba(20,26,60,.85); }
.icon-btn { width:44px; height:44px; border:0; background:transparent; } .icon-btn .nav-ic { width:26px; height:26px; }
.bell-count { top:0; right:2px; }
.human-badge { width:42px; height:42px; font-size:18px; background:linear-gradient(135deg,#7E30E8,#5F1FCC); }
/* bezpečnostní stop jako malé tlačítko vpravo (v předloze není, ale nesmí zmizet) */
#pause .pausebar { gap:10px; } #pause .big { font-size:13px; }
#pause .pausebar button { min-height:32px; padding:4px 12px; font-size:12px; border-radius:9px; }
/* navigace přes celou šířku se svítící vlnou */
.primary-nav { justify-content:space-between; padding:14px 0 22px; margin-top:4px; }
.primary-nav button,.primary-nav a { font-size:19px; padding:10px 30px; min-height:50px; gap:12px; border-radius:12px; }
.nav-ic { width:26px; height:26px; }
.primary-nav::after { bottom:6px; height:2px; background:linear-gradient(90deg,transparent 2%,rgba(146,59,255,.9) 25%,rgba(4,207,255,.75) 60%,rgba(146,59,255,.95) 85%,transparent 99%); }
.primary-nav::before { content:""; position:absolute; left:10%; right:0; bottom:0; height:26px; pointer-events:none;
  background:radial-gradient(60% 100% at 78% 100%,rgba(146,59,255,.45),transparent 70%),radial-gradient(40% 80% at 40% 100%,rgba(4,207,255,.18),transparent 70%); }
/* metriky jako na předloze */
#metrics { gap:18px; }
.metric { min-height:88px; padding:16px 18px; border-radius:14px; gap:14px; }
.metric-icon { width:56px; height:56px; } .metric-icon svg { width:50px; height:50px; stroke-width:1.6; }
.metric > div { display:flex; flex-direction:column; gap:2px; }
.metric-label { font-size:16px; color:#F1F4FF; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; } .metric-value { font-size:30px; font-weight:700; }
.metric > div { min-width:0; }
@media(min-width:1100px) and (max-height:900px) {
  .agent-hero { height:104px; width:360px; margin-top:26px; padding-left:132px; } .agent-hero[data-agent="nox"] { padding-right:132px; padding-left:14px; }
  .portrait-wrap,.portrait { width:142px; height:146px; } .agent-name { font-size:26px; } .agent-role { display:block; font-size:13px; }
  .hub { width:96px; height:96px; } .hub svg { width:60px; height:30px; } #brand .name { font-size:32px; }
  .primary-nav { padding:8px 0 16px; } .primary-nav button { font-size:17px; padding:8px 22px; min-height:44px; }
  .metric { min-height:72px; padding:10px 16px; } .metric-icon,.metric-icon svg { width:42px; height:42px; } .metric-label { font-size:15px; } .metric-value { font-size:26px; }
}
@media(max-width:1280px) { #masthead { grid-template-columns:minmax(0,1fr) auto; grid-template-areas:"brand tools" "heroes heroes" "nav nav" "pause pause"; } #pause { grid-area:pause; justify-self:start; margin-top:0; } .agent-hero { width:min(380px,100%); } }
@media(max-width:899px) { .agent-hero,.agent-hero[data-agent="nox"] { padding:10px 14px 10px 120px; } .agent-hero[data-agent="nox"] .portrait-wrap { right:auto; left:-6px; } .portrait-wrap,.portrait { width:120px; height:116px; } .hub { display:none; } .primary-nav { justify-content:flex-start; } .primary-nav button { font-size:15px; padding:8px 14px; } }
/* ── 2. kolo porovnání s výřezem 1:1 (jen desktop; mobil má vlastní hlavičku) ── */
@media(min-width:900px) {
#masthead { padding-top:0; }
.agent-hero { height:122px; margin-top:12px; border:2px solid color-mix(in srgb,var(--c) 90%,white 0%); box-shadow:0 0 22px color-mix(in srgb,var(--c) 55%,transparent),0 0 50px color-mix(in srgb,var(--c) 22%,transparent),inset 0 0 26px color-mix(in srgb,var(--c) 14%,transparent); background:linear-gradient(100deg,color-mix(in srgb,var(--c) 34%,#0B1230),rgba(8,13,34,.97) 58%); padding-left:168px; }
.agent-hero[data-agent="nox"] { background:linear-gradient(260deg,color-mix(in srgb,var(--c) 34%,#0B1230),rgba(8,13,34,.97) 58%); padding-right:168px; padding-left:18px; }
.portrait-wrap,.portrait { width:190px; height:182px; }
.portrait-wrap { left:-14px; } .agent-hero[data-agent="nox"] .portrait-wrap { right:-14px; }
.portrait { filter:drop-shadow(0 0 16px color-mix(in srgb,var(--c) 80%,transparent)) drop-shadow(0 0 4px color-mix(in srgb,var(--c) 60%,transparent)); }
.halo { width:170px; height:140px; left:6px; opacity:.7; }
.agent-name { font-size:31px; } .agent-hero .vendor-mark { width:30px; min-width:30px; height:26px; }
.agent-vendor { font-size:17px; color:#F1F4FF; } .agent-role { font-size:15.5px; color:#DCE3F5; }
.agent-state { font-size:17px; margin-top:6px; } .state-dot { width:13px; height:13px; }
.agent-hero[data-state="limited"],.agent-hero[data-state="offline"],.agent-hero[data-state="unknown"] { filter:saturate(.6); }
.hub { width:112px; height:112px; border-width:3px; box-shadow:0 0 30px rgba(146,59,255,.75),0 0 70px rgba(4,207,255,.25),inset 0 0 26px rgba(146,59,255,.35); }
.hub svg { width:78px; height:40px; filter:drop-shadow(0 0 10px rgba(4,207,255,.7)); }
#brand .mark,#brand .mark svg { width:92px; height:48px; } #brand .name { font-size:38px; }
.mode-badge { font-size:15px; padding:10px 18px; border:2px solid rgba(146,59,255,.7); }
.icon-btn .nav-ic { width:30px; height:30px; }
/* navigace: položky za sebou s pravidelným rozestupem, svítící vlna */
.primary-nav { justify-content:flex-start; gap:34px; padding:10px 0 26px; }
.primary-nav button,.primary-nav a { font-size:20px; padding:10px 26px; min-height:52px; }
.primary-nav .current { min-width:200px; justify-content:center; background:linear-gradient(135deg,#8A3BF0,#6A24D8); }
.nav-ic { width:30px; height:30px; }
.primary-nav::after { display:none; }
.primary-nav::before { left:0; right:-24px; bottom:-4px; height:46px; background:none;
  background-image:url("/assets/wave.svg"); background-size:100% 100%; background-repeat:no-repeat; }
/* metriky: jasnější okraje a záře */
.metric { border-width:2px!important; border-color:color-mix(in srgb,var(--t) 85%,transparent)!important; box-shadow:0 0 20px color-mix(in srgb,var(--t) 45%,transparent),inset 0 0 22px color-mix(in srgb,var(--t) 16%,transparent); }
.metric-label { font-size:17px; font-weight:500; }
@media(min-width:1100px) and (max-height:900px) {
  .agent-hero { height:106px; width:380px; padding-left:146px; } .agent-hero[data-agent="nox"] { padding-right:146px; }
  .portrait-wrap,.portrait { width:162px; height:158px; } .agent-vendor { font-size:15px; } .agent-role { font-size:14px; } .agent-state { font-size:15px; }
  .hub { width:96px; height:96px; } .primary-nav { gap:20px; } .primary-nav button { font-size:17px; padding:8px 20px; min-height:44px; } .nav-ic { width:24px; height:24px; }
}
}
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
/* ── Stránky Kvalita, Výzkum, Paměť, Nastavení podle předloh 01/03/04/06 (Michal 26. 11.) ── */
i[data-ic] { display:inline-grid; place-items:center; flex:none; font-style:normal; }
i[data-ic] svg { width:22px; height:22px; }
.panel-title i[data-ic] { color:var(--cyan); filter:drop-shadow(0 0 6px rgba(4,207,255,.6)); }
.pg-page { display:flex; flex-direction:column; gap:14px; padding-top:14px; }
.metrics-row { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:18px; }
.metrics-row .metric-value { font-size:26px; overflow-wrap:anywhere; } .metrics-row .metric-value.small { font-size:18px; margin-top:2px; }
.pg { display:grid; gap:14px; grid-template-columns:minmax(0,1.05fr) minmax(0,1.6fr) minmax(0,1.05fr) minmax(0,.95fr); }
.pg > .panel { min-height:0; overflow:hidden; display:flex; flex-direction:column; }
.pg-list { grid-area:list; } .pg-main { grid-area:main; } .pg-side { grid-area:side; } .pg-side2 { grid-area:side2; } .pg-act { grid-area:act; }
.pg-wide { grid-area:wide; } .pg-search { grid-area:search; } .pg-results { grid-area:results; } .pg-stop { grid-area:stop; } .pg-sec { grid-area:sec; } .pg-login { grid-area:login; }
.pg-quality { grid-template-areas:"list main side act" "list wide wide act"; grid-template-rows:minmax(300px,auto) minmax(220px,auto); }
.pg-research { grid-template-areas:"list main side act" "list main side2 act"; grid-template-rows:minmax(260px,auto) minmax(200px,auto); }
.pg-memory { grid-template-areas:"list search side act" "list results side act"; grid-template-rows:auto minmax(380px,1fr); }
.pg-settings { grid-template-columns:minmax(0,.7fr) minmax(0,1.35fr) minmax(0,1.35fr) minmax(0,1fr); grid-template-areas:"list main side act" "list stop side act" "list sec login act"; }
.pg-scroll { overflow:auto; min-height:0; flex:1; }
.pg .activity { overflow:auto; min-height:0; flex:1; }
.h-sub { margin-left:auto; color:var(--text-dim); font-size:12.5px; text-align:right; }
.panel-title .h-sub + .btn-new, .panel-title h2 + .btn-new { margin-left:auto; }
.btn-new { display:inline-flex; align-items:center; gap:6px; min-height:34px; padding:6px 14px; border-radius:10px; border:0; background:var(--grad); color:#fff; font-size:14px; font-weight:600; box-shadow:0 0 14px rgba(146,59,255,.45); cursor:pointer; }
.btn-new i[data-ic] svg { width:16px; height:16px; }
.btn-new:hover { box-shadow:0 0 22px rgba(146,59,255,.7); }
/* seznam vlevo (výzkumy, kontroly, projekty) */
.pg-item { display:flex; gap:12px; width:100%; padding:12px 14px; margin-bottom:8px; border-radius:12px; border:1px solid var(--border-soft); background:rgba(16,27,56,.6); color:var(--text); text-align:left; cursor:pointer; font:inherit; }
.pg-item:hover { border-color:rgba(146,59,255,.55); }
.pg-item.on { border-color:rgba(255,138,24,.75); background:linear-gradient(120deg,rgba(255,138,24,.18),rgba(16,27,56,.7) 70%); box-shadow:0 0 16px rgba(255,138,24,.2); }
.pg-item .pg-ic { color:var(--text-dim); } .pg-item.on .pg-ic { color:var(--ada); } .pg-item .pg-ic svg { width:26px; height:26px; }
.pg-item-body { min-width:0; flex:1; display:flex; flex-direction:column; gap:3px; }
.pg-item-title { font-weight:650; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; overflow-wrap:anywhere; }
.pg-item-sub { font-size:12.5px; color:var(--text-dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pg-item-foot { display:flex; align-items:center; gap:8px; margin-top:4px; flex-wrap:wrap; }
.pg-item-foot .bar { flex:1; min-width:60px; margin:0; } .pg-item.on .bar i { background:linear-gradient(90deg,#FF7A00,#FF8A18); box-shadow:0 0 8px rgba(255,138,24,.6); }
.pg-item-foot b { font-variant-numeric:tabular-nums; font-size:13px; }
.pg-item .count-pill { margin-left:auto; flex:none; color:var(--text); font-weight:600; font-size:13px; white-space:nowrap; }
/* detail uprostřed */
.pg-head { display:flex; align-items:flex-start; gap:10px; padding:14px 16px 6px; }
.pg-head h2 { margin:0; font-size:18px; line-height:1.3; flex:1; min-width:0; overflow-wrap:anywhere; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
.pg-head i[data-ic] { color:var(--cyan); margin-top:2px; }
.pill { flex:none; padding:3px 12px; border-radius:999px; font-size:12.5px; border:1px solid rgba(146,59,255,.7); background:rgba(146,59,255,.22); color:#E4D2FF; white-space:nowrap; }
.pill.ok { border-color:rgba(0,231,121,.6); background:rgba(0,231,121,.14); color:#8AF7C2; } .pill.bad { border-color:rgba(255,90,110,.65); background:rgba(255,90,110,.14); color:var(--bad-text); } .pill.muted { border-color:var(--border); background:none; color:var(--text-dim); }
.pg-desc { margin:0; padding:0 16px; color:var(--text-dim); font-size:13.5px; }
.pg-progress { display:flex; align-items:center; gap:14px; margin:12px 16px 0; padding:12px 14px; border-radius:12px; background:rgba(16,27,56,.55); border:1px solid var(--border-soft); font-size:14px; }
.pg-progress .bar { flex:1; height:9px; margin:0; } .pg-progress .bar i { background:var(--grad); box-shadow:0 0 10px rgba(146,59,255,.7); }
.pg-progress b { font-size:16px; font-variant-numeric:tabular-nums; }
.stepper { display:flex; justify-content:space-between; position:relative; margin:14px 16px 4px; padding:0 4px; list-style:none; }
.stepper::before { content:""; position:absolute; left:8%; right:8%; top:14px; height:2px; background:rgba(120,150,255,.25); }
.stepper li { position:relative; z-index:1; flex:1; display:flex; flex-direction:column; align-items:center; gap:6px; font-size:12.5px; color:var(--text-dim); text-align:center; min-width:0; }
.stepper .node { width:28px; height:28px; border-radius:50%; display:grid; place-items:center; border:2px solid #56628A; background:#0B1230; color:#06130C; font-weight:800; font-size:14px; }
.stepper .done .node { background:var(--nox); border-color:var(--nox); box-shadow:0 0 10px rgba(0,231,121,.7); }
.stepper .now .node { border-color:var(--purple); box-shadow:0 0 12px rgba(146,59,255,.85),inset 0 0 6px rgba(146,59,255,.6); }
.stepper .fail .node { background:#FF5A6E; border-color:#FF5A6E; color:#fff; box-shadow:0 0 10px rgba(255,90,110,.7); }
.stepper .warn .node { background:#FFB020; border-color:#FFB020; color:#231500; }
.stepper .done,.stepper .now { color:var(--text); }
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin:12px 16px 0; padding:4px; border-radius:12px; background:rgba(16,27,56,.6); border:1px solid var(--border-soft); }
.tabs button { flex:1; min-height:34px; border:0; border-radius:9px; background:none; color:var(--text-dim); font-size:13.5px; cursor:pointer; }
.tabs button[aria-selected="true"] { background:var(--grad); color:#fff; box-shadow:0 0 12px rgba(146,59,255,.5); }
.tab-body { padding:10px 16px; overflow:auto; min-height:0; flex:1; font-size:14px; }
.tab-body .md p { margin:0 0 8px; } .tab-body .md h4 { margin:12px 0 6px; font-size:14px; color:var(--cyan); }
.tab-body .md li { margin:0 0 4px; } .tab-body .md ul { margin:0 0 8px; padding-left:20px; }
.tab-body code { font-size:12.5px; }
.conclusion { margin:4px 16px 14px; padding:12px 14px; border-radius:12px; border:1.5px solid rgba(0,231,121,.65); background:rgba(0,231,121,.07); box-shadow:0 0 18px rgba(0,231,121,.18); font-size:14px; display:flex; gap:10px; }
.conclusion i[data-ic] { color:var(--nox); } .conclusion b { color:#8AF7C2; display:block; }
.conclusion.warn { border-color:rgba(255,176,32,.7); background:rgba(255,176,32,.07); box-shadow:0 0 18px rgba(255,176,32,.15); } .conclusion.warn i[data-ic],.conclusion.warn b { color:#FFD27A; }
.pg-actions { display:flex; gap:8px; flex-wrap:wrap; padding:0 16px 14px; }
.kv { display:grid; grid-template-columns:auto 1fr; gap:6px 14px; margin:0; font-size:13.5px; } .kv dt { color:var(--text-dim); } .kv dd { margin:0; overflow-wrap:anywhere; }
/* odkazy, statistiky */
.link-row { display:flex; align-items:center; gap:10px; padding:9px 12px; margin-bottom:8px; border-radius:10px; border:1px solid var(--border-soft); background:rgba(16,27,56,.55); color:var(--text); text-decoration:none; font-size:13.5px; }
.link-row span { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .link-row:hover { border-color:var(--cyan); }
.stat-list { list-style:none; margin:0; padding:8px 16px 14px; display:flex; flex-direction:column; gap:4px; }
.stat-list li { display:flex; align-items:center; gap:10px; padding:7px 0; border-bottom:1px solid var(--border-soft); font-size:14px; } .stat-list li:last-child { border-bottom:0; }
.stat-list b { margin-left:auto; font-variant-numeric:tabular-nums; }
.st-dot { width:18px; height:18px; flex:none; border-radius:50%; display:grid; place-items:center; font-size:11px; font-weight:800; border:1.5px solid #8B96B8; color:#8B96B8; }
.st-dot.ok { background:var(--nox); border-color:var(--nox); color:#06130C; box-shadow:0 0 8px rgba(0,231,121,.6); }
.st-dot.bad { background:#FF5A6E; border-color:#FF5A6E; color:#fff; } .st-dot.warn { border-color:#FFB020; color:#FFB020; } .st-dot.work { border-color:var(--purple); color:#C9A2FF; }
/* review s akcemi */
.rv-row { display:flex; align-items:center; gap:12px; padding:10px 0; border-bottom:1px solid var(--border-soft); font-size:14px; } .rv-row:last-child { border-bottom:0; }
.rv-main { flex:1; min-width:0; } .rv-main b { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .rv-main span { font-size:12.5px; color:var(--text-dim); }
.rv-row .btn { flex:none; min-height:32px; padding:4px 12px; font-size:13px; }
/* paměť */
.kb-search { display:flex; align-items:center; gap:10px; padding:0 14px; border-radius:999px; border:1.5px solid rgba(146,59,255,.7); background:rgba(16,27,56,.7); box-shadow:0 0 14px rgba(146,59,255,.25); }
.kb-search input { flex:1; border:0; background:none; min-height:44px; color:var(--text); font-size:15px; outline:none; } .kb-search:focus-within { box-shadow:0 0 0 2px var(--cyan),0 0 18px rgba(4,207,255,.35); }
.kb-search i[data-ic] { color:var(--text-dim); }
.kb-chips { padding:10px 0 0; }
.kb-row { display:flex; align-items:center; gap:10px; width:100%; padding:10px 4px; border:0; border-bottom:1px solid var(--border-soft); background:none; color:var(--text); text-align:left; font:inherit; font-size:14px; cursor:pointer; }
.kb-row:hover,.kb-row.on { background:rgba(146,59,255,.1); } .kb-row .task-text { flex:1; }
.kb-row time { flex:none; font-size:12.5px; color:var(--text-dim); }
.kb-proj { flex:none; max-width:140px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:2px 10px; border-radius:8px; font-size:12px; border:1px solid rgba(255,138,24,.6); color:#FFC08A; background:rgba(255,138,24,.1); }
.kb-detail dl { display:grid; gap:4px; margin:0 0 12px; } .kb-detail dt { color:var(--cyan); font-size:12.5px; margin-top:8px; } .kb-detail dd { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; font-size:14px; }
.pg-dialog { max-width:min(560px,calc(100vw - 32px)); width:100%; border:1px solid rgba(146,59,255,.6); border-radius:16px; background:#0D1633; color:var(--text); box-shadow:0 0 40px rgba(146,59,255,.35); padding:20px; }
.pg-dialog::backdrop { background:rgba(3,6,18,.7); }
.pg-dialog h2 { margin:0 0 12px; font-size:18px; } .pg-dialog .kb-form { display:flex; flex-direction:column; gap:10px; }
.pg-dialog label { display:flex; flex-direction:column; gap:4px; font-size:13.5px; color:var(--text-dim); }
.pg-dialog .btns { display:flex; justify-content:flex-end; gap:8px; }
/* nastavení */
.set-nav a { display:flex; align-items:center; gap:12px; margin:6px 10px 0; padding:11px 14px; border-radius:12px; color:var(--text); text-decoration:none; font-size:15px; border:1px solid transparent; }
.set-nav a:hover { border-color:rgba(146,59,255,.5); background:rgba(146,59,255,.1); }
.set-nav a.on { background:var(--grad); box-shadow:0 0 16px rgba(146,59,255,.5); }
.set-nav a i[data-ic] { color:var(--text-dim); } .set-nav a.on i[data-ic] { color:#fff; }
.set-agent { display:flex; align-items:center; gap:14px; padding:12px 0; border-bottom:1px solid var(--border-soft); } .set-agent:last-child { border-bottom:0; }
.set-agent img { width:58px; height:58px; border-radius:50%; object-fit:cover; object-position:top; border:2px solid var(--c); box-shadow:0 0 12px color-mix(in srgb,var(--c) 60%,transparent); flex:none; }
.set-agent[data-agent="ada"] { --c:var(--ada); } .set-agent[data-agent="nox"] { --c:var(--nox); }
.set-agent-body { flex:1; min-width:0; } .set-agent-body b { font-size:16px; } .set-agent-body p { margin:2px 0 0; font-size:13px; color:var(--text-dim); overflow-wrap:anywhere; }
.set-agent .tags { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }
.pg-settings .mode-grid { display:flex; flex-direction:column; gap:8px; }
.pg-settings .mode-card { text-align:left; padding:10px 14px; } .pg-settings .mode-card h3 { margin:0 0 4px; font-size:14.5px; } .pg-settings .mode-card ul { margin:0; padding-left:18px; font-size:12.5px; color:var(--text-dim); }
.pg-settings .mode-card[aria-checked="true"] { border-color:var(--purple); box-shadow:0 0 16px rgba(146,59,255,.5); background:rgba(146,59,255,.14); }
.stop-box { display:flex; align-items:center; gap:14px; flex-wrap:wrap; } .stop-box p { margin:0; flex:1; min-width:200px; font-size:14px; }
.stop-box .btn.danger { border-color:#FF5A6E; color:#FFD0D6; background:rgba(255,90,110,.14); }
.pg-login p { margin:0 0 8px; font-size:14px; }
@media(max-width:1500px) { .metrics-row { grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; } }
@media(max-width:1280px) {
  .pg { grid-template-columns:repeat(2,minmax(0,1fr)); grid-template-rows:none!important; }
  .pg-quality { grid-template-areas:"list main" "side wide" "act act"; }
  .pg-research { grid-template-areas:"list main" "side side2" "act act"; }
  .pg-memory { grid-template-areas:"search search" "list results" "side side" "act act"; }
  .pg-settings { grid-template-columns:repeat(2,minmax(0,1fr)); grid-template-areas:"list main" "side stop" "sec login" "act act"; }
  .pg > .panel { max-height:640px; }
}
@media(max-width:899px) {
  .metrics-row { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .pg,.pg-settings { grid-template-columns:1fr; }
  .pg-quality { grid-template-areas:"list" "main" "side" "wide" "act"; }
  .pg-research { grid-template-areas:"list" "main" "side" "side2" "act"; }
  .pg-memory { grid-template-areas:"search" "results" "side" "list" "act"; }
  .pg-settings { grid-template-areas:"main" "side" "stop" "sec" "login" "act"; } .set-nav { display:none!important; }
  .stepper li { font-size:11px; } .stepper .node { width:24px; height:24px; }
}
@media(min-width:1281px) and (min-height:860px) {
  .pg-page:not([hidden]) { overflow:hidden; }
  .pg { flex:1; min-height:0; } .pg-quality,.pg-research { grid-template-rows:minmax(0,1.2fr) minmax(0,1fr); } .pg-memory { grid-template-rows:auto minmax(0,1fr); }
  .pg-settings { grid-template-rows:minmax(0,1.1fr) minmax(0,.7fr) minmax(0,.9fr); }
}
.act-initial { display:grid; place-items:center; background:var(--grad); color:#fff; font-weight:700; border-color:var(--purple)!important; }
.qg-req { margin:0 12px 12px; padding:10px 12px; border-radius:10px; border:1px solid rgba(255,176,32,.5); background:rgba(255,176,32,.07); font-size:13px; } .qg-req ul { margin:4px 0 0; padding-left:18px; }
.qg-kv { margin:10px 16px; } #qgProjectDetail .qg-check-grid { margin:0 16px 12px; } #qgProjectDetail .detail-section { margin:0 16px 12px; }
.ic { width:20px; height:20px; flex:none; }
.pg-head .ic { width:24px; height:24px; color:var(--cyan); margin-top:2px; } .link-row .ic { width:18px; height:18px; color:var(--cyan); flex:none; }
.conclusion .ic { width:22px; height:22px; color:var(--nox); flex:none; } .conclusion.warn .ic { color:#FFD27A; }
.btn-new { white-space:nowrap; flex:none; } .panel-title h2 { min-width:0; }
.kb-search { width:100%; box-sizing:border-box; }
.mode-dot { display:inline-block; width:10px; height:10px; margin-right:8px; border-radius:50%; vertical-align:1px; }
.mode-dot.basic { background:var(--nox); box-shadow:0 0 8px var(--nox); } .mode-dot.intelligent { background:var(--cyan); box-shadow:0 0 8px var(--cyan); } .mode-dot.autonomous { background:var(--purple); box-shadow:0 0 8px var(--purple); }
.pg-settings .mode-card ul { display:none; } .pg-settings .mode-card[aria-checked="true"] ul { display:block; }
.pg-progress .h-sub { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
@media(min-width:1281px) and (min-height:860px) { .pg-settings { grid-template-rows:minmax(0,1.15fr) minmax(0,.75fr) minmax(0,1fr); } .pg-sec .check-list { overflow:auto; min-height:0; } }
.pg > .panel { max-height:640px; }
@media(min-width:1281px) and (min-height:860px) { .pg > .panel { max-height:none; } }
@media(min-width:1100px) and (min-height:680px) { .metrics-row { grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; } }
@media(min-width:1100px) and (max-width:1500px) { .metrics-row .metric { padding:10px 12px; gap:10px; } .metrics-row .metric-label { font-size:13px; } .metrics-row .metric-value { font-size:22px; } .metrics-row .metric-value.small { font-size:15px; } .metrics-row .metric-icon { width:34px; height:34px; } .metrics-row .metric-icon svg { width:28px; height:28px; } }
@media(max-width:599px) {
  #masthead { grid-template-columns:minmax(0,1fr)!important; grid-template-areas:"brand" "tools" "heroes" "nav" "pause"!important; }
  #brand .name { font-size:28px; } #brand .sub { display:none; } #brand .mark,#brand .mark svg { width:56px; height:30px; }
  .metric,.metrics-row .metric { padding:10px; gap:8px; } .metric-icon,.metrics-row .metric-icon { width:32px; height:32px; } .metric-icon svg,.metrics-row .metric-icon svg { width:26px; height:26px; }
  .metric-label { font-size:12.5px; white-space:normal; overflow:visible; text-overflow:clip; } .metric-value { font-size:22px; }
}
/* ── Přehlednost a živá data (Michal 27. 11.: „ne chaoticky, intuitivně, real time“) ── */
.live-pill { display:inline-flex; align-items:center; gap:8px; padding:8px 14px; border-radius:999px; border:1px solid rgba(0,231,121,.45); background:rgba(0,231,121,.08); color:#BFF7DA; font-size:13.5px; white-space:nowrap; font-variant-numeric:tabular-nums; }
.live-dot { width:9px; height:9px; border-radius:50%; background:var(--nox); box-shadow:0 0 8px var(--nox); }
.live-pill.tick .live-dot { animation:live-tick .9s ease-out; }
@keyframes live-tick { 0% { transform:scale(1.8); } 100% { transform:scale(1); } }
.live-pill[data-state="stale"] { border-color:rgba(255,176,32,.6); background:rgba(255,176,32,.1); color:#FFE2A8; } .live-pill[data-state="stale"] .live-dot { background:#FFB020; box-shadow:0 0 8px #FFB020; }
.live-pill[data-state="down"] { border-color:rgba(255,90,110,.6); background:rgba(255,90,110,.1); color:#FFD0D6; } .live-pill[data-state="down"] .live-dot { background:#FF5A6E; box-shadow:0 0 8px #FF5A6E; }
@media (prefers-reduced-motion: reduce) { .live-pill.tick .live-dot { animation:none; } }
@media(max-width:1280px) { .live-pill { padding:6px 10px; font-size:12.5px; } }
.attn-dialog { max-width:min(760px,calc(100vw - 32px)); }
.attn-head { display:flex; align-items:center; justify-content:space-between; gap:12px; } .attn-head h2 { margin:0; }
#attnList { display:flex; flex-direction:column; gap:10px; margin-top:12px; max-height:min(70vh,720px); overflow:auto; }
.attn-item { padding:12px 14px; border-radius:12px; border:1px solid var(--border-soft); background:rgba(16,27,56,.7); }
.attn-item[data-kind="review"] { border-color:rgba(255,90,110,.55); } .attn-item[data-kind="task"] { border-color:rgba(255,176,32,.55); } .attn-item[data-kind="policy"] { border-color:rgba(146,59,255,.6); }
.attn-item h3 { margin:0 0 4px; font-size:15px; overflow-wrap:anywhere; } .attn-item p { margin:0 0 8px; font-size:13.5px; color:var(--text-dim); overflow-wrap:anywhere; }
.attn-item .why { color:#FFE2A8; }
.attn-item .actions,.attn-item .btns { display:flex; flex-wrap:wrap; gap:8px; }
.attn-empty { padding:24px; text-align:center; color:#BFF7DA; font-size:15px; }
/* ── Přihlášení Windows Hello (Michal 27. 11.: „ano, a dáme PIN“) ── */
.login-screen { position:fixed; inset:0; z-index:100; display:grid; place-items:center; padding:24px; background:radial-gradient(900px 500px at 50% 0%,rgba(146,59,255,.35),transparent 65%),radial-gradient(700px 400px at 50% 100%,rgba(4,207,255,.14),transparent 60%),#060B1C; }
.login-screen[hidden] { display:none; }
.login-card { width:min(440px,100%); padding:36px 32px 28px; border-radius:22px; border:1.5px solid rgba(146,59,255,.6); background:linear-gradient(160deg,rgba(23,32,72,.95),rgba(8,13,34,.97)); box-shadow:0 0 50px rgba(146,59,255,.35),inset 0 0 30px rgba(146,59,255,.08); text-align:center; }
.login-mark { width:110px; height:56px; filter:drop-shadow(0 0 14px rgba(4,207,255,.6)); }
.login-card h1 { margin:10px 0 2px; font-size:34px; letter-spacing:-.5px; } .login-sub { margin:0 0 26px; color:#C9D2EA; }
.login-btn { display:inline-flex; align-items:center; justify-content:center; gap:12px; width:100%; min-height:56px; border:0; border-radius:14px; background:var(--grad); color:#fff; font-size:18px; font-weight:650; cursor:pointer; box-shadow:0 0 24px rgba(146,59,255,.55); }
.login-btn svg { width:24px; height:24px; } .login-btn:hover { box-shadow:0 0 34px rgba(146,59,255,.8); } .login-btn:disabled { opacity:.6; cursor:wait; }
.login-btn:focus-visible { outline:3px solid #04CFFF; outline-offset:3px; }
.login-status { min-height:22px; margin:14px 0 6px; color:#FFE2A8; font-size:14px; } .login-status.ok { color:#8AF7C2; }
.login-alt { margin:8px 0 0; color:var(--text-dim); font-size:13px; }
.pk-list { display:flex; flex-direction:column; gap:6px; margin:8px 0 10px; }
.pk-row { display:flex; align-items:center; gap:10px; padding:8px 12px; border-radius:10px; border:1px solid var(--border-soft); background:rgba(16,27,56,.6); font-size:13.5px; }
.pk-row span { flex:1; min-width:0; } .pk-row .btn { min-height:30px; padding:2px 10px; font-size:12.5px; }
/* ── Provoz a Katalog (27. 11.) ── */
.pg-ops { grid-template-columns:minmax(0,1fr) minmax(0,1.7fr) minmax(0,1fr); grid-template-areas:"list main side"; grid-template-rows:minmax(420px,1fr); }
.pg-catalog { grid-template-columns:minmax(0,.8fr) minmax(0,3fr); grid-template-areas:"list main"; grid-template-rows:minmax(420px,1fr); }
.cat-search { margin-left:auto; } .cat-search input { min-height:36px; width:min(280px,40vw); padding:0 12px; border-radius:999px; border:1px solid rgba(146,59,255,.6); background:rgba(16,27,56,.7); color:var(--text); }
.cat-src { padding:0 16px 12px; margin:0; }
.cat-section h3 { margin:14px 0 8px; font-size:15px; color:var(--cyan); }
.cat-table { width:100%; border-collapse:collapse; font-size:13.5px; } .cat-table th { text-align:left; color:var(--text-dim); font-weight:600; padding:6px 8px; border-bottom:1px solid var(--border); }
.cat-table td { padding:7px 8px; border-bottom:1px solid var(--border-soft); vertical-align:top; overflow-wrap:anywhere; } .cat-table td:first-child { font-weight:600; }
.st-pill { display:inline-block; padding:1px 9px; border-radius:999px; font-size:12px; border:1px solid var(--border); white-space:nowrap; }
.st-pill.verified { color:#8AF7C2; border-color:rgba(0,231,121,.6); background:rgba(0,231,121,.12); } .st-pill.enabled { color:#9FE8FF; border-color:rgba(4,207,255,.5); background:rgba(4,207,255,.1); }
.st-pill.failed { color:var(--bad-text); border-color:rgba(255,90,110,.6); background:rgba(255,90,110,.12); } .st-pill.installed,.st-pill.path { color:var(--text-dim); }
.use-agent { padding:12px 0; border-bottom:1px solid var(--border-soft); } .use-agent:last-child { border-bottom:0; }
.use-agent .who { display:flex; align-items:center; gap:10px; } .use-agent img { width:38px; height:38px; border-radius:50%; object-fit:cover; object-position:top; }
.use-agent dl { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; margin:8px 0 0; font-size:13.5px; } .use-agent dt { color:var(--text-dim); } .use-agent dd { margin:0; text-align:right; font-variant-numeric:tabular-nums; }
.hist-row { display:grid; grid-template-columns:86px 120px minmax(0,1fr); gap:10px; padding:8px 4px; border-bottom:1px solid var(--border-soft); font-size:13.5px; align-items:start; }
.hist-row .who-badge { justify-self:start; } .hist-row time { color:var(--text-dim); font-variant-numeric:tabular-nums; } .hist-row b { overflow-wrap:anywhere; } .hist-row .det { color:var(--text-dim); overflow-wrap:anywhere; }
.hist-row[data-tone="bad"] b { color:var(--bad-text); } .hist-row[data-tone="ok"] b { color:#8AF7C2; }
@media(max-width:1280px) { .pg-ops { grid-template-columns:1fr 1fr; grid-template-areas:"main main" "list side"; } .pg-catalog { grid-template-columns:1fr; grid-template-areas:"list" "main"; } }
@media(max-width:899px) { .pg-ops { grid-template-columns:1fr; grid-template-areas:"main" "list" "side"; } .hist-row { grid-template-columns:70px minmax(0,1fr); } .hist-row .who-badge { display:none; } .cat-search input { width:100%; } }
/* ── Intuitivnost (27. 11.): odpověď nahoře, dlaždice vedou dál ── */
.now-bar { display:flex; flex-wrap:wrap; align-items:center; gap:10px 18px; margin:0 0 14px; padding:12px 18px; border-radius:14px; border:1.5px solid rgba(0,231,121,.5); background:linear-gradient(100deg,rgba(0,231,121,.1),rgba(16,27,56,.75) 60%); font-size:15px; }
.now-bar[data-tone="attn"] { border-color:rgba(255,176,32,.7); background:linear-gradient(100deg,rgba(255,176,32,.14),rgba(16,27,56,.75) 60%); }
.now-bar[data-tone="down"] { border-color:rgba(255,90,110,.7); background:linear-gradient(100deg,rgba(255,90,110,.14),rgba(16,27,56,.75) 60%); }
.now-bar .now-main { font-weight:650; font-size:16px; } .now-bar .now-agent { display:inline-flex; align-items:center; gap:8px; color:var(--text-dim); }
.now-bar .now-agent img { width:26px; height:26px; border-radius:50%; object-fit:cover; object-position:top; }
.now-bar .now-agent b { color:var(--text); } .now-bar .btn-new { margin-left:auto; }
.metric[data-goto],.metrics-row .metric[data-goto] { cursor:pointer; transition:transform 120ms ease, box-shadow 120ms ease; }
.metric[data-goto]:hover,.metric[data-goto]:focus-visible { transform:translateY(-2px); box-shadow:0 0 26px color-mix(in srgb,var(--t,#04CFFF) 55%,transparent); outline:none; }
body.room-focus .now-bar { display:none; }
@media(max-width:599px) {
  #headTools { flex-wrap:wrap; gap:8px; justify-content:flex-start; max-width:100%; }
  .live-pill { padding:5px 10px; font-size:12px; } .mode-badge { padding:6px 10px!important; font-size:12px!important; }
  .primary-nav { overflow-x:auto; max-width:100%; scrollbar-width:none; } .primary-nav::-webkit-scrollbar { display:none; }
  .now-bar { font-size:13.5px; } .now-bar .now-main { flex-basis:100%; } .now-bar .btn-new { margin-left:0; }
}
</style>
<a class="skip" href="#main">Přejít do místnosti</a>
<div id="shell">
  <header id="masthead">
    <div id="brand"><div class="mark"><svg viewBox="0 0 64 32" fill="none" aria-hidden="true"><defs><linearGradient id="infg" x1="0" x2="1"><stop offset="0" stop-color="#04CFFF"/><stop offset="1" stop-color="#923BFF"/></linearGradient></defs><path d="M6 16c0-6 4-10 10-10 11 0 21 20 32 20 6 0 10-4 10-10S54 6 48 6C37 6 27 26 16 26 10 26 6 22 6 16z" stroke="url(#infg)" stroke-width="5" stroke-linecap="round"/></svg></div><div><h1 class="name">Agent Lease</h1><div class="sub">Společný prostor pro tvoje agenty</div></div></div>
    <div id="agentHeroes"><article class="agent-hero" id="heroAda" data-agent="ada" data-state="unknown" aria-live="polite"><div class="portrait-wrap"><span class="halo" aria-hidden="true"></span><img class="portrait" src="/assets/ada.webp" alt="Portrét Ada" width="140" height="129"></div><div class="agent-info"><div class="agent-title"><span class="agent-name">Ada<svg class="vendor-mark" viewBox="0 0 26 20" aria-hidden="true"><path fill="currentColor" d="M2 19 9 1h3l7 18h-3.6l-1.6-4.3H6.9L5.4 19zm6-7.2h4.4L10.2 6zM17 1h3.4L25 13h-3.4z"/></svg></span><span class="agent-vendor">Anthropic (Claude)</span></div><div class="agent-role">Analýza • Výzkum • Návrhy</div><div class="agent-state"><span class="state-dot" aria-hidden="true"></span><span class="state-text">Neznámý stav</span><svg class="pulse-line" viewBox="0 0 80 20" aria-hidden="true"><path d="M0 10h22l4-7 5 14 5-12 4 9 4-4h36"/></svg></div><div class="agent-task"></div></div></article><div class="hub" aria-hidden="true"><svg viewBox="0 0 64 32" fill="none" aria-hidden="true"><defs><linearGradient id="infg2" x1="0" x2="1"><stop offset="0" stop-color="#04CFFF"/><stop offset="1" stop-color="#923BFF"/></linearGradient></defs><path d="M6 16c0-6 4-10 10-10 11 0 21 20 32 20 6 0 10-4 10-10S54 6 48 6C37 6 27 26 16 26 10 26 6 22 6 16z" stroke="url(#infg2)" stroke-width="5" stroke-linecap="round"/></svg></div><article class="agent-hero" id="heroNox" data-agent="nox" data-state="unknown" aria-live="polite"><div class="portrait-wrap"><span class="halo" aria-hidden="true"></span><img class="portrait" src="/assets/nox.webp" alt="Portrét Nox" width="140" height="129"></div><div class="agent-info"><div class="agent-title"><span class="agent-name">Nox<svg class="vendor-mark" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 2.5 20 7v10l-8 4.5L4 17V7z"/><path d="M12 7.5 16 10v4l-4 2.5L8 14v-4z"/></svg></span><span class="agent-vendor">OpenAI (GPT)</span></div><div class="agent-role">Implementace • Testy • Ověření</div><div class="agent-state"><span class="state-dot" aria-hidden="true"></span><span class="state-text">Neznámý stav</span><svg class="pulse-line" viewBox="0 0 80 20" aria-hidden="true"><path d="M0 10h22l4-7 5 14 5-12 4 9 4-4h36"/></svg></div><div class="agent-task"></div></div></article></div>
    <div id="headTools"><span id="modeBadge" class="mode-badge" title="Provozní režim místnosti"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/></svg><span id="modeText">Režim: …</span></span><span id="livePill" class="live-pill" role="status" aria-live="off"><span class="live-dot" aria-hidden="true"></span><span id="liveText">Připojuji…</span></span>
      <button class="icon-btn" id="bellBtn" type="button" aria-label="Co čeká na tebe" title="Úkoly ke kontrole a review čekající na tebe"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/></svg><span id="bellCount" class="bell-count" hidden>0</span></button>
      <details class="help"><summary class="icon-btn" aria-label="Nápověda"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 1-1 1.7M12 17v.5"/></svg></summary><div class="help-pop"><b>Barvy a stavy</b><p><span class="lg lg-ada"></span>Ada (Claude) · <span class="lg lg-nox"></span>Nox (Codex)</p><p>Karta agenta pulzuje jen tehdy, když agent opravdu pracuje. Čeká = online bez úkolu. Limit = vyčerpaný limit poskytovatele. Vyžaduje pozornost = úkol ke kontrole nebo zaseknutý most. Neznámý stav = web nemá čerstvá data.</p><p>Zvonek = úkoly ke kontrole a review, které čekají na tebe.</p></div></details><span id="humanBadge" class="human-badge" title="Přihlášený člověk">M</span></div>
    <nav class="primary-nav" aria-label="Hlavní navigace"><button class="current" aria-current="page" data-view="overview"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11 12 4l9 7v9h-6v-6H9v6H3z"/></svg>Přehled</button><button data-view="room"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg>Místnost agentů</button><button data-view="quality"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/></svg>Kvalita</button><button data-view="research"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/></svg>Výzkum</button><button data-view="memory"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/></svg>Paměť</button><button data-view="catalog"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>Katalog</button><button data-view="ops"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 20V12M10 20V8M15 20v-6M20 20V4"/></svg>Provoz</button><button data-view="settings"><svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/></svg>Nastavení</button><button id="navTasks" type="button" hidden>Úkoly</button><button type="button" id="navAgents" hidden>Agenti</button><a href="#projectPanel" id="navProjects" hidden>Projekty</a><a href="#historyPanel" id="navHistory" hidden>Historie</a><button data-view="analytics" hidden>Analytics</button></nav>
    <div id="pause"><div class="pausebar"><div class="big">Načítám stav systému…</div></div></div>
  </header>
  <div id="overviewView" data-view-panel="overview">
  <div id="nowBar" class="now-bar" role="status" aria-live="polite"></div>
  <section id="overview" aria-labelledby="overviewTitle">
    <div class="section-heading"><h2 id="overviewTitle" class="view-title" tabindex="-1">Přehled místnosti</h2><span id="scope">Fronta a review: celá místnost · běžící: aktivní úkol dle přehledu každého agenta</span></div>
    <div id="metrics">
      <div class="metric" data-goto="quality" tabindex="0" role="link" aria-label="Aktivní projekty – otevřít Kvalitu" data-tone-card="cyan"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></span><div><div class="metric-label">Aktivní projekty</div><div class="metric-value" id="metricAgents">—</div></div></div>
      <div class="metric" data-goto="room-tasks" tabindex="0" role="link" aria-label="Otevřené úkoly – otevřít úkoly v místnosti" data-tone-card="purple"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/></svg></span><div><div class="metric-label">Otevřené úkoly</div><div class="metric-value" id="metricRunning">—</div></div></div>
      <div class="metric" data-goto="research" tabindex="0" role="link" aria-label="Probíhající analýzy – otevřít Výzkum" data-tone-card="orange"><span class="metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"/><path d="M7.5 15h9"/></svg></span><div><div class="metric-label">Probíhající analýzy</div><div class="metric-value" id="metricQueued">—</div></div></div>
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
  <section id="qualityPage" class="view-page pg-page" data-view-panel="quality" hidden aria-labelledby="qualityTitle"><h2 id="qualityTitle" class="view-title sr-only" tabindex="-1">Kvalita</h2>
    <div class="metrics-row" id="qgMetrics"></div>
    <div class="pg pg-quality">
      <section class="panel pg-list"><div class="panel-title"><i data-ic="doc"></i><h2>Kontroly kvality <span class="h-count" id="qgRepoCount"></span></h2></div><div class="chips" id="qgChips" role="group" aria-label="Filtr kontrol"></div><div class="panel-body pg-scroll" id="qgProjectTable"></div><div id="qgRequests" role="status"></div></section>
      <section class="panel pg-main"><div class="panel-title"><i data-ic="shield"></i><h2 id="qgProjectTitle" tabindex="-1">Detail kontroly</h2><span class="h-sub" id="qgLastCheck"></span></div><div class="panel-body pg-scroll" id="qgProjectDetail"></div></section>
      <section class="panel pg-side"><div class="panel-title"><i data-ic="review"></i><h2>Stav review</h2></div><ul class="stat-list" id="qgReviewStats"></ul></section>
      <section class="panel pg-wide"><div class="panel-title"><i data-ic="review"></i><h2>Otevřená review <span class="h-count" id="qgOpenCount"></span></h2><span class="h-sub">Zavřít smí jen člověk nebo autor po schválení</span></div><div class="chips" id="rvChips" role="group" aria-label="Filtr review"></div><div class="panel-body pg-scroll" id="qgOpenReviews"></div></section>
      <aside class="panel pg-act"><div class="panel-title"><i data-ic="pulse"></i><h2>Aktivita agentů</h2></div><ol class="activity" data-activity="agents"></ol></aside>
    </div></section>
  <section class="view-page pg-page" data-view-panel="catalog" hidden aria-labelledby="catalogTitle"><h2 id="catalogTitle" class="view-title sr-only" tabindex="-1">Katalog</h2>
    <div class="metrics-row" id="catMetrics"></div>
    <div class="pg pg-catalog">
      <section class="panel pg-list"><div class="panel-title"><i data-ic="users"></i><h2>Agent</h2></div><div class="panel-body pg-scroll" id="catAgents"></div><p class="note cat-src" id="catSource"></p></section>
      <section class="panel pg-main"><div class="panel-title"><i data-ic="cube"></i><h2 id="catTitle">Schopnosti</h2><label class="cat-search"><span class="sr-only">Hledat ve schopnostech</span><input id="catQuery" type="search" placeholder="Hledat nástroj, MCP, skill…"></label></div><div class="chips" id="catChips" role="group" aria-label="Filtr stavu"></div><div class="panel-body pg-scroll" id="catBody"></div></section>
    </div></section>
  <section class="view-page pg-page" data-view-panel="ops" hidden aria-labelledby="opsTitle"><h2 id="opsTitle" class="view-title sr-only" tabindex="-1">Provoz</h2>
    <div class="metrics-row" id="opsMetrics"></div>
    <div class="pg pg-ops">
      <section class="panel pg-list"><div class="panel-title"><i data-ic="bars"></i><h2>Spotřeba tokenů</h2><span class="h-sub" id="opsCaps"></span></div><div class="panel-body pg-scroll" id="opsUsage"></div></section>
      <section class="panel pg-main"><div class="panel-title"><i data-ic="clock"></i><h2>Historie akcí <span class="h-count" id="histCount"></span></h2><label class="cat-search"><span class="sr-only">Hledat v historii</span><input id="histQuery" type="search" placeholder="Hledat cestu, review, detail…"></label></div><div class="chips" id="histChips" role="group" aria-label="Druh akce"></div><div class="panel-body pg-scroll" id="histList"></div><div class="pg-actions"><button class="btn" id="histMore" type="button">Načíst starší</button></div></section>
      <section class="panel pg-side"><div class="panel-title"><i data-ic="flask"></i><h2>Poslední běhy na pozadí</h2></div><div class="panel-body pg-scroll" id="opsRuns"></div></section>
    </div></section>
  <section class="view-page" data-view-panel="analytics" hidden><div class="panel placeholder-page"><h2 class="view-title" tabindex="-1">Analytics · připravuje se</h2><p>Výkon za 24 hodin ani skutečná úspěšnost nejsou v dostupném API definované. Metriky a grafy zatím nejsou připojené.</p><button class="btn" data-view="overview">Zpět do místnosti</button></div></section>
  <section class="view-page pg-page" data-view-panel="research" hidden aria-labelledby="researchTitle"><h2 id="researchTitle" class="view-title sr-only" tabindex="-1">Výzkum</h2>
    <div class="metrics-row" id="rsMetrics"></div>
    <div class="pg pg-research">
      <section class="panel pg-list"><div class="panel-title"><i data-ic="target"></i><h2>Výzkumné úkoly</h2><button class="btn-new" type="button" id="rsNew"><i data-ic="plus"></i>Nový výzkum</button></div><div class="chips" id="rsChips" role="group" aria-label="Filtr výzkumů"></div><div class="panel-body pg-scroll" id="rsList" role="status">Načítám…</div></section>
      <section class="panel pg-main" id="rsDetail"><div class="empty">Vyber výzkum ze seznamu.</div></section>
      <section class="panel pg-side"><div class="panel-title"><i data-ic="layers"></i><h2>Části výzkumu <span class="h-count" id="rsPartsCount"></span></h2></div><ul class="dash-list pg-scroll" id="rsParts"></ul></section>
      <section class="panel pg-side2"><div class="panel-title"><i data-ic="link"></i><h2>Citace a odkazy</h2></div><div class="panel-body pg-scroll" id="rsLinks"></div></section>
      <aside class="panel pg-act"><div class="panel-title"><i data-ic="pulse"></i><h2>Aktivita agentů</h2></div><ol class="activity" data-activity="agents"></ol></aside>
    </div></section>
  <section class="view-page pg-page" data-view-panel="memory" hidden aria-labelledby="memoryTitle"><h2 id="memoryTitle" class="view-title sr-only" tabindex="-1">Paměť</h2>
    <div class="metrics-row" id="kbMetrics"></div>
    <div class="pg pg-memory">
      <section class="panel pg-list"><div class="panel-title"><i data-ic="folder"></i><h2>Projektová paměť</h2><button class="btn-new" type="button" id="kbNew"><i data-ic="plus"></i>Nová položka</button></div><div class="panel-body pg-scroll" id="kbProjects"></div></section>
      <section class="panel pg-search"><div class="panel-title"><i data-ic="search"></i><h2>Vyhledat v paměti</h2></div><div class="panel-body"><label class="kb-search"><span class="sr-only">Hledat v paměti</span><i data-ic="search"></i><input id="kbQuery" type="search" placeholder="Hledej příznak, chybu, příčinu nebo řešení…"></label><div class="chips kb-chips" id="kbStatus" role="group" aria-label="Stav záznamu"></div></div></section>
      <section class="panel pg-results"><div class="panel-title"><i data-ic="doc"></i><h2>Záznamy <span class="h-count" id="kbCount"></span></h2><span class="h-sub" id="kbScope"></span></div><div class="panel-body pg-scroll" id="kbList" role="status">Načítám…</div></section>
      <section class="panel pg-side" id="kbDetailPanel"><div class="panel-title"><i data-ic="flag"></i><h2 id="kbDetailTitle">Detail záznamu</h2></div><div class="panel-body pg-scroll" id="kbDetail"></div></section>
      <aside class="panel pg-act"><div class="panel-title"><i data-ic="pulse"></i><h2>Aktivita v paměti</h2></div><ol class="activity" id="kbActivity"></ol></aside>
    </div>
    <dialog id="kbDialog" class="pg-dialog" aria-labelledby="kbDialogTitle"><form method="dialog" class="kb-form"><h2 id="kbDialogTitle">Nový poznatek</h2><label>Příznak *<textarea id="kbSymptom" rows="2" maxlength="4000" required></textarea></label><label>Příčina<textarea id="kbCause" rows="2" maxlength="4000"></textarea></label><label>Řešení<textarea id="kbSolution" rows="3" maxlength="4000"></textarea></label><label>Projekt<input id="kbProject" maxlength="200" list="kbProjectList"></label><datalist id="kbProjectList"></datalist><label>Důkaz (commit, test, odkaz)<input id="kbEvidence" maxlength="1000"></label><p class="note">Uloží se jako neověřené. Ověřit záznam smí jen někdo jiný než autor, vždy s důkazem.</p><div class="btns"><button class="btn" value="cancel" formnovalidate>Zrušit</button><button class="btn detail" id="kbAdd" type="button">Uložit</button></div></form></dialog></section>
  <section class="view-page pg-page" data-view-panel="settings" hidden aria-labelledby="settingsTitle"><h2 id="settingsTitle" class="view-title sr-only" tabindex="-1">Nastavení</h2>
    <div class="metrics-row" id="setMetrics"></div>
    <div class="pg pg-settings">
      <nav class="panel pg-list set-nav" aria-label="Části nastavení"><div class="panel-title"><i data-ic="gear"></i><h2>Nastavení</h2></div><a href="#setAgents"><i data-ic="users"></i>Agenti</a><a href="#setMode"><i data-ic="sliders"></i>Provozní režim</a><a href="#setStop"><i data-ic="stop"></i>Nouzové zastavení</a><a href="#setSecurity"><i data-ic="shield"></i>Bezpečnost</a><a href="#setLogin"><i data-ic="lock"></i>Přihlášení</a></nav>
      <section class="panel pg-main" id="setAgents"><div class="panel-title"><i data-ic="users"></i><h2>Konfigurace agentů</h2></div><div class="panel-body" id="setAgentList"></div></section>
      <section class="panel pg-side" id="setMode"><div class="panel-title"><i data-ic="sliders"></i><h2>Provozní režim</h2></div><div class="panel-body"><div class="mode-grid" id="modeGrid" role="radiogroup" aria-label="Provozní režim"></div><p class="note" id="modeInfo"></p></div></section>
      <section class="panel pg-stop" id="setStop"><div class="panel-title"><i data-ic="stop"></i><h2>Nouzové zastavení</h2></div><div class="panel-body" id="setStopBody"></div></section>
      <section class="panel pg-sec" id="setSecurity"><div class="panel-title"><i data-ic="shield"></i><h2>Bezpečnost</h2><span class="tag ok">platí vždy</span></div><ul class="check-list"><li>Web poslouchá jen na tomto počítači (loopback), zvenku není dostupný.</li><li>Přihlášení jednorázovým tokenem, potom cookie nedostupná skriptům.</li><li>Každá akce z webu se zapisuje do historie i s důvodem.</li><li>Review kontroluje tajné údaje a autor nikdy neschvaluje vlastní práci.</li><li>Nejvýš 3 kola oprav, pak rozhoduješ ty.</li></ul></section>
      <section class="panel pg-login" id="setLogin"><div class="panel-title"><i data-ic="lock"></i><h2>Přihlášení do webu</h2></div><div class="panel-body"><p>Web běží pořád jako služba <code>agent-lease-web</code> na <code>http://localhost:8765</code>.</p><p>Přihlášení platí <strong>7 dní</strong>. Pak se přihlásíš přes <strong>Windows Hello (PIN)</strong> přímo na stránce, nebo ikonou <strong>Agent Lease</strong> na ploše.</p><div class="pk-list" id="pkList"></div><button type="button" class="btn-new" id="pkAdd"><i data-ic="plus"></i>Přidat Windows Hello (PIN)</button><p class="note" id="pkStatus" role="status"></p></div></section>
      <aside class="panel pg-act"><div class="panel-title"><i data-ic="clock"></i><h2>Poslední řídicí aktivita</h2></div><ol class="activity" id="setActivity"></ol></aside>
    </div></section>
</div>
<div id="loginScreen" class="login-screen" hidden role="dialog" aria-modal="true" aria-labelledby="loginTitle"><div class="login-card"><svg class="login-mark" viewBox="0 0 64 32" fill="none" aria-hidden="true"><defs><linearGradient id="loginfg" x1="0" x2="1"><stop offset="0" stop-color="#04CFFF"/><stop offset="1" stop-color="#923BFF"/></linearGradient></defs><path d="M6 16c0-6 4-10 10-10 11 0 21 20 32 20 6 0 10-4 10-10S54 6 48 6C37 6 27 26 16 26 10 26 6 22 6 16z" stroke="url(#loginfg)" stroke-width="5" stroke-linecap="round"/></svg><h1 id="loginTitle">Agent Lease</h1><p class="login-sub">Společný prostor pro tvoje agenty</p><button type="button" class="login-btn" id="loginHello"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>Přihlásit přes Windows Hello</button><p class="login-status" id="loginStatus" role="status"></p><p class="login-alt">Bez Windows Hello: ikona <b>Agent Lease</b> na ploše, nebo v terminálu <code>agent-lease web-login</code>.</p></div></div>
<dialog id="attnDialog" class="pg-dialog attn-dialog" aria-labelledby="attnTitle"><div class="attn-head"><h2 id="attnTitle">Čeká na tebe</h2><button class="btn" id="attnClose" type="button" aria-label="Zavřít">✕</button></div><p class="note">Věci, které agenti sami nesmí nebo nemohou udělat. Vše, co tu potvrdíš, se zapíše do historie.</p><div id="attnList"></div></dialog>
<dialog id="workspaceDrawer" aria-labelledby="drawerTitle"><header class="drawer-header"><h2 id="drawerTitle">Pracovní prostor</h2><button id="drawerClose" class="btn" type="button" aria-label="Zavřít panel">✕</button><div id="drawerPause"></div></header><div id="drawerContent"></div></dialog>
<script>
const KEY_STORE = "agentLeaseKey";
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
// Pruh „Teď“: jedna věta, co se děje, a co potřebuje člověka (s tlačítkem rovnou na to).
function renderNowBar() {
  const el = document.getElementById("nowBar"); if (!el) return;
  const items = attentionItems(), n = items.reduce((s, i) => s + (i.kind === "more" ? i.n : 1), 0);
  const st = {ada: agentStatus("ada"), nox: agentStatus("nox")};
  const down = connectionMode !== "live";
  const main = down ? "Spojení s místností je přerušené — data se neobnovují." : lastPaused ? "Agenti jsou zastavení. Nic na pozadí nepracuje."
    : n ? `Na tebe čeká ${n} ${n === 1 ? "věc" : n < 5 ? "věci" : "věcí"}.` : "Vše v pořádku — agenti pracují sami.";
  el.dataset.tone = down || lastPaused ? "down" : n ? "attn" : "ok";
  const agent = k => `<span class="now-agent"><img src="/assets/${k}.webp" alt=""><b>${k === "ada" ? "Ada" : "Nox"}</b> ${esc(st[k].label)}${st[k].detail && st[k].state === "working" ? " · " + esc(st[k].detail.slice(0, 60)) : ""}</span>`;
  setHTMLIfChanged(el, `<span class="now-main">${esc(main)}</span>${agent("ada")}${agent("nox")}${n ? '<button type="button" class="btn-new" id="nowAttn">Vyřešit</button>' : ""}`);
  const b = document.getElementById("nowAttn");
  if (b) b.onclick = () => { renderAttention(); document.getElementById("attnDialog").showModal(); };
}
function refreshDashboard() {
  renderNowBar();
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
    el.title = s.label + (s.detail ? ": " + s.detail : "");
  }
}
setInterval(() => { if (!document.hidden) renderHeroes(); }, 5000);
function renderWork() {
  const tasks = historyMessages().filter(m => m.kind === "task");
  const board = latestBoard || [];
  const attention = attentionItems().reduce((s, i) => s + (i.kind === "more" ? i.n : 1), 0);
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
// ── „Čeká na tebe“: jedno místo pro všechno, co potřebuje člověka ──
const TASK_ERROR = e => !e ? "" : e === "worker lease expired after start" ? "Běh na pozadí nedoběhl (most přestal hlásit průběh). Výsledek není jistý, proto se sám neopakuje."
  : e.startsWith("quarantine") ? "Úkol zadržela bezpečnostní kontrola." : e.startsWith("fix:") ? "Oprava se zápisem do repozitáře čeká na tvé povolení."
  : e.startsWith("checkpoint:") ? "Pracovní složka se od uložení změnila." : e;
function attentionItems() {
  const items = [];
  (latestReviewsAll || latestReviews || []).filter(r => r.state === "escalated").forEach(r => items.push({kind: "review", r}));
  const tasks = historyMessages().filter(m => m.kind === "task" && ["needs_review", "failed", "dead_letter"].includes(m.state));
  tasks.forEach(m => items.push({kind: "task", m}));
  (qgRecords || []).filter(r => r.policy_review_required && r.policy_review_status !== "approved" && r.run_id).forEach(r => items.push({kind: "policy", r}));
  const hidden = (latestBoard || []).reduce((s, b) => s + (b.needs_review || 0), 0) - tasks.filter(m => m.state === "needs_review").length;
  if (hidden > 0) items.push({kind: "more", n: hidden});
  return items;
}
function renderAttention() {
  const list = document.getElementById("attnList"), items = attentionItems();
  // Klíč nese vše, co se v panelu zobrazuje nebo určuje tlačítka (review #57: last_error, fix, stav review/kvality).
  const key = JSON.stringify(items.map(i => [i.kind, i.n, i.m && [i.m.id, i.m.state, i.m.last_error, i.m.fix, i.m.recipient],
    i.r && [i.r.id, i.r.run_id, i.r.state, i.r.policy_review_status, i.r.topic]]));
  if (list._key === key) return;
  list._key = key;
  list.innerHTML = items.length ? "" : '<div class="attn-empty">✓ Nic nečeká. Agenti si poradí sami.</div>';
  items.forEach(it => {
    const div = document.createElement("div");
    div.className = "attn-item"; div.dataset.kind = it.kind;
    if (it.kind === "review") {
      const v = it.r;
      div.innerHTML = `<h3>Review #${v.id}: ${esc(v.topic.slice(0, 140))}</h3><p>${esc(nameOf(v.author))} → ${esc(nameOf(v.reviewer))} · vyčerpaná 3 kola oprav, rozhoduješ ty.</p><div class="btns"><button class="btn" data-a="grant">Povolit ještě 1 kolo</button><button class="btn" data-a="close">Uzavřít review</button></div>`;
      div.querySelector('[data-a="grant"]').onclick = () => { const reason = prompt("Proč povolit další kolo oprav? (zapíše se)"); if (reason && reason.trim()) post("/api/review-grant", {id: v.id, reason}); };
      div.querySelector('[data-a="close"]').onclick = () => { const reason = prompt("Proč uzavřít? (zapíše se)", "převezmu sám"); if (reason && reason.trim()) post("/api/review-close", {id: v.id, reason}); };
    } else if (it.kind === "task") {
      const m = it.m;
      div.innerHTML = `<h3>Úkol #${m.id} pro ${esc(nameOf(m.recipient))}: ${esc((m.text || "").split(String.fromCharCode(10))[0].slice(0, 120))}</h3><p class="why">${esc(TASK_ERROR(m.last_error) || (STATE[m.state] || [m.state])[0])}</p>`;
      renderTaskActions(m, div);
    } else if (it.kind === "policy") {
      const r = it.r;
      div.innerHTML = `<h3>Kontrola kvality: ${esc(String(r.repository).split("/").pop())}</h3><p>Commit mění testy nebo pravidla a čeká na tvé schválení.</p><div class="btns"><button class="btn">Otevřít v Kvalitě</button></div>`;
      div.querySelector("button").onclick = () => { document.getElementById("attnDialog").close(); qgSelected = r.run_id; showView("quality"); };
    } else {
      div.innerHTML = `<h3>Dalších ${it.n} úkolů ke kontrole</h3><p>Jsou starší než načtená historie místnosti.</p><div class="btns"><button class="btn">Ukázat v místnosti</button></div>`;
      div.querySelector("button").onclick = () => { document.getElementById("attnDialog").close(); openRoomTasks(); };
    }
    list.appendChild(div);
  });
}
document.getElementById("bellBtn").onclick = () => { renderAttention(); document.getElementById("attnDialog").showModal(); };
document.getElementById("attnClose").onclick = () => document.getElementById("attnDialog").close();
// ── Živá data: kdy přišla poslední aktualizace ──
let lastLiveTick = 0;
setInterval(() => {
  const pill = document.getElementById("livePill"), txt = document.getElementById("liveText");
  const age = lastRoomAt ? Math.max(0, Math.round((Date.now() - lastRoomAt) / 1000)) : null;
  const state = connectionMode !== "live" ? "down" : age === null || age > 15 ? "stale" : "live";
  pill.dataset.state = state;
  txt.textContent = state === "down" ? "Spojení přerušeno" : age === null ? "Připojuji…" : state === "stale" ? `Data stará ${age} s` : "Živě";
  if (lastRoomAt && lastRoomAt !== lastLiveTick) { lastLiveTick = lastRoomAt; pill.classList.remove("tick"); void pill.offsetWidth; pill.classList.add("tick"); }
  if (document.getElementById("attnDialog").open) renderAttention();
}, 1000);
document.querySelectorAll("[data-goto]").forEach(b => { b.onclick = () => b.dataset.goto === "room-tasks" ? openRoomTasks() : showView(b.dataset.goto);
  if (b.getAttribute("role") === "link") b.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); b.onclick(); } }; });
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
setInterval(() => { if (!document.hidden && currentView === "overview") loadDashData(); }, 15000);
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
  const items = dedupeActivity(historyMessages().filter(m => (m.agent === "claude-code" || m.agent === "codex") && !isSystem(m)).reverse()).slice(0, 8);
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
let latestProjects = {};
function renderProjects(reviews) {
  lastProjReviews = reviews || [];
  const by = new Map();
  // Pracovní kopie (worktree) se počítají do svého hlavního repozitáře — mapu posílá server.
  lastProjReviews.forEach(r => { if (!r.repo) return; const key = latestProjects[r.repo] || r.repo.split("/").filter(Boolean).pop();
    const s = by.get(key) || {total: 0, done: 0, open: 0, repos: new Set()}; s.total++; s.repos.add(r.repo);
    if (["approved", "closed"].includes(r.state)) s.done++; else s.open++; by.set(key, s); });
  const rows = [...by.entries()].sort((a, b) => b[1].open - a[1].open || a[0].localeCompare(b[0]))
    .filter(([, s]) => projFilter === "all" || (projFilter === "open" ? s.open > 0 : s.open === 0));
  const folder = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
  setHTMLIfChanged(document.getElementById("projects"), rows.length ? rows.map(([repo, s], i) => {
    const pct = Math.round(100 * s.done / s.total);
    return `<div class="proj ${i === 0 && s.open ? "proj-hot" : ""}" title="${esc([...s.repos].join(String.fromCharCode(10)))}"><span class="proj-ic">${folder}</span><div class="proj-body"><div class="proj-head"><b>${esc(repo)}</b><span>${pct} %</span></div><div class="proj-sub">${s.done}/${s.total} review schváleno${s.open ? ` · ${s.open} otevřených` : ""}</div><div class="bar"><i style="width:${pct}%"></i></div></div></div>`;
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
  setTimeout(renderPages);   // Nastavení hned ukáže neznámý stav mostů
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
    qgRecords = records.filter(record => record && ((record.kind === "quality_run" && record.executed === true) || (["quality_attempt", "quality_refusal"].includes(record.kind) && Number.isInteger(record.run_id) && record.run_id > 0)));
  }
  if (Array.isArray(r.quality_requests)) qgRequests = r.quality_requests;
  if (Object.prototype.hasOwnProperty.call(r, "quality_runs") || Object.prototype.hasOwnProperty.call(r, "quality_requests")) renderQualityGate();
  latestBoard = Array.isArray(r.board) ? r.board : null;
  renderReviews(r.reviews);
  if (Array.isArray(r.reviews)) latestReviews = r.reviews;
  if (r.projects) latestProjects = r.projects;
  if (Array.isArray(r.reviews_all)) { latestReviewsAll = r.reviews_all; renderProjects(r.reviews_all); }
  onDataVersion(r.version);
  if (r.mode && r.mode.mode) renderMode(r.mode);
  if (Array.isArray(r.claims)) latestClaims = r.claims;
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
  renderPages();
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
  const es = new EventSource("/api/events?since=" + since + "&k=" + encodeURIComponent(sessionKey()));
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
  if (!token) return;  // bez tokenu zkusíme existující cookie a uložený klíč
  const response = await fetch("/session", { method: "POST", headers: { "X-Agent-Lease-Token": token } });
  if (!response.ok) throw new Error("Login failed");
  localStorage.setItem(KEY_STORE, response.headers.get("X-Agent-Lease-Key") || "");
}

// 403 = přihlášení na tomto serveru neplatí (např. po jeho restartu): říct to jasně, ne jen chybou v konzoli.
const rawFetch = window.fetch.bind(window);
let sessionLostShown = false;
// Druhý klíč přihlášení: localStorage je oddělené podle portu, cookie ne (bezpečnostní audit 26. 11.).
const sessionKey = () => localStorage.getItem(KEY_STORE) || "";
window.fetch = (url, opts = {}) => rawFetch(url, {...opts, headers: {...(opts.headers || {}), "X-Agent-Lease-Key": sessionKey()}}).then(r => {
  if (r.status === 403 && !String(url).startsWith("/session") && !sessionLostShown) { sessionLostShown = true; showSessionLost(); }
  return r;
});
function showSessionLost() {
  updateStatus("retry");
  const bar = document.createElement("div"); bar.className = "session-lost"; bar.setAttribute("role", "alert");
  bar.innerHTML = "Přihlášení k webu vypršelo. Přihlas se znovu přes Windows Hello, nebo ikonou <b>Agent Lease</b> na ploše.";
  document.body.prepend(bar);
  showLoginHelp();   // rovnou nabídnout přihlášení Windows Hello
}
// ── Windows Hello (WebAuthn): base64url ↔ ArrayBuffer ──
const b64uToBuf = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), c => c.charCodeAt(0)).buffer;
const bufToB64u = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/[+]/g, "-").replace(/[/]/g, "_").replace(/=+$/, "");
const helloSupported = () => !!window.PublicKeyCredential && location.hostname === "localhost";
async function helloLogin() {
  const btn = document.getElementById("loginHello"), st = document.getElementById("loginStatus");
  btn.disabled = true; st.className = "login-status"; st.textContent = "Potvrď přihlášení PINem ve Windows…";
  try {
    const opt = await rawFetch("/session/passkey-options", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    if (!opt.ok) throw new Error(opt.status === 429 ? "Příliš mnoho pokusů. Zkus to za 5 minut, nebo použij ikonu na ploše." : "Přihlášení teď nejde.");
    const o = await opt.json();
    if (!o.allowCredentials.length) throw new Error("Windows Hello zatím není nastavené. Přihlas se ikonou na ploše a v Nastavení → Přihlášení klikni na „Přidat Windows Hello“.");
    const cred = await navigator.credentials.get({publicKey: {challenge: b64uToBuf(o.challenge), rpId: o.rpId, userVerification: "required", timeout: o.timeout,
      allowCredentials: o.allowCredentials.map(c => ({type: "public-key", id: b64uToBuf(c.id)}))}});
    const r = cred.response;
    const res = await rawFetch("/session/passkey", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      id: cred.id, clientDataJSON: bufToB64u(r.clientDataJSON), authenticatorData: bufToB64u(r.authenticatorData), signature: bufToB64u(r.signature)})});
    if (!res.ok) throw new Error(res.status === 429 ? "Příliš mnoho pokusů. Zkus to za 5 minut." : "Ověření se nepovedlo.");
    localStorage.setItem(KEY_STORE, res.headers.get("X-Agent-Lease-Key") || "");
    st.className = "login-status ok"; st.textContent = "Přihlášeno.";
    location.reload();
  } catch (e) {
    st.textContent = e && e.name === "NotAllowedError" ? "Přihlášení bylo zrušeno nebo vypršelo." : (e && e.message) || "Přihlášení se nepovedlo.";
    btn.disabled = false;
  }
}
document.getElementById("loginHello").onclick = helloLogin;
function loadPasskeys() {
  fetch("/api/passkey").then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    setHTMLIfChanged(document.getElementById("pkList"), d.keys.length ? d.keys.map(k => `<div class="pk-row"><span>🔑 ${esc(k.name)} · přidáno ${esc(when(k.created_at))}${k.last_used ? " · naposledy " + esc(when(k.last_used)) : ""}</span><button class="btn" data-pk-remove="${esc(k.id)}">Odebrat</button></div>`).join("") : '<p class="note">Zatím žádný klíč Windows Hello.</p>');
    document.querySelectorAll("[data-pk-remove]").forEach(b => b.onclick = () => { if (confirm("Odebrat tento klíč Windows Hello?")) post("/api/passkey/remove", {id: b.dataset.pkRemove}).then(loadPasskeys); });
  }).catch(() => {});
}
document.getElementById("pkAdd").onclick = async () => {
  const st = document.getElementById("pkStatus");
  if (!helloSupported()) { st.textContent = "Windows Hello funguje jen na adrese http://localhost:8765 v prohlížeči s WebAuthn."; return; }
  try {
    st.textContent = "Potvrď přidání PINem ve Windows…";
    const o = await (await fetch("/api/passkey/register-options", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})).json();
    const cred = await navigator.credentials.create({publicKey: {...o, challenge: b64uToBuf(o.challenge), user: {...o.user, id: b64uToBuf(o.user.id)},
      excludeCredentials: o.excludeCredentials.map(c => ({type: "public-key", id: b64uToBuf(c.id)}))}});
    const ok = await post("/api/passkey/register", {id: cred.id, clientDataJSON: bufToB64u(cred.response.clientDataJSON),
      attestationObject: bufToB64u(cred.response.attestationObject), name: "Windows Hello · " + new Date().toLocaleDateString("cs-CZ")});
    st.textContent = ok ? "Hotovo. Příště se přihlásíš PINem přímo na stránce." : "";
    loadPasskeys();
  } catch (e) { st.textContent = e && e.name === "NotAllowedError" ? "Přidání bylo zrušeno." : "Přidání se nepovedlo."; }
};

function showLoginHelp() {
  // Bez přihlášení: přihlašovací obrazovka s Windows Hello; ikona na ploše zůstává jako záloha.
  const screen = document.getElementById("loginScreen");
  screen.hidden = false;
  const btn = document.getElementById("loginHello");
  if (!helloSupported()) { btn.hidden = true; document.getElementById("loginStatus").textContent = location.hostname === "localhost" ? "Tento prohlížeč neumí Windows Hello." : "Windows Hello funguje na adrese http://localhost:8765."; }
  else btn.focus();
  // Bez platného přihlášení by horní pruh navždy ukazoval „Načítám…“ (review #17).
  const help = "Nejsi přihlášený. Klikni na ikonu <b>Agent Lease</b> na ploše, nebo v terminálu spusť <b>agent-lease web-login</b>.";
  document.getElementById("pause").innerHTML = `<div class="pausebar"><div class="big">${help}</div></div>`;
  feed.innerHTML = `<div class="bubble"><div class="text">${help}</div></div>`;
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
  return safe.length ? `<ul>${safe.map(log => `<li><a href="${esc(log.url + "&k=" + encodeURIComponent(sessionKey()))}" target="_blank" rel="noopener noreferrer">${esc(log.check)} · procesní protokol</a></li>`).join("")}</ul>` : "";
}
function qgDate(value) { return value ? new Date(value).toLocaleString("cs-CZ") : "Není známá"; }
function qgTable(records,{compact=false}={}) {
  const headers = compact ? ["Repozitář",...QG_CHECKS.map(c=>c[1]),"Stav"] : ["Repozitář / větev","Commit","Agent","Kontrola",...QG_CHECKS.map(c=>c[1]),"Stav"];
  return `<div class="qg-scroll" tabindex="0" role="region" aria-label="Výsledky kontrol repozitářů; tabulku lze posouvat vodorovně"><table class="qg-table"><caption class="sr-only">${qgDemo ? "DEMO — ukázkové výsledky, ne skutečné kontroly" : records.length ? "Zaznamenané výsledky kontrol" : "Quality Gate dosud nemá výsledky"}</caption><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>`
    + (records.length ? records.map((r,i)=>`<tr><th scope="row"><button type="button" class="qg-repo" data-qg-project="${i}">${esc(r.repository)}</button>${compact ? "" : `<span class="qg-branch">${esc(r.branch || "Větev není uvedená")}</span>`}</th>${compact ? "" : `<td class="path">${esc(r.commit_sha ? r.commit_sha.slice(0,10) : "Neuveden")}</td><td>${esc(r.agent || "Neuveden")}</td><td>${esc(qgDate(r.timestamp))}</td>`}${QG_CHECKS.map(([key])=>`<td>${qgBadge(r.checks?.[key]?.status || "NOT_RUN")}</td>`).join("")}<td>${qgBadge(r.overall_status)}</td></tr>`).join("") : `<tr><td colspan="${headers.length}" class="qg-empty">Dosud bez výsledků. Pro tyto repozitáře není zaznamenaný běh kontrol.</td></tr>`)
    + `</tbody></table></div>`;
}
// ── Stránky Kvalita, Výzkum, Paměť, Nastavení (předlohy 01/03/04/06, jen skutečná data) ──
const IC = {
  search:'<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
  layers:'<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  flask:'<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"/><path d="M7.5 15h9"/>',
  check:'<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  bookmark:'<path d="M6 3h12v18l-6-4-6 4z"/>',
  bars:'<path d="M5 20V12M10 20V8M15 20v-6M20 20V4"/>',
  target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  doc:'<path d="M6 3h9l4 4v14H6z"/><path d="M9 12h7M9 16h5"/>',
  link:'<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  pulse:'<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  shield:'<path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  star:'<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/>',
  alert:'<path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17v.5"/>',
  db:'<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  network:'<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5 6.5 17M12 11.5l5.5 5.5"/>',
  flag:'<path d="M5 21V4"/><path d="M5 4h12l-2 4 2 4H5"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
  users:'<circle cx="9" cy="8" r="3.5"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M16 4.5a3.5 3.5 0 0 1 0 7M18 14c2 .8 3 2.7 3 6"/>',
  lock:'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  stop:'<circle cx="12" cy="12" r="9"/><path d="M9 9h6v6H9z"/>',
  sliders:'<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  cube:'<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  review:'<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="12" r="2.5"/><path d="M6 8.5v7M8.5 6H12a4 4 0 0 1 4 4v-.5"/>',
  bridge:'<path d="M3 17h18M5 17V9M19 17V9M5 9c4 4 10 4 14 0"/>',
};
function icon(n) { return `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${IC[n] || ""}</svg>`; }
function paintIcons(root = document) {
  root.querySelectorAll("i[data-ic]:not([data-painted])").forEach(i => { i.innerHTML = icon(i.dataset.ic); i.dataset.painted = "1"; i.setAttribute("aria-hidden", "true"); });
}
paintIcons();
function metricsRow(id, items) {
  setHTMLIfChanged(document.getElementById(id), items.map(([label, value, ic, tone, title, goto]) =>
    `<div class="metric" data-tone-card="${tone}"${title ? ` title="${esc(title)}"` : ""}${goto ? ` data-mgoto="${goto}" tabindex="0" role="link"` : ""}><span class="metric-icon" aria-hidden="true">${icon(ic)}</span><div><div class="metric-label">${esc(label)}</div><div class="metric-value${String(value).length > 7 ? " small" : ""}">${esc(value)}</div></div></div>`).join(""));
}
// Klik na dlaždici se cílem (data-mgoto): „attn“ = panel Čeká na tebe, jinak stránka.
document.addEventListener("click", e => { const m = e.target.closest("[data-mgoto]"); if (!m) return;
  if (m.dataset.mgoto === "attn") { renderAttention(); document.getElementById("attnDialog").showModal(); } else showView(m.dataset.mgoto); });
document.addEventListener("keydown", e => { const m = e.target.closest && e.target.closest("[data-mgoto]"); if (m && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); m.click(); } });
function when(t) {
  if (t === null || t === undefined || t === "") return "";
  const d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
  if (isNaN(d)) return "";
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime(), today = day(new Date());
  const hm = d.toLocaleTimeString("cs-CZ", {hour: "2-digit", minute: "2-digit"});
  return day(d) === today ? "Dnes " + hm : day(d) === today - 86400000 ? "Včera " + hm : d.toLocaleDateString("cs-CZ", {day: "numeric", month: "numeric"}) + " " + hm;
}
function agentKey(a) { return a === "claude-code" || a === "claude-code-bridge" ? "ada" : a === "codex" || a === "codex-bridge" ? "nox" : ""; }
function whoBadge(a) { const k = agentKey(a); return `<span class="who-badge ${k || "other"}">${esc(k === "ada" ? "Ada" : k === "nox" ? "Nox" : nameOf(a))}</span>`; }
function actItem(agent, t, text) {
  const k = agentKey(agent), plain = String(text || "").replace(/[*`]/g, "");
  const face = k ? `<img class="act-portrait" src="/assets/${k}.webp" alt="">` : `<span class="act-portrait act-initial" aria-hidden="true">${esc(initial(agent))}</span>`;
  return `<li class="act" data-agent="${k || "human"}">${face}<div class="act-body"><div class="act-head"><b>${esc(k === "ada" ? "Ada" : k === "nox" ? "Nox" : nameOf(agent))}</b><time>${esc(when(t))}</time></div><p>${esc(plain.slice(0, 140))}${plain.length > 140 ? "…" : ""}</p></div></li>`;
}
function chipsHTML(items, current, attr) {
  return items.map(([id, label]) => `<button type="button" class="chip${id === current ? " on" : ""}" ${attr}="${esc(id)}" aria-pressed="${id === current}">${esc(label)}</button>`).join("");
}
const STEP_SR = {done: "hotovo", now: "probíhá", fail: "selhalo", warn: "vyžaduje kontrolu", "": "čeká"};
function stepperHTML(steps) {
  return `<ol class="stepper">${steps.map(([label, s]) => `<li class="${s}"><span class="node" aria-hidden="true">${s === "done" ? "✓" : s === "fail" ? "✕" : s === "warn" ? "!" : ""}</span>${esc(label)}<span class="sr-only">: ${STEP_SR[s] || s}</span></li>`).join("")}</ol>`;
}
function inlineMd(s) { return esc(s).replace(/[*][*](.+?)[*][*]/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>"); }
function mdLite(text) {
  const out = []; let list = false;
  for (const raw of String(text || "").split(String.fromCharCode(10))) {
    const line = raw.trimEnd(), li = /^[ ]*(?:[-*•]|[0-9]+[.)])[ ]+(.*)/.exec(line);
    if (li) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inlineMd(li[1])}</li>`); continue; }
    if (list) { out.push("</ul>"); list = false; }
    if (!line.trim()) continue;
    const h = /^#{1,6}[ ]+(.*)/.exec(line);
    out.push(h ? `<h4>${inlineMd(h[1])}</h4>` : `<p>${inlineMd(line)}</p>`);
  }
  if (list) out.push("</ul>");
  return `<div class="md">${out.join("")}</div>`;
}
function dedupeActivity(list) {
  const seen = new Set();   // Nox posílá výsledek úkolu i jako odpověď na každou navázanou zprávu
  return list.filter(m => { const k = m.agent + "|" + String(m.text || "").replace(/^(Done|Hotovo) #[0-9]+: */, "").slice(0, 120); return !seen.has(k) && seen.add(k); });
}
function renderPageActivity() {
  const items = dedupeActivity(historyMessages().filter(m => agentKey(m.agent) && !isSystem(m)).reverse()).slice(0, 8);
  const html = items.length ? items.map(m => actItem(m.agent, Date.now() / 1000 - (m.seconds_ago || 0), m.text)).join("") : "<li class='empty'>Čekám na zprávy Ady a Noxe.</li>";
  document.querySelectorAll('[data-activity="agents"]').forEach(el => setHTMLIfChanged(el, html));
}
function renderPages() {
  if (currentView === "quality") renderQualityGate();
  else if (currentView === "research") renderResearchPage();
  else if (currentView === "memory") renderMemoryPage();
  else if (currentView === "settings") renderSettings();
  if (["quality", "research"].includes(currentView)) renderPageActivity();
}

// ── Kvalita ──
let qgFilter = "all", rvFilter = "you";
const QG_OPEN = s => ["REVIEW", "SETUP_REQUIRED"].includes(s);
function qgLatest() {
  const seen = new Set();
  return (qgRecords || []).filter(r => !seen.has(r.repository) && seen.add(r.repository));
}
function renderQualityGate() {
  const latest = qgLatest(), all = latestReviewsAll || [];
  document.querySelectorAll("[data-qg-mode]").forEach(el => { el.textContent = qgRecords === null ? "Výsledky nejsou dostupné" : "Zaznamenané běhy kontrol"; });
  document.getElementById("qgCompactSummary").innerHTML = qgSummary(latest);
  document.querySelectorAll("[data-qg-state]").forEach(el => { el.title = QG_STATUS[el.dataset.qgState].meaning; });
  document.getElementById("qgCompactTable").innerHTML = qgTable(latest, {compact: true});
  document.querySelectorAll("#qgCompactTable [data-qg-project]").forEach(b => b.onclick = () => { qgSelected = latest[Number(b.dataset.qgProject)].run_id; showView("quality"); });
  const n = s => latest.filter(r => r.overall_status === s).length, known = qgRecords !== null;
  const approved = all.filter(r => r.verdict === "approved").length;
  metricsRow("qgMetrics", [
    ["Repozitáře", known ? latest.length : "—", "layers", "cyan"],
    ["Kontroly PASS", known ? n("PASS") : "—", "check", "green", "Poslední běh v repozitáři prošel všemi povinnými kontrolami"],
    ["Kontroly FAIL", known ? n("FAIL") : "—", "alert", "orange"],
    ["Ke kontrole", known ? latest.filter(r => QG_OPEN(r.overall_status)).length : "—", "flask", "purple"],
    ["Schválená review", all.length ? `${approved} / ${all.length}` : "—", "review", "purple", "Review se schvalujícím verdiktem ze všech zaznamenaných"],
    ["Čeká na tebe", latestReviewsAll === null ? "—" : all.filter(r => r.state === "escalated").length, "flag", "cyan", "Eskalovaná review po 3 kolech oprav", "attn"],
  ]);
  if (currentView !== "quality") return;
  const rows = latest.filter(r => qgFilter === "all" || (qgFilter === "open" ? QG_OPEN(r.overall_status) : r.overall_status === qgFilter));
  setHTMLIfChanged(document.getElementById("qgChips"), chipsHTML([["all", `Vše (${latest.length})`], ["PASS", `PASS (${n("PASS")})`], ["FAIL", `FAIL (${n("FAIL")})`], ["open", `Ke kontrole (${latest.filter(r => QG_OPEN(r.overall_status)).length})`]], qgFilter, "data-qg-filter"));
  document.querySelectorAll("[data-qg-filter]").forEach(b => b.onclick = () => { qgFilter = b.dataset.qgFilter; renderQualityGate(); });
  document.getElementById("qgRepoCount").textContent = known ? `(${latest.length})` : "";
  if (qgSelected === null || !latest.some(r => r.run_id === qgSelected)) qgSelected = rows[0] ? rows[0].run_id : null;
  setHTMLIfChanged(document.getElementById("qgProjectTable"), rows.length ? rows.map(r => {
    const name = String(r.repository || "").split("/").filter(Boolean).pop() || r.repository;
    return `<button type="button" class="pg-item${r.run_id === qgSelected ? " on" : ""}" data-qg-run="${r.run_id}" title="${esc(r.repository)}"><span class="pg-ic">${icon("shield")}</span><span class="pg-item-body"><span class="pg-item-title">${esc(name)}</span><span class="pg-item-sub">${esc(r.branch || "větev neuvedena")} · ${esc(when(r.timestamp))}</span><span class="pg-item-foot">${qgBadge(r.overall_status)}${r.agent ? whoBadge(r.agent) : ""}</span></span></button>`;
  }).join("") : `<div class="empty">${known ? "Žádný zaznamenaný běh kontrol pro tento filtr." : "Výsledky kontrol nejsou dostupné."}</div>`);
  document.querySelectorAll("[data-qg-run]").forEach(b => b.onclick = () => { qgSelected = Number(b.dataset.qgRun); renderQualityGate(); });
  const pending = qgRequests.filter(q => q.state !== "completed").slice(0, 5);
  const requestLabels = {queued: "čeká ve frontě", running: "kontroly běží", busy: "čeká na volný runner", refused: "je potřeba nastavení", interrupted: "běh byl přerušen"};
  setHTMLIfChanged(document.getElementById("qgRequests"), pending.length ? `<div class="qg-req"><b>Požadavky na kontrolu</b><ul>${pending.map(q => `<li>${esc(String(q.repository).split("/").pop())} · ${esc(requestLabels[q.state] || q.state)}${q.detail ? " · " + esc(q.detail) : ""}</li>`).join("")}</ul></div>` : "");
  renderQualityProject();
  // stav review
  const RS = [["approved", "Schválená", "ok", "✓"], ["awaiting_review", "Čeká na review", "work", "○"], ["changes_requested", "Požadované změny", "warn", "!"], ["escalated", "Čeká na tebe", "bad", "!"], ["closed", "Uzavřená", "", "–"]];
  setHTMLIfChanged(document.getElementById("qgReviewStats"), latestReviewsAll === null ? "<li class='empty'>Načítám…</li>" : RS.map(([s, label, tone, sym]) => `<li><span class="st-dot ${tone}" aria-hidden="true">${sym}</span>${label}<b>${all.filter(r => r.state === s).length}</b></li>`).join(""));
  const openAll = all.filter(r => r.state !== "closed").sort((a, b) => (b.state === "escalated") - (a.state === "escalated") || b.id - a.id);
  const RV = {you: r => r.state === "escalated", merge: r => r.state === "approved", work: r => ["awaiting_review", "changes_requested", "working"].includes(r.state), all: () => true};
  if (rvFilter === "you" && latestReviewsAll !== null && !openAll.some(RV.you)) rvFilter = "all";
  const open = openAll.filter(RV[rvFilter]);
  document.getElementById("qgOpenCount").textContent = latestReviewsAll === null ? "" : `(${openAll.length})`;
  setHTMLIfChanged(document.getElementById("rvChips"), chipsHTML([["you", `Čeká na tebe (${openAll.filter(RV.you).length})`], ["merge", `Schválené k uzavření (${openAll.filter(RV.merge).length})`], ["work", `Rozpracované (${openAll.filter(RV.work).length})`], ["all", "Vše"]], rvFilter, "data-rv-filter"));
  document.querySelectorAll("[data-rv-filter]").forEach(b => b.onclick = () => { rvFilter = b.dataset.rvFilter; renderQualityGate(); });
  setHTMLIfChanged(document.getElementById("qgOpenReviews"), open.length ? open.map(v => {
    const st = REVIEW[v.state] || [v.state, ""];
    return `<div class="rv-row"><span class="st-dot ${st[1] === "gone" ? "" : st[1]}" aria-hidden="true">${v.state === "approved" ? "✓" : v.state === "escalated" || v.state === "changes_requested" ? "!" : "○"}</span><div class="rv-main"><b title="${esc(v.topic)}">#${v.id} ${esc(v.topic)}</b><span>${esc(String(v.repo || "").split("/").pop())} · ${esc(nameOf(v.author))}${v.reviewer ? " → " + esc(nameOf(v.reviewer)) : ""} · ${esc(st[0])}</span></div>${v.state === "escalated" ? `<button class="btn" data-rv-grant="${v.id}">Povolit kolo</button>` : ""}<button class="btn" data-rv-close="${v.id}" data-rv-state="${esc(v.state)}">Uzavřít</button></div>`;
  }).join("") : `<div class="empty">${latestReviewsAll === null ? "Načítám…" : rvFilter === "all" ? "Žádné otevřené review." : "V této skupině nic není."}</div>`);
  document.querySelectorAll("[data-rv-close]").forEach(b => b.onclick = () => {
    const reason = prompt(`Proč uzavřít review #${b.dataset.rvClose}? (zapíše se do historie)`, b.dataset.rvState === "approved" ? "zamergováno" : "nahrazeno novějším review");
    if (reason && reason.trim()) { b.disabled = true; post("/api/review-close", {id: Number(b.dataset.rvClose), reason}).then(ok => { if (!ok) b.disabled = false; }); }
  });
  document.querySelectorAll("[data-rv-grant]").forEach(b => b.onclick = () => {
    const reason = prompt("Proč povolit další kolo oprav? (zapíše se do historie)");
    if (reason && reason.trim()) { b.disabled = true; post("/api/review-grant", {id: Number(b.dataset.rvGrant), reason}).then(ok => { if (!ok) b.disabled = false; }); }
  });
}
function renderQualityProject() {
  const r = qgSelected === null ? null : (qgRecords || []).find(x => x.run_id === qgSelected);
  const el = document.getElementById("qgProjectDetail");
  document.getElementById("qgProjectTitle").textContent = r ? String(r.repository).split("/").filter(Boolean).pop() : "Detail kontroly";
  document.getElementById("qgLastCheck").textContent = r ? when(r.timestamp) : "";
  if (!r) { setHTMLIfChanged(el, `<div class="empty">${qgRecords === null ? "Výsledky nejsou dostupné." : "Vyber kontrolu ze seznamu."}</div>`); return; }
  const tone = s => s === "PASS" ? "done" : s === "FAIL" ? "fail" : s === "REVIEW" ? "warn" : "";
  const history = (qgRecords || []).filter(x => x.repository === r.repository && x.run_id !== r.run_id).slice(0, 6);
  const canApprove = r.policy_review_required && r.policy_review_status !== "approved" && r.review_status === "approved" && Object.values(r.checks || {}).every(c => !c.required || c.status === "PASS");
  const executionLabels = {not_started: "Kontroly nebyly spuštěny", busy: "Runner je obsazený", unverified: "Průběh kontrol nelze ověřit", interrupted: "Kontroly byly přerušeny", completed: "Kontroly byly dokončeny"};
  setHTMLIfChanged(el, `<div class="pg-progress">Celkový stav ${qgBadge(r.overall_status)}<span class="h-sub">${esc(r.repository)}</span></div>`
    + (r.execution_status ? `<p class="pg-desc">${esc(executionLabels[r.execution_status] || "Stav spuštění není známý")}${r.error ? ": " + esc(r.error) : ""}</p>` : "")
    + stepperHTML(QG_CHECKS.map(([key, label]) => [label, tone(r.checks?.[key]?.status)]))
    + `<dl class="kv qg-kv"><dt>Větev</dt><dd>${esc(r.branch || "neuvedena")}</dd><dt>Commit</dt><dd class="path">${esc(r.commit_sha || "neuveden")}</dd><dt>Agent</dt><dd>${esc(r.agent ? nameOf(r.agent) : "neuveden")}</dd><dt>Review</dt><dd>${esc(r.review_status === "approved" ? "schváleno pro tento commit" : r.review_status === "pending" ? "čeká na nezávislé review" : r.review_status || "není dostupné")}${r.reviewer ? " · " + esc(nameOf(r.reviewer)) : ""}</dd><dt>Změny testů a pravidel</dt><dd>${r.policy_review_required ? r.policy_review_status === "approved" ? "kritické změny schválil člověk" : "kritické změny čekají na tvé schválení" : "schválení není potřeba"}</dd></dl>`
    + `<div class="qg-check-grid">${QG_CHECKS.map(([key, label]) => { const c = r.checks?.[key]; return `<section class="card"><h3>${label}</h3>${qgBadge(c?.status || "NOT_RUN")}<p>${esc(c?.value || c?.detail || "Výsledek není dostupný.")}</p></section>`; }).join("")}</div>`
    + (canApprove ? '<div class="pg-actions"><button class="btn" id="qgApprovePolicy">Schválit kritické změny</button></div>' : "")
    + (qgLogs(r) ? `<div class="detail-section"><h3>Protokoly</h3>${qgLogs(r)}</div>` : "")
    + (history.length ? `<div class="detail-section"><h3>Předchozí běhy</h3><ul>${history.map(h => `<li>${esc(when(h.timestamp))} · ${qgBadge(h.overall_status)} · ${esc((h.commit_sha || "").slice(0, 10))}</li>`).join("")}</ul></div>` : ""));
  const approve = document.getElementById("qgApprovePolicy");
  if (approve) approve.onclick = () => {
    const reason = prompt("Proč schvaluješ změny testů nebo pravidel pro tento commit?");
    if (!reason || reason.trim().length < 10 || !confirm("Schválit kritické změny pro commit " + r.commit_sha + "?")) return;
    approve.disabled = true;
    post("/api/quality-policy-approve", {run_id: r.run_id, commit_sha: r.commit_sha, reason}).then(ok => { if (!ok) approve.disabled = false; });
  };
}

// ── Výzkum ──
const RS_LABEL = {running: "Probíhající výzkum", synthesizing: "Syntéza", done: "Hotovo", partial: "Částečně", failed: "Selhalo", cancelled: "Zrušeno"};
const RS_FIN = ["succeeded", "failed", "dead_letter", "cancelled", "needs_review"];
let rsSel = null, rsFilter = "all", rsTab = "summary";
function loadResearch() {
  fetch("/api/research").then(r => r.ok ? r.json() : Promise.reject(r)).then(d => { dashResearch = d.research || []; renderResearchPage(); })
    .catch(() => { document.getElementById("rsList").textContent = "Výzkum se nepodařilo načíst."; });
}
const rsParts = r => (r.parts || []).filter(p => p.role === "part");
function rsProgress(r) {
  if (r.state === "done") return 100;
  const parts = rsParts(r), fin = parts.filter(p => RS_FIN.includes(p.state)).length;
  return Math.round(100 * (fin + (r.state === "partial" ? 1 : 0)) / (parts.length + 1));
}
function rsSteps(r) {
  const st = r.state, stopped = ["failed", "cancelled"].includes(st);
  return [["Zadání", "done"], ["Části", st === "running" ? "now" : stopped && !r.synth_id ? "fail" : "done"],
    ["Syntéza", st === "synthesizing" ? "now" : ["done", "partial"].includes(st) ? "done" : stopped && r.synth_id ? "fail" : ""],
    ["Hotovo", st === "done" ? "done" : st === "partial" ? "warn" : stopped ? "fail" : ""]];
}
const URL_RE = /https?:[/][/][^ <>()"'`]+/g, FILE_RE = /(?:^|[ (`])((?:[A-Za-z0-9_.-]+[/])*[A-Za-z0-9_.-]+[.](?:md|py|ts|tsx|js|cjs|json|toml|rs|yml|yaml))(?![A-Za-z0-9_])/g;
const flat = t => String(t || "").split(String.fromCharCode(10)).join(" ").split(String.fromCharCode(9)).join(" ");
const trimUrl = u => { while (u && ".,;:!?)]".includes(u.slice(-1))) u = u.slice(0, -1); return u; };
function rsRefs(r) {
  const text = flat([r.question, r.summary].join(" "));
  const urls = [...new Set((text.match(URL_RE) || []).map(trimUrl))];
  const files = [...new Set([...text.matchAll(FILE_RE)].map(m => m[1]))].filter(f => !urls.some(u => u.includes(f)));
  return {urls, files};
}
function renderResearchPage() {
  const runs = dashResearch;
  const kb = dashKnowledge;
  const active = (runs || []).filter(r => ["running", "synthesizing"].includes(r.state));
  const fin = (runs || []).filter(r => ["done", "partial", "failed", "cancelled"].includes(r.state));
  const allRefs = new Set(); (runs || []).forEach(r => { const x = rsRefs(r); x.urls.forEach(u => allRefs.add(u)); });
  const spend = (runs || []).reduce((s, r) => s + (r.spend_tokens || 0), 0);
  metricsRow("rsMetrics", [
    ["Aktivní výzkumy", runs ? active.length : "—", "search", "cyan"],
    ["Probíhající části", runs ? runs.reduce((s, r) => s + rsParts(r).filter(p => !RS_FIN.includes(p.state)).length, 0) : "—", "flask", "orange"],
    ["Dokončené", runs ? runs.filter(r => ["done", "partial"].includes(r.state)).length : "—", "check", "green"],
    ["Citované zdroje", runs ? allRefs.size : "—", "link", "purple", "Odkazy uvedené v zadáních a souhrnech"],
    ["Spotřeba tokenů", runs ? spend.toLocaleString("cs-CZ") : "—", "bars", "purple", "Součet spotřeby posledních výzkumů"],
    ["Úspěšnost", runs && fin.length ? Math.round(100 * runs.filter(r => r.state === "done").length / fin.length) + " %" : "—", "star", "cyan", "Hotové bez výpadku ze všech ukončených"],
  ]);
  if (currentView !== "research") return;
  if (runs === null) return;
  const F = {all: () => true, active: r => ["running", "synthesizing"].includes(r.state), done: r => ["done", "partial"].includes(r.state), failed: r => ["failed", "cancelled"].includes(r.state)};
  const list = runs.filter(F[rsFilter]);
  setHTMLIfChanged(document.getElementById("rsChips"), chipsHTML([["all", `Vše (${runs.length})`], ["active", `Probíhající (${runs.filter(F.active).length})`], ["done", `Dokončené (${runs.filter(F.done).length})`], ["failed", `Neúspěšné (${runs.filter(F.failed).length})`]], rsFilter, "data-rs-filter"));
  document.querySelectorAll("[data-rs-filter]").forEach(b => b.onclick = () => { rsFilter = b.dataset.rsFilter; renderResearchPage(); });
  if (rsSel === null || !runs.some(r => r.id === rsSel)) rsSel = (active[0] || runs[0] || {}).id ?? null;
  setHTMLIfChanged(document.getElementById("rsList"), list.length ? list.map(r => {
    const p = rsProgress(r), who = [...new Set(rsParts(r).map(x => x.recipient).filter(Boolean))];
    return `<button type="button" class="pg-item${r.id === rsSel ? " on" : ""}" data-rs="${r.id}"><span class="pg-ic">${icon(["running", "synthesizing"].includes(r.state) ? "search" : r.state === "done" ? "check" : "cube")}</span><span class="pg-item-body"><span class="pg-item-title">${esc(r.question)}</span><span class="pg-item-sub">#${r.id} · ${esc(RS_LABEL[r.state] || r.state)} · ${esc(when(r.created_at))}</span><span class="pg-item-foot">${(who.length ? who : [r.requester]).map(whoBadge).join("")}<span class="bar"><i style="width:${p}%"></i></span><b>${p} %</b></span></span></button>`;
  }).join("") : `<div class="empty">${runs.length ? "Pro tento filtr nic není." : "Zatím žádný výzkum. Zadej ho tlačítkem Nový výzkum."}</div>`);
  document.querySelectorAll("[data-rs]").forEach(b => b.onclick = () => { rsSel = Number(b.dataset.rs); renderResearchPage(); });
  const r = runs.find(x => x.id === rsSel), det = document.getElementById("rsDetail");
  if (!r) { setHTMLIfChanged(det, "<div class='empty'>Vyber výzkum ze seznamu.</div>"); setHTMLIfChanged(document.getElementById("rsParts"), ""); setHTMLIfChanged(document.getElementById("rsLinks"), ""); return; }
  const p = rsProgress(r), parts = rsParts(r), done = parts.filter(x => x.state === "succeeded").length, refs = rsRefs(r);
  const tone = r.state === "done" ? "ok" : ["failed", "cancelled"].includes(r.state) ? "bad" : r.state === "partial" ? "muted" : "";
  const concl = r.state === "done" ? ["", "Výzkum je hotový", "Zjištění jsou neověřená, dokud je nepotvrdí jiný agent nebo člověk v paměti."]
    : r.state === "partial" ? ["warn", "Souhrn je neúplný", "Část podtémat nedoběhla včas" + (r.reason ? ": " + r.reason : ".")]
    : ["failed", "cancelled"].includes(r.state) ? ["warn", RS_LABEL[r.state], r.reason || "Důvod není zaznamenaný."]
    : ["", "Výzkum běží", `Hotovo ${done} z ${parts.length} částí${r.state === "synthesizing" ? ", běží syntéza" : ""}. Termín ${when(r.deadline_at)}.`];
  const tabs = [["summary", "Hlavní zjištění"], ["question", "Zadání"], ["details", "Detaily"]];
  const body = rsTab === "summary" ? (r.summary ? mdLite(r.summary) : `<p class="note">${["running", "synthesizing"].includes(r.state) ? "Souhrn vznikne po syntéze." : "Souhrn není k dispozici."}</p>`)
    : rsTab === "question" ? `<p style="white-space:pre-wrap">${esc(r.question)}</p>`
    : `<dl class="kv"><dt>Zadal</dt><dd>${esc(nameOf(r.requester))}</dd><dt>Druh</dt><dd>${esc(r.kind)}</dd>${r.model ? `<dt>Model</dt><dd>${esc(r.model)}</dd>` : ""}${r.repo ? `<dt>Repozitář</dt><dd class="path">${esc(r.repo)}${r.repo_commit ? " @ " + esc(r.repo_commit.slice(0, 10)) : ""}</dd>` : ""}<dt>Tokeny</dt><dd>${esc((r.spend_tokens || 0).toLocaleString("cs-CZ"))} z ${esc((r.budget_tokens || 0).toLocaleString("cs-CZ"))}</dd><dt>Zadáno</dt><dd>${esc(when(r.created_at))}</dd><dt>Termín</dt><dd>${esc(when(r.deadline_at))}</dd>${r.finished_at ? `<dt>Dokončeno</dt><dd>${esc(when(r.finished_at))}</dd>` : ""}${r.reason ? `<dt>Poznámka</dt><dd>${esc(r.reason)}</dd>` : ""}</dl>`;
  setHTMLIfChanged(det, `<div class="pg-head">${icon("search")}<h2 title="${esc(r.question)}">${esc(r.question)}</h2><span class="pill ${tone}">${esc(RS_LABEL[r.state] || r.state)}</span></div>`
    + `<p class="pg-desc">#${r.id} · ${esc(r.kind)} · zadal ${esc(nameOf(r.requester))}</p>`
    + `<div class="pg-progress">Celkový postup<span class="bar" role="progressbar" aria-valuenow="${p}" aria-valuemin="0" aria-valuemax="100" aria-label="Postup výzkumu"><i style="width:${p}%"></i></span><b>${p} %</b></div>`
    + stepperHTML(rsSteps(r))
    + `<div class="tabs" role="tablist">${tabs.map(([id, l]) => `<button type="button" role="tab" data-rs-tab="${id}" aria-selected="${id === rsTab}">${l}</button>`).join("")}</div>`
    + `<div class="tab-body" role="tabpanel">${body}</div>`
    + `<div class="conclusion ${concl[0]}">${icon("flag")}<div><b>${esc(concl[1])}</b>${esc(concl[2])}</div></div>`
    + (["running", "synthesizing"].includes(r.state) ? '<div class="pg-actions"><button class="btn" data-rs-cancel>✕ Zrušit výzkum</button></div>' : ""));
  det.querySelectorAll("[data-rs-tab]").forEach(b => b.onclick = () => { rsTab = b.dataset.rsTab; renderResearchPage(); });
  const cancel = det.querySelector("[data-rs-cancel]");
  if (cancel) cancel.onclick = () => { if (!confirm("Zrušit výzkum včetně běžících částí a syntézy?")) return; cancel.disabled = true; post("/api/research-cancel", {id: r.id}).then(() => loadResearch()); };
  document.getElementById("rsPartsCount").textContent = `(${parts.length})`;
  const PART = s => s === "succeeded" ? ["ok", "✓"] : ["failed", "dead_letter", "cancelled"].includes(s) ? ["bad", "✕"] : s === "needs_review" ? ["warn", "!"] : ["started", "leased"].includes(s) ? ["work", "▸"] : ["", "○"];
  setHTMLIfChanged(document.getElementById("rsParts"), parts.length ? parts.map(x => { const [t, sym] = PART(x.state); return `<li class="src-row"><span class="st-dot ${t}" aria-hidden="true">${sym}</span><span class="task-text" title="${esc(x.topic)}">${esc(x.topic)}</span>${x.recipient ? whoBadge(x.recipient) : ""}<span class="sr-only">${esc(x.state || "neznámý stav")}</span></li>`; }).join("") : "<li class='empty'>Výzkum nemá samostatné části.</li>");
  setHTMLIfChanged(document.getElementById("rsLinks"), refs.urls.length || refs.files.length
    ? refs.urls.map(u => `<a class="link-row" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${icon("link")}<span>${esc(u.replace(/^https?:[/][/]/, ""))}</span>↗</a>`).join("") + refs.files.slice(0, 12).map(f => `<div class="link-row">${icon("doc")}<span title="${esc(f)}">${esc(f)}</span></div>`).join("")
    : "<div class='empty'>Souhrn neuvádí žádné odkazy ani soubory.</div>");
  void kb;
}
document.getElementById("rsNew").onclick = () => {
  const q = prompt("Na co se má výzkum zaměřit? Úkol dostane Ada a spustí ho nástrojem research.");
  if (!q || !q.trim()) return;
  post("/api/send", {kind: "task", to: "claude-code", text: "Spusť prosím výzkum nástrojem research a pak mi shrň výsledek: " + q.trim()})
    .then(ok => { if (ok) alert("Úkol jsem poslal Adě. Výzkum se tady objeví, jakmile ho spustí."); });
};

// ── Paměť ──
const KB_LABEL = {verified: "ověřeno", unverified: "neověřeno — jen tip", rejected: "zamítnuto", stale: "zastaralé"};
let kbTimer = null, kbStatusSel = "", kbProjectSel = null, kbSel = null, kbEntries = [];
function loadKnowledge() {
  const q = document.getElementById("kbQuery").value.trim();
  const params = new URLSearchParams({...(q ? {q} : {}), ...(kbStatusSel ? {status: kbStatusSel} : {}), ...(kbProjectSel !== null ? {project: kbProjectSel} : {})});
  fetch("/api/knowledge?" + params).then(r => r.ok ? r.json() : Promise.reject(r))
    .then(d => { kbEntries = d.entries || []; renderKnowledgeList(); })
    .catch(() => { document.getElementById("kbList").textContent = "Paměť se nepodařilo načíst."; });
  fetch("/api/knowledge").then(r => r.ok ? r.json() : null).then(d => { if (d) { dashKnowledge = d.entries || []; renderMemoryPage(); } }).catch(() => {});
}
function renderMemoryPage() {
  const all = dashKnowledge, n = s => (all || []).filter(e => e.status === s).length;
  const projects = new Map(); (all || []).forEach(e => projects.set(e.project || "", (projects.get(e.project || "") || 0) + 1));
  metricsRow("kbMetrics", [
    ["Uložené poznatky", all ? (all.length >= 100 ? "100+" : all.length) : "—", "db", "cyan"],
    ["Ověřené", all ? n("verified") : "—", "check", "green", "Za fakt platí jen ověřené záznamy"],
    ["Neověřené tipy", all ? n("unverified") : "—", "flask", "orange"],
    ["Zamítnuté", all ? n("rejected") + n("stale") : "—", "alert", "purple"],
    ["Projekty", all ? [...projects.keys()].filter(Boolean).length : "—", "folder", "purple"],
    ["Nové za 7 dní", all ? all.filter(e => Date.now() / 1000 - e.created_at < 7 * 86400).length : "—", "bars", "cyan"],
  ]);
  if (currentView !== "memory") return;
  const projItems = [[null, "Vše", all ? all.length : 0], ...[...projects.entries()].sort((a, b) => b[1] - a[1]).map(([p, c]) => [p, p || "Obecné", c])];
  setHTMLIfChanged(document.getElementById("kbProjects"), all === null ? "Načítám…" : projItems.map(([p, label, c]) =>
    `<button type="button" class="pg-item${p === kbProjectSel ? " on" : ""}" data-kb-proj="${p === null ? "__all__" : esc(p)}"><span class="pg-ic">${icon("folder")}</span><span class="pg-item-body"><span class="pg-item-title">${esc(label)}</span><span class="pg-item-sub">${p === null ? "Celá sdílená paměť" : `${(all || []).filter(e => (e.project || "") === p && e.status === "verified").length} ověřených`}</span></span><span class="count-pill">${c} ${c === 1 ? "záznam" : c >= 2 && c <= 4 ? "záznamy" : "záznamů"}</span></button>`).join("")
    + (all && !all.length ? "<p class='note'>Paměť je zatím prázdná. Agenti do ní ukládají ověřená řešení chyb; můžeš přidat i vlastní.</p>" : ""));
  document.querySelectorAll("[data-kb-proj]").forEach(b => b.onclick = () => { kbProjectSel = b.dataset.kbProj === "__all__" ? null : b.dataset.kbProj; kbSel = null; loadKnowledge(); });
  setHTMLIfChanged(document.getElementById("kbStatus"), chipsHTML([["", "Vše"], ["verified", "Ověřené"], ["unverified", "Neověřené"], ["rejected", "Zamítnuté"], ["stale", "Zastaralé"]], kbStatusSel, "data-kb-status"));
  document.querySelectorAll("[data-kb-status]").forEach(b => b.onclick = () => { kbStatusSel = b.dataset.kbStatus; kbSel = null; loadKnowledge(); });
  document.getElementById("kbProjectList").innerHTML = [...projects.keys()].filter(Boolean).map(p => `<option value="${esc(p)}">`).join("");
  const act = [...(all || [])].sort((a, b) => b.updated_at - a.updated_at).slice(0, 8);
  setHTMLIfChanged(document.getElementById("kbActivity"), act.length ? act.map(e => e.verified_by && e.updated_at > e.created_at
    ? actItem(e.verified_by, e.updated_at, `${KB_LABEL[e.status] || e.status}: ${e.symptom}`)
    : actItem(e.author, e.created_at, `Uložil(a) poznatek: ${e.symptom}`)).join("") : "<li class='empty'>V paměti zatím nic není.</li>");
  renderKnowledgeList();
}
function renderKnowledgeList() {
  const el = document.getElementById("kbList"), q = document.getElementById("kbQuery").value.trim();
  document.getElementById("kbCount").textContent = `(${kbEntries.length})`;
  document.getElementById("kbScope").textContent = [kbProjectSel !== null ? (kbProjectSel || "Obecné") : "", q ? `„${q}“` : ""].filter(Boolean).join(" · ");
  if (kbSel !== null && !kbEntries.some(e => e.id === kbSel)) kbSel = null;
  if (kbSel === null && kbEntries.length) kbSel = kbEntries[0].id;
  setHTMLIfChanged(el, kbEntries.length ? kbEntries.map(e => `<button type="button" class="kb-row${e.id === kbSel ? " on" : ""}" data-kb-id="${e.id}"><span class="st-dot ${e.status === "verified" ? "ok" : e.status === "rejected" ? "bad" : e.status === "stale" ? "" : "warn"}" aria-hidden="true">${e.status === "verified" ? "✓" : e.status === "rejected" ? "✕" : e.status === "stale" ? "–" : "?"}</span><span class="task-text" title="${esc(e.symptom)}">${esc(e.symptom)}</span>${e.project ? `<span class="kb-proj">${esc(e.project)}</span>` : ""}<time>${esc(when(e.updated_at))}</time><span class="sr-only">${esc(KB_LABEL[e.status] || e.status)}</span></button>`).join("")
    : `<p class="note">${q || kbStatusSel || kbProjectSel !== null ? "Nic neodpovídá hledání." : "Žádné záznamy."}</p>`);
  el.querySelectorAll("[data-kb-id]").forEach(b => b.onclick = () => { kbSel = Number(b.dataset.kbId); renderKnowledgeList(); });
  const e = kbEntries.find(x => x.id === kbSel), det = document.getElementById("kbDetail");
  document.getElementById("kbDetailTitle").textContent = e ? `Záznam #${e.id}` : "Detail záznamu";
  if (!e) { setHTMLIfChanged(det, "<div class='empty'>Vyber záznam ze seznamu.</div>"); return; }
  setHTMLIfChanged(det, `<div class="kb-detail"><div class="tags"><span class="kb-state ${esc(e.status)}">${esc(KB_LABEL[e.status] || e.status)}</span>${e.project ? `<span class="kb-proj">${esc(e.project)}</span>` : ""}</div>
    <dl><dt>Příznak</dt><dd>${esc(e.symptom)}</dd>${e.cause ? `<dt>Příčina</dt><dd>${esc(e.cause)}</dd>` : ""}${e.solution ? `<dt>Řešení</dt><dd>${esc(e.solution)}</dd>` : ""}${e.evidence ? `<dt>Důkaz</dt><dd>${esc(e.evidence)}</dd>` : ""}${e.verdict_evidence ? `<dt>Důkaz posudku</dt><dd>${esc(e.verdict_evidence)}${e.verdict_note ? " — " + esc(e.verdict_note) : ""}</dd>` : ""}<dt>Autor</dt><dd>${esc(nameOf(e.author))} · ${esc(when(e.created_at))}${e.verified_by ? ` · posoudil ${esc(nameOf(e.verified_by))}` : ""}</dd></dl>
    <div class="pg-actions" style="padding:0"><button class="btn" data-kb="verified">✓ Ověřit</button><button class="btn" data-kb="rejected">✕ Nefunguje</button><button class="btn" data-kb="stale">◷ Zastaralé</button></div></div>`);
  det.querySelectorAll("[data-kb]").forEach(b => b.onclick = () => {
    const evidence = prompt("Důkaz (commit, test, odkaz) — povinné:");
    if (!evidence || !evidence.trim()) return;
    b.disabled = true;
    post("/api/knowledge-verdict", {id: e.id, verdict: b.dataset.kb, evidence}).then(ok => { b.disabled = false; if (ok) loadKnowledge(); });
  });
}
document.getElementById("kbQuery").addEventListener("input", () => { clearTimeout(kbTimer); kbTimer = setTimeout(loadKnowledge, 250); });
document.getElementById("kbNew").onclick = () => document.getElementById("kbDialog").showModal();
document.getElementById("kbAdd").onclick = () => {
  const val = id => document.getElementById(id).value;
  if (!val("kbSymptom").trim()) { document.getElementById("kbSymptom").focus(); return; }
  const btn = document.getElementById("kbAdd"); btn.disabled = true;
  post("/api/knowledge-add", {symptom: val("kbSymptom"), cause: val("kbCause"), solution: val("kbSolution"), project: val("kbProject"), evidence: val("kbEvidence")})
    .then(ok => { btn.disabled = false; if (ok) { ["kbSymptom", "kbCause", "kbSolution", "kbEvidence"].forEach(i => document.getElementById(i).value = ""); document.getElementById("kbDialog").close(); loadKnowledge(); } });
};

// ── Nastavení ──
const MODES = [
  {id: "basic", name: "Základní", items: ["Bridge sám nepřebírá žádné úkoly", "Pracuje jen session, kterou spustíš", "Sdílená paměť vypnutá pro agenty", "Minimální spotřeba tokenů"]},
  {id: "intelligent", name: "Inteligentní", items: ["Bridge přebírá read-only úkoly", "Review mezi agenty (max 3 kola)", "Sdílená paměť zapnutá"]},
  {id: "autonomous", name: "Autonomní", items: ["Opravy v izolovaném worktree", "Povinné review, rozpočtové stropy", "Eskalace na tebe po 3 pokusech"]},
];
const MODE_NAME = {basic: "Základní", intelligent: "Inteligentní", autonomous: "Autonomní"};
let lastMode = null, latestClaims = [];
function renderMode(m) {
  if (!m) return;
  lastMode = m;
  document.getElementById("modeText").textContent = "Režim: " + (MODE_NAME[m.mode] || m.mode);
  const grid = document.getElementById("modeGrid");
  setHTMLIfChanged(grid, MODES.map(x => { const lock = m.locked && m.locked[x.id]; const on = m.mode === x.id;
    return `<button type="button" class="mode-card" role="radio" data-mode="${x.id}" aria-checked="${on}" aria-disabled="${!!lock}"><h3><span class="mode-dot ${x.id}" aria-hidden="true"></span>${x.name}${on ? " · aktivní" : ""}</h3><ul>${x.items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>${lock ? `<p class="note">🔒 ${esc(lock)}</p>` : ""}</button>`; }).join(""));
  document.getElementById("modeInfo").textContent = m.by ? `Naposledy přepnul ${nameOf(m.by)} · ${when(m.at)}${m.reason ? " · " + m.reason : ""}` : "Výchozí režim (zatím nikdo nepřepínal).";
  grid.querySelectorAll("[data-mode]").forEach(b => b.onclick = () => {
    if (b.getAttribute("aria-disabled") === "true" || b.getAttribute("aria-checked") === "true") return;
    const reason = prompt("Proč přepínáš režim? (zapíše se do historie)");
    if (reason === null) return;
    fetch("/api/mode", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({mode: b.dataset.mode, reason})})
      .then(r => r.ok ? r.json().then(x => { renderMode(x); renderSettings(); }) : r.text().then(t => alert(t)))
      .catch(() => alert("Spojení se nezdařilo. Zkontroluj režim před opakováním."));
  });
}
function loadMode() { fetch("/api/snapshot?since=" + Number.MAX_SAFE_INTEGER).then(r => r.json()).then(s => { renderMode(s.mode); renderSettings(); }).catch(() => {}); }
function renderSettings() {
  const st = {ada: agentStatus("ada"), nox: agentStatus("nox")};
  const peer = a => latestPeers.find(p => p.agent === a);
  // Stejná ochrana jako agentStatus: po výpadku spojení / >60 s bez dat most nesmí zůstat „online" (review #54).
  const stale = connectionMode !== "live" || !lastRoomAt || Date.now() - lastRoomAt > STALE_MS;
  const bridgeOn = a => { const p = peer(a); return !stale && !!(p && p.active && p.seen_seconds_ago != null && p.seen_seconds_ago < ONLINE_S); };
  const bridgeText = a => stale ? "most: stav neznámý" : "most " + (bridgeOn(a) ? "online" : "offline");
  const all = latestReviewsAll || [];
  metricsRow("setMetrics", [
    ["Provozní režim", lastMode ? MODE_NAME[lastMode.mode] || lastMode.mode : "—", "sliders", "cyan"],
    ["Provoz", lastPaused === undefined ? "—" : lastPaused ? "Zastaveno" : "Povolen", "stop", lastPaused ? "orange" : "green"],
    ["Agenti online", stale ? "—" : `${["ada", "nox"].filter(k => !["offline", "unknown"].includes(st[k].state)).length} / 2`, "users", "purple"],
    ["Mosty online", stale ? "—" : `${["claude-code-bridge", "codex-bridge"].filter(bridgeOn).length} / 2`, "bridge", "purple", "Mosty přebírají úkoly na pozadí"],
    ["Zamčené cesty", latestClaims.length, "lock", "orange"],
    ["Review k rozhodnutí", latestReviewsAll === null ? "—" : all.filter(r => r.state === "escalated").length, "flag", "cyan", "Otevřít Čeká na tebe", "attn"],
  ]);
  if (currentView !== "settings") return;
  const INFO = {ada: ["Ada", "Anthropic (Claude)", "Analýza · Výzkum · Návrhy", "claude-code-bridge"], nox: ["Nox", "OpenAI (GPT)", "Implementace · Testy · Ověření", "codex-bridge"]};
  const TONE = {working: "work", idle: "ok", limited: "bad", error: "warn", offline: "", unknown: "warn"};
  setHTMLIfChanged(document.getElementById("setAgentList"), ["ada", "nox"].map(k => { const [name, vendor, role, bridge] = INFO[k], s = st[k];
    return `<div class="set-agent" data-agent="${k}"><img src="/assets/${k}.webp" alt=""><div class="set-agent-body"><b>${name}</b> <span class="note">${vendor}</span><p>${role}</p>${s.detail ? `<p title="${esc(s.detail)}">${esc(s.detail.slice(0, 120))}</p>` : ""}<div class="tags">${tag(s.label, TONE[s.state])}${tag(bridgeText(bridge), stale ? "warn" : bridgeOn(bridge) ? "ok" : "")}</div></div></div>`; }).join(""));
  if (lastMode) renderMode(lastMode);
  const p = lastPaused;
  setHTMLIfChanged(document.getElementById("setStopBody"), p === undefined ? "<div class='empty'>Stav není známý.</div>"
    : `<div class="stop-box"><p>${p ? `⏸ <b>Agenti jsou zastavení.</b> Zastavil ${esc(nameOf(p.by))}${p.at ? " " + esc(when(p.at)) : ""}${p.reason ? " · " + esc(p.reason) : ""}.` : "● <b>Provoz povolen.</b> Zastavení okamžitě zastaví všechnu práci na pozadí, rozpracované úkoly se nezahodí."}</p><button class="btn ${p ? "" : "danger"}" id="setStopBtn">${p ? "▶ Pustit agenty znovu" : "Zastavit všechny agenty"}</button></div>`);
  const sb = document.getElementById("setStopBtn");
  if (sb) sb.onclick = () => { if (p) { if (confirm("Pustit agenty znovu do samostatné práce?")) pauseCall("resume", ""); } else { const r = prompt("Opravdu zastavit všechny agenty? Napiš důvod (zaznamená se):", "nouzové zastavení"); if (r !== null) pauseCall("pause", r); } };
  const ev = [];
  if (lastMode && lastMode.by) ev.push([lastMode.by, lastMode.at, `Přepnul(a) režim na ${MODE_NAME[lastMode.mode] || lastMode.mode}${lastMode.reason ? ": " + lastMode.reason : ""}`]);
  if (p && p.by) ev.push([p.by, p.at, `Zastavil(a) agenty${p.reason ? ": " + p.reason : ""}`]);
  historyMessages().filter(m => m.agent === me || m.kind === "control").slice(-12).forEach(m => ev.push([m.agent, Date.now() / 1000 - (m.seconds_ago || 0), `${m.kind === "task" ? "Úkol pro " + nameOf(m.recipient) : m.kind === "control" ? "Řízení" : "Zpráva pro " + nameOf(m.recipient)}: ${m.text}`]));
  ev.sort((a, b) => (b[1] || 0) - (a[1] || 0));
  setHTMLIfChanged(document.getElementById("setActivity"), ev.length ? ev.slice(0, 8).map(([a, t, x]) => actItem(a, t, x)).join("") : "<li class='empty'>Zatím žádná řídicí akce.</li>");
}
// Stránky se obnovují i bez nové zprávy: stáří dat (Nastavení) a průběh výzkumu/paměti (review #54).
setInterval(() => { if (!document.hidden) renderPages(); }, 5000);
// Server pošle „room“ hned po každém zápisu do DB; při změně verze se obnoví i výzkum, paměť a provoz.
let lastDataVersion = null, versionReloadAt = 0;
function onDataVersion(v) {
  if (v === undefined || v === lastDataVersion) return;
  const first = lastDataVersion === null;
  lastDataVersion = v;
  if (first || document.hidden || Date.now() - versionReloadAt < 1500) return;
  versionReloadAt = Date.now();
  if (currentView === "research") loadResearch(); else if (currentView === "memory") loadKnowledge();
  else if (currentView === "ops") loadOps(); else if (currentView === "overview") loadDashData();
}
setInterval(() => { if (document.hidden) return; if (currentView === "research") loadResearch(); else if (currentView === "memory") loadKnowledge(); }, 30000);
document.querySelectorAll(".set-nav a").forEach(a => a.onclick = ev => {
  ev.preventDefault();
  document.querySelectorAll(".set-nav a").forEach(x => x.classList.toggle("on", x === a));
  const target = document.querySelector(a.getAttribute("href"));
  target.scrollIntoView({behavior: reducedMotion.matches ? "auto" : "smooth", block: "nearest"});
  motion(target, [{boxShadow: "0 0 0 2px #923BFF"}, {boxShadow: "none"}], 900);
});
document.querySelector(".set-nav a").classList.add("on");
// ── Provoz: spotřeba tokenů a historie akcí ──
const fmtTok = n => n == null ? "—" : n >= 1e6 ? (n / 1e6).toLocaleString("cs-CZ", {maximumFractionDigits: 1}) + " mil." : n >= 1e3 ? Math.round(n / 1e3).toLocaleString("cs-CZ") + " tis." : String(n);
const ACTION = {"claim": ["Zamkl(a) cestu", ""], "release": ["Uvolnil(a) cestu", ""], "claim-refused": ["Zámek odmítnut", "bad"], "accept": ["Převzal(a) zprávu", ""],
  "expire": ["Nájem vypršel", ""], "blocked": ["Úkol zablokován", "bad"], "failover": ["Převzetí úkolů jiným agentem", ""], "limit": ["Nahlášený limit", "bad"],
  "review-open": ["Otevřel(a) review", ""], "review-submit": ["Poslal(a) do review", ""], "review-verdict": ["Verdikt review", "ok"], "review-close": ["Uzavřel(a) review", "ok"], "review-grant-cycle": ["Povolil(a) další kolo", ""],
  "quality-run": ["Kontrola kvality", ""], "quality-request": ["Požadavek na kontrolu", ""], "quality-request-unavailable": ["Kontrola nedostupná", "bad"],
  "pause": ["Zastavil(a) agenty", "bad"], "resume": ["Pustil(a) agenty", "ok"], "mode": ["Přepnul(a) režim", ""], "provider-state": ["Stav poskytovatele", ""],
  "web-login-passkey": ["Přihlášení Windows Hello", "ok"], "web-login-passkey-failed": ["Neúspěšné přihlášení", "bad"], "web-passkey-added": ["Přidán klíč Windows Hello", ""], "web-passkey-removed": ["Odebrán klíč Windows Hello", ""]};
const HIST_GROUPS = [["main", "Důležité", a => !(a.startsWith("claim") || ["release", "accept", "expire", "provider-state"].includes(a))], ["all", "Vše", () => true], ["review", "Review", a => a.startsWith("review")], ["control", "Řízení a přihlášení", a => ["pause", "resume", "mode"].includes(a) || a.startsWith("web-")],
  ["quality", "Kvalita", a => a.startsWith("quality")], ["tasks", "Úkoly", a => ["accept", "expire", "blocked", "failover", "limit"].includes(a)], ["leases", "Zámky cest", a => a.startsWith("claim") || a === "release"]];
let histEvents = [], histGroup = "main", histTimer = null, opsData = null;
function loadOps(more = false) {
  const q = document.getElementById("histQuery").value.trim();
  const before = more && histEvents.length ? histEvents[histEvents.length - 1].id : 0;
  fetch("/api/history?" + new URLSearchParams({limit: "200", ...(q ? {q} : {}), ...(before ? {before: String(before)} : {})}))
    .then(r => r.ok ? r.json() : Promise.reject(r)).then(d => { histEvents = more ? histEvents.concat(d.events) : d.events; renderHistory(d.events.length < 200); })
    .catch(() => { document.getElementById("histList").textContent = "Historii se nepodařilo načíst."; });
  if (!more) fetch("/api/usage").then(r => r.ok ? r.json() : null).then(d => { if (d) { opsData = d; renderUsage(); } }).catch(() => {});
}
function renderHistory(end) {
  const g = HIST_GROUPS.find(x => x[0] === histGroup), rows = histEvents.filter(e => g[2](e.action));
  setHTMLIfChanged(document.getElementById("histChips"), chipsHTML(HIST_GROUPS.map(([id, l, f]) => [id, `${l} (${histEvents.filter(e => f(e.action)).length})`]), histGroup, "data-hist"));
  document.querySelectorAll("[data-hist]").forEach(b => b.onclick = () => { histGroup = b.dataset.hist; renderHistory(end); });
  document.getElementById("histCount").textContent = `(${rows.length})`;
  setHTMLIfChanged(document.getElementById("histList"), rows.length ? rows.map(e => { const [label, tone] = ACTION[e.action] || [e.action, ""];
    return `<div class="hist-row" data-tone="${tone}"><time title="${esc(new Date(e.at * 1000).toLocaleString("cs-CZ"))}">${esc(when(e.at))}</time>${whoBadge(e.agent)}<div><b>${esc(label)}</b>${e.path || e.detail ? `<div class="det">${esc([e.path, e.detail].filter(Boolean).join(" · ").slice(0, 300))}</div>` : ""}</div></div>`; }).join("")
    : "<div class='empty'>Žádné akce pro tento filtr.</div>");
  document.getElementById("histMore").hidden = !!end;
}
function renderUsage() {
  const d = opsData; if (!d) return;
  const tok = x => (x.input_tokens || 0) + (x.output_tokens || 0);
  const day = Object.values(d.agents).reduce((s, a) => s + tok(a.day), 0), week = Object.values(d.agents).reduce((s, a) => s + tok(a.week), 0);
  const cost = Object.values(d.agents).reduce((s, a) => s + (a.week.cost_usd || 0), 0);
  metricsRow("opsMetrics", [
    ["Tokeny za 24 h", fmtTok(day), "bars", "cyan", "Vstup + výstup běhů na pozadí (mosty, výzkum, opravy)"],
    ["Tokeny za 7 dní", fmtTok(week), "layers", "purple"],
    ["Běhy za 24 h", Object.values(d.agents).reduce((s, a) => s + (a.day.attempts || 0), 0), "flask", "orange"],
    ["Cena za 7 dní", cost ? "$" + cost.toFixed(2) : "neuvedena", "star", "green", "Poskytovatel cenu u předplatného nehlásí"],
    ["Denní strop", d.caps.daily_tokens ? fmtTok(d.caps.daily_tokens) : "bez stropu", "shield", "purple"],
    ["Akce v historii", histEvents.length >= 200 ? "200+" : histEvents.length, "clock", "cyan"],
  ]);
  document.getElementById("opsCaps").textContent = d.caps.task_tokens ? `strop na úkol ${fmtTok(d.caps.task_tokens)}` : "";
  setHTMLIfChanged(document.getElementById("opsUsage"), Object.entries(d.agents).map(([a, u]) => { const k = agentKey(a), cap = d.caps.daily_tokens, used = tok(u.day);
    return `<div class="use-agent"><div class="who"><img src="/assets/${k}.webp" alt=""><b>${k === "ada" ? "Ada" : "Nox"}</b></div>${cap ? `<div class="bar" title="${esc(fmtTok(used))} z ${esc(fmtTok(cap))}"><i style="width:${Math.min(100, Math.round(100 * used / cap))}%"></i></div>` : ""}<dl><dt>24 h vstup / výstup</dt><dd>${esc(fmtTok(u.day.input_tokens))} / ${esc(fmtTok(u.day.output_tokens))}</dd><dt>24 h z mezipaměti</dt><dd>${esc(fmtTok(u.day.cache_read_tokens))}</dd><dt>7 dní celkem</dt><dd>${esc(fmtTok(tok(u.week)))}</dd><dt>Běhy 24 h / 7 dní</dt><dd>${u.day.attempts || 0} / ${u.week.attempts || 0}</dd></dl></div>`; }).join("")
    + '<p class="note">Počítají se jen běhy na pozadí přes mosty. Tvoje interaktivní relace s Adou a Noxem tu nejsou.</p>');
  setHTMLIfChanged(document.getElementById("opsRuns"), d.recent.length ? d.recent.map(r => `<div class="hist-row"><time>${esc(when(r.at))}</time>${whoBadge(r.recipient)}<div><b>Úkol #${r.message_id}${r.attempt > 1 ? ` · pokus ${r.attempt}` : ""}</b><div class="det">${esc(fmtTok((r.input_tokens || 0) + (r.output_tokens || 0)))} tokenů${r.model || r.requested_model ? " · " + esc(r.model || r.requested_model) : ""}</div></div></div>`).join("")
    : "<div class='empty'>Zatím žádný zaznamenaný běh.</div>");
}
document.getElementById("histQuery").addEventListener("input", () => { clearTimeout(histTimer); histTimer = setTimeout(() => loadOps(), 300); });
document.getElementById("histMore").onclick = () => loadOps(true);
// ── Katalog schopností z ověřené inventury ──
let catalog = null, catAgent = "ada", catStatus = "all";
const CAT_STATUS = s => { const t = String(s).toLowerCase(); return t.includes("verified") ? "verified" : t.includes("failed") ? "failed" : t.includes("enabled") ? "enabled" : t.includes("installed") ? "installed" : t.includes("path") ? "path" : ""; };
const CAT_LABEL = {verified: "ověřeno", enabled: "zapnuto", installed: "nainstalováno", failed: "nefunguje", path: "jen v PATH"};
function loadCatalog() {
  if (catalog) return renderCatalog();
  fetch("/api/catalog").then(r => r.ok ? r.json() : Promise.reject(r)).then(d => { catalog = d; renderCatalog(); })
    .catch(() => { document.getElementById("catBody").textContent = "Katalog se nepodařilo načíst."; });
}
function renderCatalog() {
  const all = catalog.agents, rows = a => all[a].flatMap(sec => sec.rows.map(r => ({sec, r, st: CAT_STATUS(r.join(" "))})));
  const count = (a, st) => rows(a).filter(x => x.st === st).length;
  metricsRow("catMetrics", [["Položek u Ady", rows("ada").length, "users", "orange"], ["Položek u Noxe", rows("nox").length, "users", "green"],
    ["Ověřeno", count("ada", "verified") + count("nox", "verified"), "check", "green"], ["Zapnuto", count("ada", "enabled") + count("nox", "enabled"), "layers", "cyan"],
    ["Nefunguje", count("ada", "failed") + count("nox", "failed"), "alert", "orange"], ["Inventura", new Date(catalog.as_of).toLocaleDateString("cs-CZ"), "clock", "purple"]]);
  setHTMLIfChanged(document.getElementById("catAgents"), ["ada", "nox"].map(a => `<button type="button" class="pg-item${a === catAgent ? " on" : ""}" data-cat-agent="${a}"><img class="act-portrait" src="/assets/${a}.webp" alt=""><span class="pg-item-body"><span class="pg-item-title">${a === "ada" ? "Ada · Claude Code" : "Nox · Codex"}</span><span class="pg-item-sub">${rows(a).length} položek · ${count(a, "verified")} ověřeno</span></span></button>`).join(""));
  document.querySelectorAll("[data-cat-agent]").forEach(b => b.onclick = () => { catAgent = b.dataset.catAgent; renderCatalog(); });
  document.getElementById("catSource").textContent = `Zdroj: ${catalog.generated_from[catAgent]} (inventura ${new Date(catalog.as_of).toLocaleDateString("cs-CZ")}). Mezi agenty neplatí „co má jeden, má i druhý“.`;
  document.getElementById("catTitle").textContent = catAgent === "ada" ? "Schopnosti Ady" : "Schopnosti Noxe";
  setHTMLIfChanged(document.getElementById("catChips"), chipsHTML([["all", "Vše"], ...["verified", "enabled", "installed", "failed", "path"].filter(st => count(catAgent, st)).map(st => [st, `${CAT_LABEL[st]} (${count(catAgent, st)})`])], catStatus, "data-cat-st"));
  document.querySelectorAll("[data-cat-st]").forEach(b => b.onclick = () => { catStatus = b.dataset.catSt; renderCatalog(); });
  const q = document.getElementById("catQuery").value.trim().toLowerCase();
  setHTMLIfChanged(document.getElementById("catBody"), all[catAgent].map(sec => {
    const list = sec.rows.filter(r => (catStatus === "all" || CAT_STATUS(r.join(" ")) === catStatus) && (!q || r.join(" ").toLowerCase().includes(q)));
    if (!list.length) return "";
    return `<section class="cat-section"><h3>${esc(sec.section)}</h3><table class="cat-table"><thead><tr>${sec.columns.map(c => `<th scope="col">${esc(c)}</th>`).join("")}</tr></thead><tbody>${list.map(r => `<tr>${r.map(c => { const st = CAT_STATUS(c); return `<td>${st && c.length < 40 ? `<span class="st-pill ${st}">${esc(c)}</span>` : esc(c)}</td>`; }).join("")}</tr>`).join("")}</tbody></table></section>`;
  }).join("") || "<div class='empty'>Nic neodpovídá filtru.</div>");
}
document.getElementById("catQuery").addEventListener("input", () => { if (catalog) renderCatalog(); });

function showView(view,{focus=true}={}) {
  if(view==="ops")loadOps();
  if(view==="catalog")loadCatalog();
  if(view==="settings"){loadMode();loadPasskeys();}
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
  renderPages();
}
document.querySelectorAll("[data-view]").forEach(b=>b.addEventListener("click",()=>showView(b.dataset.view)));
renderQualityGate();

document.getElementById("navAgents").onclick=()=>{showView("overview",{focus:false});openPanel("side");};
for(const [id,target,panel] of [["navProjects","projectPanel","side"],["navHistory","historyPanel","inspector"]])document.getElementById(id).onclick=e=>{e.preventDefault();showView("overview",{focus:false});openPanel(panel);document.getElementById(target).scrollIntoView({block:"start"});};
</script>
"""


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower()
    except OSError:
        return False


def open_url(url: str) -> bool:
    """Ve WSL otevře prohlížeč ve Windows (`webbrowser` by spustil linuxový ve WSLg, 26. 11.)."""
    if _is_wsl():
        cmd = shutil.which("cmd.exe") or "/mnt/c/Windows/System32/cmd.exe"
        if Path(cmd).exists():
            try:
                # URL jde jako argument procesu, ne přes shell; token je urlsafe (bez znaků, které cmd vykládá).
                subprocess.run([cmd, "/c", "start", "", url], cwd="/mnt/c" if Path("/mnt/c").is_dir() else None,
                               check=True, timeout=20, capture_output=True)
                return True
            except (OSError, subprocess.SubprocessError):
                pass
    return webbrowser.open(url)


PROC_TCP_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")


def listener_owned_by_me(port: int, tables: tuple[str, ...] = PROC_TCP_TABLES) -> bool | None:
    """Naslouchá na portu proces tohoto uživatele? None = nejde zjistit (není Linux /proc)."""
    found = None
    for name in tables:
        try:
            rows = Path(name).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            f = row.split()
            if len(f) > 7 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:   # 0A = LISTEN
                if int(f[7]) != os.getuid():
                    return False
                found = True
    return found


WSL_FORWARDERS = {"wslrelay", "wslhost", "wslservice"}


def windows_port_forwards_to_wsl(port: int) -> bool:
    """Ve WSL prohlížeč jde na localhost Windows. Když tam port drží jiný program než předávání WSL, je to cizí cíl."""
    ps = shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    # Fail-closed (review #55): každá chyba dotazu ukončí skript bez koncové značky = neověřeno.
    script = ("$ErrorActionPreference = 'Stop'; "
              f"$l = @(Get-NetTCPConnection -State Listen | Where-Object {{ $_.LocalPort -eq {int(port)} }}); "
              "foreach ($c in $l) { try { (Get-Process -Id $c.OwningProcess).ProcessName } catch { 'unknown-process' } }; "
              "'AL-END'")
    try:
        out = subprocess.run([ps, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=20,
                             cwd="/mnt/c" if Path("/mnt/c").is_dir() else None, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return False   # nejde ověřit = nepředávat
    lines = [n.strip() for n in out.splitlines() if n.strip()]
    if not lines or lines[-1] != "AL-END":
        return False                   # dotaz nedoběhl celý = neověřeno
    return {n.lower() for n in lines[:-1]} <= WSL_FORWARDERS   # nic = ve Windows port nikdo nedrží (mirrored)


def login(port: int = 8765) -> int:
    """`agent-lease web-login`: otevře už běžící web (služba agent-lease-web) přihlášený, token nevypisuje."""
    owner = listener_owned_by_me(port)
    if owner is not True:
        # Kód by přes prohlížeč dostala stránka toho, kdo port drží (location.hash) — posílá se jen na ověřený port.
        print(f"Web na http://localhost:{port}/ neběží pod tímto uživatelem (nebo to nejde ověřit). "
              "Spusť: systemctl --user start agent-lease-web", file=sys.stderr)
        return 2 if owner is False else 1
    if _is_wsl() and not windows_port_forwards_to_wsl(port):
        print(f"Port {port} ve Windows drží jiný program než WSL. Kód neposílám.", file=sys.stderr)
        return 2
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except OSError:
        print(f"Web na http://localhost:{port}/ neběží. Spusť: systemctl --user start agent-lease-web",
              file=sys.stderr)
        return 1
    settings = Settings.from_env()
    load_or_create_token(settings)
    if not open_url(f"http://localhost:{port}/#token={issue_login_code(settings)}"):
        print("Prohlížeč se nepodařilo otevřít.", file=sys.stderr)
        return 1
    print(f"Otevřeno: http://localhost:{port}/ (přihlášení platí {SESSION_MAX_AGE_SECONDS // 86400} dní)")
    return 0


def run(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False) -> int:
    settings = Settings.from_env()
    store = Store(settings=settings)
    token = load_or_create_token(settings)
    handler = make_handler(store, settings, token, port)
    server = type("CappedServer", (_ConnectionCap, ThreadingHTTPServer), {})((host, port), handler)
    server.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"Live room at {url}")
    if open_browser:
        if not open_url(f"{url}#token={issue_login_code(settings)}"):
            print("Could not open a browser. Run `agent-lease web-login` once a browser is available.",
                  file=sys.stderr)
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
