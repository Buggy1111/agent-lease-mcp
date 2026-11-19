"""Sdílené konstanty, SQL kousky a převodníky řádků pro části `Store`."""

from __future__ import annotations

import json
import sqlite3

from .config import (
    valid_agent_name,
)
from .hygiene import (
    check_name,
)
from .models import Claim

# Jak dlouho se nepotvrzená zpráva připomíná (pak už jen v `overdue`).
ACCEPT_WINDOW_SECONDS = 3 * 86400

# Poznámka, se kterou bridge přijímá úkol (migrace podle ní pozná staré bridge receipty).
BRIDGE_ACCEPT_NOTE = "bridge took the task, starting a headless run (read-only)"


# Společné kousky čtení zpráv (Nox, rozbor #516). WHERE/ORDER/LIMIT zůstávají
# u každé metody zvlášť; nesjednocovat LEFT a INNER JOIN.
_MSG_COLS = ("m.*, d.state AS delivery_state, d.attempts, d.last_error, "
             "f.repo AS fix_repo, f.base_commit AS fix_base, f.allowed AS fix_allowed")
_RECEIPT_COLS = "r.seen_at, r.accepted_at, r.note AS accept_note"
_FROM_MSG = ("FROM messages m LEFT JOIN deliveries d "
             "ON d.message_id=m.id AND d.recipient=m.recipient "
             # C1a: člověk musí vidět přesný rozsah opravy, kterou schvaluje (Nox, #13)
             "LEFT JOIN fix_tasks f ON f.message_id=m.id")
_JOIN_RECEIPTS = "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=m.recipient"

def _check_agent(agent: str) -> None:
    if not valid_agent_name(agent) and not agent.startswith(("untrusted-", "unconfigured-")):
        raise ValueError(f"invalid agent name: {agent!r}")
    check_name("agent", agent)


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        path=row["path"],
        agent=row["agent"],
        purpose=row["purpose"],
        claimed_at=row["claimed_at"],
        expires_at=row["expires_at"],
    )


def _job_state(row: sqlite3.Row) -> str:
    """Úkol má vlastní stavy. Zpráva (chat/result/control) se nikdy neprovádí,
    takže její `pending` by visel navždy: ve výpisu se ukáže, jestli ji příjemce
    viděl nebo převzal. Uložený stav doručení se nemění (retence s ním počítá)."""
    if row["kind"] == "task" or row["state"] != "pending":
        return row["state"]
    keys = row.keys()
    if "accepted_at" in keys and row["accepted_at"]:
        return "accepted"
    if "seen_at" in keys and row["seen_at"]:
        return "seen"
    return "unread"


def _row_to_job(row: sqlite3.Row, now: float) -> dict:
    return {
        "id": row["id"], "agent": row["agent"], "recipient": row["recipient"],
        "kind": row["kind"], "text": row["text"], "state": _job_state(row),
        "attempts": row["attempts"],
        # APP-003: lease token se vrací jen vlastníkovi v `lease_next`, nikdy ve výpisu.
        "lease_until": row["lease_until"], "last_error": row["last_error"],
        "reply_to": row["reply_to"], "seconds_ago": int(now - row["sent_at"]),
    }


def _row_to_message(row: sqlite3.Row, now: float) -> dict:
    keys = row.keys()
    return {
        "id": row["id"], "agent": row["agent"], "recipient": row["recipient"],
        "kind": row["kind"], "text": row["text"], "reply_to": row["reply_to"],
        "state": row["delivery_state"], "attempts": row["attempts"],
        "last_error": row["last_error"], "seconds_ago": int(now - row["sent_at"]),
        "seen": bool(row["seen_at"]) if "seen_at" in keys else None,
        "accepted": bool(row["accepted_at"]) if "accepted_at" in keys else None,
        "accept_note": row["accept_note"] if "accept_note" in keys else "",
        "fix": ({"repo": row["fix_repo"], "base_commit": row["fix_base"],
                 "allowed": json.loads(row["fix_allowed"])}
                if "fix_repo" in keys and row["fix_repo"] else None),
    }


def _limit_reset_passed(provider: dict | None, now: float) -> bool:
    return bool(provider and provider.get("state") == "limited"
                and provider.get("resets_at") is not None and provider["resets_at"] <= now)


def _presence_state(status: str, age: float, stale_after: int) -> str:
    if age <= stale_after:
        lowered = status.lower()
        if "rate-limit" in lowered or "rate limit" in lowered or "kvót" in lowered:
            return "rate-limited"
        return "busy" if status else "idle"
    if status and age <= stale_after * 2:
        return "stalled"
    return "offline"


def _limit(value: int) -> int:
    """LIMIT do rozumného rozsahu; záporné číslo by v SQLite znamenalo „vše" (APP-009)."""
    return max(1, min(int(value), 500))
