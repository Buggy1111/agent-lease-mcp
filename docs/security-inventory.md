# Security inventory

## Scope and evidence

This inventory describes the implemented attack surface of `agent-lease-mcp` at
commit `76419684b960f75b15cd4cd903ab4b097f5e0f2b`. It is an inventory, not a
vulnerability report: the risk column is a review priority based on exposure and
authority, not a claim that a vulnerability exists.

Evidence was collected from tracked source, tests, `pyproject.toml`, `uv.lock`, the
repository history, and the documented WSL runtime. No exploit, dependency/CVE scan,
container, external API, or production system was used in this phase.

Formal component ownership has not been configured: the repository has no
`CODEOWNERS`, and all commits currently visible in the repository are authored as
`Buggy1111`. Therefore every implemented component below lists **Buggy1111 / component
owner not formally assigned**. Claude Code and Codex are implementation/review agents,
not security owners or approval authorities. Michal is the sole approval authority for
security patches, disclosure, and merge.

### Risk scale

| Priority | Inventory meaning |
|---|---|
| High | The component can authorize actions, mutate shared state, expose sensitive local data, or inject content into an agent context. |
| Medium | The component influences security decisions or supply-chain/runtime integrity but has a narrower direct authority. |
| Low | The component is primarily informational or test-only and has no production authority. |

## Services and executable surfaces

