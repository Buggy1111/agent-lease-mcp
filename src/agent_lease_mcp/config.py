"""
Nastavení z prostředí, na jednom místě.

Identita agenta se čte na dvou místech (server i hook) a MUSÍ vyjít stejně —
kdyby se rozešla, agent by si zablokoval vlastní soubory a nikdo by nepochopil
proč. Proto je to funkce tady, ne dvě kopie fallbacku.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL_SECONDS = 1800
MAX_TTL_SECONDS = 8 * 3600
STALE_PEER_SECONDS = 900
RESERVED_HUMAN_AGENT = "michal"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    agent: str
    default_ttl: int

    @classmethod
    def from_env(cls) -> Settings:
        raw_db = os.environ.get("AGENT_LEASE_DB")
        db_path = Path(raw_db).expanduser() if raw_db else Path.home() / ".agent-lease" / "room.db"
        return cls(
            db_path=db_path,
            agent=cls._agent_from_env(),
            default_ttl=_int_env("AGENT_LEASE_TTL", DEFAULT_TTL_SECONDS),
        )

    @staticmethod
    def _agent_from_env() -> str:
        """
        `AGENT_NAME` nastavuje konfigurace klienta (`claude-code`, `codex`).

        Fallback je schválně ošklivý a unikátní: když ho uvidíš v `room`, je to
        signál, že někde chybí konfigurace — ne stav, ve kterém se má zůstat.
        """
        configured = os.environ.get("AGENT_NAME")
        if configured == RESERVED_HUMAN_AGENT:
            # Lidskou autoritu smí vytvořit jen autentizovaný webový endpoint.
            # CLI/MCP/hook se samotnou proměnnou prostředí za člověka vydávat nesmí.
            return f"untrusted-{RESERVED_HUMAN_AGENT}-{os.getpid()}"
        return configured or f"unconfigured-{socket.gethostname()}-{os.getpid()}"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default
