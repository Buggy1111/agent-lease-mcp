# Changelog

## Unreleased

### Fixed

- Preserve SQLite busy/locked errors across the broker boundary so the bridge
  can back off without replaying a request. Other database errors remain errors.

### Security

- Require FastMCP 3.4.10 or later in the 3.x series to address
  GHSA-7xgj-8392-5q4r; add an in-process authorization regression test.
- Require patched Authlib, certifi, dnspython and joserfc versions, including
  fixes for GHSA-6v38-4h7w-rj9h and GHSA-6q48-6c7w-9gw2. Add regression tests
  for unsigned JWS acceptance and the compromised CA root.

### Added

- Phase A safety work (not yet complete; see process supervision limitations):
  session-owned task acceptance; lease deadline checks everywhere; atomic
  `complete()` (state + result in one transaction) and `defer()` after a provider
  limit; worker process groups killed and confirmed; broker-side `reconcile()`;
  quarantine enforced for the bridge with a human-only approval (web,
  bound to text, recipient and one attempt); emergency stop (`emergency_stop`
  MCP tool, `agent-lease stop`, web; resume only in the web UI); task
  `checkpoint()` with a content fingerprint of the workspace; verified daily DB
  backup (`agent-lease backup`) while a fixed SQLite is not available for Python.
- `tests/chaos/run_chaos.py`: isolated chaos test (killed workers and broker,
  cancels, competing sessions, pause) checking effects, recorded process IDs,
  stuck leases, false "Done" messages, and database integrity. It does not yet
  prove that every escaped descendant is terminated.
- Opt-in delegated cgroup containment and restart recovery for bridge commands,
  guarded by a persistent fence and verified group identity. An independent
  watchdog also cleans the group after bridge death, and a watchdog failure
  stops the command. If every guard dies, cleanup waits for recovery after a
  restart. This is not enabled in deployed services. See
  `docs/PHASE_A_PROCESS_SUPERVISION.md`.

- `wait(message_id, timeout_seconds)` MCP tool: blocks until the counterpart
  answers or the task reaches a final state, so a sender no longer polls `jobs`.
  Automatic "seen"/"taking" receipts do not end the wait; only the counterpart
  of the message can (a reply from anyone else is ignored).
- Chats nobody handled within 48 h (`AGENT_LEASE_CHAT_EXPIRE_HOURS`) are closed
  as `expired` by the broker; the sender of an unread one gets a short notice.
  Tasks are never expired this way (they may wait for a limit to end).

- Wake-up without a human: Claude Code via `asyncRewake` hooks
  (`init --rewake`), Codex via the `agent-lease-codex-wake` service, which
  nudges a named session (`codex queue`) for chats, results and tasks and, after
  a provider limit ends, once per episode for unfinished work.
- `doctor` checks the Codex wake service and its named session.
- `doctor` warns when a broker, bridge or wake service is crash-looping
  (more than 3 automatic restarts).
- Tasks can declare `--path` worksets and a `--priority`; the scheduler skips
  tasks whose paths are held by another agent, and the board shows the blocking
  path, owner and TTL.
- `worker-activate` / `failover-activate` set safe baselines without running or
  moving a task; `provider-sync` / `provider-clear` manage typed provider state;
  `doctor --json` separates delivery, receipt, acceptance, execution, hook
  identity, worker health and failover activation.
- The room shows offline, provider-unknown and provider-limited states with a
  bounded reason.

### Fixed

- Require PyJWT 2.15+ and OpenTelemetry API 1.45.1+; update the lockfile to
  PyJWT 2.15.1 and OpenTelemetry API 1.45.2. Add a regression test for
  GHSA-ffc3-869f-jxw9's malformed-PEM algorithm confusion.
- A process-group guard kills ordinary descendants when the bridge is killed;
  escaped sessions and a crash of the guard still require stronger isolation.
- CLI process launches synchronize with pause and cancellation confirmation;
  a stuck launch is reported as unconfirmed rather than a successful stop.
- Completion retries are idempotent only for the original lease token and exact
  result; stale tokens and differing results no longer receive a false success.
- `room` returned the ten *oldest* messages ever; it now returns the newest,
  shortens long texts to 400 characters and hides agents unseen for over a day
  (`hidden_offline_peers`, `all_peers=True` shows them).

### Changed

- SQLite now requires rollback journal mode `DELETE` to avoid the WAL-reset bug
  in affected SQLite versions. Existing WAL databases require an offline migration
  after stopping all clients; see `docs/AGENT_LEASE_IMPROVEMENTS_TODO.md`.
- Checkpoint fingerprints distinguish staged and unstaged changes, include the
  worktree identity, and fail closed for unreadable files or submodules. A match
  does not establish whether external effects occurred, which must be verified.
- `store.py` split by area into `_claims`, `_messaging`, `_availability`,
  `_deliveries` and `_store_common`; delivery state sets live in `states.py`.
  `Store`'s public API and the broker's `METHODS` are unchanged, guarded by a
  characterisation snapshot test.

- A worker lease and interactive acceptance prevent ordinary concurrent starts.
  External effects and recovery after a process crash still need verification;
  exactly-once execution is not guaranteed.
- Schema migrations are serialised; receipts written by the old bridge are
  backfilled so retried tasks are not skipped.
