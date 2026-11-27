# Implementation review — relay-phase-a-observer — round 2

<!-- codex-impl-review-status: pending -->

## Summary

- Rechecked the 33 round-1 findings against the finalized plan, implementation and supplied driver. Verified-copy loading, evidence redaction, historical scope retention, resumed-session binding and recording-store identity are substantially improved.
- C10, C11 and the predecessor projection correctly remain explicit Unsupported gates. The measured journal, receipt and installed-unit pins are accepted.
- Remaining blockers concern pre-run verification, containment and preserving mutation receipts during cancellation. Further findings cover gate concurrency, clock calculations, recovery evidence and failure classification.
- Both round-1 pushbacks are strengthened below. Fourteen focused non-writing tests passed; additional in-memory fixtures reproduced several remaining defects. The worktree is unchanged; full Unix-socket, production-mount and live execution tests were not run.

## Findings

### Complete verification before starting the holder
**Location:** scripts/relay_phase_a_host.py:190; scripts/relay-phase-a/remote/dw.py:171; scripts/relay-phase-a/remote/dw.py:337
**Severity:** blocker
<!-- codex: round-2: The new-root journal projection still discards seal and session/command state, so a seal appended after the pinned pause passes the pre-run checks; artifact verification also omits the executable that systemctl will start, whose hash is checked only later by holder_record. Establish the new root's unsealed, drained state and hash the installed executable against the qualified pin before reserving the startup intent or starting the service. -->

### Make remote startup containment exception-safe
**Location:** scripts/relay-phase-a/remote/dw.py:359; scripts/relay_phase_a_host.py:217
**Severity:** blocker
<!-- codex: round-2: Containment is reached only if hello and holder return normally: a status-read exception after systemctl start skips the stop entirely, and the outer SSH timeout can kill this sequence before it reaches containment because no stop budget is reserved. Protect the post-start sequence with exception-safe containment, reserve sufficient time for a verified stop, and refuse dispatch when the remaining budget cannot accommodate that workflow. -->

### Keep transitional service states unresolved
**Location:** scripts/relay_phase_a_host.py:253; scripts/relay_phase_a_host.py:260; scripts/relay-phase-a/remote/dw.py:371
**Severity:** important
<!-- codex: round-2: _classify_start reports every state except active as not-running, including activating and an unavailable ActiveState, although a timed-out systemctl start can leave a queued job that subsequently starts the holder; containment also trusts stopped without checking the returned unit state. Poll or retain these outcomes as unresolved, and require a positively terminal stopped state before recording not-running or contained-unpaused. -->

### Compare the handoff window in one clock domain
**Location:** scripts/relay_phase_a_collectors.py:395; scripts/relay_phase_a_host.py:165
**Severity:** important
<!-- codex: round-2: C3 now constructs its window on the runner's clock through runner_floor, but mutation_cap compares that window with the workstation's time.time and derives the remote cap from that mismatched comparison. A permitted clock offset can reject the beginning of a valid window or overestimate its remaining duration; retain conservative monotonic bounds for the granted window and runner cutoff and use those bounds before every mutation. -->

### Preserve receipts during cancellation (round-1 pushback)
**Location:** scripts/relay_phase_a_collectors.py:87; scripts/relay_phase_a_host.py:344; scripts/relay_operator_session.py:660; scripts/relay_operator_session.py:668
**Severity:** blocker
<!-- codex: round-2: Retain the finding: waiting out a bounded subprocess changes the outcome because it can exceed the 10-second drain, after which cleanup closes the ledger while the daemon worker still needs to append its mutation receipt; the independent 45-second classification can exceed that drain as well. Make cancellation and classification cooperate with cleanup, keep the ledger available until receipt persistence completes or a durable unresolved outcome is recorded, and allow revocation to proceed independently. -->

### Finish C4 validation before recording success
**Location:** scripts/relay_phase_a_host.py:319; scripts/relay_phase_a_host.py:324
**Severity:** important
<!-- codex: round-2: The handoff success receipt is appended before runner_floor and the observed>=startedAt check, which sit outside the classification try/except; a failure there leaves handoff/ok as the terminal ledger record despite a failed C4. Include all final evidence validation and cancellation/deadline checks in the guarded completion path, then write the terminal success receipt only after they pass. -->

