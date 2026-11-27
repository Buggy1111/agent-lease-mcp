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
