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
    """Token žije vedle DB, ne v env — přežije restart procesu i terminálu."""
    path = _token_path(settings)
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            path.chmod(0o600)
            return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    path.chmod(0o600)
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
  :root { color-scheme: light dark; }
  body { font: 14px/1.4 system-ui, sans-serif; margin: 0; display: flex; height: 100vh; }
  #side { width: 260px; flex: none; border-right: 1px solid #8883; padding: 10px; overflow-y: auto; }
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #feed { flex: 1; overflow-y: auto; padding: 10px; }
  .msg { margin-bottom: 8px; padding: 6px 8px; border-radius: 6px; background: #8881; }
  .msg .meta { opacity: .6; font-size: 12px; }
  .msg .state { float: right; font-weight: 600; }
  .kind-task { border-left: 3px solid #e67e22; }
  .kind-broadcast { border-left: 3px solid #8888; }
  .kind-chat { border-left: 3px solid #3498db; }
  .kind-control { border-left: 3px solid #c0392b; }
  .kind-result { border-left: 3px solid #27ae60; }
  #compose { display: flex; gap: 6px; padding: 8px; border-top: 1px solid #8883; }
  #compose input[type=text] { flex: 1; }
  select, input, button { font: inherit; }
  h3 { font-size: 12px; text-transform: uppercase; opacity: .6; margin: 12px 0 4px; }
  .peer, .claim { font-size: 12px; margin-bottom: 4px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
  .dot.on { background: #27ae60; } .dot.off { background: #999; }
  #status { font-size: 12px; opacity: .6; padding: 4px 8px; }
</style>
<div id="side">
  <h3>Přítomnost</h3>
  <div id="peers"></div>
  <h3>Nájmy</h3>
  <div id="claims"></div>
</div>
<div id="main">
  <div id="status">připojuji…</div>
  <div id="feed"></div>
  <div id="compose">
    <select id="kind">
      <option value="chat">chat</option>
      <option value="task">task</option>
      <option value="broadcast">broadcast</option>
      <option value="control">control</option>
    </select>
    <input id="to" type="text" placeholder="komu (claude-code / codex)" size="16">
    <input id="text" type="text" placeholder="zpráva…">
    <button id="sendBtn">Poslat</button>
  </div>
</div>
<script>
const TOKEN = "__TOKEN__";
const feed = document.getElementById("feed");
const peersEl = document.getElementById("peers");
const claimsEl = document.getElementById("claims");
const statusEl = document.getElementById("status");
const kindEl = document.getElementById("kind");
const toEl = document.getElementById("to");

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

function renderMessage(m) {
  let div = document.getElementById("msg-" + m.id);
  const fresh = !div;
  if (fresh) div = document.createElement("div");
  div.id = "msg-" + m.id;
  div.className = "msg kind-" + esc(m.kind || "chat");
  const to = m.recipient && m.recipient !== "*" ? (" → " + esc(m.recipient)) : "";
  const state = m.state ? `<span class="state">${esc(m.state)}</span>` : "";
  div.innerHTML = `<div class="meta">#${m.id} ${esc(m.kind)} · ${esc(m.agent)}${to}${state}</div><div>${esc(m.text)}</div>`;
  if (m.kind === "task" && m.recipient && m.state) {
    if (["pending", "leased", "started", "failed", "needs_review", "dead_letter"].includes(m.state)) {
      const cancel = document.createElement("button");
      cancel.textContent = "Zrušit";
      cancel.onclick = () => control("cancel", m.id, m.recipient);
      div.appendChild(cancel);
    }
    if (["failed", "needs_review", "dead_letter"].includes(m.state)) {
      const retry = document.createElement("button");
      retry.textContent = "Opakovat";
      retry.onclick = () => control("retry", m.id, m.recipient);
      div.appendChild(retry);
    }
  }
  if (fresh) { feed.appendChild(div); feed.scrollTop = feed.scrollHeight; }
}

function renderRoom(r) {
  (r.messages || []).forEach(renderMessage);
  peersEl.innerHTML = (r.peers || []).map(p =>
    `<div class="peer"><span class="dot ${p.active ? "on" : "off"}"></span>${esc(p.agent)} · ${esc(p.presence || (p.active ? "online" : "offline"))}<br><span style="opacity:.6">${esc(p.status || "")}</span></div>`
  ).join("") || "<div class='peer'>nikdo</div>";
  claimsEl.innerHTML = (r.claims || []).map(c =>
    `<div class="claim">🔒 ${esc(c.path)}<br><span style="opacity:.6">${esc(c.held_by)} · ${esc(c.purpose || "")}</span></div>`
  ).join("") || "<div class='claim'>volno</div>";
}

function connect(since) {
  const es = new EventSource("/api/events?token=" + encodeURIComponent(TOKEN) + "&since=" + since);
  es.addEventListener("message", e => renderMessage(JSON.parse(e.data)));
  es.addEventListener("room", e => renderRoom(JSON.parse(e.data)));
  es.onopen = () => statusEl.textContent = "živě připojeno";
  es.onerror = () => statusEl.textContent = "spojení vypadlo, prohlížeč se sám zkusí znovu…";
}

// Nejdřív snapshot, potom SSE od jeho posledního ID. Set `seen` navíc chrání
// proti duplicitě při závodu mezi oběma požadavky i při reconnectu.
fetch("/api/snapshot?since=0", { headers: { "X-Agent-Lease-Token": TOKEN } })
  .then(r => r.json())
  .then(s => {
    s.messages.forEach(renderMessage);
    renderRoom(s);
    connect(s.messages.length ? s.messages[s.messages.length - 1].id : 0);
  })
  .catch(() => { statusEl.textContent = "snapshot selhal, připojuji živý stream…"; connect(0); });

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
