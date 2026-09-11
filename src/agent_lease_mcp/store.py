"""
Perzistence. Jen SQLite — pravidla jsou v `policy.py`, slovník v `models.py`.

Proč SQLite a ne paměť: každý MCP klient (Claude Code, Codex) si spouští VLASTNÍ
proces serveru přes stdio. Sdílené tedy nemůže být nic v paměti — jediné společné
místo je soubor na disku.

Proč tu není abstraktní úložiště: jedno je, druhé v dohledu není, a `Store` je
malý natolik, že by rozhraní jen přidalo vrstvu k proklikávání. Až přijde
potřeba (víc strojů), je to jedna třída k přepsání — ne architektura k rozbití.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import MAX_TTL_SECONDS, STALE_PEER_SECONDS, Settings
from .models import Claim, ClaimResult, Delivery, DeliveryState, MessageKind
from .policy import covers, normalize_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    path        TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    purpose     TEXT NOT NULL DEFAULT '',
    claimed_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    agent      TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT '',
    cwd        TEXT NOT NULL DEFAULT '',
    seen_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    agent    TEXT NOT NULL,
    text     TEXT NOT NULL,
    sent_at  REAL NOT NULL,
    recipient TEXT NOT NULL DEFAULT '*',
    kind      TEXT NOT NULL DEFAULT 'broadcast',
    project   TEXT NOT NULL DEFAULT '',
    not_before REAL NOT NULL DEFAULT 0,
    expires_at REAL,
    dedupe_key TEXT,
    reply_to  INTEGER REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    recipient   TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until REAL,
    last_error  TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL,
    PRIMARY KEY(message_id, recipient)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_queue
ON deliveries(recipient, state, message_id);

-- Audit: kdo, kdy, na co sáhl a jak to dopadlo.
-- Celý projekt vznikl proto, že po kolizi nešlo zpětně zjistit, kdo co kdy
-- editoval. Koordinace bez záznamu rozhodnutí by tu otázku nezodpověděla ani teď.
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    agent    TEXT NOT NULL,
    action   TEXT NOT NULL,
    path     TEXT NOT NULL DEFAULT '',
    detail   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);

-- Kam až má který agent doručené vzkazy. Bez toho by se při každém promptu
-- injektovaly pořád stejné zprávy dokola.
CREATE TABLE IF NOT EXISTS cursors (
    agent      TEXT PRIMARY KEY,
    last_msg   INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, db_path: Path | None = None, *, settings: Settings | None = None) -> None:
        """
        `db_path` je poziční schválně — to je to, co chce volající z testu i
        z nástroje. `settings` je keyword-only, aby nešlo omylem předat cestu
        tam, kde se čeká nastavení (což se mi při refaktoru stalo).
        """
        self.settings = settings or Settings.from_env()
        self.db_path = db_path or self.settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            self._migrate_messages(con)

    @staticmethod
    def _migrate_messages(con: sqlite3.Connection) -> None:
        """Rozšíří databáze z původní broadcast-only verze bez ztráty historie."""
        columns = {row["name"] for row in con.execute("PRAGMA table_info(messages)")}
        additions = {
            "recipient": "TEXT NOT NULL DEFAULT '*'",
            "kind": "TEXT NOT NULL DEFAULT 'broadcast'",
            "project": "TEXT NOT NULL DEFAULT ''",
            "not_before": "REAL NOT NULL DEFAULT 0",
            "expires_at": "REAL",
            "dedupe_key": "TEXT",
            "reply_to": "INTEGER REFERENCES messages(id)",
        }
        for name, declaration in additions.items():
            if name not in columns:
                con.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe "
            "ON messages(agent, dedupe_key) WHERE dedupe_key IS NOT NULL"
        )
        con.execute("PRAGMA user_version=1")

    @contextmanager
    def _connect(self):
        # check_same_thread=False: fastmcp obsluhuje nástroje z různých vláken,
        # spojení otevíráme na každou operaci znovu.
        con = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=5000")
            yield con
            con.commit()
        finally:
            con.close()

    # ── nájmy ────────────────────────────────────────────────────────────────

    def claim(
        self,
        paths: list[str],
        agent: str,
        purpose: str = "",
        ttl_seconds: int | None = None,
    ) -> ClaimResult:
        """
        Vezme nájem na VŠECHNY cesty, nebo na žádnou.

        Všechno-nebo-nic schválně: agent, který dostane půlku cest, stejně nemůže
        pracovat, ale ty půlky mezitím drží a blokuje druhého. Částečný úspěch je
        horší než čistý neúspěch.
        """
        ttl = max(1, min(int(ttl_seconds or self.settings.default_ttl), MAX_TTL_SECONDS))
        now = time.time()
        wanted = [normalize_path(p) for p in paths]

        with self._connect() as con:
            # IMMEDIATE = zámek na zápis hned na začátku transakce. Bez toho by
            # dva agenti oba přečetli „volno" a oba zapsali nájem.
            con.execute("BEGIN IMMEDIATE")
            self._prune(con, now)

            conflicts = self._conflicts(con, wanted, agent)
            if conflicts:
                for c in conflicts:
                    self._record(con, agent, "claim-refused", c.path, f"drží {c.agent}")
                return ClaimResult(granted=[], conflicts=conflicts)

            for path in wanted:
                con.execute(
                    "INSERT INTO claims(path, agent, purpose, claimed_at, expires_at) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "  agent=excluded.agent, purpose=excluded.purpose, "
                    "  expires_at=excluded.expires_at",
                    (path, agent, purpose, now, now + ttl),
                )
                self._record(con, agent, "claim", path, purpose)
            return ClaimResult(granted=wanted, conflicts=[])

    def _conflicts(self, con: sqlite3.Connection, wanted: list[str], agent: str) -> list[Claim]:
        """
        Kolize v OBOU směrech: cizí nájem na adresář nade mnou i cizí nájem na
        soubor uvnitř adresáře, který si beru.
        """
        held = [
            _row_to_claim(r)
            for r in con.execute("SELECT * FROM claims WHERE agent <> ?", (agent,)).fetchall()
        ]
        found: list[Claim] = []
        for target in wanted:
            for claim_ in held:
                if covers(claim_.path, target) or covers(target, claim_.path):
                    found.append(claim_)
        return found

    def release(self, paths: list[str], agent: str) -> list[str]:
        """Uvolní jen to, co agent opravdu drží — cizí nájem nesmí sundat."""
        released: list[str] = []
        with self._connect() as con:
            for path in (normalize_path(p) for p in paths):
                if con.execute(
                    "DELETE FROM claims WHERE path = ? AND agent = ?", (path, agent)
                ).rowcount:
                    released.append(path)
                    self._record(con, agent, "release", path)
        return released

    def release_all(self, agent: str) -> list[str]:
        with self._connect() as con:
            rows = con.execute("SELECT path FROM claims WHERE agent = ?", (agent,)).fetchall()
            con.execute("DELETE FROM claims WHERE agent = ?", (agent,))
            for r in rows:
                self._record(con, agent, "release", r["path"])
        return [r["path"] for r in rows]

    def holder_of(self, path: str) -> Claim | None:
        """Nájem, který na tuhle cestu dosáhne — přímo, nebo přes adresář nad ní."""
        target = normalize_path(path)
        now = time.time()
        with self._connect() as con:
            self._prune(con, now)
            for row in con.execute("SELECT * FROM claims").fetchall():
                claim_ = _row_to_claim(row)
                if covers(claim_.path, target):
                    return claim_
        return None

    def claims(self) -> list[Claim]:
        now = time.time()
        with self._connect() as con:
            self._prune(con, now)
            rows = con.execute("SELECT * FROM claims ORDER BY claimed_at").fetchall()
        return [_row_to_claim(r) for r in rows]

    @staticmethod
    def _prune(con: sqlite3.Connection, now: float) -> None:
        con.execute("DELETE FROM claims WHERE expires_at <= ?", (now,))

    # ── přítomnost ───────────────────────────────────────────────────────────

    def heartbeat(self, agent: str, status: str | None = None, cwd: str | None = None) -> None:
        """
        Ohlásit, že agent žije. `status`/`cwd` = None znamená „nech, co tam je".

        ⚠️ Prázdný řetězec status smaže, None ne — a ten rozdíl je podstatný.
        Dokud ho tu nebylo, každé `room()` bez argumentu přepsalo popis práce
        prázdnem a `say` slovem „say", takže v místnosti svítilo jméno
        posledního volání místo toho, na čem druhý agent dělá.
        """
        with self._connect() as con:
            con.execute(
                "INSERT INTO agents(agent, status, cwd, seen_at) VALUES(?,?,?,?) "
                "ON CONFLICT(agent) DO UPDATE SET "
                "  status=COALESCE(?, agents.status), "
                "  cwd=COALESCE(?, agents.cwd), "
                "  seen_at=excluded.seen_at",
                (agent, status or "", cwd or "", time.time(), status, cwd),
            )

    def peers(self, stale_after: int = STALE_PEER_SECONDS) -> list[dict]:
        now = time.time()
        with self._connect() as con:
            rows = con.execute("SELECT * FROM agents ORDER BY seen_at DESC").fetchall()
        return [
            {
                "agent": r["agent"],
                "status": r["status"],
                "cwd": r["cwd"],
                "seen_seconds_ago": int(now - r["seen_at"]),
                "active": r["seen_at"] >= now - stale_after,
            }
            for r in rows
        ]

    # ── vzkazy ───────────────────────────────────────────────────────────────

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
    ) -> int:
        """Atomicky uloží adresovanou zprávu a její doručení."""
        kind_value = MessageKind(kind).value
        recipient = recipient.strip()
        if not recipient:
            raise ValueError("recipient nesmí být prázdný")
        if not text.strip():
            raise ValueError("text nesmí být prázdný")
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
            if recipient != "*":
                con.execute(
                    "INSERT INTO deliveries(message_id, recipient, updated_at) VALUES(?,?,?)",
                    (message_id, recipient, now),
                )
        return message_id

    def inbox(self, since_id: int = 0, limit: int = 50) -> list[dict]:
        now = time.time()
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?", (since_id, limit)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "agent": r["agent"],
                "recipient": r["recipient"],
                "kind": r["kind"],
                "text": r["text"],
                "reply_to": r["reply_to"],
                "seconds_ago": int(now - r["sent_at"]),
            }
            for r in rows
        ]

    def latest_messages(self, limit: int = 200) -> list[dict]:
        """Nejnovější historie v chronologickém pořadí pro první UI snapshot."""
        now = time.time()
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) "
                "ORDER BY id",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"], "agent": row["agent"],
                "recipient": row["recipient"], "kind": row["kind"],
                "text": row["text"], "reply_to": row["reply_to"],
                "seconds_ago": int(now - row["sent_at"]),
            }
            for row in rows
        ]

    # ── audit ────────────────────────────────────────────────────────────────

    def record(self, agent: str, action: str, path: str = "", detail: str = "") -> None:
        with self._connect() as con:
            self._record(con, agent, action, path, detail)

    @staticmethod
    def _record(
        con: sqlite3.Connection, agent: str, action: str, path: str = "", detail: str = ""
    ) -> None:
        con.execute(
            "INSERT INTO audit(at, agent, action, path, detail) VALUES(?,?,?,?,?)",
            (time.time(), agent, action, path, detail),
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
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM messages WHERE id>? AND agent<>? "
                "AND recipient IN ('*', ?) ORDER BY id LIMIT ?",
                (self.cursor(agent), agent, agent, limit),
            ).fetchall()
        return [
            {
                "id": row["id"], "agent": row["agent"],
                "recipient": row["recipient"], "kind": row["kind"],
                "text": row["text"], "reply_to": row["reply_to"],
                "seconds_ago": int(now - row["sent_at"]),
            }
            for row in rows
        ]

    def jobs(self, recipient: str, *, limit: int = 50) -> list[dict]:
        """Adresovaná doručení pro agenta, včetně historie stavů."""
        now = time.time()
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.*, d.state, d.attempts, d.lease_token, d.lease_until, "
                "d.last_error, d.updated_at FROM deliveries d "
                "JOIN messages m ON m.id = d.message_id "
                "WHERE d.recipient = ? ORDER BY m.id DESC LIMIT ?",
                (recipient, limit),
            ).fetchall()
        return [_row_to_job(r, now) for r in rows]

    def addressed(self, recipient: str, *, since_id: int = 0, limit: int = 20) -> list[dict]:
        """Nové zprávy výslovně adresované agentovi; broadcast nikoho nebudí."""
        now = time.time()
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM messages WHERE id>? AND recipient=? "
                "ORDER BY id LIMIT ?",
                (since_id, recipient, limit),
            ).fetchall()
        return [
            {
                "id": row["id"], "agent": row["agent"],
                "recipient": row["recipient"], "kind": row["kind"],
                "text": row["text"], "reply_to": row["reply_to"],
                "seconds_ago": int(now - row["sent_at"]),
            }
            for row in rows
        ]

    def lease_next(self, recipient: str, *, lease_seconds: int = 60) -> Delivery | None:
        """Atomicky převezme nejstarší připravený task pro jeden worker."""
        now = time.time()
        token = uuid.uuid4().hex
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE deliveries SET state='expired', lease_token=NULL, lease_until=NULL, "
                "updated_at=? WHERE recipient=? AND state='pending' AND message_id IN "
                "(SELECT id FROM messages WHERE expires_at IS NOT NULL AND expires_at<=?)",
                (now, recipient, now),
            )
            con.execute(
                "UPDATE deliveries SET state='needs_review', lease_token=NULL, lease_until=NULL, "
                "last_error='worker lease expired after start', updated_at=? "
                "WHERE recipient=? AND state='started' AND lease_until<=?",
                (now, recipient, now),
            )
            con.execute(
                "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
                "updated_at=? WHERE recipient=? AND state='leased' AND lease_until<=?",
                (now, recipient, now),
            )
            row = con.execute(
                "SELECT m.*, d.state, d.attempts FROM deliveries d "
                "JOIN messages m ON m.id=d.message_id "
                "WHERE d.recipient=? AND d.state='pending' AND m.kind='task' "
                "AND m.not_before<=? AND (m.expires_at IS NULL OR m.expires_at>?) "
                "ORDER BY m.id LIMIT 1",
                (recipient, now, now),
            ).fetchone()
            if not row:
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
        """Posune stav; vlastněný lease lze potvrdit jen jeho tokenem."""
        state_value = DeliveryState(state).value
        terminal = {
            DeliveryState.SUCCEEDED.value, DeliveryState.FAILED.value,
            DeliveryState.NEEDS_REVIEW.value, DeliveryState.CANCELLED.value,
            DeliveryState.EXPIRED.value, DeliveryState.DEAD_LETTER.value,
        }
        with self._connect() as con:
            row = con.execute(
                "SELECT lease_token FROM deliveries WHERE message_id=? AND recipient=?",
                (message_id, recipient),
            ).fetchone()
            if not row or (row["lease_token"] and row["lease_token"] != lease_token):
                return False
            clear_lease = state_value in terminal or state_value == DeliveryState.PENDING.value
            cur = con.execute(
                "UPDATE deliveries SET state=?, last_error=?, updated_at=?, "
                "lease_token=CASE WHEN ? THEN NULL ELSE lease_token END, "
                "lease_until=CASE WHEN ? THEN NULL ELSE lease_until END "
                "WHERE message_id=? AND recipient=?",
                (state_value, error, time.time(), clear_lease, clear_lease,
                 message_id, recipient),
            )
        return bool(cur.rowcount)

    def retry(self, message_id: int, recipient: str, *, not_before: float = 0) -> bool:
        """Vrátí neúspěšné doručení do fronty a zachová počet pokusů."""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
                "last_error='', updated_at=? WHERE message_id=? AND recipient=? "
                "AND state IN ('failed','needs_review','dead_letter')",
                (time.time(), message_id, recipient),
            )
            if cur.rowcount:
                con.execute(
                    "UPDATE messages SET not_before=? WHERE id=?",
                    (not_before, message_id),
                )
        return bool(cur.rowcount)

    def cancel(self, message_id: int, recipient: str) -> bool:
        """Zruší nedokončené doručení; výsledek zůstane v auditu fronty."""
        with self._connect() as con:
            cur = con.execute(
                "UPDATE deliveries SET state='cancelled', lease_token=NULL, lease_until=NULL, "
                "updated_at=? WHERE message_id=? AND recipient=? "
                "AND state NOT IN ('succeeded','cancelled','expired')",
                (time.time(), message_id, recipient),
            )
        return bool(cur.rowcount)

    def history(self, limit: int = 50, path: str | None = None) -> list[dict]:
        """Kdo na co sáhl a jak to dopadlo — odpověď na 'proč mě to zablokovalo'."""
        now = time.time()
        sql = "SELECT * FROM audit"
        args: tuple = ()
        if path:
            sql += " WHERE path = ?"
            args = (normalize_path(path),)
        sql += " ORDER BY id DESC LIMIT ?"
        args += (limit,)
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        return [
            {
                "agent": r["agent"],
                "action": r["action"],
                "path": r["path"],
                "detail": r["detail"],
                "seconds_ago": int(now - r["at"]),
            }
            for r in rows
        ]


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        path=row["path"],
        agent=row["agent"],
        purpose=row["purpose"],
        claimed_at=row["claimed_at"],
        expires_at=row["expires_at"],
    )


def _row_to_job(row: sqlite3.Row, now: float) -> dict:
    return {
        "id": row["id"], "agent": row["agent"], "recipient": row["recipient"],
        "kind": row["kind"], "text": row["text"], "state": row["state"],
        "attempts": row["attempts"], "lease_token": row["lease_token"],
        "lease_until": row["lease_until"], "last_error": row["last_error"],
        "reply_to": row["reply_to"], "seconds_ago": int(now - row["sent_at"]),
    }
