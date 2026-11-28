# Implementation review — relay-phase-a-observer — round 2

<!-- codex-impl-review-status: finalized -->

## Resolution (final)

Every round-2 finding was accepted, and both strengthened round-1 pushbacks were too. The fixes are in 82693a61..HEAD:
- gate admission ownership and terminal-first answers;
- exception-safe host-side containment with a reserved stop;
- the prepared-journal and executable pins;
- one monotonic timeline for clocks, budgets and the C3 window;
- receipt-first, classified handoff outcomes reaching result.json;
- typed Lease loss and evidence precedence;
- revoke and logout before every optional cleanup stage, with the ledger and evidence kept open for an undrained worker;
- JavaScript-exact integers and lone surrogates;
- option-ID uniqueness;
- per-call native multiplicity;
- the remaining lifecycle tests, including C4's output validated by the pinned driver's own preservingHandoff().

The broker, proxy, operator and observer suites (122 tests) pass three consecutive times, and the live read-only probe passes 22/22. The full observer-enabled broker run through the real CLI remains a prerequisite of the phase A bundle manifest PR. That run needs #2060 on main, and it is a plan gate. Two review rounds were run (the cap), and no disagreement remains.

## Findings

### Complete verification before starting the holder
**Location:** scripts/relay_phase_a_host.py:190; scripts/relay-phase-a/remote/dw.py:171; scripts/relay-phase-a/remote/dw.py:337
**Severity:** blocker

### Make remote startup containment exception-safe
**Location:** scripts/relay-phase-a/remote/dw.py:359; scripts/relay_phase_a_host.py:217
**Severity:** blocker

### Keep transitional service states unresolved
**Location:** scripts/relay_phase_a_host.py:253; scripts/relay_phase_a_host.py:260; scripts/relay-phase-a/remote/dw.py:371
**Severity:** important

### Compare the handoff window in one clock domain
**Location:** scripts/relay_phase_a_collectors.py:395; scripts/relay_phase_a_host.py:165
**Severity:** important

### Preserve receipts during cancellation (round-1 pushback)
**Location:** scripts/relay_phase_a_collectors.py:87; scripts/relay_phase_a_host.py:344; scripts/relay_operator_session.py:660; scripts/relay_operator_session.py:668
**Severity:** blocker

### Finish C4 validation before recording success
**Location:** scripts/relay_phase_a_host.py:319; scripts/relay_phase_a_host.py:324
**Severity:** important

### Preserve unresolved daemon operations and final state in the result
**Location:** scripts/relay_phase_a_host.py:341; scripts/relay-phase-a/remote/dw.py:382; scripts/relay_operator_session.py:434
**Severity:** important

### Reserve revocation time across the whole cleanup
**Location:** scripts/relay_operator_session.py:658; scripts/relay_operator_session.py:672; scripts/relay_operator_session.py:714
**Severity:** important

### Give the gate busy flag an owner
**Location:** scripts/relay-observer-gate.py:228; scripts/relay-observer-gate.py:247
**Severity:** important

### Make gate stalls and thread faults visible to the host
**Location:** scripts/relay-observer-gate.py:112; scripts/relay-observer-gate.py:298; scripts/relay_phase_a_observer.py:543
**Severity:** important

### Anchor clock calculations to a common monotonic timeline
**Location:** scripts/relay_phase_a_collectors.py:45; scripts/relay_phase_a_collectors.py:262; scripts/relay_phase_a_observer.py:351; scripts/relay_phase_a_observer.py:475
**Severity:** important

### Keep permission option IDs unambiguous
**Location:** scripts/relay_phase_a_collectors.py:550
**Severity:** important

### Preserve native shell-call multiplicity
**Location:** scripts/relay_phase_a_collectors.py:645
**Severity:** important

### Complete JavaScript canonicalization
**Location:** scripts/relay_phase_a_observer.py:122; scripts/relay_phase_a_observer.py:126
**Severity:** important

### Preserve evidence-failure precedence on every path
**Location:** scripts/relay_operator_session.py:584; scripts/relay_phase_a_observer.py:424
**Severity:** important

### Preserve Lease-loss and interruption classification
**Location:** scripts/relay_phase_a_host.py:210; scripts/relay_phase_a_host.py:218; scripts/relay_operator_session.py:579
**Severity:** important

### Add the remaining lifecycle tests (round-1 pushback)
**Location:** scripts/tests/test_relay_phase_a_observer.py:712; scripts/tests/test_relay_phase_a_observer.py:774; scripts/tests/test_relay_phase_a_observer.py:804
**Severity:** important

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