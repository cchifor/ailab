# Implementation review — relay-phase-a-observer — round 1

<!-- codex-impl-review-status: complete -->

## Resolution

Every finding was accepted and fixed in commits 789dc835 through 27308ade (gate, host core, collectors, handoff, broker, tests), except for the two partial pushbacks below. The live read-only probe passes 22/22.

## Findings

### Execute the verified observer bytes
**Location:** scripts/relay_operator_session.py:164; scripts/relay_operator_session.py:522; scripts/relay_operator_session.py:566
**Severity:** blocker

### Verify preservation before mutating the holder
**Location:** scripts/relay_phase_a_host.py:156; scripts/relay-phase-a/remote/dw.py:302; scripts/relay_phase_a_host.py:235
**Severity:** blocker

### Contain an unpaused startup despite transport failure
**Location:** scripts/relay_phase_a_host.py:133; scripts/relay-phase-a/remote/dw.py:322
**Severity:** blocker

### Fence dispatch against Lease loss and window expiry
**Location:** scripts/relay_phase_a_host.py:115; scripts/relay_phase_a_host.py:125; scripts/relay_phase_a_observer.py:455
**Severity:** blocker

### Record terminal outcomes for interrupted mutations
**Location:** scripts/relay_phase_a_host.py:194; scripts/relay_phase_a_host.py:202
**Severity:** blocker

### Reserve the ledger atomically and enforce its permissions
**Location:** scripts/relay_phase_a_host.py:52; scripts/relay_phase_a_host.py:58
**Severity:** important

### Permission descriptions bypass evidence redaction
**Location:** scripts/relay_phase_a_collectors.py:143; scripts/relay_phase_a_collectors.py:486
**Severity:** blocker

### Rejection and echoed-profile paths also expose untrusted text
**Location:** scripts/relay_phase_a_observer.py:95; scripts/relay_phase_a_observer.py:483; scripts/relay_phase_a_collectors.py:331
**Severity:** important

### Permission approval lacks authoritative generation and tool checks
**Location:** scripts/relay-phase-a/sql/permission.sql:5; scripts/relay_phase_a_collectors.py:469
**Severity:** blocker

### Recovery confirmation does not prove approved outcomes
**Location:** scripts/relay_phase_a_collectors.py:559; scripts/relay-phase-a/remote/dw.py:286
**Severity:** blocker

### Retirement confirmation lacks the required configuration evidence
**Location:** scripts/relay_phase_a_collectors.py:583; scripts/relay-phase-a/remote/dw.py:292
**Severity:** blocker

### Bind resumed sessions before approving continuation
**Location:** scripts/relay_phase_a_collectors.py:401; scripts/relay_phase_a_observer.py:264
**Severity:** important

### Distinguish continuation from a finished run
**Location:** scripts/relay_phase_a_observer.py:220; scripts/relay_phase_a_observer.py:266
**Severity:** important

### Support reused installation evidence without losing host binding
**Location:** scripts/relay-phase-a/sql/delivery.sql:10
**Severity:** important

### Preserve historical scope independently of runner input
**Location:** scripts/relay_phase_a_collectors.py:340; scripts/relay_phase_a_host.py:101
**Severity:** important

### Supervise stalled work and bound pipe writes
**Location:** scripts/relay_phase_a_observer.py:375; scripts/relay_phase_a_observer.py:443; scripts/relay-observer-gate.py:80
**Severity:** important

### Guard gate thread failures and strict JSON parsing
**Location:** scripts/relay-observer-gate.py:44; scripts/relay-observer-gate.py:139
**Severity:** important

### Avoid rejecting a sequential request during reply completion
**Location:** scripts/relay-observer-gate.py:185; scripts/relay-observer-gate.py:207; scripts/relay-observer-gate.py:247
**Severity:** important

### Enforce the planned host-owned budgets
**Location:** scripts/relay_phase_a_observer.py:34; scripts/relay_phase_a_observer.py:466
**Severity:** important

