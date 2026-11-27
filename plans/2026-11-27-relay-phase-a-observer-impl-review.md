# Implementation review — relay-phase-a-observer — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The component split is reasonable, and the SSH token validation, parameterized SQL, and credential-free client provide useful safeguards. The deliberate C10 and predecessor projection gates, and the measured schema corrections, are not findings.
- The preserving-holder handoff lacks required preservation checks, deadline fencing, reliable containment, and terminal classification of interrupted mutations.
- Permission, recovery, and retirement confirmations can approve insufficient evidence; resumed-session binding is also missing.
- Seven non-writing unit tests passed, and the worktree remained unchanged. Required collector-validator, crash, supervision, and full broker-flow coverage is substantially incomplete.

## Findings

### Execute the verified observer bytes
**Location:** scripts/relay_operator_session.py:164; scripts/relay_operator_session.py:522; scripts/relay_operator_session.py:566
**Severity:** blocker
<!-- codex: verify_observer hashes files into memory, but the broker subsequently imports the host modules from the mutable checkout and mounts that checkout for the gate, so the code executed with operator credentials need not be the code verified. Copy all observer files into a private staging directory, verify the copied bytes, and load and mount only that copy as the plan requires. -->

### Verify preservation before mutating the holder
**Location:** scripts/relay_phase_a_host.py:156; scripts/relay-phase-a/remote/dw.py:302; scripts/relay_phase_a_host.py:235
**Severity:** blocker
<!-- codex: The pre-run checks never establish the old root's pinned seal, the new root's unsealed state, the receipt's old/new identities, or drained offline state, yet C4 eventually returns preservedOldStateConfirmed:true; the started journal is also accepted without comparison to the preparation's expected journal. Add those source-backed checks before starting or resuming the holder, including artifact verification before startup, and derive the confirmation from their successful results. -->

### Contain an unpaused startup despite transport failure
**Location:** scripts/relay_phase_a_host.py:133; scripts/relay-phase-a/remote/dw.py:322
**Severity:** blocker
<!-- codex: Containment happens only after the complete remote startup response reaches the host, so an SSH timeout, failed status read, or exhausted budget after systemctl starts the service can leave an unpaused holder running without any stop attempt. Perform the paused-state check and containment on the remote host with reserved time, then independently classify an unavailable response and verify the stop outcome before recording contained-unpaused. -->

### Fence dispatch against Lease loss and window expiry
**Location:** scripts/relay_phase_a_host.py:115; scripts/relay_phase_a_host.py:125; scripts/relay_phase_a_observer.py:455
**Severity:** blocker
<!-- codex: Pre-run startup never calls lease_ok, ordinary collector dispatch does not check Lease ownership, and C4 checks only that its window has not already ended rather than clamping the forthcoming mutation to the remaining window and runner cutoff. Recheck ownership and cancellation immediately before dispatch, stop dispatch after Lease loss, and bound each mutating stage by the minimum remaining authorized deadline. -->

### Record terminal outcomes for interrupted mutations
**Location:** scripts/relay_phase_a_host.py:194; scripts/relay_phase_a_host.py:202
**Severity:** blocker
<!-- codex: Exceptions and timeouts after a ledger intent bypass both readback and terminal receipt creation, while successful resume/enable receipts can remain the last records even when final handoff verification fails. Add a bounded failure-classification path that records actual service/admission state and unresolved Docker or systemd operations in the ledger and result.json without assuming that terminating the local command terminated the remote operation. -->

### Reserve the ledger atomically and enforce its permissions
**Location:** scripts/relay_phase_a_host.py:52; scripts/relay_phase_a_host.py:58
**Severity:** important
<!-- codex: require_empty followed later by O_CREAT|O_APPEND is not an exclusive reservation, allowing competing or stale executions to pass the check and append intents to the same preparation; Windows privacy is also assumed from inheritance instead of enforced. Create the ledger exclusively before the first intent, retain a controlled append handle, and establish and verify the required private directory ACL. -->

