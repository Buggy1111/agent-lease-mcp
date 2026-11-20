"""
Sdílené nastavení testů. Musí se načíst dřív než `agent_lease_mcp` (pytest
načítá conftest první), protože jméno člověka se čte při importu konfigurace.
Testy používají člověka „michal" — stejně jako Michalova skutečná místnost.
"""

import os

os.environ.setdefault("AGENT_LEASE_HUMAN", "michal")

# Podprocesy testů (`python -m agent_lease_mcp.process_guard` aj.) musí najít testovaný kód i bez
# instalace balíčku — v izolovaném běhu Quality Gate (B7) žádné `pip install -e` není.
_SRC = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")
os.environ["PYTHONPATH"] = os.pathsep.join(
    p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)
