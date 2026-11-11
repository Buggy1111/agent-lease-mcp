"""B7 evidence. Broker clients request execution but cannot submit its results.

The socket is trusted to the same OS user, as elsewhere in Store. This module
does not authenticate that user's Python code or turn a supplied exit code into
proof of execution. The isolated supervisor calls the private writer after
collecting observations itself, never from a client-supplied report.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from .config import human_name
from .hygiene import check_path, check_short
from .policy import normalize_path
from .quality import COMMAND_CHECKS, Check, Inspection, Status, aggregate, inspect_repository
from .secret_scan import scan_diff
from .workspace import commit_diff


@dataclass(frozen=True)
class MetricObservation:
    format: str
    covered: int
    total: int
    crap_known: int = 0
    crap_unknown: int = 0
    crap_review: int = 0
    crap_max_milli: int | None = None

    def __post_init__(self) -> None:
        if self.format not in {"coverage.py", "istanbul"}:
            raise ValueError("Unsupported metric format")
        for value in (self.covered, self.total, self.crap_known, self.crap_unknown, self.crap_review):
            if type(value) is not int or not 0 <= value <= 1_000_000_000:
                raise ValueError("Invalid metric count")
        if not 0 <= self.covered <= self.total or self.total == 0 or self.crap_review > self.crap_known:
            raise ValueError("Inconsistent metric counts")
        if self.crap_known:
            if type(self.crap_max_milli) is not int or not 0 <= self.crap_max_milli <= 10**12:
                raise ValueError("Invalid CRAP score")
        elif self.crap_max_milli is not None:
            raise ValueError("CRAP without measured functions is not a score")


@dataclass(frozen=True)
class CommandObservation:
    """Process metadata only. Raw output, arguments and arbitrary verdicts stay out."""

    check: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    output_bytes: int
    output_sha256: str
    interrupted: bool = False
    output_limited: bool = False
    metrics: MetricObservation | None = None

    def __post_init__(self) -> None:
        if self.check not in COMMAND_CHECKS:
            raise ValueError("Only configured command checks have process observations")
        if type(self.timed_out) is not bool:
            raise ValueError("Invalid process timeout flag")
        if type(self.interrupted) is not bool or type(self.output_limited) is not bool:
            raise ValueError("Invalid supervision flags")
        if self.metrics is not None and (type(self.metrics) is not MetricObservation or self.check != "coverage"):
            raise ValueError("Numeric metrics belong only to the coverage command")
        if (self.exit_code is None and not self.timed_out) or (
            self.exit_code is not None and
            (type(self.exit_code) is not int or not -255 <= self.exit_code <= 255)
        ):
            raise ValueError("Invalid process exit code")
        for value in (self.duration_ms, self.output_bytes):
            if type(value) is not int or not 0 <= value <= 2**53 - 1:
                raise ValueError("Invalid process measurement")
        if not isinstance(self.output_sha256, str) or not re.fullmatch(
                r"[0-9a-f]{64}", self.output_sha256):
            raise ValueError("Invalid process output digest")


def _command_result(observation: CommandObservation) -> tuple[Status, str]:
    if observation.interrupted:
        return Status.REVIEW, "Run was interrupted by pause, lost ownership or its total time budget."
    if observation.output_limited:
        return Status.REVIEW, "Command exceeded the bounded output capture limit."
    if observation.timed_out:
        return Status.FAIL, "Command exceeded the runner time limit."
    if observation.exit_code != 0:
        return Status.FAIL, "Configured command exited unsuccessfully."
    if observation.check == "coverage":
        if observation.metrics is not None:
            metric = observation.metrics
            status = Status.PASS if metric.covered * 100 >= 80 * metric.total else Status.REVIEW
            return status, "Validated line coverage report; below 80% requests review, never security failure."
        return Status.REVIEW, "Coverage command succeeded; a validated numeric metric is not imported."
    return Status.PASS, "Configured command exited successfully."


class QualityRunsMixin:
    def _quality_record_attempt(self, agent: str, inspection: Inspection, result: dict) -> dict:
        """Keep refusals visible without inventing process observations or execution."""
        self._check_review_agent(agent)
        evidence = json.dumps({"attempt": inspection.public() | {"kind": "quality_attempt"}
                               | result | {"agent": agent}}, sort_keys=True)
        with self._connect() as con:
            cursor = con.execute(
                "INSERT INTO quality_runs(execution_id,repository,commit_sha,recorded_at,"
                "evidence_sha256,evidence_json) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, inspection.repository, inspection.commit_sha, time.time(),
                 hashlib.sha256(evidence.encode()).hexdigest(), evidence))
            row = con.execute("SELECT * FROM quality_runs WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._quality_public(con, row)

    def _quality_record_run(self, agent: str, inspection: Inspection,
                            observations: tuple[CommandObservation, ...], *,
                            execution_id: str, task_id: int | None = None,
                            sandbox: dict | None = None, branch: str | None = None) -> dict:
        """Private runner sink, deliberately absent from the broker/MCP write API.

        Re-inspect immutable Git objects; no raw commands/output are persisted.
        Retries of the same execution are idempotent, conflicting reuse rejected.
        This method itself never starts a process or creates an execution claim.
        """
        self._check_review_agent(agent)
        if task_id is not None and (type(task_id) is not int or task_id <= 0):
            raise ValueError("Invalid quality task identity")
        if branch is not None and (not isinstance(branch, str) or not 0 < len(branch) <= 256
                                   or any(ord(c) < 32 or ord(c) == 127 for c in branch)):
            raise ValueError("Invalid quality branch label")
        if (not isinstance(execution_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", execution_id)):
            raise ValueError("Invalid execution identity")
        if type(inspection) is not Inspection:
            raise TypeError("An inspection is required, not a submitted JSON report")
        current = inspect_repository(inspection.repository, inspection.commit_sha,
                                     inspection.base_commit or None, policy_base=inspection.policy_base)
        if replace(current, dirty=inspection.dirty) != inspection:
            raise ValueError("Run plan does not match committed project metadata")
        if not inspection.enabled or inspection.problems:
            raise ValueError("Cannot record execution of an opted-out or invalid plan")
        if (not isinstance(observations, tuple) or not observations
                or any(type(o) is not CommandObservation for o in observations)):
            raise ValueError("A run requires process observations; an inspection is not a run")
        by_name = {o.check: o for o in observations}
        if len(by_name) != len(observations):
            raise ValueError("Duplicate process observation")
        configured = {c.name for c in inspection.checks if c.command}
        if by_name.keys() - configured:
            raise ValueError("Observation does not belong to a committed command")
        checks = []
        coverage = by_name.get("coverage")
        metric = (coverage.metrics if coverage and coverage.exit_code == 0 and not
                  (coverage.interrupted or coverage.timed_out or coverage.output_limited) else None)
        for check in inspection.checks:
            if check.name in by_name:
                status, detail = _command_result(by_name[check.name])
                check = replace(check, status=status, detail=detail)
            if check.name == "crap" and metric is not None:
                if metric.crap_known:
                    status = (Status.REVIEW if metric.crap_review else Status.NA if metric.crap_unknown else Status.PASS)
                    check = replace(check, status=status,
                                    detail="Python CRAP uses pinned Lizard and attributable coverage; report only.")
                elif metric.crap_unknown:
                    check = replace(check, status=Status.NA,
                                    detail="Function coverage could not be attributed; CRAP is N/A.")
            public = check.public() | {"name": check.name}
            if check.name == "coverage" and metric is not None:
                public["value"] = f"{100 * metric.covered / metric.total:.2f}% lines ({metric.covered}/{metric.total})"
            if check.name == "crap" and metric is not None and metric.crap_known:
                public["value"] = (f"max {metric.crap_max_milli / 1000:.3f}; "
                                   f"{metric.crap_known} measured, {metric.crap_review} REVIEW, {metric.crap_unknown} N/A")
            checks.append(public)
        # Explicit allowlist: Inspection.public() is a plan, never the stored run.
        evidence = {"repository": inspection.repository, "commit_sha": inspection.commit_sha,
                    "base_commit": inspection.base_commit, "agent": agent,
                    "policy_base": inspection.policy_base,
                    "language": inspection.language, "config_digest": inspection.config_digest,
                    "policy_paths": list(inspection.policy_paths), "checks": checks,
                    "task_id": task_id, "sandbox": sandbox,
                    "branch": branch,
                    "observations": [asdict(by_name[name]) for name in sorted(by_name)]}
        # Celý rozsah od základu připnutého operátorem, ne od základu volajícího: jinak
        # by se starší commit ze zásobníku P→A→B schoval před A..B (review #12, F2).
        evidence["broker_scan_clean"] = not scan_diff(commit_diff(
            inspection.repository, inspection.commit_sha,
            inspection.policy_base or inspection.base_commit))
        body = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM quality_runs WHERE execution_id=?",
                              (execution_id,)).fetchone()
            if row:
                if row["evidence_sha256"] != digest:
                    raise ValueError("Execution identity already belongs to different evidence")
            else:
                con.execute(
                    "INSERT INTO quality_runs(execution_id, repository, commit_sha, recorded_at, "
                    "evidence_sha256, evidence_json) VALUES(?,?,?,?,?,?)",
                    (execution_id, inspection.repository, inspection.commit_sha,
                     time.time(), digest, body),
                )
                self._record(con, agent, "quality-run", inspection.repository,
                             f"execution={execution_id} commit={inspection.commit_sha}")
                row = con.execute("SELECT * FROM quality_runs WHERE execution_id=?",
                                  (execution_id,)).fetchone()
            return self._quality_public(con, row)

    @staticmethod
    def _quality_public(con, row) -> dict:
        evidence = json.loads(row["evidence_json"])
        if "attempt" in evidence:
            return evidence["attempt"] | {
                "run_id": row["id"], "execution_id": row["execution_id"],
                "timestamp": datetime.fromtimestamp(row["recorded_at"], UTC).isoformat(),
                "evidence_sha256": row["evidence_sha256"], "history": [], "logs": [],
                "policy_approval": None, "observations": [], "review_status": "pending",
                "policy_review_status": "pending" if evidence["attempt"]["policy_paths"] else "not_required"}
        # Approval is read from the existing B6 authority at read time. A new
        # review/re-submit invalidates it; neither caller nor project sets it.
        # Review must start at the operator-pinned base: a review of only the
        # last commit of a stack would leave the earlier commits unreviewed (#12, F2).
        review = con.execute(
            "SELECT * FROM reviews WHERE repo=? AND head_commit=? AND base_commit=? "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (row["repository"], row["commit_sha"],
             evidence.get("policy_base") or evidence["base_commit"]),
        ).fetchone()
        independent = bool(review and review["verdict_by"]
                           and review["verdict_by"] != review["author"]
                           and review["verdict_commit"] == row["commit_sha"])
        approved = bool(independent and review["state"] in ("approved", "closed")
                        and review["verdict"] == "approved" and review["security"] == "pass"
                        and review["security_by"] == review["verdict_by"]
                        and review["scan_commit"] == row["commit_sha"]
                        and review["scan_findings"] == "[]"
                        and evidence.get("broker_scan_clean") is True)
        security_failed = bool(independent and review["security"] == "fail"
                               and review["security_by"] == review["verdict_by"])
        security_failed = security_failed or evidence.get("broker_scan_clean") is False
        checks = []
        public_checks = {}
        for item in evidence["checks"]:
            item = dict(item)
            name = item.pop("name")
            if name == "security":
                item["status"] = (Status.PASS if approved else Status.FAIL if security_failed
                                  else Status.REVIEW).value
                item["detail"] = ("Exact-commit independent B6 review and diff scan approved."
                                  if approved else "Independent B6 review is pending or not approved.")
                if security_failed:
                    item["detail"] = "Broker diff scan or independent B6 security review rejected this commit."
            public_checks[name] = item
            checks.append(Check(name, Status(item["status"]), item["required"], item["detail"]))
        policy_approval = con.execute("SELECT approved_by,approved_at FROM quality_policy_approvals WHERE run_id=?",
                                      (row["id"],)).fetchone()
        status = aggregate(tuple(checks), peer_review="approved" if approved else "pending",
                           policy_review_required=bool(evidence["policy_paths"]) and not policy_approval)
        interrupted = any(o.get("interrupted") or o.get("output_limited") for o in evidence["observations"])
        if interrupted and status is not Status.FAIL:
            status = Status.REVIEW
        return {"schema_version": 1, "kind": "quality_run", "executed": True,
                "run_id": row["id"], "execution_id": row["execution_id"],
                "repository": row["repository"], "commit_sha": row["commit_sha"],
                "base_commit": evidence["base_commit"] or None,
                "policy_base": evidence.get("policy_base"),
                "branch": evidence.get("branch"), "agent": evidence["agent"],
                "task_id": evidence.get("task_id"), "sandbox": evidence.get("sandbox"),
                "execution_status": "interrupted" if interrupted else "completed",
                "timestamp": datetime.fromtimestamp(row["recorded_at"], UTC).isoformat(),
                "language": evidence["language"], "config_digest": evidence["config_digest"],
                "checks": public_checks, "observations": evidence["observations"],
                "overall_status": status.value,
                "policy_review_required": bool(evidence["policy_paths"]),
                "policy_review_status": "approved" if policy_approval else "pending" if evidence["policy_paths"] else "not_required",
                "policy_approval": dict(policy_approval) if policy_approval else None,
                "policy_paths": evidence["policy_paths"],
                "reviewer": review["verdict_by"] if independent else None,
                "review_status": "approved" if approved else "pending",
                "review_id": review["id"] if review else None,
                "logs": [{"check": o["check"],
                          "url": f"/api/quality-log?run_id={row['id']}&check={o['check']}"}
                         for o in evidence["observations"]],
                "history": [], "evidence_sha256": row["evidence_sha256"]}

    def quality_human_approve_policy(self, by: str, run_id: int, commit_sha: str, reason: str) -> dict:
        """Human-only, exact-run approval. Never bypasses failed checks or peer review."""
        if by != human_name(self.db_path):
            raise PermissionError("Only the human web operator can approve critical policy changes")
        reason = check_short("reason", reason).strip()
        if len(reason) < 10:
            raise ValueError("Describe why the critical change is accepted")
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("Invalid run identity")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM quality_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["commit_sha"] != commit_sha:
                raise ValueError("Approval must match the exact recorded run and commit")
            run = self._quality_public(con, row)
            if (not run["policy_review_required"] or run["review_status"] != "approved"
                    or any(c["required"] and c["status"] != "PASS" for c in run["checks"].values())
                    or run["execution_status"] != "completed"):
                raise PermissionError("Required checks and independent B6 review must pass first")
            if run["policy_approval"] is None:
                con.execute("INSERT INTO quality_policy_approvals VALUES(?,?,?,?)",
                            (run_id, by, time.time(), reason))
                self._record(con, by, "quality-policy-approval", run["repository"],
                             f"run={run_id} commit={commit_sha} {reason}")
            return self._quality_public(con, row)

    def quality_check(self, agent: str, repo: str, commit: str = "HEAD", base: str | None = None,
                      *, task_id: int | None = None, lease_token: str | None = None) -> dict:
        from .quality_runner import run_quality
        return run_quality(self, agent, repo, commit, base, task_id=task_id, lease_token=lease_token)

    def quality_log(self, run_id: int, check: str) -> str | None:
        """Fully redacted process log: no raw project output or arbitrary path serving."""
        run = self.quality_get(run_id)
        if run is None:
            return None
        observation = next((o for o in run["observations"] if o["check"] == check), None)
        if observation is None:
            return None
        return json.dumps({"run_id": run_id, "commit_sha": run["commit_sha"],
                           "output_policy": "Project output fully withheld; process metadata only.",
                           "observation": observation}, indent=2) + "\n"

    def quality_runs(self, *, repository: str | None = None, limit: int = 30) -> list[dict]:
        """Latest run per repository, bounded and read-only; no Git/process calls."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Quality list limit must be between 1 and 100")
        where, args = "", []
        if repository is not None:
            where = " WHERE repository=?"
            args.append(normalize_path(check_path(repository)))
        with self._connect() as con:
            con.execute("BEGIN")
            rows = con.execute(
                "SELECT * FROM quality_runs WHERE id IN (SELECT MAX(id) FROM quality_runs"
                + where + " GROUP BY repository) ORDER BY id DESC LIMIT ?", (*args, limit),
            ).fetchall()
            results = []
            for row in rows:
                result = self._quality_public(con, row)
                previous = con.execute(
                    "SELECT * FROM quality_runs WHERE repository=? AND id<? ORDER BY id DESC LIMIT 5",
                    (row["repository"], row["id"]),
                ).fetchall()
                for older in previous:
                    projected = self._quality_public(con, older)
                    result["history"].append({"run_id": older["id"],
                                              "timestamp": projected["timestamp"],
                                              "status": projected["overall_status"],
                                              "note": projected["commit_sha"]})
                results.append(result)
            return results

    def quality_get(self, run_id: int) -> dict | None:
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("Quality run id must be a positive integer")
        with self._connect() as con:
            con.execute("BEGIN")
            row = con.execute("SELECT * FROM quality_runs WHERE id=?", (run_id,)).fetchone()
            return self._quality_public(con, row) if row else None
