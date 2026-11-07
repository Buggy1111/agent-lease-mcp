# B7.2 — isolated Quality Gate runner

Local implementation following the GitHub `TODO.md` and improvements roadmap.
This supersedes the execution limitations in the historical B7.1 and B7.2a
reports. It is not a deployment record or independent approval.

## Running checks

`agent-lease quality inspect --repo PATH` reads committed metadata without
executing it. `agent-lease quality check --repo PATH --commit FULL_SHA --base
BASE_SHA` asks the existing broker to run the committed commands. Exit codes
are 0 PASS, 1 FAIL, 2 REVIEW/SETUP REQUIRED/unknown reply. Use
`--task-id ID --lease-token TOKEN` for an owned, started task; the token is never
persisted in evidence. `quality enqueue` submits a deduplicated request and
returns without waiting for expensive checks. Its success means acceptance,
not a passing gate.

Python and TypeScript/JavaScript adapters inspect exact Git objects and CI or
explicit `.agent-lease/quality.toml`. Unknown commands/frameworks require setup;
there is no guessed command execution or dependency download. Repositories can
opt out with `enabled=false`. Project configuration does not grant execution:
the separate operator policy must allow the canonical repository and pin tools.

The broker reads private `quality-runner.toml` beside its database (owner-only
0600, no symlinks). It is disabled by default. Version 1 requires `enabled=true`,
`repositories=["/canonical/repository"]`, pinned `[tools.NAME]` entries with
`path` and `sha256`, and optional `[mounts.NAME]` entries with `path` and
`tree_sha256`. Supported mount names include `python`, `site`, `typescript`,
and `node_modules`. All mounts are read-only. Pins cover the complete tree;
project code cannot select an arbitrary host mount. No package installation is
performed. The Agent Lease build needs a pinned build frontend, Hatchling and
its dependencies because its command uses `build --no-isolation` offline.

## Isolation and cancellation

Linux requires working unprivileged user/network/mount/PID namespaces, bubblewrap,
`ip`, and effective cgroup limits at most 2 GiB memory, one CPU, 256 tasks.
Unsupported hosts fail closed before executing project code. Per-command timeout
is operator-controlled (1–600 seconds), the complete job has a 720-second budget,
and a room lock prevents concurrent jobs multiplying that budget.

Export uses exact committed blobs, ignores the mutable checkout, and rejects
links, submodules and special paths. Snapshot limits are 2,000 files/64 MiB and
30 seconds preparation. Each command gets a fresh writable tmpfs copy; the
source snapshot and tools remain read-only. Only private loopback works; host
HOME, credentials, broker sockets, external network and cgroup controls are
absent. A synthetic passwd entry allows tools to resolve HOME without inheriting
host configuration. Namespace root maps to the invoking host user; capabilities
are dropped before project execution.

Pause, shutdown, task cancellation/expiry/lost ownership and job deadline are
polled while commands run. Parent-death signals and PID namespaces terminate
processes even after `setsid`, normal daemonization or supervisor SIGKILL.
Output is bounded at 1 MiB. Timeout fails the command; cancellation/output cap
requests review. Interrupted requests are not silently replayed after restart.

## Evidence, metrics and authority

The supervisor persists process metadata, bounded-output digest and exact repo,
base/head, branch-at-start, agent, task, time and operator-policy digest. There is
no broker API for importing caller-supplied outcomes. Raw command output never
leaves the sandbox through the public log endpoint: authenticated log links
show redacted process metadata only. Do not expect project diagnostics there.

Coverage commands can emit `coverage.json` (coverage.py) or
`coverage/coverage-summary.json` (Istanbul). A trusted read-only namespace init
removes stale reports, runs the command without inheriting the metadata FD,
then parses at most 4 MiB JSON without following links/FIFOs. It exports only
validated numeric counts through a private memfd; project processes have no
capabilities and cannot reopen the nondumpable collector's `/proc/1/fd`.
Malformed/missing reports never pass coverage. At least 80% line coverage passes;
lower coverage requests review. This fixed threshold cannot be changed in repo
configuration. Coverage reports are produced by project tests, not cryptographic
proof that tests are honest; changes to tests and gate authority require review.

Python CRAP optionally uses operator-pinned Lizard plus coverage.py. It analyzes
immutable source with AST function ownership, excluding nested functions and
excluded lines. Scores above 30 request review only. Missing attribution/tool
support is N/A. TypeScript Istanbul measures compiled JavaScript lines in the
fixture; no TypeScript source-map CRAP support is claimed. Parent-only coverage
does not imply subprocess coverage. Numeric results contain no source/function
names or file paths.

PASS also requires independent B6 approval and security/clean scan for the exact
repo/base/head. Author cannot approve their own change. Approval revocation is
reflected when reading evidence. Changes to tests, CI, security policy and gate
authority additionally require a direct authenticated human approval scoped to
that exact run, with a reason. This cannot waive failed required checks or
pending independent review. Role-labelled synthetic test rooms do not provide
independent review of this implementation.

## Automatic events and dashboard

With explicit operator `automatic=true`, the existing Stop/release entrypoint
and review submission enqueue clean committed HEAD asynchronously. The existing
broker owns the queue worker; no new LLM worker or service is created. Requests
are deduplicated per agent/repo/head/base/policy. At most 20 are active; policy
changes refuse queued requests rather than executing with changed authority.
Dirty worktrees are not automatically queued. Explicit check still selects
committed objects.

Existing Codex installation uses `.codex/hooks.json`. Codex requires human trust
of hooks via `/hooks`; do not bypass it. Stop is the suitable event for this
short enqueue operation. SessionEnd has a short client budget and is not a
reliable expensive-check trigger. Existing Claude release hooks use the same
queue entrypoint. Global agent configuration has not been edited or activated.

The existing snapshot/SSE transport and Quality Gate renderers display real runs,
selection/history, task binding, typecheck, queue status and safe metadata log
links. Demo rows remain visibly separate. Human approval has confirmation and
requires a reason. The dashboard does not enforce GitHub merges. The inspected
GitHub main branch was unprotected; required checks/release enforcement remain
an explicit owner/admin activation task.

See [activation plan](PHASE_B7_ACTIVATION_PLAN.md) for rollout boundaries.
