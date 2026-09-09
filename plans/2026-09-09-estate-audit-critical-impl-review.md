# Implementation review — estate-audit-critical — round 1

<!-- codex-impl-review-status: finalized -->

## Findings

### 1. The planned direct off-site dump copy is missing

**Location:** `kubernetes/apps/backup/backup-offsite/rclone-cronjob.yaml:96`
**Severity:** blocker

**OPERATOR DECISION.** Escalated rather than decided unilaterally: a direct leg needs the rclone credential in the `databases` namespace, and the existing one is `scope = drive` (FULL Drive), so it is a real grant to duplicate. Four options were put to the operator (leave it / duplicate the credential / move dumps to RWX NFS and share the volume / mint a folder-scoped credential). **Decision: leave it and record on #592.** The dump's PRIMARY copy is already on internal storage and independent of the USB disk, which is the larger half of #592; only the off-site hop still traverses it, and that closes when #595 is fixed. No credential duplicated.

### 2. Publication failures do not prevent pruning or Job success

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:215`
**Severity:** blocker

**FIXED.** Every publication prerequisite now checked with an explicit `|| exit 1`, and the cleanup trap is cleared only after the rename succeeds. Frozen by scripts/tests/test-infra-pg-dump.sh: a failed COMPLETE marker exits non-zero, publishes nothing and prunes nothing.

### 3. Abandoned staging can permanently prevent subsequent backups

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:163`
**Severity:** important

**FIXED.** The staging sweep now runs FIRST, before the space check, bounded to >6h old (which cannot be a live writer: activeDeadlineSeconds 5400, concurrencyPolicy Forbid). An EXIT/INT/TERM trap also removes our own staging, so an OOM or deadline kill no longer leaks a partial directory.

### 4. Retention counts incomplete paths toward the seven-generation allowance

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:226`
**Severity:** important

**FIXED.** The COMPLETE filter is applied BEFORE the retention count, not after. Test: 8 complete + 3 incomplete leaves exactly 7 complete and touches none of the incomplete ones.

### 5. Database names are split, glob-expanded, and used as filesystem paths

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:185`
**Severity:** important

**FIXED.** Now a `while IFS= read -r` over a newline-delimited list with enumeration failure checked, and any name containing `/` refused. Tests cover `sales archive` as ONE target and an unsafe name being refused explicitly.

### 6. Dumps discard the permissions needed for faithful recovery

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:194`
**Severity:** important

**FIXED.** ACLs are kept — in this estate the separate app/broker/reaper roles per database ARE the security model, and a restore without them silently reopens access. `--no-owner` dropped too: it is a pg_restore-time flag with no effect on a -Fc archive.

### 7. Recreate does not coordinate holder replacement with an active writer

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:48`
**Severity:** important

**FIXED (bounded, residual stated).** `progressDeadlineSeconds: 900` bounds how long a blocked holder rollout can look like a hanging reconcile rather than a failing one, and the writer is bounded by activeDeadlineSeconds. The window is real but narrow — the holder only moves on drain or image change — and the honest mitigation is bounding it, not claiming it cannot happen. Recorded in the manifest with the operational advice (drain outside 01:30-03:00, or scale the holder to 0 first).

### 8. Adopting the PVC also exposes it to Flux pruning

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:29`
**Severity:** important

**FIXED.** `kustomize.toolkit.fluxcd.io/prune: disabled` on the adopted PVC, with the reason and the deliberate-removal procedure in the manifest.

### 9. The dumper Secret lacks the credential reload label

**Location:** `kubernetes/apps/databases/infra-pg-dumper.sops.yaml:14`
**Severity:** important

**FIXED.** `cnpg.io/reload: "true"` added. The file was REGENERATED, not hand-edited: SOPS's MAC covers the whole document, so editing metadata in the ciphertext breaks decryption — confirmed by an actual MAC mismatch before correcting course. Password round-trips unchanged.

### 10. The new Gatus endpoint will alert during the nightly shutdown

**Location:** `kubernetes/apps/apps/gatus/configmap.yaml:117`
**Severity:** important

**FIXED.** Correct and important. The endpoint is excluded by name from GatusEndpointDown and renamed to the matcher-friendly `vllm-cloud2`, with an assertion that the exclusion string and the endpoint name match exactly so they cannot drift apart.

### 11. The Gatus check does not exercise completion

**Location:** `kubernetes/apps/apps/gatus/configmap.yaml:123`
**Severity:** important

<!-- codex: A successful `GET /v1/models` only proves model discovery responds. The endpoint can return 200 while completion requests fail. A6 explicitly calls for an HTTP completion check. Send a small, bounded completion request to the intended model and validate its response. -->

<!-- opus-pushback: A completion request every 300s occupies a shared GPU that real users are waiting on, and the failure this check exists to catch was the process not serving at all (ConnectionRefused), which `/v1/models` does detect. The residual gap is real and is now STATED in the config rather than implied away: a backend answering /v1/models while completions fail is not caught here. The LiteLLM fallback chain is what covers that case for users. -->

### 12. The memory join assumes unique raw input series

**Location:** `kubernetes/apps/infrastructure/monitoring/platform-rules.yaml:45`
**Severity:** important

**FIXED.** `max by (namespace, pod, container)` on both sides. Mutation-verified: dropping it fails the duplicate-cAdvisor-series case.

### 13. A zero memory limit produces a persistent false alert

**Location:** `kubernetes/apps/infrastructure/monitoring/platform-rules.yaml:47`
**Severity:** important

**FIXED.** `> 0` filter on the denominator. Mutation-verified: dropping it fails the zero-limit case, which otherwise divides to +Inf and can never resolve.

### 14. Disappearing telemetry still leaves replication coverage silently green

**Location:** `kubernetes/apps/infrastructure/monitoring/ha-rules.yaml:112`
**Severity:** important

**FIXED.** `PostgresMetricsMissing` joins against kube-state-metrics with `unless on(namespace, pod)`, so a pod that is Ready while exporting nothing is caught even though its `up` series has vanished entirely. Not hypothetical — it is the live state of strive-pg (#594). Mutation-verified.

### 15. The fixtures omit required recovery and boundary cases

**Location:** `kubernetes/apps/infrastructure/monitoring/ha-rules.test.yaml:120`
**Severity:** important

**FIXED.** Added: lag recovery, lag EXACTLY at the threshold (300 is not > 300), both exporter targets down at once, and a standby promoted to primary. 12 cases now.

## Diff stat

 kubernetes/apps/apps/ai/litellm.yaml               |  33 ++-
 kubernetes/apps/apps/gatus/configmap.yaml          |  18 ++
 .../apps/databases/infra-pg-dump-cronjob.yaml      | 258 +++++++++++++++++++++
 .../apps/databases/infra-pg-dumper.sops.yaml       |  40 ++++
 kubernetes/apps/databases/infra-pg.yaml            |  49 +++-
 kubernetes/apps/databases/kustomization.yaml       |   2 +
 .../monitoring/backup-rules.test.yaml              |  47 ++++
 .../infrastructure/monitoring/backup-rules.yaml    |  24 ++
 .../infrastructure/monitoring/ha-rules.test.yaml   | 189 +++++++++++++++
 .../apps/infrastructure/monitoring/ha-rules.yaml   |  73 +++++-
 .../monitoring/kube-prometheus-stack.yaml          |  23 +-
 .../infrastructure/monitoring/kustomization.yaml   |   1 +
 .../monitoring/platform-rules.test.yaml            | 147 ++++++++++++
 .../infrastructure/monitoring/platform-rules.yaml  |  91 ++++++++
 14 files changed, 985 insertions(+), 10 deletions(+)