### Permission descriptions bypass evidence redaction
**Location:** scripts/relay_phase_a_collectors.py:143; scripts/relay_phase_a_collectors.py:486
**Severity:** blocker
<!-- codex: Remote.sql stores complete permission context rows in obs.sql, which Host.handle serializes on success, so descriptions and option names are persisted verbatim despite the separate hashed permission projection. Keep raw query results out of evidence and apply an explicit field/type/length projection at the logging boundary; an in-memory sentinel check confirmed this leak. -->

### Rejection and echoed-profile paths also expose untrusted text
**Location:** scripts/relay_phase_a_observer.py:95; scripts/relay_phase_a_observer.py:483; scripts/relay_phase_a_collectors.py:331
**Severity:** important
<!-- codex: Refusal messages embed unexpected runner-supplied key names directly into evidence, and C2 returns and logs the entire supplied profiles objects after checking only selected fields, allowing arbitrary extensions to reach the audit file. Use fixed error identifiers with hashed untrusted details and construct validated profile projections instead of echoing input objects. -->

### Permission approval lacks authoritative generation and tool checks
**Location:** scripts/relay-phase-a/sql/permission.sql:5; scripts/relay_phase_a_collectors.py:469
**Severity:** blocker
<!-- codex: C8 compares generation only with retained runner context, never reads authoritative generation/rebinding state, and accepts marker descriptions without requiring the driver's exact Allow bash? title; a focused fixture with generation 999 and rebinding:true still returned true. Query and validate the current permission/session identity, require the exact tool identity and unambiguous option, and match the driver's full-input parsing and timeout rules before confirming. -->

### Recovery confirmation does not prove approved outcomes
**Location:** scripts/relay_phase_a_collectors.py:559; scripts/relay-phase-a/remote/dw.py:286
**Severity:** blocker
<!-- codex: C9 accepts every permission state except pending, retains no permission-ID/option records from C8 to compare, and treats any toolResult with a matching ID as terminal without checking rejection, execution outcome, or duplicate results. Retain and reconcile the exact C8 decisions and validate native result semantics for the denied and effects commands, returning unsupported when the transcript format cannot establish those facts. -->

### Retirement confirmation lacks the required configuration evidence
**Location:** scripts/relay_phase_a_collectors.py:583; scripts/relay-phase-a/remote/dw.py:292
**Severity:** blocker
<!-- codex: C11 accepts the last recorded high thinking level, any text containing SESSION_CONFIGURATION_CHANGED, and any prompt refusal without verifying a completed settings-applied receipt, current control epoch, exact notice text, or correlation to the probe operation. A focused fixture passed without a set-config receipt and with an unknown-state prompt, so require the correlated authoritative records or keep this collector unsupported. -->

### Bind resumed sessions before approving continuation
**Location:** scripts/relay_phase_a_collectors.py:401; scripts/relay_phase_a_observer.py:264
**Severity:** important
<!-- codex: The driver calls launch(true) after C9 and goes directly to C7, but bind_session is called only by C6, leaving run.session_id and generation attached to the closed session and making later resumed-session permissions fail. Establish the successor binding through the launch table, validate resume_from, resume_generation, native ID and acknowledgement, then update retained session state before continuation; the current resume_from branch is otherwise dead code. -->

### Distinguish continuation from a finished run
**Location:** scripts/relay_phase_a_observer.py:220; scripts/relay_phase_a_observer.py:266
**Severity:** important
<!-- codex: open2 is entered immediately after C10 or C11 and is also treated as permission to start the next run, although the real driver still performs continuation prompts, close, and final evidence afterward. Introduce a finished state backed by the final observation and authoritative closed-session state, and require the recovered-phase zero-attempt observation before advancing past C9. -->

### Support reused installation evidence without losing host binding
**Location:** scripts/relay-phase-a/sql/delivery.sql:10
**Severity:** important
<!-- codex: The query always requires an installation attached to the current launch/session, rejecting the explicitly permitted Codex effort case where the renewed credential's earlier installation is reused; meanwhile its consumer join does not constrain the consumer to the intended holder host. Implement the planned reusable-installation selection with exact credential, binding, generation and holder checks, and require the active consumer to belong to the pinned host. -->

