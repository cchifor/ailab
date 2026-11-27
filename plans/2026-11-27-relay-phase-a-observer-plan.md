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
- collects real machine evidence;
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

## Review resolution

Two rounds were run, each with codex and an independent Fable review. Round 1 produced 52 codex findings plus Fable's
report; round 2 produced 26 codex findings plus Fable's 8. **Every finding is accepted.** No finding was disputed, so
nothing is escalated. Where the reviewers chose differently:

- **Transport.** Both rejected the Windows file mailbox. **Chosen:** Fable's stdio-bridged gate sidecar, the broker's
  existing proxy pattern. Round 2's protocol fixes from both reviewers are in section 2.
- **C11 trajectory.** **Chosen:** codex's fresh snapshot against the retained one with an unchanged `since`.
- **Driver validators at runtime.** **Not adopted:** they need Runner state the client lacks. They are used in tests
  instead.
- **C4 re-entry.** Both reviewers found the compare-and-swap re-entry unprovable, because the maintenance status does
  not name the resuming operation. Fable also found it conflicted with the pre-run check. **Chosen:** codex's cut. The
  ledger records intent; a later run that finds a non-terminal intent classifies it read-only and refuses.
- **Cut as over-engineered** (both reviewers): the four-clock probe on every request (now C5 only), the
  returned/accepted bookkeeping, the `git show` loader (the broker's `copy_bundle` is reused), and `REPEATABLE READ` on
  single-statement reads.
- **Evidence sources for C6, C9 and C11.** Codex: blocker; Fable: important. The sources are now named from the live
  schema (section 4). A source the probe cannot establish leaves its collector `unsupported`, which is a gate, never a
  pass.

## Approach

### 1. Two halves: a credential-less client in the runner, collectors on the workstation

**Client** (`scripts/relay-phase-a/observer-client.cjs`, in the runner bundle). It implements the three integration
functions with three distinct envelopes (section 2) and strips `AbortSignal`. It holds no credentials and never
evaluates `instructions`: it sends their SHA-256.

**Entrypoint** (`scripts/relay-phase-a/phase-a-entry.cjs`, in the bundle):
1. requires the inference session's pinned `owner-phase-a-cli.cjs`;
2. converts the broker's epoch-second `RUNNER_DEADLINE` into the 13-digit millisecond string, via `runCli({env})`;
3. passes `--predecessor /predecessor/report.json --output /state/run/phase-a`, the client's functions and the pin
   loaded from the bundle (section 6);
4. propagates `runCli`'s return code.

**Staging.** `createStore()` needs the output's parent to exist and be private. The orchestrator therefore creates
`/state/run` as UID 10001, mode 0700, and leaves `/state/run/phase-a` absent.

**Host observer** (`scripts/relay_phase_a_observer.py`). It is a component of the `relay-operator.py session`
orchestrator, started only for a manifest with an `observer` block.

**Pinned code.** Its code, remote scripts, SQL and config are named with SHA-256 in the manifest. They are copied with
the broker's `copy_bundle` (hashes over the copied bytes, commit on gitea/main) and executed only from that copy.

**Credentials.** The collectors use the operator's host credentials, which never enter a container:
- `kubectl --context admin@ai -n <ns>` on every call;
- `ssh` to `c4@192.168.0.8` with `StrictHostKeyChecking=yes` against a pinned `known_hosts` entry.

### 2. Transport: a stdio-bridged gate sidecar

**The gate** is `relay-observer-gate-<run>`, launched like the proxy:
- `docker run -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges --log-driver none`;
- UID 10001, a pids limit of 32, 64 MiB of memory;
- the shared tmpfs socket volume;
- the pinned `relay-observer-gate.py`.

**What it serves.** It holds no credentials and serves `/run/relay-session/observer.sock`:
- **Connections.** One connection is one request; a second concurrent connection is refused.
- **Reads.** At most 64 KiB, with a 5 s read deadline. A request is one JSON line; duplicate keys and non-finite
  numbers are refused.
- **Framing.** The gate assigns the next `seq` and its receipt time `gateNow` (the Docker VM clock, which the runner
  shares). It writes `{"seq", "gateNow", "request"}` as one line to stdout.
- **Replies.** It waits for the host's line with that `seq` on stdin and relays it to the client. The client never
  sees or predicts `seq`: it validates `v` and the shape. All socket and pipe writes are bounded by the request
  deadline.

**Cancellation is terminal** (a one-shot run needs no resynchronization). A client disconnect or a deadline makes the
gate emit `{"seq", "cancel": true}` and refuse all further connections. A host reply that arrives after a cancel is
discarded.

**Supervision.** The gate is supervised as follows:
- **Startup.** The gate emits a ready line only after the socket listens. The runner starts only after that line
  arrives, within 10 s.
- **Logs.** The gate's own diagnostics go to stderr, with fixed words only.
- **Raw stdout.** Raw stdout frames contain permission descriptions. They are parsed in memory and never pass through
  the broker's evidence streamer or its `docker logs` recovery.

**The host side** has three threads, supervised by the orchestrator:
- a **reader** of gate stdout;
- one **collector worker**;
- an independent **supervisor** that watches both, plus the gate process.

Gate EOF, process death, a protocol violation, a thread death or a missed heartbeat goes to the run's stop condition
(section 9). The host never relies on a dead thread to report itself.

**Envelopes:**
- `checkpoint`: `{v:1, call, kind, name, instructionsSha256, deadline, context}`;
- `deployment`: `{v:1, call, deadline, context}`;
- `predecessor`: `{v:1, call, deadline, context: {source, schemaVersion, sessionId, generation,
  priorReportCanonicalSha256, commands}}`.

`name` is recorded only.

**Responses:**
- the shape is `{v:1, ok:true, value}` or `{v:1, ok:false, code}`, with `code` a fixed word: `refused`, `failed`,
  `deadline`, `unsupported` or `observer-down`;
- a response holds at most 512 KiB;
- host error text never travels.

**Deadlines.**
- **Clamps and budget.** The host clamps each request to its per-kind maximum: 120 s, 30 s for permission, 15 s for
  the predecessor, and the shared C1/C2 budget. It runs on one monotonic budget.
- **Remote steps.** Every remote step carries its own remote bound: `timeout --kill-after=2`, or the in-pod
  `node`/`psql` timeouts.
- **Unresolved operations.** A remote `docker run` or `systemctl` operation that a timeout interrupts is recorded as
  **unresolved** until a readback shows its outcome. No later step depends on its presumed termination.

### 3. Retained state, session binding and the transition table

**State.** The observer keeps run-scoped state in host memory. It is never restored from anything the runner can write:
- **Historical (C3):** the predecessor's codex snapshot (old principal, original `since`) and its marker baseline,
  initialized from the pinned predecessor report. It is kept **separate** from every successor baseline.
- **Bootstrap (C2):** the renewed profiles projection, the new Router keys (the principal allowlist for C5 and C7) and
  the earliest expiry. **C3:** the granted window. **Pre-run:** the holder's measured incarnation and journal, and the
  pinned Router pod identity.
- **Per successor run** (provider, runId; the effort run is its own runId): its own C5 trajectory and marker baseline,
  the last returned snapshot, the approved marker SHA-256 and the marker-decision counters.

**Session binding.** A session id is bound to this execution only through the hub table `agent_session_launches`. The
row must have:
- `actor_id` = the JIT operator user, and `created_at` at or after the run start;
- the pinned `profile_id` and revision 1, and the pinned cwd;
- `holder_id` and `holder_journal_id` equal to the measured holder.

C6 binds a lifecycle runId to the launch's `session_id`. A resume is bound through `resume_from` (the C9 closed
session), `resume_generation`, `resume_native_id` (unchanged) and `resume_acknowledge_unknown_tool_effects = false`. C8
and C10 for a session resolve the runId through these bindings, never by parsing.

