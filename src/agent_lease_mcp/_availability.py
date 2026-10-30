"""Přítomnost, limity poskytovatelů, failover a přehled.

Část `Store`; rozděleno ze store.py podle rozboru Noxe (#516).
"""

from __future__ import annotations

import time

from ._store_common import (
    _check_agent,
    _limit_reset_passed,
    _presence_state,
)
from .config import (
    STALE_PEER_SECONDS,
    human_name,
)
from .hygiene import (
    check_path,
    check_short,
    heuristic_risk,
    redact,
    untrusted,
)
from .policy import covers
from .states import (
    UNFINISHED_TASK,
    sql_in,
)


class AvailabilityMixin:
    def heartbeat(self, agent: str, status: str | None = None, cwd: str | None = None) -> None:
        """
        Ohlásit, že agent žije. `status`/`cwd` = None znamená „nech, co tam je".

        ⚠️ Prázdný řetězec status smaže, None ne — a ten rozdíl je podstatný.
        Dokud ho tu nebylo, každé `room()` bez argumentu přepsalo popis práce
        prázdnem a `say` slovem „say", takže v místnosti svítilo jméno
        posledního volání místo toho, na čem druhý agent dělá.
        """
        _check_agent(agent)
        if status:
            check_short("status", status)
        if cwd:
            check_path(cwd)
        with self._connect() as con:
            con.execute(
                "INSERT INTO agents(agent, status, cwd, seen_at) VALUES(?,?,?,?) "
                "ON CONFLICT(agent) DO UPDATE SET "
                "  status=COALESCE(?, agents.status), "
                "  cwd=COALESCE(?, agents.cwd), "
                "  seen_at=excluded.seen_at",
                (agent, status or "", cwd or "", time.time(), status, cwd),
            )

    def peers(self, stale_after: int = STALE_PEER_SECONDS) -> list[dict]:
        now = time.time()
        with self._connect() as con:
            rows = con.execute("SELECT * FROM agents ORDER BY seen_at DESC").fetchall()
            limited = {r["agent"]: r["until"] for r in
                       con.execute("SELECT agent, until FROM limits WHERE until > ?", (now,))}
            providers = {r["agent"]: dict(r) for r in
                         con.execute("SELECT * FROM provider_states")}
        return [
            {
                "agent": r["agent"],
                "status": r["status"],
                "cwd": r["cwd"],
                "seen_seconds_ago": int(now - r["seen_at"]),
                "active": r["seen_at"] >= now - stale_after,
                "presence": (
                    "rate-limited" if r["agent"] in limited else
                    # Reset od poskytovatele už prošel: worker to zkusí s dalším úkolem,
                    # do té doby nehlásit „rate-limited" navždy.
                    "recovering" if _limit_reset_passed(providers.get(r["agent"]), now) else
                    "rate-limited" if providers.get(r["agent"], {}).get("state") == "limited" else
                    "provider-unknown" if providers.get(r["agent"], {}).get("state") == "unknown"
                    else _presence_state(r["status"], now - r["seen_at"], stale_after)
                ),
                "limited_until": limited.get(r["agent"]),
                "provider_state": providers.get(r["agent"], {}).get("state"),
                "provider_source": providers.get(r["agent"], {}).get("source"),
                "provider_reason": providers.get(r["agent"], {}).get("reason", ""),
                "provider_resets_at": providers.get(r["agent"], {}).get("resets_at"),
            }
            for r in rows
        ]

    def report_limit(self, agent: str, until: float, reason: str = "") -> None:
        """Agent (nebo bridge) hlásí vyčerpaný limit do času `until` (epoch)."""
        _check_agent(agent)
        reason = redact(check_short("reason", reason))
        until = min(until, time.time() + 7 * 86400)  # strop: týden
        with self._connect() as con:
            con.execute("INSERT OR REPLACE INTO limits(agent, until, reason, at) VALUES(?,?,?,?)",
                        (agent, until, reason, time.time()))
            self._record(con, agent, "limit", "", f"do {int(until)}: {reason}")
        self.failover_sweep(grace=0)  # bez čekání: kdo hlásí limit, nic nepřevezme

    def clear_limit(self, agent: str) -> bool:
        with self._connect() as con:
            return bool(con.execute("DELETE FROM limits WHERE agent=?", (agent,)).rowcount)

    def limits(self) -> list[dict]:
        now = time.time()
        with self._connect() as con:
            con.execute("DELETE FROM limits WHERE until <= ?", (now,))
            rows = con.execute("SELECT * FROM limits ORDER BY until").fetchall()
        return [{"agent": r["agent"], "until": r["until"], "reason": r["reason"],
                 "seconds_left": int(r["until"] - now)} for r in rows]

    def report_provider_state(self, agent: str, state: str, source: str, *,
                              observed_at: float | None = None,
                              resets_at: float | None = None, reason: str = "") -> None:
        """Persist an explicit provider signal; never manufacture a reset deadline."""
        _check_agent(agent)
        if state not in {"available", "limited", "unknown"}:
            raise ValueError("state must be available, limited or unknown")
        source = redact(check_short("source", source))
        reason = redact(check_short("reason", reason))
        at = time.time() if observed_at is None else observed_at
        with self._connect() as con:
            con.execute(
                "INSERT INTO provider_states(agent,state,source,observed_at,resets_at,reason) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET "
                "state=excluded.state, source=excluded.source, observed_at=excluded.observed_at, "
                "resets_at=excluded.resets_at, reason=excluded.reason",
                (agent, state, source, at, resets_at, reason),
            )
            self._record(con, agent, "provider-state", "", f"{state} via {source}: {reason}")
        if state == "limited":
            self.failover_sweep(grace=0)

    def provider_states(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM provider_states ORDER BY agent").fetchall()
        return [dict(row) for row in rows]

    def provider_state(self, agent: str) -> dict | None:
        _check_agent(agent)
        with self._connect() as con:
            row = con.execute("SELECT * FROM provider_states WHERE agent=?", (agent,)).fetchone()
        return dict(row) if row else None

    def clear_provider_state(self, agent: str) -> bool:
        """Forget a provider observation after a human/agent confirms recovery."""
        _check_agent(agent)
        with self._connect() as con:
            removed = bool(con.execute(
                "DELETE FROM provider_states WHERE agent=?", (agent,)
            ).rowcount)
            if removed:
                self._record(con, agent, "provider-clear", "", "explicit recovery confirmation")
        if removed:
            self._signal(agent)
        return removed

    def is_available(self, agent: str) -> bool:
        """Logical agent has either a live interactive or live worker execution path."""
        available = {p["agent"] for p in self.peers() if p["presence"] in ("idle", "busy")}
        return agent in available or f"{agent}-bridge" in available

    def failover_activation(self, agent: str, *, include_existing: bool = False) -> int:
        """Create once and return the durable lower task-id bound for failover."""
        return self._activate("failover_activations", "failover-activate", agent, include_existing)

    def failover_sweep(self, *, grace: int | None = None) -> list[dict]:
        """
        Přesune NEZAHÁJENÉ úkoly od nedostupného agenta na první dostupnou náhradu
        z jeho řetězce (`AGENT_LEASE_FALLBACKS`). Nic nespouští ani nemaže:
        - jen `pending` tasky (cokoli rozdělaného zůstává u svého agenta),
        - jen po `grace` sekundách čekání (krátký výpadek nevyvolá přeskakování),
        - odesílatel i člověk dostanou zprávu, kam úkol putuje.
        Idempotentní; volá ho broker (každých ~15 s), bridge a hooky.
        """
        grace = self.settings.failover_grace if grace is None else grace
        chains = self.settings.fallbacks
        if not chains:
            return []
        now = time.time()
        limited = {l["agent"]: l for l in self.limits()}
        limited.update({p["agent"]: p for p in self.provider_states()
                        if p["state"] == "limited"})
        alive = {p["agent"] for p in self.peers() if p["presence"] in ("idle", "busy")}

        def can_run(agent: str) -> bool:
            return agent in alive or f"{agent}-bridge" in alive

        moves: list[dict] = []
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # nouzové zastavení zmrazí i přesouvání úkolů (#550)
            if con.execute("SELECT 1 FROM control WHERE key='paused' AND value='1'").fetchone():
                return []
            for src, chain in chains.items():
                if can_run(src):
                    continue  # zdroj je v pořádku
                target = next((c for c in chain if can_run(c)), None)
                if not target:
                    continue
                activation = con.execute(
                    "SELECT after_message_id FROM failover_activations WHERE agent=?", (src,)
                ).fetchone()
                after_message_id = int(activation["after_message_id"]) if activation else 0
                source_limited = src in limited or f"{src}-bridge" in limited
                rows = con.execute(
                    "SELECT m.id, m.agent, m.text, m.sent_at FROM deliveries d "
                    "JOIN messages m ON m.id=d.message_id "
                    "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=d.recipient "
                    "WHERE d.recipient=? AND d.state='pending' AND m.kind='task' "
                    "AND r.accepted_at IS NULL AND m.id>? AND m.sent_at <= ? AND m.agent<>?",
                    # Explicitní limit = krátká lhůta. Pouhé „nedává znamení" = dlouhá:
                    # agent může spát v `wait` a úkol si vzít, jakmile ho probudíme.
                    (src, after_message_id, now - (grace if source_limited
                                                   else max(grace, self.settings.offline_grace)),
                     target),
                ).fetchall()
                for r in rows:
                    if con.execute("SELECT 1 FROM deliveries WHERE message_id=? AND recipient=?",
                                   (r["id"], target)).fetchone():
                        continue
                    con.execute("UPDATE deliveries SET recipient=?, updated_at=? "
                                "WHERE message_id=? AND recipient=?", (target, now, r["id"], src))
                    con.execute("UPDATE messages SET recipient=? WHERE id=?", (target, r["id"]))
                    con.execute("DELETE FROM receipts WHERE message_id=? AND recipient=?",
                                (r["id"], src))
                    limit = limited.get(src) or limited.get(f"{src}-bridge")
                    if limit and limit.get("until"):
                        why = ("limited until "
                               f"{time.strftime('%H:%M', time.localtime(limit['until']))}")
                    elif limit:
                        why = f"provider limit ({limit.get('reason') or limit.get('source')})"
                    else:
                        why = "unavailable"
                    note = f"↪ task #{r['id']} moved {src} → {target} ({src}: {why})"
                    for to in {r["agent"], human_name(self.db_path)}:
                        self._insert_event(con, "broker", to, note, r["id"], now)
                    snippet = " ".join(r["text"].split())[:400]
                    # Zprávy od brokeru se nescreenují (věří se jim), takže cizí text
                    # do nich nesmí jít bez kontroly — jinak by přesun úkolu obešel
                    # screening prompt injection (security review 16. 10.).
                    if heuristic_risk(snippet) >= 0.5:
                        snippet = (f"[held back: possible injection — read #{r['id']} via "
                                   "`inbox` and judge it yourself]")
                    else:
                        snippet = untrusted(snippet, 400)
                    handover = (f"↪ You took over task #{r['id']} from {src} ({why}), "
                                f"assigned by {r['agent']}: {snippet}")
                    # Nový příjemce dostane ZPRÁVU S NOVÝM ID: původní úkol má staré id,
                    # které může být za kurzorem cíle, a do jeho kontextu by se nikdy nevložilo.
                    self._insert_event(con, "broker", target, handover, r["id"], now)
                    self._record(con, "broker", "failover", "", f"#{r['id']} {src}->{target}")
                    moves.append({"id": r["id"], "from": src, "to": target})
        for m in moves:
            self._signal(m["to"])
        return moves

    def board(self) -> list[dict]:
        """Přehled „kdo co dělá": agent → přítomnost, limit, aktivní úkol, fronta, nepotvrzené."""
        now = time.time()
        peers = {p["agent"]: p for p in self.peers()}
        with self._connect() as con:
            rows = con.execute(
                "SELECT d.recipient, d.state, d.message_id, m.text, m.agent AS sender, "
                "m.sent_at, r.accepted_at FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "LEFT JOIN receipts r ON r.message_id=d.message_id AND r.recipient=d.recipient "
                f"WHERE d.state IN {sql_in(UNFINISHED_TASK)} "
                "AND m.kind IN ('task','chat','control') ORDER BY d.message_id"
            ).fetchall()
            path_rows = con.execute(
                "SELECT tp.message_id, tp.path FROM task_paths tp "
                "JOIN deliveries d ON d.message_id=tp.message_id "
                "WHERE d.state='pending' ORDER BY tp.message_id, tp.path"
            ).fetchall()
            names = set(peers) | {r["recipient"] for r in rows}
        paths_by_message: dict[int, list[str]] = {}
        for path_row in path_rows:
            paths_by_message.setdefault(int(path_row["message_id"]), []).append(path_row["path"])
        claims = self.claims()
        board = []
        for name in sorted(names):
            mine = [r for r in rows if r["recipient"] == name]
            active = next((r for r in mine if r["state"] in ("started", "leased")), None)
            blocked_task = None
            for pending in (r for r in mine if r["state"] == "pending"):
                for path in paths_by_message.get(int(pending["message_id"]), []):
                    blocker = next((claim for claim in claims
                                    if claim.agent not in (name, f"{name}-bridge") and
                                    (covers(claim.path, path) or covers(path, claim.path))), None)
                    if blocker:
                        blocked_task = {
                            "id": pending["message_id"], "path": path,
                            "held_by": blocker.agent, "expires_in": blocker.expires_in,
                        }
                        break
                if blocked_task:
                    break
            provider_reason = (
                "provider rate limit" if peers.get(name, {}).get("provider_state") == "limited"
                else "provider signal unavailable"
                if peers.get(name, {}).get("provider_state") == "unknown"
                else "reported rate limit" if peers.get(name, {}).get("limited_until")
                else "no recent heartbeat" if peers.get(name, {}).get("presence") == "offline"
                else ""
            )
            board.append({
                "agent": name,
                "presence": peers.get(name, {}).get("presence", "offline"),
                "status": peers.get(name, {}).get("status", ""),
                "limited_until": peers.get(name, {}).get("limited_until"),
                "provider_state": peers.get(name, {}).get("provider_state"),
                "provider_source": peers.get(name, {}).get("provider_source"),
                "provider_reason": peers.get(name, {}).get("provider_reason", ""),
                "provider_resets_at": peers.get(name, {}).get("provider_resets_at"),
                "blocked_reason": provider_reason or (
                    "task lease conflict" if blocked_task else ""
                ),
                "blocked_task": blocked_task,
                "active": ({"id": active["message_id"], "text": active["text"][:120],
                            "from": active["sender"]} if active else None),
                "queued": sum(r["state"] == "pending" for r in mine),
                "unaccepted": sum(r["state"] == "pending" and not r["accepted_at"] for r in mine),
                "needs_review": sum(r["state"] == "needs_review" for r in mine),
                "oldest_wait_s": int(now - min((r["sent_at"] for r in mine
                                                if r["state"] == "pending"), default=now)),
            })
        return board

    def task_summary(self, recipient: str) -> dict:
        """Aggregate task pipeline state without loading task text or lease tokens."""
        _check_agent(recipient)
        with self._connect() as con:
            rows = con.execute(
                "SELECT d.state, COUNT(*) AS total, "
                "SUM(CASE WHEN r.accepted_at IS NULL THEN 1 ELSE 0 END) AS unaccepted "
                "FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "LEFT JOIN receipts r ON r.message_id=m.id AND r.recipient=d.recipient "
                "WHERE d.recipient=? AND m.kind='task' GROUP BY d.state",
                (recipient,),
            ).fetchall()
        states = {r["state"]: int(r["total"]) for r in rows}
        pending = states.get("pending", 0)
        pending_row = next((r for r in rows if r["state"] == "pending"), None)
        unaccepted = int(pending_row["unaccepted"] or 0) if pending_row else 0
        return {
            "pending": pending,
            "unaccepted": unaccepted,
            "accepted_without_start": max(0, pending - unaccepted),
            "leased": states.get("leased", 0),
            "started": states.get("started", 0),
            "needs_review": states.get("needs_review", 0),
        }
