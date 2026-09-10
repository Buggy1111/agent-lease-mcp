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
from contextlib import contextmanager
from pathlib import Path

from .config import MAX_TTL_SECONDS, STALE_PEER_SECONDS, Settings
from .models import Claim, ClaimResult
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
    sent_at  REAL NOT NULL
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
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO messages(agent, text, sent_at) VALUES(?,?,?)",
                (agent, text, time.time()),
            )
        return int(cur.lastrowid)

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
                "text": r["text"],
                "seconds_ago": int(now - r["sent_at"]),
            }
            for r in rows
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
        return [m for m in self.inbox(since_id=self.cursor(agent), limit=limit) if m["agent"] != agent]

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
