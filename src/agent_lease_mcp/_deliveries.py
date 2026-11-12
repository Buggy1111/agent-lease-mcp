"""Fronta úkolů: aktivace workerů, lease, ack, retry, cancel.

Část `Store`; rozděleno ze store.py podle rozboru Noxe (#516).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from ._store_common import (
    _check_agent,
)
from .config import (
    MAX_TASK_LEASE_SECONDS,
    STALE_PEER_SECONDS,
    human_name,
)
from .hygiene import check_short, check_text, heuristic_risk, redact
from .launch_gate import launch_gate
from .models import Delivery, DeliveryState, MessageKind
from .states import (
    ACK_TRANSITIONS,
    CHECKPOINT_HOLD,
    HELD,
    QUEUED,
    RETRYABLE,
    TERMINAL,
    UNCANCELLABLE,
    sql_in,
)
from .workspace import matches as workspace_matches


class DeliveriesMixin:
    @staticmethod
    def _final_hashes(token: str, method: str, state: str, result: str, error: str) -> tuple[str, str]:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        payload = json.dumps([method, state, result, error], ensure_ascii=False).encode()
        return token_hash, hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _same_completion(row: sqlite3.Row, hashes: tuple[str, str]) -> bool:
        token_hash, payload_hash = row["final_token_hash"], row["final_payload_hash"]
        return bool(token_hash and payload_hash and hmac.compare_digest(token_hash, hashes[0])
                    and hmac.compare_digest(payload_hash, hashes[1]))

    def worker_activation(self, agent: str, *, include_existing: bool = False) -> int:
        """Create once and return the durable lower task-id bound for a worker."""
        return self._activate("worker_activations", "worker-activate", agent, include_existing)

    # Jen tyto dvě tabulky; jméno tabulky jde do SQL, proto pevný whitelist.
    _ACTIVATION_TABLES = frozenset({"failover_activations", "worker_activations"})

    def _activate(self, table: str, action: str, agent: str, include_existing: bool) -> int:
        """Společné jádro worker/failover aktivace (Nox, rozbor #516)."""
        if table not in self._ACTIVATION_TABLES:
            raise ValueError(f"unknown activation table: {table}")
        _check_agent(agent)
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                f"SELECT after_message_id FROM {table} WHERE agent=?", (agent,)
            ).fetchone()
            if row:
                return int(row["after_message_id"])
            after = 0
            if not include_existing:
                after = int(con.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages"
                ).fetchone()[0])
            con.execute(
                f"INSERT INTO {table}(agent, after_message_id, activated_at) VALUES(?,?,?)",
                (agent, after, now),
            )
            self._record(con, agent, action, "", f"after #{after}")
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

    @staticmethod
    def _expire_leases(con: sqlite3.Connection, now: float, recipient: str | None = None) -> int:
        """
        Prošlé lease a termíny: `leased` → `pending` (nic nespuštěno), `started` →
        `needs_review` (nevíme, co proběhlo — nikdy tiché opakování), prošlý
        `expires_at` → `expired`. Bez `recipient` pro všechny (reconciliace brokeru).
        """
        who, args = ("", ()) if recipient is None else (" AND recipient=?", (recipient,))
        changed = con.execute(
            "UPDATE deliveries SET state='expired', lease_token=NULL, lease_until=NULL, "
            f"updated_at=? WHERE state='pending'{who} AND message_id IN "
            "(SELECT id FROM messages WHERE expires_at IS NOT NULL AND expires_at<=?)",
            (now, *args, now),
        ).rowcount
        changed += con.execute(
            "UPDATE deliveries SET state='needs_review', lease_token=NULL, lease_until=NULL, "
            "last_error='worker lease expired after start', updated_at=? "
            f"WHERE state='started'{who} AND lease_until<=?",
            (now, *args, now),
        ).rowcount
        changed += con.execute(
            "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
            f"updated_at=? WHERE state='leased'{who} AND lease_until<=?",
            (now, *args, now),
        ).rowcount
        return changed

    def reconcile(self) -> int:
        """
        Pravidelný úklid fronty pro VŠECHNY adresáty (volá broker sweeper).
        Dřív se prošlé lease uklízely jen v `lease_next` adresáta — agent v limitu
        tak nechal přerušený `started` viset (Nox, audit #535 bod 1).
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            changed = self._expire_leases(con, now)
            self._prune(con, now)  # B1: úklid prošlých nájmů i bez nového claim
            return changed

    def lease_next(
        self, recipient: str, *, lease_seconds: int = 60, after_message_id: int = 0,
        skip_accepted: bool = False, defer_if_present: float = 0, owner: str | None = None,
        start: bool = False, workspace: Sequence[str] | None = None,
    ) -> Delivery | None:
        """
        Atomicky převezme nejstarší připravený task pro jeden worker.

        Pro autonomní worker (bridge), který sdílí frontu s interaktivní session
        stejného jména:
        - `skip_accepted` — task, který adresát už vědomě přijal (`accept`), patří
          jemu; worker ho nesmí vzít taky (jinak by proběhl dvakrát).
        - `defer_if_present` — je-li adresát přítomný (heartbeat), nechá worker
          task tolik sekund jemu a převezme ho až potom.
        - `start=True` vydá úkol rovnou jako started; navazuje-li na checkpoint
          minulého pokusu, musí volající doložit shodný `workspace` (git HEAD,
          otisk změn), jinak úkol jde k člověku (A6 jako invariant úložiště).
        """
        lease_seconds = max(1, min(int(lease_seconds), MAX_TASK_LEASE_SECONDS))  # APP-009
        after_message_id = max(0, int(after_message_id))
        token = uuid.uuid4().hex
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()  # lock wait must not shorten a new lease or revive expired tasks
            self._expire_leases(con, now, recipient)
            self._prune(con, now)  # B1: prošlý cizí nájem nesmí úkol blokovat
            # nouzové zastavení se kontroluje v TÉŽE transakci jako převzetí (A10)
            if con.execute("SELECT 1 FROM control WHERE key='paused' AND value='1'").fetchone():
                return None
            newest = None  # None = bez odkladu
            if defer_if_present > 0:
                seen = con.execute(
                    "SELECT seen_at FROM agents WHERE agent=?", (recipient,)
                ).fetchone()
                if seen and now - seen["seen_at"] <= STALE_PEER_SECONDS:
                    newest = now - float(defer_if_present)
            # C3b: denní strop agenta – nic nového nezačne, úkoly počkají ve frontě.
            # Běžící pokus se nezabíjí; strop se uplatní až před dalším startem.
            if self._daily_over(con, recipient, now):
                return None
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
                # interaktivní session nedostane úkol převzatý jinou session (audit #535 2a)
                "AND (? IS NULL OR r.accepted_at IS NULL OR r.by_lease = 1 OR r.owner = ?) "
                "ORDER BY COALESCE(tm.priority, 0) DESC, m.id",
                (recipient, after_message_id, now, now, newest, newest, int(bool(skip_accepted)),
                 owner, owner),
            ).fetchall()
            row = None
            held = self._held(con) if rows else []  # jeden snímek pro všechny kandidáty
            for candidate in rows:
                paths = [r["path"] for r in con.execute(
                    "SELECT path FROM task_paths WHERE message_id=? ORDER BY path",
                    (candidate["id"],),
                ).fetchall()]
                # B1: cizí nájem i rozběhnutý úkol nad stejnými cestami → zatím ne.
                # Kontrola i převzetí jsou v jedné transakci, takže vyhraje jen jeden.
                if paths and self._conflicts(con, paths, recipient, task_id=candidate["id"],
                                             held=held):
                    continue
                over = self._task_over(con, candidate["id"], recipient)
                if over:
                    # C3b: úkol vyčerpal svůj strop – další pokus jen po rozhodnutí člověka
                    con.execute(
                        "UPDATE deliveries SET state='needs_review', last_error=?, updated_at=? "
                        "WHERE message_id=? AND recipient=? AND state='pending'",
                        (over, now, candidate["id"], recipient),
                    )
                    self._insert_event(con, "broker", candidate["agent"],
                                       f"Task #{candidate['id']} not started again: {over}.",
                                       candidate["id"], now)
                    continue
                risk = self._quarantined(con, candidate["id"], recipient)
                if risk:
                    # karanténa: nespustit, rovnou k člověku (všechny cesty, Nox #555/3)
                    con.execute(
                        "UPDATE deliveries SET state='needs_review', last_error=?, updated_at=? "
                        "WHERE message_id=? AND recipient=? AND state='pending'",
                        (f"quarantine: risk {risk:.0%}, not started", now, candidate["id"],
                         recipient),
                    )
                    self._insert_event(con, "broker", candidate["agent"],
                                       f"Task #{candidate['id']} held back (risk {risk:.0%}); "
                                       "not started, waiting for the human to review.",
                                       candidate["id"], now)
                    continue
                if start and self._checkpoint_hold(con, candidate["id"], recipient,
                                                   candidate["attempts"] + 1, workspace,
                                                   candidate["agent"], now):
                    continue
                row = candidate
                break
            if row is None:
                return None
            # start=True (interaktivní next_task): rovnou started — kdo úkol dostane,
            # může ho začít dělat, a pád pak nesmí úkol vrátit do fronty (Nox #555/2)
            con.execute(
                "UPDATE deliveries SET state=?, attempts=attempts+1, "
                "lease_token=?, lease_until=?, updated_at=? "
                "WHERE message_id=? AND recipient=? AND state='pending'",
                ("started" if start else "leased", token, now + max(1, lease_seconds), now,
                 row["id"], recipient),
            )
            return Delivery(
                message_id=row["id"], sender=row["agent"], recipient=recipient,
                kind=MessageKind(row["kind"]), text=row["text"],
                state=DeliveryState.STARTED if start else DeliveryState.LEASED,
                attempts=row["attempts"] + 1,
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
        workspace: Sequence[str] | None = None,
    ) -> bool:
        """
        Potvrdí pouze platný přechod vlastněného pracovního lease.

        Přechod do `started` u úkolu s checkpointem minulého pokusu vyžaduje shodný
        `workspace` (nebo lidské schválení); jinak úkol atomicky skončí v
        needs_review a nic se nespustí (A6, Nox #566/P1).
        """
        state_value = DeliveryState(state).value
        terminal, allowed = TERMINAL, ACK_TRANSITIONS
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT state, lease_token, lease_until, final_token_hash, final_payload_hash "
                "FROM deliveries "
                "WHERE message_id=? AND recipient=?",
                (message_id, recipient),
            ).fetchone()
            if not row:
                return False
            current = row["state"]
            # Prošlý lease už nepatří volajícímu: nesmí ho oživit ani dokončit, i když
            # ho úklid ještě nestihl přepnout (Nox, audit #535 bod 2b).
            if (current in HELD and row["lease_until"] is not None
                    and row["lease_until"] <= time.time()):
                return False
            # Opakované potvrzení téhož finálního výsledku je bezpečně idempotentní.
            hashes = self._final_hashes(lease_token or "", "ack", state_value, "", error)
            if current in terminal and state_value == current:
                return bool(lease_token and self._same_completion(row, hashes))
            if state_value not in allowed.get(current, set()):
                return False
            # během nouzového zastavení nic nového nezačne (#550/1)
            if state_value == "started" and con.execute(
                    "SELECT 1 FROM control WHERE key='paused' AND value='1'").fetchone():
                return False
            if not lease_token or row["lease_token"] != lease_token:
                return False
            if state_value == "started":
                att = con.execute("SELECT d.attempts, m.agent FROM deliveries d JOIN messages m "
                                  "ON m.id=d.message_id WHERE d.message_id=? AND d.recipient=?",
                                  (message_id, recipient)).fetchone()
                if self._checkpoint_hold(con, message_id, recipient, att["attempts"], workspace,
                                         att["agent"], time.time(), held_state=current):
                    return False
            clear_lease = state_value in terminal
            cur = con.execute(
                "UPDATE deliveries SET state=?, last_error=?, updated_at=?, "
                "final_token_hash=CASE WHEN ? THEN ? ELSE NULL END, "
                "final_payload_hash=CASE WHEN ? THEN ? ELSE NULL END, "
                "lease_token=CASE WHEN ? THEN NULL ELSE lease_token END, "
                "lease_until=CASE WHEN ? THEN NULL ELSE lease_until END "
                "WHERE message_id=? AND recipient=? AND state=?",
                (state_value, error, time.time(), clear_lease, hashes[0], clear_lease, hashes[1],
                 clear_lease, clear_lease,
                 message_id, recipient, current),
            )
        return bool(cur.rowcount)

    def complete(self, message_id: int, recipient: str, lease_token: str,
                 state: DeliveryState | str, result_text: str, *, error: str = "") -> bool:
        """
        Dokončení úkolu ATOMICKY: přechod stavu + zpráva s výsledkem v jedné transakci,
        jen s platným (neprošlým) lease tokenem. Dřív se „Done“ posílalo před `ack`
        a výsledek ack se ignoroval → po zrušení přišlo Done, pád mezi tím nechal
        rozpor (audit #535 bod 2d). Opakované volání se stejným výsledkem je no-op.
        """
        state_value = DeliveryState(state).value
        if state_value not in TERMINAL:
            raise ValueError("complete() needs a final state")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()  # až po zámku (Nox #545/6)
            row = con.execute(
                "SELECT d.state, d.lease_token, d.lease_until, d.final_token_hash, "
                "d.final_payload_hash, m.agent AS sender "
                "FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "WHERE d.message_id=? AND d.recipient=?", (message_id, recipient),
            ).fetchone()
            if not row:
                return False
            hashes = self._final_hashes(lease_token or "", "complete", state_value,
                                        result_text, error)
            if row["state"] == state_value and row["lease_token"] is None:
                return bool(lease_token and self._same_completion(row, hashes))
            if (row["state"] not in ACK_TRANSITIONS or not lease_token
                    or row["lease_token"] != lease_token
                    or (row["lease_until"] is not None and row["lease_until"] <= now)
                    or state_value not in ACK_TRANSITIONS[row["state"]]):
                return False
            con.execute(
                "UPDATE deliveries SET state=?, last_error=?, updated_at=?, "
                "final_token_hash=?, final_payload_hash=?, lease_token=NULL, "
                "lease_until=NULL WHERE message_id=? AND recipient=?",
                (state_value, error, now, hashes[0], hashes[1], message_id, recipient),
            )
            con.execute(
                "INSERT INTO messages(agent, text, sent_at, recipient, kind, reply_to) "
                "VALUES(?,?,?,?,?,?)",
                (recipient, result_text, now, row["sender"], MessageKind.RESULT.value, message_id),
            )
        self._signal(row["sender"])
        return True

    # ── nouzové zastavení (fáze A10) ─────────────────────────────────────────

    def paused(self) -> dict | None:
        """Je autonomní provádění pozastavené? Vrací kdo/kdy/proč, jinak None."""
        with self._connect() as con:
            row = con.execute(
                "SELECT value, changed_by, changed_at, reason FROM control WHERE key='paused'"
            ).fetchone()
        if not row or row["value"] != "1":
            return None
        return {"by": row["changed_by"], "at": row["changed_at"], "reason": row["reason"]}

    def pause(self, by: str, reason: str = "") -> None:
        """Zapsat pauzu a potvrdit ji až po synchronizaci rozbíhaných CLI příkazů.

        Běžící workery dostanou pokyn skončit; úplný úklid potomků a volatelných
        runnerů není zaručen. Smí kdokoli — zastavení je bezpečný směr.
        """
        reason = check_short("reason", reason)
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO control(key, value, changed_by, changed_at, reason) "
                "VALUES('paused','1',?,?,?) ON CONFLICT(key) DO UPDATE SET value='1', "
                "changed_by=excluded.changed_by, changed_at=excluded.changed_at, "
                "reason=excluded.reason", (by, now, reason),
            )
            self._record(con, by, "pause", "", reason)
        # Publish the pause first, then wait for any launch already in flight.
        # If a launcher is stuck, report failure instead of falsely confirming it.
        with launch_gate(self.db_path):
            if not self.paused():
                raise RuntimeError("pause was resumed before it could be confirmed")

    def resume(self, by: str, reason: str = "") -> None:
        """Obnovit provádění. NENÍ v METHODS brokeru: jen člověk (web UI / přímý přístup)."""
        reason = check_short("reason", reason)
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO control(key, value, changed_by, changed_at, reason) "
                "VALUES('paused','0',?,?,?) ON CONFLICT(key) DO UPDATE SET value='0', "
                "changed_by=excluded.changed_by, changed_at=excluded.changed_at, "
                "reason=excluded.reason", (by, now, reason),
            )
            self._record(con, by, "resume", "", reason)

    # ── checkpoint (fáze A6) ─────────────────────────────────────────────────

    def _checkpoint_hold(self, con: sqlite3.Connection, message_id: int, recipient: str,
                         attempt: int, workspace: Sequence[str] | None, sender: str,
                         now: float, *, held_state: str = "pending") -> bool:
        """
        Smí pokus `attempt` začít? Má-li úkol checkpoint z dřívějšího pokusu, start
        projde jen se shodným otiskem pracovní kopie, nebo s lidským schválením právě
        pro tento pokus. Jinak úkol v téže transakci přejde do needs_review (žádný
        token, žádný start) a vrátí True.
        """
        cp = con.execute(
            "SELECT attempt, git_head, dirty, resume_approved_at, resume_approved_attempt "
            "FROM checkpoints WHERE message_id=? AND recipient=?", (message_id, recipient),
        ).fetchone()
        if not cp or cp["attempt"] >= attempt:
            return False
        if cp["resume_approved_at"] is not None and attempt == (cp["resume_approved_attempt"] or 0) + 1:
            return False
        ws = list(workspace) if workspace is not None else []
        if len(ws) == 2 and workspace_matches(dict(cp), str(ws[0]), str(ws[1])):
            return False
        con.execute(
            "UPDATE deliveries SET state='needs_review', last_error=?, updated_at=?, "
            "lease_token=NULL, lease_until=NULL "
            "WHERE message_id=? AND recipient=? AND state=?",
            (CHECKPOINT_HOLD, now, message_id, recipient, held_state),
        )
        self._insert_event(con, "broker", sender,
                           f"Task #{message_id} not started: the workspace changed since the "
                           f"checkpoint of attempt {cp['attempt']} (or cannot be verified). "
                           "Restore it and retry, or approve resuming in the web UI.",
                           message_id, now)
        return True

    def checkpoint(self, message_id: int, recipient: str, lease_token: str, step_done: str,
                   next_step: str = "", git_head: str = "", dirty: str = "",
                   evidence: str = "") -> bool:
        """
        Uloží postup úkolu. Jen vlastník platného lease ve stavu started (cizí/starý
        token ani dosud nespuštěný pokus nepřepíše otisk minulého pokusu, Nox #566/P1).
        """
        for label, value in (("step_done", step_done), ("next_step", next_step),
                             ("evidence", evidence)):
            check_short(label, value) if label != "evidence" else check_text(value)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()  # až po zámku (Nox #545/6)
            row = con.execute(
                "SELECT attempts FROM deliveries WHERE message_id=? AND recipient=? "
                "AND lease_token=? AND state='started' AND lease_until>?",
                (message_id, recipient, lease_token, now),
            ).fetchone()
            if not row:
                return False
            con.execute(
                "INSERT INTO checkpoints(message_id, recipient, attempt, step_done, next_step, "
                "git_head, dirty, evidence, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(message_id, recipient) DO UPDATE SET attempt=excluded.attempt, "
                "step_done=excluded.step_done, next_step=excluded.next_step, "
                "git_head=excluded.git_head, dirty=excluded.dirty, evidence=excluded.evidence, "
                "updated_at=excluded.updated_at",
                (message_id, recipient, row["attempts"], redact(step_done), redact(next_step),
                 git_head, dirty, redact(evidence), now),
            )
        return True

    def get_checkpoint(self, message_id: int, recipient: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM checkpoints WHERE message_id=? AND recipient=?",
                (message_id, recipient),
            ).fetchone()
        return dict(row) if row else None

    # ── ověřená záloha databáze (náhrada za opravenou SQLite, fáze A8) ────────

    def backup(self, directory: Path | None = None, keep: int = 7) -> dict:
        """
        Konzistentní kopie DB přes SQLite backup API + `integrity_check`. Nasazená
        SQLite 3.50.4 má známou chybu WAL-reset a opravená verze zatím není pro
        Python k dispozici; ověřená denní záloha dává cestu zpět po poškození.
        """
        directory = directory or (self.db_path.parent / "backups")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:6]}"
        tmp = directory / f".room-{stamp}.tmp"
        target = directory / f"room-{stamp}.db"
        with self._connect() as con:
            ok = self._integrity(con)
            dst = sqlite3.connect(tmp)
            try:
                con.backup(dst)
                copy_ok = self._integrity(dst)
            finally:
                dst.close()
        tmp.chmod(0o600)
        if ok != "ok" or copy_ok != "ok":
            # neplatná kopie se nezveřejní a nevytlačí poslední platné (#550/7)
            bad = directory / f"invalid-{stamp}.db"
            tmp.rename(bad)
            return {"path": str(bad), "integrity": ok, "backup_integrity": copy_ok}
        tmp.rename(target)                     # atomické zveřejnění
        # Jedna (nejnovější) záloha na den, `keep` dní. Počítat soubory nestačí:
        # sedm restartů brokeru za den by smazalo všechny starší dny.
        newest_per_day: dict[str, Path] = {}
        # jméno = room-YYYYMMDD-HHMMSS-<náhodný hex>; ve stejné sekundě rozhoduje
        # náhoda, proto právě zapsaná záloha jde mezi stejnými časy vždy poslední.
        for path in sorted(directory.glob("room-*.db"), key=lambda p: (p.name[5:20], p == target)):
            if (old := newest_per_day.get(path.name[5:13])) is not None:
                old.unlink(missing_ok=True)
            newest_per_day[path.name[5:13]] = path
        for day in sorted(newest_per_day)[:-keep]:
            newest_per_day[day].unlink(missing_ok=True)
        return {"path": str(target), "integrity": ok, "backup_integrity": copy_ok}

    def last_backup_at(self) -> float:
        """Čas poslední platné zálohy (0, když žádná není) — broker po restartu nezálohuje znovu."""
        try:
            return max((p.stat().st_mtime for p in (self.db_path.parent / "backups").glob("room-*.db")),
                       default=0.0)
        except OSError:
            return 0.0

    @staticmethod
    def _integrity(con: sqlite3.Connection) -> str:
        return con.execute("PRAGMA integrity_check").fetchone()[0]

    def backup_now(self) -> dict:
        """Záloha bez parametrů pro broker: pevný adresář a rotace (cesta nejde podstrčit)."""
        return self.backup()

    QUARANTINE_RISK = 0.7   # stejné jako jev.RISK_THRESHOLD

    def _quarantined(self, con: sqlite3.Connection, message_id: int, recipient: str,
                     *, next_attempt: bool = True) -> float:
        """
        Riziko úkolu, pokud je v karanténě a NENÍ schválený pro tento pokus; jinak 0.
        Riziko = max(uložený screening, deterministická heuristika); zprávy člověka
        a brokeru se neposuzují. Sdílí lease_next i accept (Nox #555/3).
        """
        row = con.execute(
            "SELECT m.agent, m.text, d.attempts, s.risk, s.approved_hash, "
            "s.approved_recipient, s.approved_attempt FROM messages m "
            "JOIN deliveries d ON d.message_id=m.id AND d.recipient=? "
            "LEFT JOIN screens s ON s.message_id=m.id WHERE m.id=?",
            (recipient, message_id),
        ).fetchone()
        if not row or row["agent"] in (human_name(self.db_path), "broker"):
            return 0.0
        risk = max(float(row["risk"] or 0.0), heuristic_risk(row["text"]))
        if risk < self.QUARANTINE_RISK:
            return 0.0
        attempt = row["attempts"] + (1 if next_attempt else 0)
        if (row["approved_hash"] == self._text_hash(row["text"])
                and row["approved_recipient"] == recipient
                and attempt == (row["approved_attempt"] or 0) + 1):
            return 0.0
        return risk

    def holds(self, message_id: int, recipient: str, lease_token: str) -> bool:
        """Levná kontrola vlastnictví: platný, neprošlý lease s tímto tokenem.
        Při nouzovém zastavení vrací False → bridge běh zastaví."""
        if self.paused():
            return False
        with self._connect() as con:
            row = con.execute(
                f"SELECT 1 FROM deliveries WHERE message_id=? AND recipient=? AND lease_token=? "
                f"AND state IN {sql_in(HELD)} AND lease_until>?",
                (message_id, recipient, lease_token, time.time()),
            ).fetchone()
        return row is not None

    def defer(self, message_id: int, recipient: str, lease_token: str, not_before: float,
              notice: str, *, error: str = "") -> bool:
        """
        Bezpečné odložení po limitu poskytovatele: `started`/`leased` → `pending`
        s `not_before` a zpráva odesílateli, ATOMICKY a jen s platným lease.
        Dřív `ack(FAILED)` + `retry()` zvlášť: po propadlém lease ack selhal, ale
        retry vrátil i `needs_review` úkol do fronty (Nox, review #540 bod 3).
        Volat jen pro běh bez vedlejších efektů (read-only).
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()  # až po zámku (Nox #545/6)
            row = con.execute(
                "SELECT d.state, d.lease_token, d.lease_until, m.agent AS sender "
                "FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "WHERE d.message_id=? AND d.recipient=?", (message_id, recipient),
            ).fetchone()
            if (not row or row["state"] not in HELD or row["lease_token"] != lease_token
                    or row["lease_until"] is None or row["lease_until"] <= now):
                return False
            con.execute(
                "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
                "last_error=?, updated_at=? WHERE message_id=? AND recipient=?",
                (error, now, message_id, recipient),
            )
            con.execute("UPDATE messages SET not_before=? WHERE id=?", (not_before, message_id))
            self._insert_event(con, recipient, row["sender"], notice, message_id, now)
        self._signal(row["sender"])
        return True

    @staticmethod
    def _text_hash(text: str) -> str:
        import hashlib
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def quarantine_approved(self, message_id: int, recipient: str, text: str) -> bool:
        """
        Schválil člověk právě tento text, pro tohoto adresáta a pro TENTO pokus?
        Schválení platí na jeden běh: pokus musí být první po schválení.
        """
        with self._connect() as con:
            row = con.execute(
                "SELECT s.approved_hash, s.approved_recipient, s.approved_attempt, d.attempts "
                "FROM screens s JOIN deliveries d ON d.message_id=s.message_id AND d.recipient=? "
                "WHERE s.message_id=? AND s.approved_at IS NOT NULL",
                (recipient, message_id),
            ).fetchone()
        return (bool(row) and row["approved_recipient"] == recipient
                and row["approved_hash"] == self._text_hash(text)
                and row["attempts"] == (row["approved_attempt"] or 0) + 1)

    def approve_quarantine(self, message_id: int, recipient: str, approver: str,
                           reason: str) -> bool:
        """
        LIDSKÉ schválení úkolu zadrženého karanténou + jeden nový pokus (Nox #540/6).
        Jen úkol, který se kvůli karanténě NESPUSTIL (`needs_review` s chybou
        „quarantine“), ne nejistý rozběhnutý běh. Schválení je vázané na přesný text
        (hash) a auditované. Není v METHODS brokeru: agent si ho dát nemůže, volá ho
        jen web UI s ověřenou lidskou session.
        """
        reason = check_short("reason", reason.strip()) or "approved"
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()  # až po zámku (Nox #545/6)
            row = con.execute(
                "SELECT d.state, d.last_error, d.attempts, m.text FROM deliveries d "
                "JOIN messages m ON m.id=d.message_id WHERE d.message_id=? AND d.recipient=?",
                (message_id, recipient),
            ).fetchone()
            if (not row or row["state"] != "needs_review"
                    or not (row["last_error"] or "").startswith("quarantine")):
                return False
            # řádek screeningu nemusí existovat (karanténa z heuristiky v lease_next)
            con.execute(
                "INSERT INTO screens(message_id, risk, actionable, at, approved_by, approved_at, "
                "approved_hash, approved_reason, approved_recipient, approved_attempt) "
                "VALUES(?,?,NULL,?,?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
                "approved_by=excluded.approved_by, approved_at=excluded.approved_at, "
                "approved_hash=excluded.approved_hash, approved_reason=excluded.approved_reason, "
                "approved_recipient=excluded.approved_recipient, "
                "approved_attempt=excluded.approved_attempt",
                (message_id, heuristic_risk(row["text"]), now, approver, now,
                 self._text_hash(row["text"]), reason, recipient, row["attempts"]),
            )
            con.execute(
                "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
                "last_error='', updated_at=? WHERE message_id=? AND recipient=?",
                (now, message_id, recipient),
            )
            self._record(con, approver, "approve-quarantine", "", f"#{message_id}: {reason}")
        self._signal(recipient)
        return True

    def approve_checkpoint_resume(self, message_id: int, recipient: str, approver: str,
                                  reason: str) -> bool:
        """
        LIDSKÉ schválení jednoho pokusu, který naváže na checkpoint, ačkoli se
        pracovní kopie od něj změnila nebo ji nejde ověřit (A6, přísný režim).
        Jen úkol zadržený právě touto kontrolou (nespustil se). Není v METHODS
        brokeru: agent si ho dát nemůže, volá ho jen web UI s lidskou session.
        """
        reason = check_short("reason", reason.strip()) or "approved"
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = con.execute(
                "SELECT d.state, d.last_error, d.attempts FROM deliveries d "
                "JOIN checkpoints c ON c.message_id=d.message_id AND c.recipient=d.recipient "
                "WHERE d.message_id=? AND d.recipient=?",
                (message_id, recipient),
            ).fetchone()
            if (not row or row["state"] != "needs_review"
                    or not (row["last_error"] or "").startswith("checkpoint:")):
                return False
            con.execute(
                "UPDATE checkpoints SET resume_approved_by=?, resume_approved_at=?, "
                "resume_approved_attempt=? WHERE message_id=? AND recipient=?",
                (approver, now, row["attempts"], message_id, recipient),
            )
            con.execute(
                "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
                "last_error='', updated_at=? WHERE message_id=? AND recipient=?",
                (now, message_id, recipient),
            )
            self._record(con, approver, "approve-checkpoint-resume", "",
                         f"#{message_id}: {reason}")
        self._signal(recipient)
        return True

    def checkpoint_resume_approved(self, message_id: int, recipient: str) -> bool:
        """Platí jen pro pokus bezprostředně po schválení (jednorázové)."""
        with self._connect() as con:
            row = con.execute(
                "SELECT c.resume_approved_attempt, d.attempts FROM checkpoints c "
                "JOIN deliveries d ON d.message_id=c.message_id AND d.recipient=c.recipient "
                "WHERE c.message_id=? AND c.recipient=? AND c.resume_approved_at IS NOT NULL",
                (message_id, recipient),
            ).fetchone()
        return bool(row) and row["attempts"] == (row["resume_approved_attempt"] or 0) + 1

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
            # A failed or dead-letter attempt may also have produced an effect.
            # Only a human can decide whether any terminal failure is safe to retry.
            state = con.execute("SELECT state FROM deliveries WHERE message_id=? AND recipient=?",
                                (message_id, recipient)).fetchone()
            # (jméno člověka jako `actor` nestačí — agent by ho přes broker podvrhl;
            #  člověk je jen ověřená session webu, actor=None)
            if state and state["state"] in {"needs_review", "failed", "dead_letter"} \
                    and actor is not None:
                return False
            cur = con.execute(
                "UPDATE deliveries SET state='pending', lease_token=NULL, lease_until=NULL, "
                "last_error='', updated_at=? WHERE message_id=? AND recipient=? "
                f"AND state IN {sql_in(RETRYABLE)}",
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
                f"AND state NOT IN {sql_in(UNCANCELLABLE)}",
                (time.time(), message_id, recipient),
            )
        if cur.rowcount:
            # A launcher that passed its last check may still be in flight.
            # Do not acknowledge cancellation until it has completed or failed.
            with launch_gate(self.db_path):
                pass
        return bool(cur.rowcount)

    def overdue(self, seconds: int = 120) -> list[dict]:
        """Adresované zprávy, které déle než `seconds` nikdo nepotvrdil (nebo ani neviděl)."""
        now = time.time()
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.id, m.agent, m.recipient, m.kind, m.text, m.sent_at, "
                "r.seen_at, r.accepted_at FROM messages m "
                "JOIN deliveries d ON d.message_id=m.id AND d.recipient=m.recipient "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=m.recipient "
                f"WHERE m.kind IN ('chat','task','control') AND d.state IN {sql_in(QUEUED)} "
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
                f"AND recipient=? AND lease_token=? AND state IN {sql_in(HELD)} "
                "AND lease_until>? "  # prošlý lease nejde oživit (audit #535 bod 2b)
                # při nouzovém zastavení se lease neprodlouží → worker zastaví (#550/1)
                "AND NOT EXISTS (SELECT 1 FROM control WHERE key='paused' AND value='1')",
                (time.time() + max(1, min(int(lease_seconds), MAX_TASK_LEASE_SECONDS)),
                 time.time(), message_id, recipient,
                 lease_token, time.time()),
            )
        return bool(cur.rowcount)

    def needs_review_list(self, limit: int = 20) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT m.id, m.recipient, m.text, d.last_error, d.updated_at FROM deliveries d "
                "JOIN messages m ON m.id=d.message_id WHERE d.state IN "
                "('needs_review','dead_letter','failed') ORDER BY m.id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [{"id": r["id"], "to": r["recipient"], "text": r["text"],
                 "error": r["last_error"], "updated_at": r["updated_at"]} for r in rows]

    def get_screen(self, message_id: int) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT risk, actionable FROM screens WHERE message_id=?",
                              (message_id,)).fetchone()
        return {"risk": float(row["risk"]), "actionable": row["actionable"]} if row else None

    def set_screen(self, message_id: int, risk: float, actionable: float | None = None) -> None:
        """
        Uloží výsledek screeningu. Riziko jde jen NAHORU a nikdy pod deterministickou
        heuristiku textu: agent přes broker nesmí karanténu „vypnout“ zápisem risk=0
        (Nox #545/3). Schválení zůstává jen lidské (approve_quarantine).
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT text FROM messages WHERE id=?", (message_id,)).fetchone()
            floor = heuristic_risk(row["text"]) if row else 0.0
            prev = con.execute("SELECT risk FROM screens WHERE message_id=?",
                               (message_id,)).fetchone()
            risk = max(float(risk), floor, float(prev["risk"]) if prev else 0.0)
            con.execute(
                "INSERT INTO screens(message_id, risk, actionable, at) VALUES(?,?,?,?) "
                "ON CONFLICT(message_id) DO UPDATE SET risk=excluded.risk, "
                "actionable=excluded.actionable, at=excluded.at",
                (message_id, risk, actionable, time.time()),
            )
