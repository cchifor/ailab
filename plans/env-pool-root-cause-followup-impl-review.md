# Implementation review — env-pool-root-cause-followup — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- PR-A broadly follows T1/T2/T3/T5 without obvious scope creep. G1 is recorded; deferred T4a/T4b and unexecuted G2 are not treated as missing implementation.
- The Kata base file matches the planned SHA-256. Template gating and the relay’s namespace, ServiceAccount, placement, and fsGroup protections match the intended design.
- All 26 soak tests pass, but additional synthetic cases reproduce false `OK`, false `RECURRENCE-CONTAINED`, and false `UNRESOLVED` verdicts. Recovery and evidence completeness need further work before these reports support soak acceptance.
- The reaper needs a tighter final identity check and automated BusyBox ash coverage. WSL access was denied here, and existing watchdog runtime tests were blocked by the read-only temporary-directory restrictions.
- G2’s live capture, recovery, and synthetic-secret privacy checks remain necessary. The runbook prematurely describes their intended configuration as already active.

## Findings

### Missing telemetry can still produce a clean verdict

**Location:** scripts/env-pool-soak.py:461  
**Severity:** important  
<!-- codex: An empty member_ready series or empty restarts result still produces OK in an otherwise quiet fixture, while a ten-minute interior readiness gap in a one-hour window passes the 80% coverage threshold and produces RECURRENCE-CONTAINED after a closure. Require the necessary readiness/restart series independently of event detection, check contiguous gaps as well as overall coverage, and add regression cases so absence of observations cannot prove recovery or absence of restarts. -->

### Capacity loss is ignored without an event inside the window

**Location:** scripts/env-pool-soak.py:455; scripts/env-pool-soak.py:252  
**Severity:** important  
<!-- codex: Recovery validation only iterates event_times, so an all-zero capacity timeline without an in-window closure, reap, disappearance, restart, or reboot returns OK when no alert has fired; additionally, UNRESOLVED reports advance the checkpoint without preserving the open incident, allowing its trigger to fall outside subsequent overlap windows. Derive outage intervals from readiness itself and preserve unresolved incident state across checkpoints, with tests covering an outage already underway at the window start and a capacity loss shorter than the alert's firing delay. -->

### Separate incidents are joined into a false slow recovery

**Location:** scripts/env-pool-soak.py:472  
**Severity:** important  
<!-- codex: The search for drop is unbounded across the remaining window, so a transient check failure that never loses capacity is attached to a later unrelated outage: a failure at minute 10 and a separate dip at minutes 40–44 reproduce UNRESOLVED with a fictitious 34-minute recovery. Group related events into bounded incident episodes, use recovery observations to close each episode, and test multiple faults plus transient failures that never close the ready-port. -->

### Leased sandboxes can mask missing warm capacity

**Location:** scripts/env-pool-soak.py:71; scripts/env-pool-soak.py:361  
**Severity:** important  
<!-- codex: The member/readiness queries include every Sandbox-owned pod, and the planned warm-versus-leased identification is absent, so a healthy active lease can keep the aggregate at 1 while the warm replacement never becomes Ready; ordinary lease expiry is also reported as a member recurrence. Implement the planned membership/claim correlation and distinguish lease turnover from warm-member failures, adding a fixture with a Ready leased sandbox alongside an unavailable warm pool. -->

### Failed evidence collection counts as successful evidence

**Location:** scripts/env-pool-soak.py:400; scripts/env-pool-soak.py:427  
**Severity:** important  
<!-- codex: The evidence matcher accepts the reaper's “incomplete (timeout … or read error)” line, and the completeness check only requires some evidence anywhere in the window, so a timeout-only dump reproduces RECURRENCE-CONTAINED and one successful dump can conceal another sandbox's missing evidence. Associate successful evidence with each stage-1 sandbox/PID, recognize incomplete or unavailable dumps explicitly, and add timeout-only and mixed-success regression cases. -->

### Relay replay is mistaken for fresh source evidence

