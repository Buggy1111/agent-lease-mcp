"""Slovník domény. Žádné IO, žádná závislost na SQLite ani na MCP."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class Claim:
    path: str
    agent: str
    purpose: str
    claimed_at: float
    expires_at: float

    @property
    def expires_in(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "held_by": self.agent,
            "purpose": self.purpose,
            "expires_in_seconds": self.expires_in,
        }


@dataclass(frozen=True)
class ClaimResult:
    """Prázdné `conflicts` = povedlo se všechno. Částečný úspěch neexistuje."""

    granted: list[str]
    conflicts: list[Claim]

    @property
    def ok(self) -> bool:
        return not self.conflicts


class Verdict(str, Enum):
    ALLOW = "allow"          # cesta je moje, nebo ji nehlídáme
    AUTO_CLAIMED = "auto"    # byla volná, vzali jsme ji za agenta
    DENY = "deny"            # drží ji někdo jiný


class MessageKind(str, Enum):
    """Zpráva v místnosti není automaticky oprávnění spustit práci."""

    CHAT = "chat"
    TASK = "task"
    BROADCAST = "broadcast"
    CONTROL = "control"
    RESULT = "result"


class DeliveryState(str, Enum):
    """Trvalý stav adresovaného doručení."""

    PENDING = "pending"
    LEASED = "leased"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class Delivery:
    message_id: int
    sender: str
    recipient: str
    kind: MessageKind
    text: str
    state: DeliveryState
    attempts: int
    lease_token: str | None = None
    lease_until: float | None = None
    reply_to: int | None = None


@dataclass(frozen=True)
class Decision:
    """
    Výsledek rozhodování hooku. Oddělené od CLI schválně: rozhodnutí se dá
    otestovat v procesu, exit kód je až tenká slupka kolem něj.
    """

    verdict: Verdict
    reason: str = ""
    path: str | None = None
    blocked_by: Claim | None = None

    @property
    def blocks(self) -> bool:
        return self.verdict is Verdict.DENY
