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

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import (
    MAX_PATHS_PER_CLAIM,
    MAX_TASK_LEASE_SECONDS,
    MAX_TTL_SECONDS,
    STALE_PEER_SECONDS,
    Settings,
    human_name,
    valid_agent_name,
)
from .hygiene import (
    check_name,
    check_path,
    check_short,
    check_text,
    heuristic_risk,
    redact,
    untrusted,
)
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


# Jak dlouho se nepotvrzená zpráva připomíná (pak už jen v `overdue`).
ACCEPT_WINDOW_SECONDS = 3 * 86400

# Poznámka, se kterou bridge přijímá úkol (migrace podle ní pozná staré bridge receipty).
BRIDGE_ACCEPT_NOTE = "bridge took the task, starting a headless run (read-only)"


class Store:
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
        _check_agent(agent)
        if not paths or len(paths) > MAX_PATHS_PER_CLAIM:
            raise ValueError(f"claim chce 1–{MAX_PATHS_PER_CLAIM} cest")
        check_short("purpose", purpose)
        wanted = [normalize_path(check_path(p)) for p in paths]

        with self._connect() as con:
            # IMMEDIATE = zámek na zápis hned na začátku transakce. Bez toho by
            # dva agenti oba přečetli „volno" a oba zapsali nájem.
            con.execute("BEGIN IMMEDIATE")
            self._prune(con, now)

            conflicts = self._conflicts(con, wanted, agent)
            if conflicts:
                for c in conflicts:
                    self._record(con, agent, "claim-refused", c.path, f"held by {c.agent}")
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

    def edit_gate(self, path: str, agent: str, purpose: str = "auto (edit)") -> Claim | None:
        """
        Jedno atomické rozhodnutí pro hook (APP-007): smí `agent` editovat `path`?

        Vrací cizí nájem, který brání, nebo None. Pokud je cesta volná, nájem se
        vezme TADY, ve stejné transakci jako kontrola — mezi „zjistil jsem, že je
        volno" a „zapsal jsem nájem" se tak nemá kam vecpat druhý agent. Vlastní
        nájem se při každém zápisu obnoví, ať nevyprší mezi kontrolou a editací.
        """
        _check_agent(agent)
        target = normalize_path(check_path(path))
        now = time.time()
        # Stejný strop jako `claim()` — jinak by si agent s obří AGENT_LEASE_TTL
        # vzal nájem na roky a po pádu by blokoval ostatní (Nox, review 17. 10.).
        ttl = max(1, min(int(self.settings.default_ttl), MAX_TTL_SECONDS))
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._prune(con, now)
            foreign = self._conflicts(con, [target], agent)
            if foreign:
                self._record(con, agent, "blocked", target, foreign[0].agent)
                return foreign[0]
            mine = [
                _row_to_claim(r)
                for r in con.execute("SELECT * FROM claims WHERE agent = ?", (agent,))
            ]
            covering = [c for c in mine if covers(c.path, target)]
            if covering:
                marks = ",".join("?" * len(covering))
                con.execute(
                    f"UPDATE claims SET expires_at = MAX(expires_at, ?) "
                    f"WHERE agent = ? AND path IN ({marks})",
                    (now + ttl, agent, *[c.path for c in covering]),
                )
            else:
                con.execute(
                    "INSERT INTO claims(path, agent, purpose, claimed_at, expires_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                    "agent=excluded.agent, purpose=excluded.purpose, expires_at=excluded.expires_at",
                    (target, agent, purpose[:500], now, now + ttl),
                )
                self._record(con, agent, "claim", target, purpose[:500])
        return None

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
        _check_agent(agent)
        if status:
            check_short("status", status)
        if cwd:
            check_path(cwd)
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
            limited = {r["agent"]: r["until"] for r in
                       con.execute("SELECT agent, until FROM limits WHERE until > ?", (now,))}
            providers = {r["agent"]: dict(r) for r in
                         con.execute("SELECT * FROM provider_states")}
        return [
            {
                "agent": r["agent"],
                "status": r["status"],
                "cwd": r["cwd"],
                "seen_seconds_ago": int(now - r["seen_at"]),
                "active": r["seen_at"] >= now - stale_after,
                "presence": (
                    "rate-limited" if r["agent"] in limited else
                    # Reset od poskytovatele už prošel: worker to zkusí s dalším úkolem,
                    # do té doby nehlásit „rate-limited" navždy.
                    "recovering" if _limit_reset_passed(providers.get(r["agent"]), now) else
                    "rate-limited" if providers.get(r["agent"], {}).get("state") == "limited" else
                    "provider-unknown" if providers.get(r["agent"], {}).get("state") == "unknown"
                    else _presence_state(r["status"], now - r["seen_at"], stale_after)
                ),
                "limited_until": limited.get(r["agent"]),
                "provider_state": providers.get(r["agent"], {}).get("state"),
                "provider_source": providers.get(r["agent"], {}).get("source"),
                "provider_reason": providers.get(r["agent"], {}).get("reason", ""),
                "provider_resets_at": providers.get(r["agent"], {}).get("resets_at"),
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

    # ── probuzení a potvrzení příjmu ─────────────────────────────────────────

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
                        con.execute(
                            "INSERT INTO messages(agent, text, sent_at, recipient, kind, reply_to) "
                            "VALUES('broker',?,?,?,?,?)",
                            (f"✓ {recipient} saw #{mid} (“{snippet}”)", now,
                             msg["agent"], MessageKind.RESULT.value, mid),
                        )
        for sender in senders:
            self._signal(sender)
        return fresh

    def accept(self, message_id: int, recipient: str, note: str = "",
               lease_token: str | None = None) -> bool:
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
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            msg = con.execute(
                "SELECT agent, recipient, kind FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if not msg or msg["recipient"] != recipient:
                return False
            by_lease = 0
            if msg["kind"] == MessageKind.TASK.value:
                d = con.execute(
                    "SELECT state, lease_token FROM deliveries WHERE message_id=? AND recipient=?",
                    (message_id, recipient),
                ).fetchone()
                held = d is not None and d["state"] in ("leased", "started")
                if held and not (lease_token and lease_token == d["lease_token"]):
                    return False  # drží ho worker — nepřebírat
                by_lease = 1 if held else 0
            row = con.execute(
                "SELECT accepted_at, by_lease FROM receipts WHERE message_id=? AND recipient=?",
                (message_id, recipient),
            ).fetchone()
            if row and row["accepted_at"] and (row["by_lease"] == by_lease or by_lease):
                return True  # idempotentní
            con.execute(
                "INSERT INTO receipts(message_id, recipient, seen_at, accepted_at, note, by_lease) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(message_id, recipient) DO UPDATE SET "
                "accepted_at=excluded.accepted_at, note=excluded.note, by_lease=excluded.by_lease, "
                "seen_at=COALESCE(receipts.seen_at, excluded.seen_at)",
                (message_id, recipient, now, now, note, by_lease),
            )
            con.execute(
                "INSERT INTO messages(agent, text, sent_at, recipient, kind, reply_to) "
                "VALUES(?,?,?,?,?,?)",
                (recipient, f"✔ taking #{message_id}" + (f": {note}" if note else ""), now,
                 msg["agent"], MessageKind.RESULT.value, message_id),
            )
            self._record(con, recipient, "accept", "", f"#{message_id}")
        self._signal(msg["agent"])
        return True

    # ── limity a failover ────────────────────────────────────────────────────

    def report_limit(self, agent: str, until: float, reason: str = "") -> None:
        """Agent (nebo bridge) hlásí vyčerpaný limit do času `until` (epoch)."""
        _check_agent(agent)
        reason = redact(check_short("reason", reason))
        until = min(until, time.time() + 7 * 86400)  # strop: týden
        with self._connect() as con:
            con.execute("INSERT OR REPLACE INTO limits(agent, until, reason, at) VALUES(?,?,?,?)",
                        (agent, until, reason, time.time()))
            self._record(con, agent, "limit", "", f"do {int(until)}: {reason}")
        self.failover_sweep(grace=0)  # bez čekání: kdo hlásí limit, nic nepřevezme

    def clear_limit(self, agent: str) -> bool:
        with self._connect() as con:
            return bool(con.execute("DELETE FROM limits WHERE agent=?", (agent,)).rowcount)

    def limits(self) -> list[dict]:
        now = time.time()
        with self._connect() as con:
            con.execute("DELETE FROM limits WHERE until <= ?", (now,))
            rows = con.execute("SELECT * FROM limits ORDER BY until").fetchall()
        return [{"agent": r["agent"], "until": r["until"], "reason": r["reason"],
                 "seconds_left": int(r["until"] - now)} for r in rows]

    def report_provider_state(self, agent: str, state: str, source: str, *,
                              observed_at: float | None = None,
                              resets_at: float | None = None, reason: str = "") -> None:
        """Persist an explicit provider signal; never manufacture a reset deadline."""
        _check_agent(agent)
        if state not in {"available", "limited", "unknown"}:
            raise ValueError("state must be available, limited or unknown")
        source = redact(check_short("source", source))
        reason = redact(check_short("reason", reason))
        at = time.time() if observed_at is None else observed_at
        with self._connect() as con:
            con.execute(
                "INSERT INTO provider_states(agent,state,source,observed_at,resets_at,reason) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET "
                "state=excluded.state, source=excluded.source, observed_at=excluded.observed_at, "
                "resets_at=excluded.resets_at, reason=excluded.reason",
                (agent, state, source, at, resets_at, reason),
            )
            self._record(con, agent, "provider-state", "", f"{state} via {source}: {reason}")
        if state == "limited":
            self.failover_sweep(grace=0)

    def provider_states(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM provider_states ORDER BY agent").fetchall()
        return [dict(row) for row in rows]

    def provider_state(self, agent: str) -> dict | None:
        _check_agent(agent)
        with self._connect() as con:
            row = con.execute("SELECT * FROM provider_states WHERE agent=?", (agent,)).fetchone()
        return dict(row) if row else None

    def clear_provider_state(self, agent: str) -> bool:
        """Forget a provider observation after a human/agent confirms recovery."""
        _check_agent(agent)
        with self._connect() as con:
            removed = bool(con.execute(
                "DELETE FROM provider_states WHERE agent=?", (agent,)
            ).rowcount)
            if removed:
                self._record(con, agent, "provider-clear", "", "explicit recovery confirmation")
        if removed:
            self._signal(agent)
        return removed

    def is_available(self, agent: str) -> bool:
        """Logical agent has either a live interactive or live worker execution path."""
        available = {p["agent"] for p in self.peers() if p["presence"] in ("idle", "busy")}
        return agent in available or f"{agent}-bridge" in available

    def failover_activation(self, agent: str, *, include_existing: bool = False) -> int:
        """Create once and return the durable lower task-id bound for failover."""
        _check_agent(agent)
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT after_message_id FROM failover_activations WHERE agent=?", (agent,)
            ).fetchone()
            if row:
                return int(row["after_message_id"])
            after = 0
            if not include_existing:
                after = int(con.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages"
                ).fetchone()[0])
            con.execute(
                "INSERT INTO failover_activations(agent, after_message_id, activated_at) "
                "VALUES(?,?,?)", (agent, after, now),
            )
            self._record(con, agent, "failover-activate", "", f"after #{after}")
            return after

    def failover_sweep(self, *, grace: int | None = None) -> list[dict]:
        """
        Přesune NEZAHÁJENÉ úkoly od nedostupného agenta na první dostupnou náhradu
        z jeho řetězce (`AGENT_LEASE_FALLBACKS`). Nic nespouští ani nemaže:
        - jen `pending` tasky (cokoli rozdělaného zůstává u svého agenta),
        - jen po `grace` sekundách čekání (krátký výpadek nevyvolá přeskakování),
        - odesílatel i člověk dostanou zprávu, kam úkol putuje.
        Idempotentní; volá ho broker (každých ~15 s), bridge a hooky.
        """
        grace = self.settings.failover_grace if grace is None else grace
        chains = self.settings.fallbacks
        if not chains:
            return []
        now = time.time()
        limited = {l["agent"]: l for l in self.limits()}
        limited.update({p["agent"]: p for p in self.provider_states()
                        if p["state"] == "limited"})
        alive = {p["agent"] for p in self.peers() if p["presence"] in ("idle", "busy")}

        def can_run(agent: str) -> bool:
            return agent in alive or f"{agent}-bridge" in alive

        moves: list[dict] = []
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for src, chain in chains.items():
                if can_run(src):
                    continue  # zdroj je v pořádku
                target = next((c for c in chain if can_run(c)), None)
                if not target:
                    continue
                activation = con.execute(
                    "SELECT after_message_id FROM failover_activations WHERE agent=?", (src,)
                ).fetchone()
                after_message_id = int(activation["after_message_id"]) if activation else 0
                source_limited = src in limited or f"{src}-bridge" in limited
                rows = con.execute(
                    "SELECT m.id, m.agent, m.text, m.sent_at FROM deliveries d "
                    "JOIN messages m ON m.id=d.message_id "
                    "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=d.recipient "
                    "WHERE d.recipient=? AND d.state='pending' AND m.kind='task' "
                    "AND r.accepted_at IS NULL AND m.id>? AND m.sent_at <= ? AND m.agent<>?",
                    # Explicitní limit = krátká lhůta. Pouhé „nedává znamení" = dlouhá:
                    # agent může spát v `wait` a úkol si vzít, jakmile ho probudíme.
                    (src, after_message_id, now - (grace if source_limited
                                                   else max(grace, self.settings.offline_grace)),
                     target),
                ).fetchall()
                for r in rows:
                    if con.execute("SELECT 1 FROM deliveries WHERE message_id=? AND recipient=?",
                                   (r["id"], target)).fetchone():
                        continue
                    con.execute("UPDATE deliveries SET recipient=?, updated_at=? "
                                "WHERE message_id=? AND recipient=?", (target, now, r["id"], src))
                    con.execute("UPDATE messages SET recipient=? WHERE id=?", (target, r["id"]))
                    con.execute("DELETE FROM receipts WHERE message_id=? AND recipient=?",
                                (r["id"], src))
                    limit = limited.get(src) or limited.get(f"{src}-bridge")
                    if limit and limit.get("until"):
                        why = ("limited until "
                               f"{time.strftime('%H:%M', time.localtime(limit['until']))}")
                    elif limit:
                        why = f"provider limit ({limit.get('reason') or limit.get('source')})"
                    else:
                        why = "unavailable"
                    note = f"↪ task #{r['id']} moved {src} → {target} ({src}: {why})"
                    for to in {r["agent"], human_name(self.db_path)}:
                        con.execute(
                            "INSERT INTO messages(agent, text, sent_at, recipient, kind, reply_to) "
                            "VALUES('broker',?,?,?,'result',?)", (note, now, to, r["id"]))
                    snippet = " ".join(r["text"].split())[:400]
                    # Zprávy od brokeru se nescreenují (věří se jim), takže cizí text
                    # do nich nesmí jít bez kontroly — jinak by přesun úkolu obešel
                    # screening prompt injection (security review 16. 10.).
                    if heuristic_risk(snippet) >= 0.5:
                        snippet = (f"[held back: possible injection — read #{r['id']} via "
                                   "`inbox` and judge it yourself]")
                    else:
                        snippet = untrusted(snippet, 400)
                    handover = (f"↪ You took over task #{r['id']} from {src} ({why}), "
                                f"assigned by {r['agent']}: {snippet}")
                    # Nový příjemce dostane ZPRÁVU S NOVÝM ID: původní úkol má staré id,
                    # které může být za kurzorem cíle, a do jeho kontextu by se nikdy nevložilo.
                    con.execute(
                        "INSERT INTO messages(agent, text, sent_at, recipient, kind, reply_to) "
                        "VALUES('broker',?,?,?,'result',?)",
                        (handover, now, target, r["id"]))
                    self._record(con, "broker", "failover", "", f"#{r['id']} {src}->{target}")
                    moves.append({"id": r["id"], "from": src, "to": target})
        for m in moves:
            self._signal(m["to"])
        return moves

    def mark_notified(self, key: str) -> bool:
        """True, pokud je to poprvé (a tedy se má poslat)."""
        with self._connect() as con:
            return bool(con.execute("INSERT OR IGNORE INTO notified(key, at) VALUES(?,?)",
                                    (key, time.time())).rowcount)

    def unmark_notified(self, key: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM notified WHERE key=?", (key,))

    def needs_review_list(self, limit: int = 20) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.id, m.recipient, m.text, d.last_error, d.updated_at FROM deliveries d "
                "JOIN messages m ON m.id=d.message_id WHERE d.state IN "
                "('needs_review','dead_letter','failed') ORDER BY m.id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [{"id": r["id"], "to": r["recipient"], "text": r["text"],
                 "error": r["last_error"], "updated_at": r["updated_at"]} for r in rows]

    def recent_audit(self, actions: tuple[str, ...], since_id: int = 0, limit: int = 50) -> list[dict]:
        marks = ",".join("?" * len(actions))
        with self._connect() as con:
            rows = con.execute(
                f"SELECT id, at, agent, action, path, detail FROM audit WHERE id>? "
                f"AND action IN ({marks}) ORDER BY id LIMIT ?", (since_id, *actions, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def board(self) -> list[dict]:
        """Přehled „kdo co dělá": agent → přítomnost, limit, aktivní úkol, fronta, nepotvrzené."""
        now = time.time()
        peers = {p["agent"]: p for p in self.peers()}
        with self._connect() as con:
            rows = con.execute(
                "SELECT d.recipient, d.state, d.message_id, m.text, m.agent AS sender, "
                "m.sent_at, r.accepted_at FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "LEFT JOIN receipts r ON r.message_id=d.message_id AND r.recipient=d.recipient "
                "WHERE d.state IN ('pending','leased','started','needs_review') "
                "AND m.kind IN ('task','chat','control') ORDER BY d.message_id"
            ).fetchall()
            path_rows = con.execute(
                "SELECT tp.message_id, tp.path FROM task_paths tp "
                "JOIN deliveries d ON d.message_id=tp.message_id "
                "WHERE d.state='pending' ORDER BY tp.message_id, tp.path"
            ).fetchall()
            names = set(peers) | {r["recipient"] for r in rows}
        paths_by_message: dict[int, list[str]] = {}
        for path_row in path_rows:
            paths_by_message.setdefault(int(path_row["message_id"]), []).append(path_row["path"])
        claims = self.claims()
        board = []
        for name in sorted(names):
            mine = [r for r in rows if r["recipient"] == name]
            active = next((r for r in mine if r["state"] in ("started", "leased")), None)
            blocked_task = None
            for pending in (r for r in mine if r["state"] == "pending"):
                for path in paths_by_message.get(int(pending["message_id"]), []):
                    blocker = next((claim for claim in claims
                                    if claim.agent not in (name, f"{name}-bridge") and
                                    (covers(claim.path, path) or covers(path, claim.path))), None)
                    if blocker:
                        blocked_task = {
                            "id": pending["message_id"], "path": path,
                            "held_by": blocker.agent, "expires_in": blocker.expires_in,
                        }
                        break
                if blocked_task:
                    break
            provider_reason = (
                "provider rate limit" if peers.get(name, {}).get("provider_state") == "limited"
                else "provider signal unavailable"
                if peers.get(name, {}).get("provider_state") == "unknown"
                else "reported rate limit" if peers.get(name, {}).get("limited_until")
                else "no recent heartbeat" if peers.get(name, {}).get("presence") == "offline"
                else ""
            )
            board.append({
                "agent": name,
                "presence": peers.get(name, {}).get("presence", "offline"),
                "status": peers.get(name, {}).get("status", ""),
                "limited_until": peers.get(name, {}).get("limited_until"),
                "provider_state": peers.get(name, {}).get("provider_state"),
                "provider_source": peers.get(name, {}).get("provider_source"),
                "provider_reason": peers.get(name, {}).get("provider_reason", ""),
                "provider_resets_at": peers.get(name, {}).get("provider_resets_at"),
                "blocked_reason": provider_reason or (
                    "task lease conflict" if blocked_task else ""
                ),
                "blocked_task": blocked_task,
                "active": ({"id": active["message_id"], "text": active["text"][:120],
                            "from": active["sender"]} if active else None),
                "queued": sum(r["state"] == "pending" for r in mine),
                "unaccepted": sum(r["state"] == "pending" and not r["accepted_at"] for r in mine),
                "needs_review": sum(r["state"] == "needs_review" for r in mine),
                "oldest_wait_s": int(now - min((r["sent_at"] for r in mine
                                                if r["state"] == "pending"), default=now)),
            })
        return board

    def task_summary(self, recipient: str) -> dict:
        """Aggregate task pipeline state without loading task text or lease tokens."""
        _check_agent(recipient)
        with self._connect() as con:
            rows = con.execute(
                "SELECT d.state, COUNT(*) AS total, "
                "SUM(CASE WHEN r.accepted_at IS NULL THEN 1 ELSE 0 END) AS unaccepted "
                "FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=d.recipient "
                "WHERE d.recipient=? AND m.kind='task' GROUP BY d.state",
                (recipient,),
            ).fetchall()
        states = {r["state"]: int(r["total"]) for r in rows}
        pending = states.get("pending", 0)
        pending_row = next((r for r in rows if r["state"] == "pending"), None)
        unaccepted = int(pending_row["unaccepted"] or 0) if pending_row else 0
        return {
            "pending": pending,
            "unaccepted": unaccepted,
            "accepted_without_start": max(0, pending - unaccepted),
            "leased": states.get("leased", 0),
            "started": states.get("started", 0),
            "needs_review": states.get("needs_review", 0),
        }

    def get_screen(self, message_id: int) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT risk, actionable FROM screens WHERE message_id=?",
                              (message_id,)).fetchone()
        return {"risk": float(row["risk"]), "actionable": row["actionable"]} if row else None

    def set_screen(self, message_id: int, risk: float, actionable: float | None = None) -> None:
        with self._connect() as con:
            con.execute("INSERT OR REPLACE INTO screens(message_id, risk, actionable, at) "
                        "VALUES(?,?,?,?)", (message_id, risk, actionable, time.time()))

    def overdue(self, seconds: int = 120) -> list[dict]:
        """Adresované zprávy, které déle než `seconds` nikdo nepotvrdil (nebo ani neviděl)."""
        now = time.time()
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.id, m.agent, m.recipient, m.kind, m.text, m.sent_at, "
                "r.seen_at, r.accepted_at FROM messages m "
                "JOIN deliveries d ON d.message_id=m.id AND d.recipient=m.recipient "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=m.recipient "
                "WHERE m.kind IN ('chat','task','control') AND d.state IN ('pending','leased') "
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
                "AND recipient=? AND lease_token=? AND state IN ('leased','started')",
                (time.time() + max(1, min(int(lease_seconds), MAX_TASK_LEASE_SECONDS)),
                 time.time(), message_id, recipient,
                 lease_token),
            )
        return bool(cur.rowcount)

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
                "AND d.state IN ('pending','leased') AND (r.accepted_at IS NULL) "
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
                "SELECT m.*, d.state AS delivery_state, d.attempts, d.last_error, "
                "r.seen_at, r.accepted_at, r.note AS accept_note "
                "FROM messages m LEFT JOIN deliveries d "
                "ON d.message_id=m.id AND d.recipient=m.recipient "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=m.recipient "
                "WHERE m.id > ? ORDER BY m.id LIMIT ?", (since_id, limit)
            ).fetchall()
        return [_row_to_message(row, now) for row in rows]

    def latest_messages(self, limit: int = 200) -> list[dict]:
        """Nejnovější historie v chronologickém pořadí pro první UI snapshot."""
        now = time.time()
        limit = _limit(limit)
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.*, d.state AS delivery_state, d.attempts, d.last_error, "
                "r.seen_at, r.accepted_at, r.note AS accept_note "
                "FROM messages m LEFT JOIN deliveries d "
                "ON d.message_id=m.id AND d.recipient=m.recipient "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=m.recipient "
                "WHERE m.id IN "
                "(SELECT id FROM messages ORDER BY id DESC LIMIT ?) ORDER BY m.id",
                (limit,),
            ).fetchall()
        return [_row_to_message(row, now) for row in rows]

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
        limit = _limit(limit)
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.*, d.state AS delivery_state, d.attempts, d.last_error "
                "FROM messages m LEFT JOIN deliveries d "
                "ON d.message_id=m.id AND d.recipient=m.recipient "
                "WHERE m.id>? AND m.agent<>? AND m.recipient IN ('*', ?) "
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
                "d.last_error, d.updated_at FROM deliveries d "
                "JOIN messages m ON m.id = d.message_id "
                "WHERE d.recipient = ? AND (? IS NULL OR d.recipient = ? OR m.agent = ?) "
                "ORDER BY m.id DESC LIMIT ?",
                (recipient, viewer, viewer, viewer, limit),
            ).fetchall()
        return [_row_to_job(r, now) for r in rows]

    def addressed(self, recipient: str, *, since_id: int = 0, limit: int = 20) -> list[dict]:
        """Nové zprávy výslovně adresované agentovi; broadcast nikoho nebudí."""
        now = time.time()
        limit = _limit(limit)
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.*, d.state AS delivery_state, d.attempts, d.last_error "
                "FROM messages m LEFT JOIN deliveries d "
                "ON d.message_id=m.id AND d.recipient=m.recipient "
                "WHERE m.id>? AND m.recipient=? ORDER BY m.id LIMIT ?",
                (since_id, recipient, limit),
            ).fetchall()
        return [_row_to_message(row, now) for row in rows]

    def worker_activation(self, agent: str, *, include_existing: bool = False) -> int:
        """Create once and return the durable lower task-id bound for a worker."""
        _check_agent(agent)
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT after_message_id FROM worker_activations WHERE agent=?", (agent,)
            ).fetchone()
            if row:
                return int(row["after_message_id"])
            after = 0
            if not include_existing:
                after = int(con.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages"
                ).fetchone()[0])
            con.execute(
                "INSERT INTO worker_activations(agent, after_message_id, activated_at) "
                "VALUES(?,?,?)", (agent, after, now),
            )
            self._record(con, agent, "worker-activate", "", f"after #{after}")
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
        terminal = {
            DeliveryState.SUCCEEDED.value, DeliveryState.FAILED.value,
            DeliveryState.NEEDS_REVIEW.value, DeliveryState.CANCELLED.value,
            DeliveryState.EXPIRED.value, DeliveryState.DEAD_LETTER.value,
        }
        allowed = {
            DeliveryState.LEASED.value: terminal | {DeliveryState.STARTED.value},
            DeliveryState.STARTED.value: terminal,
        }
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT state, lease_token FROM deliveries "
                "WHERE message_id=? AND recipient=?",
                (message_id, recipient),
            ).fetchone()
            if not row:
                return False
            current = row["state"]
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
                "AND state IN ('pending','failed','needs_review','dead_letter')",
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
                "AND state NOT IN ('succeeded','cancelled','expired')",
                (time.time(), message_id, recipient),
            )
        return bool(cur.rowcount)

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
                    "d.state IN ('pending','leased','started','needs_review')))",
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


def _row_to_job(row: sqlite3.Row, now: float) -> dict:
    return {
        "id": row["id"], "agent": row["agent"], "recipient": row["recipient"],
        "kind": row["kind"], "text": row["text"], "state": row["state"],
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
