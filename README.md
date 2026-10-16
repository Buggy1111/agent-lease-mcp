# agent-lease-mcp

<!-- mcp-name: io.github.Buggy1111/agent-lease-mcp -->

Keep two coding agents (Claude Code and Codex, or two sessions of either) from
overwriting each other's work in the same repository.

agent-lease gives them **leases on paths, presence and a shared message log** —
and the lease is **enforced by a PreToolUse hook**, not by good intentions.
An agent that tries to edit a file another agent is working on is refused before
the edit happens.

[Česky / Czech version](README.cs.md)

## Why it exists

9 September 2026. A coordination file, `docs/AGENT-HANDOFF.md`, existed and both
agents knew about it. At 22:28 they still both touched `deploy/make-work-package.sh`:
one was editing it while the other was running it. Bash reads a script as it goes,
the text shifted under the running process, and instead of a 3.7 GB installer the
build produced a useless 198 MB one.

The lesson the whole project rests on: **coordination that can be ignored will be
ignored.** Not out of malice — an agent at work simply does not look at the notice
board.

The same collision was later replayed on purpose: Codex ran the script, Claude Code
tried to edit it mid-run, and the hook refused the edit. The file stayed intact.

## How it differs

| | locks | enforcement | TTL | presence | messages | audit |
|---|---|---|---|---|---|---|
| [AgentRoom](https://arxiv.org/html/2608.23740v1) (paper) | ✅ | prompt only | ? | ✅ | ✅ | ✅ |
| [Agent Claim MCP](https://glama.ai/mcp/servers/vk0dev/agent-claim-mcp) | ✅ | prompt only | ✅ | ❌ | ❌ | ❌ |
| [agent-orchestration](https://github.com/madebyaris/agent-orchestration) | ✅ | prompt only | ❌ | ✅ | ✅ | ❌ |
| **agent-lease-mcp** | ✅ | **PreToolUse hook** | ✅ | ✅ | ✅ | ✅ |

Advisory locks get broken; AgentRoom documents this itself. Claude Code and Codex
both have PreToolUse hooks with the same semantics (exit 2 = block, reason on
stderr), so a lease can be enforced deterministically at the tool-call level. One
script serves both clients.

## Rules it is built on

- **A lease, not a lock.** 30-minute TTL. A crashed agent cannot block the repo
  forever.
- **All or nothing.** A claim on several paths succeeds completely or not at all.
- **A lease on a directory covers everything under it.**
- **A free file is claimed automatically** on the first edit, so nobody has to
  remember to call `claim`.
- **A running script is leased for the duration of the run** — exactly the
  combination that broke the build.
- **Fail-open by default.** Broken coordination must not stop work; otherwise the
  first thing anyone does is switch the hook off. `AGENT_LEASE_FAIL_CLOSED=1`
  flips this for unattended runs.

## What runs on its own

| hook event | what happens |
|---|---|
| **SessionStart** | the room state is injected into context: who is here, what they hold, messages for you |
| **UserPromptSubmit** | new messages are delivered once, via a cursor |
| **PreToolUse** (Edit/Write/apply_patch…) | an edit of a file someone else holds is refused |
| **PreToolUse** (Bash) | a script being run is leased; obvious writes (`>`, `tee`, `sed -i`, `cp`/`mv` target) into a held file are refused |
| **Stop / SessionEnd** | all leases are returned when the agent finishes its turn |

Injections stay silent when there is nothing to say.

## Tools

| MCP tool | purpose |
|---|---|
| `claim(paths, purpose, ttl_seconds)` | reserve ahead — typically a directory before running a script |
| `release(paths?)` | give back; without arguments returns everything you hold |
| `owner(path)` | who holds this path (including through a directory lease) |
| `room(status?)` | one call per checkpoint: who is here, what is held, what is new |
| `say(text)` / `inbox(since_id)` | messages to the room |
| `send` / `jobs` / `next_task` / `ack` / `retry` / `cancel` | addressed messages and durable tasks with delivery state |
| `accept(message_id, note)` | acknowledge you are taking an addressed message — the sender sees it |
| `report_limit(minutes, reason)` | out of quota: your unstarted tasks move to a fallback agent |
| `history(path?)` | who touched what and how it ended — the answer to "why was I blocked" |

The same operations exist as a CLI (`agent-lease room|claim|release|say|send|…`),
because Codex in non-interactive mode cannot approve MCP calls but can run Bash.
`agent-lease wait --for <agent>` blocks without spending model tokens and wakes a
background session when a message arrives.

`say`/`inbox` is not a live chat: MCP is pull, the other side reads a message when
it next looks. For a live view there is a local web room:
`agent-lease web --open` (loopback only, see [Security](#security)).

## Quick start (two minutes)

```bash
uv tool install agent-lease-mcp
agent-lease init claude-code --write --rewake   # writes all hooks, keeps a .bak backup
agent-lease init codex --write
agent-lease simulate                            # 2 s: replays a full scenario in a temp DB
agent-lease doctor                              # checks it really works
agent-lease web --open                          # live room in the browser
```

`init` without `--write` only shows what it would write. Manual setup below.

## Install

```bash
uv tool install agent-lease-mcp
```

This puts `agent-lease-mcp`, `agent-lease`, `agent-lease-guard`,
`agent-lease-context` and `agent-lease-release` on your `PATH`.

**MCP server** (stdio; each client runs its own process, the shared state is a
SQLite file):

```bash
# Claude Code
claude mcp add agent-lease -e AGENT_NAME=claude-code -- agent-lease-mcp
```

```toml
# Codex — ~/.codex/config.toml
[mcp_servers.agent-lease]
command = "agent-lease-mcp"
env = { AGENT_NAME = "codex" }

# Codex writes to ~/.agent-lease, outside the workspace — allow it:
[sandbox_workspace_write]
writable_roots = ["/home/YOU/.agent-lease"]
```

**Hooks — without them this is just a polite notice board:**

```json
// ~/.claude/settings.json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
      "hooks": [{ "type": "command", "command": "AGENT_NAME=claude-code agent-lease-guard" }]
    }],
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "AGENT_NAME=claude-code agent-lease-context" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "AGENT_NAME=claude-code agent-lease-context" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "AGENT_NAME=claude-code agent-lease-release" }] }]
  }
}
```

```json
// ~/.codex/hooks.json — same events, AGENT_NAME=codex
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "*",
      "hooks": [{ "type": "command", "command": "AGENT_NAME=codex agent-lease-guard" }]
    }]
  }
}
```

`AGENT_NAME` must match between the server and the hooks, otherwise an agent
blocks its own files. An agent called `unconfigured-*` in `room` means exactly that.

| variable | default | purpose |
|---|---|---|
| `AGENT_NAME` | `unconfigured-<host>-<pid>` | name in the room |
| `AGENT_LEASE_DB` | `~/.agent-lease/room.db` | where the state lives |
| `AGENT_LEASE_TTL` | `1800` | default lease length (s) |
| `AGENT_LEASE_RETENTION_DAYS` | `30` | messages and audit older than this are deleted (`0` = keep) |
| `AGENT_LEASE_FAIL_CLOSED` | off | `1` = block the tool call when the hook itself fails |
| `AGENT_LEASE_HUMAN` | `human` | the human's name in the room (stored next to the DB by the web UI) |
| `AGENT_LEASE_TASK_SENDERS` | human + configured agents | who may create tasks (comma-separated) |
| `AGENT_LEASE_FALLBACKS` | — | failover chains, e.g. `codex=claude-code;claude-code=codex` |

### Optional: Bash safety gate

A separate PreToolUse hook (`agent-lease-jev-guard`) for a different question —
not "who holds this file" but "is this command destructive". Hard rules
(`rm -rf /`, force push, `git reset --hard`, `curl | sh`, `mkfs`…) apply as soon
as it is wired in. An optional external classifier (TypeSafe Jev) is off by
default, starts in shadow mode, receives only redacted commands and is advisory —
never the only safeguard. Details in [ADR 0003](docs/adr/0003-jev-samostatny-guard.md).

## Receipts, failover, bridge and broker

- **Every addressed message has a visible state** ⏳ → 👁 seen → ✔ taking → done.
  "Seen" is written by the hook automatically; "taking" must be acknowledged by
  the agent (`accept`) and is reminded on every prompt. Nothing gets lost
  silently. `agent-lease overdue` lists what nobody acknowledged.
- **Limits and failover.** When an agent runs out of quota (`agent-lease limit`
  or MCP `report_limit`), its unstarted tasks move to the next agent in
  `AGENT_LEASE_FALLBACKS` (e.g. `codex=claude-code;claude-code=codex`) after a
  grace period, and the sender is told.
- **Bridge** (`agent-lease bridge --agent codex`) — an autonomous worker: takes a
  task, acknowledges it, runs `codex exec` / `claude -p` read-only and returns the
  result. `--provider openrouter --model <model>` sends it to any model through
  OpenRouter as plain text without tools (read-only; good for review and a
  second opinion). Keys only from the environment.
- **Broker** (`agent-lease broker`) — single SQLite writer over a `0600` Unix
  socket; other tools use it automatically when it runs, so a sandboxed Codex
  does not need direct access to the database.
- **Overview panel** in the live room: each agent's active task, queue,
  unacknowledged items, limits and items to review. `agent-lease notify --loop`
  sends to Telegram only what waits on the human, each event at most once
  (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` from the environment; `--dry` to test).
- systemd units in `deploy/systemd/`, Windows/WSL starter in `deploy/windows/`.

Details (in Czech): [receipts](docs/ZPRAVY-A-POTVRZENI.md),
[limits and failover](docs/LIMITY-A-FAILOVER.md), [Jev](docs/JEV.md).

## Security

agent-lease is a coordination tool for agents running **under one local user
account**. What that means in practice:

- **Local trust model.** Agent identity is the `AGENT_NAME` environment variable.
  Any process running as your user can claim to be any agent. The only reserved
  identity is the human one (`michal` in this build), which only the authenticated
  web UI can use. This protects against agents tripping over each other, not
  against a hostile process on your account.
- **State is owner-only.** `room.db` (and its WAL/SHM files) is `0600` in a `0700`
  directory; existing installs are tightened on start.
- **Peer messages are untrusted data.** In model context every message carries its
  kind, sender and recipient, is folded onto one line and is introduced with a note
  that it is information from a peer, not an instruction from the human. A task
  from another agent is a request, not an order.
- **Tasks are bound to the caller.** A peer sees only tasks it sent or received;
  lease tokens only for its own deliveries; retry/cancel need the sender or the
  recipient.
- **Web UI** binds to loopback only, exchanges a one-time URL-fragment token for an
  HttpOnly, SameSite=Strict session cookie, checks Origin and Host, and caps
  concurrent event streams.
- **Bounds and retention.** Message text, short fields, list limits and task lease
  length are bounded; old messages and audit entries expire, unfinished tasks never.

Known limits, stated plainly:

- **Shell commands are checked best effort.** Redirects, `tee`, `sed -i`,
  `truncate`, `cp`/`mv` targets, wrappers like `sudo`/`env` and `sh -c` are
  recognised. `eval`, variables in paths and arbitrary programs are not. This
  catches mistakes, not an agent that is trying to evade the hook.
- **Check-then-use gap.** The hook checks a path before the client opens it. A
  symlink swapped in between is not caught; a hook cannot bind the later file open
  to the checked inode.
- **One machine.** The state is a file on disk.

Found something? Please report it privately via
[GitHub Security Advisories](https://github.com/Buggy1111/agent-lease-mcp/security/advisories/new).

## Development

```bash
uv sync --extra dev
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

Design decisions: [ADR 0001](docs/adr/0001-vynuceni-hookem.md) (hook enforcement),
[ADR 0002](docs/adr/0002-sqlite-a-stdio.md) (SQLite and stdio),
[ADR 0003](docs/adr/0003-jev-samostatny-guard.md) (separate Bash safety gate) — in Czech.

Built by Michal with Claude Code and Codex — the two agents it coordinates.

MIT licence.
