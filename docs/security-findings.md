# Security discovery findings

## Status and handling notice

This is the sanitized Phase 2 discovery register for `agent-lease-mcp`. It is suitable
for a public repository: it contains no secret values, exploit payloads, private room
content, sensitive local paths, or instructions for exploiting an unpatched issue.

Every item below is a **candidate** with status `unverified`. Static evidence or a
repository setting can be factual without proving exploitability or practical impact.
No dynamic proof of concept, Docker validation, destructive test, GitHub issue,
Security Advisory, patch, or application-code change was performed in this phase.

### Severity scale

Severity is a preliminary prioritization based on potential impact and current trust
boundaries, not a final CVSS score. Phase 3 validation may lower, raise, merge, or close
an item as a false positive.

## Scope and method

Discovery covered tracked source, tests, documentation, `pyproject.toml`, `uv.lock`,
pattern-based secret review of the tracked tree and Git history, and read-only GitHub
repository security settings as of 2026-09-11. Review methods were:

- manual data-flow, authorization, state-machine, hook, HTTP, storage, and agent-context
  review;
- targeted searches for injection sinks, subprocess execution, secret-bearing filenames,
  unsafe rendering, network clients, and path/file operations;
- Ruff security rules against production source;
- a lockfile advisory scan and manual reachability classification;
- read-only GitHub API inspection of branch and security settings.

No runtime system, live database, live credential, browser history, external agent, or
production service was exercised. Suggested validation steps below are proposals for a
separately approved Phase 3 and must use synthetic data and isolated scratch state.

## Summary

| ID | Candidate | Severity | Confidence | Status |
|---|---|---:|---:|---|
| APP-001 | SQLite files may inherit permissive creation modes | High | High | `unverified` |
| APP-002 | Machine identity is forgeable through the environment | High | High | `unverified` |
| APP-003 | Cross-recipient task access may disclose or misuse capabilities | High | High | `unverified` |
| APP-004 | Peer content crosses into model context without trust labeling | High | High | `unverified` |
| APP-005 | Machine peers can create tasks without sender authority policy | High | High | `unverified` |
| APP-006 | Guard coverage permits unsupported write-capable tool shapes | High | High | `unverified` |
| APP-007 | Guard auto-claim check and acquisition are not one decision | High | High | `unverified` |
| APP-008 | Guard fails open on malformed input and internal errors | Medium | High | `unverified` |
| APP-009 | Several non-web inputs lack explicit resource bounds | Medium | High | `unverified` |
| APP-010 | Persistent human-authority token is carried in URLs and stdout | Medium | High | `unverified` |
| APP-011 | Unbounded SSE connections may exhaust local server resources | Medium | Medium | `unverified` |
| APP-012 | Shared message data has no redaction or retention policy | Medium | High | `unverified` |
| APP-013 | Path checks may be subject to a symlink/rename race | Medium | Medium-low | `unverified` |
| DEP-001 | Development pytest version matches a published advisory range | Moderate | High version match; unverified reachability | `unverified` |
| REPO-001 | Effective default branch conflicts with repository documentation | Low | High | `unverified` |
| REPO-002 | Effective default branch has no branch protection | High | High configuration fact | `unverified` |
| REPO-003 | Dependabot alerts and security updates are disabled | Medium | High configuration fact | `unverified` |

## Application candidates

### APP-001 — SQLite files may inherit permissive creation modes

