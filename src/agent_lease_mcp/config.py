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
RESERVED_HUMAN_AGENT = "michal"

# Horní meze vstupů (APP-009): bez nich jde jednou zprávou nafouknout DB i kontext
# druhého agenta. Hodnoty jsou štědré pro běžnou práci, ne pro hromadná data.
MAX_TEXT_CHARS = 20_000
MAX_NAME_CHARS = 64
MAX_SHORT_CHARS = 500
MAX_PATH_CHARS = 4096
MAX_PATHS_PER_CLAIM = 64
RETENTION_DAYS = 30

# Jména, která si agent nesmí vzít přes prostředí: lidská autorita a interní role.
RESERVED_NAMES = frozenset({RESERVED_HUMAN_AGENT, "human", "user", "system", "broker"})
UNTRUSTED_PREFIXES = ("untrusted-", "unconfigured-")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def valid_agent_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def is_trusted_agent(name: str) -> bool:
    """Má jméno nakonfigurovaný (ne záložní) agent? Jen takový smí zadávat tasky."""
    return not name.startswith(UNTRUSTED_PREFIXES)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    agent: str
    default_ttl: int
    # Kdo smí vytvářet `task`/`control` (APP-005). Prázdná množina = výchozí
    # pravidlo: člověk a nakonfigurovaní agenti. Jinak jen vyjmenovaní.
    task_senders: frozenset[str] = frozenset()
    retention_days: int = RETENTION_DAYS
    fail_closed: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        raw_db = os.environ.get("AGENT_LEASE_DB")
        db_path = Path(raw_db).expanduser() if raw_db else Path.home() / ".agent-lease" / "room.db"
        return cls(
            db_path=db_path,
            agent=cls._agent_from_env(),
            default_ttl=_int_env("AGENT_LEASE_TTL", DEFAULT_TTL_SECONDS),
            task_senders=frozenset(
                x.strip() for x in os.environ.get("AGENT_LEASE_TASK_SENDERS", "").split(",")
                if x.strip()
            ),
            retention_days=_int_env("AGENT_LEASE_RETENTION_DAYS", RETENTION_DAYS),
            fail_closed=os.environ.get("AGENT_LEASE_FAIL_CLOSED", "") in ("1", "true", "yes"),
        )

    def may_send_task(self, sender: str) -> bool:
        if self.task_senders:
            return sender in self.task_senders
        return sender == RESERVED_HUMAN_AGENT or is_trusted_agent(sender)

    @staticmethod
    def _agent_from_env() -> str:
        """
        `AGENT_NAME` nastavuje konfigurace klienta (`claude-code`, `codex`).

        Fallback je schválně ošklivý a unikátní: když ho uvidíš v `room`, je to
        signál, že někde chybí konfigurace — ne stav, ve kterém se má zůstat.
        """
        configured = os.environ.get("AGENT_NAME")
        if configured in RESERVED_NAMES or (configured and not valid_agent_name(configured)):
            # Lidskou autoritu smí vytvořit jen autentizovaný webový endpoint.
            # CLI/MCP/hook se samotnou proměnnou prostředí za člověka vydávat nesmí
            # (ani za interní roli) a divné znaky ve jménu končí v nedůvěryhodné větvi.
            return f"untrusted-{(configured or 'x')[:16]}-{os.getpid()}"
        return configured or f"unconfigured-{socket.gethostname()[:24]}-{os.getpid()}"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default
