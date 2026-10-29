"""
Množiny stavů doručení na jednom místě (refaktoring podle rozboru Noxe #516).

Množiny se úmyslně liší a nesmí se slévat do jedné: `needs_review` je konečný
stav pro čekání (`wait`) i pro `ack`, ale retence ho chrání a jde znovu spustit
(`retry`) i zrušit. Každá množina proto nese jméno podle toho, k čemu slouží.
"""

from __future__ import annotations

import re

from .models import DeliveryState as S


def _values(*states: S) -> frozenset[str]:
    return frozenset(s.value for s in states)


# Konec práce na doručení: `ack` z nich už nepřechází jinam, `wait` na nich končí.
TERMINAL = _values(S.SUCCEEDED, S.FAILED, S.NEEDS_REVIEW, S.CANCELLED, S.EXPIRED, S.DEAD_LETTER)
# Povolené přechody `ack` z vlastněného pracovního lease.
ACK_TRANSITIONS: dict[str, frozenset[str]] = {
    S.LEASED.value: TERMINAL | {S.STARTED.value},
    S.STARTED.value: TERMINAL,
}
# Úkol, na kterém se ještě pracuje nebo čeká na člověka: retence ho nesmaže.
UNFINISHED_TASK = _values(S.PENDING, S.LEASED, S.STARTED, S.NEEDS_REVIEW)
# Ve frontě, ještě nezačato (doručení/převzetí).
QUEUED = _values(S.PENDING, S.LEASED)
# Drží ho worker (lease token).
HELD = _values(S.LEASED, S.STARTED)
# Co jde vrátit do fronty (`retry`).
RETRYABLE = _values(S.PENDING, S.FAILED, S.NEEDS_REVIEW, S.DEAD_LETTER)
# Co už zrušit nejde (`cancel`).
UNCANCELLABLE = _values(S.SUCCEEDED, S.CANCELLED, S.EXPIRED)

_SAFE = re.compile(r"^[a-z_]+$")


def sql_in(states: frozenset[str]) -> str:
    """`('a','b')` pro SQL. Jen pro tyto konstanty, nikdy pro vstup od uživatele."""
    assert states and all(_SAFE.match(s) for s in states)
    return "(" + ",".join(f"'{s}'" for s in sorted(states)) + ")"
