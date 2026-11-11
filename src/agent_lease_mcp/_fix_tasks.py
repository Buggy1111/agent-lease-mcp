"""Úkol „oprava“ (fáze C1a): úkol, který smí zapisovat, ale jen se dvěma svoleními.

Část `Store`. Oprava je běžný `task` (fronta, lease, zrušení, náhradník fungují
beze změny) plus řádek ve `fix_tasks`: repozitář, základní commit a povolené
cesty. Než ji kdokoli převezme (bridge i interaktivní session), musí platit:

1. operátorská politika `fix-runner.toml` vedle DB povoluje tento repozitář
   a tohoto adresáta (soubor patří člověku, agent ho přes broker nezmění);
2. člověk ve webu schválil přesně tohle zadání, tomuto adresátovi, na jeden pokus.

Jinak úkol skončí v `needs_review` s chybou „fix: …“ a nic se nespustí. Samotný
zápisový běh ve worktree je C1c; tady je jen brána, ať ji nejde obejít žádnou
cestou převzetí.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import tomllib
from pathlib import Path, PurePosixPath

from .hygiene import check_path, check_short

FIX_POLICY = "fix-runner.toml"
MAX_FIX_PATHS = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS fix_tasks (
    message_id         INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    repo               TEXT NOT NULL,
    base_commit        TEXT NOT NULL,
    allowed            TEXT NOT NULL DEFAULT '[]',
    approved_by        TEXT,
    approved_at        REAL,
    approved_hash      TEXT,
    approved_recipient TEXT,
    approved_attempt   INTEGER,
    approved_reason    TEXT
);
"""


def check_allowed(paths: list[str] | None) -> list[str]:
    """Povolené cesty relativně ke kořeni repa; prázdný seznam = celé repo (C1c ubere kritické)."""
    paths = list(paths or [])
    if len(paths) > MAX_FIX_PATHS:
        raise ValueError(f"at most {MAX_FIX_PATHS} allowed paths")
    clean: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str):
            raise TypeError("allowed paths must be strings")
        path = PurePosixPath(check_path(raw.strip()))
        if (not raw.strip() or path.is_absolute() or ".." in path.parts
                or any(ord(c) < 32 for c in raw)):
            raise ValueError(f"allowed path must be relative inside the repository: {raw[:80]!r}")
        clean.add(path.as_posix())
    return sorted(clean - {"."}) if clean != {"."} else []


def fix_policy(db_path: Path) -> dict | None:
    """Platná zapnutá politika, nebo None (chybí, je cizí/čitelná pro ostatní, nebo neplatná)."""
    from .quality_runner import policy_bytes
    try:
        data = tomllib.loads(policy_bytes(db_path, FIX_POLICY).decode())
    except (OSError, ValueError, UnicodeError):
        return None
    repos, agents = data.get("repositories"), data.get("agents")
    if (set(data) - {"version", "enabled", "repositories", "agents"} or data.get("version") != 1
            or type(data.get("version")) is not int or data.get("enabled") is not True
            or not isinstance(repos, list) or not isinstance(agents, list)
            or not all(isinstance(r, str) and Path(r).is_absolute() for r in repos)
            or not all(isinstance(a, str) and a for a in agents)):
        return None
    return {"repositories": {str(Path(r).resolve()) for r in repos}, "agents": set(agents)}


class FixTasksMixin:
    @staticmethod
    def _fix_hash(text: str, repo: str, base: str, allowed: str) -> str:
        return hashlib.sha256(json.dumps([text, repo, base, allowed]).encode()).hexdigest()

    def _fix_hold(self, con: sqlite3.Connection, message_id: int, recipient: str,
                  *, next_attempt: bool = True) -> str | None:
        """Důvod, proč opravu teď nesmí převzít `recipient` („fix: …“), nebo None."""
        row = con.execute(
            "SELECT f.*, m.text, d.attempts FROM fix_tasks f JOIN messages m ON m.id=f.message_id "
            "JOIN deliveries d ON d.message_id=f.message_id AND d.recipient=? "
            "WHERE f.message_id=?", (recipient, message_id),
        ).fetchone()
        if row is None:
            return None  # běžný úkol
        policy = fix_policy(self.db_path)
        if policy is None:
            return "fix: write mode is not enabled (operator policy missing or invalid)"
        if row["repo"] not in policy["repositories"] or recipient not in policy["agents"]:
            return "fix: operator policy does not allow this repository or agent"
        attempt = row["attempts"] + (1 if next_attempt else 0)
        if (row["approved_hash"] != self._fix_hash(row["text"], row["repo"], row["base_commit"],
                                                   row["allowed"])
                or row["approved_recipient"] != recipient
                or attempt != (row["approved_attempt"] or 0) + 1):
            return "fix: waiting for the human to approve this write-mode run"
        return None

    def fix_task(self, message_id: int) -> dict | None:
        """Repozitář, základ a povolené cesty opravy (pro bridge v C1c); None u běžného úkolu."""
        with self._connect() as con:
            row = con.execute("SELECT repo, base_commit, allowed FROM fix_tasks WHERE message_id=?",
                              (int(message_id),)).fetchone()
        if row is None:
            return None
        return {"repo": row["repo"], "base_commit": row["base_commit"],
                "allowed": json.loads(row["allowed"])}

    def approve_fix(self, message_id: int, recipient: str, approver: str, reason: str) -> bool:
        """
        LIDSKÉ schválení jednoho zápisového běhu opravy. Vázané na přesné zadání
        (text, repo, základ, cesty), adresáta a příští pokus. Není v METHODS brokeru:
        volá ho jen web UI s ověřenou lidskou session. Politiku nenahrazuje.
        """
        reason = check_short("reason", reason.strip())
        if not reason:
            raise ValueError("a reason is required")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = con.execute(
                "SELECT f.*, m.text, d.state, d.last_error, d.attempts FROM fix_tasks f "
                "JOIN messages m ON m.id=f.message_id "
                "JOIN deliveries d ON d.message_id=f.message_id AND d.recipient=? "
                "WHERE f.message_id=?", (recipient, message_id),
            ).fetchone()
            # schvaluje se jen oprava, která ještě neběžela, nebo ji zadržela tahle brána
            if row is None or not (row["state"] == "pending" or (
                    row["state"] == "needs_review" and (row["last_error"] or "").startswith("fix:"))):
                return False
            con.execute(
                "UPDATE fix_tasks SET approved_by=?, approved_at=?, approved_hash=?, "
                "approved_recipient=?, approved_attempt=?, approved_reason=? WHERE message_id=?",
                (approver, now, self._fix_hash(row["text"], row["repo"], row["base_commit"],
                                               row["allowed"]),
                 recipient, row["attempts"], reason, message_id),
            )
            con.execute(
                "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
                "last_error='', updated_at=? WHERE message_id=? AND recipient=?",
                (now, message_id, recipient),
            )
            self._record(con, approver, "approve-fix", row["repo"], f"#{message_id}: {reason}")
        self._signal(recipient)
        return True