### Preserve historical scope independently of runner input
**Location:** scripts/relay_phase_a_collectors.py:340; scripts/relay_phase_a_host.py:101
**Severity:** important
<!-- codex: C3 accepts any positive oldScope.since supplied by the runner and compares marker files with a config literal rather than initializing historical scope and baseline from the pinned report, so a shifted window can exclude historical evidence before replacement is authorized. Parse the independent report once, retain its original scope and baseline, and require C3 observations and context to agree with them. -->

### Supervise stalled work and bound pipe writes
**Location:** scripts/relay_phase_a_observer.py:375; scripts/relay_phase_a_observer.py:443; scripts/relay-observer-gate.py:80
**Severity:** important
<!-- codex: The supervisor checks thread existence but never examines beats or an active request deadline, while gate stdout and host stdin writes can block indefinitely without consulting a Budget. Implement deadline-aware I/O and active-work progress supervision so a wedged reader, worker, pipe write, or evidence operation triggers the broker stop condition rather than merely remaining an alive thread. -->

### Guard gate thread failures and strict JSON parsing
**Location:** scripts/relay-observer-gate.py:44; scripts/relay-observer-gate.py:139
**Severity:** important
<!-- codex: Deeply nested JSON can raise an uncaught RecursionError in a gate thread, leaving the process alive without a terminal fault, and strict_json accepts exponent overflow such as 1e999 as infinity despite the non-finite prohibition. Bound nesting and decoded numeric values, wrap both gate threads with terminal fault reporting, and test these cases alongside malformed host replies. -->

### Avoid rejecting a sequential request during reply completion
**Location:** scripts/relay-observer-gate.py:185; scripts/relay-observer-gate.py:207; scripts/relay-observer-gate.py:247
**Severity:** important
<!-- codex: The client resolves and closes as soon as it receives the reply, but the gate clears busy only later in the handler's finally block, so an immediately following legitimate request can race with that cleanup and receive busy. Synchronize reply completion with admission of the next connection, and test back-to-back successful requests without intervening sleeps. -->

### Enforce the planned host-owned budgets
**Location:** scripts/relay_phase_a_observer.py:34; scripts/relay_phase_a_observer.py:466
**Severity:** important
<!-- codex: C1, C2, C3 and C4 all receive the generic 120-second limit rather than their planned 20/10/100/90-second limits, no shared C1/C2 bootstrap deadline is retained, and a fresh worker Budget ignores time already spent in transit or queued. Track absolute monotonic deadlines from receipt, retain the shared bootstrap budget, and apply the per-stage caps independently of runner-supplied deadlines. -->

### Bound remote work independently of the local process
**Location:** scripts/relay_phase_a_collectors.py:95; scripts/relay_phase_a_collectors.py:125; scripts/relay-phase-a/remote/router.js:24
**Severity:** important
<!-- codex: Router scripts have neither an in-pod execution deadline nor fetch timeouts, and SSH's remote timeout uses the fixed cap even when the remaining local Budget is shorter, so killing kubectl or ssh can leave remote work running after cancellation. Pass a remaining-time bound into every remote script, abort waits and HTTP operations there, and make local subprocess waits cancellation-aware while preserving unresolved mutation classification. -->

### Honor the driver's timestamp boundaries
**Location:** scripts/relay_phase_a_collectors.py:291; scripts/relay_phase_a_collectors.py:357
**Severity:** important
<!-- codex: C1 ignores notBefore and stamps the host clock directly, so an allowed negative host/runner offset can make its observation predate the driver's checkpoint; C3's fixed two-second backdating can likewise leave windowStartUtc in the runner's future under a permitted larger positive offset. Wait against the relevant source clock for C1 and derive C3's start from a measured clock bound that satisfies the driver's start<=now validator. -->

