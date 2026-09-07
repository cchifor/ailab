# Implementation review — estate-anomalies — round 1

<!-- codex-impl-review-status: finalized -->

## Summary

- Focused changes within W1–W4; no W5/W6 scope creep or new credential exposure found. Passing CI is accepted as given.
- W1 corrects the target addresses. The hostname guard intentionally broadens the planned kernel check, but does not establish the expected runner inventory.
- W3 safely retains `llm-node1` during migration. The recomputed checksum matches `4c10fb34e594` and changes the pod template, triggering a rollout despite the previously stale checksum; successful completion still needs verification. Removing `.44` preserves `.45` scraping through the unchanged ServiceMonitor-selected Service label.
- Keeping Velero’s node-agent is defensible for future non-testpool volumes, while retaining its host-volume access. The namespace-wide backup exclusion needs an explicit recovery decision.
- Renovate’s TTL is correctly placed and equals 48 hours, but history limits can delete Jobs sooner. W3 deletion and W4’s old-Job cleanup, forge migration, reviewbot processing, entitlement resolution, and live acceptance checks remain outstanding or unverified.

## Findings

### Record the recovery contract before excluding all of testpool
**Location:** kubernetes/apps/infrastructure/storage/velero/helmrelease.yaml:180
**Severity:** important

### Pod creation time is insufficient to authorize Service deletion
**Location:** kubernetes/apps/apps/ai/llm-service.yaml:40
**Severity:** important

### The identity guard accepts incorrect runner inventories
**Location:** kubernetes/apps/infrastructure/monitoring/ci-runners-rules.yaml:298
**Severity:** important

### The TTL does not guarantee two days of failure evidence
**Location:** kubernetes/apps/apps/renovate/cronjob.yaml:20
**Severity:** nit

### Retirement comments remain contradictory
**Location:** kubernetes/apps/apps/ai/litellm-local.yaml:17
**Severity:** nit

## Diff stat

```text
 kubernetes/apps/apps/ai/litellm-local.yaml         | 20 +++++++-------
 kubernetes/apps/apps/ai/llm-service.yaml           | 27 +++++++++++++------
 kubernetes/apps/apps/renovate/cronjob.yaml         |  9 +++++++
 .../apps/infrastructure/monitoring/alloy.yaml      |  7 +++++
 .../infrastructure/monitoring/ci-runners-node.yaml | 18 +++++++++----
 .../monitoring/ci-runners-rules.yaml               | 31 ++++++++++++++++++++++
 .../monitoring/storage-fabric-probe.yaml           |  7 +++++
 .../infrastructure/storage/velero/helmrelease.yaml | 31 ++++++++++++++++++++--
 8 files changed, 124 insertions(+), 26 deletions(-)
```