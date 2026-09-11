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
si nevybírá — je napevno `WEB_AGENT`.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import Settings
from .models import MessageKind
from .store import Store

WEB_AGENT = "michal"
POLL_SECONDS = 0.7
ROOM_SECONDS = 2.0


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


def make_handler(store: Store, settings: Settings, token: str, port: int):
    allowed_origins = {
        f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "agent-lease-webui/0.1"

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

        def _query_token_ok(self, qs: dict[str, list[str]]) -> bool:
            values = qs.get("token", [])
            return bool(values) and secrets.compare_digest(values[0], token)

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
                return self._handle_index(qs)
            if parts.path == "/api/events":
                return self._handle_events(qs)
            if parts.path == "/api/snapshot":
                return self._handle_snapshot(qs)
            self._reject(404, "not found")

        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            if not self._origin_ok():
                return self._reject(403, "bad origin")
            if not self._bearer_ok():
                return self._reject(403, "bad token")
            if parts.path == "/api/send":
                return self._handle_send()
            if parts.path == "/api/retry":
                return self._handle_retry()
            if parts.path == "/api/cancel":
                return self._handle_cancel()
            self._reject(404, "not found")

        # ── handlery ────────────────────────────────────────────────────
        def _handle_index(self, qs: dict[str, list[str]]) -> None:
            if not self._query_token_ok(qs):
                return self._reject(403, "chybí nebo sedí špatný ?token=")
            body = PAGE_HTML.replace("__TOKEN__", html.escape(qs["token"][0])).encode("utf-8")
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

        def _handle_snapshot(self, qs: dict[str, list[str]]) -> None:
            if not self._bearer_ok():
                return self._reject(403, "bad token")
            try:
                since = max(0, int(qs.get("since", ["0"])[0]))
            except ValueError:
                return self._reject(400, "since musí být celé číslo")
            payload = {
                "me": WEB_AGENT,
                "messages": (
                    store.inbox(since_id=since, limit=200)
                    if since else store.latest_messages(limit=200)
                ),
                "peers": store.peers(),
                "claims": [c.as_dict() for c in store.claims()],
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
                self._reject(400, "prázdné nebo příliš dlouhé tělo")
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                self._reject(400, "špatný JSON")
                return None
            if not isinstance(data, dict):
                self._reject(400, "JSON tělo musí být objekt")
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
                return self._reject(400, "prázdný text")
            try:
                kind_enum = MessageKind(kind)
            except ValueError:
                return self._reject(400, f"neznámý kind: {kind}")
            if kind_enum == MessageKind.BROADCAST:
                recipient = "*"
            elif not recipient:
                return self._reject(400, "chat/task/control potřebuje 'to'")
            message_id = store.send(WEB_AGENT, recipient, text, kind=kind_enum)
            store.heartbeat(WEB_AGENT, cwd=os.getcwd())
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
                return self._reject(400, "retry potřebuje platné 'id' a 'to'")
            if not recipient:
                return self._reject(400, "retry potřebuje platné 'id' a 'to'")
            if not store.retry(message_id, recipient, not_before=not_before):
                return self._reject(409, "task není ve stavu, který lze opakovat")
            self._json_response({"id": message_id, "state": "pending"})

        def _handle_cancel(self) -> None:
            data = self._read_json()
            if data is None:
                return
            try:
                message_id = int(data["id"])
                recipient = str(data["to"]).strip()
            except (KeyError, TypeError, ValueError):
                return self._reject(400, "cancel potřebuje platné 'id' a 'to'")
            if not recipient:
                return self._reject(400, "cancel potřebuje platné 'id' a 'to'")
            if not store.cancel(message_id, recipient):
                return self._reject(409, "task už nejde zrušit nebo neexistuje")
            self._json_response({"id": message_id, "state": "cancelled"})

        def _handle_events(self, qs: dict[str, list[str]]) -> None:
            if not self._query_token_ok(qs):
                return self._reject(403, "bad token")
            # Last-Event-ID má přednost před ?since= — to je to, co prohlížeč
            # pošle sám při reconnectu, a je to spolehlivější než náš JS.
            last_event_id = self.headers.get("Last-Event-ID")
            try:
                since = max(
                    0, int(last_event_id) if last_event_id else int(qs.get("since", ["0"])[0])
                )
            except ValueError:
                return self._reject(400, "since/Last-Event-ID musí být celé číslo")

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self._security_headers()
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            next_room_at = 0.0
            try:
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
                            "messages": store.latest_messages(limit=200),
                        }
                        self.wfile.write(_sse("room", None, room))
                        self.wfile.flush()
                        next_room_at = now + ROOM_SECONDS

                    time.sleep(POLL_SECONDS)
            except (BrokenPipeError, ConnectionResetError):
                return  # klient zavřel kartu nebo se prohlížeč odpojil

    return Handler


PAGE_HTML = """<!doctype html>
<title>agent-lease · live chat</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    color-scheme: dark light;
    --bg: #0a0b10; --surface: #12141c; --surface-2: #171a24; --raised: #1d212c;
    --border: rgba(255,255,255,.08); --border-soft: rgba(255,255,255,.05);
    --text: #eef0f6; --text-dim: #9aa0b4; --text-faint: #676d82;
    --accent-1: #8b7bff; --accent-2: #ff6fae; --accent-3: #37e0c4;
    --grad: linear-gradient(135deg, var(--accent-1), var(--accent-2));
    --ok: #38d996; --warn: #ffb648; --bad: #ff5c7a;
    --shadow: 0 8px 28px rgba(0,0,0,.35);
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f4f5f9; --surface: #ffffff; --surface-2: #f0f1f7; --raised: #ffffff;
      --border: rgba(15,17,30,.09); --border-soft: rgba(15,17,30,.05);
      --text: #14162a; --text-dim: #565c74; --text-faint: #9498ab;
      --shadow: 0 8px 24px rgba(30,20,60,.08);
    }
  }
  * { box-sizing: border-box; }
  body {
    font: 14px/1.5 "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    margin: 0; height: 100vh; display: flex; background: var(--bg); color: var(--text);
    -webkit-font-smoothing: antialiased;
  }
  ::selection { background: var(--accent-1); color: #fff; }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 8px; }

  #side {
    width: 280px; flex: none; padding: 18px 14px; overflow-y: auto;
    background: var(--surface); border-right: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 18px;
  }
  #brand { display: flex; align-items: center; gap: 10px; padding: 0 2px 4px; }
  #brand .mark {
    width: 30px; height: 30px; border-radius: 9px; background: var(--grad);
    box-shadow: var(--shadow); flex: none;
  }
  #brand .name { font-weight: 650; letter-spacing: -.01em; font-size: 15px; }
  #brand .sub { font-size: 11px; color: var(--text-faint); }

  h3 {
    font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: .08em;
    color: var(--text-faint); margin: 0 0 8px 2px;
  }
  .section { display: flex; flex-direction: column; gap: 8px; }

  .peer {
    display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 12px;
    background: var(--surface-2); border: 1px solid var(--border-soft); transition: transform .15s;
  }
  .peer:hover { transform: translateX(2px); }
  .avatar {
    width: 30px; height: 30px; border-radius: 50%; flex: none; display: grid; place-items: center;
    color: #fff; font-weight: 700; font-size: 12px; position: relative; box-shadow: 0 2px 8px rgba(0,0,0,.25);
  }
  .avatar .dot {
    position: absolute; right: -1px; bottom: -1px; width: 9px; height: 9px; border-radius: 50%;
    border: 2px solid var(--surface-2); background: var(--text-faint);
  }
  .avatar .dot.on { background: var(--ok); box-shadow: 0 0 0 0 rgba(56,217,150,.6); animation: pulse 2s infinite; }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(56,217,150,.55); }
    70%  { box-shadow: 0 0 0 6px rgba(56,217,150,0); }
    100% { box-shadow: 0 0 0 0 rgba(56,217,150,0); }
  }
  .peer .who { min-width: 0; }
  .peer .agent { font-weight: 600; font-size: 12.5px; }
  .peer .meta { font-size: 11px; color: var(--text-faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .peer .task { font-size: 11px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .empty { font-size: 12px; color: var(--text-faint); padding: 6px 2px; }

  .claim {
    padding: 8px 10px; border-radius: 12px; background: var(--surface-2);
    border: 1px solid var(--border-soft); font-size: 11.5px;
  }
  .claim .path { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; color: var(--text); overflow-wrap: anywhere; }
  .claim .row { display: flex; justify-content: space-between; align-items: center; margin-top: 4px; color: var(--text-faint); }
  .ttl { font-variant-numeric: tabular-nums; padding: 1px 7px; border-radius: 999px; background: var(--border-soft); }

  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #topbar {
    display: flex; align-items: center; justify-content: space-between; padding: 14px 22px;
    border-bottom: 1px solid var(--border); backdrop-filter: blur(6px);
  }
  #topbar .title { font-weight: 650; font-size: 14.5px; background: var(--grad);
    -webkit-background-clip: text; background-clip: text; color: transparent; }
  #status { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--text-dim); }
  #status .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-faint); }
  #status .dot.live { background: var(--ok); animation: pulse 2s infinite; }
  #status .dot.retry { background: var(--warn); }

  #feed { flex: 1; overflow-y: auto; padding: 18px 22px; display: flex; flex-direction: column; gap: 10px; }
  .msg {
    max-width: 720px; padding: 10px 14px; border-radius: 14px; background: var(--surface);
    border: 1px solid var(--border); box-shadow: var(--shadow); animation: rise .25s ease-out;
  }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .msg .meta { display: flex; align-items: center; gap: 8px; font-size: 11.5px; color: var(--text-faint); margin-bottom: 4px; }
  .msg .meta .who { color: var(--text-dim); font-weight: 600; }
  .msg .meta .arrow { opacity: .6; }
  .msg .text { font-size: 13.5px; white-space: pre-wrap; word-break: break-word; }
  .pill {
    font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
    padding: 2px 8px; border-radius: 999px; color: #fff; flex: none;
  }
  .pill.kind-chat      { background: linear-gradient(135deg,#4d7dff,#6fa8ff); }
  .pill.kind-task      { background: linear-gradient(135deg,#ff9d42,#ffbe63); color:#2a1600; }
  .pill.kind-broadcast { background: linear-gradient(135deg,#7d84a3,#9aa1c2); }
  .pill.kind-control   { background: linear-gradient(135deg,#ff5c7a,#ff8aa0); }
  .pill.kind-result    { background: linear-gradient(135deg,#2fd6a8,#5be8c4); color:#003326; }
  .state { margin-left: auto; font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 999px; }
  .state.pending, .state.leased, .state.expired { background: var(--border-soft); color: var(--text-dim); }
  .state.started      { background: rgba(139,123,255,.18); color: #a598ff; }
  .state.succeeded     { background: rgba(56,217,150,.16); color: var(--ok); }
  .state.failed, .state.dead_letter { background: rgba(255,92,122,.16); color: var(--bad); }
  .state.needs_review  { background: rgba(255,182,72,.18); color: var(--warn); }
  .state.cancelled     { background: var(--border-soft); color: var(--text-faint); text-decoration: line-through; }
  .msg .actions { display: flex; gap: 6px; margin-top: 8px; }
  .btn-ghost {
    font: inherit; font-size: 11.5px; font-weight: 600; padding: 4px 10px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--surface-2); color: var(--text-dim); cursor: pointer;
    transition: all .15s;
  }
  .btn-ghost:hover { color: var(--text); border-color: var(--accent-1); }

  #compose {
    display: flex; gap: 8px; align-items: center; padding: 14px 22px; border-top: 1px solid var(--border);
    background: var(--surface);
  }
  select, input, button { font: inherit; color: var(--text); }
  #kind {
    appearance: none; padding: 9px 28px 9px 12px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--surface-2) url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6"><path d="M0 0l5 6 5-6z" fill="%239aa0b4"/></svg>') no-repeat right 10px center;
    cursor: pointer; flex: none;
  }
  #to {
    width: 150px; padding: 9px 12px; border-radius: 10px; border: 1px solid var(--border); background: var(--surface-2);
  }
  #text {
    flex: 1; padding: 9px 14px; border-radius: 10px; border: 1px solid var(--border); background: var(--surface-2);
  }
  #to:focus, #text:focus, #kind:focus { outline: none; border-color: var(--accent-1); box-shadow: 0 0 0 3px rgba(139,123,255,.15); }
  #sendBtn {
    padding: 9px 18px; border-radius: 10px; border: none; background: var(--grad); color: #fff;
    font-weight: 650; cursor: pointer; box-shadow: 0 4px 14px rgba(139,123,255,.35); transition: transform .12s, box-shadow .12s;
  }
  #sendBtn:hover { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(139,123,255,.45); }
  #sendBtn:active { transform: translateY(0); }

  @media (max-width: 720px) {
    body { flex-direction: column; }
    #side { width: auto; height: 40vh; border-right: none; border-bottom: 1px solid var(--border); }
  }
</style>
<div id="side">
  <div id="brand"><div class="mark"></div><div><div class="name">agent-lease</div><div class="sub">live chat</div></div></div>
  <div class="section"><h3>Přítomnost</h3><div id="peers"></div></div>
  <div class="section"><h3>Nájmy</h3><div id="claims"></div></div>
</div>
<div id="main">
  <div id="topbar">
    <div class="title">Místnost</div>
    <div id="status"><span class="dot"></span><span id="statusText">připojuji…</span></div>
  </div>
  <div id="feed"></div>
  <div id="compose">
    <select id="kind">
      <option value="chat">chat</option>
      <option value="task">task</option>
      <option value="broadcast">broadcast</option>
      <option value="control">control</option>
    </select>
    <input id="to" type="text" placeholder="komu">
    <input id="text" type="text" placeholder="napiš zprávu…">
    <button id="sendBtn">Poslat</button>
  </div>
</div>
<script>
const TOKEN = "__TOKEN__";
const feed = document.getElementById("feed");
const peersEl = document.getElementById("peers");
const claimsEl = document.getElementById("claims");
const statusDot = document.querySelector("#status .dot");
const statusText = document.getElementById("statusText");
const kindEl = document.getElementById("kind");
const toEl = document.getElementById("to");

function esc(s) {
  const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML;
}

function hueOf(name) {
  let h = 0; for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}
function avatarStyle(name) {
  const h = hueOf(name);
  return `background: linear-gradient(135deg, hsl(${h} 75% 55%), hsl(${(h + 45) % 360} 75% 55%))`;
}
function initial(name) { return String(name || "?").trim().slice(0, 1).toUpperCase(); }

function ago(seconds) {
  const s = Math.max(0, seconds | 0);
  if (s < 60) return `před ${s} s`;
  if (s < 3600) return `před ${Math.floor(s / 60)} min`;
  if (s < 86400) return `před ${Math.floor(s / 3600)} h`;
  return `před ${Math.floor(s / 86400)} d`;
}
function ttl(seconds) {
  const s = Math.max(0, seconds | 0);
  const m = Math.floor(s / 60), r = s % 60;
  return m > 0 ? `${m}m ${r}s` : `${r}s`;
}

function updateStatus(mode) {
  statusDot.className = "dot" + (mode === "live" ? " live" : mode === "retry" ? " retry" : "");
  statusText.textContent = mode === "live" ? "živě připojeno"
    : mode === "retry" ? "spojení vypadlo, zkouším znovu…" : "připojuji…";
}

function renderMessage(m) {
  let div = document.getElementById("msg-" + m.id);
  const fresh = !div;
  if (fresh) div = document.createElement("div");
  div.id = "msg-" + m.id;
  div.className = "msg";
  const to = m.recipient && m.recipient !== "*" ? `<span class="arrow">→</span> ${esc(m.recipient)}` : "";
  const state = m.state ? `<span class="state ${esc(m.state)}">${esc(m.state)}</span>` : "";
  div.innerHTML =
    `<div class="meta">` +
      `<span class="pill kind-${esc(m.kind || "chat")}">${esc(m.kind || "chat")}</span>` +
      `<span class="who">${esc(m.agent)}</span> ${to}` +
      `<span>· ${ago(m.seconds_ago)}</span>` +
      state +
    `</div>` +
    `<div class="text">${esc(m.text)}</div>`;
  if (m.kind === "task" && m.recipient && m.state) {
    const actions = document.createElement("div");
    actions.className = "actions";
    if (["pending", "leased", "started", "failed", "needs_review", "dead_letter"].includes(m.state)) {
      const cancel = document.createElement("button");
      cancel.className = "btn-ghost"; cancel.textContent = "Zrušit";
      cancel.onclick = () => control("cancel", m.id, m.recipient);
      actions.appendChild(cancel);
    }
    if (["failed", "needs_review", "dead_letter"].includes(m.state)) {
      const retry = document.createElement("button");
      retry.className = "btn-ghost"; retry.textContent = "Opakovat";
      retry.onclick = () => control("retry", m.id, m.recipient);
      actions.appendChild(retry);
    }
    if (actions.childElementCount) div.appendChild(actions);
  }
  if (fresh) { feed.appendChild(div); feed.scrollTop = feed.scrollHeight; }
}

function renderRoom(r) {
  (r.messages || []).forEach(renderMessage);
  peersEl.innerHTML = (r.peers || []).map(p => `
    <div class="peer">
      <div class="avatar" style="${avatarStyle(p.agent)}">${esc(initial(p.agent))}
        <span class="dot ${p.active ? "on" : ""}"></span>
      </div>
      <div class="who">
        <div class="agent">${esc(p.agent)}</div>
        <div class="task">${esc(p.status || (p.active ? "—" : ago(p.seen_seconds_ago)))}</div>
      </div>
    </div>`).join("") || "<div class='empty'>nikdo tu není</div>";
  claimsEl.innerHTML = (r.claims || []).map(c => `
    <div class="claim">
      <div class="path">🔒 ${esc(c.path)}</div>
      <div class="row"><span>${esc(c.held_by)}${c.purpose ? " · " + esc(c.purpose) : ""}</span>
        <span class="ttl">${ttl(c.expires_in_seconds)}</span></div>
    </div>`).join("") || "<div class='empty'>nic zamčené</div>";
}

function connect(since) {
  const es = new EventSource("/api/events?token=" + encodeURIComponent(TOKEN) + "&since=" + since);
  es.addEventListener("message", e => renderMessage(JSON.parse(e.data)));
  es.addEventListener("room", e => renderRoom(JSON.parse(e.data)));
  es.onopen = () => updateStatus("live");
  es.onerror = () => updateStatus("retry");
}

// Nejdřív snapshot, ať karta nezačíná prázdná, pak SSE od jeho posledního ID —
// `Last-Event-ID`/`since` na serveru zajistí navazující stream bez díry i bez duplicit.
fetch("/api/snapshot?since=0", { headers: { "X-Agent-Lease-Token": TOKEN } })
  .then(r => r.json())
  .then(s => {
    s.messages.forEach(renderMessage);
    renderRoom(s);
    connect(s.messages.length ? s.messages[s.messages.length - 1].id : 0);
  })
  .catch(() => { updateStatus("retry"); connect(0); });

function send() {
  const text = document.getElementById("text");
  const body = { text: text.value, kind: kindEl.value, to: toEl.value };
  if (!body.text.trim()) return;
  fetch("/api/send", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Agent-Lease-Token": TOKEN },
    body: JSON.stringify(body),
  }).then(r => { if (r.ok) text.value = ""; else r.text().then(t => alert(t)); });
}
function control(action, id, to) {
  fetch("/api/" + action, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Agent-Lease-Token": TOKEN },
    body: JSON.stringify({ id, to }),
  }).then(r => { if (!r.ok) r.text().then(t => alert(t)); });
}
function syncToField() { toEl.style.display = kindEl.value === "broadcast" ? "none" : ""; }
kindEl.addEventListener("change", syncToField); syncToField();

document.getElementById("sendBtn").onclick = send;
document.getElementById("text").addEventListener("keydown", e => { if (e.key === "Enter") send(); });
</script>
"""


def run(host: str = "127.0.0.1", port: int = 8765) -> int:
    settings = Settings.from_env()
    store = Store(settings=settings)
    token = load_or_create_token(settings)
    handler = make_handler(store, settings, token, port)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    url = f"http://{host}:{port}/?token={token}"
    print(f"Live chat na {url}")
    print("Token je uložený vedle DB (webui.token, 0600) — nesdílej ho, je to plná autorita 'michal'.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-lease-webui", description="Lokální live chat nad agent-lease")
    parser.add_argument("--host", default="127.0.0.1", help="jen loopback — nikdy 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print("Odmítnuto: live chat smí poslouchat jen na loopbacku.", file=sys.stderr)
        return 2
    return run(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