### Replace the assumed gate transit bound
**Location:** scripts/relay_phase_a_collectors.py:230
**Severity:** important
<!-- codex: clocksWithin5s is asserted using wall-clock brackets and an assumed 500 ms gate-to-host transit time, so clock offset can be hidden by pipe delay and the claimed worst-case proof is not established. Obtain a bounded clock exchange or equivalent measured interval, use monotonic elapsed-time brackets, and fail when the required bound cannot be proven. -->

### Refresh Router identity across each observation
**Location:** scripts/relay_phase_a_collectors.py:97; scripts/relay_phase_a_collectors.py:172
**Severity:** important
<!-- codex: _routerPod is cached for the entire checkpoint, including waits and trajectory re-polls, so a container restart after the first identity read can still produce coverageConfirmed:true using the old restart count. Read and compare fresh pod identity before and after each observation, including the final snapshot, and establish the initial identity at the planned pre-run boundary. -->

### Retain newly observed unsettled rows
**Location:** scripts/relay_phase_a_collectors.py:153
**Severity:** important
<!-- codex: The polling loop discards each unsettled envelope, and rows_retained compares only against the previously returned snapshot, allowing a newly seen incomplete row to disappear on the next poll and be reported as zero added attempts. Retain every row ID seen during polling, permit only valid settlement changes, and fail if any observed row vanishes. -->

### Refuse an unknown recording-store identity
**Location:** scripts/relay-phase-a/remote/router.js:29
**Severity:** important
<!-- codex: Missing or unparseable ROUTER_DATABASE becomes the constant hash of unparsed, which coverage accepts as a stable store identity even though no database identity was established. Require a recognized, verified store configuration and retention projection, returning failed or unsupported when that identity cannot be obtained. -->

### Detect short evidence writes and preserve code 6
**Location:** scripts/relay_phase_a_observer.py:138; scripts/relay_operator_session.py:550
**Severity:** important
<!-- codex: Evidence.write ignores the byte count returned by its unbuffered write, allowing a truncated line to be fsynced and followed by a successful reply, while an fsync failure is routed only to observer_fault and therefore code 9 instead of the broker's evidence-failure code 6. Complete or reject short writes and propagate a distinct evidence failure into the broker's existing durability path. -->

### Keep observations when a collector fails
**Location:** scripts/relay_phase_a_observer.py:476; scripts/relay_phase_a_observer.py:482
**Severity:** important
<!-- codex: Observations are attached to the evidence record only on success, so failed or timed-out calls lose their collected command outcomes, timeout information and readbacks precisely when recovery needs them. Preserve a sanitized observation projection on every outcome and record individual command durations, which the planned latency and failure audit requires. -->

### Validate the predecessor pin before startup
**Location:** scripts/relay_phase_a_host.py:95; scripts/relay_operator_session.py:527; scripts/relay-phase-a/observer-config.json:98
**Severity:** important
<!-- codex: The broker verifies the report against manifest.predecessorSha256, but C1 checks a separate config predecessor.rawSha256 that remains null in the shipped configuration and is never populated or checked for consistency during preparation. Reconcile these pins and reject incomplete configuration before starting the holder, rather than discovering an inevitable C1 failure after the one-shot pre-run mutation. -->

### Match the driver's canonical serialization
**Location:** scripts/relay_phase_a_observer.py:87
**Severity:** important
<!-- codex: Python json.dumps is not equivalent to the driver's JavaScript canonical function: focused checks produced 1.0, -0.0 and 1e-07 where JavaScript produced 1, 0 and 1e-7, which can reject an unchanged report at C3. Use a serializer matching JavaScript number formatting and key ordering, or constrain and validate the report domain explicitly, with cross-language boundary tests. -->

### Stage prerequisites before consuming the handoff attempt
**Location:** scripts/relay_operator_session.py:424; scripts/relay_operator_session.py:554
**Severity:** important
<!-- codex: The holder is started before the state directory and predecessor volume are staged, reversing the planned order and consuming the durable one-shot ledger attempt even when a later Docker staging operation fails. Prepare and verify the production mounts and required artifacts before the pre-run mutation so staging failures remain safely retryable. -->

