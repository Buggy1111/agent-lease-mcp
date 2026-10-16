"""
Nastavení z prostředí, na jednom místě.

Identita agenta se čte na dvou místech (server i hook) a MUSÍ vyjít stejně —
kdyby se rozešla, agent by si zablokoval vlastní soubory a nikdo by nepochopil
proč. Proto je to funkce tady, ne dvě kopie fallbacku.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL_SECONDS = 1800
MAX_TTL_SECONDS = 8 * 3600
STALE_PEER_SECONDS = 900
# Jméno člověka v místnosti. Vyhrazené: smí ho použít jen autentizované web UI,
# agent se za člověka vydávat nesmí. Michalova instalace: AGENT_LEASE_HUMAN=michal.
HUMAN_AGENT = os.environ.get("AGENT_LEASE_HUMAN", "human").strip() or "human"
RESERVED_HUMAN_AGENT = HUMAN_AGENT
HUMAN_NAME_FILE = "human-name"


def human_name(db_path: Path) -> str:
    """
    Jméno člověka pro tuhle místnost. Pravdu drží soubor vedle DB (zapisuje ho
    web UI), ne proměnná prostředí agenta — tu si agent může nastavit sám a
    pak by se za člověka vydával (security review 16. 10.).
    """
    try:
        stored = (db_path.parent / HUMAN_NAME_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    return stored or HUMAN_AGENT


def reserved_names(db_path: Path) -> set[str]:
    """Jména, která agent přes AGENT_NAME nikdy nedostane."""
    return {"human", "michal", human_name(db_path), HUMAN_AGENT}

# Meze vstupů (APP-009). Místnost je lokální, ale jeden zacyklený agent ji umí
# zahltit stejně dobře jako útočník — a negativní LIMIT v SQLite znamená „vše".
MAX_TEXT_CHARS = 8000
MAX_SHORT_CHARS = 500          # status, purpose, chyba úkolu
MAX_LIST_LIMIT = 500
MAX_TASK_LEASE_SECONDS = 3600

# Retence (APP-012): staré vzkazy a audit se mažou samy. Nedokončené úkoly nikdy.
DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class Settings:
    db_path: Path
    agent: str
    default_ttl: int
    retention_days: int = DEFAULT_RETENTION_DAYS

    @classmethod
    def from_env(cls) -> Settings:
        raw_db = os.environ.get("AGENT_LEASE_DB")
        db_path = Path(raw_db).expanduser() if raw_db else Path.home() / ".agent-lease" / "room.db"
        return cls(
            db_path=db_path,
            agent=cls._agent_from_env(db_path),
            default_ttl=_int_env("AGENT_LEASE_TTL", DEFAULT_TTL_SECONDS),
            retention_days=_int_env("AGENT_LEASE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
        )

    @staticmethod
    def _agent_from_env(db_path: Path) -> str:
        """
        `AGENT_NAME` nastavuje konfigurace klienta (`claude-code`, `codex`).

        Fallback je schválně ošklivý a unikátní: když ho uvidíš v `room`, je to
        signál, že někde chybí konfigurace — ne stav, ve kterém se má zůstat.
        """
        # Nejdřív normalizovat, pak kontrolovat — jinak `michal\u200b`, homoglyfy
        # nebo jiné bílé znaky projdou kontrolou a v UI vypadají jako člověk.
        # Jméno je jen štítek: ASCII písmena, číslice, `.`, `_`, `-`, max 64 znaků.
        raw = os.environ.get("AGENT_NAME") or ""
        configured = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")[:64]
        if configured and configured.lower() in {n.lower() for n in reserved_names(db_path)}:
            # Lidskou autoritu smí vytvořit jen autentizovaný webový endpoint.
            # CLI/MCP/hook se samotnou proměnnou prostředí za člověka vydávat nesmí.
            return f"untrusted-{configured}-{os.getpid()}"
        return configured or f"unconfigured-{socket.gethostname()}-{os.getpid()}"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default
