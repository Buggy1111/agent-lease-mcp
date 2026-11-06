"""Reproduce B1 reservation scaling using synthetic rows in an isolated temp DB.

Not a wall-clock pytest gate: machine speed varies. Reports actual timing and asserts
only the real behavioral requirement (every overlapping pending task stays blocked).
Run from the repo with PYTHONPATH=src. No broker or production database is used.
"""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from agent_lease_mcp.store import Store


def benchmark(count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="nox-b1-reservations-") as tmp:
        root = Path(tmp)
        store = Store(root / "isolated.db")
        now = time.time()
        # Seeding is excluded from the measurement. Active worksets do not overlap;
        # all pending worksets conflict with active task zero.
        with store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for index in range(count):
                mid = con.execute(
                    "INSERT INTO messages(agent,text,sent_at,recipient,kind) "
                    "VALUES('michal','TEST ONLY',?,'worker','task')", (now,),
                ).lastrowid
                con.execute(
                    "INSERT INTO deliveries(message_id,recipient,state,attempts,lease_token,lease_until,updated_at) "
                    "VALUES(?,'worker','started',1,?,?,?)",
                    (mid, uuid.uuid4().hex, now + 3600, now),
                )
                con.execute("INSERT INTO task_paths(message_id,path) VALUES(?,?)",
                            (mid, str(root / "worksets" / str(index))))
            for _ in range(count):
                mid = con.execute(
                    "INSERT INTO messages(agent,text,sent_at,recipient,kind) "
                    "VALUES('michal','TEST ONLY',?,'codex','task')", (now,),
                ).lastrowid
                con.execute(
                    "INSERT INTO deliveries(message_id,recipient,state,updated_at) "
                    "VALUES(?,'codex','pending',?)", (mid, now),
                )
                con.execute("INSERT INTO task_paths(message_id,path) VALUES(?,?)",
                            (mid, str(root / "worksets" / "0" / "file.py")))
        reservation_selects = [0]
        connect = store._connect

        def trace(sql):
            # Diagnostic for this candidate's SQL shape; not a pytest assertion or API contract.
            if "FROM task_paths tp JOIN deliveries" in sql:
                reservation_selects[0] += 1

        @contextmanager
        def measured_connect():
            with connect() as con:
                con.set_trace_callback(trace)
                yield con

        store._connect = measured_connect
        started = time.perf_counter()
        leased = store.lease_next("codex")
        elapsed = time.perf_counter() - started
        assert leased is None, "an overlapping pending task was incorrectly leased"
        with store._connect() as con:
            pending = con.execute(
                "SELECT COUNT(*) FROM deliveries WHERE recipient='codex' AND state='pending'"
            ).fetchone()[0]
            busy_timeout = con.execute("PRAGMA busy_timeout").fetchone()[0]
        assert pending == count
        return {"active_tasks": count, "blocked_pending": count,
                "lease_next_seconds": round(elapsed, 6), "busy_timeout_ms": busy_timeout,
                "no_lease": True, "pending_unchanged": True,
                "reservation_selects": reservation_selects[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[200, 1000])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if any(count <= 0 or count > 10000 for count in args.counts):
        parser.error("counts must be between 1 and 10000")
    report = {"captured_at": datetime.now(UTC).isoformat(), "python": platform.python_version(),
              "sqlite": sqlite3.sqlite_version, "scenario": "synthetic B1 reservation benchmark",
              "results": [benchmark(count) for count in args.counts]}
    data = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(data)
    print(data, end="")


if __name__ == "__main__":
    main()
