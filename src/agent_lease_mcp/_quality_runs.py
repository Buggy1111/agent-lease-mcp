"""B7 run evidence. Only an in-process runner can write; broker clients can read.

The socket is trusted to the same OS user, as elsewhere in Store. This module
does not authenticate that user's Python code or turn a supplied exit code into
proof of execution. The future isolated runner must call the private writer
after collecting observations itself, never from a client-supplied report.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from .hygiene import check_path
from .policy import normalize_path
from .quality import COMMAND_CHECKS, Check, Inspection, Status, aggregate, inspect_repository


@dataclass(frozen=True)
class CommandObservation:
    """Process metadata only. Raw output, arguments and arbitrary verdicts stay out."""

    check: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    output_bytes: int
    output_sha256: str

    def __post_init__(self) -> None:
        if self.check not in COMMAND_CHECKS:
            raise ValueError("Only configured command checks have process observations")
        if type(self.timed_out) is not bool:
            raise ValueError("Invalid process timeout flag")
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
    if observation.timed_out:
        return Status.FAIL, "Command exceeded the runner time limit."
    if observation.exit_code != 0:
        return Status.FAIL, "Configured command exited unsuccessfully."
    if observation.check == "coverage":
        return Status.REVIEW, "Coverage command succeeded; a validated numeric metric is not imported."
    return Status.PASS, "Configured command exited successfully."


class QualityRunsMixin:
    def _quality_record_run(self, agent: str, inspection: Inspection,
                            observations: tuple[CommandObservation, ...], *,
                            execution_id: str) -> dict:
        """Private runner sink, deliberately absent from the broker/MCP write API.

        Re-inspect immutable Git objects; no raw commands/output are persisted.
        Retries of the same execution are idempotent, conflicting reuse rejected.
        This method itself never starts a process or creates an execution claim.
        """
        self._check_review_agent(agent)
        if (not isinstance(execution_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", execution_id)):
            raise ValueError("Invalid execution identity")
        if type(inspection) is not Inspection:
            raise TypeError("An inspection is required, not a submitted JSON report")
        current = inspect_repository(inspection.repository, inspection.commit_sha,
                                     inspection.base_commit or None)
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
        for check in inspection.checks:
            if check.name in by_name:
                status, detail = _command_result(by_name[check.name])
                check = replace(check, status=status, detail=detail)
            checks.append(check.public() | {"name": check.name})
        # Explicit allowlist: Inspection.public() is a plan, never the stored run.
        evidence = {"repository": inspection.repository, "commit_sha": inspection.commit_sha,
                    "base_commit": inspection.base_commit, "agent": agent,
                    "language": inspection.language, "config_digest": inspection.config_digest,
                    "policy_paths": list(inspection.policy_paths), "checks": checks,
                    "observations": [asdict(by_name[name]) for name in sorted(by_name)]}
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
        # Approval is read from the existing B6 authority at read time. A new
        # review/re-submit invalidates it; neither caller nor project sets it.
        review = con.execute(
            "SELECT * FROM reviews WHERE repo=? AND head_commit=? AND base_commit=? "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (row["repository"], row["commit_sha"], evidence["base_commit"]),
        ).fetchone()
        independent = bool(review and review["verdict_by"]
                           and review["verdict_by"] != review["author"]
                           and review["verdict_by"] != evidence["agent"]
                           and review["verdict_commit"] == row["commit_sha"])
        approved = bool(independent and review["state"] in ("approved", "closed")
                        and review["verdict"] == "approved" and review["security"] == "pass"
                        and review["security_by"] == review["verdict_by"]
                        and review["scan_commit"] == row["commit_sha"]
                        and review["scan_findings"] == "[]")
        security_failed = bool(independent and review["security"] == "fail"
                               and review["security_by"] == review["verdict_by"])
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
                    item["detail"] = "Independent B6 security review rejected this commit."
            public_checks[name] = item
            checks.append(Check(name, Status(item["status"]), item["required"], item["detail"]))
        status = aggregate(tuple(checks), peer_review="approved" if approved else "pending",
                           policy_review_required=bool(evidence["policy_paths"]))
        return {"schema_version": 1, "kind": "quality_run", "executed": True,
                "run_id": row["id"], "execution_id": row["execution_id"],
                "repository": row["repository"], "commit_sha": row["commit_sha"],
                "base_commit": evidence["base_commit"] or None,
                "branch": None, "agent": evidence["agent"],
                "timestamp": datetime.fromtimestamp(row["recorded_at"], UTC).isoformat(),
                "language": evidence["language"], "config_digest": evidence["config_digest"],
                "checks": public_checks, "observations": evidence["observations"],
                "overall_status": status.value,
                "policy_review_required": bool(evidence["policy_paths"]),
                "policy_paths": evidence["policy_paths"],
                "reviewer": review["verdict_by"] if independent else None,
                "review_status": "approved" if approved else "pending",
                "review_id": review["id"] if review else None,
                "logs": [], "history": [], "evidence_sha256": row["evidence_sha256"]}

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
