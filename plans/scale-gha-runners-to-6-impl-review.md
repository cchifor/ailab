# Implementation review — scale-gha-runners-to-6 — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- All 8 planned edits are present, complete, and syntactically correct (HCL, YAML, Markdown). No unplanned changes detected.
- VM IDs (4101–4106), IPs (.33–.35 + .47–.49), and placement (2 per host) match the plan exactly with no collisions against Talos/dev-workers/registry ranges.
- Resource allocation properly enforced: all 6 runners draw identical sizing from shared `runner_*` vars via `for_each`, guaranteeing "same config for all" structurally.
- Docs (network-plan, runbooks, ADR, monitoring manifest) updated consistently; free-space annotation corrected from `.5–.36+.50` to `.5–.32+.50`.
- ADR update comprehensive and properly scopes the RAM-check gate and fault-isolation trade-off.

## Findings

### Network plan free-space correction is accurate
**Location:** docs/network-plan.md lines 17, 23–25
**Severity:** nit (correctness only)
<!-- codex: The table now correctly reflects `.5–.32 + .50` as free static space, and properly adds the two missing rows (`.33–.35` runners, `.36` registry). Cross-checked against inventory and TF vars — all IPs correctly owned. Existing DHCP pool boundary at `.51+` remains enforced as designed. -->

### Runner placement enforced 2-per-host via map keys
**Location:** kubernetes/infra/runners/variables.tf lines 141–147
**Severity:** nit (structural soundness)
<!-- codex: The `runner_nodes` map has 6 entries with node_name assignments confirmed: gha-runner-1/4 → ai-node1, gha-runner-2/5 → ai-node2, gha-runner-3/6 → ai-node3. The `for_each = var.runner_nodes` loop in main.tf applies identical resource blocks to all 6, so placement is immutable and auditable. -->

### All sizing vars remain unchanged and shared
**Location:** kubernetes/infra/runners/variables.tf lines 87–116
**Severity:** nit (design verification)
<!-- codex: Verified: `runner_cores=8`, `runner_memory_mib=24576`, `runner_memory_floating_mib=12288`, `runner_rootfs_gb=120` are all unchanged and apply to all 6 runners via the `for_each` loop. No per-runner overrides or role-level tweaks in the diff. Ballooning floor (12 GiB) preserved as per #620 fix. -->

### ADR update comprehensive and properly frames the gate
**Location:** docs/decisions/0013-ci-self-hosted-runners.md lines 13–20
**Severity:** nit (documentation completeness)
<!-- codex: The update correctly identifies the 2-per-host placement, references the plan, and explicitly documents the RAM-check gate (MemAvailable ≳ 14 GiB minimum, balloon-reclaim/swap under peak as no-go). The blast-radius note on App key distribution to 3 more runners is correctly scoped as an accepted, pre-existing risk (ADR 0013 rationale). Cross-references to plan and related ADRs are present. -->

### AI LXC comment update aligns with network-plan rationale
**Location:** kubernetes/infra/ai-lxc/variables.tf lines 110–111
**Severity:** nit (consistency)
<!-- codex: Comment now correctly states `.44–.46` are in use and explains the `.51+` DHCP pool boundary (avoiding the #600-era collision). This matches the rationale in the updated network-plan and keeps the IP design justification consistent across modules. -->

### Monitoring manifest endpoints syntax consistent with Kubernetes conventions
**Location:** kubernetes/apps/infrastructure/monitoring/ci-runners-node.yaml lines 1–31
**Severity:** nit (audit)
<!-- codex: Added 3 endpoints (`.33–.35`) to the list in the same inline syntax as the existing 3 (`.47–.49`). Metadata, port config, and ServiceMonitor relabeling unchanged. No service restructuring. Flux-driven, so will be in-cluster only after merge + reconcile. -->

### Runbook "new runner VMs" phrasing is slightly vague but contextually clear
**Location:** docs/runbooks/ci-runners.md line 47
**Severity:** nit (phrasing)
<!-- codex: Comment changed from "creates the 3 VMs" to "creates the new runner VMs". In context (step 3: Create the VMs), this is acceptable — "new" refers to the newly provisioned runners 4/5/6, not the entire set. Could be clearer as "creates gha-runner-4/5/6" but not incorrect. All other runbook updates are precise (header now "6 ephemeral", verify step now "ephem-gha-runner-{1..6}"). -->

### All 8 planned edits accounted for; no scope creep
**Location:** Commit c547d2c diff summary
**Severity:** nit (completeness check)
<!-- codex: Diff shows 8 files changed: runners/variables.tf, runners/main.tf, inventory/hosts.yml, ci-runners-node.yaml, network-plan.md, ci-runners.md, 0013-ci-self-hosted-runners.md, ai-lxc/variables.tf. Line counts (40 insertions, 13 deletions) match plan scope (additive runner entries, updated comments, doc refinements). No untracked Ansible role changes, no secret rotation, no apply/validate steps in the diff (gated operator-run, as stated). -->

## Diff stat
```
docs/decisions/0013-ci-self-hosted-runners.md               |  8 ++++++++
 docs/network-plan.md                                        |  6 ++++--
 docs/runbooks/ci-runners.md                                 |  8 ++++----
 inventory/hosts.yml                                         |  6 ++++++
 .../apps/infrastructure/monitoring/ci-runners-node.yaml     |  7 +++++--
 kubernetes/infra/ai-lxc/variables.tf                        |  3 ++-
 kubernetes/infra/runners/main.tf                            |  2 +-
 kubernetes/infra/runners/variables.tf                       | 13 ++++++++++---
 8 files changed, 40 insertions(+), 13 deletions(-)
```
