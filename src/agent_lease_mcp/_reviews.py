"""Review jako stavový automat (fáze B3) s pevným stropem opravných kol (B4).

Část `Store`. Pravidla:

- Stavy: working → awaiting_review → (approved | changes_requested) → … ;
  escalated a closed jsou koncové pro agenty.
- Verdikt je vázaný na přesný commit. Verdikt na jiný commit než ten, který
  autor naposledy předložil, se odmítne; nový commit po schválení schválení ruší.
- Autor neschválí sám sebe. Je-li určen recenzent, verdikt dá jen on (nebo člověk).
- Autor sám zavře jen schválené nebo nepředložené review; jinak člověk. Tím
  close + nové review téhož tématu strop kol nevynuluje.
- Nejvýš `max_fix_cycles` (3) opravných kol. Žádost o změny po vyčerpání kol
  review rovnou eskaluje na člověka. Počítadlo je v SQLite, restart ho nevynuluje.
- Člověk jedná jen přes metody `review_human_*`/`review_grant_cycle`, které NEJSOU
  v METHODS brokeru (volá je web UI s přímým přístupem). Agentní metody jméno
  člověka odmítnou už v `_check_review_agent` (rezervované jméno), takže se za něj
  agent přes broker nevydá.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

from ._store_common import _check_agent
from .config import human_name, reserved_names
from .hygiene import check_path, check_short, check_text
from .policy import normalize_path

MAX_FIX_CYCLES = 3
STALE_REVIEW_SECONDS = 24 * 3600  # po té době bez verdiktu se ozve člověk

WORKING = "working"
AWAITING = "awaiting_review"
CHANGES = "changes_requested"
APPROVED = "approved"
ESCALATED = "escalated"
CLOSED = "closed"
REVIEW_STATES = (WORKING, AWAITING, CHANGES, APPROVED, ESCALATED, CLOSED)
OPEN_REVIEW_STATES = (WORKING, AWAITING, CHANGES, APPROVED, ESCALATED)

# Odkud smí autor předložit commit k review.
_SUBMITTABLE = frozenset({WORKING, AWAITING, CHANGES, APPROVED})
# Co smí autor sám zavřít: schválené (sloučeno) a rozpracované, které nikdo neviděl.
_AUTHOR_CLOSABLE = frozenset({WORKING, APPROVED})
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


SECURITY_VERDICTS = ("pass", "fail")


def check_findings(findings: list[dict] | None) -> str | None:
    """B6: výsledek secret scanu jako JSON pro DB; jen soubor, řádek a druh, žádné hodnoty."""
    if findings is None:
        return None
    if not isinstance(findings, list) or len(findings) > 50:
        raise ValueError("secret_findings must be a list of at most 50 findings")
    clean = []
    for item in findings:
        if not isinstance(item, dict):
            raise TypeError("each finding must be an object")
        kind = str(item.get("kind", ""))
        if not re.fullmatch(r"[a-z0-9-]{1,40}", kind):
            raise ValueError(f"invalid finding kind {kind[:40]!r}")
        clean.append({"file": check_path(str(item.get("file", ""))[:500]),
                      "line": int(item.get("line", 0)), "kind": kind})
    return json.dumps(clean, ensure_ascii=False)


def check_commit(label: str, value: str, *, optional: bool = False) -> str:
    """Plný hash commitu (SHA-1 i SHA-256). Zkratky a jména větví ne: musí být jednoznačné."""
    value = (value or "").strip().lower()
    if optional and not value:
        return ""
    if not _COMMIT.fullmatch(value):
        raise ValueError(f"{label} must be a full commit hash (40 or 64 hex), got {value[:80]!r}")
    return value


class ReviewsMixin:
    def _check_review_agent(self, agent: str) -> None:
        """Agentní strana review: platné jméno, ale nikdy člověk ani systém."""
        _check_agent(agent)
        if agent.lower() in reserved_names(self.db_path):
            raise PermissionError(f"{agent!r} is reserved; human review actions go through the web UI")

    @staticmethod
    def _review_row(con: sqlite3.Connection, review_id: int) -> sqlite3.Row:
        row = con.execute("SELECT * FROM reviews WHERE id=?", (int(review_id),)).fetchone()
        if not row:
            raise KeyError(f"review #{review_id} does not exist")
        return row

    @staticmethod
    def _review_event(con: sqlite3.Connection, review_id: int, actor: str, event: str,
                      from_state: str, to_state: str, commit: str = "", note: str = "") -> None:
        con.execute(
            "INSERT INTO review_events(review_id, at, actor, event, from_state, to_state, commit_sha, note) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (review_id, time.time(), actor, event, from_state, to_state, commit, note),
        )

    @staticmethod
    def _review_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["cycles_left"] = max(0, data["max_fix_cycles"] - data["fix_cycles"])
        raw = data.pop("scan_findings", None)
        # B6: None = commit nebyl zkontrolován; [] = čistý; jinak nálezy (bez hodnot)
        data["secret_findings"] = (json.loads(raw) if raw and data.get("scan_commit") == data["head_commit"]
                                   else None)
        return data

    def _review_notify(self, sender: str, recipient: str, text: str) -> None:
        """Vzkaz o změně stavu. Best effort: stav review je zapsaný už předtím."""
        if not recipient or recipient == sender:
            return
        try:
            self.send(sender, recipient, text, kind="chat")
        except (ValueError, PermissionError, sqlite3.Error):
            pass

    def review_stale_sweep(self, now: float | None = None) -> int:
        """
        Review čekající na recenzenta déle než `STALE_REVIEW_SECONDS` ohlásí člověku,
        jednou za předložení. Vzkaz recenzentovi je jen `chat` (bridge ho neprovede,
        po 48 h vyprší), takže bez tohohle review tiše visí (#9 od 7. 11.).
        Volá ho sweeper brokeru; nové předložení (`updated_at`) hodiny nuluje.
        """
        now = time.time() if now is None else now
        human = human_name(self.db_path)
        sent = 0
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            stale = con.execute(
                "SELECT * FROM reviews r WHERE state=? AND updated_at < ? AND NOT EXISTS ("
                "SELECT 1 FROM review_events e WHERE e.review_id=r.id AND e.event='stale-notice' "
                "AND e.at >= r.updated_at)",
                (AWAITING, now - STALE_REVIEW_SECONDS),
            ).fetchall()
            for r in stale:
                hours = int((now - r["updated_at"]) // 3600)
                self._insert_event(
                    con, "broker", human,
                    f"Review #{r['id']} čeká na {r['reviewer'] or 'recenzenta'} už {hours} h "
                    f"a nikdo ho nevzal: {r['topic']} @ {r['head_commit'][:12]}", None, now)
                self._review_event(con, r["id"], "broker", "stale-notice", AWAITING, AWAITING,
                                   r["head_commit"], f"{hours} h")
                sent += 1
        return sent

    def review_open(self, author: str, repo: str, topic: str, reviewer: str = "",
                    base_commit: str = "", head_commit: str = "",
                    secret_findings: list[dict] | None = None) -> dict:
        """Založí review. S `head_commit` rovnou čeká na recenzenta, jinak je ve stavu working."""
        self._check_review_agent(author)
        if reviewer:
            self._check_review_agent(reviewer)
            if reviewer == author:
                raise PermissionError("the author cannot be the reviewer")
        repo = normalize_path(check_path(repo))
        topic = check_short("topic", topic).strip()
        if not topic:
            raise ValueError("topic must not be empty")
        base = check_commit("base_commit", base_commit, optional=True)
        head = check_commit("head_commit", head_commit, optional=True)
        state = AWAITING if head else WORKING
        scan = check_findings(secret_findings) if head else None
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            busy = con.execute(
                f"SELECT id FROM reviews WHERE repo=? AND topic=? AND state IN ({','.join('?' * len(OPEN_REVIEW_STATES))})",
                (repo, topic, *OPEN_REVIEW_STATES),
            ).fetchone()
            if busy:
                raise ValueError(f"review #{busy['id']} for this topic is still open")
            cur = con.execute(
                "INSERT INTO reviews(repo, topic, author, reviewer, state, base_commit, head_commit, "
                "fix_cycles, max_fix_cycles, created_at, updated_at, scan_commit, scan_findings) "
                "VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?)",
                (repo, topic, author, reviewer, state, base, head, MAX_FIX_CYCLES, now, now,
                 head if scan is not None else None, scan),
            )
            review_id = cur.lastrowid
            self._review_event(con, review_id, author, "open", "", state, head)
            self._record(con, author, "review-open", repo, f"#{review_id} {topic}")
            row = self._review_row(con, review_id)
        if head:
            self._review_notify(author, reviewer, f"Review #{review_id} čeká na tebe: {topic} @ {head[:12]}")
            self._quality_auto_request(author, repo, head, base)
        return self._review_dict(row)

    def review_submit(self, author: str, review_id: int, head_commit: str, base_commit: str = "",
                      note: str = "", secret_findings: list[dict] | None = None) -> dict:
        """
        Autor předloží commit k review. Po žádosti o změny je to další opravné kolo;
        nový commit po schválení schválení ruší (starý verdikt neplatí pro nový diff).
        """
        self._check_review_agent(author)
        head = check_commit("head_commit", head_commit)
        base = check_commit("base_commit", base_commit, optional=True)
        note = check_short("note", note)
        scan = check_findings(secret_findings)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._review_row(con, review_id)
            if row["author"] != author:
                raise PermissionError(f"only the author ({row['author']}) can submit to review #{review_id}")
            state = row["state"]
            if state == ESCALATED:
                raise PermissionError(
                    f"review #{review_id} used all {row['max_fix_cycles']} fix cycles and waits for the human")
            if state not in _SUBMITTABLE:
                raise ValueError(f"review #{review_id} is {state}; nothing can be submitted")
            if base and base != row["base_commit"]:
                # B6: základ určuje rozsah kontroly; po založení ho autor nesmí posunout
                raise ValueError(f"review #{review_id} has base {row['base_commit'][:12]}; "
                                 "the base cannot change after the review is opened")
            if state in (APPROVED, AWAITING) and head == row["head_commit"]:
                return self._review_dict(row)  # stejný commit znovu: nic se nemění
            cycles = row["fix_cycles"] + (1 if state == CHANGES else 0)
            con.execute(
                "UPDATE reviews SET state=?, head_commit=?, base_commit=CASE WHEN ?='' THEN base_commit ELSE ? END, "
                "fix_cycles=?, verdict=NULL, verdict_by=NULL, verdict_commit=NULL, verdict_note=NULL, "
                "verdict_at=NULL, security=NULL, security_by=NULL, scan_commit=?, scan_findings=?, "
                "updated_at=? WHERE id=?",
                (AWAITING, head, base, base, cycles, head if scan is not None else None, scan,
                 time.time(), review_id),
            )
            event = "fix" if state == CHANGES else ("resubmit-after-approval" if state == APPROVED else "submit")
            self._review_event(con, review_id, author, event, state, AWAITING, head, note)
            self._record(con, author, "review-submit", row["repo"], f"#{review_id} {head[:12]} {event}")
            row = self._review_row(con, review_id)
        label = f"kolo oprav {cycles}/{row['max_fix_cycles']}" if cycles else "nové"
        self._review_notify(author, row["reviewer"],
                            f"Review #{review_id} čeká na tebe ({label}): {row['topic']} @ {head[:12]}")
        self._quality_auto_request(author, row["repo"], head, row["base_commit"])
        return self._review_dict(row)

    def review_verdict(self, reviewer: str, review_id: int, verdict: str, commit: str,
                       note: str = "", security: str = "",
                       reviewer_findings: list[dict] | None = None) -> dict:
        """
        Verdikt `approved` nebo `changes_requested` přesně na předložený commit.
        B6: `approved` vyžaduje výslovné `security="pass"` recenzenta a čistý secret scan
        právě tohoto commitu; nezkontrolovaný nebo nálezový commit schválit nejde.
        Scan musí být čistý dvakrát: u autora (při předložení) i nezávisle u recenzenta
        (`reviewer_findings`, spočítá ho recenzentův nástroj), aby ho autor nemohl podvrhnout.
        """
        self._check_review_agent(reviewer)
        commit = check_commit("commit", commit)
        note = check_text(note)
        if verdict not in (APPROVED, CHANGES):
            raise ValueError("verdict must be 'approved' or 'changes_requested'")
        if verdict == CHANGES and not note.strip():
            raise ValueError("changes_requested needs a note saying what to change")
        if security and security not in SECURITY_VERDICTS:
            raise ValueError("security must be 'pass' or 'fail'")
        if verdict == APPROVED and security != "pass":
            raise PermissionError("approval needs your security verdict: security='pass' "
                                  "(no secrets, no unsafe change in this diff)")
        own_scan = check_findings(reviewer_findings)
        if verdict == APPROVED and own_scan is None:
            raise PermissionError("approval needs the reviewer's own secret scan of the diff")
        if verdict == APPROVED and own_scan != "[]":
            raise PermissionError(f"your secret scan found {len(json.loads(own_scan))} possible "
                                  "secret(s); it cannot be approved")
        human = human_name(self.db_path)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._review_row(con, review_id)
            if row["author"] == reviewer:
                raise PermissionError("the author cannot review their own work")
            if row["reviewer"] and reviewer != row["reviewer"]:
                raise PermissionError(f"review #{review_id} is assigned to {row['reviewer']}")
            if row["state"] != AWAITING:
                raise ValueError(f"review #{review_id} is {row['state']}, not awaiting_review")
            if commit != row["head_commit"]:
                raise ValueError(
                    f"stale verdict: review #{review_id} is at {row['head_commit'][:12]}, "
                    f"not {commit[:12]}; review the current commit")
            if verdict == APPROVED:
                if row["scan_commit"] != commit or row["scan_findings"] is None:
                    raise PermissionError(f"commit {commit[:12]} has no secret scan; the author must "
                                          "submit it again with the current tools")
                found = json.loads(row["scan_findings"])
                if found:
                    raise PermissionError(f"secret scan found {len(found)} possible secret(s) in "
                                          f"{commit[:12]}; it cannot be approved")
            new_state = verdict
            if verdict == CHANGES and row["fix_cycles"] >= row["max_fix_cycles"]:
                new_state = ESCALATED  # B4: další kolo by bylo nad strop → člověk
            con.execute(
                "UPDATE reviews SET state=?, verdict=?, verdict_by=?, verdict_commit=?, verdict_note=?, "
                "verdict_at=?, security=?, security_by=?, updated_at=? WHERE id=?",
                (new_state, verdict, reviewer, commit, note, time.time(), security or None,
                 reviewer if security else None, time.time(), review_id),
            )
            self._review_event(con, review_id, reviewer, verdict, AWAITING, new_state, commit, note[:500])
            self._record(con, reviewer, "review-verdict", row["repo"], f"#{review_id} {verdict} {commit[:12]}")
            row = self._review_row(con, review_id)
        texts = {
            APPROVED: f"Review #{review_id} SCHVÁLENO ({reviewer}) @ {commit[:12]}: {row['topic']}",
            CHANGES: f"Review #{review_id}: požadované změny ({reviewer}), zbývá kol {row['max_fix_cycles'] - row['fix_cycles']}: {note[:300]}",
            ESCALATED: f"Review #{review_id} ESKALOVÁNO na člověka: vyčerpána {row['max_fix_cycles']} opravná kola. Poslední výhrady: {note[:300]}",
        }
        self._review_notify(reviewer, row["author"], texts[new_state])
        if new_state == ESCALATED:
            self._review_notify(reviewer, human, texts[ESCALATED])
        return self._review_dict(row)

    def review_close(self, actor: str, review_id: int, note: str = "") -> dict:
        """
        Autor uzavře schválené review (sloučeno) nebo rozpracované, které ještě nikdo
        neviděl. Rozběhnuté review (čeká, požadované změny, eskalace) zavře jen člověk:
        jinak by close + nové review stejného tématu vynulovalo strop kol (Nox #655/P1).
        """
        self._check_review_agent(actor)
        return self._close_review(actor, review_id, note, human=False)

    def review_human_close(self, human: str, review_id: int, note: str = "") -> dict:
        """Člověk uzavře jakékoli review. NENÍ v METHODS brokeru."""
        return self._close_review(human, review_id, note, human=True)

    def _close_review(self, actor: str, review_id: int, note: str, *, human: bool) -> dict:
        note = check_short("note", note)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._review_row(con, review_id)
            if not human and actor != row["author"]:
                raise PermissionError(f"only the author ({row['author']}) or the human can close review #{review_id}")
            if row["state"] == CLOSED:
                return self._review_dict(row)
            if not human and row["state"] not in _AUTHOR_CLOSABLE:
                raise PermissionError(
                    f"review #{review_id} is {row['state']}; the author can close only an approved or "
                    "unsubmitted review, anything else goes to the human")
            con.execute("UPDATE reviews SET state=?, updated_at=? WHERE id=?", (CLOSED, time.time(), review_id))
            self._review_event(con, review_id, actor, "close", row["state"], CLOSED, row["head_commit"], note)
            self._record(con, actor, "review-close", row["repo"], f"#{review_id} {note}")
            return self._review_dict(self._review_row(con, review_id))

    def review_grant_cycle(self, human: str, review_id: int, reason: str) -> dict:
        """
        Člověk povolí jedno další opravné kolo eskalovanému review. NENÍ v METHODS
        brokeru: agent si strop sám nezvedne.
        """
        reason = check_short("reason", reason).strip()
        if not reason:
            raise ValueError("a reason is required")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._review_row(con, review_id)
            if row["state"] != ESCALATED:
                raise ValueError(f"review #{review_id} is {row['state']}, not escalated")
            con.execute(
                "UPDATE reviews SET state=?, max_fix_cycles=max_fix_cycles+1, updated_at=? WHERE id=?",
                (CHANGES, time.time(), review_id),
            )
            self._review_event(con, review_id, human, "grant-cycle", ESCALATED, CHANGES, row["head_commit"], reason)
            self._record(con, human, "review-grant-cycle", row["repo"], f"#{review_id} {reason}")
            row = self._review_row(con, review_id)
        self._review_notify(human, row["author"],
                            f"Review #{review_id}: člověk povolil ještě jedno kolo oprav ({reason})")
        return self._review_dict(row)

    def review_get(self, review_id: int, *, events: bool = True) -> dict:
        with self._connect() as con:
            data = self._review_dict(self._review_row(con, review_id))
            if events:
                data["events"] = [dict(r) for r in con.execute(
                    "SELECT at, actor, event, from_state, to_state, commit_sha, note FROM review_events "
                    "WHERE review_id=? ORDER BY id", (int(review_id),))]
        return data

    def reviews(self, state: str | None = None, repo: str | None = None, *,
                include_closed: bool = False, limit: int = 50) -> list[dict]:
        where, args = [], []
        if state:
            if state not in REVIEW_STATES:
                raise ValueError(f"unknown review state: {state}")
            where.append("state=?")
            args.append(state)
        elif not include_closed:
            where.append("state<>?")
            args.append(CLOSED)
        if repo:
            where.append("repo=?")
            args.append(normalize_path(check_path(repo)))
        sql = "SELECT * FROM reviews" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._connect() as con:
            return [self._review_dict(r) for r in con.execute(sql, args)]

    def review_approved(self, repo: str, commit: str) -> dict | None:
        """Je přesně tento commit schválený? Vrátí schvalující review, jinak None."""
        repo = normalize_path(check_path(repo))
        commit = check_commit("commit", commit)
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM reviews WHERE repo=? AND state IN (?,?) AND head_commit=? "
                "AND verdict=? AND verdict_commit=? AND security='pass' "
                "ORDER BY updated_at DESC LIMIT 1",
                (repo, APPROVED, CLOSED, commit, APPROVED, commit),
            ).fetchone()
        return self._review_dict(row) if row else None