### Bound remote work independently of the local process
**Location:** scripts/relay_phase_a_collectors.py:95; scripts/relay_phase_a_collectors.py:125; scripts/relay-phase-a/remote/router.js:24
**Severity:** important
<!-- codex: Router scripts have neither an in-pod execution deadline nor fetch timeouts, and SSH's remote timeout uses the fixed cap even when the remaining local Budget is shorter, so killing kubectl or ssh can leave remote work running after cancellation. Pass a remaining-time bound into every remote script, abort waits and HTTP operations there, and make local subprocess waits cancellation-aware while preserving unresolved mutation classification. -->
<!-- opus-pushback: Fixed in part (1ed2a915). router.js gets an absolute deadline, exits at it and bounds every fetch with AbortSignal.timeout; the ssh remote `timeout` is now derived from the remaining budget, and every local wait is capped by that same budget. A cancellation that arrives during a subprocess still waits out that subprocess's bounded timeout rather than killing it: the gate's cancel is terminal, the late reply is discarded, and mutating stages re-check the Lease, the window and cancellation before each step. Interruptible Popen polling would add complexity without changing any outcome. -->

### Honor the driver's timestamp boundaries
**Location:** scripts/relay_phase_a_collectors.py:291; scripts/relay_phase_a_collectors.py:357
**Severity:** important

### Replace the assumed gate transit bound
**Location:** scripts/relay_phase_a_collectors.py:230
**Severity:** important

### Refresh Router identity across each observation
**Location:** scripts/relay_phase_a_collectors.py:97; scripts/relay_phase_a_collectors.py:172
**Severity:** important

### Retain newly observed unsettled rows
**Location:** scripts/relay_phase_a_collectors.py:153
**Severity:** important

### Refuse an unknown recording-store identity
**Location:** scripts/relay-phase-a/remote/router.js:29
**Severity:** important

### Detect short evidence writes and preserve code 6
**Location:** scripts/relay_phase_a_observer.py:138; scripts/relay_operator_session.py:550
**Severity:** important

### Keep observations when a collector fails
**Location:** scripts/relay_phase_a_observer.py:476; scripts/relay_phase_a_observer.py:482
**Severity:** important

### Validate the predecessor pin before startup
**Location:** scripts/relay_phase_a_host.py:95; scripts/relay_operator_session.py:527; scripts/relay-phase-a/observer-config.json:98
**Severity:** important

### Match the driver's canonical serialization
**Location:** scripts/relay_phase_a_observer.py:87
**Severity:** important

### Stage prerequisites before consuming the handoff attempt
**Location:** scripts/relay_operator_session.py:424; scripts/relay_operator_session.py:554
**Severity:** important

### Keep observer cleanup within the revoke cutoff
**Location:** scripts/relay_operator_session.py:614
**Severity:** important

### Preserve observer fault classification during startup
**Location:** scripts/relay_operator_session.py:522; scripts/relay_operator_session.py:554; scripts/relay_operator_session.py:402
**Severity:** important

### Make probe success reflect established evidence
**Location:** scripts/relay_phase_a_host.py:267; scripts/relay_phase_a_host.py:283
**Severity:** important

### Add the required collector and broker-flow tests
**Location:** scripts/tests/test_relay_phase_a_observer.py:504; scripts/tests/test_relay_phase_a_observer.py:529; scripts/tests/test_relay_phase_a_observer.py:585
**Severity:** important
<!-- codex: The suite has no C6/C9/C11 collector fixtures, successful C4 or stage-by-stage crash tests, stalled-thread/slow-pipe tests, evidence fsync-failure tests, or full observer-enabled broker execution; driver agreement covers only rendering, one canonical example and a baseline marker, and can silently skip. Add the planned real-driver validator and production-mount flow tests, including both recovery launches, credential reuse, failure exit codes and deadline-preserving cleanup. -->
<!-- opus-pushback: Added (27308ade): C6 reuse, C9 reconciliation and outcomes, C8 authoritative generation/rebinding/tool, C2 extensions, C4 success plus an interrupted stage, pre-run lost-response containment, preservation drift, Lease loss, ledger exclusivity, JS numbers, short writes, gate nesting/overflow/back-to-back, verified-copy import. 107 tests. Not added: the full observer-enabled broker run through the real CLI. It needs #2060's CLI on main (a gate in the plan). It lands with the phase A bundle manifest PR, which is the first change that can run it. -->

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