- **Location:** [`store.py`](../src/agent_lease_mcp/store.py#L25), especially lines
  93–105 and 129–141.
- **Description/data flow:** Store initialization creates the database parent and opens
  SQLite before any web-specific permission repair. Database, WAL, and shared-memory
  files can therefore inherit the process umask when CLI or MCP is the first entrypoint.
- **Preconditions:** A multi-user host, a permissive umask or directory policy, and
  another local account able to traverse the database parent.
- **Potential impact:** Disclosure or modification of room messages, claims, paths,
  delivery state, errors, or task capability tokens.
- **Severity/rationale:** **High** because the store contains confidentiality and
  integrity-sensitive coordination data. Practical exposure depends on host permissions.
- **Static evidence:** `mkdir`/SQLite connection creation has no explicit database-file
  mode enforcement. The web token path is separately hardened, but that does not prove
  equivalent protection for the database and sidecar files.
- **Existing mitigations:** The current inspected host appeared single-user; the
  documented deployment is WSL/local Unix-account scoped. Restrictive home-directory
  permissions reduce exposure where configured.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Under an isolated temporary home/database path and a
  deliberately permissive umask, start each entrypoint and inspect file modes only.
  Never use the real room database.

### APP-002 — Machine identity is forgeable through the environment

- **Location:** [`config.py`](../src/agent_lease_mcp/config.py#L38),
  [`server.py`](../src/agent_lease_mcp/server.py#L24), and
  [`cli.py`](../src/agent_lease_mcp/cli.py#L93).
- **Description/data flow:** `AGENT_NAME` supplies the identity used by machine-facing
  MCP/CLI operations. The reserved human name is remapped, but other peer names are not
  cryptographically authenticated.
- **Preconditions:** Ability to run a local process under the trusted Unix account or
  influence the environment of an agent-lease entrypoint.
- **Potential impact:** Impersonating a peer to send messages, release claims, obtain
  tasks, or perform task-state operations attributed to that peer.
- **Severity/rationale:** **High** because identity controls coordination and task
  capabilities, although the current security model already trusts the local account.
- **Static evidence:** Identity is derived from environment/config and reused across
  server and CLI operations; no per-agent credential is verified.
- **Existing mitigations:** Local-account boundary; explicit protection of the reserved
  human identity `michal`; web mutations require the separate human-authority token.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Use a scratch database and two subprocesses with synthetic
  agent names to build an operation/identity matrix. Do not target live peers or claims.

### APP-003 — Cross-recipient task access may disclose or misuse capabilities

- **Location:** [`server.py`](../src/agent_lease_mcp/server.py#L100),
  [`cli.py`](../src/agent_lease_mcp/cli.py#L59), and
  [`store.py`](../src/agent_lease_mcp/store.py#L401), especially store lines 522–548 and
  584–590.
- **Description/data flow:** Recipient selectors are caller supplied. Job listings can
  return delivery details including lease capability material, while retry/cancel and
  CLI acknowledgement paths accept a chosen recipient.
- **Preconditions:** A local machine peer can invoke the CLI/MCP surface and knows or can
  enumerate another recipient/delivery.
- **Potential impact:** Cross-recipient visibility, unauthorized state transitions, or
  use of another worker's task capability.
- **Severity/rationale:** **High** because a disclosed lease token may authorize durable
  task-state changes. Exact reachability and state checks remain to be tested.
- **Static evidence:** Public tool/CLI parameters flow into recipient-scoped store reads
  and mutations; the discovery review found no authenticated caller-to-recipient binding.
- **Existing mitigations:** Some state transitions require a matching lease token and
  valid delivery state; access is currently local-account scoped.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Populate a scratch database with agents A and B, then
  assert an explicit authorization matrix for A attempting B's reads and transitions.

### APP-004 — Peer content crosses into model context without trust labeling

- **Location:** [`store.py`](../src/agent_lease_mcp/store.py#L287) lines 287–333 and
  387–399 → [`lifecycle.py`](../src/agent_lease_mcp/lifecycle.py#L43) lines 43–61 →
  [`briefing.py`](../src/agent_lease_mcp/briefing.py#L20) lines 20–66.
- **Description/data flow:** Persisted peer text is rendered into lifecycle
  `additionalContext`. The briefing does not preserve enough message type/recipient
  context to make chat, control, result, broadcast, and task authority visibly distinct.
- **Preconditions:** A peer can persist text that will be delivered to another agent's
  lifecycle context.
- **Potential impact:** Agent-to-agent prompt or memory poisoning, misleading authority,
  and unsafe tool decisions if the receiving model treats peer text as instructions.
- **Severity/rationale:** **High** because this crosses an explicit untrusted-content to
  model-context boundary. Model behavior and tool-policy consequences are unverified.
- **Static evidence:** Raw stored text flows through the briefing renderer into hook
  context; type and authority metadata are not consistently presented alongside it.
- **Existing mitigations:** Deterministic tool guard, local-peer trust boundary, and
  human approval requirements limit some downstream actions.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Render a pure briefing/lifecycle fixture containing an
  inert marker and assert clear provenance, message kind, recipient, and delimiters.
  Do not invoke a model or any tools.

### APP-005 — Machine peers can create tasks without sender authority policy

- **Location:** [`server.py`](../src/agent_lease_mcp/server.py#L83),
  [`cli.py`](../src/agent_lease_mcp/cli.py#L52), and
  [`store.py`](../src/agent_lease_mcp/store.py#L291), especially store lines 427–472.
- **Description/data flow:** A machine-facing sender can create a `task` for an arbitrary
  recipient. Leasing checks recipient and state, but discovery found no sender-authority
  policy before a future worker accepts the task.
- **Preconditions:** A local peer can use the MCP/CLI send surface; autonomous task
  adapters must be enabled for unattended execution.
- **Potential impact:** Unauthorized work initiation or excessive agency under a future
  worker's filesystem/tool permissions.
- **Severity/rationale:** **High** due to the eventual execution boundary, but the most
  serious outcome is partly dormant because autonomous adapters are not implemented.
- **Static evidence:** Caller-selected kind/recipient is stored and leased without an
  allowlist binding sender authority to recipient/action.
- **Existing mitigations:** No autonomous adapters today; normal interactive work still
  has human and agent-runtime controls.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** In a scratch store, evaluate a documented sender/recipient
  authority matrix. Never start an agent or execute task content.

### APP-006 — Guard coverage permits unsupported write-capable tool shapes

- **Location:** [`policy.py`](../src/agent_lease_mcp/policy.py#L14) lines 14–24, 54–75,
  and 97–129; [`guard.py`](../src/agent_lease_mcp/guard.py#L33); relevant expectations in
  [`test_hook.py`](../tests/test_hook.py#L74) and [`test_policy.py`](../tests/test_policy.py#L54).
- **Description/data flow:** The guard recognizes a bounded set of tool/payload shapes.
  Unsupported tools, generic shell forms, or commands not recognized by the narrow
  script parser can be allowed without checking the actual write target.
- **Preconditions:** An agent runtime exposes a write-capable tool or shell syntax outside
  the recognized set and relies on this hook as its collision boundary.
- **Potential impact:** Modification of another agent's claimed path, undermining the
  primary coordination guarantee.
- **Severity/rationale:** **High** because bypassing claim enforcement can corrupt shared
  work, though exploitability depends on the host tool schema and sandbox.
- **Static evidence:** Explicit allow behavior exists for unrecognized shapes; tests
  document part of that behavior. No destructive command was run.
- **Existing mitigations:** Runtime sandbox permissions, explicit claim discipline, and
  coverage for recognized editor/write tools.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Feed synthetic hook JSON for representative write tools
  and commands targeting harmless temporary files; inspect decisions only.

### APP-007 — Guard auto-claim check and acquisition are not one decision

- **Location:** [`guard.py`](../src/agent_lease_mcp/guard.py#L44) and atomic claim logic in
  [`store.py`](../src/agent_lease_mcp/store.py#L145).
- **Description/data flow:** The guard checks current ownership and subsequently attempts
  a claim. Discovery indicates the returned claim result is not used to deny the tool if
  another process wins between those operations.
- **Preconditions:** Two agents concurrently target the same unclaimed path within the
  check/acquire window.
- **Potential impact:** Both tool calls may be permitted even though only one claim was
  acquired, allowing conflicting edits.
- **Severity/rationale:** **High** because it affects the core concurrency guarantee;
  the timing window and observed behavior remain unverified.
- **Static evidence:** Ownership read and claim are separate guard steps; store claim is
  atomic, but the losing result is not enforced by the caller.
- **Existing mitigations:** Atomic database claim prevents dual recorded ownership;
  cooperative agents and short operation windows lower likelihood.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Use deterministic monkeypatching or barriers against a
  scratch database to force the race and assert hook decisions. Do not edit shared files.

### APP-008 — Guard fails open on malformed input and internal errors

- **Location:** [`guard.py`](../src/agent_lease_mcp/guard.py#L27) lines 27–31 and 44–64;
  [`policy.py`](../src/agent_lease_mcp/policy.py#L54) lines 54–64 and 74–75; documented by
  [`test_hook.py`](../tests/test_hook.py#L91).
- **Description/data flow:** Malformed JSON, schema drift, path-processing failures, and
  some store errors return success rather than blocking the pending tool operation.
- **Preconditions:** Invalid hook input or an internal/database failure while the hook is
  expected to enforce a claim.
- **Potential impact:** A write may proceed without collision enforcement.
- **Severity/rationale:** **Medium** because fail-open is explicit behavior and improves
  availability, but weakens a security boundary during failures.
- **Static evidence:** Exception/error branches return exit code zero; existing tests
  explicitly expect malformed-input fail-open behavior.
- **Existing mitigations:** Host sandbox/tool permissions and agent cooperation remain;
  malformed input does not itself grant new OS permission.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Run the hook subprocess with synthetic malformed inputs
  and an injected store failure, then evaluate whether fail-open is an accepted contract.

### APP-009 — Several non-web inputs lack explicit resource bounds

- **Location:** [`server.py`](../src/agent_lease_mcp/server.py#L71),
  [`cli.py`](../src/agent_lease_mcp/cli.py#L35), and store operations in
  [`store.py`](../src/agent_lease_mcp/store.py#L248), lines 291–357, 401–470, and 550–561.
- **Description/data flow:** Message text, status, purpose, history/list limits, task
  lease duration, and repeated request volume do not consistently have explicit bounds.
  Some negative SQL `LIMIT` values have special SQLite semantics rather than rejecting
  the request.
- **Preconditions:** A local client can repeatedly call CLI/MCP operations or submit
  unusually large/negative values.
- **Potential impact:** Database growth, unexpectedly broad reads, long leases, memory or
  CPU pressure, and local availability degradation.
- **Severity/rationale:** **Medium** because the surface is local today; future autonomous
  adapters would increase exposure.
- **Static evidence:** Parameters flow to storage/query operations without a common
  length/range policy. The web request body separately has an 8 KiB cap.
- **Existing mitigations:** Loopback/stdio/local-account scope; bounded web body; SQLite
  remains local.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Add bounded unit cases around maximum, zero, and negative
  values using a scratch database. Avoid actual exhaustion or large allocations.

### APP-010 — Persistent human-authority token is carried in URLs and stdout

- **Location:** [`webui.py`](../src/agent_lease_mcp/webui.py#L108), lines 108–110,
  152–166, 269–280, 502, 600–610, and 645–654.
- **Description/data flow:** The web authority token is printed in the startup URL,
  accepted in query parameters for initial/SSE access, and retained by page JavaScript.
  It has no built-in expiry or rotation schedule.
- **Preconditions:** Access to terminal output, browser history/page state, process
  inspection, or another local disclosure channel.
- **Potential impact:** Acting through the local web API as the reserved human identity.
- **Severity/rationale:** **Medium** because the capability is powerful but restricted to
  a loopback service and protected by OS file permissions and high entropy.
- **Static evidence:** The token's URL/stdout flow and persistence are directly visible
  in the handler/startup code; no real token was inspected.
- **Existing mitigations:** 256-bit randomness, constant-time comparison, token file mode
  `0600`, parent `0700`, loopback binding, no-referrer/no-store headers, and POST Origin
  checks.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Inject a synthetic token into handler/startup fixtures and
  capture generated URLs/stdout. Never inspect or copy a real token/browser history.

### APP-011 — Unbounded SSE connections may exhaust local server resources

- **Location:** [`webui.py`](../src/agent_lease_mcp/webui.py#L90), lines 90–95,
  269–313, and 645–660.
- **Description/data flow:** `ThreadingHTTPServer` creates a thread per connection and SSE
  handlers can remain open indefinitely. No explicit connection cap, socket timeout, or
  rate limiter was found.
- **Preconditions:** A local process can open and retain many loopback connections; token
  requirements for the SSE path may constrain some request forms but not necessarily
  connection establishment cost.
- **Potential impact:** Thread/file-descriptor exhaustion and local live-chat unavailability.
- **Severity/rationale:** **Medium** due to local-only availability impact and unverified
  practical limits.
- **Static evidence:** Threaded server plus long-lived loop; no admission-control primitive
  is present in the implementation.
- **Existing mitigations:** Loopback-only listener, token authentication, and no claimed
  remote deployment.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Prefer a bounded socket-free concurrency model. If network
  validation is later approved, use a capped isolated container and a strict small limit,
  never the live server.

### APP-012 — Shared message data has no redaction or retention policy

- **Location:** [`store.py`](../src/agent_lease_mcp/store.py#L291), lines 291–333,
  474–520, and 584–600; readers at lines 335–425; web distribution in
  [`webui.py`](../src/agent_lease_mcp/webui.py#L168) and 269–309.
- **Description/data flow:** Arbitrary message/result/error text is persisted and later
  redistributed through room, job, web, and lifecycle views. No field classification,
  automatic redaction, default expiry, or retention limit was found.
- **Preconditions:** A user, peer, or failing task stores confidential material or an
  accidental credential in a message-like field.
- **Potential impact:** Long-term local disclosure and propagation into browsers, CLI
  output, logs, or model context.
- **Severity/rationale:** **Medium** because sensitivity depends on submitted content and
  current access is local, but propagation is broad and durable.
- **Static evidence:** Store schemas and readers retain/return raw text; no retention or
  redaction layer appears on these paths.
- **Existing mitigations:** Local database boundary and web authentication; users are
  instructed not to share secrets.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Store an obvious inert fake-secret marker in a scratch
  database and document only which output surfaces reproduce it.

### APP-013 — Path checks may be subject to a symlink/rename race

- **Location:** [`policy.py`](../src/agent_lease_mcp/policy.py#L30),
  [`guard.py`](../src/agent_lease_mcp/guard.py#L44), and
  [`store.py`](../src/agent_lease_mcp/store.py#L145).
- **Description/data flow:** A target path is resolved during hook evaluation, but the
  client tool opens it later. A rename or symlink substitution between those events may
  cause the checked path and modified filesystem object to differ.
- **Preconditions:** A cooperating or adversarial local process can alter the isolated
  path between guard decision and tool file open.
- **Potential impact:** Modification outside the intended claim or collision with another
  agent's work.
- **Severity/rationale:** **Medium**, confidence **medium-low**: this is a generic TOCTOU
  concern and actual tool/open behavior has not been demonstrated.
- **Static evidence:** `Path.resolve` canonicalizes aliases at check time, but the guard
  cannot bind a later external tool open to the same inode.
- **Existing mitigations:** Resolution handles existing aliases at check time; local
  sandbox and cooperative-user assumptions reduce attackability.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Use harmless files in an isolated temporary tree and
  deterministic barriers; never target repository or user data.

## Dependency candidate

### DEP-001 — Development pytest version matches a published advisory range

- **Location/setting:** [`pyproject.toml`](../pyproject.toml#L24) and `uv.lock` lock
  `pytest==8.4.2`; advisory
  [GHSA-6w46-j5rx-g56g](https://github.com/advisories/GHSA-6w46-j5rx-g56g).
- **Description/data flow:** Lockfile scanning reported the pinned development-only pytest
  version as matching the advisory's affected range.
- **Preconditions:** The advisory-specific pytest behavior must be reachable through this
  repository's tests, plugins, or CI inputs. That reachability was not established.
- **Potential impact:** Impact is limited to environments that execute the affected test
  path; pytest is not an application runtime dependency.
- **Severity/rationale:** **Moderate** advisory severity, treated here as dev-only and
  `unverified` until exact affected-range and reachability validation is completed.
- **Static/config evidence:** The locked version is a verified fact and the advisory match
  was reported by the dependency scan. A version match is not proof that this project's
  usage is exploitable.
- **Existing mitigations:** Development dependency only; no repository CI currently runs
  untrusted pull-request tests; production entrypoints do not import pytest.
- **Status:** `unverified`.
- **Safe Phase 3 validation:** Confirm the advisory's canonical affected/fixed ranges,
  inspect whether the relevant pytest feature is used, then test an upgrade in an
  isolated environment without running untrusted test content.

## Repository configuration candidates

### REPO-001 — Effective default branch conflicts with repository documentation

- **Location/setting:** GitHub repository default branch
  `auto/presence-a-sandbox`; [`TODO.md`](../TODO.md#L96) describes merging that branch
  into `master`.
- **Description/data flow:** The repository was created from the feature branch, making it
  the effective default, while project documentation treats `master` as the destination.
- **Preconditions:** Contributors, automation, or security tooling infer release intent
  from the GitHub default branch.
- **Potential impact:** Scans and pull requests may target a branch different from the
  intended stable line; maintainers may apply protection or release actions to the wrong
  branch.
- **Severity/rationale:** **Low** because this is primarily governance ambiguity, but it
  compounds branch-protection risk.
- **Static/config evidence:** The GitHub default-branch value and documentation text were
  read-only verified. This does not establish that `master` is currently release-ready.
- **Existing mitigations:** The active feature branch contains the latest pushed work and
  Git history is intact.
- **Status:** `unverified` pending maintainer confirmation of intended branch policy.
- **Safe Phase 3 validation:** Ask the maintainer to designate the canonical branch, then
  compare repository links, clone behavior, open work, and planned protection without
  changing settings.

### REPO-002 — Effective default branch has no branch protection

- **Location/setting:** GitHub branch protection/rules for the effective default branch
  `auto/presence-a-sandbox`.
- **Description/data flow:** Read-only repository inspection found no rule preventing
  direct or unreviewed changes to the branch GitHub currently treats as default.
- **Preconditions:** A principal with write permission pushes directly or merges without
  the desired review/status checks.
- **Potential impact:** Security regressions or compromised changes can enter the default
  branch without mandatory review, tests, or protected-history controls.
- **Severity/rationale:** **High** because source integrity is a central trust boundary;
  the practical threat depends on collaborator permissions and credential security.
- **Static/config evidence:** Absence of branch protection is a verified repository
  setting. No unauthorized push was attempted.
- **Existing mitigations:** Current repository ownership is narrow; secret scanning and
  push protection are enabled; human process currently requires Michal's approval.
- **Status:** `unverified` as a vulnerability until the intended canonical branch and
  required protection policy are confirmed.
- **Safe Phase 3 validation:** Read-only enumerate collaborators, rulesets, and available
  status checks; propose least-privilege rules for the maintainer to approve separately.

### REPO-003 — Dependabot alerts and security updates are disabled

- **Location/setting:** GitHub repository security settings:
  vulnerability alerts and Dependabot security updates.
- **Description/data flow:** Read-only API checks found both vulnerability alerts and
  automatic security-update pull requests disabled. New vulnerable dependency versions
  therefore lack GitHub's continuous alert/update path.
- **Preconditions:** A newly disclosed advisory affects a committed dependency and no
  separate scanner detects or routes it promptly.
- **Potential impact:** Delayed detection and remediation of vulnerable dependencies.
- **Severity/rationale:** **Medium** because this is a missing detective/remediation
  control rather than direct application exploitability.
- **Static/config evidence:** Disabled settings are verified configuration facts as of
  2026-09-11. The repository has no tracked CI security scanner that compensates for them.
- **Existing mitigations:** Lockfile review in this Phase 2; GitHub secret scanning and
  push protection are enabled; dependencies are version locked.
- **Status:** `unverified` pending maintainer choice of the continuous dependency policy.
- **Safe Phase 3 validation:** Confirm notification routing and expected update workflow,
  then prepare a settings-only proposal. Enabling settings requires separate approval.

## Posture observations (not vulnerabilities)

- **Private vulnerability reporting is disabled.** This prevents external reporters from
  using GitHub's private reporting intake, but repository owners can still create draft
  Security Advisories. It is therefore recorded as a governance choice, not a finding.
- **Secret scanning and push protection are enabled.** These are positive repository
  controls and do not prove that past or unsupported secret formats are absent.
- **No branch is changed by this report.** The default-branch and protection observations
  describe read-only state only.

## Clean scans and non-findings

The following checks produced no candidate finding in the reviewed scope:

- The lockfile advisory scan reported no affected package other than `DEP-001`.
  Installed/imported/reachable status remains distinct from a lockfile version match.
- Pattern-based secret review of the tracked tree and Git history did not reveal a
  committed secret value. No secret values were printed or copied into this report.
- Ruff security rules passed against `src`; findings against tests were test-fixture
  patterns such as hardcoded dummy values, not production credentials.
- SQL values are parameterized. The schema migration f-string in `store.py` uses a fixed
  internal mapping rather than caller-controlled SQL.
- No production `subprocess`, `os.system`, shell execution, `eval`, or `exec` sink was
  found; subprocess use is confined to tests.
- No static stored-XSS sink was established: room-controlled UI strings reviewed in the
  discovery path pass through `esc()` before `innerHTML`. Dynamic adversarial browser
  validation has not yet been performed.
- No direct CSRF bypass was established: state-changing web requests require a custom
  token header and allowed Origin, and no CORS permission is emitted. Acceptance of a
  missing Origin alone is not sufficient evidence of a bypass.
- No SSRF, open redirect, LDAP client, NoSQL client, template engine, or password-based
  authentication surface was found.
- Non-loopback bind targets are rejected in
  [`webui.py`](../src/agent_lease_mcp/webui.py#L664).
- No direct path-traversal file-write sink was identified; path strings are primarily
  coordination metadata and are normalized before policy comparison.

These are scoped non-findings, not guarantees. They can change when broker/adapters,
CI, new dependencies, deployment services, or new endpoints are added.

## Disclosure and issue index

No GitHub issue or Security Advisory has been created for any item in this document.
No candidate has been publicly disclosed beyond this sanitized register, dynamically
validated, patched, or approved for exploitation testing.

| Finding | Public issue | Private advisory | Patch | Disclosure state |
|---|---|---|---|---|
| APP-001–APP-013 | none | none | none | discovery candidate only |
| DEP-001 | none | none | none | public upstream advisory; local reachability unverified |
| REPO-001–REPO-003 | none | none | none | repository posture candidate only |

High-severity candidates must use a draft GitHub Security Advisory if Phase 3 validates
practical impact. Lower-severity public issues still require a disclosure decision and
maintainer approval. Secret values must never be placed in an issue or advisory.

## Phase boundary

This file records discovery only. Phase 3 may validate candidates using isolated scratch
state and synthetic data after explicit approval. It does not authorize Docker execution,
live-system testing, GitHub setting changes, advisories/issues, branches, patches, CI,
merges, or disclosure.