### Keep observer cleanup within the revoke cutoff
**Location:** scripts/relay_operator_session.py:614
**Severity:** important
<!-- codex: The added drain, gate-stop, gate-remove and predecessor-remove stages use independent fixed timeouts ahead of revoke without receiving the cleanup deadline, and a timed-out drain is followed by closing evidence while its worker may still run and write. Cancel the host immediately on broker stop, reserve the revoke budget, bound observer cleanup by the remaining time, and coordinate worker termination with evidence closure. -->

### Preserve observer fault classification during startup
**Location:** scripts/relay_operator_session.py:522; scripts/relay_operator_session.py:554; scripts/relay_operator_session.py:402
**Severity:** important
<!-- codex: Observer imports and most staging/process-start operations occur outside their observer-specific exception handlers, so failures there fall into the generic runner-error path and return code 8 instead of the specified observer code 9. Wrap the complete observer startup lifecycle, preserve interruption and Lease-loss classifications, and always record the observer cause separately in result.json. -->

### Make probe success reflect established evidence
**Location:** scripts/relay_phase_a_host.py:267; scripts/relay_phase_a_host.py:283
**Severity:** important
<!-- codex: The probe marks any non-throwing result successful, including route checks returning ok:false and transcript results such as sessions-0, while its historical-key trajectory checks use a fresh since and therefore do not exercise the predecessor's actual rows. Validate each result's success semantics, probe the retained historical window and required source formats, and report unsupported sources distinctly from passed checks. -->

### Add the required collector and broker-flow tests
**Location:** scripts/tests/test_relay_phase_a_observer.py:504; scripts/tests/test_relay_phase_a_observer.py:529; scripts/tests/test_relay_phase_a_observer.py:585
**Severity:** important
<!-- codex: The suite has no C6/C9/C11 collector fixtures, successful C4 or stage-by-stage crash tests, stalled-thread/slow-pipe tests, evidence fsync-failure tests, or full observer-enabled broker execution; driver agreement covers only rendering, one canonical example and a baseline marker, and can silently skip. Add the planned real-driver validator and production-mount flow tests, including both recovery launches, credential reuse, failure exit codes and deadline-preserving cleanup. -->

## Diff stat
 docs/runbooks/relay.md                            |  53 ++
 scripts/relay-observer-gate.py                    | 264 +++++++++
 scripts/relay-operator.py                         |  21 +-
 scripts/relay-phase-a/known_hosts                 |   1 +
 scripts/relay-phase-a/observer-client.cjs         | 135 +++++
 scripts/relay-phase-a/observer-client.test.cjs    |  90 ++++
 scripts/relay-phase-a/observer-config.json        | 101 ++++
 scripts/relay-phase-a/phase-a-entry.cjs           |  57 ++
 scripts/relay-phase-a/remote/dw.py                | 396 ++++++++++++++
 scripts/relay-phase-a/remote/router.js            |  88 +++
 scripts/relay-phase-a/sql/delivery.sql            |  14 +
 scripts/relay-phase-a/sql/holder-health.sql       |  10 +
 scripts/relay-phase-a/sql/permission.sql          |   9 +
 scripts/relay-phase-a/sql/renewed-credentials.sql |  15 +
 scripts/relay-phase-a/sql/session-commands.sql    |  14 +
 scripts/relay-phase-a/sql/session-launch.sql      |  14 +
 scripts/relay-phase-a/sql/session-permissions.sql |   8 +
 scripts/relay_operator_session.py                 | 186 ++++++-
 scripts/relay_phase_a_collectors.py               | 613 +++++++++++++++++++++
 scripts/relay_phase_a_host.py                     | 343 ++++++++++++
 scripts/relay_phase_a_observer.py                 | 495 +++++++++++++++++
 scripts/tests/test_relay_phase_a_observer.py      | 625 ++++++++++++++++++++++
 22 files changed, 3536 insertions(+), 16 deletions(-)