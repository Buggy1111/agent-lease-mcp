"""Spotřeba tokenů jednotlivých pokusů úkolu (fáze C3a).

Část `Store`. Zapisuje ji bridge podle toho, co hlásí klient (`claude --output-format
json`, `codex --json`). Chybějící údaj zůstává NULL („neznámo“), nikdy nula. Je to
evidence pro přehled a pozdější stropy (C3b), ne účetnictví: zapisující proces je
stejný uživatel jako agenti.
"""

from __future__ import annotations

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

    def record_usage(self, message_id: int, recipient: str, lease_token: str, usage: dict) -> bool:
        """
        Spotřeba právě běžícího pokusu; zapisuje jen držitel platného lease.

        Lease ale nedokazuje, že čísla změřil bridge: agent s vlastním lease může
        zapsat dřív. Proto platí: první měřený zápis nahradí založení ("unreported"),
        každý další už jen zhorší – neznámá hodnota vyhraje, známé jen rostou. Podvrh
        tak stropy C3b může jen zpřísnit, ne obejít (review #15 F1, Nox).
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

    def usage_summary(self, *, since: float = 0, recipient: str | None = None) -> dict:
        """Součty od `since` (unix čas), volitelně pro jednoho agenta; neznámé hodnoty se nesčítají."""
        where, args = "WHERE at >= ?", [float(since)]
        if recipient:
            where += " AND recipient = ?"
            args.append(recipient)
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS attempts, SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
                "SUM(cache_creation_tokens) AS cache_creation_tokens, SUM(cost_usd) AS cost_usd, "
                "SUM(cost_usd IS NULL) AS cost_unknown "
                f"FROM task_usage {where}", args,
            ).fetchone()
        return dict(row)
