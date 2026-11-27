# Relay phase A observer: the checkpoint collector and deployment observer for automated phase A

## Codex Review

- The split between a credential-less runner and pinned host collectors is sensible, and retaining the driver's validators preserves important safeguards.
- The predecessor projection's freshness/hash conflict is unresolved; neither proposed workaround satisfies the current driver contract.
- Collector details need correction for renewed credentials, Router principals, collection boundaries, delivery evidence and confirmation-state tracking.
- The writable mailbox and mutating handoff need stronger filesystem, cancellation and cross-run replay protections.
- Verification must exercise actual collector outputs against the driver, broker integration and realistic timing, especially the shorter permission/projection deadlines and holder handoff.

## Context

The owner's standing directive (2026-11-27) says Relay phase A must run without manual owner steps. The inference
session's automation host (`docs/runbooks/inference-canary-v0113/AUTOMATED-PHASE-A.md`, ailab #2060) runs the unchanged
`Runner.replaceStopped()` driver non-interactively. It refuses to start until the operator links three integrations:

1. `collectCheckpoint({name, instructions, deadline, signal, context})` returns the evidence value for each driver
   checkpoint. The driver's own validator then checks it. An empty object, a stale observation or an unconditional
   `true` is not a substitute.
2. `observeDeployment(context)` returns `{hubSource, ready, observedUtc}` before any credential request (renewal
   prelude).
3. `readPredecessorEvidence(context)` plus `predecessorEvidencePin`: the optional, separately pinned read-only
   projection of the stopped predecessor's three command receipts (`projection-contract.md`).

