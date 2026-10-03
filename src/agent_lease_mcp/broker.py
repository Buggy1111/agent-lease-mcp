"""
Broker: jediný proces, který otevírá SQLite pro zápis (fáze 2).

Proč: (1) sandboxovaný agent (Codex) nepotřebuje přístup k DB, stačí mu socket;
(2) jeden writer = žádné souběhy mezi procesy; (3) místo pro event-driven probuzení.

Protokol: jeden JSON řádek na požadavek a jeden na odpověď přes Unix socket 0600.
Povolené jsou jen veřejné metody `Store` ze seznamu níže (žádné `__getattr__`
do blba). Důvěra = přístup k socketu, tedy stejný uživatel OS; identita agenta
se posílá v argumentech stejně jako dnes u CLI.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import socket
import socketserver
import sys
import threading
from enum import Enum
from pathlib import Path

from .config import Settings
from .models import Claim, ClaimResult, Delivery, DeliveryState, MessageKind
from .store import Store

METHODS = frozenset({
    "claim", "release", "release_all", "holder_of", "claims", "edit_gate", "heartbeat", "peers",
    "say", "send", "inbox", "latest_messages", "record", "cursor", "set_cursor", "undelivered",
    "jobs", "addressed", "lease_next", "ack", "retry", "cancel", "history", "prune", "mark_seen",
    "accept", "awaiting_accept", "report_limit", "clear_limit", "limits", "is_available",
    "failover_sweep", "get_screen", "set_screen", "overdue", "extend_lease", "signal_mtime",
})
_TYPES = {"Claim": Claim, "ClaimResult": ClaimResult, "Delivery": Delivery}
_MAX_LINE = 1_000_000


def default_socket_path(settings: Settings) -> Path:
    env = os.environ.get("AGENT_LEASE_SOCKET")
    if env:
        return Path(env)
    return settings.db_path.parent / "broker.sock"


def encode(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        data = {f.name: encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        return {"__t": type(obj).__name__, **data}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: encode(v) for k, v in obj.items()}
    return obj


def decode(obj):
    if isinstance(obj, list):
        return [decode(x) for x in obj]
    if isinstance(obj, dict):
        tag = obj.get("__t")
        fields = {k: decode(v) for k, v in obj.items() if k != "__t"}
        if tag in _TYPES:
            if tag == "Delivery":
                fields["kind"] = MessageKind(fields["kind"])
                fields["state"] = DeliveryState(fields["state"])
            return _TYPES[tag](**fields)
        return fields
    return obj


class _Handler(socketserver.StreamRequestHandler):
    store: Store

    def handle(self) -> None:
        for raw in self.rfile:
            if len(raw) > _MAX_LINE:
                self._reply({"ok": False, "error": "ValueError", "message": "příliš velký požadavek"})
                return
            try:
                req = json.loads(raw)
                method = req["method"]
                if method == "ping":
                    self._reply({"ok": True, "result": "pong"})
                    continue
                if method not in METHODS:
                    raise PermissionError(f"metoda {method!r} není povolená")
                result = getattr(self.store, method)(*req.get("args", []), **req.get("kwargs", {}))
                self._reply({"ok": True, "result": encode(result)})
            except (PermissionError, ValueError, TypeError, KeyError) as exc:
                self._reply({"ok": False, "error": type(exc).__name__, "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._reply({"ok": False, "error": "RuntimeError", "message": str(exc)})

    def _reply(self, payload: dict) -> None:
        self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        self.wfile.flush()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(settings: Settings, path: Path | None = None) -> _Server:
    path = path or default_socket_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        # živý broker → odmítnout, mrtvý soubor → uklidit
        if ping(path):
            raise RuntimeError(f"broker už běží na {path}")
        path.unlink()
    handler = type("Handler", (_Handler,), {"store": Store(settings=settings)})
    old = os.umask(0o177)  # socket vznikne rovnou 0600
    try:
        server = _Server(str(path), handler)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return server


def ping(path: Path, timeout: float = 1.0) -> bool:
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall(b'{"method":"ping"}\n')
            return "pong" in s.makefile().readline()
    except OSError:
        return False


class RemoteStore:
    """Má stejné rozhraní jako `Store`, ale každé volání jde přes broker."""

    def __init__(self, settings: Settings, path: Path | None = None) -> None:
        self.settings = settings
        self.path = path or default_socket_path(settings)
        self.db_path = settings.db_path

    def _call(self, method: str, args, kwargs):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(30)
            s.connect(str(self.path))
            s.sendall((json.dumps({"method": method, "args": encode(list(args)),
                                   "kwargs": encode(kwargs)}) + "\n").encode())
            reply = json.loads(s.makefile().readline())
        if reply["ok"]:
            return decode(reply["result"])
        exc = {"PermissionError": PermissionError, "ValueError": ValueError,
               "TypeError": TypeError, "KeyError": KeyError}.get(reply["error"], RuntimeError)
        raise exc(reply["message"])

    def __getattr__(self, name: str):
        if name not in METHODS:
            raise AttributeError(name)
        return lambda *a, **kw: self._call(name, a, kw)


def open_store(settings: Settings | None = None):
    """Broker, pokud běží (nebo je vyžádán `AGENT_LEASE_SOCKET`); jinak přímo SQLite."""
    settings = settings or Settings.from_env()
    path = default_socket_path(settings)
    if (os.environ.get("AGENT_LEASE_SOCKET") or path.exists()) and ping(path):
        return RemoteStore(settings, path)
    return Store(settings=settings)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-lease-broker")
    p.add_argument("--socket", default=None)
    args = p.parse_args(argv)
    settings = Settings.from_env()
    path = Path(args.socket) if args.socket else None
    try:
        server = serve(settings, path)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    store = server.RequestHandlerClass.store

    def sweeper() -> None:
        import time as _t
        while True:
            _t.sleep(15)
            try:
                store.failover_sweep()
            except Exception as exc:  # noqa: BLE001 — sweep nesmí shodit broker
                print(f"failover_sweep: {exc}", file=sys.stderr)

    threading.Thread(target=sweeper, daemon=True).start()
    print(f"broker poslouchá na {path or default_socket_path(settings)} (0600)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
