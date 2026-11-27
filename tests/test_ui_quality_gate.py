"""Kontrola kvality v neonovém UI ve skutečném prohlížeči (review #56). Bez Playwrightu se přeskočí."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_lease_mcp.webui import PAGE_HTML

SCRIPT = Path(__file__).parent / "ui" / "quality_gate_checks.cjs"


def test_quality_gate_in_real_browser(tmp_path: Path):
    module = os.environ.get("PLAYWRIGHT_MODULE", "")
    if not shutil.which("node") or not module or not Path(module).exists():
        pytest.skip("nastav PLAYWRIGHT_MODULE na nainstalovaný playwright")
    page = tmp_path / "page.html"
    page.write_text(PAGE_HTML)
    run = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=180, check=False,
                         env={**os.environ, "UI_PAGE_HTML": str(page)})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "5 PASS" in run.stdout


def test_windows_hello_login_in_real_browser(tmp_path: Path):
    """Přidání klíče, přihlášení PINem a odmítnutí bez PINu přes skutečné HTTP API webu."""
    from http.server import ThreadingHTTPServer
    from threading import Thread

    from agent_lease_mcp.config import Settings
    from agent_lease_mcp.store import Store
    from agent_lease_mcp.webui import issue_login_code, make_handler

    module = os.environ.get("PLAYWRIGHT_MODULE", "")
    if not shutil.which("node") or not module or not Path(module).exists():
        pytest.skip("nastav PLAYWRIGHT_MODULE na nainstalovaný playwright")
    settings = Settings(db_path=tmp_path / "room.db", agent="web", default_ttl=1800)
    store = Store(settings=settings)
    for i in range(5):
        store.send("codex", "michal", f"TEST ONLY {i}", kind="chat")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, settings, "t", 0))
    except PermissionError:
        pytest.skip("loopback socket není povolený")
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(store, settings, "t", port)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run = subprocess.run(["node", str(Path(__file__).parent / "ui" / "passkey_checks.cjs"),
                              f"http://localhost:{port}/", issue_login_code(settings)],
                             capture_output=True, text=True, timeout=180, check=False)
        assert run.returncode == 0 and "PASSKEY OK" in run.stdout, run.stdout + run.stderr
    finally:
        server.shutdown()
        server.server_close()
