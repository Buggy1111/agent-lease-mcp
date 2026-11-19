"""
Sdílené nastavení testů. Musí se načíst dřív než `agent_lease_mcp` (pytest
načítá conftest první), protože jméno člověka se čte při importu konfigurace.
Testy používají člověka „michal" — stejně jako Michalova skutečná místnost.
"""

import os

os.environ.setdefault("AGENT_LEASE_HUMAN", "michal")