- Clients wait for a restarting broker instead of crashing the bridges and the
  wake service; a connection lost mid-call is reported as a connection error,
  never repeated, and the worker loops continue with the next call.
- Screening: a bare quoted pattern in a technical report is low risk; a quoted
  pattern carrying any further instruction stays high risk.
- Receipts are recognised only in their exact generated shape; CLI writes
  without `AGENT_NAME` are refused.
- Long-running services set `AGENT_LEASE_REQUIRE_BROKER=1` and always use the
  broker, even when started before it binds its socket; `doctor` reads the room
  through the broker too.
- codex-wake no longer crashes after a provider limit ends: `task_summary` is
  proxied by the broker; a guard test checks every store method broker clients use.
- A limit whose provider reset has passed shows as `recovering`; `jobs` shows
  messages as unread/seen/accepted instead of pending forever.
- Broker: relative paths are resolved in the client, not in the broker's own
  working directory (could protect the wrong file).
- Headless workers use their own identity (`codex-bridge`, `claude-code-bridge`),
  so a worker quota no longer marks the interactive agent as limited.
- Limits: Claude's weekly quota response is recognised; reset times are never
  invented; recovery probes without a provider-supplied reset are off, which
  stops the repeated retry and notification storm.
- Failover skips accepted and historical tasks; an explicit reschedule of a
  pending task is allowed.
- Deploy: the user's Codex binary is visible to the bridge service.
- Bridge: a reported limit (`agent-lease limit`) is honoured even when the
  provider's own status says available.
- Bridge: the first provider-limit check after boot is no longer skipped.
- Bridge: Codex results are its final answer (`--output-last-message`), not the
  whole run transcript.

## 0.2.0 — 2026-10-16

First public release. Two lines of work merged: delivery receipts, limit
failover and an autonomous bridge (PR #1), and hardening after a joint security
review by the two agents the tool coordinates (Claude Code and Codex), followed by
a cross-review of each other's fixes.

### Added

- **Delivery receipts** — an automatic "seen" when a message reaches the
  recipient's context and a required "taking" (`accept`), reminded on every
  prompt until given; `agent-lease overdue` lists what nobody acknowledged.
- **Fast wake-up** — signal files make `wait` react within ~0.2 s;
  `wait --rewake` is meant for an asyncRewake hook.
- **Limits and failover** — `agent-lease limit` / MCP `report_limit`; unstarted
  tasks of an agent that ran out of quota move along `AGENT_LEASE_FALLBACKS`
  after a grace period; sleeping agents are not robbed.
- **Bridge** — an autonomous worker for one agent's queue (`codex exec`,
  `claude -p` or OpenRouter) with an hourly cap, rate-limit back-off and lease
  heartbeat.
- **Broker** — optional single SQLite writer over a `0600` Unix socket, picked
  automatically when running.
- `agent-lease init` (writes hooks idempotently, with a backup), `doctor`,
  `simulate` (two-second post-install check), `notify` (Telegram), `prune`.
- Live room: overview panel with each agent's active task, queue and limits;
  receipt states on messages.
- Optional Jev integration for routing and screening (off by default).

### Security

- **Race between check and claim** — two agents editing the same free file at the
  same moment could both be allowed. The atomic claim result now decides, and an
  agent's own lease is renewed atomically on every write.
- **Shell writes** — obvious writes into a file another agent holds (`>`, `>>`,
  `tee`, `sed -i`, `truncate`, `cp`/`mv`/`install` destination including `-t`,
  through `sudo`/`env`/`nohup` and `sh -c`) are refused. Best effort, documented.
- **Peer messages in model context** are labelled with kind, sender and recipient,
  folded onto one line, and introduced as information from a peer, not
  instructions from the human. All peer-supplied metadata is folded too.
- **Database files** are owner-only (`0600` in a `0700` directory, WAL/SHM
  included); existing installs are tightened on start.
- **Tasks are bound to the caller** — a peer sees only tasks it sent or received,
  lease tokens only for its own deliveries; retry/cancel need sender or recipient.
- **Human identity** — the reserved human name is stored next to the database and
  cannot be released by an agent's environment.
- **Web UI** — the token no longer travels in URLs or stdout: a URL-fragment token
  is exchanged for a random, expiring, HttpOnly SameSite=Strict session cookie;
  Origin and Host are checked; event streams are capped and time out.
- **Bounds and retention** — message, status and purpose length, list limits and
  task lease length are bounded; messages and audit older than 30 days are
  removed (`AGENT_LEASE_RETENTION_DAYS`), unfinished tasks never.
- **Optional fail-closed mode** — `AGENT_LEASE_FAIL_CLOSED=1`.
- **Bash safety gate** (optional, separate hook) — commands are redacted before
  logging or classification, the log is owner-only, hard rules cover common
  spellings of `rm -rf /` and force push.
- Development dependency `pytest` upgraded past GHSA-6w46-j5rx-g56g.

### Changed

- English hook messages, room briefing, CLI output and MCP tool descriptions.
- `README.md` is English; the Czech original is `README.cs.md`.
- `agent-lease web --open` opens the browser with the login fragment.

## 0.1.0 — 2026-09-11

Internal: hook-enforced leases, presence, messages, addressed tasks, local web room.
