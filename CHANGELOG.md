# Changelog

## Unreleased

### Added

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

- A task can no longer run twice: accepting a task held by a worker requires its
  lease token; the bridge yields to an interactive session that accepted it.
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