**Location:** scripts/env-pool-soak.py:129; scripts/env-pool-soak.py:441; docs/runbooks/env-pool.md:124  
**Severity:** important  
<!-- codex: Deduplication and relay-health checks use Loki's outer ingestion timestamp rather than the containerd record's original time, so reconnect replay is counted repeatedly and periodic replay of the same previous-day record reproduces OK despite providing no current source evidence. Parse original timestamps for source freshness and incident correlation, deduplicate by source/original timestamp/content while preserving raw records, and correct the runbook's obsolete “last 200 lines” and timestamp-only deduplication instructions. -->

### The final identity check rescans unrelated host processes

**Location:** kubernetes/apps/infrastructure/testpool/env-reaper.yaml:200  
**Severity:** important  
<!-- codex: still_ours calls the full procs_of scan and pipes it into grep -q; after grep finds the target, the shell can still wait for the producer to traverse unrelated processes before returning, adding unbounded work outside the evidence timeout and widening the checked-identity-to-kill interval. Validate only the requested PID immediately before signalling, and add BusyBox ash tests for changed/disappearing identity, evidence timeout, unreadable proc entries, and thread truncation—the current tests do not execute these new shell helpers. -->

### Fault injection bypasses the watchdog and controller path

**Location:** docs/runbooks/env-pool.md:212  
**Severity:** important  
<!-- codex: Immediately deleting the Sandbox after stopping virtiofsd exercises hung teardown but bypasses V2's required watchdog → NotReady → controller GC sequence, allowing that prevention path to remain broken while the documented test succeeds. For the full V2 test, first let the idle member exceed the controller's 15-minute creation-age grace, then freeze it and observe automatic deletion with recorded timestamps; retain manual deletion only as an explicitly separate teardown test. -->

### The runbook describes G2 as already applied

**Location:** docs/runbooks/env-pool.md:88; docs/runbooks/env-pool.md:139  
**Severity:** nit  
<!-- codex: The runbook says debug is enabled and applies are staged, whereas this tree deliberately retains kata_debug=false and env-node-1.apply_mode=no_reboot pending G2; its procedure does not explicitly describe changing both settings before the first debug apply. Mark the section as pending G2, document that transition and its privacy/capture acceptance checks, and describe the configuration as active only after the applied commit and validation are recorded. -->

## Diff stat

```text
 .gitea/workflows/manifests.yaml                    |   2 +-
 CLAUDE.md                                          |   6 +-
 docs/network-plan.md                               |  15 +-
 docs/runbooks/env-pool.md                          | 160 +++++-
 justfile                                           |  11 +
 .../agent-sandbox/kustomization.yaml               |   6 +-
 .../monitoring/cri-log-relay-talosconfig.sops.yaml |  37 ++
 .../infrastructure/monitoring/cri-log-relay.yaml   | 143 ++++++
 .../infrastructure/monitoring/kustomization.yaml   |   2 +
 .../apps/infrastructure/testpool/env-reaper.yaml   |  74 ++-
 kubernetes/infra/env-pool/SPIKE-REPORT.md          |   5 +
 kubernetes/infra/env-pool/backend.tf               |  12 +-
 kubernetes/infra/env-pool/imports.tf               |  10 +
 .../machine-config/cri-20-customization.part       |  15 +
 .../machine-config/kata/config.d/10-debug.toml     |  25 +
 .../machine-config/kata/configuration.toml         | 469 ++++++++++++++++++
 .../env-pool/machine-config/worker.yaml.tftpl      |  23 +
 kubernetes/infra/env-pool/main.tf                  |  11 +-
 kubernetes/infra/env-pool/node-labels.tf           |   8 +-
 kubernetes/infra/env-pool/talos.tf                 |  13 +-
 kubernetes/infra/env-pool/terraform.tfvars.example |  16 +
 kubernetes/infra/env-pool/variables.tf             |  40 +-
 ...2026-09-20-env-pool-root-cause-followup-plan.md |   2 +-
 scripts/env-pool-soak.py                           | 542 +++++++++++++++++++++
 scripts/tests/test_env_pool_soak.py                | 374 ++++++++++++++
 25 files changed, 1972 insertions(+), 49 deletions(-)
```