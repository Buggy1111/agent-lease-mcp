"""Offline WAL-to-rollback-journal migration; never run alongside old clients."""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def migrate(db: Path) -> None:
    """Caller must stop every process using db before invoking this function."""
    if not db.is_file():
        raise FileNotFoundError(db)
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=rw", uri=True, timeout=5)) as con:
        con.execute("PRAGMA busy_timeout=5000")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("integrity check failed; database was not migrated")
        mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if mode.lower() != "delete":
            raise sqlite3.OperationalError(f"failed to change journal mode: {mode}")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("integrity check failed after journal mode change")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-clients-stopped", action="store_true", required=True,
                        help="confirm that every process using this database is stopped")
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    migrate(args.database)
    print("Journal mode is DELETE; integrity check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
