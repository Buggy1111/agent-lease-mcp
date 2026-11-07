# B7 — activation plan (not executed)

Production remains runtime `9134485371fdd6d1c92c16562aa04d188c3f2125`.
Michal explicitly authorized publishing the B7 candidate from
`nox/b7-mvp-20261107` to `main`. That publication does not activate production.
Phase B remains open until both Ada and Nox approve after Ada's capacity returns.
Global hook changes, persistent service changes and production activation still
require their own explicit owner approval.

1. Finish the exact candidate's automated validation and record any failures.
   Obtain independent review; Nox authored this code and cannot self-approve.
   Deterministic tests continue while Ada is limited; send her no notifications.
2. Prepare the final wheel/sdist, source identity, hashes and third-party tool
   notices. Reuse isolated local tool dependencies; install nothing globally.
   Initially allow only Agent Lease. Earth Pulse, LILI and Silikon Manager follow
   after the core is stable, with separate project opt-in and pinned dependencies.
3. Obtain Michal's explicit approval for the concrete production candidate and
   service/operator configuration. The session's AGENTS instructions require
   this for production deployment, systemd services and agent configuration.
4. Before switching runtime, pause through the existing maintenance procedure,
   back up database/config/queue, record hashes and integrity, and stage a new
   immutable runtime directory. Copy pinned tool trees into the reviewed runtime
   location before enabling the policy; the development scratch toolchain is not
   a production installation. Recalculate and compare tree/executable pins. Do not overwrite runtime-9134485 or discard jobs.
5. Apply the [reviewed broker limit drop-in](../deploy/quality/broker-limits.conf.example) (`MemoryMax=2G`, `CPUQuota=100%`,
   `TasksMax=256` in `[Service]`). Confirm effective ancestor limits. Without
   these, the runner intentionally returns SETUP REQUIRED. Avoid a second worker
   or increasing permissions. Stage a private owner-only operator policy beside
   the broker DB, first with automatic execution disabled. Pin the reviewed
   Python/site trees and executables, repository allowlist and time budget.
6. Switch all five entrypoints consistently under the established deployment
   procedure; check broker/bridge/wake/web health, DB integrity and unchanged
   message/task queue. Restart client MCP connections after the runtime switch;
   they must spawn the new executable. Web login sessions may require refreshing
   authentication. Never print tokens. Human alone resumes a maintenance pause.
7. Run an exact-commit manual check and inspect dashboard/history/log metadata.
   Independently approve code and obtain human policy approval through the
   existing authenticated UI. Then enable operator automatic enqueue. Confirm
   both clients' existing Stop hooks are trusted and enqueue once, including
   restart, cancellation and policy-change refusal. A simulated role fixture is
   not a live Claude/Codex acceptance run.
8. Obtain separate GitHub owner approval for publishing and branch protection /
   required checks or a controlled release workflow. The inspected main branch
   is unprotected. Until real enforcement is wired, dashboard FAIL cannot block
   a manual GitHub merge. Do not mark this as enforced.

Rollback: disable automatic execution, pause, stop the candidate using the
approved maintenance procedure, preserve diagnostic DB/queue backups, restore
previous runtime entrypoints and the reviewed previous service/policy settings.
Restore a database backup only after confirming no newer tasks will be lost.
Check integrity and exact queue state, reconnect MCP/web clients, let the human
resume. Never silently replay a request interrupted during restart.

Phase B stays open until required acceptance, independent review and the approved
activation/enforcement work are complete. No automatic transition to C.
