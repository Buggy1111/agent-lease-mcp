"""Small durable queue for the existing broker's deterministic quality worker.

Stop hooks submit metadata and return immediately. In-progress requests are
never silently replayed after restart. No second service or model worker.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import tomllib

from .config import is_trusted_agent
from .quality import inspect_repository
from .quality_runner import load_policy, policy_bytes


class QualityQueueMixin:
    def quality_autorun_enabled(self) -> bool:
        try:
            policy = tomllib.loads(policy_bytes(self.db_path).decode())
            return (type(policy.get("version")) is int and policy["version"] == 1
                    and policy.get("enabled") is True and policy.get("automatic") is True)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return False

    def _quality_auto_request(self, agent: str, repo: str, commit: str, base: str) -> None:
        if (os.environ.get("AGENT_LEASE_QUALITY_AUTORUN", "").lower() not in {"1", "true", "yes"}
                and not self.quality_autorun_enabled()):
            return
        try:
            self.quality_request(agent, repo, commit, base or None)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            self.record(agent, "quality-request-unavailable", repo,
                        "Automatic quality request needs operator setup; no execution claimed.")

    def quality_request(self, agent: str, repo: str, commit: str = "HEAD",
                        base: str | None = None) -> dict:
        self._check_review_agent(agent)
        if not is_trusted_agent(agent):
            raise PermissionError("Quality execution requires a configured agent identity")
        plan = inspect_repository(repo, commit, base)
        policy = load_policy(self.db_path, plan.repository, verify_pins=False)
        identity = "\0".join((agent, plan.repository, plan.commit_sha,
                              plan.base_commit, policy["sha256"]))
        key = hashlib.sha256(identity.encode()).hexdigest()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM quality_requests WHERE request_key=?", (key,)).fetchone()
            if row:
                return dict(row)
            if con.execute("SELECT COUNT(*) FROM quality_requests WHERE state IN ('queued','running')").fetchone()[0] >= 20:
                raise ValueError("Quality queue is full")
            now = time.time()
            cursor = con.execute(
                "INSERT INTO quality_requests(request_key,agent,repository,commit_sha,base_commit,"
                "policy_sha256,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?)",
                (key, agent, plan.repository, plan.commit_sha, plan.base_commit, policy["sha256"], now, now))
            self._record(con, agent, "quality-request", plan.repository, f"request={cursor.lastrowid} commit={plan.commit_sha}")
            return dict(con.execute("SELECT * FROM quality_requests WHERE id=?", (cursor.lastrowid,)).fetchone())

    def quality_requests(self, *, limit: int = 30) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid quality request limit")
        with self._connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM quality_requests ORDER BY id DESC LIMIT ?", (limit,))]

    def _quality_recover_requests(self) -> None:
        with self._connect() as con:
            con.execute("UPDATE quality_requests SET state='interrupted',updated_at=?,"
                        "detail='Broker restarted during execution; explicit inspection/retry required.' "
                        "WHERE state='running'", (time.time(),))

    def _quality_drain_one(self) -> bool:
        if self.paused():
            return False
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM quality_requests WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return False
            con.execute("UPDATE quality_requests SET state='running',updated_at=? WHERE id=?",
                        (time.time(), row["id"]))
        state, detail, run_id = "refused", "Runner configuration or execution could not be verified.", None
        try:
            policy = load_policy(self.db_path, row["repository"], verify_pins=False)
            if policy["sha256"] != row["policy_sha256"]:
                detail = "Operator policy changed; submit a fresh request."
            else:
                result = self.quality_check(row["agent"], row["repository"], row["commit_sha"],
                                            row["base_commit"] or None)
                run_id = result.get("run_id")
                state = ("completed" if run_id else "interrupted" if result.get("executed") else "refused")
                detail = result["overall_status"]
        except (OSError, ValueError, RuntimeError):
            pass  # only fixed diagnostics, no exception/source text in public queue
        with self._connect() as con:
            con.execute("UPDATE quality_requests SET state=?,run_id=?,detail=?,updated_at=? WHERE id=?",
                        (state, run_id, detail, time.time(), row["id"]))
        return True


class QualityWorker:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        store._quality_shutdown = self.stop
        self.thread = threading.Thread(target=self._run, daemon=True, name="agent-lease-quality")

    def start(self):
        self.store._quality_recover_requests()
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=7)

    def _run(self):
        while not self.stop.is_set():
            try:
                worked = self.store._quality_drain_one()
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                worked = False
            if not worked:
                self.stop.wait(.5)