**Transition table** (per run; a request outside it is refused):

| state | accepted next |
|---|---|
| start | `deployment` (prelude) |
| prelude done | C1 |
| C1 | C2 |
| C2 | `predecessor` (at most once), then C3 |
| C3 | C4 |
| C4, or a finished run | C5 for the next run in the fixed order: codex lifecycle, qwen lifecycle, codex effort |
| C5(run) | C6(run) |
| C6(run) | C7 or C8 (run's session) |
| C7 / C8 | C7 / C8, or C9 (lifecycle), or C11 (effort) |
| C9 | C7 (recovery imported, zero attempts) |
| that C7 | C10 |
| C10 = true | C7 / C8 (the reconcile prompt), then C7 / C8 to the run's end |
| C10 = false | C7 / C8 to the run's end |
| C11 | C7 / C8 to the run's end |

- `launch(true)` after C9 goes straight to C7, without C5 or C6.
- Per-kind counts are capped at the inventory maxima.
- C8 is optional anywhere a prompt is in flight.

### 4. Collectors by checkpoint kind

**Shared rules:**
- **Confirmations.** Every `...Confirmed` or `operatorVerified` field is set only when its named checks passed in
  this call. Any failed check returns `failed`.
- **`notBefore` waits.** These happen on the **source clock**: the pod for `requestedAt`, dev-worker-1 for
  `observedUtc`. The remote script sleeps until its own clock passes `notBefore`, then observes.
- **Unsettled rows** are re-polled with a bounded backoff. `since` never changes and no row is dropped.
- **Clock check (C5 only).** Each remote clock is sampled between two host monotonic stamps, and offset bounds are
  computed from those intervals. The runner's clock comes from `gateNow` against the host's receipt stamps. The check
  refuses unless every pair's worst-case offset is proven under 4 s. The result is `clocksWithin5s`.

**Collectors:**

**`deployment`.** Reads Deployment `relay`: `spec.replicas >= 1`, `observedGeneration == generation`, and updated,
ready and available equal to the spec. On the current ReplicaSet's pods, every `relay` container's `imageID` digest
equals the pinned receipt's digest, and the receipt supplies `hubSource`. Returns `{hubSource, ready, observedUtc}`.

**C1.** A fresh deployment observation, never reused from the prelude. The raw SHA-256 of the host's independent
predecessor copy must equal the pin. Budget: 20 s. Returns
`{confirmMetadataReview, reportDownloaded, operatorVerified, deploymentVerified, hubSource, observedUtc}`.

**C2.** Reads `router_credential_bindings` with its credential, for both pinned bindings:
- **ready:** the credential is `current_credential_id`;
- **installing:** `current_credential_id` is null and the credential is `pending_credential_id`.

The full projection (renewed key, generation, revision, expiry) must equal `context.profiles`, and `context.expiresAt`
must equal the earlier expiry. The keys are stored. Budget: 10 s. Returns
`{confirmRenewedCredentials:true, profiles, expiresAt}`.

**C3.**
- **Trajectory:** for `context.oldScope`, with a historical pinned principal. It holds exactly rows `338869f5-`
  (complete) and `811cab9f-` (cancelled), one attempt each.
- **Marker:** the codex baseline equals the historical baseline.
- **Timing:** both are collected after `notBefore` on their source clocks, and collection is capped at 100 s.
- **Window:** it starts at the reply time minus 2 s, so the validator's 120 s age bound holds. It ends at
  `min(start + 900 s, the C2 expiry - 60 s, the runner cutoff - the cleanup budget)`.
- **Report hash:** `priorReportCanonicalSha256` is the host's `canonical(parsed report)` hash and must equal
  `context.canonicalReportSha256`.

Returns `{confirmReplacement, reportDownloaded, operatorVerified, priorReportCanonicalSha256, windowStartUtc,
windowEndUtc, trajectory, marker}`.

**C4.** Section 5.

**C5.**
- **Holder:** maintenance status merged with the live hello and the artifact hashes (see below). It must show ready, 0
  sessions, 0 pending commands, the measured incarnation and journal equal to the context, and the published hashes.
- **Route:** exactly one enabled deployment, with the pinned account and model, `maxAttempts` 1 and no fallback.
- **Clock check.**
- **Trajectory:** the C2 key. The pod waits past `sinceAfter`, then sets `since = podNow`.
- **Marker:** a fresh baseline, which becomes this run's own baseline.

Returns `{trajectory, marker, coverageConfirmed, holderReadyConfirmed, routeConfirmed, clocksWithin5s}`.

**C6.**
- **Install receipt:** a `holder_router_installations` row for this run's launch, session, holder and the C2
  credential, binding and generation. Its `state` and `receipt` must be terminal-installed, `rendered_at` must be set,
  and `render_failure` null. The codex effort launch reuses the credential, so the binding's earlier install is allowed.
- **Consumer:** a `router_credential_consumers` row for that binding, credential and generation. It must be `active`
  on this holder's host, with `lease_expires_at` in the future.
- **File:** `credential.json` is checked through one descriptor: `os.open(O_NOFOLLOW)` in the fixed verified
  directory, then `fstat`. It must belong to c4, with mode 0600 and one link, and is never read.
- **Marker:** collected after `notBefore`, unchanged from C5's.

Returns `{marker, deliveryConfirmed}`.

**C7.**
- **Trajectory:** for `context.scope`, with an allowlisted principal and the same `since`, collected after
  `notBefore`.
- **Marker:** for `context.phase` when that is non-null.

Returns `{trajectory, marker?, coverageConfirmed}`.

**C8.** Section 7. Returns `true`.

**C9.** `context.generation` is the session that was just closed.
- **Holder:** the status and hello show the same incarnation and journal, 0 sessions and 0 pending commands.
- **Hub:** every `agent_commands` row of the closed session at that generation has a receipt with status `completed`.
  `unknown`, `withdrawn` and `rejected` stop the run. Every `permission_requests` row of the session is decided,
  matching C8's records.
- **Native record:** the closed session's native transcript, under the holder's native home, is read read-only. Every
  tool call must have a terminal result. The only shell calls must be the two marker commands, with the outcome C8
  approved: denied rejected, effects executed once.
- **Marker:** the approved marker bytes equal the retained SHA-256.

If the probe cannot parse the native transcript format, C9 stays `unsupported` (a gate). Returns `true`.

**C10.** Section 7 (a gate). Returns `{reconcile}`.

**C11.** Neither the Router, the hub nor the native record states the effort setting and refusal as one fact, so C11
cross-checks all three:
- **Effort:** `thought_level` `currentValue` is `high` at `context.generation` and the current control epoch. The
  source is the native session's applied config record, cross-checked against the hub's completed set-config
  `agent_commands` receipt.
- **Refusal:** the probe prompt's `agent_commands` receipt has `stopReason` `refusal`. The native record carries the
  exact `SESSION_CONFIGURATION_CHANGED` notice text the driver checks.
- **Trajectory:** fresh, with the retained `since`, showing 0 attempts added.

A source the probe cannot establish leaves C11 `unsupported`. Returns `true`.

**`predecessor`.** Section 6. Returns the raw UTF-8 projection.

**`coverageConfirmed`** is derived, not assumed:
- the pinned Router pod is unchanged (UID, restart count);
- its recording config is read at each observation and is unchanged and enabled (config digest equal to the pre-run
  one);
- the recording store's identity (database and retention settings from the Router config) is unchanged;
- every retained row id is present and unchanged;
- the envelope is a recognized one;
- empty snapshots additionally require the recording config's continuity.

Any gap or restart stops the run.

**Holder state** is one typed record. It merges maintenance `status` (admission paused and revision, pending commands,
seal, nativeStateReady) with the live hello (incarnation, journal, sessions) and the measured binary and profile
hashes. A missing field is a failure.

**Remote scripts** are piped over ssh stdin (`python3 -I -`) or `kubectl exec -i ... node -`, with no staging. Reads
are batched per checkpoint. The marker workspace root is the profiles' fixed cwd root, which is distinct from the
holder state root (open question Q5 to the inference session).

### 5. The preserving-holder handoff

**Pre-run step** (the orchestrator, under the Lease, before the gate and the runner). It is a mutation, so its intent
goes to the durable ledger first.

**The ledger:**
- **Location:** `%LOCALAPPDATA%\relay-operator\handoff-ledger\<preparationId>.jsonl`, private to the user (the
  directory ACL is set at creation), outside any run directory.
- **Writes:** created exclusively, and appended with flush plus fsync per record.
- **Records:** each carries a CRC. A torn or unreadable record makes the ledger unusable, and the run refuses.

1. **Read-only checks:**
   - both units' effective `ExecStart`, state paths and drop-ins equal the pins (holder v0.13.4 / 64dc50f4 / d406b1f3,
     connector, holder-maintenance image);
   - the handoff receipt's contents match the preparation, the old and new identities, the new root and the seal;
   - the old root is sealed with the pinned seal, and the new root is unsealed;
   - reconciliation is inactive;
   - offline maintenance `status` shows admission **paused** (persisted), 0 sessions and 0 pending;
   - the ledger has no record for this preparation.
