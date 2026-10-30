"""Fronta úkolů: aktivace workerů, lease, ack, retry, cancel.

Část `Store`; rozděleno ze store.py podle rozboru Noxe (#516).
"""

from __future__ import annotations

import sqlite3
import time
import uuid

from ._store_common import (
    _check_agent,
)
from .config import (
    MAX_TASK_LEASE_SECONDS,
    STALE_PEER_SECONDS,
)
from .models import Delivery, DeliveryState, MessageKind
from .states import (
    ACK_TRANSITIONS,
    HELD,
    QUEUED,
    RETRYABLE,
    TERMINAL,
    UNCANCELLABLE,
    sql_in,
)


class DeliveriesMixin:
    def worker_activation(self, agent: str, *, include_existing: bool = False) -> int:
        """Create once and return the durable lower task-id bound for a worker."""
        return self._activate("worker_activations", "worker-activate", agent, include_existing)

    # Jen tyto dvě tabulky; jméno tabulky jde do SQL, proto pevný whitelist.
    _ACTIVATION_TABLES = frozenset({"failover_activations", "worker_activations"})

    def _activate(self, table: str, action: str, agent: str, include_existing: bool) -> int:
        """Společné jádro worker/failover aktivace (Nox, rozbor #516)."""
        if table not in self._ACTIVATION_TABLES:
            raise ValueError(f"unknown activation table: {table}")
        _check_agent(agent)
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                f"SELECT after_message_id FROM {table} WHERE agent=?", (agent,)
            ).fetchone()
            if row:
                return int(row["after_message_id"])
            after = 0
            if not include_existing:
                after = int(con.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages"
                ).fetchone()[0])
            con.execute(
                f"INSERT INTO {table}(agent, after_message_id, activated_at) VALUES(?,?,?)",
                (agent, after, now),
            )
            self._record(con, agent, action, "", f"after #{after}")
            return after

    def activation_status(self, agent: str) -> dict:
        """Return persisted safety baselines without creating or changing them."""
        _check_agent(agent)
        with self._connect() as con:
            worker = con.execute(
                "SELECT after_message_id FROM worker_activations WHERE agent=?", (agent,)
            ).fetchone()
            failover = con.execute(
                "SELECT after_message_id FROM failover_activations WHERE agent=?", (agent,)
            ).fetchone()
        return {
            "worker_after_message_id": int(worker["after_message_id"]) if worker else None,
            "failover_after_message_id": int(failover["after_message_id"]) if failover else None,
        }

    @staticmethod
    def _expire_leases(con: sqlite3.Connection, now: float, recipient: str | None = None) -> int:
        """
        Prošlé lease a termíny: `leased` → `pending` (nic nespuštěno), `started` →
        `needs_review` (nevíme, co proběhlo — nikdy tiché opakování), prošlý
        `expires_at` → `expired`. Bez `recipient` pro všechny (reconciliace brokeru).
        """
        who, args = ("", ()) if recipient is None else (" AND recipient=?", (recipient,))
        changed = con.execute(
            "UPDATE deliveries SET state='expired', lease_token=NULL, lease_until=NULL, "
            f"updated_at=? WHERE state='pending'{who} AND message_id IN "
            "(SELECT id FROM messages WHERE expires_at IS NOT NULL AND expires_at<=?)",
            (now, *args, now),
        ).rowcount
        changed += con.execute(
            "UPDATE deliveries SET state='needs_review', lease_token=NULL, lease_until=NULL, "
            "last_error='worker lease expired after start', updated_at=? "
            f"WHERE state='started'{who} AND lease_until<=?",
            (now, *args, now),
        ).rowcount
        changed += con.execute(
            "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
            f"updated_at=? WHERE state='leased'{who} AND lease_until<=?",
            (now, *args, now),
        ).rowcount
        return changed

    def reconcile(self) -> int:
        """
        Pravidelný úklid fronty pro VŠECHNY adresáty (volá broker sweeper).
        Dřív se prošlé lease uklízely jen v `lease_next` adresáta — agent v limitu
        tak nechal přerušený `started` viset (Nox, audit #535 bod 1).
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            return self._expire_leases(con, time.time())

    def lease_next(
        self, recipient: str, *, lease_seconds: int = 60, after_message_id: int = 0,
        skip_accepted: bool = False, defer_if_present: float = 0,
    ) -> Delivery | None:
        """
        Atomicky převezme nejstarší připravený task pro jeden worker.

        Pro autonomní worker (bridge), který sdílí frontu s interaktivní session
        stejného jména:
        - `skip_accepted` — task, který adresát už vědomě přijal (`accept`), patří
          jemu; worker ho nesmí vzít taky (jinak by proběhl dvakrát).
        - `defer_if_present` — je-li adresát přítomný (heartbeat), nechá worker
          task tolik sekund jemu a převezme ho až potom.
        """
        lease_seconds = max(1, min(int(lease_seconds), MAX_TASK_LEASE_SECONDS))  # APP-009
        after_message_id = max(0, int(after_message_id))
        now = time.time()
        token = uuid.uuid4().hex
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._expire_leases(con, now, recipient)
            newest = None  # None = bez odkladu
            if defer_if_present > 0:
                seen = con.execute(
                    "SELECT seen_at FROM agents WHERE agent=?", (recipient,)
                ).fetchone()
                if seen and now - seen["seen_at"] <= STALE_PEER_SECONDS:
                    newest = now - float(defer_if_present)
            rows = con.execute(
                "SELECT m.*, d.state, d.attempts, COALESCE(tm.priority, 0) AS priority "
                "FROM deliveries d "
                "JOIN messages m ON m.id=d.message_id "
                "LEFT JOIN task_metadata tm ON tm.message_id=m.id "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=d.recipient "
                "WHERE d.recipient=? AND d.state='pending' AND m.kind='task' "
                "AND m.id>? "
                "AND m.not_before<=? AND (m.expires_at IS NULL OR m.expires_at>?) "
                "AND (? IS NULL OR m.sent_at<=?) "
                "AND (? = 0 OR r.accepted_at IS NULL OR r.by_lease = 1) "
                "ORDER BY COALESCE(tm.priority, 0) DESC, m.id",
                (recipient, after_message_id, now, now, newest, newest, int(bool(skip_accepted))),
            ).fetchall()
            row = None
            for candidate in rows:
                paths = [r["path"] for r in con.execute(
                    "SELECT path FROM task_paths WHERE message_id=? ORDER BY path",
                    (candidate["id"],),
                ).fetchall()]
                if not paths or not self._conflicts(con, paths, recipient):
                    row = candidate
                    break
            if row is None:
                return None
            con.execute(
                "UPDATE deliveries SET state='leased', attempts=attempts+1, "
                "lease_token=?, lease_until=?, updated_at=? "
                "WHERE message_id=? AND recipient=? AND state='pending'",
                (token, now + max(1, lease_seconds), now, row["id"], recipient),
            )
            return Delivery(
                message_id=row["id"], sender=row["agent"], recipient=recipient,
                kind=MessageKind(row["kind"]), text=row["text"],
                state=DeliveryState.LEASED, attempts=row["attempts"] + 1,
                lease_token=token, lease_until=now + max(1, lease_seconds),
                reply_to=row["reply_to"],
            )

    def ack(
        self,
        message_id: int,
        recipient: str,
        state: DeliveryState | str,
        *,
        lease_token: str | None = None,
        error: str = "",
    ) -> bool:
        """Potvrdí pouze platný přechod vlastněného pracovního lease."""
        state_value = DeliveryState(state).value
        terminal, allowed = TERMINAL, ACK_TRANSITIONS
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT state, lease_token, lease_until FROM deliveries "
                "WHERE message_id=? AND recipient=?",
                (message_id, recipient),
            ).fetchone()
            if not row:
                return False
            current = row["state"]
            # Prošlý lease už nepatří volajícímu: nesmí ho oživit ani dokončit, i když
            # ho úklid ještě nestihl přepnout (Nox, audit #535 bod 2b).
            if (current in HELD and row["lease_until"] is not None
                    and row["lease_until"] <= time.time()):
                return False
            # Opakované potvrzení téhož finálního výsledku je bezpečně idempotentní.
            if current in terminal and state_value == current:
                return True
            if state_value not in allowed.get(current, set()):
                return False
            if not lease_token or row["lease_token"] != lease_token:
                return False
            clear_lease = state_value in terminal
            cur = con.execute(
                "UPDATE deliveries SET state=?, last_error=?, updated_at=?, "
                "lease_token=CASE WHEN ? THEN NULL ELSE lease_token END, "
                "lease_until=CASE WHEN ? THEN NULL ELSE lease_until END "
                "WHERE message_id=? AND recipient=? AND state=?",
                (state_value, error, time.time(), clear_lease, clear_lease,
                 message_id, recipient, current),
            )
        return bool(cur.rowcount)

    def _may_manage(self, con: sqlite3.Connection, message_id: int, recipient: str,
                    actor: str | None) -> bool:
        """`actor=None` = ověřený člověk (web). Agent smí spravovat jen svoje nebo jím zadané."""
        if actor is None:
            return True
        row = con.execute("SELECT agent FROM messages WHERE id=?", (message_id,)).fetchone()
        return bool(row) and actor in (recipient, row["agent"])

    def retry(self, message_id: int, recipient: str, *, not_before: float = 0,
              actor: str | None = None) -> bool:
        """Vrátí neúspěšné doručení nebo přeplánuje pending úkol; zachová pokusy."""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not self._may_manage(con, message_id, recipient, actor):
                return False
            cur = con.execute(
                "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
                "last_error='', updated_at=? WHERE message_id=? AND recipient=? "
                f"AND state IN {sql_in(RETRYABLE)}",
                (time.time(), message_id, recipient),
            )
            if cur.rowcount:
                con.execute(
                    "UPDATE messages SET not_before=? WHERE id=?",
                    (not_before, message_id),
                )
        return bool(cur.rowcount)

    def cancel(self, message_id: int, recipient: str, *, actor: str | None = None) -> bool:
        """Zruší nedokončené doručení; výsledek zůstane v auditu fronty."""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not self._may_manage(con, message_id, recipient, actor):
                return False
            cur = con.execute(
                "UPDATE deliveries SET state='cancelled', lease_token=NULL, lease_until=NULL, "
                "updated_at=? WHERE message_id=? AND recipient=? "
                f"AND state NOT IN {sql_in(UNCANCELLABLE)}",
                (time.time(), message_id, recipient),
            )
        return bool(cur.rowcount)

    def overdue(self, seconds: int = 120) -> list[dict]:
        """Adresované zprávy, které déle než `seconds` nikdo nepotvrdil (nebo ani neviděl)."""
        now = time.time()
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.id, m.agent, m.recipient, m.kind, m.text, m.sent_at, "
                "r.seen_at, r.accepted_at FROM messages m "
                "JOIN deliveries d ON d.message_id=m.id AND d.recipient=m.recipient "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=m.recipient "
                f"WHERE m.kind IN ('chat','task','control') AND d.state IN {sql_in(QUEUED)} "
                "AND r.accepted_at IS NULL AND m.sent_at <= ? ORDER BY m.id",
                (now - seconds,),
            ).fetchall()
        return [
            {"id": r["id"], "from": r["agent"], "to": r["recipient"], "kind": r["kind"],
             "text": r["text"], "waiting_seconds": int(now - r["sent_at"]),
             "problem": "seen, not acknowledged" if r["seen_at"] else "not delivered"}
            for r in rows
        ]

    def extend_lease(self, message_id: int, recipient: str, lease_token: str,
                     lease_seconds: int = 60) -> bool:
        """Heartbeat dlouhé práce: bez něj by běžící task po vypršení lease skončil v needs_review."""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE deliveries SET lease_until=?, updated_at=? WHERE message_id=? "
                f"AND recipient=? AND lease_token=? AND state IN {sql_in(HELD)} "
                "AND lease_until>?",  # prošlý lease nejde oživit (audit #535 bod 2b)
                (time.time() + max(1, min(int(lease_seconds), MAX_TASK_LEASE_SECONDS)),
                 time.time(), message_id, recipient,
                 lease_token, time.time()),
            )
        return bool(cur.rowcount)

    def needs_review_list(self, limit: int = 20) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.id, m.recipient, m.text, d.last_error, d.updated_at FROM deliveries d "
                "JOIN messages m ON m.id=d.message_id WHERE d.state IN "
                "('needs_review','dead_letter','failed') ORDER BY m.id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [{"id": r["id"], "to": r["recipient"], "text": r["text"],
                 "error": r["last_error"], "updated_at": r["updated_at"]} for r in rows]

    def get_screen(self, message_id: int) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT risk, actionable FROM screens WHERE message_id=?",
                              (message_id,)).fetchone()
        return {"risk": float(row["risk"]), "actionable": row["actionable"]} if row else None

    def set_screen(self, message_id: int, risk: float, actionable: float | None = None) -> None:
        with self._connect() as con:
            con.execute("INSERT OR REPLACE INTO screens(message_id, risk, actionable, at) "
                        "VALUES(?,?,?,?)", (message_id, risk, actionable, time.time()))
