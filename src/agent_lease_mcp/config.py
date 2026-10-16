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
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TTL_SECONDS = 1800
MAX_TTL_SECONDS = 8 * 3600
STALE_PEER_SECONDS = 900
# Jméno člověka v místnosti. Vyhrazené: smí ho použít jen autentizované web UI,
# agent se za člověka vydávat nesmí. Pravdu drží soubor `human-name` vedle DB
# (zakládá ho web UI), ne proměnná prostředí agenta — tu si agent nastaví sám.
# Michalova instalace: AGENT_LEASE_HUMAN=michal.
HUMAN_AGENT = os.environ.get("AGENT_LEASE_HUMAN", "human").strip() or "human"
RESERVED_HUMAN_AGENT = HUMAN_AGENT
HUMAN_NAME_FILE = "human-name"

# Horní meze vstupů (APP-009): bez nich jde jednou zprávou nafouknout DB i kontext
# druhého agenta. Hodnoty jsou štědré pro běžnou práci, ne pro hromadná data.
MAX_TEXT_CHARS = 20_000
MAX_NAME_CHARS = 64
MAX_SHORT_CHARS = 500
MAX_PATH_CHARS = 4096
MAX_PATHS_PER_CLAIM = 64
RETENTION_DAYS = 30
MAX_TASK_LEASE_SECONDS = 3600

# Jména, která si agent nesmí vzít přes prostředí: lidská autorita a interní role.
RESERVED_NAMES = frozenset({RESERVED_HUMAN_AGENT, "human", "michal", "user", "system", "broker"})
UNTRUSTED_PREFIXES = ("untrusted-", "unconfigured-")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def valid_agent_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def _label(value: str) -> str:
    """Jméno v místnosti: ASCII písmena, číslice, `.`, `_`, `-`, max 64 znaků."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:64]


def human_name(db_path: Path) -> str:
    """Jméno člověka pro tuhle místnost (soubor vedle DB, jinak AGENT_LEASE_HUMAN)."""
    try:
        stored = (db_path.parent / HUMAN_NAME_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    return _label(stored) or _label(HUMAN_AGENT) or "human"


def reserved_names(db_path: Path) -> set[str]:
    """Jména, která agent přes AGENT_NAME nikdy nedostane (bez ohledu na velikost)."""
    return {n.lower() for n in (*RESERVED_NAMES, human_name(db_path))}


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
    # Řetězce náhrady při vyčerpaném limitu: {"codex": ("claude-code", "openrouter")}.
    fallbacks: dict = field(default_factory=dict, hash=False, compare=False)
    failover_grace: int = 60
    # Agent bez limitu, který jen dlouho nedal znamení (spí v `wait`), se předbíhat nesmí hned.
    offline_grace: int = 900

    @classmethod
    def from_env(cls) -> Settings:
        raw_db = os.environ.get("AGENT_LEASE_DB")
        db_path = Path(raw_db).expanduser() if raw_db else Path.home() / ".agent-lease" / "room.db"
        return cls(
            db_path=db_path,
            agent=cls._agent_from_env(db_path),
            default_ttl=_int_env("AGENT_LEASE_TTL", DEFAULT_TTL_SECONDS),
            task_senders=frozenset(
                x.strip() for x in os.environ.get("AGENT_LEASE_TASK_SENDERS", "").split(",")
                if x.strip()
            ),
            retention_days=_int_env("AGENT_LEASE_RETENTION_DAYS", RETENTION_DAYS),
            fallbacks=parse_fallbacks(os.environ.get("AGENT_LEASE_FALLBACKS", "")),
            failover_grace=_int_env("AGENT_LEASE_FAILOVER_GRACE", 60),
            offline_grace=_int_env("AGENT_LEASE_OFFLINE_GRACE", 900),
            fail_closed=os.environ.get("AGENT_LEASE_FAIL_CLOSED", "") in ("1", "true", "yes"),
        )

    def may_send_task(self, sender: str) -> bool:
        if self.task_senders:
            return sender in self.task_senders
        return sender == human_name(self.db_path) or is_trusted_agent(sender)

    @staticmethod
    def _agent_from_env(db_path: Path) -> str:
        """
        `AGENT_NAME` nastavuje konfigurace klienta (`claude-code`, `codex`).

        Fallback je schválně ošklivý a unikátní: když ho uvidíš v `room`, je to
        signál, že někde chybí konfigurace — ne stav, ve kterém se má zůstat.
        """
        raw = os.environ.get("AGENT_NAME") or ""
        configured = _label(raw)
        if raw and (configured.lower() in reserved_names(db_path) or not valid_agent_name(raw)):
            # Lidskou autoritu smí vytvořit jen autentizovaný webový endpoint.
            # CLI/MCP/hook se samotnou proměnnou prostředí za člověka vydávat nesmí
            # (ani za interní roli). Kontroluje se až normalizovaný tvar, takže
            # `michal\u200b`, homoglyfy ani nový řádek ve jménu neprojdou.
            return f"untrusted-{(configured or 'x')[:16]}-{os.getpid()}"
        return configured or f"unconfigured-{socket.gethostname()[:24]}-{os.getpid()}"


def parse_fallbacks(raw: str) -> dict[str, tuple[str, ...]]:
    """`codex=claude-code,openrouter;claude-code=codex,openrouter` → řetězce náhrady."""
    out: dict[str, tuple[str, ...]] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        agent, chain = part.split("=", 1)
        names = tuple(n.strip() for n in chain.split(",") if n.strip())
        if agent.strip() and names:
            out[agent.strip()] = names
    return out


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default