2. **Start only `relay-holder.service`**, bounded to 30 s, after a ledger intent `pre-run-start`.
3. **Read back** a hello plus status. Admission must still be paused, with 0 sessions, 0 pending, `nativeStateReady`,
   and the pinned journal and root. The incarnation is **measured, not pinned**. If admission is not paused, the
   holder is stopped at once (as `activate.sh` contains a failed pause), and the run fails. A terminal ledger receipt
   records the result.

**C4** has a 90 s budget inside its 120 s. It re-checks the Lease, cancellation and the C3 window **before every
mutating stage**, and each remote deadline is clamped to the window and the runner cutoff.

1. **Ledger intent** `resume`, with a fresh operationId and the revision just observed.
2. **Resume** `<revision> <operationId>`, in 25 s or less.
3. **Ledger intent** `enable-start`. Then enable both units and start only reconciliation, in 15 s or less.
   - Enabling is the handoff's documented reboot behaviour (UPGRADE-USED.md), and the runbook records it.
4. **Read back** in 40 s or less:
   - **reconciliation unit:** its effective path;
   - **hello and status:** admission unpaused, the same incarnation, MainPID unchanged since the pre-run;
   - **`agent_holder_health`:** pinned tenant and host, the measured holder and journal, a timestamp after the resume.
