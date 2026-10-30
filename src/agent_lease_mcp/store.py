"""
Perzistence. Jen SQLite — pravidla jsou v `policy.py`, slovník v `models.py`.

Proč SQLite a ne paměť: každý MCP klient (Claude Code, Codex) si spouští VLASTNÍ
proces serveru přes stdio. Sdílené tedy nemůže být nic v paměti — jediné společné
místo je soubor na disku.

Proč tu není abstraktní úložiště: jedno je, druhé v dohledu není. `Store` je
jedna fasáda se stálým veřejným API (volá ho broker podle `METHODS`); metody
jsou podle oblastí v mixinech: `_claims` (nájmy), `_messaging` (zprávy a příjmy),
`_availability` (přítomnost, limity, failover), `_deliveries` (fronta úkolů).
Tady zůstává schéma, migrace, připojení, audit a retence.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ._availability import AvailabilityMixin
from ._claims import ClaimsMixin
from ._deliveries import DeliveriesMixin
from ._messaging import MessagingMixin
from ._store_common import (
    BRIDGE_ACCEPT_NOTE,
    _limit,
)
from .config import (
    Settings,
)
from .policy import normalize_path
from .states import (
    UNFINISHED_TASK,
    sql_in,
)

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

-- Volitelný explicitní workset a priorita tasku. Cesty se nikdy nehádají z
-- textu; bez deklarace se zachová původní FIFO chování.
CREATE TABLE IF NOT EXISTS task_metadata (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    priority   INTEGER NOT NULL DEFAULT 0 CHECK(priority BETWEEN -100 AND 100)
);

CREATE TABLE IF NOT EXISTS task_paths (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    PRIMARY KEY(message_id, path)
);

CREATE INDEX IF NOT EXISTS idx_task_priority ON task_metadata(priority, message_id);

-- Potvrzení příjmu. `seen_at` píše automaticky hook ve chvíli, kdy zprávu vloží
-- do kontextu agenta (nelze zapomenout); `accepted_at` je vědomé „beru, dělám X"
-- od agenta samotného. Odesílatel tak vždy ví, jestli zpráva dorazila a jestli ji
-- někdo převzal — nemusí se ptát.
CREATE TABLE IF NOT EXISTS receipts (
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    recipient   TEXT NOT NULL,
    seen_at     REAL,
    accepted_at REAL,
    note        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(message_id, recipient)
);

-- Výsledky screeningu vzkazů (Jev): cache, ať se jedna zpráva neposuzuje dvakrát.
CREATE TABLE IF NOT EXISTS screens (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    risk       REAL NOT NULL,
    actionable REAL,
    at         REAL NOT NULL
);

-- Vyčerpané limity agentů (kvóta poskytovatele). Dokud `until` neuplyne, agent
-- se nepočítá jako dostupný a jeho nepřevzaté úkoly se přesouvají na náhradu.
CREATE TABLE IF NOT EXISTS limits (
    agent  TEXT PRIMARY KEY,
    until  REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at     REAL NOT NULL
);

-- Poslední typovaný signál přímo od poskytovatele. `unknown` je samostatný stav:
-- nesmí se zaměnit za obnovenou dostupnost ani za prokázaný limit.
CREATE TABLE IF NOT EXISTS provider_states (
    agent       TEXT PRIMARY KEY,
    state       TEXT NOT NULL CHECK(state IN ('available', 'limited', 'unknown')),
    source      TEXT NOT NULL,
    observed_at REAL NOT NULL,
    resets_at   REAL,
    reason      TEXT NOT NULL DEFAULT ''
);

-- Co už šlo na Telegram/notifikace, ať se nic neposílá dvakrát.
CREATE TABLE IF NOT EXISTS notified (
    key TEXT PRIMARY KEY,
    at  REAL NOT NULL
);

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

-- Bezpečný bod prvního spuštění autonomního workeru. Staré tasky mohly být
-- ručně přijaté nebo už dávno překonané; nový worker je nesmí po instalaci
-- hromadně spustit. Hodnota je trvalá, takže restart naopak neztratí tasky,
-- které dorazily během výpadku workeru.
CREATE TABLE IF NOT EXISTS worker_activations (
    agent             TEXT PRIMARY KEY,
    after_message_id  INTEGER NOT NULL,
    activated_at      REAL NOT NULL
);

-- Stejná pojistka pro první zapnutí failoveru. Bez explicitní aktivace zůstává
-- staré chování kvůli zpětné kompatibilitě; instalační postup nastaví hranici
-- před zapnutím řetězců, aby nerozeslal historický backlog.
CREATE TABLE IF NOT EXISTS failover_activations (
    agent             TEXT PRIMARY KEY,
    after_message_id  INTEGER NOT NULL,
    activated_at      REAL NOT NULL
);
"""