The authenticated transport is done: the session broker (`scripts/relay-operator.py session`, ailab #2062) runs the
runner bundle in a network-less container that talks only to the scoped Unix-socket proxy. This plan adds the missing
**observer**. It collects real machine evidence (Router trajectories, the dev-worker-1 marker files, holder hello, hub
deployment, read-only database facts), performs the one operator action the driver asks for (the preserving-holder
handoff on dev-worker-1), and applies a fixed, reviewed canary policy to the confirmation checkpoints.

The checkpoint inventory (from `owner-phase-a.js` at the #2060 head): 13 `ask` sites, 11 reachable from
`replaceStopped()`. A run makes at least 51 checkpoint calls, up to about 79 with the Relay-read approvals. Each call
carries a structured `context.kind`; collectors key on it and never parse `name`, `instructions` or provider text.

## Approach

### 1. Two halves: a credential-less client in the runner, collectors on the workstation

- **Client** (`scripts/relay-phase-a/observer-client.cjs`, in the runner bundle) implements the three integration
  functions. Each call becomes one request to the host; the returned value goes straight back to the driver.
  The client holds no credentials, opens no network and never evaluates `instructions`. Only their SHA-256 travels,
  for the evidence record.
- **Entrypoint** (`scripts/relay-phase-a/phase-a-entry.cjs`, in the bundle): requires the inference session's pinned
  `owner-phase-a-cli.cjs` and calls `runCli({collectCheckpoint, observeDeployment, readPredecessorEvidence,
  predecessorEvidencePin})` with the client's functions.
  <!-- codex: Blocking integration mismatch: the supplied broker exports RUNNER_DEADLINE as epoch seconds, while runCli requires a 13-digit epoch-millisecond string. Specify the conversion, exact --predecessor/--output arguments and propagation of runCli's returned exit code. -->
- **Host observer** (`scripts/relay_phase_a_observer.py`) runs inside the `relay-operator.py session` orchestrator
  process on the workstation, as a thread active only while the runner container runs. Its collectors use the
  operator's existing host credentials, which never enter a container:
  - `kubectl --context admin@ai`, for `exec` into the llm-router pod (the Router admin token stays in the pod, as in
    the existing collector), `get` of the hub deployment, and read-only `psql` in relay-postgres-0;
  - `ssh c4@192.168.0.8` (dev-worker-1), running only pinned scripts copied from the repo.

### 2. Transport: a file mailbox on a dedicated bind mount

The runner stays `--network none` with no new socket. The orchestrator creates `<evidence-dir>/mailbox/`, an empty
directory, and bind-mounts it read-write at `/observer`. This is the runner's only host-visible writable path; the
state volume stays a Docker volume.

<!-- codex: Explain why a mailbox is preferable to framed stdin/stdout on this workstation or a dedicated Unix socket where supported; --network none itself does not prohibit Unix sockets. The mailbox may be appropriate for Windows/Docker interoperability, but introduces filesystem races, polling and writable host storage that those alternatives avoid. -->
<!-- codex: Specify ownership/ACL setup for the runner's UID 10001 and verify rename, visibility and permissions on the actual Docker backend. Bound total mailbox bytes and entries, including unexpected files, so the runner cannot exhaust the filesystem holding cleanup evidence. -->

- **Request.** The client writes `req-<seq>.json` (temp file plus rename) with
  `{version:1, seq, call, kind, name, instructionsSha256, deadline, context}`:
  - `call` is `checkpoint`, `deployment` or `predecessor`;
  - `seq` starts at 1 and increases by one; exactly one request is outstanding (the host enforces both, matching
    the driver, which allows one checkpoint at a time).
  <!-- codex: Define separate envelopes for all three callbacks: deployment receives only deadline/signal, and predecessor receives source/schema/session/commands/deadline/signal without a checkpoint kind or name. Strip AbortSignal explicitly and specify absent fields instead of assuming every call has the checkpoint schema. -->
- **Host handling.** The host polls every 100 ms. It accepts only the next `req-<seq>.json`: a regular file (lstat;
  symlinks and other types refused), at most 64 KiB, strict JSON. It validates the request against the per-kind schema
  in section 3:
  - UUIDs canonical lowercase; provider in `{codex, qwen}`; phase in the fixed set; ISO or epoch times finite; unknown
    keys refused;
  - `name` checked against that kind's allowed labels, as a consistency check only.
  <!-- codex: lstat followed by an ordinary open is vulnerable to replacement races in this runner-writable directory. Use race-safe opens and descriptor checks, bounded reads, exclusive temporary-file creation and safe response publication, with equivalent protection against Windows reparse points; reject duplicate JSON keys and non-finite numbers. -->
  <!-- codex: Syntactically valid UUIDs are not authorization to use host credentials. Specify checks binding requests to the pinned tenant, permitted profiles/principals, this execution's sessions and allowed checkpoint transitions, especially before C4 or a true-only confirmation. -->
- **Response.** The host writes `resp-<seq>.json` with either `{version:1, seq, ok:true, value}` or
  `{version:1, seq, ok:false, code}`. `code` is a fixed word such as `refused`, `failed`, `deadline` or `unsupported`.
  Host errors never travel; they go to the host evidence log only.
  <!-- codex: Specify atomic response publication, response byte limits and strict client validation of version, sequence and shape, including oversized trajectory envelopes. Because the runner can alter files on this mount, retained mailbox responses cannot serve as independent host evidence. -->
- **Deadlines.** The client polls for the response until `deadline` or abort. On abort or deadline it writes
  `cancel-<seq>` and throws, and the driver stops. Every collector subprocess is bounded by the request deadline minus
  a 2 s margin. A cancelled request whose collector already started a mutating step records an **uncertain** receipt
  (section 5) and is never repeated.
  <!-- codex: Budget against the actual contracts: ordinary checkpoints default to 120 s, permission has at most 30 s and predecessor reading has 15 s; C1/C2 and their intervening metadata reads share one 120 s bootstrap budget. Clamp caller deadlines to host-owned limits and use one remaining monotonic budget across waits, subprocesses, serialization and fsync. -->
  <!-- codex: A blocking collector must not prevent the host from noticing cancellation, and killing ssh or kubectl does not establish that remote work stopped. Specify independent cancellation monitoring and remote deadline enforcement, with no later handoff writes after cancellation and uncertain status whenever termination cannot be established. -->
- **After the runner exits.** The host stops, lists the mailbox and records any unexpected file names (not contents)
  in the evidence. It leaves the mailbox in the evidence directory.

### 3. Collectors by checkpoint kind

Each collector returns exactly the object the driver's validator asks for. Every boolean `...Confirmed` or
`operatorVerified` field is set only when the named check passed in this call; any failed check returns
`ok:false, code:failed`, and the driver stops. Times are observed at collection, never copied from context.

<!-- codex: Specify the observer's retained state: C2 establishes renewed profiles/expiry, C3 establishes the replacement window, and later confirmations require earlier trajectory and marker observations. Key it by execution, provider and runId, distinguish returned evidence from driver-accepted evidence, and never restore it merely from runner-writable mailbox files. -->

| kind / call | Collected (all read-only unless marked) | Returned |
|---|---|---|
| `deployment` (observeDeployment) and `renewal-deployment` (C1) | Hub Deployment `relay` in namespace relay: `observedGeneration == generation`, updated, ready and available replicas equal to the spec, every pod's `imageID` digest equal to the pinned release receipt's digest; the hub source is looked up from that receipt (section 6). C1 adds the predecessor-file check (section 6) for `reportDownloaded`. | `{hubSource, ready, observedUtc}`; C1 `{confirmMetadataReview, reportDownloaded, operatorVerified, deploymentVerified, hubSource, observedUtc}` <!-- codex: Require a positive replica count and an explicit selection of the Deployment's current pods and hub container; equality at zero replicas otherwise passes vacuously. Both callbacks require a fresh observation during their own review, so the renewal-prelude result cannot simply be reused for C1. --> |
| `renewed-credentials` (C2) | Read-only DB: for the two pinned bindings, the current credential IDs, their `ready` or `installing` states and the earlier expiry; must equal `context.profiles` and `context.expiresAt` | `{confirmRenewedCredentials:true, profiles, expiresAt}` <!-- codex: In the driver's valid installing case current_credential_id is null and pending_credential_id identifies the credential; a current-only query rejects normal renewal before holder startup. Validate the complete profiles projection, including renewed key, generation, revision and numeric expiresAt, rather than comparing only IDs and copying unchecked context fields. --> |
| `stopped-replacement` (C3) | Router trajectory for `context.oldScope` (original `since`); the codex marker baseline; the predecessor file hash; the window `[now, min(now+900 s, earliest credential expiry - 60 s, runner deadline - cleanup budget)]` | `{confirmReplacement, reportDownloaded, operatorVerified, priorReportCanonicalSha256, windowStartUtc, windowEndUtc, trajectory, marker}` <!-- codex: C3 also requires trajectory.requestedAt and marker.observedUtc at or after context.notBefore, exactly the two known complete/cancelled historical rows with one attempt each, and the unchanged predecessor baseline. Its context has neither renewed expiry nor the overall runner deadline, so obtain those from verified retained state and the orchestrator, not originalDeadline or this checkpoint's timeout. --> |
| `preserving-holder` (C4) | **MUTATING, once per run**: section 5 | the `preservingHandoff` object |
| `pre-launch` (C5) | holder hello (ready, 0 sessions, incarnation and journal equal to `context.holderId` / `journalId`); the route `canary-pi-<provider>` with exactly one enabled deployment, the pinned account and model, `maxAttempts` 1 and no fallback; the three clocks (workstation, Router pod, dev-worker-1) within 4 s; a fresh trajectory with `since = max(now, sinceAfter + 1 ms)`; the marker baseline | `{trajectory, marker, coverageConfirmed, holderReadyConfirmed, routeConfirmed, clocksWithin5s}` <!-- codex: Choose since using the Router's measured clock after waiting beyond sinceAfter; a workstation timestamp up to four seconds ahead can violate snapshot's since <= requestedAt requirement. Clock verification must include the runner clock and account for measurement round-trip time, and holder readiness must include current published pins and absence of pending/unrelated work. --> |
| `post-open` (C6) | `credential.json` for the provider's binding is owned by c4, mode 0600 and single-link; the DB binding and current credential are `ready/ready`; the marker baseline, collected at or after `notBefore` | `{marker, deliveryConfirmed}` <!-- codex: File permissions plus ready/ready do not prove installation of this run's selected credential/principal or holder/profile readiness, which this attestation covers. Add the matching delivery/install receipts and safe no-follow file inspection without logging credential contents. --> |
| `observation` (C7, the most frequent) | the trajectory for `context.scope` (same `since`), collected at or after `notBefore`; the marker for `context.phase` when it is non-null | `{trajectory, marker?, coverageConfirmed}` |
| `permission` (C8) | section 4 | `true` |
| `recover-history` (C9) | holder hello: same incarnation and journal as the context, 0 sessions, 0 pending commands; the marker for the run's last observed phase unchanged; DB: the session's prior generation closed with no command in an unknown state | `true` <!-- codex: context.generation identifies the session just closed, not a generation to decrement, and recovery creates a new Relay session. Require settled command/native-effect evidence and comparison with the retained approved marker bytes/hash; absence of a DB command named unknown alone does not prove absence of unknown tool effects. --> |
| `reconcile-choice` (C10) | section 4 | `{reconcile: <bool>}` |
| `retire-branch` (C11) | the session is open at `context.generation`; a fresh trajectory since the previous observation shows 0 added attempts; the effort is `high` per the session read | `true` <!-- codex: The driver reads effort from /settings.snapshot.configOptions, bound to generation/controlEpoch, and requires the specific configuration-changed refusal before this checkpoint. Identify the equivalent verified sources and compare a fresh snapshot against the retained snapshot with unchanged scope.since; a new window cannot establish the required delta. --> |
| `predecessor` (readPredecessorEvidence) | section 6 | raw UTF-8 projection |

- **Trajectories.** These use the existing collector's logic, moved into the repo as
  `scripts/relay-phase-a/router-trajectory.js`. It runs inside the llm-router pod with `node`, against
  `127.0.0.1:8787/admin/v1/trajectories` with the pinned key per provider. It projects the 11 row fields and emits the
  envelope `{version, source, origin, scope, requestedAt, observedAt, fetchedRows, rows, totalAttempts}`. It refuses at
  499 or more rows, or on a row outside the scope.
  <!-- codex: The existing collect.sh hard-codes historical keys, but renewal requires distinct new keys: only C3 uses oldScope, while C5/C7 must use the verified renewed principal supplied in context.scope. Keeping one static key per provider will fail successor snapshot validation. -->
  <!-- codex: coverageConfirmed needs evidence of continuously enabled recording, retained history and stable Router process/database identity, not merely a successful listing or a source string copied from config. Specify these checks and stop on gaps/restarts, including when both snapshots are empty and delta cannot detect lost coverage. -->
  <!-- codex: Do not retain collect.sh's fallback from an unrecognized response shape to an empty items array, which can manufacture zero-attempt evidence. Refuse malformed envelopes and define bounded polling for unsettled rows or delayed recording without changing since, dropping rows or retrying provider work. -->
- **Markers and hello.** These use the existing `observe.py` and `hello-state.py`, moved to `scripts/relay-phase-a/`.
  They are copied to a fresh `mktemp -d` on dev-worker-1 per call and removed afterwards. They read only the fixed
  paths.
  <!-- codex: Define the exact adapter from hello-state.py's scalars/profiles/sessions summary to the requested fields; it does not preserve nested admission state, while resume.sh obtains paused/revision from maintenance status. Missing fields must fail rather than become zero/false, and the new holder socket root must remain distinct from the original fixed marker workspace root. -->
  <!-- codex: Repeated temp-directory creation, copying and separate ssh connections add avoidable latency across 51–79 calls. Measure this cost and consider one verified private script directory per execution or batching reads within each checkpoint, while still collecting fresh observations. -->

### 4. Confirmation policy (C8 to C11)

The driver validators for C8, C9 and C11 accept only `true`, and C10 accepts a boolean. The observer must therefore be
the independent check, with its evidence recorded host-side (section 7).

- **C8 `permission`.** The observer reads the pending permission row from the hub DB (read-only, by
  `context.permission.id`). It requires the session, generation, pending state and option ID to equal the context, and
  the option kind to equal `decisionKind`. Then the fixed policy:
  <!-- codex: Pin the actual DB projection and joins: the driver validates context.title and JSON in context.description, not an established separate tool-name/input schema. Also require unexpired permission, no rebinding and exactly one matching once-only option, with the session tied to this execution. -->
  - `relay-message-read`: `decisionKind` is `allow_once`. The DB tool name is exactly
    `mcp__relay__relay_messages_read`. The input has only `channelId` (absent, or exactly
    `b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1`), `after` (a canonical decimal at most 2^63-1) and `limit` (1 to 50). At most
    one read is approved per prompt operation.
    <!-- codex: All three input fields are optional in communicationPermission; when present, after is an exact decimal string and limit is a safe integer. The checkpoint supplies no prompt operation ID, so define a verified DB correlation for the one-read limit or explicitly retain that responsibility in the driver's existing guard. -->
  - `marker`: the DB tool input equals the observer's own rendering of the fixed marker command for (provider, runId,
    step) and equals `context.expectedCommand`. `decisionKind` is `reject_once` for the run's first marker permission
    (`denied`) and `allow_once` for the second (`effects`). Any other marker permission stops the run.
    <!-- codex: Permission context contains no provider, runId or step, so define their checked derivation from retained session/phase state rather than parsing expectedCommand. Count marker decisions per lifecycle provider/runId: the full flow has two for Codex and two for Qwen, and validate the command field plus the driver's optional timeout rule rather than comparing an input object to a string. -->
  - Anything else stops the run. The observer never returns `false` to mean "deny": a denial is the driver's own
    `reject_once` decision, which the observer confirms.
- **C9 and C11.** `true` only after the table's checks pass.
- **C10 `reconcile-choice`.** `reconcile` is derived from a fact, not a default: `true` exactly when the recovered
  session's read-only state shows the prior branch still needs reconstruction. **Open: the inference session is to
  name the field**; until it does, the policy is `false` only when the session shows no pending reconciliation and no
  configuration-changed refusal, and anything ambiguous stops the run.
  <!-- codex: Blocking: neither this checkpoint context nor the supplied session contract establishes such a field, and absence of a refusal before continuation is not positive evidence that reconstruction is unnecessary. Resolve and test an authoritative native-branch observation for both true and false before enabling C10; absent/unknown evidence must stop. -->

### 5. The preserving-holder handoff (C4): the one mutating collector

This is `scratchpad/handoff/resume.sh`, productionized as `scripts/relay-phase-a/holder-handoff.sh` (run on
dev-worker-1 through `bash -s`), with every input pinned in the observer config:

<!-- codex: Enforce the previously granted C3 window and current orchestrator Lease before every mutating stage. At C4 the driver has not yet set this.run, so Runner.ask does not cap the handoff by the replacement window, and a syntactically valid C4 request alone is insufficient authorization. -->
<!-- codex: The existing resume.sh allows a 30 s hello wait and up to 90 s of hub-health polling, before adding this plan's initial 30 s hello wait, service startup and transport overhead. Define a measured end-to-end budget fitting C4's actual 120 s limit minus response margin, or obtain an explicit reviewed driver timeout change; extending only the client deadline cannot work. -->

1. **Preconditions (read-only).** The pinned holder binary hash is d406b1f3 (v0.13.4 at 64dc50f4) and the profiles
   hash is pinned. The state directory `/home/c4/.local/state/relay-holder-handoff-<preparationId>` exists, and so does
   the receipt `/home/c4/.config/relay-holder/used-handoffs/<preparationId>/complete.json`; its SHA-256 is computed.
   The old state root is sealed. No `relay-holder*` unit is active, except the holder in a re-entry (step 3).
   <!-- codex: Validate the completed receipt's contents and provenance against the preparation, old identities and new root, using full artifact hashes and safe file/path checks. Existence plus a newly computed hash does not prove a preserving handoff or that the old seal and retained state are the expected ones. -->
   <!-- codex: Verify both units' effective executable/state paths, drop-ins and connector/maintenance-image pins before starting either service. Checking the reconciliation path only after starting it is too late to prevent acting on the wrong root, and resume.sh has additional pinned dependencies beyond the holder binary. -->
2. **Once-only record.** `<evidence-dir>/observer-actions.jsonl` gets `{op: "holder-handoff", operationId, run}`, synced
   before any write on dev-worker-1. A second C4 request in the same run is refused. A run that finds an earlier run's
   uncertain or incomplete record refuses before acting, and the record is reviewed by hand. This is the one manual
   fallback; it is reached only after an interruption.
   <!-- codex: Blocking: #2062 requires a new evidence directory for every execution, so this per-run file cannot discover earlier incomplete handoffs by itself. Specify a durable registry keyed by preparation/holder, protected against concurrent runs, with intent and terminal stage receipts, directory synchronization and a defined read-only re-entry path that never redispatches uncertain mutations. -->
3. **Holder-only start.** `systemctl --user start relay-holder.service`. Then wait (bounded to 30 s) for a fresh hello
   on the new socket with admission paused, 0 sessions, 0 pending commands and `nativeStateReady`.
4. **Resume.** The holder-maintenance `resume <revision> <operationId>` step at the freshly observed revision (never
   from a snapshot). Then enable both units and start only `relay-holder-control-plane.service` (reconciliation).
   <!-- codex: Enabling both units changes reboot behavior beyond this bounded execution; document why that persistent change is required and authorized as part of the fixed handoff. Preserve the existing helper's checks that the new root is unsealed, reconciliation was inactive and the holder did not restart during resume. -->
5. **Read back.** The unit's effective state path, a fresh hello with `admissionPaused:false`, and the hub's
   `agent_holder_health` for this holder. Return
   `{holderId, journalId, oldHolderId, oldJournalId, preparationId, stateDirectory, handoffReceipt:{path, sha256},
   observedUtc, qualifiedRelease, holderSource, holderBinarySha256, profilesSha256, nativeRecovery, nativeStateReady,
   activeSessions, pendingCommands, admissionPaused, preservedOldStateConfirmed, operatorVerified}`. Every value comes
   from the hello and the files, except `oldHolderId` and `oldJournalId`, which the context supplies and the observer
   checks against the sealed old root's last hello record.
   <!-- codex: Require the hub-health row to match the pinned tenant/host and measured holder/journal, with a fresh post-resume timestamp, rather than accepting a stale matching row. preservingHandoff also requires each new identity to differ from its corresponding predecessor and observedUtc to be at least context.startedAt; the two new identities may equal each other. -->
6. **Stop on any failure.** Nothing is deleted or rolled back automatically: a half-started holder stays as found and is
   recorded. Steps 3 and 4 are the only writes on dev-worker-1 in the whole run.
   <!-- codex: Clarify that these are the only operational mutations: script staging/removal also writes remotely. Record the final known service/admission state and unresolved remote processes, since revoking the JIT session does not undo holder resume or stop these services. -->

### 6. Pins, release receipts and the predecessor

- **The observer config** is `scripts/relay-phase-a/observer-config.json`. It pins the bindings, profiles, Router keys
  and source `4e4bb34e…`, the marker-command template, the holder pins, the handoff `preparationId` (256a052b…) and the
  hub release receipts (`{release, imageDigest, source}`; v0.14.1 now, v0.14.2 when it rolls). The orchestrator refuses
  to start the observer unless the session manifest names the config and its SHA-256, and the config's commit is on
  gitea/main (the manifest rule from #2062).
  <!-- codex: Pin and execute verified copies of the host observer, remote scripts and SQL as well as the config; the runner bundle's existing hash check does not cover arbitrary host working-tree code. Define the manifest fields that bind that complete dependency set to reviewed commits. -->
  <!-- codex: A v0.14.2 receipt alone is insufficient: the supplied driver and renewal prelude require RUN_SOURCE=5f90d03a..., and the predecessor projection requires that source plus schemaVersion 93. Gate the rollout on a matching reviewed driver/source bridge and revalidated schema contracts. -->
- **The predecessor report** (the original saved report of `relay-pi-phase-a-20261126-v1`) reaches the runner as a
  separate read-only, 0600 file. Its SHA-256 is pinned in the session manifest. C1 and C3 report
  `reportDownloaded:true` only when the host re-hashes the evidence copy and gets the pin.
  <!-- codex: Distinguish this raw-byte SHA-256 from C3's priorReportCanonicalSha256, which the driver computes over canonical(parsedReport) and supplies as context.canonicalReportSha256. Verify both against the same preserved report; they are generally different hashes. -->
  <!-- codex: runCli also requires the mounted file to be canonical, regular, single-link, bounded and owned by the runner UID, so a host-owned 0600 bind mount can fail before startup. Specify a verified private staging copy with the required container-visible ownership and test it on the workstation's Docker backend. -->
- **The predecessor projection** uses a reviewed read-only SQL file, `scripts/relay-phase-a/predecessor-commands.sql`,
  whose SHA-256 is the pin's `querySha256`. The host runs it in a `BEGIN READ ONLY` transaction and emits the exact
  projection bytes. **Open (for the inference session):** the pin's `sha256` is of the exact bytes, but the bytes
  contain an `observedUtc` that must be under 120 s old when the driver checks it. That is at `replacementPreflight`,
  minutes after construction. The proposal is that the pin carries tenant, database label, query hash and commands,
  and the host supplies the fresh bytes at call time with their hash checked against a value the host commits to the
  mailbox evidence before the call. Alternatively, `observedUtc` could be excluded from the hashed bytes.
  <!-- codex: Blocking: Runner clones the independent pin at construction, predecessorProjection requires its sha256 field, and hashes all exact UTF-8 bytes including observedUtc; a later mailbox commitment satisfies none of these requirements. Excluding observedUtc also requires a reviewed contract change that preserves an independently pinned receipt payload and separately establishes freshness, rather than trusting a hash derived from the callback's own output. -->
  <!-- codex: The plan and manifest do not specify how predecessorEvidencePin, including tenant/database/query identity and the three command IDs, is provisioned before runCli constructs Runner. Define that trusted input path and test stale bytes, changed receipts, tenant/query mismatch and the 15 s callback bound; omitting the projection is not a usable fallback for the JIT actor's inaccessible predecessor operations. -->

### 7. Evidence

- **`<evidence-dir>/observer.jsonl`** is append-only, fsynced per line. Each line holds `seq`, `kind`, `name`,
  `contextSha256`, `instructionsSha256`, the start and end times, every subcommand with its exit code and duration, the
  raw collected outputs (trajectory envelopes, marker reports, hello scalars, DB rows), the returned value's SHA-256
  and the outcome.
  <!-- codex: Persist the validated, safely projected request identity, observations and decision before publishing success, and stop new work if evidence writing/fsync fails. A hash alone is insufficient to reconstruct an approval when its only underlying request copy remains runner-writable. -->
- **No secrets exist in any of it.** The Router admin token stays in the pod, the DB rows selected hold no credential
  columns, and the hello output carries no secrets. A test asserts the field allowlist.
  <!-- codex: The existing hello helper allows arbitrary short scalar strings, and raw permission descriptions, subprocess stderr or exception messages can contain sensitive data even without credential-named columns. Apply explicit field/type/length projections before all logging, including errors and unexpected filenames, and test secret-bearing values rather than relying on token-pattern detection. -->
- **`observer-actions.jsonl`** holds the C4 receipts (section 5).

### 8. DB reads

DB reads go through `kubectl exec relay-postgres-0 -c postgres -- psql -X -At -v ON_ERROR_STOP=1`, as postgres. Each
query file starts with `BEGIN READ ONLY;` and sets a 5 s `statement_timeout`. Values pass only as psql variables
(`:'session_id'`), never by string interpolation, after the host validates them as canonical UUIDs. The query files
are pinned by hash in the observer config.

<!-- codex: Pin the Kubernetes context/namespace, database and user explicitly, and execute query files through stdin or -f so psql variable substitution actually applies. Specify tenant/profile/session predicates, exact row cardinalities and typed handling for non-UUID values, with a consistent snapshot where a decision spans multiple reads. -->

Using the postgres superuser keeps this slice small. A dedicated read-only role is a follow-up; it was not
introduced here, because the reaper (#2063) shows the role and NetworkPolicy work is its own review.

<!-- codex: BEGIN READ ONLY is an accident guard, not a superuser privilege boundary: it does not prevent sensitive SELECTs, privileged functions or psql metacommands. If the role remains deferred, constrain execution to verified immutable SQL with schema-qualified explicit columns and a fixed search_path, disallow request-controlled SQL/identifiers, and document that the reviewed host code remains fully trusted. -->

### 9. Session integration (`relay_operator_session.py`, after #2062 merges)

- The manifest gains an optional `observer` block: `{config, configSha256, predecessorReport, predecessorSha256}`.
  Without it, a session behaves exactly as now; the smoke has no observer.
- With it, the orchestrator does the following:
  - creates and bind-mounts the mailbox;
  - mounts the predecessor read-only;
  - starts the observer thread before the runner starts, and stops it after the runner exits and before
    cleanup;
  - waits for the observer's in-flight collector (bounded) before the revoke.
  <!-- codex: Integrate observer failure into the orchestrator's active stop condition, and place cancellation/drain in independent finally stages covering startup failure, runner exit and signals. A hung observer must not delay the broker's cleanup/revocation cutoff, and no collector may continue dispatching after the run loses its Lease. -->
- **Exit code.** An observer internal failure is a new exit code (9) in the existing precedence, after the runner's
  own code.
  <!-- codex: The broker maps runner failure to 8 and uses an explicit PRECEDENCE list, so define the full updated order and retain the observer cause in result.json even when 8 wins. Evidence durability failure must still receive the existing evidence-failure treatment rather than disappearing behind a generic observer code. -->

## Critical files

- `scripts/relay_phase_a_observer.py` (new): the mailbox loop, request schemas, collectors, policy, evidence.
- `scripts/relay-phase-a/` (new):
  - `observer-client.cjs` and `phase-a-entry.cjs` (bundle);
  - `router-trajectory.js`, `observe.py`, `hello-state.py` and `holder-handoff.sh` (pinned remote scripts);
  - `observer-config.json`;
  - `predecessor-commands.sql` and the read-only query files.
- `scripts/relay_operator_session.py` and `scripts/relay-operator.py`: the manifest `observer` block, the mounts, the
  thread lifecycle and exit code 9.
- `scripts/tests/test_relay_phase_a_observer.py` (new), and `scripts/relay-phase-a/observer-client.test.cjs` (new).
- `docs/runbooks/relay.md` (the observer subsection) and the phase A runbook pointer (owned by the inference
  session).

## Verification

1. **Unit tests (docker python:3.13-slim, node:22).**
   - Request schema refusals: unknown kind, extra key, bad UUID, seq gap, a second outstanding request, symlink,
     oversize.
   - Each collector against faked `kubectl` and `ssh` runners: success, every failed check, deadline.
   - The C8 policy table: read allowed; marker denied then effects allowed; a third marker refused; wrong option kind,
     wrong command or wrong session refused.
   - C10 refuses on ambiguous state.
   - C4 once-only, including a re-entry after an uncertain record.
   - The evidence field allowlist (no token-like strings).
   <!-- codex: Feed actual collector-produced values into the pinned driver's real validators, covering installing credentials, renewed versus historical keys, all marker phases and the separate Codex effort runId, canonical hashes and future notBefore boundaries. Faked subprocess success alone can validate an incorrect collector contract. -->
   <!-- codex: Add mailbox replacement/hard-link/reparse races, duplicate keys, response tampering/oversize, file flooding, disk/fsync failure and cancellation during each stage. Exercise C4 crashes before and after every mutation, lost responses, lingering remote work and restart with a different evidence directory, proving no replay and bounded independent revocation. -->
2. **Offline full flow.** Run the inference session's synthetic Unix-HTTP integration with the real `phase-a-entry.cjs`
   and client over a real mailbox directory, against a fake host observer whose collectors return the fixture's
   synthetic evidence. This proves the wire and abort paths end to end, not live facts.
   <!-- codex: Include a flow through the actual #2062 orchestrator/proxy specification and real observer collectors with faked external commands, at UID 10001 with the production mounts and environment. This must catch seconds/milliseconds conversion, predecessor ownership, two-socket routing, exit-code propagation and 20 s lease renewals while collectors block. -->
3. **Live read-only probe.** `relay-operator.py observer-probe` exercises `deployment`, the trajectory for both
   providers, the marker baseline, the route, the clocks and the DB reads against the live estate, with no Relay
   session and no holder start. It expects every check to pass except the holder hello, because the holder is down by
   design until C4.
   <!-- codex: Distinguish successful read/query validation from phase-dependent predicates: renewed installation, live-session permissions, recovery and retirement cannot all pass before renewal and launch. Capture collector latency and use it to budget the full 15/15/10-minute phases, boundary waits and handoff; the supplied synthetic flow already takes about five minutes without real kubectl/ssh costs. -->
4. **The real run** is gated on all of these:
   - #2060 merged with the two-socket adapter;
   - #2062 merged and its live smoke passed;
   - the reaper (#2063) merged, with its crash drill passed;
   - v0.14.2 rolled and its receipt pinned;
   - the predecessor report recovered and pinned;
   - this plan's implementation reviewed and merged.
   <!-- codex: Add explicit gates for the resolved projection-pin contract, an authoritative C10 observation, verified holder preparation/maintenance dependencies and a mutually compatible driver/config/schema bundle. The existing gates do not resolve those open blockers merely by merging this implementation. -->
   <!-- codex: Define real-run acceptance evidence as well as entry gates: five successor launches including two resumes, phase results and accounting, preserving-handoff receipts, and confirmed broker logout/revocation or explicit unresolved outcomes. State which holder services and renewed credentials remain afterward, since final grant revocation is outside replaceStopped(). -->

<!-- codex-review-status: complete -->