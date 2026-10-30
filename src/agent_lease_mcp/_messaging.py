"""Zprávy, příjmy, převzetí a čtení historie.

Část `Store`; rozděleno ze store.py podle rozboru Noxe (#516).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from ._store_common import (
    _FROM_MSG,
    _JOIN_RECEIPTS,
    _MSG_COLS,
    _RECEIPT_COLS,
    ACCEPT_WINDOW_SECONDS,
    _check_agent,
    _job_state,
    _limit,
    _row_to_job,
    _row_to_message,
)
from .config import (
    MAX_PATHS_PER_CLAIM,
)
from .hygiene import (
    check_name,
    check_path,
    check_short,
    check_text,
    redact,
)
from .models import MessageKind
from .policy import normalize_path
from .states import (
    HELD,
    QUEUED,
    TERMINAL,
    sql_in,
)


class MessagingMixin:
    def say(self, agent: str, text: str) -> int:
        """Zpětně kompatibilní broadcast do místnosti."""
        return self.send(agent, "*", text, kind=MessageKind.BROADCAST)

    def send(
        self,
        agent: str,
        recipient: str,
        text: str,
        *,
        kind: MessageKind | str = MessageKind.CHAT,
        project: str = "",
        not_before: float = 0,
        expires_at: float | None = None,
        dedupe_key: str | None = None,
        reply_to: int | None = None,
        paths: list[str] | None = None,
        priority: int = 0,
    ) -> int:
        """Atomicky uloží adresovanou zprávu a její doručení."""
        kind_value = MessageKind(kind).value
        _check_agent(agent)
        recipient = check_name("recipient", recipient.strip())
        if not recipient:
            raise ValueError("recipient must not be empty")
        if not text.strip():
            raise ValueError("text must not be empty")
        text = redact(check_text(text))
        check_short("project", project)
        if dedupe_key:
            check_name("dedupe_key", dedupe_key)
        paths = paths or []
        if len(paths) > MAX_PATHS_PER_CLAIM:
            raise ValueError(f"at most {MAX_PATHS_PER_CLAIM} task paths are allowed")
        task_paths = sorted({normalize_path(check_path(path)) for path in paths})
        priority = int(priority)
        if not -100 <= priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        if kind_value != MessageKind.TASK.value and (task_paths or priority):
            raise ValueError("paths and priority are only valid for tasks")
        if kind_value in (MessageKind.TASK.value, MessageKind.CONTROL.value) and (
            not self.settings.may_send_task(agent)
        ):
            # APP-005: z textu zprávy jiného agenta nesmí vzniknout spustitelná práce.
            raise PermissionError(f"{agent} may not create a {kind_value}")
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if dedupe_key:
                existing = con.execute(
                    "SELECT id FROM messages WHERE agent = ? AND dedupe_key = ?",
                    (agent, dedupe_key),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            cur = con.execute(
                "INSERT INTO messages(agent, text, sent_at, recipient, kind, project, "
                "not_before, expires_at, dedupe_key, reply_to) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (agent, text, now, recipient, kind_value, project, not_before,
                 expires_at, dedupe_key, reply_to),
            )
            message_id = int(cur.lastrowid)
            if kind_value == MessageKind.TASK.value:
                con.execute(
                    "INSERT INTO task_metadata(message_id, priority) VALUES(?,?)",
                    (message_id, priority),
                )
                con.executemany(
                    "INSERT INTO task_paths(message_id, path) VALUES(?,?)",
                    [(message_id, path) for path in task_paths],
                )
            if recipient != "*" and kind_value != MessageKind.RESULT.value:
                con.execute(
                    "INSERT INTO deliveries(message_id, recipient, updated_at) VALUES(?,?,?)",
                    (message_id, recipient, now),
                )
        self._signal(recipient)
        return message_id

    def _signal_path(self, recipient: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in recipient) or "_"
        return self.db_path.parent / "signals" / safe

    def _signal(self, recipient: str) -> None:
        """Zvedne „signální" soubor adresáta; `wait` na něj reaguje do desetin sekundy."""
        targets = [recipient] if recipient != "*" else [p["agent"] for p in self.peers(10**9)]
        for name in targets:
            path = self._signal_path(name)
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.touch(mode=0o600)
                os.utime(path, None)
            except OSError:
                pass  # signál je optimalizace; pravda je v DB a `wait` ji stejně dotazuje

    def signal_mtime(self, recipient: str) -> float:
        try:
            return self._signal_path(recipient).stat().st_mtime
        except OSError:
            return 0.0

    def mark_seen(self, recipient: str, message_ids: list[int]) -> list[int]:
        """
        Zaznamená, že zprávy dorazily do kontextu `recipient`, a dá o tom vědět
        odesílateli (krátký automatický příjem). Vrací jen NOVĚ spatřené id,
        takže opakované vložení nepíše další příjmy.
        """
        fresh: list[int] = []
        senders: set[str] = set()
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for mid in message_ids:
                msg = con.execute(
                    "SELECT agent, kind, recipient, text FROM messages WHERE id=?", (mid,)
                ).fetchone()
                if (not msg or msg["recipient"] != recipient or msg["agent"] == recipient
                        or msg["kind"] == MessageKind.RESULT.value):
                    continue  # příjemky na příjemky se nepíšou (nekonečná smyčka)
                senders.add(msg["agent"])
                if con.execute(
                    "INSERT OR IGNORE INTO receipts(message_id, recipient, seen_at) VALUES(?,?,?)",
                    (mid, recipient, now),
                ).rowcount:
                    fresh.append(mid)
                    if msg["agent"] != "broker":
                        snippet = " ".join(msg["text"].split())[:60]
                        self._insert_event(con, "broker", msg["agent"],
                                           f"✓ {recipient} saw #{mid} (“{snippet}”)", mid, now)
        for sender in senders:
            self._signal(sender)
        return fresh

    def accept(self, message_id: int, recipient: str, note: str = "",
               lease_token: str | None = None, owner: str | None = None) -> bool:
        """
        Vědomé „beru to" od adresáta; odesílatel dostane zprávu s tím, co se bude dít.

        U tasku je přijetí zároveň vlastnictví a musí být atomické s leasem
        (review Nox, 20. 10. 2026). Task, který už drží worker (`leased`/`started`),
        smí přijmout jen ten, kdo zná jeho lease token; jinak `False`. Bez toho
        by interaktivní session a bridge stejného jména provedly úkol oba.
        Přijetí s tokenem se značí `by_lease` — když lease vyprší, task se vrátí
        do fronty a worker ho smí vzít znovu (`lease_next(skip_accepted=True)`).
        """
        note = redact(check_short("note", note))
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()  # až po zámku: čekání na zámek nesmí prodloužit lease
            msg = con.execute(
                "SELECT agent, recipient, kind, not_before, expires_at FROM messages WHERE id=?",
                (message_id,)
            ).fetchone()
            if not msg or msg["recipient"] != recipient:
                return False
            by_lease = 0
            if msg["kind"] == MessageKind.CHAT.value and con.execute(
                "SELECT 1 FROM deliveries WHERE message_id=? AND recipient=? AND state='expired'",
                (message_id, recipient),
            ).fetchone():
                return False  # vypršený chat se už nepřebírá (Nox, review #524)
            if msg["kind"] == MessageKind.TASK.value:
                d = con.execute(
                    "SELECT state, lease_token, lease_until FROM deliveries "
                    "WHERE message_id=? AND recipient=?",
                    (message_id, recipient),
                ).fetchone()
                held = d is not None and d["state"] in HELD
                if held and not (lease_token and lease_token == d["lease_token"]):
                    return False  # drží ho worker — nepřebírat
                # prošlý lease už nepatří nikomu (Nox #545/2)
                if held and (d["lease_until"] is None or d["lease_until"] <= now):
                    return False
                paused = con.execute(
                    "SELECT 1 FROM control WHERE key='paused' AND value='1'").fetchone()
                if held and paused:
                    return False
                # karanténa platí pro VŠECHNY cesty převzetí, ne jen bridge (Nox #555/3)
                # držený lease = pokus už započítaný; jinak se počítá příští pokus
                if self._quarantined(con, message_id, recipient, next_attempt=not held):
                    return False
                # Bez lease jde převzít jen úkol, který ještě čeká; hotový, zrušený
                # ani vypršený už ne (audit #535 bod 2a).
                if not held and (d is None or d["state"] != "pending"):
                    return False
                # převzetí bez lease jen s identitou session (Nox #545/1) a jen úkolu,
                # který už smí běžet a nevypršel
                if not held and (not owner or paused or msg["not_before"] > now
                                 or (msg["expires_at"] is not None and msg["expires_at"] <= now)):
                    return False
                by_lease = 1 if held else 0
                # Úkol převzatý jinou session stejného jména nepatří téhle (2a).
                prior = con.execute(
                    "SELECT accepted_at, by_lease, owner FROM receipts "
                    "WHERE message_id=? AND recipient=?", (message_id, recipient),
                ).fetchone()
                # i staré přijetí bez owneru je cizí (fail-closed, Nox #545/1)
                if (not by_lease and prior and prior["accepted_at"] and not prior["by_lease"]
                        and prior["owner"] != owner):
                    return False
            # Transition even on an idempotent receipt from an earlier lease.
            if msg["kind"] == MessageKind.TASK.value and by_lease and d["state"] == "leased":
                con.execute(
                    "UPDATE deliveries SET state='started', updated_at=? "
                    "WHERE message_id=? AND recipient=? AND state='leased'",
                    (now, message_id, recipient),
                )
            row = con.execute(
                "SELECT accepted_at, by_lease FROM receipts WHERE message_id=? AND recipient=?",
                (message_id, recipient),
            ).fetchone()
            if row and row["accepted_at"] and (row["by_lease"] == by_lease or by_lease):
                return True  # idempotentní
            con.execute(
                "INSERT INTO receipts(message_id, recipient, seen_at, accepted_at, note, by_lease, "
                "owner) VALUES(?,?,?,?,?,?,?) ON CONFLICT(message_id, recipient) DO UPDATE SET "
                "accepted_at=excluded.accepted_at, note=excluded.note, by_lease=excluded.by_lease, "
                "owner=COALESCE(excluded.owner, receipts.owner), "
                "seen_at=COALESCE(receipts.seen_at, excluded.seen_at)",
                (message_id, recipient, now, now, note, by_lease, owner),
            )
            self._insert_event(con, recipient, msg["agent"],
                               f"✔ taking #{message_id}" + (f": {note}" if note else ""),
                               message_id, now)
            self._record(con, recipient, "accept", "", f"#{message_id}")
        self._signal(msg["agent"])
        return True

    def awaiting_accept(self, recipient: str, *, limit: int = 10) -> list[dict]:
        """Adresované zprávy, které `recipient` viděl, ale ještě je nepotvrdil."""
        now = time.time()
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.*, r.seen_at FROM messages m "
                "JOIN deliveries d ON d.message_id=m.id AND d.recipient=m.recipient "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=m.recipient "
                "LEFT JOIN screens sc ON sc.message_id=m.id "
                "WHERE m.recipient=? AND m.agent<>? AND m.kind IN ('chat','task','control') "
                f"AND d.state IN {sql_in(QUEUED)} AND (r.accepted_at IS NULL) "
                # jen čerstvé: týdny staré úkoly z doby před potvrzováním nepřipomínat
                "AND m.sent_at >= ? "
                # informativní chat (Jev: akce < 30 %) se nepřipomíná; „viděl" stačí
                "AND NOT (m.kind='chat' AND sc.actionable IS NOT NULL AND sc.actionable < 0.3) "
                "ORDER BY m.id LIMIT ?",
                (recipient, recipient, now - ACCEPT_WINDOW_SECONDS, limit),
            ).fetchall()
        return [
            {"id": r["id"], "agent": r["agent"], "kind": r["kind"], "text": r["text"],
             "seconds_ago": int(now - r["sent_at"]),
             "seen": r["seen_at"] is not None}
            for r in rows
        ]

    def inbox(self, since_id: int = 0, limit: int = 50) -> list[dict]:
        now = time.time()
        limit = _limit(limit)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT {_MSG_COLS}, {_RECEIPT_COLS} {_FROM_MSG} {_JOIN_RECEIPTS} "
                "WHERE m.id > ? ORDER BY m.id LIMIT ?", (since_id, limit)
            ).fetchall()
        return [_row_to_message(row, now) for row in rows]

    def latest_messages(self, limit: int = 200) -> list[dict]:
        """Nejnovější historie v chronologickém pořadí pro první UI snapshot."""
        now = time.time()
        limit = _limit(limit)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT {_MSG_COLS}, {_RECEIPT_COLS} {_FROM_MSG} {_JOIN_RECEIPTS} "
                "WHERE m.id IN "
                "(SELECT id FROM messages ORDER BY id DESC LIMIT ?) ORDER BY m.id",
                (limit,),
            ).fetchall()
        return [_row_to_message(row, now) for row in rows]

    @staticmethod
    def _insert_event(con: sqlite3.Connection, agent: str, recipient: str, text: str,
                      reply_to: int, now: float) -> None:
        """
        Automatická zpráva (příjem, převzetí, přesun, vypršení) jako `result`.

        Nevolá veřejné `send()`: to má vlastní transakci, zakládá doručení a jinou
        kontrolu. Automatické zprávy doručení nemají (Nox, rozbor #516).
        """
        con.execute(
            "INSERT INTO messages(agent, text, sent_at, recipient, kind, reply_to) "
            "VALUES(?,?,?,?,?,?)",
            (agent, text, now, recipient, MessageKind.RESULT.value, reply_to),
        )

    def cursor(self, agent: str) -> int:
        with self._connect() as con:
            row = con.execute("SELECT last_msg FROM cursors WHERE agent = ?", (agent,)).fetchone()
        return int(row["last_msg"]) if row else 0

    def set_cursor(self, agent: str, last_msg: int) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO cursors(agent, last_msg) VALUES(?,?) "
                "ON CONFLICT(agent) DO UPDATE SET last_msg=excluded.last_msg",
                (agent, last_msg),
            )

    def undelivered(self, agent: str, limit: int = 20) -> list[dict]:
        """Vzkazy od ostatních, které tenhle agent ještě neviděl."""
        now = time.time()
        limit = _limit(limit)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT {_MSG_COLS} {_FROM_MSG} "
                "WHERE m.id>? AND m.agent<>? AND m.recipient IN ('*', ?) "
                # Vypršený chat už do živého kontextu nepatří (Nox, review #524).
                "AND NOT (m.kind='chat' AND COALESCE(d.state, '')='expired') "
                "ORDER BY m.id LIMIT ?",
                (self.cursor(agent), agent, agent, limit),
            ).fetchall()
        return [_row_to_message(row, now) for row in rows]

    def jobs(self, recipient: str, *, limit: int = 50, viewer: str | None = None) -> list[dict]:
        """
        Adresovaná doručení pro agenta, včetně historie stavů.

        `viewer` = kdo se ptá (APP-003). Agent vidí jen to, co sám dostal nebo
        sám poslal. Lease token se ve výpisu nevrací nikdy (jen v `lease_next`).
        `None` = autentizovaný člověk (web UI).
        """
        now = time.time()
        limit = max(1, min(int(limit), 500))
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.*, d.state, d.attempts, d.lease_token, d.lease_until, "
                "d.last_error, d.updated_at, r.seen_at, r.accepted_at FROM deliveries d "
                "JOIN messages m ON m.id = d.message_id "
                "LEFT JOIN receipts r ON r.message_id = d.message_id AND r.recipient = d.recipient "
                "WHERE d.recipient = ? AND (? IS NULL OR d.recipient = ? OR m.agent = ?) "
                "ORDER BY m.id DESC LIMIT ?",
                (recipient, viewer, viewer, viewer, limit),
            ).fetchall()
        return [_row_to_job(r, now) for r in rows]

    def outcome(self, message_id: int, *, viewer: str | None = None) -> dict:
        """
        Jak dopadla jedna zpráva/úkol: stav doručení a odpovědi na ni.

        Pro `wait` — odesílatel čeká na výsledek bez ručního dotazování.
        `answers` jsou skutečné odpovědi; automatické příjmy („✓ … saw“ od
        brokeru a „✔ taking #…“ z `accept`) jsou zvlášť v `receipts`, aby
        čekání neskončilo na pouhém „viděl jsem“. Vidí jen odesílatel a adresát.
        """
        now = time.time()
        with self._connect() as con:
            msg = con.execute(
                "SELECT m.*, d.state, d.attempts, d.last_error, r.seen_at, r.accepted_at "
                "FROM messages m LEFT JOIN deliveries d "
                "ON d.message_id=m.id AND d.recipient=m.recipient "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=m.recipient "
                "WHERE m.id=?", (message_id,)
            ).fetchone()
            if not msg:
                raise KeyError(f"message #{message_id} does not exist")
            if viewer is not None and viewer not in (msg["agent"], msg["recipient"]):
                raise PermissionError("only the sender or the recipient may wait on a message")
            # Odpověď = zpráva PRO toho, kdo čeká, od někoho jiného než on sám a ne
            # automatický příjem (Nox, review #516: cizí zpráva s reply_to ani vlastní
            # doplnění zadání nesmí čekání ukončit). Bez viewera (web UI) se čeká
            # za odesílatele. Filtruje se v SQL PŘED limitem, jinak by 50 příjmů
            # schovalo skutečný výsledek.
            waiter = viewer or msg["agent"]
            # Jen protistrana: odesílatel čeká na adresáta a naopak. Cizí agent nesmí
            # čekání ukončit podvrženou „odpovědí“ s reply_to (security review).
            counterpart = msg["recipient"] if waiter == msg["agent"] else msg["agent"]
            # Přesný tvar z `accept`: „✔ taking #N“ nebo „✔ taking #N: poznámka“
            # (prostý prefix by u #5 chytil i „✔ taking #516“).
            taking, taking_note = f"✔ taking #{message_id}", f"✔ taking #{message_id}: %"
            answer_rows = con.execute(
                "SELECT id, agent, kind, text, sent_at FROM messages WHERE reply_to=? "
                # protistrana nebo její bridge worker (`codex` → `codex-bridge`)
                "AND recipient=? AND agent IN (?, ?) AND agent<>? AND agent<>'broker' "
                "AND text<>? AND text NOT LIKE ? ORDER BY id LIMIT 20",
                (message_id, waiter, counterpart, f"{counterpart}-bridge", waiter, taking,
                 taking_note)
            ).fetchall()
            receipt_rows = con.execute(
                "SELECT id, agent, kind, text, sent_at FROM messages WHERE reply_to=? "
                "AND recipient=? AND (agent='broker' OR (agent=? AND (text=? OR text LIKE ?))) "
                "ORDER BY id DESC LIMIT 10",
                (message_id, waiter, msg["recipient"], taking, taking_note)
            ).fetchall()

        def item(r: sqlite3.Row) -> dict:
            return {"id": r["id"], "agent": r["agent"], "kind": r["kind"], "text": r["text"],
                    "seconds_ago": int(now - r["sent_at"])}

        answers = [item(r) for r in answer_rows]
        receipts = [item(r) for r in reversed(receipt_rows)]
        state = _job_state(msg) if msg["state"] is not None else None
        return {
            "id": message_id, "kind": msg["kind"], "recipient": msg["recipient"],
            "state": state, "final": state in TERMINAL,
            "last_error": msg["last_error"] or "", "answers": answers, "receipts": receipts,
        }

    def addressed(self, recipient: str, *, since_id: int = 0, limit: int = 20) -> list[dict]:
        """Nové zprávy výslovně adresované agentovi; broadcast nikoho nebudí."""
        now = time.time()
        limit = _limit(limit)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT {_MSG_COLS} {_FROM_MSG} "
                "WHERE m.id>? AND m.recipient=? ORDER BY m.id LIMIT ?",
                (since_id, recipient, limit),
            ).fetchall()
        return [_row_to_message(row, now) for row in rows]

    def expire_stale_chats(self, older_than_hours: int | None = None) -> list[int]:
        """
        Uzavře staré nevyřízené chaty (`pending` → `expired`).

        Chat se nikdy neprovádí, takže jeho doručení by zůstalo `pending` až do
        retence (30 dní) a agent by ho po dnech mohl „převzít“ bez souvislosti.
        Úkoly (`task`) se nechávají: ve frontě čekají schválně, třeba na konec
        limitu. Odesílatel nepřečteného chatu dostane krátké upozornění.
        """
        hours = self.settings.chat_expire_hours if older_than_hours is None else older_than_hours
        if hours <= 0:
            return []
        now = time.time()
        cutoff = now - hours * 3600
        notify: set[str] = set()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT m.id, m.agent, m.recipient, m.text, r.seen_at, r.accepted_at "
                "FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "LEFT JOIN receipts r ON r.message_id=d.message_id AND r.recipient=d.recipient "
                # Lhůta běží od chvíle, kdy zpráva smí být doručena (not_before), ne od
                # odeslání — naplánovaný chat nesmí vypršet dřív (Nox, review #524).
                "WHERE m.kind='chat' AND d.state='pending' "
                "AND MAX(m.sent_at, COALESCE(m.not_before, 0)) < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                con.execute(
                    "UPDATE deliveries SET state='expired', last_error=?, updated_at=? "
                    "WHERE message_id=? AND recipient=? AND state='pending'",
                    (f"nevyřízeno do {hours} h", now, row["id"], row["recipient"]),
                )
                if not row["seen_at"] and row["agent"] != "broker":
                    snippet = " ".join(row["text"].split())[:60]
                    notice = (f"⌛ {row['recipient']} #{row['id']} nepřečetl do {hours} h, "
                              f"uzavřeno (“{snippet}”)")
                    self._insert_event(con, "broker", row["agent"], notice, row["id"], now)
                    notify.add(row["agent"])
            if rows:
                self._record(con, "broker", "expire", "", f"{len(rows)} chat(s)")
        for agent in notify:
            self._signal(agent)
        return [r["id"] for r in rows]