class Store(ClaimsMixin, MessagingMixin, AvailabilityMixin, DeliveriesMixin):
    def __init__(self, db_path: Path | None = None, *, settings: Settings | None = None) -> None:
        """
        `db_path` je poziční schválně — to je to, co chce volající z testu i
        z nástroje. `settings` je keyword-only, aby nešlo omylem předat cestu
        tam, kde se čeká nastavení (což se mi při refaktoru stalo).
        """
        self.settings = settings or Settings.from_env()
        self.db_path = db_path or self.settings.db_path
        # APP-001: DB, WAL i SHM dědí režim hlavního souboru, takže ho vytvoříme
        # jako 0600 dřív, než ho SQLite otevře s výchozím umaskem. Adresář
        # zakládáme 0700; existujícímu cizímu adresáři práva neměníme.
        parent = self.db_path.parent
        created = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created or parent == Path.home() / ".agent-lease":
            os.chmod(parent, 0o700)  # mkdir mode neplatí pro existující adresář
        if not self.db_path.exists():
            os.close(os.open(self.db_path, os.O_WRONLY | os.O_CREAT, 0o600))
        for suffix in ("", "-wal", "-shm"):  # dorovnat i starší instalace
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists() and candidate.stat().st_mode & 0o077:
                os.chmod(candidate, 0o600)
        with self._connect() as con:
            con.executescript(SCHEMA)
            # Migrace = PRAGMA + ALTER. Souběžné Store() by jinak oba viděli chybějící
            # sloupec a druhý ALTER by spadl (duplicate column). Zámek až po
            # executescript, ten otevřenou transakci sám commitne.
            con.execute("BEGIN IMMEDIATE")
            self._migrate_messages(con)
            self._migrate_receipts(con)
            self._migrate_screens(con)
        if self.settings.retention_days > 0:
            self.prune()  # retence běží sama (APP-012), ne až když si někdo vzpomene

    @staticmethod
    def _migrate_receipts(con: sqlite3.Connection) -> None:
        """`by_lease` = převzal to worker s lease tokenem (bridge), ne interaktivní session."""
        columns = {row["name"] for row in con.execute("PRAGMA table_info(receipts)")}
        if "by_lease" not in columns:
            con.execute("ALTER TABLE receipts ADD COLUMN by_lease INTEGER NOT NULL DEFAULT 0")
            # Starší bridge přijímal úkoly bez by_lease. Bez backfillu by takový úkol po
            # retry vypadal jako převzatý interaktivně a bridge by ho navždy přeskočil.
            con.execute(
                "UPDATE receipts SET by_lease=1 WHERE accepted_at IS NOT NULL AND note=? "
                "AND EXISTS (SELECT 1 FROM deliveries d WHERE d.message_id=receipts.message_id "
                "AND d.recipient=receipts.recipient AND d.attempts>0)",
                (BRIDGE_ACCEPT_NOTE,),
            )

        # `owner` = která interaktivní session (proces MCP serveru) úkol převzala.
        # Dvě session stejného jména si tak jeden úkol nevezmou obě (audit #535 bod 2a).
        if "owner" not in columns:
            con.execute("ALTER TABLE receipts ADD COLUMN owner TEXT")

    @staticmethod
    def _migrate_screens(con: sqlite3.Connection) -> None:
        """Lidské schválení karantény: kdo, kdy, proč a na jaký přesný text."""
        columns = {row["name"] for row in con.execute("PRAGMA table_info(screens)")}
        for name, decl in (("approved_by", "TEXT"), ("approved_at", "REAL"),
                           ("approved_hash", "TEXT"), ("approved_reason", "TEXT")):
            if name not in columns:
                con.execute(f"ALTER TABLE screens ADD COLUMN {name} {decl}")

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











    # ── přítomnost ───────────────────────────────────────────────────────────



    # ── vzkazy ───────────────────────────────────────────────────────────────



    # ── probuzení a potvrzení příjmu ─────────────────────────────────────────






    # ── limity a failover ────────────────────────────────────────────────────











    def mark_notified(self, key: str) -> bool:
        """True, pokud je to poprvé (a tedy se má poslat)."""
        with self._connect() as con:
            return bool(con.execute("INSERT OR IGNORE INTO notified(key, at) VALUES(?,?)",
                                    (key, time.time())).rowcount)

    def unmark_notified(self, key: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM notified WHERE key=?", (key,))


    def recent_audit(self, actions: tuple[str, ...], since_id: int = 0, limit: int = 50) -> list[dict]:
        marks = ",".join("?" * len(actions))
        with self._connect() as con:
            rows = con.execute(
                f"SELECT id, at, agent, action, path, detail FROM audit WHERE id>? "
                f"AND action IN ({marks}) ORDER BY id LIMIT ?", (since_id, *actions, limit),
            ).fetchall()
        return [dict(r) for r in rows]










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

















    def prune(self, older_than_days: int | None = None) -> dict[str, int]:
        """
        Retence (APP-012): smaže zprávy a audit starší než N dní.

        Chráněný je jen *úkol* (`task`) s nedokončeným doručením
        (pending/leased/started/needs_review). Chat/control/result s doručením
        `pending` se nikdy „nedokončí" (nejde je převzít ani potvrdit), takže by
        jinak zůstaly navždy (Nox, review 16. 10.). Odpověď na smazanou zprávu
        zůstává, jen ztratí odkaz — foreign_keys v SQLite nejsou zapnuté, takže by
        visící reply_to nikdo nechytil.
        """
        days = self.settings.retention_days if older_than_days is None else older_than_days
        if days < 0:
            raise ValueError("days must not be negative")
        cutoff = time.time() - days * 86400
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            doomed = [
                r["id"] for r in con.execute(
                    "SELECT id FROM messages m WHERE sent_at < ? AND NOT (m.kind = 'task' "
                    "AND EXISTS (SELECT 1 FROM deliveries d WHERE d.message_id = m.id AND "
                    f"d.state IN {sql_in(UNFINISHED_TASK)}))",
                    (cutoff,),
                )
            ]
            for i in range(0, len(doomed), 500):
                chunk = doomed[i:i + 500]
                marks = ",".join("?" * len(chunk))
                # foreign_keys v SQLite nejsou zapnuté — závislé řádky mazat ručně,
                # jinak by potvrzení a screening zůstaly navždy (Nox, review 17. 10.).
                for table in ("deliveries", "receipts", "screens"):
                    con.execute(f"DELETE FROM {table} WHERE message_id IN ({marks})", chunk)
                con.execute(f"DELETE FROM messages WHERE id IN ({marks})", chunk)
            if doomed:
                con.execute(
                    "UPDATE messages SET reply_to = NULL WHERE reply_to IS NOT NULL "
                    "AND reply_to NOT IN (SELECT id FROM messages)"
                )
            audit = con.execute("DELETE FROM audit WHERE at < ?", (cutoff,)).rowcount
        return {"messages": len(doomed), "audit": audit}

    def history(self, limit: int = 50, path: str | None = None) -> list[dict]:
        """Kdo na co sáhl a jak to dopadlo — odpověď na 'proč mě to zablokovalo'."""
        now = time.time()
        sql = "SELECT * FROM audit"
        args: tuple = ()
        if path:
            sql += " WHERE path = ?"
            args = (normalize_path(path),)
        sql += " ORDER BY id DESC LIMIT ?"
        args += (_limit(limit),)
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