| Service / surface | Code and entry point | Owner | What it calls | Data processed | Trust boundary | Risk |
|---|---|---|---|---|---|---|
| FastMCP stdio server | [`pyproject.toml`](../pyproject.toml#L29), [`server.py`](../src/agent_lease_mcp/server.py#L23) → `agent-lease-mcp` | Buggy1111 / unassigned | `FastMCP`, `Settings`, direct `Store`/SQLite calls | agent identities, paths, cwd, claims, messages, task state, lease tokens, errors | MCP client/LLM input → trusted local process → shared DB | High |
| General CLI | [`pyproject.toml`](../pyproject.toml#L31), [`cli.py`](../src/agent_lease_mcp/cli.py#L28) → `agent-lease` | Buggy1111 / unassigned | direct `Store`; can start the web UI | same room, claim, task and audit data as MCP; command-line arguments and environment | local shell/environment → shared DB | High |
| PreToolUse enforcement hook | [`pyproject.toml`](../pyproject.toml#L34), [`guard.py`](../src/agent_lease_mcp/guard.py#L27) → `agent-lease-guard` | Buggy1111 / unassigned | `policy.extract_target`, `policy.script_target`, `Store`; exits `2` to block | hook JSON, tool name, shell command text, paths, holder identity | agent tool request → deterministic policy → allow/block | High |
| Session/prompt context hook | [`pyproject.toml`](../pyproject.toml#L37), [`lifecycle.py`](../src/agent_lease_mcp/lifecycle.py#L33) → `agent-lease-context` | Buggy1111 / unassigned | `Store.undelivered`, briefing renderer, hook JSON output | peer state, claims, paths, messages and status text injected into an LLM context | persisted peer/user content → model context | High |
| Stop/session release hook | [`pyproject.toml`](../pyproject.toml#L39), [`lifecycle.py`](../src/agent_lease_mcp/lifecycle.py#L67) → `agent-lease-release` | Buggy1111 / unassigned | `Store.release_all` | current agent identity and owned claim paths | lifecycle event → deletion of current agent claims | Medium |
| Loopback live-chat HTTP/SSE server | [`pyproject.toml`](../pyproject.toml#L40), [`webui.py`](../src/agent_lease_mcp/webui.py#L645) → `agent-lease-webui` or `agent-lease web` | Buggy1111 / unassigned | stdlib `ThreadingHTTPServer`, direct `Store` | complete room history, recipients, tasks, presence, cwd, claim paths, human-authority token | browser → loopback HTTP → human-authorized DB mutations | High |
| Shared SQLite store | [`config.py`](../src/agent_lease_mcp/config.py#L29), [`store.py`](../src/agent_lease_mcp/store.py#L25) | Buggy1111 / unassigned | Python `sqlite3`; default `~/.agent-lease/room.db`, WAL mode | claims, peers, messages, deliveries, cursors, audit events, delivery lease tokens | multiple local processes → one shared file and Unix-account boundary | High |
| Addressed-message waiter | [`cli.py`](../src/agent_lease_mcp/cli.py#L70), [`cli.py`](../src/agent_lease_mcp/cli.py#L154) → `agent-lease wait` | Buggy1111 / unassigned | polls `Store.addressed` once per second until message/timeout | addressed message content and delivery state | shared DB → harness notification | Medium |

`agent-lease wait` is a foreground polling command, not a daemon, cron job, exclusive
consumer, or task lease. Atomic task ownership is provided only by
`next_task`/`Store.lease_next`.

## HTTP endpoints

The HTTP server rejects non-loopback bind targets in
[`webui.py`](../src/agent_lease_mcp/webui.py#L664). Its default listener is
`127.0.0.1:8765`. Loopback reduces exposure but is not authentication; the browser-facing
routes use a persistent local token.

| Method and path | Handler | Authentication / authorization | Operation and data | Risk |
|---|---|---|---|---|
| `GET /` | `_handle_index` | token in `?token=` query | returns embedded HTML/CSS/JS application containing the token for subsequent calls | High |
| `GET /api/events` | `_handle_events` | token in query; supports `Last-Event-ID` | long-lived SSE stream of messages plus periodic peer/claim state | High |
| `GET /api/snapshot` | `_handle_snapshot` | `X-Agent-Lease-Token` | returns up to 200 messages, peers and active claims | High |
| `POST /api/send` | `_handle_send` | token header and Origin allowlist | creates chat/task/control/broadcast as reserved human identity `michal` | High |
| `POST /api/retry` | `_handle_retry` | token header and Origin allowlist | moves eligible failed delivery back to `pending` | High |
| `POST /api/cancel` | `_handle_cancel` | token header and Origin allowlist | cancels an unfinished addressed delivery | High |

Relevant controls already present, to be tested rather than assumed in later phases:

- constant-time token comparison;
- token file creation with mode `0600` and parent directory repair to `0700`;
- explicit Origin allowlist for POST operations;
- 8 KiB request-body limit;
- CSP, `X-Content-Type-Options`, `X-Frame-Options`, no-referrer and no-store headers;
- HTML/DOM escaping of rendered room-controlled values;
- loopback-only host validation.

The token is also placed in the initial URL, embedded into page JavaScript and printed
to stdout. Token lifecycle, browser/terminal history, rotation, rate/resource limits,
timeouts and multi-client SSE behavior are later review surfaces, not findings from this
inventory phase.

## MCP tools

The MCP transport is stdio (`mcp.run()`), so these are tool endpoints rather than HTTP
routes. Arguments originate in an LLM client and are untrusted.

| Tool | Code | Reads / writes | Sensitive capability or data | Risk |
|---|---|---|---|---|
| `claim` | [`server.py`](../src/agent_lease_mcp/server.py#L28) | writes claims and heartbeat | reserves filesystem paths for an agent | High |
| `release` | [`server.py`](../src/agent_lease_mcp/server.py#L48) | deletes owned claims | changes coordination enforcement state | High |
| `owner` | [`server.py`](../src/agent_lease_mcp/server.py#L63) | reads claims | exposes path and holder metadata | Medium |
| `say` | [`server.py`](../src/agent_lease_mcp/server.py#L70) | writes broadcast message | content may later enter another model context | High |
| `send` | [`server.py`](../src/agent_lease_mcp/server.py#L83) | writes addressed delivery | content, recipient, kind, dedupe/reply relation | High |
| `jobs` | [`server.py`](../src/agent_lease_mcp/server.py#L100) | reads delivery history | may expose active delivery lease token | High |
| `next_task` | [`server.py`](../src/agent_lease_mcp/server.py#L107) | atomically leases task | grants task ownership and returns lease token | High |
| `ack` | [`server.py`](../src/agent_lease_mcp/server.py#L129) | transitions delivery state | commits result using recipient identity and lease token | High |
| `retry` | [`server.py`](../src/agent_lease_mcp/server.py#L139) | transitions failed delivery | re-enables execution of a task | High |
| `cancel` | [`server.py`](../src/agent_lease_mcp/server.py#L149) | transitions unfinished delivery | prevents future execution | High |
| `inbox` | [`server.py`](../src/agent_lease_mcp/server.py#L159) | reads messages | returns room content after caller-supplied cursor | Medium |
| `room` | [`server.py`](../src/agent_lease_mcp/server.py#L166) | reads room; writes heartbeat/status | returns peers, claims and recent messages | Medium |
| `history` | [`server.py`](../src/agent_lease_mcp/server.py#L183) | reads audit log | exposes paths, actors and actions | Medium |

The configured `AGENT_NAME` is local identity metadata, not cryptographic authentication.
The reserved literal `michal` is remapped for CLI/MCP/hook processes in
[`config.py`](../src/agent_lease_mcp/config.py#L38), while the authenticated web handler
sets the human identity internally.

## CLI commands

The CLI exposes `room`, `claim`, `release`, `owner`, `say`, `send`, `jobs`, `ack`,
`wait`, `retry`, `cancel`, `web`, and `history`. It shares `Store` directly with the MCP
server and web process. It has no separate authentication beyond the local Unix account
and environment-derived agent identity. Commands accepting `--for` or `--to` can select
a recipient, which is a later authorization/IDOR review surface.

## Hooks, timers, cron, CI and deployment jobs

| Trigger / job | Implemented? | Location | Behavior |
|---|---|---|---|
| Claude/Codex `SessionStart` | Yes | `agent-lease-context` | heartbeat plus room briefing injection |
| `UserPromptSubmit` | Yes | `agent-lease-context` | heartbeat plus undelivered-message injection |
| `PreToolUse` | Yes | `agent-lease-guard` | path extraction, auto-claim or deterministic block |
| `Stop` / `SessionEnd` | Yes | `agent-lease-release` | releases current agent claims |
| Repository cron | No | none | no cron definition is tracked |
| systemd service/timer | No | none | mentioned only as future architecture |
| GitHub Actions | No | no `.github/workflows` | no CI or scheduled security scan is tracked |
| Docker/Compose job | No | none | no Dockerfile or Compose definition is tracked |
| Broker/Unix socket | No | `TODO.md`/design only | planned, not part of the current attack surface |
| Claude/Codex autonomous adapter | No | design only | planned, not implemented |

Local Claude/Codex hook configuration lives outside this repository and is an external
deployment concern. The repository documents example configurations, but does not own
or deploy them automatically.

## Data inventory and classification

| Data | Storage / flow | Classification | Notes |
|---|---|---|---|
| Human and agent message/task text | SQLite `messages`; web/MCP/CLI output; model context | Potentially confidential, untrusted content | may contain code, paths, accidental secrets, PII or adversarial instructions |
| Agent names and recipients | SQLite `agents`, `messages`, `deliveries` | Internal identifier | not a cryptographic principal by itself |
| Absolute claim/audit paths and cwd | SQLite `claims`, `agents`, `audit` | Sensitive local metadata | reveals project/user directory structure |
| Presence/status and error text | SQLite `agents`, `deliveries` | Internal, untrusted content | may enter UI, CLI output or model context |
| Web authority token | `~/.agent-lease/webui.token`, browser URL/page memory, server stdout | Secret credential | grants ability to act as reserved human identity in the local web API |
| Delivery lease token | SQLite `deliveries`; MCP/CLI job results | Secret capability | authorizes task state transitions while a lease is active |
| Audit and cursor state | SQLite `audit`, `cursors` | Internal integrity metadata | supports attribution and message-delivery position |
| Package author name/e-mail | `pyproject.toml` | Public PII | intentionally published package metadata unless the maintainer says otherwise |
| Payment/customer records | none | Not applicable | no such schema or connector exists |

## Trust boundaries and principal data flows

1. **Browser boundary:** browser-controlled request → loopback HTTP handler → token and
   Origin checks → shared SQLite state.
2. **MCP boundary:** LLM-produced tool arguments → FastMCP validation → direct store
   operation.
3. **Hook enforcement boundary:** agent tool JSON → path/script extraction → claim policy
   → allow or exit `2`.
4. **Agent communication boundary:** human/peer message → SQLite → lifecycle briefing →
   another LLM's context. Content remains untrusted even when it comes from a named peer.
5. **Local process boundary:** CLI, MCP, hooks and web UI share the same Unix account and
   SQLite file; current machine identity is not proof of process identity.
6. **Supply-chain boundary:** `uv`/build frontend resolves pinned runtime/dev packages and
   an unpinned build backend requirement from external package infrastructure.
7. **GitHub boundary:** source publication, future CI, issues/advisories and merges are
   external state changes; Michal remains the human approval authority.

## External integrations and dependencies

### Runtime and platform integrations

| External component | How it is used | Network/data behavior | Ownership |
|---|---|---|---|
| Claude Code | starts MCP server and lifecycle/PreToolUse hooks from user configuration | passes tool and lifecycle JSON; receives room content in model context | external Anthropic product/user configuration |
| Codex CLI/app | starts hooks/CLI integration from user configuration | passes tool/lifecycle input; operates under its sandbox permissions | external OpenAI product/user configuration |
| Web browser | connects to loopback web UI | receives room history and holds the web token in the page session | external local client |
| GitHub | public source remote and future issue/advisory/CI location | no repository runtime call; Git operations are operator actions | external hosted service |
| Python | runtime `>=3.11` | executes all entry points | external runtime |
| SQLite | Python stdlib binding; WAL file storage | local filesystem only | external embedded database/runtime library |
| Shell/hook host | invokes CLI and hook entry points | supplies environment, stdin and command/path metadata | local platform configuration |

No repository-owned runtime code currently calls a payment processor, identity provider,
cloud database, telemetry service, LDAP/NoSQL server, or other production API.

### Declared direct dependencies

| Type | Requirement | Locked version | Purpose | Risk |
|---|---|---|---|---|
| Runtime | `fastmcp>=3.4,<4` | `3.4.7` | MCP protocol/server framework | High supply-chain review priority because it brings a broad transitive web/auth stack |
| Development | `pytest>=8,<9` | `8.4.2` | tests | Medium |
| Development | `pytest-asyncio>=0.23,<2` | `1.4.0` | async tests | Medium |
| Development | `ruff>=0.5,<1` | `0.16.6` | lint/static checks | Low runtime risk; medium security-control relevance |
| Build system | `hatchling` | not pinned by project requirement | package build backend | Medium supply-chain priority |

### Locked transitive package inventory

`uv.lock` is the canonical resolved inventory. The following versions are recorded at
the scoped commit; platform markers may mean that not every package is installed on
every host. No CVE status was evaluated in this phase.

```text
aiofile 3.12.3                 annotated-types 0.8.0       anyio 4.15.1
attrs 26.1.0                   authlib 1.8.0               backports-tarfile 1.2.0
beartype 0.22.9                cachetools 7.1.8            caio 0.12.4
certifi 2026.7.22              cffi 2.1.1                  click 8.5.0
colorama 0.4.6                 cryptography 50.0.1         cyclopts 4.25.2
dnspython 2.8.0                docstring-parser 0.18.0     email-validator 2.3.0
exceptiongroup 1.3.1           fastmcp 3.4.7               fastmcp-slim 3.4.7
griffelib 2.3.0                h11 0.16.0                  httpcore 1.0.9
httpx 0.28.1                   httpx-sse 0.4.3             idna 3.19
importlib-metadata 9.0.1       iniconfig 2.3.0             jaraco-classes 3.4.0
jaraco-context 6.1.2           jaraco-functools 4.6.0      jeepney 0.9.0
joserfc 1.7.5                  jsonref 1.1.0               jsonschema 4.26.0
jsonschema-path 0.5.0          jsonschema-specifications 2025.9.1
keyring 25.7.0                 markdown-it-py 4.2.0        mcp 1.30.0
mdurl 0.1.2                    more-itertools 11.1.0       openapi-pydantic 0.5.1
opentelemetry-api 1.44.0       packaging 26.3              pathable 0.6.0
platformdirs 4.11.8            pluggy 1.6.0                py-key-value-aio 0.4.5
pycparser 3.0                  pydantic 2.13.5             pydantic-core 2.46.5
pydantic-settings 2.15.0       pygments 2.21.0             pyjwt 2.13.0
pyperclip 1.11.0               python-dotenv 1.2.3         python-multipart 0.0.32
pywin32 312                    pywin32-ctypes 0.2.3        pyyaml 6.0.3
referencing 0.37.0             rich 15.0.0                 rich-rst 2.1.0
rpds-py 2026.6.3               secretstorage 3.5.0         sse-starlette 3.4.11
starlette 1.6.0                typing-extensions 4.16.0    typing-inspection 0.4.4
uncalled-for 0.4.0             uvicorn 0.52.4              watchfiles 1.2.0
websockets 17.1                zipp 4.1.0
```

The lock also records the project itself and the direct development tools listed above.
Dependency reachability matters: the live-chat server uses stdlib `http.server`, while
many web/auth packages arrive transitively through FastMCP and may not be imported by
the project's own runtime path. Later dependency findings must distinguish installed,
imported, reachable, and actually affected packages.

## Existing verification assets

The current suite contains 85 passing tests. Security-relevant reusable patterns include:

- socket-free HTTP handler invocation in [`test_webui.py`](../tests/test_webui.py#L20);
- optional real loopback fixture in [`test_webui.py`](../tests/test_webui.py#L41);
- authentication, Origin and human-identity tests in
  [`test_webui.py`](../tests/test_webui.py#L79);
- token permission tests in [`test_webui.py`](../tests/test_webui.py#L120);
- concurrent delivery lease/state tests in [`test_store.py`](../tests/test_store.py#L135);
- subprocess hook isolation in [`test_hook.py`](../tests/test_hook.py#L18);
- malformed-hook fail-open behavior in [`test_hook.py`](../tests/test_hook.py#L91).

The documented verification commands are:

```bash
uv sync --extra dev
.venv/bin/python -m pytest tests -q
.venv/bin/python -m ruff check src tests
```

Ruff currently has only a line-length setting; a dedicated security ruleset is not
configured. There is no current CI runner to execute these checks automatically.

## Explicit absences and open ownership questions

- No formal component owner or backup approver is recorded.
- No `SECURITY.md`, private-reporting procedure, security advisory policy or severity
  SLA is defined in the repository.
- No CI, scheduled scan, SBOM, dependency bot or provenance verification is configured.
- No production deployment is declared; the only implemented listener is loopback.
- No broker capability-token model exists yet; current non-web identity relies on local
  process/Unix-account trust.
- Local cron/systemd state and GitHub repository security settings are deployment state,
  not tracked source, and were not asserted by this inventory.
- Future broker, adapters and startup services must be inventoried when implemented;
  they are not silently treated as present today.

## Phase boundary

This document completes inventory only. Candidate review surfaces above are not findings.
Discovery, CVE lookup, secret scanning, dynamic reproduction, Docker execution, GitHub
issues/advisories, branches, patches, CI configuration and infrastructure changes remain
out of scope until Michal explicitly approves the next phase.