5. **Return** the `preservingHandoff` object.
   - Fields come from status, hello, the receipt and the files.
   - `oldHolderId` and `oldJournalId` come from the context and are checked against the receipt.
   - `qualifiedHolder` is compared with the config, never selected from it.
   - The new identities must differ from the predecessor's, and `observedUtc` must be at or after `startedAt`.

**On a failure, timeout or cancellation:**
- nothing is rolled back;
- a read-only readback classifies the state into a terminal receipt;
- the final service and admission state, plus any unresolved remote operation, go into the result.

**No automatic re-entry.** A later run that finds any intent without a terminal success receipt refuses. The runbook's
recovery section explains the read-only classification. Revoking the JIT session does not undo the resume.

### 6. Pins, release receipts and the predecessor

**The observer config** (`scripts/relay-phase-a/observer-config.json`) pins:
- the tenant, the bindings, the profiles and the historical Router keys;
- the Router source `4e4bb34e…`, the Router pod selector and the recording-config digest rule;
- the marker template, the holder and connector pins, the preparation and the seal;
- the hub release receipts `{release, imageDigest, source}`;
- the SQL file hashes.

**The manifest's `observer` block** is `{files: {path: sha256}, config, predecessorReport: {path, sha256},
predecessorPin}`.

**Source compatibility (gate).** The driver pins `RUN_SOURCE = 5f90d03a` and schema 93. A v0.14.2 run needs the
inference session's reviewed source bridge and revalidated schema contracts.

**The predecessor report.** The orchestrator stages it through a `--network none` helper into its **own** volume. The
volume is mounted **read-only** at `/predecessor`: a regular, single-link file owned by 10001 with mode 0400, as
`runCli` requires. The runner cannot rewrite it through `/state`. C1 and C3 hash the host's independent copy (the
original file's raw SHA-256 must equal the pin), never the runner-visible one.

**The predecessor projection (gate).**
- **The contradiction.** The Runner clones `predecessorEvidencePin.sha256` at construction. The exact projection bytes
  include an `observedUtc` that must be under 120 s old at `replacementPreflight`.
- **Requested driver change.** Pin the SHA-256 of the canonical projection excluding `observedUtc`, and check
  freshness on that field.
- **The pin.** `predecessor-pin.json` is produced once by the reviewed `predecessor-commands.sql` (`querySha256`). It
  is reviewed in a PR and loaded by the entry before the Runner is constructed. It is never derived from callback
  output.
- **Tests:** stale bytes, changed receipts, a tenant or query mismatch, and the 15 s bound.

### 7. Confirmation policy (C8, C10)

**C8.** The permission comes from `permission_requests`, selected by explicit columns: id, session, state, expiry,
context and decision. It must be pending and unexpired, on a session bound to this run (section 3) at
`context.generation`, with no rebinding. Exactly one option must have `context.optionId`, and its kind must equal
`decisionKind`.

- **Relay message read:**
  - the title is exactly `Allow mcp__relay__relay_messages_read?`;
  - the description's JSON may carry only these optional fields: `channelId` (exactly
    `b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1`), `after` (a canonical decimal string up to 2^63-1) and `limit` (a safe
    integer from 1 to 50);
  - the decision is `allow_once`;
  - there is a per-run cap. The one-read-per-prompt rule stays with the driver.
- **Marker:**
  - the runId comes from the session binding;
  - the input's `command` must equal the observer's rendering, and `timeout` must follow the driver's rule;
  - the `denied` rendering allows only `reject_once`, and `effects` only `allow_once`, each once per lifecycle runId;
  - `context.expectedCommand` must equal the rendering.
- **Anything else** stops the run. The observer never returns `false` to mean "deny".

**C10 (gate).** The collector answers `unsupported` until the inference session names the authoritative native-branch
observation. Then:
- `true` exactly when that observation says reconstruction is required;
- `false` exactly when it positively says not;
- anything absent or unknown stops the run.

Both values are tested.

### 8. Evidence and DB reads

**`<evidence-dir>/observer.jsonl`** is host-only and append-only, fsynced per line.
- **Contents:** the gate `seq`, the kind, the context SHA-256 and a projected request, the subcommand names, exit codes
  and durations, the projected observations, the returned value and its SHA-256, and the decision.
- **Order:** a line is written before its reply. An fsync failure stops the run through the broker's
  evidence-failure path.
- **Untrusted text:** permission descriptions, option names and any stderr are logged only as SHA-256 plus length.
  Explicit field, type and length projections apply to every log path, errors included.
- **Test:** secret-bearing values are fed through every path.

**DB reads.** Each query runs as `kubectl --context admin@ai -n relay exec -i relay-postgres-0 -c postgres -- psql -X
-At -v ON_ERROR_STOP=1 -U postgres -d relay -f -`, with the verified SQL on stdin.
- **Transaction:** `BEGIN READ ONLY` plus `SET LOCAL statement_timeout = '5s'` and `SET LOCAL search_path =
  pg_catalog`. `REPEATABLE READ` is used only for a multi-statement decision.
- **Queries:** schema-qualified explicit columns, tenant and session predicates, and an exact expected cardinality.
- **Values:** only `-v` variables (validated UUIDs or integers) referenced as `:'name'`.

The reviewed host code is fully trusted. `BEGIN READ ONLY` is an accident guard, not a boundary. A dedicated read-only
role is a follow-up.

### 9. Session integration (`relay_operator_session.py`, after #2062 merges)

**Without an `observer` block,** nothing changes. **With one,** the orchestrator runs these steps in order:
1. copy and verify the observer files;
2. stage `/state/run` and the predecessor volume;
3. the pre-run holder step;
4. the gate and its ready line;
5. the host threads and the supervisor;
6. the runner.

**Stopping.** An observer fault, a supervisor alarm or an evidence failure sets the run's stop condition, as a signal
would. The runner is stopped (SIGTERM, then grace) and the in-flight collector is cancelled.

**Drain.** The drain is its own `finally` stage, bounded to 10 s. It never delays the cleanup or revoke cutoff. No
collector dispatches after the Lease is lost.

**Exit codes.**
- **Code 9** is for observer internal faults only: gate or thread death, a protocol violation, a pre-run failure.
- A collector `failed` (a phase A evidence outcome) stays inside the runner's 8.
- `PRECEDENCE` becomes `[3, 4, 5, 6, 7, 9, 8, 2]`.
- `result.json` always records the observer cause, and evidence durability keeps its code.

### 10. Gates before a real run

- #2060 merged, with the two-socket adapter and a source bridge matching the deployed hub.
- #2062 merged, and its live smoke passed.
- #2063 merged, and its crash drill passed.
- The projection-pin driver change merged, and the pin reviewed.
- The authoritative C10 field named, with both branches tested.
- The C9 native-transcript and C11 sources established by the probe (otherwise those collectors stay `unsupported`).
- The predecessor report recovered and pinned.
- The pre-run read-only checks passing on the live dev-worker-1.
- This implementation reviewed and merged, and the probe passing with measured latencies that fit the phase budgets.

**Acceptance evidence:**
- five successor launches, including two resumes;
- the phase results and accounting from the CLI's final record;
- the ledger receipts;
- the broker's logout, revoke and Lease release, or explicit unresolved outcomes.

**Afterwards:** the holder and reconciliation stay running and enabled, and the renewed credentials stay live. Final
revocation is separate.

## Critical files

- `scripts/relay_phase_a_observer.py` (new): the threads and supervisor, schemas, retained state and transitions,
  collectors, policy, evidence and ledger.
- `scripts/relay-observer-gate.py` (new): the gate sidecar.
- `scripts/relay-phase-a/` (new):
  - `observer-client.cjs` and `phase-a-entry.cjs`;
  - `router-trajectory.js`, `observe-marker.py`, `holder-state.py`, `native-transcript.py` and `holder-handoff.sh`;
  - `observer-config.json` and `predecessor-pin.json`;
  - `*.sql`.
- `scripts/relay_operator_session.py` and `scripts/relay-operator.py`: the `observer` block, staging, the pre-run step,
  the gate and threads, the stop condition, exit code 9 and `observer-probe`.
- `scripts/tests/test_relay_phase_a_observer.py` and `scripts/relay-phase-a/observer-client.test.cjs` (new).
- `docs/runbooks/relay.md`: the observer, the services left running, ledger recovery and the gates.

## Verification

1. **Unit tests** (docker python:3.13-slim, node:22):
   - **Real validators:** collector outputs built from faked command responses run through the pinned driver's real
     validators (`snapshot`, `marker`, `delta`, `preservingHandoff` and the inline ones). Cases: installing and ready
     credentials, renewed versus historical keys, separate historical and successor baselines, all marker phases, the
     effort runId, canonical versus raw hashes, a future `notBefore`, unsettled rows, a Router restart.
   - **Source fixtures for the true-only kinds:** source-backed projection fixtures for C6, C9 and C11, including
     terminal commands that lack the required evidence, which must fail.
   - **Gate:**
     - malformed input: duplicate keys, oversize, a partial line;
     - concurrency and timing: a second connection, a slow reader, terminal cancel, a late reply discarded;
     - lifecycle: EOF, the ready handshake, the supervisor's alarm.
   - **Transitions:** the transition table and per-kind caps.
   - **C8:** marker decisions bound to the command, and session-binding refusals.
   - **C10:** `unsupported` until the field is named, then both values.
   - **Ledger:**
     - a crash before and after each pre-run and C4 stage;
     - torn records;
     - a later run refusing a non-terminal intent;
     - an unresolved remote operation.
   - **Evidence:** the secret-bearing paths and an fsync failure.
2. **Offline full flow** through the real broker path: gate, client and entry, the real collectors with faked external
   commands, UID 10001 with the production mounts. It catches the seconds/milliseconds conversion, the `/state/run`
   and predecessor staging, two-socket routing, exit codes, and 20 s lease renewals while collectors block.
3. **Live read-only probe** (`relay-operator.py observer-probe`), with no Relay session:
   - **Hub and Router reads:** deployment, the historical trajectories, marker baselines, the route, the four-clock
     check, the DB query shapes and cardinalities.
   - **Host and staging:** the SSH host key, the predecessor staging, the pre-run read-only checks.
   - **Formats:** the native-transcript and C11 source formats, on the predecessor's retained records.

   Phase-dependent predicates are reported as "not yet applicable". It records the per-call latency used for the phase
   budgets.

<!-- codex-review-status: finalized -->
