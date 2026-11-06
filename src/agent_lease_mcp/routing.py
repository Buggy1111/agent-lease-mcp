"""
Komu poslat úkol s `--to auto` (fáze B2).

Kandidát je jen skutečný agent: nikdy most (`*-bridge`), budík (`*-wake`), broker,
člověk ani nedůvěryhodná identita. Agent je dosažitelný, když je sám aktivní, nebo
když běží jeho most (úkol pak udělá most na pozadí).

Schopnosti agentů jsou krátký text pro výběr (Jev). Výchozí popisy lze přepsat
proměnnou `AGENT_LEASE_CAPABILITIES="codex=…;claude-code=…"`.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import reserved_names

SERVICE_SUFFIXES = ("-bridge", "-wake")
DEFAULT_CAPABILITIES = {
    "claude-code": "Ada (Claude Code): návrh, implementace, dokumentace, review, nasazení, práce s webem a prohlížečem.",
    "codex": "Nox (Codex): nezávislé review, hledání chyb a bezpečnostních děr, testy a jejich reprodukce, opravy.",
}


def is_service(name: str, db_path: Path) -> bool:
    """Servisní nebo lidská identita, na kterou se úkol automaticky nesměruje."""
    lowered = name.lower()
    return (lowered.endswith(SERVICE_SUFFIXES)
            or lowered.startswith(("untrusted-", "unconfigured-"))
            or lowered in reserved_names(db_path))


def capabilities() -> dict[str, str]:
    caps = dict(DEFAULT_CAPABILITIES)
    for item in os.environ.get("AGENT_LEASE_CAPABILITIES", "").split(";"):
        name, sep, text = item.partition("=")
        if sep and name.strip() and text.strip():
            caps[name.strip()] = text.strip()[:300]
    return caps


def candidates(peers: list[dict], me: str, db_path: Path) -> list[str]:
    """Dosažitelní skuteční agenti kromě odesílatele, ve stálém pořadí."""
    active = {p["agent"] for p in peers if p.get("active")}
    names = {p["agent"] for p in peers}
    found = set()
    for name in names | {n[: -len("-bridge")] for n in active if n.endswith("-bridge")}:
        if name == me or is_service(name, db_path):
            continue
        if name in active or f"{name}-bridge" in active:
            found.add(name)
    return sorted(found)
