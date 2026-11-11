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
_SOURCES = frozenset({"claude-json", "codex-jsonl"})


class UsageMixin:
    def record_usage(self, message_id: int, recipient: str, attempt: int, usage: dict) -> None:
        """Spotřeba jednoho pokusu; opakovaný zápis téhož pokusu ho přepíše."""
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
            con.execute(
                "INSERT INTO task_usage(message_id, recipient, attempt, at, source, model, "
                "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(message_id, recipient, attempt) "
                "DO UPDATE SET at=excluded.at, source=excluded.source, model=excluded.model, "
                "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
                "cache_read_tokens=excluded.cache_read_tokens, "
                "cache_creation_tokens=excluded.cache_creation_tokens, cost_usd=excluded.cost_usd",
                (int(message_id), recipient, int(attempt), time.time(), usage["source"], model,
                 *values, cost),
            )

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
