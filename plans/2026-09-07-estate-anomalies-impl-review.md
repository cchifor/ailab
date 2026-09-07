# Implementation review — estate-anomalies — round 1

<!-- codex-impl-review-status: pending -->

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
<!-- codex: The reviewed material establishes lease churn and a missing-PV race, but does not establish W4's required recovery contract for every resource in testpool; Flux will apply the namespace-wide exclusion to both schedules on merge, regardless of whether the node-agent remains. Document that all namespace contents are disposable or covered by another recovery mechanism, otherwise narrow the exclusion to the disposable resources. -->

### Pod creation time is insufficient to authorize Service deletion
**Location:** kubernetes/apps/apps/ai/llm-service.yaml:40
**Severity:** important
<!-- codex: A pod created after merge can remain blocked in wait-for-db while the old ReplicaSet still serves, so this cleanup instruction weakens the plan's verification gate even though retaining the Service in this commit is safe. Require completion of the Deployment rollout with the expected checksum, verification that the running proxy loaded no llm-node1 deployment, and successful authenticated inference before merging the deletion commit; merely creating two commits does not guarantee Flux reconciles them separately. -->

### The identity guard accepts incorrect runner inventories
**Location:** kubernetes/apps/infrastructure/monitoring/ci-runners-rules.yaml:298
**Severity:** important
<!-- codex: The prefix matcher accepts a foreign or duplicate host reporting ci-runner-*, produces no result when node_uname_info is absent despite up=1, and can misclassify a genuine runner whose hostname or nodename label changes; the successful live query establishes coverage of this incident, not the comment's universal identity claim. Add the planned expected IP/nodename inventory assertion and a per-target missing-uname check, and document the naming assumption and 15-minute firing delay ([Prometheus selector semantics](https://prometheus.io/docs/prometheus/latest/querying/basics/)). -->

### The TTL does not guarantee two days of failure evidence
**Location:** kubernetes/apps/apps/renovate/cronjob.yaml:20
**Severity:** nit
<!-- codex: Although 172800 correctly means 48 hours, failedJobsHistoryLimit: 3 independently removes the oldest failure when the fourth accumulates—approximately 12 hours into continuous four-hourly failures—so the promised investigation window is not guaranteed ([Kubernetes history limits](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/#jobs-history-limits)). Raise the history limit to cover the intended window or describe the TTL as cleanup eligibility rather than minimum retention; persistent failures retain recent failed Jobs, and the existing failed Job still needs separate cleanup after its logs are captured. -->

### Retirement comments remain contradictory
**Location:** kubernetes/apps/apps/ai/litellm-local.yaml:17
**Severity:** nit
<!-- codex: The header and model-list preamble still describe node1/node2 and two deployments, despite the new single-node route, while llm-service.yaml:106 still calls .44:8082 free despite the added comments describing llama-swap there. Update these statements and clarify that model inventory intentionally differs from the main proxy, while identifying which settings must remain synchronized. -->

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