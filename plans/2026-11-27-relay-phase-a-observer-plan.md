# Relay phase A observer: the checkpoint collector and deployment observer for automated phase A

## Context

The owner's standing directive (2026-11-27) says Relay phase A must run without manual owner steps. The inference
session's automation host (`docs/runbooks/inference-canary-v0113/AUTOMATED-PHASE-A.md`, ailab #2060) runs the unchanged
`Runner.replaceStopped()` driver non-interactively. It refuses to start until the operator links three integrations:

1. `collectCheckpoint({name, instructions, deadline, signal, context})`: return the evidence value for each driver
   checkpoint. The driver's own validator checks it. An empty object, a stale observation or an unconditional `true`
   is not a substitute.
2. `observeDeployment(context)` returns `{hubSource, ready, observedUtc}` before any credential request (renewal
   prelude).
3. `readPredecessorEvidence(context)` plus `predecessorEvidencePin`: the separately pinned read-only projection of the
   stopped predecessor's three command receipts (`projection-contract.md`).

The authenticated transport exists: the session broker (`scripts/relay-operator.py session`, ailab #2062) runs the
runner bundle as UID 10001 in a `--network none` container. That container talks only to the scoped Unix-socket proxy
on a 1 MiB tmpfs volume. This plan adds the missing **observer**, which:
- collects real machine evidence: Router trajectories, the dev-worker-1 marker files, holder hello, hub deployment,
  read-only database facts;
- performs the one operator action the driver asks for, the preserving-holder handoff on dev-worker-1;
- applies a fixed, reviewed canary policy to the confirmation checkpoints.

**Checkpoint inventory** (`owner-phase-a.js` at the #2060 head):
- 13 `ask` sites, 11 of them reachable from `replaceStopped()`;
- at least 51 calls per run, up to about 79 with the Relay-read approvals;
- every call carries a structured `context.kind`, and collectors key on it. They never parse `name`, `instructions` or
  provider text.

**Timeouts:**
- ordinary checkpoints: 120 s;
- permission (C8): at most 30 s;
- the predecessor reader: 15 s;
- C1 and C2 share one 120 s bootstrap budget with the metadata reads between them.

## Review resolution (round 1: codex plus an independent Fable review)

Both reviewers accepted the split into a credential-less runner and pinned host collectors. Every codex finding is
accepted and folded into the sections below. Where the two reviews chose differently, the choice and the reason:

- **Transport.** Both rejected the Windows file mailbox (rename races, reparse points, no quota, fictional modes).
  **Chosen:** Fable's stdio-bridged gate sidecar (section 2). It reuses the broker's existing pattern for the proxy
  (`docker run -i` plus a tmpfs socket volume) and keeps every credential on the host.
- **C11 trajectory.** Fable proposed reusing the preceding zero-attempt observation. Codex required a fresh snapshot
  against the retained one with an unchanged `since`. **Chosen:** codex's. It costs one read and proves the delta
  rather than inferring it.
- **Runtime pre-validation with the driver's exported validators (Fable).** **Not adopted at runtime.** Those
  validators need Runner state that the client does not have, so the host performs the settle and `notBefore`
  waits itself. **Adopted in tests** instead: every collector output is fed into the real driver validators
  (Verification 1).
- **C4 timing.** **Adopted:** Fable's move of the holder-only start before the runner, plus codex's measured end-to-end
  budget.
- **Blockers.** Both flagged C10 and the predecessor projection as blockers. Both become explicit gates that need
  driver-side answers (section 10).

## Approach

### 1. Two halves: a credential-less client in the runner, collectors on the workstation

- **Client** (`scripts/relay-phase-a/observer-client.cjs`, in the runner bundle) implements the three integration
  functions. There are three distinct envelopes (section 2), and `AbortSignal` is stripped before sending. The client
  holds no credentials and never evaluates `instructions`; it sends only their SHA-256.
- **Entrypoint** (`scripts/relay-phase-a/phase-a-entry.cjs`, in the bundle):
  1. requires the inference session's pinned `owner-phase-a-cli.cjs`;
  2. converts the broker's epoch-second `RUNNER_DEADLINE` into the 13-digit epoch-millisecond string that `runCli`
     requires, by passing `runCli({env: {...process.env, RUNNER_DEADLINE: <ms>}})`;
  3. calls `runCli` with argv `--predecessor /state/predecessor/report.json --output /state/run/phase-a`, plus the
     client's functions and the pin (section 6);
  4. propagates `runCli`'s return code as the process exit code.
- **Host observer** (`scripts/relay_phase_a_observer.py`) runs inside the `relay-operator.py session` orchestrator
  process, as a thread started only for a manifest with an `observer` block. Its code, remote scripts, SQL and config
  are loaded from the manifest's reviewed commit (`git show <commit>:<path>` into a private temporary directory, each
  file hashed), never from the working tree. Its collectors use the operator's existing host credentials, which never
  enter a container:
  - `kubectl` with an explicit `--context admin@ai` and a namespace on every call;
  - `ssh` to `c4@192.168.0.8` with `StrictHostKeyChecking=yes` against a pinned `known_hosts` entry (the probe checks
    it).

### 2. Transport: a stdio-bridged gate sidecar

**The gate.** It is a new container, `relay-observer-gate-<run>`, launched like the proxy:
- `docker run -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges`, UID 10001, a pids limit
  of 32 and 64 MiB of memory;
- the same tmpfs socket volume;
- a small pinned Python script, `relay-observer-gate.py`, from the reviewed tool directory.

The gate holds no credentials. It serves `/run/relay-session/observer.sock`:
- one connection at a time, refusing a second;
- one request per connection: one JSON line of at most 64 KiB, with duplicate keys and non-finite numbers rejected;
- it stamps the request with its own receipt time `gateNow` (the runner's clock: both containers share the Docker VM
  clock);
- it writes `{"seq": n, "gateNow": ms, "request": {...}}` as one line to stdout, then waits for the host's matching
  `{"seq": n, ...}` line on stdin;
- it relays that line to the client and closes the connection.

On client disconnect or deadline, it writes `{"seq": n, "cancel": true}` to stdout.

**The host thread** reads the gate's stdout, validates and executes the request, and writes the response to the gate's
stdin. A request whose `seq` is not the next one is refused, as is a second outstanding request, so ordering is enforced
on both sides.

**Envelopes:**
- `checkpoint`: `{v:1, call:"checkpoint", kind, name, instructionsSha256, deadline, clientNow, context}`;
- `deployment`: `{v:1, call:"deployment", deadline, clientNow, context}`, where context is the renewal prelude's
  object;
- `predecessor`: `{v:1, call:"predecessor", deadline, clientNow, context:{source, schemaVersion, sessionId, generation,
  priorReportCanonicalSha256, commands}}`.

`name` is recorded but never validated or used.

**Responses** are `{v:1, seq, ok:true, value}` or `{v:1, seq, ok:false, code}`:
- `code` is a fixed word such as `refused`, `failed`, `deadline`, `unsupported` or `observer-down`;
- a response holds at most 512 KiB (a full trajectory envelope fits);
- the client checks `v`, `seq` and the shape strictly.

Host error text never travels.

**Deadlines and cancellation:**
- The host clamps `deadline` to its own per-kind maxima: 120 s, 30 s for permission, 15 s for the predecessor, and the
  shared C1/C2 budget. It runs every collector against one monotonic remaining budget, including waits, subprocesses
  and fsync.
- Collectors run in a worker thread, while the host thread keeps reading the gate's stdout, so a `cancel` line is seen
  at once.
- Every remote step is wrapped in a remote `timeout` (`timeout --kill-after=2 <s>` on dev-worker-1, and in-pod
  `node` / `psql` statement timeouts), so a local kill is never the only stop.
- After cancellation no further mutating step starts. A cancelled mutating stage is classified by a read-only readback
  (section 5), never by assumption.

**Liveness.** If the host thread or a worker dies, the host answers all further requests `observer-down` and signals the
orchestrator's stop condition (section 9).

### 3. Retained observer state

The observer keeps a run-scoped state machine in host memory. It is never restored from anything the runner can write.
It is keyed by execution, provider and runId, and records:
- **from C2:** the renewed profiles projection, the new Router keys (principal allowlist for C5 and C7) and the earliest
  expiry;
- **from C3:** the granted replacement window;
- **from the pre-run step:** the holder start and the pinned Router pod identity (UID, container restart count);
- **per provider and runId:** the last returned trajectory, marker reports (bytes and SHA-256) and the marker-decision
  counters.

It distinguishes "returned" from "accepted": acceptance is inferred only when the driver's next checkpoint for that
runId arrives.

**Kind order is enforced.** For example, C4 is accepted only after this run's C3 returned `ok`. C5, C6, C7 and C8 for a
runId are accepted only after its C5. A request outside the expected order is refused. Per-kind request counts are
capped at the inventory maxima.

### 4. Collectors by checkpoint kind

**Shared rules:**
- Every `...Confirmed` or `operatorVerified` field is set only when its named checks passed in this call. Any failed
  check returns `ok:false, code:failed`, and the driver stops.
- **Source clocks.** A trajectory's `requestedAt` is the Router pod's clock, and a marker's `observedUtc` is
  dev-worker-1's. Every `notBefore` wait happens at the source: the remote script sleeps until its own clock passes
  `notBefore` before observing.
- **Clock gate.** On every request the host measures four clocks within one bounded round: the workstation, the Router
  pod, dev-worker-1, and the runner (`gateNow`, with the gate round trip subtracted). It refuses if any pair differs by
  4 s or more. C5's `clocksWithin5s` is this result.

| kind / call | Collected (read-only unless marked) | Returned |
|---|---|---|
| `deployment` (observeDeployment) | Deployment `relay` in namespace relay: `spec.replicas >= 1`; `observedGeneration == generation`; updated, ready and available replicas equal to the spec. The current ReplicaSet's pods: every `relay` container's `imageID` digest equals the pinned release receipt's digest. The source comes from that receipt (section 6). | `{hubSource, ready, observedUtc}` |
| `renewal-deployment` (C1) | A fresh deployment observation, never the prelude's. The predecessor report's raw SHA-256 is re-hashed from the staged copy and must equal the manifest pin. The C1 budget is 20 s. | `{confirmMetadataReview, reportDownloaded, operatorVerified, deploymentVerified, hubSource, observedUtc}` |
| `renewed-credentials` (C2) | Read-only DB, both pinned bindings. In the `ready` case the credential is `current_credential_id`; in the `installing` case `current_credential_id` is null and it is `pending_credential_id`. The full projection must equal `context.profiles`: renewed key, generation, revision and expiry. `context.expiresAt` must equal the numeric earlier expiry. Stores the new keys and expiry (section 3). The budget is 10 s. | `{confirmRenewedCredentials:true, profiles, expiresAt}`, echoed only after the full match |
| `stopped-replacement` (C3) | A trajectory for `context.oldScope`: the principal must be one of the pinned historical keys, with the original `since`. It must hold exactly the two known rows: ids starting `338869f5-` complete and `811cab9f-` cancelled, with one attempt each. The codex marker baseline must be unchanged from the retained predecessor baseline. Both are observed at or after `context.notBefore` on their source clocks. The window starts at `gateNow - 2 s` and ends at `min(start + 900 s, the C2 expiry - 60 s, the orchestrator's runner deadline - the cleanup budget)`. The raw report hash is re-checked, and `priorReportCanonicalSha256` is the host's own `canonical(parsed report)` hash, which must equal `context.canonicalReportSha256`. | `{confirmReplacement, reportDownloaded, operatorVerified, priorReportCanonicalSha256, windowStartUtc, windowEndUtc, trajectory, marker}` |
| `preserving-holder` (C4) | **MUTATING** (section 5) | the `preservingHandoff` object |
| `pre-launch` (C5) | **Holder hello:** ready, 0 sessions, 0 pending commands, incarnation and journal equal to `context.holderId` / `journalId`, and the published binary and profile hashes. **Route** `canary-pi-<provider>`: exactly one enabled deployment, the pinned account and model, `maxAttempts` 1, no fallback. **Clock gate.** **Trajectory:** the principal must be the C2-verified renewed key. `since` is chosen in the pod: the pod waits until its clock passes `sinceAfter`, then uses `since = podNow`, before `requestedAt`. **Marker baseline.** | `{trajectory, marker, coverageConfirmed, holderReadyConfirmed, routeConfirmed, clocksWithin5s}` |
| `post-open` (C6) | **`credential.json`:** inspected with no-follow (`stat -L` refused, `find -maxdepth 0 -type f`); owner c4, mode 0600, one link; contents never read. **DB:** the binding's installation receipt for THIS run's credential (C2) is terminal `installed` after the launch, and the binding and credential are `ready/ready`. **Marker baseline** at or after `notBefore`, unchanged from C5's. | `{marker, deliveryConfirmed}` |
| `observation` (C7) | A trajectory for `context.scope`, with its principal on the allowlist and the same `since`, at or after `notBefore`. Unsettled rows: re-poll with a bounded backoff inside the budget (the driver refuses unsettled rows); `since` never changes, no row is dropped, no provider work is retried. The marker for `context.phase` when non-null. | `{trajectory, marker?, coverageConfirmed}` |
| `permission` (C8) | section 7 | `true` |
| `recover-history` (C9) | `context.generation` is the session just closed. **Hello:** same incarnation and journal as the context, 0 sessions, 0 pending commands. **Marker:** the run's approved marker bytes equal the retained SHA-256. **DB:** every command of the closed session is settled (terminal state and receipt), with no native tool effect recorded as unknown. | `true` |
| `reconcile-choice` (C10) | section 7 (gated) | `{reconcile}` |
| `retire-branch` (C11) | **DB:** the session is at `context.generation`. Its settings snapshot `configOptions` shows effort `high`, bound to that generation and control epoch. The recorded `SESSION_CONFIGURATION_CHANGED` refusal is present. **Trajectory:** fresh, with the retained snapshot's `since`, and 0 attempts added since that snapshot. | `true` |
| `predecessor` | section 6 | raw UTF-8 projection |

**`coverageConfirmed` is derived from these checks, not from a successful listing:**
- The Router pod is the pinned one: same UID and same restart count as at the pre-run step.
- Trajectory recording is enabled in its live config.
- The new snapshot contains every row id of the retained one, unchanged.
- The response is a recognized envelope. An unrecognized shape is a failure, never an empty row list.
- A Router restart or any gap stops the run.

**Remote scripts** are piped over ssh stdin (`python3 -I - <args>`) or `kubectl exec -i ... node -`. Nothing is
staged in a temporary directory. The scripts are:
- `router-trajectory.js`: the collector logic from `collect.sh`, with keys passed as validated arguments;
- `observe-marker.py`: reads only the fixed marker files;
- `holder-hello.py`: emits only an allowlisted key set with typed values. A missing field is a failure, never a zero or
  false.

All reads for one checkpoint are batched into one ssh call and one `kubectl exec` call where possible. The live probe
measures each call's latency (Verification 3).

### 5. The preserving-holder handoff (C4): the one mutating step

**Pre-run step** (orchestrator, after the Lease and before the runner starts):
1. Verify both units' effective `ExecStart` paths, state directories and drop-ins against the pins:
   - holder v0.13.4 at 64dc50f4, binary d406b1f3;
   - the connector and holder-maintenance image digests from `resume.sh`;
   - the state root `relay-holder-handoff-<preparationId>`.
2. Validate the handoff receipt `used-handoffs/<preparationId>/complete.json`: preparation, old and new identities, new
   root and seal ID. Its SHA-256 is recorded. Check that the old root is sealed with the pinned seal.
3. Start only `relay-holder.service`.
4. Wait for a hello that shows admission paused, 0 sessions, 0 pending commands, `nativeStateReady` and the pinned
   incarnation.

Admission is paused, so a started holder can take no work. If the run never reaches C4, the holder stays paused and
running, and the result records it.

**C4 itself.** Its total budget is at most 90 s, measured within C4's 120 s:
1. **Authorization.** This run's C3 returned `ok`, the Lease is still held, and the time is inside the C3 window.
2. **Durable ledger.** `%LOCALAPPDATA%\relay-operator\handoff-ledger\<preparationId>.jsonl` lives outside any per-run
   directory and is written only under the Lease. It records the intent `{op: resume, operationId, revision}` with
   fsync before dispatch.
   - The operationId is deterministic: `uuid5(preparationId, "resume")`.
   - Holder-maintenance `resume <revision> <operationId>` is a compare-and-swap. On re-entry the ledger plus a
     read-only status readback decides: "already resumed with this operationId" goes straight to readback, and
     "paused at the recorded revision" may dispatch.
   - Any other state stops the run.
3. **Resume** at the freshly observed revision, in 25 s or less.
4. **Enable both units and start only reconciliation**, in 15 s or less. Enabling is the handoff's documented normal
   reboot behaviour (UPGRADE-USED.md): the new holder must come back after a host reboot. The runbook records it.
5. **Read back**, in 40 s or less:
   - the reconciliation unit's effective state path;
   - a fresh hello: `admissionPaused:false`, same incarnation, the holder did not restart (MainPID unchanged);
   - the hub's `agent_holder_health` row: pinned tenant and host, the measured holder and journal, and a timestamp
     after the resume.
6. **Return** the `preservingHandoff` object.
   - Every field comes from the hello, the receipt and the files, except `oldHolderId` and `oldJournalId`. Those come
     from the context and are checked against the receipt.
   - `context.qualifiedHolder` is compared with the config, never selected from.
   - Each new identity must differ from its predecessor, and `observedUtc` must be at or after `context.startedAt`.

Each stage writes a terminal ledger receipt. On a failure, timeout or cancellation:
- nothing is rolled back;
- a read-only readback classifies the state;
- the final service and admission state, plus any unresolved remote process, go into the result.

Revoking the JIT session does not undo the resume or stop these services; the runbook states this.

### 6. Pins, release receipts and the predecessor

**The observer config** is `scripts/relay-phase-a/observer-config.json`. It pins:
- the tenant, the bindings, the profiles and the historical Router keys;
- the Router source `4e4bb34e…`;
- the marker-command template, the holder and connector pins, the handoff preparation and seal;
- the hub release receipts (`{release, imageDigest, source}`);
- the Router pod selector and the SQL file hashes.

**The manifest's `observer` block** is `{configPath, configSha256, observerPaths: {path: sha256}, predecessorReport:
{path, sha256}, predecessorPin: path}`. Every listed file is loaded from the manifest commit and hash-checked, and the
commit must be on gitea/main (the #2062 rule).

**Driver and source compatibility (gate).** The driver pins `RUN_SOURCE = 5f90d03a` (v0.14.1), and the projection
requires schema 93. A v0.14.2 run therefore needs a reviewed driver source bridge and revalidated schema contracts, not
only a new receipt.

**The predecessor report** is the original saved report of `relay-pi-phase-a-20261126-v1`.
- **Staging.** It is staged by the orchestrator into the state volume as `/state/predecessor/report.json`, using a
  `--network none` helper container: copied, owned by 10001, mode 0400, regular, single-link. `runCli` requires all
  of these.
- **Hashes.** The raw SHA-256 is pinned in the manifest. The canonical hash (`canonical(parsed)`) is computed by the
  host and must equal C3's `context.canonicalReportSha256`.
- **Probe.** The live probe checks the staging on the real Docker backend.

**The predecessor projection (gate).** The current contract cannot be met.
- **The contradiction.** The Runner clones `predecessorEvidencePin.sha256` at construction. The exact projection bytes
  include an `observedUtc` that must be under 120 s old at `replacementPreflight`, which is minutes later.
- **Requested resolution.** A driver change pins the SHA-256 of the canonical projection *excluding* `observedUtc`
  (tenant, database label, query hash, session, generation, report hash and the three exact receipts), and checks
  freshness on the unpinned field.
- **Pin provisioning.** The pin is produced once by the reviewed read-only query, `predecessor-commands.sql` (its
  SHA-256 is `querySha256`). It is reviewed in a PR as `scripts/relay-phase-a/predecessor-pin.json`, named by the
  manifest, and loaded by the entry before the Runner is constructed. It is never derived from callback output.
- **Tests.** They cover stale bytes, changed receipts, a tenant or query mismatch and the 15 s bound.

### 7. Confirmation policy (C8, C10)

**C8 `permission`.** Reads the permission from the DB with explicit columns: id, session, generation, state, expiry,
tool-call id, title, description and options. Requirements:
- the permission is pending and unexpired;
- the session is this run's, with no rebinding, at `context.generation`;
- exactly one option has `context.optionId`, and its kind equals `decisionKind`.

Two purposes are allowed:
- **`relay-message-read`:**
  - the title is exactly `Allow mcp__relay__relay_messages_read?`;
  - the description's JSON input has only optional `channelId` (exactly `b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1`),
    `after` (an exact canonical decimal string up to 2^63-1) and `limit` (a safe integer from 1 to 50);
  - `decisionKind` is `allow_once`;
  - a per-run cap equals the run's prompt budget. The one-read-per-prompt rule stays with the driver's own guard, since
    the context names no prompt.
- **`marker`:** the provider and runId come from retained state for this session, never from parsing. The input's
  `command` field must equal the observer's own rendering for one of the two steps, and its optional `timeout` must
  follow the driver's rule. The decision is bound to the command, not to the order:
  - the `denied` rendering allows only `reject_once`, and the `effects` rendering only `allow_once`;
  - each is allowed once per lifecycle runId: two for Codex and two for Qwen in the full flow;
  - `context.expectedCommand` must equal the rendering.

Anything else stops the run. The observer never returns `false` to mean "deny".

The proxy already lets the runner post decisions, so this check is integrity and evidence, not containment.

**C10 `reconcile-choice` (gate).** Neither the context nor the session contract has an authoritative field yet:
- `true` costs a prompt and requires "Previous branch retired.";
- `false` continues to `RECOVERED_OK`.

The collector stays `unsupported` until the inference session names the native-branch observation. The policy is then:
`true` exactly when it says reconstruction is required, `false` exactly when it positively says not, and absent or
unknown stops the run. Both values are tested.

### 8. Evidence and DB reads

**`<evidence-dir>/observer.jsonl`** is host-only and append-only, fsynced per line.
- Each line holds `seq`, `kind`, the context SHA-256 and a safely projected copy of the validated request, the
  instructions SHA-256, the times, and each subcommand's name, exit code and duration.
- It holds the projected observations (trajectory envelopes, marker reports, allowlisted hello fields, DB rows with
  explicit columns), the returned value and its SHA-256, and the decision.
- A line is written before the response is sent. An fsync failure stops new work and takes the broker's
  evidence-failure path.
- Permission descriptions and any stderr are logged only as SHA-256 plus length.
- A test feeds secret-bearing values through every path.

**`handoff-ledger`** holds the C4 receipts (section 5).

**DB reads** go through `kubectl --context admin@ai -n relay exec -i relay-postgres-0 -c postgres -- psql -X -At -v
ON_ERROR_STOP=1 -U postgres -d relay -f -`, with the verified SQL file on stdin:
- each file is `BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ;` (one snapshot per decision),
  `SET LOCAL statement_timeout = '5s'; SET LOCAL search_path = pg_catalog;`, schema-qualified explicit columns, tenant
  and session predicates and an exact expected row count;
- values come only from psql variables (`-v name=value`, validated canonical UUIDs or integers) and are referenced as
  `:'name'`;
- no SQL text, identifier or metacommand comes from a request.

`BEGIN READ ONLY` is only an accident guard: the reviewed host code is fully trusted, and a dedicated read-only role is
a follow-up.

### 9. Session integration (`relay_operator_session.py`, after #2062 merges)

- **Without an `observer` block** a session behaves exactly as now; the smoke has none.
- **With one,** the orchestrator:
  - verifies and loads the observer files;
  - stages the predecessor;
  - runs the pre-run holder step;
  - starts the gate and the host thread before the runner.

An observer failure, a death or an evidence failure sets the run's stop condition (the same path as a signal). The
orchestrator then stops the runner (SIGTERM, then the existing grace) and cancels the in-flight collector.

The observer drain runs in its own `finally` stage. It is bounded to 10 s and never delays the broker's cleanup or
revoke cutoff. No collector dispatches after the Lease is lost.

**Exit codes.** A new code, 9, means an observer failure. `PRECEDENCE` becomes `[3, 4, 5, 6, 7, 9, 8, 2]`: an observer
cause outranks the generic runner failure 8 it usually causes. `result.json` always records the observer cause, even
when another code wins. Evidence durability keeps its existing code.

### 10. Gates before a real run

- #2060 merged, with the two-socket adapter and a driver source bridge matching the deployed hub.
- #2062 merged, and its live smoke passed.
- The reaper (#2063) merged, and its crash drill passed.
- The projection-pin contract change is merged and the pin reviewed (section 6).
- An authoritative C10 field is named and both branches tested (section 7).
- The predecessor report is recovered and pinned.
- The pre-run holder checks pass on the live dev-worker-1.
- This implementation is reviewed and merged, and the live probe passes with measured latencies that fit the phase
  budgets.

**Acceptance evidence** for the real run:
- five successor launches, including two resumes;
- the phase results and accounting from the CLI's final record;
- the C4 ledger and receipts;
- the broker's logout, revoke and Lease release, or explicit unresolved outcomes.

**Afterwards** the new holder and reconciliation stay running and enabled, and the renewed credentials stay live.
Final grant revocation is a separate step, outside `replaceStopped()`.

## Critical files

- `scripts/relay_phase_a_observer.py` (new): the host thread, request schemas, retained state, collectors, policy,
  evidence and the C4 ledger.
- `scripts/relay-observer-gate.py` (new): the credential-less gate sidecar.
- `scripts/relay-phase-a/` (new):
  - `observer-client.cjs` and `phase-a-entry.cjs` (bundle);
  - `router-trajectory.js`, `observe-marker.py`, `holder-hello.py` and `holder-handoff.sh`;
  - `observer-config.json` and `predecessor-pin.json`;
  - `*.sql`.
- `scripts/relay_operator_session.py` and `scripts/relay-operator.py`: the `observer` block, verified loading, staging,
  the pre-run step, the gate and thread lifecycle, the stop condition, exit code 9 and the `observer-probe` subcommand.
- `scripts/tests/test_relay_phase_a_observer.py` (new), and `scripts/relay-phase-a/observer-client.test.cjs` (new).
- `docs/runbooks/relay.md`: the observer subsection, the services left running, and the gates.

## Verification

1. **Unit tests (docker python:3.13-slim, node:22).**
   - **Driver validators:** every collector's output, produced from faked `kubectl` and `ssh` responses, is fed
     through the pinned driver's real validators (`snapshot`, `marker`, `delta`, `preservingHandoff` and the inline
     validators). Cases:
     - installing and ready credentials;
     - renewed versus historical keys;
     - all marker phases;
     - the separate effort runId;
     - the canonical versus raw report hashes;
     - a future `notBefore`;
     - unsettled rows;
     - a Router restart.
   - **Gate and envelopes:**
     - duplicate keys, oversize, a second connection, a sequence gap, response tampering and oversize;
     - cancel on disconnect;
     - the host's per-kind clamps;
     - kind-order refusals.
   - **C8:** read allowed; marker decisions bound to the command; a third marker refused; wrong option kind, session,
     expiry or rebinding refused.
   - **C10:** `unsupported` until the field is pinned, then both values.
   - **C4:** a crash before and after every stage, a lost response, a lingering remote process, and a restart with a
     different evidence directory (the ledger prevents a replay).
   - **Secrets:** secret-bearing strings never reach the evidence. An fsync failure stops the run.
2. **Offline full flow.** Run the inference session's synthetic Unix-HTTP integration through the real broker code path:
   - the gate, the client and `phase-a-entry.cjs`;
   - the real collectors with faked external commands;
   - UID 10001 with the production mounts and environment.

   It must catch the seconds/milliseconds conversion, predecessor ownership, two-socket routing, exit-code propagation
   and 20 s lease renewals while collectors block.
3. **Live read-only probe** (`relay-operator.py observer-probe`). It runs, with no Relay session:
   - the deployment check, the trajectory envelopes for both historical keys and the marker baselines;
   - the route, the four-clock gate, the DB queries (shape and cardinality only), the pinned SSH host key and the
     staging of the predecessor;
   - the pre-run holder checks, read-only and without starting anything.

   Phase-dependent predicates are reported as "not yet applicable", not passed: renewed installation, live permissions,
   recovery, retirement. It records the per-call latency, which is used to budget the 15, 15 and 10-minute phases.

<!-- codex-review-status: pending -->
