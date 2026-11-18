"""Sdílená paměť „příznak → příčina → řešení“ (fáze D1).

Část `Store`. Pravidla:

- Nový záznam je vždy `unverified` — tvrzení jednoho agenta, ne fakt.
- Ověřit (`verified`) může jen JINÝ agent než autor, a jen s doloženým důkazem
  (commit, test, odkaz). Stejné pravidlo jako u review: autor nepotvrzuje sám sebe.
- `rejected` = řešení nefunguje, `stale` = přestalo platit (nová verze, jiná příčina).
  Obojí smí kdokoli kromě autora, také s důkazem; záznam zůstává kvůli historii.
- Hledání (SQLite FTS5) vrací ověřené záznamy první a u každého jasně stav;
  zamítnuté a zastaralé jen na vyžádání. Neověřené se nikdy nepodávají jako fakt.
- Text se redaguje (klíče, tokeny) stejně jako zprávy v místnosti; každá změna jde do auditu.
"""

from __future__ import annotations

import re
import sqlite3
import time

from ._store_common import _check_agent
from .config import reserved_names
from .hygiene import check_len, check_short, redact

UNVERIFIED, VERIFIED, REJECTED, STALE = "unverified", "verified", "rejected", "stale"
KNOWLEDGE_STATES = (UNVERIFIED, VERIFIED, REJECTED, STALE)
_VERDICTS = (VERIFIED, REJECTED, STALE)
MAX_FIELD_CHARS = 4000
_WORD = re.compile(r"[\w.\-/]+", re.UNICODE)

KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symptom     TEXT NOT NULL,
    cause       TEXT NOT NULL DEFAULT '',
    solution    TEXT NOT NULL DEFAULT '',
    project     TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'unverified',
    evidence    TEXT NOT NULL DEFAULT '',
    verified_by TEXT NOT NULL DEFAULT '',
    verdict_evidence TEXT NOT NULL DEFAULT '',
    verdict_note TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_project ON knowledge(project, status);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    symptom, cause, solution, content='knowledge', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, symptom, cause, solution) VALUES (new.id, new.symptom, new.cause, new.solution);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, symptom, cause, solution)
    VALUES ('delete', old.id, old.symptom, old.cause, old.solution);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE OF symptom, cause, solution ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, symptom, cause, solution)
    VALUES ('delete', old.id, old.symptom, old.cause, old.solution);
    INSERT INTO knowledge_fts(rowid, symptom, cause, solution) VALUES (new.id, new.symptom, new.cause, new.solution);