### Preserve unresolved daemon operations and final state in the result
**Location:** scripts/relay_phase_a_host.py:341; scripts/relay-phase-a/remote/dw.py:382; scripts/relay_operator_session.py:434
**Severity:** important
<!-- codex: round-2: The failure classifier records a snapshot of admission and unit states but does not establish whether a timed-out Docker maintenance container or systemd job can still complete afterward, and its state projection never reaches result.json. Track or explicitly retain unresolved remote operations, correlate resume readback with its operation ID, and include the final service/admission classification and unresolved outcomes in the broker result. -->

### Reserve revocation time across the whole cleanup
**Location:** scripts/relay_operator_session.py:658; scripts/relay_operator_session.py:672; scripts/relay_operator_session.py:714
**Severity:** important
<!-- codex: round-2: cleanup_deadline bounds only observer-drain; gate stopping/removal, predecessor removal, log recovery and the 120-second state export still run with independent timeouts before revoke. When cleanup begins near the runner's cleanup cutoff these stages can consume the remaining revocation time, so apply the remaining deadline to every pre-revoke stage or move optional collection/removal after the authority cleanup. -->

### Give the gate busy flag an owner
**Location:** scripts/relay-observer-gate.py:228; scripts/relay-observer-gate.py:247
**Severity:** important
<!-- codex: round-2: Clearing busy before sending fixes the original sequential-request refusal, but the old handler's finally block clears it again after a successor may have acquired it; a third connection can then enter and overwrite waiting while the successor is outstanding. Release admission exactly once using a handler ownership token or equivalent synchronization, and test the interleaving where the successor starts before the previous handler exits. -->

### Make gate stalls and thread faults visible to the host
**Location:** scripts/relay-observer-gate.py:112; scripts/relay-observer-gate.py:298; scripts/relay_phase_a_observer.py:543
**Severity:** important
<!-- codex: round-2: Gate.emit still performs an unbounded pipe write, and guarded only sets a local terminal flag on an unexpected thread error while leaving the process and stdout open; the host can therefore see a live gate with no inflight request and never report an observer fault. Bound gate writes and publish terminal faults through a supervised channel or process exit so blocked framing and dead gate threads reliably reach the broker stop condition and code 9. -->

### Anchor clock calculations to a common monotonic timeline
**Location:** scripts/relay_phase_a_collectors.py:45; scripts/relay_phase_a_collectors.py:262; scripts/relay_phase_a_observer.py:351; scripts/relay_phase_a_observer.py:475
**Severity:** important
<!-- codex: round-2: Each clock bracket takes a fresh wall-clock anchor, so a host clock step between samples invalidates comparison of their offset intervals; a fixture with a 10-second step and corresponding remote skew nevertheless reported worstPairMs=200, while runner_floor and bootstrap accounting also use wall-clock elapsed time. Use one wall/monotonic anchor for the complete exchange, retain monotonic receipt timestamps for elapsed budgets and runner estimates, and reject detected clock discontinuities. -->

### Keep permission option IDs unambiguous
**Location:** scripts/relay_phase_a_collectors.py:550
**Severity:** important
<!-- codex: round-2: Replacing the optionId uniqueness check with an of_kind check permits two options with the same ID but different kinds; a fixture containing allow_once and reject_once under o1 still confirmed the denied marker, although the submitted ID no longer identifies a unique decision. Require exactly one option with the selected ID and verify its kind, while retaining the new uniqueness check for the requested kind. -->

### Preserve native shell-call multiplicity
**Location:** scripts/relay_phase_a_collectors.py:645
**Severity:** important
<!-- codex: round-2: Converting shellCommands to a dictionary keyed by command hash silently discards repeated calls with distinct tool-call IDs, so duplicateResults does not catch them and an earlier unknown or failed outcome can be overwritten; a three-call fixture still returned C9=true. Validate the original call list and require exactly one denied call and one effects call, each with its own single terminal result, before confirming recovery. -->

