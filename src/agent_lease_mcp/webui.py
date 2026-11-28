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


def about_report(store: Store, settings: Settings, port: int) -> dict:
    """Nastavení → O systému: co běží a kde (bez tajemství)."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        pkg = version("agent-lease-mcp")
    except PackageNotFoundError:
        pkg = "neznámá"
    with store._connect() as con:
        journal = con.execute("PRAGMA journal_mode").fetchone()[0]
        messages = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    runtime = Path(sys.prefix).resolve().parent.name
    return {"version": pkg, "runtime": runtime if runtime.startswith("runtime-") else "vývojové prostředí",
            "address": f"http://localhost:{port}", "db": str(settings.db_path), "journal": journal,
            "messages": messages, "session_days": SESSION_MAX_AGE_SECONDS // 86400, "python": sys.version.split()[0]}


def db_version(path: Path) -> int:
    try:
        st = os.stat(path)
    except OSError:
        return 0
    return st.st_mtime_ns ^ st.st_size


MEMORY_MAX_CHARS = 64_000   # jedna poznámka; delší se ořízne (web je náhled, ne editor)


def _memory_files(home: Path) -> list[tuple[str, Path]]:
    """Skutečná paměť agentů: poznámky Ady (Claude Code auto-memory) a Noxova AGENTS.md. Bez symlinků."""
    files = [("ada", f) for f in sorted((home / ".claude" / "projects").glob("*/memory/*.md")) if f.name != "MEMORY.md"]
    files.append(("nox", home / ".codex" / "AGENTS.md"))
    # review #64: symlink nesmí být ani soubor, ani adresář memory/projekt (.codex) – jinak by web vydal cizí soubory
    return [(agent, f) for agent, f in files
            if f.is_file() and not any(x.is_symlink() for x in (f, f.parent, f.parent.parent)[: 3 if agent == "ada" else 2])]


def _stat(f: Path):
    try:
        return f.stat()
    except OSError:   # soubor zmizel mezi výpisem a čtením (review #64): přeskočit, ne shodit stream
        return None


def _front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n") or "\n---" not in text[4:]:
        return {}, text
    head, body = text[4:].split("\n---", 1)
    meta = {}
    for line in head.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and value.strip() and key in ("name", "description", "type"):
            value = value.strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":   # YAML uvozovky kolem hodnoty
                value = value[1:-1].replace('\\"', '"')
            meta[key] = value
    return meta, body.lstrip("-").lstrip("\n")


def memory_version(home: Path | None = None) -> str:
    """Levná změna paměti (počet, nejnovější čas, velikost) pro živé obnovení stránky."""
    stats = [st for _, f in _memory_files(home or Path.home()) if (st := _stat(f))]
    return f"{len(stats)}:{max((s.st_mtime_ns for s in stats), default=0)}:{sum(s.st_size for s in stats)}"


def memory_report(home: Path | None = None) -> dict:
    """Paměť Ady a Noxe pro web (jen čtení, jen přihlášený člověk)."""
    home = home or Path.home()
    out: dict[str, list] = {"ada": [], "nox": []}
    for agent, f in _memory_files(home):
        try:
            text, mtime = f.read_text(encoding="utf-8", errors="replace")[:MEMORY_MAX_CHARS], f.stat().st_mtime
        except OSError:
            continue
        meta, body = _front_matter(text)
        project = f.parent.parent.name if agent == "ada" else ""
        out[agent].append({"id": f"{agent}:{project}/{f.stem}", "slug": f.stem, "name": meta.get("name") or f.stem,
                           "description": meta.get("description", ""), "type": meta.get("type") or "none",
                           "project": project.replace("-", "/").replace("/home/" + home.name, "~") if project else "",
                           "updated_at": mtime, "body": body.strip()})
    out["version"] = memory_version(home)
    return out


OPEN_JOB_STATES = ("pending", "leased", "started", "failed", "needs_review", "dead_letter")


def open_jobs(store: Store, limit: int = 300) -> list[dict]:
    """Provoz: úkoly na pozadí, které běží, čekají nebo potřebují zásah (zrušit / znovu / schválit)."""
    return store.open_tasks(OPEN_JOB_STATES, limit=limit)


def usage_report(store: Store, settings: Settings) -> dict:
    """Spotřeba tokenů pro web: 24 h a 7 dní po agentech, stropy z nastavení, poslední běhy."""
    now = time.time()
    agents = {}
    for agent in ("claude-code", "codex"):
        agents[agent] = {"day": store.usage_summary(since=now - 86400, recipient=agent),
                         "week": store.usage_summary(since=now - 7 * 86400, recipient=agent),
                         "last_at": store.usage_summary(recipient=agent)["last_at"]}
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
            if parts.path in ("/api/usage", "/api/history", "/api/catalog", "/api/jobs", "/api/memory"):
                if not self._session_ok():
                    return self._reject(403, "bad token")
                if parts.path == "/api/memory":
                    return self._json_response(memory_report())
                if parts.path == "/api/jobs":
                    return self._json_response({"jobs": open_jobs(store)})
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
            if parts.path == "/api/about":
                if not self._session_ok():
                    return self._reject(403, "bad token")
                return self._json_response(about_report(store, settings, port))
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
            if parts.path == "/api/logout":
                session_id = self._session_id()
                with sessions_lock:
                    sessions.pop(session_id, None)
                    session_keys.pop(session_id, None)
                store.record(web_agent, "web-logout", "", "")
                self.send_response(204)
                self._security_headers()
                self.send_header("Set-Cookie", f"{cookie_name}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
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
                last_version, backlog = None, False
                last_memory, next_memory_at = None, 0.0
                while self._session_alive(stream_session):   # po vypršení přihlášení stream skončí
                    # Skutečně živě (Michal 27. 11.): každý zápis do DB mění soubor (journal DELETE),
                    # takže změnu poznáme hned a pošleme ji; bez změny jen levná kontrola času souboru.
                    version = db_version(settings.db_path)
                    changed = version != last_version
                    last_version = version
                    if time.monotonic() >= next_memory_at:   # poznámky agentů nejsou v DB: stat ~190 souborů 1× za s
                        next_memory_at = time.monotonic() + 1.0
                        memory = memory_version()
                        changed = changed or (last_memory is not None and memory != last_memory)
                        last_memory = memory
                    # Plná dávka = možná další čekají; fallback čte inbox i bez změny verze (mtime^size není bezkolizní).
                    drain = changed or backlog or time.monotonic() >= next_room_at
                    messages = store.inbox(since_id=since, limit=200) if drain else []
                    backlog = len(messages) >= 200
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
                            "memory_version": memory_version(),   # Paměť se obnoví hned po změně poznámky
                        }
                        room["projects"] = {r["repo"]: project_of(r["repo"]) for r in room["reviews_all"] if r.get("repo")}
                        self.wfile.write(_sse("room", None, room))
                        self.wfile.flush()
                        next_room_at = now + ROOM_SECONDS

                    if now >= next_heartbeat_at:
                        self.wfile.write(_sse_heartbeat())
                        self.wfile.flush()
                        next_heartbeat_at = now + SSE_HEARTBEAT_SECONDS

                    if not backlog:
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


# Stránka žije v souborech web/index.html, app.css a app.js (refaktoring 27. 11.); server je skládá do
# jednoho dokumentu, aby zůstala jedna odpověď s CSP nonce pro jediný inline skript.
_WEB_DIR = Path(__file__).with_name("web")


def _load_page() -> str:
    shell = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
    css = (_WEB_DIR / "app.css").read_text(encoding="utf-8")
    js = (_WEB_DIR / "app.js").read_text(encoding="utf-8")
    return shell.replace("{{CSS}}", css, 1).replace("{{JS}}", js, 1)


PAGE_HTML = _load_page()


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