END;
"""


def _clean(label: str, value: str, required: bool = False) -> str:
    value = redact((value or "").strip())
    if required and not value:
        raise ValueError(f"{label} must not be empty")
    return check_len(label, value, MAX_FIELD_CHARS)


def _fts_query(text: str) -> str:
    """Bezpečný FTS5 dotaz: jen slova v uvozovkách spojená OR (žádná syntaxe od agenta)."""
    words = [w for w in _WORD.findall(text.lower()) if len(w) >= 2][:20]
    return " OR ".join('"' + w.replace('"', "") + '"' for w in words)


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["fact"] = d["status"] == VERIFIED   # jen ověřené smí agent brát jako fakt
    return d


class KnowledgeMixin:
    def _check_knowledge_agent(self, agent: str) -> None:
        _check_agent(agent)
        if agent.lower() in reserved_names(self.db_path):  # type: ignore[attr-defined]
            raise PermissionError(f"{agent!r} is reserved; human actions go through the web UI")

    def knowledge_add(self, agent: str, symptom: str, cause: str = "", solution: str = "",
                      project: str = "", evidence: str = "") -> dict:
        self._check_knowledge_agent(agent)
        return self._knowledge_insert(agent, symptom, cause, solution, project, evidence)

    def knowledge_human_add(self, human: str, symptom: str, cause: str = "", solution: str = "",
                            project: str = "", evidence: str = "") -> dict:
        """Jen web (lidská session); schválně není v METHODS brokeru."""
        return self._knowledge_insert(human, symptom, cause, solution, project, evidence)

    def _knowledge_insert(self, agent: str, symptom: str, cause: str, solution: str,
                          project: str, evidence: str) -> dict:
        now = time.time()
        fields = (_clean("symptom", symptom, required=True), _clean("cause", cause),
                  _clean("solution", solution), check_short("project", redact((project or "").strip())),
                  _clean("evidence", evidence))
        with self._connect() as con:  # type: ignore[attr-defined]
            cur = con.execute(
                "INSERT INTO knowledge(symptom, cause, solution, project, evidence, author, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)", (*fields, agent, now, now))
            kid = cur.lastrowid
            self._record(con, agent, "knowledge_add", fields[3], f"#{kid}")  # type: ignore[attr-defined]
            return _row(con.execute("SELECT * FROM knowledge WHERE id=?", (kid,)).fetchone())

    def knowledge_verdict(self, agent: str, knowledge_id: int, verdict: str, evidence: str,
                          note: str = "") -> dict:
        """Ověřit / zamítnout / označit jako zastaralé — nikdy ne vlastní záznam, vždy s důkazem."""
        self._check_knowledge_agent(agent)
        # Bezejmenná session by mohla potvrdit záznam, který sama napsala pod svým jménem.
        if agent.startswith(("untrusted-", "unconfigured-")):
            raise PermissionError("an unnamed session cannot judge knowledge; set AGENT_NAME")
        return self._knowledge_judge(agent, knowledge_id, verdict, evidence, note, human=False)

    def knowledge_human_verdict(self, human: str, knowledge_id: int, verdict: str, evidence: str,
                                note: str = "") -> dict:
        """Člověk z webu; schválně není v METHODS brokeru."""
        return self._knowledge_judge(human, knowledge_id, verdict, evidence, note, human=True)

    def _knowledge_judge(self, agent: str, knowledge_id: int, verdict: str, evidence: str,
                         note: str, *, human: bool) -> dict:
        if verdict not in _VERDICTS:
            raise ValueError(f"verdict must be one of {', '.join(_VERDICTS)}")
        evidence = _clean("evidence", evidence, required=True)
        note = _clean("note", note)
        with self._connect() as con:  # type: ignore[attr-defined]
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM knowledge WHERE id=?", (int(knowledge_id),)).fetchone()
            if not row:
                raise KeyError(f"knowledge #{knowledge_id} does not exist")
            if row["author"] == agent:
                raise PermissionError("the author cannot verify, reject or retire their own entry")
            if not human and row["verified_by"].lower() in reserved_names(self.db_path):  # type: ignore[attr-defined]
                raise PermissionError("the human already judged this entry; only the human can change it")
            con.execute(
                "UPDATE knowledge SET status=?, verified_by=?, verdict_evidence=?, verdict_note=?, updated_at=?"
                " WHERE id=?", (verdict, agent, evidence, note, time.time(), int(knowledge_id)))
            self._record(con, agent, f"knowledge_{verdict}", row["project"], f"#{knowledge_id}")  # type: ignore[attr-defined]
            return _row(con.execute("SELECT * FROM knowledge WHERE id=?", (int(knowledge_id),)).fetchone())

    def knowledge_list(self, project: str = "", status: str = "", limit: int = 50) -> list[dict]:
        """Nejnovější záznamy (pro web a přehled), volitelně jen jeden projekt / stav."""
        if status and status not in KNOWLEDGE_STATES:
            raise ValueError(f"status must be one of {', '.join(KNOWLEDGE_STATES)}")
        sql, args = "SELECT * FROM knowledge WHERE 1=1", []
        if project:
            sql += " AND project=?"
            args.append(project)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        with self._connect() as con:  # type: ignore[attr-defined]
            return [_row(r) for r in con.execute(sql, args)]

    def knowledge_get(self, knowledge_id: int) -> dict:
        with self._connect() as con:  # type: ignore[attr-defined]
            row = con.execute("SELECT * FROM knowledge WHERE id=?", (int(knowledge_id),)).fetchone()
        if not row:
            raise KeyError(f"knowledge #{knowledge_id} does not exist")
        return _row(row)

    def knowledge_search(self, query: str, project: str = "", include_unverified: bool = True,
                         include_retired: bool = False, limit: int = 10, status: str = "") -> list[dict]:
        """Ověřené první, pak podle relevance (bm25). Zamítnuté/zastaralé jen na vyžádání.

        `status` omezí výsledky na jeden stav už v SQL (před LIMIT).
        """
        if status and status not in KNOWLEDGE_STATES:
            raise ValueError(f"status must be one of {', '.join(KNOWLEDGE_STATES)}")
        q = _fts_query(check_len("query", query or "", MAX_FIELD_CHARS))
        if not q:
            return []
        states = [status] if status else (
            [VERIFIED] + ([UNVERIFIED] if include_unverified else []) + ([REJECTED, STALE] if include_retired else []))
        sql = ("SELECT k.*, bm25(knowledge_fts) AS score FROM knowledge_fts JOIN knowledge k ON k.id=knowledge_fts.rowid"
               f" WHERE knowledge_fts MATCH ? AND k.status IN ({','.join('?' * len(states))})")
        args: list = [q, *states]
        if project:
            sql += " AND (k.project=? OR k.project='')"
            args.append(project)
        sql += " ORDER BY (k.status='verified') DESC, score LIMIT ?"
        args.append(max(1, min(int(limit), 50)))
        with self._connect() as con:  # type: ignore[attr-defined]
            return [_row(r) for r in con.execute(sql, args)]
