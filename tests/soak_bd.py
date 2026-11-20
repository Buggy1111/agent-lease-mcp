"""Opt-in, paced Store-state soak. No broker, model or quality runner is launched."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class Pace:
    """At most two workload calls in a rolling second, including racing pairs."""

    def __init__(self):
        self.recent = deque(maxlen=2)
        self.lock = threading.Lock()
        self.latencies = []

    def reserve(self, count=1):
        with self.lock:
            while self.recent and len(self.recent) + count > 2:
                time.sleep(max(0, self.recent[0] + 1.01 - time.monotonic()))
                now = time.monotonic()
                while self.recent and now - self.recent[0] >= 1:
                    self.recent.popleft()
            now = time.monotonic()
            self.recent.extend([now] * count)

    def call(self, fn, *args, **kw):
        self.reserve()
        started = time.monotonic()
        try:
            return fn(*args, **kw)
        finally:
            self.latencies.append(time.monotonic() - started)

    def race(self, left, right):
        self.reserve(2)
        barrier = threading.Barrier(2, timeout=10)

        def run(fn):
            barrier.wait()
            return fn()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, fn) for fn in (left, right)]
            return [f.result(timeout=20) for f in futures]


def refused(fn, error=ValueError):
    try:
        fn()
    except error:
        return True
    raise AssertionError(f"expected {error.__name__}")


class Soak:
    def __init__(self, root):
        # Reuse the existing synthetic fixtures; this harness requires the dev environment.
        from test_quality_runner import make_repo, write_policy

        from agent_lease_mcp.config import Settings
        from agent_lease_mcp.store import Store

        self.root, self.pace, self.counts = root, Pace(), Counter()
        self.settings = Settings(root / "room.db", "codex", 1800)
        self.store_type = Store
        self.stores = [Store(settings=self.settings) for _ in range(2)]
        self.repo = make_repo(root)
        self.head = subprocess.check_output(
            ["/usr/bin/git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        self.base = subprocess.check_output(
            ["/usr/bin/git", "rev-parse", "HEAD^"], cwd=self.repo, text=True).strip()
        self.write_quality_policy = lambda timeout: write_policy(
            self.stores[0], self.repo, timeout=timeout)
        self.write_quality_policy(10)
        self.fix_policy = root / "fix-runner.toml"
        self.fix_policy.write_text('version=1\nenabled=true\nagents=["codex"]\n'
                                   f'repositories=[{json.dumps(str(self.repo))}]\n')
        self.fix_policy.chmod(0o600)
        self.research_policy = root / "research.toml"
        self.research_policy.write_text('version=1\nenabled=true\nagents=["claude-code"]\n'
                                        'budget_tokens=10000\n')
        self.research_policy.chmod(0o600)
        self.last_reopen = time.monotonic()

    def capacity(self):
        s = self.stores[0]
        call = self.pace.call
        for n in range(20):
            self.write_quality_policy(100 + n)
            call(s.quality_request, "codex", str(self.repo))
        self.write_quality_policy(120)
        call(refused, lambda: s.quality_request("codex", str(self.repo)))
        with s._connect() as con:
            con.execute("UPDATE quality_requests SET state='running' WHERE state='queued'")
        call(s._quality_recover_requests)
        a = call(s.research_start, "michal", "capacity one", ["one", "two"])["id"]
        b = call(s.research_start, "michal", "capacity two", ["one", "two"])["id"]
        call(refused, lambda: s.research_start("michal", "capacity third", ["one", "two"]))
        assert call(s.research_cancel, a, actor="michal")
        assert call(s.research_cancel, b, actor="michal")
        self.counts["capacity_checks"] += 1

    def rows(self, sql, params=()):
        with self.stores[0]._connect() as con:
            return [dict(r) for r in con.execute(sql, params)]

    def review(self, number):
        s, other = self.stores
        call = self.pace.call
        rid = call(s.review_open, "codex", str(self.repo), f"soak-{number}", "claude-code",
                   self.base, self.head, secret_findings=[])["id"]

        def verdict(store, kind):
            try:
                store.review_verdict("claude-code", rid, kind, self.head, "fixture",
                                     security="pass", reviewer_findings=[])
                return True
            except ValueError:
                return False

        assert sorted(self.pace.race(lambda: verdict(s, "approved"),
                                    lambda: verdict(other, "changes_requested"))) == [False, True]
        for n in range(3):
            head = f"{number * 10 + n + 100:040x}"
            call(s.review_submit, "codex", rid, head, secret_findings=[])
            row = self.rows("SELECT * FROM reviews WHERE id=?", (rid,))[0]
            assert row["state"] == "awaiting_review" and row["verdict"] is None
            call(s.review_verdict, "claude-code", rid, "changes_requested", head, "fixture")
        row = self.rows("SELECT * FROM reviews WHERE id=?", (rid,))[0]
        if row["state"] != "escalated":
            head = f"{number * 10 + 109:040x}"
            call(s.review_submit, "codex", rid, head, secret_findings=[])
            call(s.review_verdict, "claude-code", rid, "changes_requested", head, "fixture")
        assert self.rows("SELECT state,fix_cycles FROM reviews WHERE id=?", (rid,)) == [
            {"state": "escalated", "fix_cycles": 3}]
        self.counts["reviews"] += 1

    def fix(self, number):
        s = self.stores[0]
        call = self.pace.call
        mid = call(s.send, "claude-code", "codex", f"synthetic fix {number}", kind="task",
                   fix_repo=str(self.repo), fix_base=self.base, fix_allowed=["src"])
        assert call(s.approve_fix, mid, "codex", "michal", "synthetic approval only")
        policy = self.fix_policy.read_text()
        self.fix_policy.write_text(policy.replace("enabled=true", "enabled=false"))
        assert call(s.lease_next, "codex") is None
        self.fix_policy.write_text(policy)
        assert call(s.approve_fix, mid, "codex", "michal", "synthetic reapproval")
        lease = call(s.lease_next, "codex")
        assert lease and lease.message_id == mid
        assert call(s.ack, mid, "codex", "started", lease_token=lease.lease_token)
        assert call(s.complete, mid, "codex", lease.lease_token, "succeeded", "synthetic state")
        rid = call(s.fix_record_result, "codex", mid, self.head)
        call(s.review_verdict, "claude-code", rid, "changes_requested", self.head, "synthetic note")
        linked = self.rows("SELECT message_id FROM fix_loops WHERE review_id=? ORDER BY round", (rid,))
        assert len(linked) == 2
        nxt = linked[-1]["message_id"]
        assert call(s.lease_next, "codex") is None
        row = self.rows("SELECT state,last_error FROM deliveries WHERE message_id=?", (nxt,))[0]
        assert row["state"] == "needs_review" and "approve" in row["last_error"]
        self.counts["fixes"] += 1

    def research(self, number):
        s, other = self.stores
        call = self.pace.call
        rid = call(s.research_start, "michal", f"synthetic question {number}", ["one", "two"])["id"]

        def run(report=True):
            lease = call(s.lease_next, "claude-code", worker=True, research=True)
            assert lease
            mid, token = lease.message_id, lease.lease_token
            assert call(s.ack, mid, "claude-code", "started", lease_token=token)
            key = call(s.begin_usage, mid, "claude-code", token)
            if report:
                assert call(s.record_usage, mid, "claude-code", token,
                            {"source": "claude-json", "input_tokens": 10, "output_tokens": 0,
                             "cache_read_tokens": 0, "cache_creation_tokens": 0}, report_key=key)
            assert call(s.complete, mid, "claude-code", token, "succeeded", "synthetic result")
            return mid

        variant = number % 5
        if variant == 2:
            with s._connect() as con:
                con.execute("UPDATE research SET budget_tokens=10 WHERE id=?", (rid,))
        first = run(report=variant != 3)
        if variant == 0:
            assert call(s.research_cancel, rid, actor="michal")
            expected = "cancelled"
            self.counts["research_cancel"] += 1
        elif variant in (2, 3):
            assert call(s.lease_next, "claude-code", worker=True, research=True) is None
            expected = "partial"
            self.counts["research_budget" if variant == 2 else "research_unknown"] += 1
        else:
            if variant == 4:
                with s._connect() as con:
                    con.execute("UPDATE research SET deadline_at=? WHERE id=?", (time.time(), rid))
                self.counts["research_cutoff"] += 1
            else:
                run()
            self.pace.race(s.research_sweep, other.research_sweep)
            assert len(self.rows("SELECT * FROM research_msgs WHERE research_id=? AND role='synth'",
                                 (rid,))) == 1
            if variant == 4:
                assert json.loads(self.rows("SELECT used_parts FROM research WHERE id=?", (rid,))[0]["used_parts"]) == [first]
            run()
            expected = "done"
        call(s.research_sweep)
        assert self.rows("SELECT state FROM research WHERE id=?", (rid,)) == [{"state": expected}]
        self.counts["research"] += 1

    def quality(self, number):
        s, other = self.stores
        call = self.pace.call
        self.write_quality_policy(200 + number * 2)
        requests = self.pace.race(lambda: s.quality_request("codex", str(self.repo)),
                                  lambda: other.quality_request("codex", str(self.repo)))
        assert requests[0]["id"] == requests[1]["id"]
        self.write_quality_policy(201 + number * 2)
        assert call(s._quality_drain_one)
        assert self.rows("SELECT state FROM quality_requests WHERE id=?", (requests[0]["id"],)) == [
            {"state": "refused"}]
        # Fixture crashpoint only: never claim this is actual project execution or a broker crash.
        request = call(s.quality_request, "codex", str(self.repo))
        with s._connect() as con:
            con.execute("UPDATE quality_requests SET state='running' WHERE id=?", (request["id"],))
        call(other._quality_recover_requests)
        assert not call(s._quality_drain_one)
        assert self.rows("SELECT state,run_id FROM quality_requests WHERE id=?", (request["id"],)) == [
            {"state": "interrupted", "run_id": None}]
        self.counts["quality"] += 1

    def check(self):
        assert self.rows("PRAGMA integrity_check") == [{"integrity_check": "ok"}]
        assert not self.rows("PRAGMA foreign_key_check")
        assert not self.rows("SELECT id FROM reviews WHERE fix_cycles>max_fix_cycles")
        assert not self.rows("SELECT research_id FROM research_msgs WHERE role='synth' "
                             "GROUP BY research_id HAVING count(*)>1")
        for table, reference, target in [("fix_loops", "review_id", "reviews"),
                                         ("research_msgs", "research_id", "research"),
                                         ("fix_loops", "message_id", "messages"),
                                         ("research_msgs", "message_id", "messages")]:
            assert not self.rows(f"SELECT t.{reference} FROM {table} t LEFT JOIN {target} r "
                                 f"ON r.id=t.{reference} WHERE r.id IS NULL")
        assert not self.rows("SELECT q.id FROM quality_requests q LEFT JOIN quality_runs r "
                             "ON r.id=q.run_id WHERE q.run_id IS NOT NULL AND r.id IS NULL")
        assert not self.rows("SELECT * FROM quality_runs")  # no fake quality PASS
        assert not self.rows("SELECT d.message_id FROM deliveries d JOIN messages m ON m.id=d.message_id "
                             "WHERE m.kind='task' AND d.state IN ('leased','started','pending')")
        assert not self.rows("SELECT * FROM quality_requests WHERE state IN ('queued','running')")
        assert not (self.root / "provider-attempted").exists()

    def reopen(self):
        self.stores = [self.store_type(settings=self.settings) for _ in range(2)]
        self.last_reopen = time.monotonic()
        self.counts["store_reopens"] += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="new short absolute directory; never a live room")
    parser.add_argument("--seconds", type=int, choices=(120, 600), default=120)
    args = parser.parse_args()
    root = args.root
    if not __debug__:
        parser.error("assertions must be enabled; do not use python -O")
    if not root.is_absolute() or len(os.fsencode(root)) > 60 or root.exists():
        parser.error("root must be a NEW absolute path of at most 60 bytes")
    os.nice(19)
    if os.getpriority(os.PRIO_PROCESS, 0) != 19:
        parser.error("nice19 required")
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    signal.alarm(args.seconds + 60)  # a hung workload must never outlive its bounded window
    root.mkdir(mode=0o700)
    (root / "bin").mkdir()
    for name in ("codex", "claude"):
        stub = root / "bin" / name
        stub.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(root / "provider-attempted"))}\nexit 99\n')
        stub.chmod(0o700)
    os.environ.clear()
    os.environ.update(HOME=str(root), PATH=f"{root}/bin:/usr/bin:/bin",
                      AGENT_LEASE_DB=str(root / "room.db"), AGENT_LEASE_HUMAN="michal",
                      AGENT_NAME="codex", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    result = {"status": "FAIL", "scope": "Store-state only, not broker crash or runner E2E",
              "requested_seconds": args.seconds, "nice": 19, "address_space_limit_mib": 512,
              "source_commit": subprocess.check_output(
                  ["/usr/bin/git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                  text=True).strip(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "schedule": "deterministic round-robin; no random seed"}
    started = time.monotonic()
    try:
        soak = Soak(root)
        soak.capacity()
        number = 0
        while time.monotonic() - started < args.seconds:
            for scenario in (soak.review, soak.fix, soak.research, soak.quality):
                scenario(number)
                if time.monotonic() - soak.last_reopen >= 30:
                    soak.reopen()
            soak.check()
            number += 1
            print(json.dumps({"elapsed": time.monotonic() - started, "counts": soak.counts}), flush=True)
        minimum = 1 if args.seconds == 120 else 20
        assert time.monotonic() - started <= args.seconds + 60
        assert all(soak.counts[name] >= minimum for name in ("reviews", "fixes", "research", "quality"))
        soak.check()
        result.update(status="PASS", counts=dict(soak.counts),
                      max_call_seconds=max(soak.pace.latencies, default=0))
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        result.update(elapsed_seconds=time.monotonic() - started,
                      peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      database_bytes=(root / "room.db").stat().st_size if (root / "room.db").exists() else 0)
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
