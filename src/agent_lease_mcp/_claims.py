"""Nájmy na cesty (claim, edit_gate, release…).

Část `Store`; rozděleno ze store.py podle rozboru Noxe (#516).
"""

from __future__ import annotations

import sqlite3
import time

from ._store_common import (
    _check_agent,
    _row_to_claim,
)
from .config import (
    MAX_PATHS_PER_CLAIM,
    MAX_TTL_SECONDS,
)
from .hygiene import (
    check_path,
    check_short,
)
from .models import Claim, ClaimResult
from .policy import covers, normalize_path


class ClaimsMixin:
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
        ttl = self._ttl(ttl_seconds)
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
                self._upsert_claim(con, path, agent, purpose, now, ttl)
            return ClaimResult(granted=wanted, conflicts=[])

    def _ttl(self, ttl_seconds: int | None = None) -> int:
        """Délka nájmu se stropem: obří AGENT_LEASE_TTL nesmí blokovat roky (Nox, 17. 10.)."""
        return max(1, min(int(ttl_seconds or self.settings.default_ttl), MAX_TTL_SECONDS))

    def _upsert_claim(self, con: sqlite3.Connection, path: str, agent: str, purpose: str,
                      now: float, ttl: int) -> None:
        """Zapíše (nebo převezme) nájem na cestu a zaznamená to do auditu."""
        con.execute(
            "INSERT INTO claims(path, agent, purpose, claimed_at, expires_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "agent=excluded.agent, purpose=excluded.purpose, expires_at=excluded.expires_at",
            (path, agent, purpose, now, now + ttl),
        )
        self._record(con, agent, "claim", path, purpose)

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
        ttl = self._ttl()
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
                self._upsert_claim(con, target, agent, purpose[:500], now, ttl)
        return None

    def _conflicts(self, con: sqlite3.Connection, wanted: list[str], agent: str, *,
                   task_id: int | None = None) -> list[Claim]:
        """
        Kolize v OBOU směrech: cizí nájem na adresář nade mnou i cizí nájem na
        soubor uvnitř adresáře, který si beru.

        B1: rozběhnutý úkol (leased/started s platným lease) drží svoje `task_paths`
        jako nájem svého adresáta, dokud lease platí. Rezervace tak nevyžaduje nové
        řádky ani úklid: skončí sama s koncem lease. Pro převzetí úkolu (`task_id`)
        brání i úkoly téhož adresáta, ať dvě session stejného jména nepracují nad
        stejnými soubory souběžně.
        """
        now = time.time()
        held = [
            _row_to_claim(r)
            for r in con.execute(
                "SELECT * FROM claims WHERE agent <> ? AND expires_at > ?", (agent, now)
            ).fetchall()
        ]
        held += [
            Claim(path=r["path"], agent=r["recipient"], purpose=f"task #{r['message_id']}",
                  claimed_at=r["updated_at"], expires_at=r["lease_until"])
            for r in con.execute(
                "SELECT tp.path, d.recipient, d.message_id, d.updated_at, d.lease_until "
                "FROM task_paths tp JOIN deliveries d ON d.message_id=tp.message_id "
                "WHERE d.state IN ('leased','started') AND d.lease_until > ? "
                "AND (d.recipient <> ? OR (? IS NOT NULL AND d.message_id <> ?))",
                (now, agent, task_id, task_id),
            ).fetchall()
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
            # B1: rezervace rozběhnutého úkolu (hook ji vynucuje, má ji vidět i `owner`)
            for r in con.execute(
                "SELECT tp.path, d.recipient, d.message_id, d.updated_at, d.lease_until "
                "FROM task_paths tp JOIN deliveries d ON d.message_id=tp.message_id "
                "WHERE d.state IN ('leased','started') AND d.lease_until > ?", (now,),
            ).fetchall():
                if covers(r["path"], target):
                    return Claim(path=r["path"], agent=r["recipient"],
                                 purpose=f"task #{r['message_id']}",
                                 claimed_at=r["updated_at"], expires_at=r["lease_until"])
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