### Complete JavaScript canonicalization
**Location:** scripts/relay_phase_a_observer.py:122; scripts/relay_phase_a_observer.py:126
**Severity:** important
<!-- codex: round-2: The new float formatter passed the sampled comparisons, but integers bypass it: canonical(1000000000000000000000) produces decimal digits instead of JavaScript's 1e+21, and 9007199254740993 differs after JavaScript Number rounding; lone-surrogate JSON strings also fail during encoding instead of receiving JSON.stringify's escape. Implement JavaScript semantics for the entire accepted JSON domain, or validate a restricted domain before startup, with cross-language integer and Unicode boundary tests. -->

### Preserve evidence-failure precedence on every path
**Location:** scripts/relay_operator_session.py:584; scripts/relay_phase_a_observer.py:424
**Severity:** important
<!-- codex: round-2: EvidenceFailure during pre-run is converted to code 9 by _observer_prepare, and Host.fault ignores a later evidence=True notification once any earlier fault has been recorded, preventing the required upgrade to code 6. Handle observer evidence failures explicitly during preparation and propagate durability failure independently of whether the observer already has a terminal fault reason. -->

### Preserve Lease-loss and interruption classification
**Location:** scripts/relay_phase_a_host.py:210; scripts/relay_phase_a_host.py:218; scripts/relay_operator_session.py:579
**Severity:** important
<!-- codex: round-2: Pre-run Lease checks raise observer Fault rather than LeaseLost, and the lost-response handler catches the broker's Interrupted exception and wraps it as Fault, so _observer_prepare's explicit preservation handlers cannot retain the intended codes. Record any necessary containment receipt, then propagate the original interruption or a typed Lease-loss cause so these paths produce code 8 or 7 respectively instead of code 9. -->

### Add the remaining lifecycle tests (round-1 pushback)
**Location:** scripts/tests/test_relay_phase_a_observer.py:712; scripts/tests/test_relay_phase_a_observer.py:774; scripts/tests/test_relay_phase_a_observer.py:804
**Severity:** important
<!-- codex: round-2: Retain a narrower finding: the added fixtures still omit observer-enabled broker execution, cancellation during each mutation/readback stage, cleanup with a live worker, blocked gate pipes, fault-precedence escalation and real-driver snapshot/handoff validation; these can run against fakes and the supplied driver without waiting for #2060 to merge. Add those deterministic tests now, including the changed staging order for non-observer/smoke runs, and keep the production-mount full-CLI run as an explicit prerequisite of the future bundle manifest. -->

## Diff stat

 docs/runbooks/relay.md                             |   4 +-
 ...026-11-27-relay-phase-a-observer-impl-review.md |  45 +-
 scripts/relay-observer-gate.py                     |  60 ++-
 scripts/relay-phase-a/observer-config.json         |  37 +-
 scripts/relay-phase-a/remote/dw.py                 |  49 ++-
 scripts/relay-phase-a/remote/router.js             |  35 ++-
 scripts/relay-phase-a/sql/delivery-reuse.sql       |  17 +
 scripts/relay-phase-a/sql/delivery.sql             |   1 +
 scripts/relay-phase-a/sql/permission-decisions.sql |   9 +
 scripts/relay-phase-a/sql/session-launch.sql       |   2 +-
 scripts/relay-phase-a/sql/session-permissions.sql  |   8 -
 scripts/relay-phase-a/sql/session-resume.sql       |  14 +
 scripts/relay-phase-a/sql/session-state.sql        |   9 +
 scripts/relay_operator_session.py                  | 149 ++++---
 scripts/relay_phase_a_collectors.py                | 384 +++++++++-------
 scripts/relay_phase_a_host.py                     | 418 ++++++++++++------
 scripts/relay_phase_a_observer.py                  | 250 ++++++++---
 scripts/tests/test_relay_phase_a_observer.py       | 487 ++++++++++++++++-----
 18 files changed, 1400 insertions(+), 578 deletions(-)