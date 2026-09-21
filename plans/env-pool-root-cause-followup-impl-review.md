# Implementation review — env-pool-root-cause-followup — round 1

<!-- codex-impl-review-status: complete -->

## Findings

### Missing telemetry can still produce a clean verdict

**Location:** scripts/env-pool-soak.py:461  
**Severity:** important  
**Resolution:** Resolved in e2fc579d: every required series (node_ready, kubelet_up, boot_time, member_age, warm_ready, warm_spec, restarts) must be present, cover both window boundaries and have no interior gap > 3 steps, or the run is INCOMPLETE; tests: empty required series, 10-minute hole under 80 % coverage.

### Capacity loss is ignored without an event inside the window

**Location:** scripts/env-pool-soak.py:455; scripts/env-pool-soak.py:252  
**Severity:** important  
**Resolution:** Resolved in e2fc579d + 3329e93e: incidents are derived from the SandboxWarmPool's own readyReplicas < replicas (new kube-state-metrics customResourceState series), so an outage underway at the window start or a loss shorter than the alert delay is seen without any signal; the checkpoint no longer advances while an incident is open. Tests added for both.

### Separate incidents are joined into a false slow recovery

**Location:** scripts/env-pool-soak.py:472  
**Severity:** important  
**Resolution:** Resolved in e2fc579d: signals attach to an incident only within ±5 min of its start (or inside it); otherwise they are reported as transient. Test: failure at minute 10 + unrelated dip at minutes 40–44 → contained, nothing unresolved.

### Leased sandboxes can mask missing warm capacity

**Location:** scripts/env-pool-soak.py:71; scripts/env-pool-soak.py:361  
**Severity:** important  
**Resolution:** Resolved in 3329e93e + e2fc579d: warm capacity is the pool's own accounting (agentsandbox_warmpool_ready_replicas), never pod readiness; a Sandbox pod gone without a capacity incident is reported as lease turnover. Tests: Ready leased pod + empty pool → UNRESOLVED; lease turnover → OK.

### Failed evidence collection counts as successful evidence

**Location:** scripts/env-pool-soak.py:400; scripts/env-pool-soak.py:427  
**Severity:** important  
**Resolution:** Resolved in e2fc579d: evidence is matched per (sandbox, pid) against each stage-1 kill; only lines carrying state= (a completed dump) count and 'incomplete' lines never do. Tests: timeout-only, mixed success across two sandboxes.

### Relay replay is mistaken for fresh source evidence

**Location:** scripts/env-pool-soak.py:129; scripts/env-pool-soak.py:441; docs/runbooks/env-pool.md:124  
**Severity:** important  
**Resolution:** Resolved in e2fc579d: relay records are deduplicated on the containerd record's own time= field + content and freshness/gaps use that source time; lines without a source time are flagged. Runbook corrected (whole-ring replay, dedupe on time+content). Test: yesterday's record replayed every 5 min → gap → INCOMPLETE.

### The final identity check rescans unrelated host processes

**Location:** kubernetes/apps/infrastructure/testpool/env-reaper.yaml:200  
**Severity:** important  
**Resolution:** Resolved in 3329e93e: still_ours reads only the given pid's cgroup/exe/cmdline; scripts/tests/test-env-reaper.sh runs the helpers under the DaemonSet's busybox image against a synthetic /proc (changed/vanished identity, evidence timeout via a FIFO, unreadable entries, thread truncation, skip-kill) and is a CI step.

### Fault injection bypasses the watchdog and controller path

**Location:** docs/runbooks/env-pool.md:212  
**Severity:** important  
**Resolution:** Resolved in e2fc579d (runbook): the V2 sequence freezes a member older than the 15-minute grace and lets watchdog → NotReady → GC → reaper run with timestamps recorded; the manual Sandbox delete is documented as a separate teardown-only test.

### The runbook describes G2 as already applied

**Location:** docs/runbooks/env-pool.md:88; docs/runbooks/env-pool.md:139  
**Severity:** nit  
**Resolution:** Resolved in e2fc579d (runbook): the section is marked active only after gate G2, names the two settings that flip there (kata_debug, apply_mode) and the acceptance checks that make the description true.

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