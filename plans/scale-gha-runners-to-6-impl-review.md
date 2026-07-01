# Implementation review — scale-gha-runners-to-6 — round 1

<!-- codex-impl-review-status: finalized -->

## Outcome
Codex reviewed the implementation diff (`5b24d5c..c547d2c`) against the finalized plan and found
**no defects** — all 8 findings were `nit`-severity confirmations. Converged in round 1; no code changes
were required.

Verified by the review:
- All 8 planned edits present, syntactically valid (HCL/YAML/Markdown), and no scope creep (40 insertions,
  13 deletions across exactly the 8 planned files; no Ansible role or SOPS-secret change).
- No IP / `vm_id` collisions: `.33/.34/.35` + `4104/4105/4106` are unused and inside the static `.2–.50`
  block, clear of Talos (`.40–.43`/`4001–4003`), dev-workers (`.37–.39`/`4201–4203`), AI LXC
  (`.44–.46`/`5001–5003`), and registry (`.36`/`5004`).
- Placement is 2-per-host via the `runner_nodes` map keys; `for_each` applies identical resource blocks,
  and the sizing vars (`runner_cores=8`, `runner_memory_mib=24576`, `runner_memory_floating_mib=12288`,
  `runner_rootfs_gb=120`) are unchanged — so "same allocation and configuration for all 6" holds
  structurally.
- Docs internally consistent (network-plan table + free-range note, runbook counts, ADR 0013 update,
  monitoring endpoints, ai-lxc comment).

Resolution of the single actionable nit:
- **Runbook `docs/runbooks/ci-runners.md:47` phrasing** ("creates the new runner VMs") — **no change**.
  Codex rated it "acceptable / not incorrect" and suggested the more specific "creates gha-runner-4/5/6".
  Declined: that `tofu apply` step is a reusable runbook instruction (fresh install *and* scale-up), so
  the count-agnostic wording is deliberately preferable to hardcoding this scale-up's VM names.

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
