"""Spotřeba tokenů jednotlivých pokusů úkolu (fáze C3a).

Část `Store`. Zapisuje ji bridge podle toho, co hlásí klient (`claude --output-format
json`, `codex --json`). Chybějící údaj zůstává NULL („neznámo“), nikdy nula. Je to
evidence pro přehled a pozdější stropy (C3b), ne účetnictví: zapisující proces je
stejný uživatel jako agenti.
"""

from __future__ import annotations

import hashlib
import secrets
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_usage (
    message_id            INTEGER NOT NULL,
    recipient             TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    at                    REAL NOT NULL,
    source                TEXT NOT NULL,
    model                 TEXT,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd              REAL,
    report_key_hash       TEXT,
    requested_model       TEXT,
    PRIMARY KEY(message_id, recipient, attempt)
);
"""

_COUNTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
# "unreported": pokus proběhl, ale klient nic strojově čitelného nevrátil (timeout, text…).
_SOURCES = frozenset({"claude-json", "codex-jsonl", "unreported"})


_TOKENS = ("COALESCE(input_tokens,0)+COALESCE(output_tokens,0)"
           "+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0)")


# Tokeny pokusu jsou neznámé, chybí-li kterékoli pole, které jeho zdroj hlásí (částečný
# údaj by jinak tiše počítal chybějící část jako nulu – review #16, Nox). Codex zápis
# do cache nehlásí vůbec, proto se u něj cache_creation nevyžaduje.
_NO_TOKENS = ("(input_tokens IS NULL OR output_tokens IS NULL OR cache_read_tokens IS NULL "
              "OR (cache_creation_tokens IS NULL AND source <> 'codex-jsonl'))")


class UsageMixin:
    @staticmethod
    def _migrate_usage(con) -> None:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(task_usage)")}
        if "report_key_hash" not in cols:
            con.execute("ALTER TABLE task_usage ADD COLUMN report_key_hash TEXT")
        if "requested_model" not in cols:
            con.execute("ALTER TABLE task_usage ADD COLUMN requested_model TEXT")

    def begin_usage(self, message_id: int, recipient: str, lease_token: str,
                    requested_model: str | None = None) -> str | None:
        """Reserve metering before launch; return a key once, never to the child.

        A lease permits task execution, not replacement of the bridge's report.
        An existing row cannot be adopted or reset with the lease token alone.
        """
        # Požadovaný (připnutý) model se ukládá zvlášť od skutečně použitého (`model` z výstupu
        # CLI) — rozdíl ukáže tichý fallback poskytovatele (oponentura fáze C, Nox).
        if requested_model is not None and (not isinstance(requested_model, str) or len(requested_model) > 200):
            raise ValueError("invalid requested_model")
        key = secrets.token_urlsafe(32)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            held = con.execute(
                "SELECT attempts FROM deliveries WHERE message_id=? AND recipient=? "
                "AND lease_token=? AND state='started' AND lease_until>?",
                (int(message_id), recipient, lease_token or "", now),
            ).fetchone()
            if held is None:
                return None
            cur = con.execute(
                "INSERT INTO task_usage(message_id, recipient, attempt, at, source, report_key_hash, "
                "requested_model) VALUES(?,?,?,?,'unreported',?,?) ON CONFLICT DO NOTHING",
                (int(message_id), recipient, held["attempts"], now,
                 hashlib.sha256(key.encode()).hexdigest(), requested_model or None),
            )
            return key if cur.rowcount else None

    def _over(self, con, where: str, args: tuple, cap_tokens: int, cap_usd: float,
              label: str) -> str | None:
        """
        Důvod zastavení podle stropu, nebo None. Fail-closed: pokus, jehož metrika
        hlídaná stropem je neznámá (timeout, klient bez čísel, Codex bez ceny), se
        nepočítá jako nula, ale zastaví – jinak by neznámá spotřeba strop obešla
        (review #16 F1). Codex cenu nehlásí, takže s USD stropem po pokusu stojí.
        """
        if not (cap_tokens or cap_usd):
            return None
        row = con.execute(
            f"SELECT COALESCE(SUM({_TOKENS}),0), COALESCE(SUM(cost_usd),0), "
            f"SUM({_NO_TOKENS}), SUM(cost_usd IS NULL) FROM task_usage WHERE {where}", args,
        ).fetchone()
        tokens, usd, no_tokens, no_cost = int(row[0]), float(row[1]), row[2] or 0, row[3] or 0
        if cap_tokens and no_tokens:
            return f"budget: {label} has {no_tokens} attempt(s) with unknown token usage"
        if cap_usd and no_cost:
            return f"budget: {label} has {no_cost} attempt(s) with unknown cost"
        if cap_tokens and tokens >= cap_tokens:
            return f"budget: {label} used {tokens} tokens (cap {cap_tokens})"
        if cap_usd and usd >= cap_usd:
            return f"budget: {label} used {usd:.2f} USD (cap {cap_usd:.2f})"
        return None

    def _daily_over(self, con, recipient: str, now: float) -> str | None:
        """Důvod, proč agent teď nic nového nezačne (okno 24 h, C3b), nebo None."""
        s = self.settings
        return self._over(con, "recipient=? AND at>=?", (recipient, now - 86400),
                          s.daily_budget_tokens, s.daily_budget_usd, f"{recipient} in 24 h")

    def _task_over(self, con, message_id: int, recipient: str) -> str | None:
        """
        Důvod, proč úkol nesmí dostat další pokus, nebo None. Sčítají se pokusy všech
        příjemců: převzetí jiným agentem (failover) strop nevynuluje (review #16 F4).
        """
        s = self.settings
        return self._over(con, "message_id=?", (message_id,),
                          s.task_budget_tokens, s.task_budget_usd, "task")

    def record_usage(self, message_id: int, recipient: str, lease_token: str, usage: dict, *,
                     report_key: str | None = None) -> bool:
        """
        Spotřeba právě běžícího pokusu; zapisuje jen držitel platného lease.

        Bridge rezervuje řádek přes begin_usage a drží samostatný report_key mimo
        potomka. Samotný lease pak nedovolí změnit ani resetovat jeho neznámou
        spotřebu. Starší, nerezervované zápisy zachovávají původní rozhraní.
        Další reporty jen zhorší: neznámá vyhraje, známé hodnoty rostou.
        Vrací False bez platného lease.
        """
        if not isinstance(usage, dict) or usage.get("source") not in _SOURCES:
            raise ValueError("unknown usage source")
        values = []
        for key in _COUNTS:
            value = usage.get(key)
            if value is not None and (type(value) is not int or not 0 <= value < 10**12):
                raise ValueError(f"invalid {key}")
            values.append(value)
        cost = usage.get("cost_usd")
        if cost is not None and (not isinstance(cost, (int, float)) or not 0 <= cost < 10**6):
            raise ValueError("invalid cost_usd")
        model = usage.get("model")
        if model is not None and (not isinstance(model, str) or len(model) > 200):
            raise ValueError("invalid model")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            held = con.execute(
                "SELECT attempts FROM deliveries WHERE message_id=? AND recipient=? "
                "AND lease_token=? AND state IN ('leased','started') AND lease_until>?",
                (int(message_id), recipient, lease_token or "", now),
            ).fetchone()
            if held is None:
                return False
            row = con.execute(
                "SELECT report_key_hash FROM task_usage WHERE message_id=? AND recipient=? "
                "AND attempt=?", (int(message_id), recipient, held["attempts"]),
            ).fetchone()
            key_hash = row["report_key_hash"] if row else None
            if key_hash is not None:
                if not isinstance(report_key, str) or not secrets.compare_digest(
                        key_hash, hashlib.sha256(report_key.encode()).hexdigest()):
                    return False
            elif report_key is not None:
                return False
            cur = con.execute(
                "INSERT INTO task_usage(message_id, recipient, attempt, at, source, model, "
                "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(message_id, recipient, attempt) DO UPDATE SET "
                + ", ".join(f"{c} = CASE WHEN task_usage.source = 'unreported' THEN excluded.{c} "
                            f"WHEN excluded.{c} IS NULL OR task_usage.{c} IS NULL THEN NULL "
                            f"ELSE MAX(task_usage.{c}, excluded.{c}) END"
                            for c in (*_COUNTS, "cost_usd"))
                + ", source = CASE WHEN excluded.source = 'unreported' THEN source "
                  "ELSE excluded.source END, model = COALESCE(excluded.model, model)",
                (int(message_id), recipient, held["attempts"], now, usage["source"], model,
                 *values, cost),
            )
        return cur.rowcount > 0

    def usage_recent(self, limit: int = 30) -> list[dict]:
        """Poslední zaznamenané běhy pro web (jen čísla a model, žádný obsah úkolu)."""
        with self._connect() as con:
            rows = con.execute(
                f"SELECT message_id, recipient, attempt, at, source, model, requested_model, input_tokens, "
                f"output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, {_TOKENS} AS tokens, "
                f"NOT {_NO_TOKENS} AS tokens_known FROM task_usage ORDER BY at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),)).fetchall()
        return [dict(r) for r in rows]

    def usage_summary(self, *, since: float = 0, recipient: str | None = None) -> dict:
        """Součty od `since` (unix čas), volitelně pro jednoho agenta; neznámé hodnoty se nesčítají."""
        where, args = "WHERE at >= ?", [float(since)]
        if recipient:
            where += " AND recipient = ?"
            args.append(recipient)
        with self._connect() as con:
            row = con.execute(
                f"SELECT COUNT(*) AS attempts, COALESCE(SUM({_TOKENS}),0) AS tokens, SUM({_NO_TOKENS}) AS tokens_unknown, "
                "SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
                "SUM(cache_creation_tokens) AS cache_creation_tokens, SUM(cost_usd) AS cost_usd, "
                "SUM(cost_usd IS NULL) AS cost_unknown "
                f"FROM task_usage {where}", args,
            ).fetchone()
        return dict(row)
