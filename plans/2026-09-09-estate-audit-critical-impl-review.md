# Implementation review — estate-audit-critical — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Most monitoring and availability changes follow the finalized plan. The supplied promtool results are accepted, and the existing checksum checker passes for the updated LiteLLM configuration.
- Change B is incomplete: the direct off-site dump copy is missing, and unchecked publication failures can still trigger deletion of older backups.
- The backup script needs failure-path tests covering interrupted writes, exhausted space, unusual database names, and retention containing incomplete generations.
- Initial holder deployment can proceed, but holder replacement during an active dump can violate co-location and block Flux readiness. Initial PVC adoption is compatible with the existing Bound claim; subsequent pruning introduces an orphaning risk.
- CNPG's role fields and Secret reference are supported, including adoption of existing roles. Alert fixtures demonstrate several firing/resolving paths, but omit important boundary and missing-data cases. Live backup, restore, and re-clone acceptance remains unproven by the supplied evidence.

## Findings

### 1. The planned direct off-site dump copy is missing

**Location:** `kubernetes/apps/backup/backup-offsite/rclone-cronjob.yaml:96`
**Severity:** blocker

<!-- codex: B4 requires copying completed dump generations directly to `gdrive-crypt`, bypassing versitygw. The implementation leaves this file unchanged: it still copies only the Velero and Talos buckets and has no access to `postgres-dumps`. Adding the holder only enables the existing Velero path, which still depends on the USB-backed gateway. Implement the direct copy with access compatible with the PVC's namespace and RWO placement. -->

### 2. Publication failures do not prevent pruning or Job success

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:215`
**Severity:** blocker

<!-- codex: With `set -uo pipefail`, failed commands do not terminate execution automatically. Neither checksum generation nor writing `COMPLETE` is checked; `sync` is also unchecked. A checksum read/write error or failed marker write can therefore be followed by a successful rename, pruning of older complete generations, and `DUMP_OK`. Explicitly check every publication prerequisite with `|| exit 1` after each critical step. -->

### 3. Abandoned staging can permanently prevent subsequent backups

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:163`
**Severity:** important

<!-- codex: Abandoned staging directories are swept only after successful publication. Once interrupted runs leave less than 4 GiB available, every subsequent run exits at the space check before reaching that sweep. Clean safely identified abandoned stages before checking available space, with exclusion of active writers. Add cleanup for termination signals. -->

### 4. Retention counts incomplete paths toward the seven-generation allowance

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:226`
**Severity:** important

<!-- codex: The pipeline selects everything matching `/dumps/[0-9]*`, then filters by COMPLETE marker. Incomplete directories and unrelated numeric names consume retention slots. Filter to recognized complete generation directories before sorting and selecting deletion candidates. -->

### 5. Database names are split, glob-expanded, and used as filesystem paths

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:185`
**Severity:** important

<!-- codex: `for db in $DBS` performs whitespace splitting and pathname expansion. A database named `sales archive` becomes two dump targets. Using database names directly beneath `$STAGE` permits names containing path separators to escape staging. Check enumeration exit status and read names using an unambiguous format like newline separation. Use safe filenames such as OIDs. -->

### 6. Dumps discard the permissions needed for faithful recovery

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:194`
**Severity:** important

<!-- codex: `--no-acl` removes database-object grants and revocations. This matters for the estate's separate application, broker, and reaper roles. Preserving ACLs is essential for restoring the security model. Also remove `--no-owner` as it is ignored for custom-format archives. -->

### 7. Recreate does not coordinate holder replacement with an active writer

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:48`
**Severity:** important

<!-- codex: If a dump is running on node A when the holder is replaced, the new holder may schedule on node B while the writer retains RWO attachment on A, violating co-location. The holder can remain Pending beyond the Kustomization health timeout, blocking downstream reconciliation. Coordinate placement across both consumers and test replacement during an active dump. -->

### 8. Adopting the PVC also exposes it to Flux pruning

**Location:** `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml:29`
**Severity:** important

<!-- codex: With `databases` having `prune: true`, reverting the new PVC resource or removing its manifest causes Flux to delete the previously unmanaged claim. The qnap-iscsi Retain policy leaves an orphaned PV. Add `kustomize.toolkit.fluxcd.io/prune: disabled` annotation and document deliberate removal/rebinding. -->

### 9. The dumper Secret lacks the credential reload label

**Location:** `kubernetes/apps/databases/infra-pg-dumper.sops.yaml:14`
**Severity:** important

<!-- codex: The role declaration is valid for CNPG 1.24.1, but the Secret omits `cnpg.io/reload: "true"`. A new Job can consume a rotated password before CNPG applies it. Add the reload label and verify password rotation with a fresh connection. -->

### 10. The new Gatus endpoint will alert during the nightly shutdown

**Location:** `kubernetes/apps/apps/gatus/configmap.yaml:117`
**Severity:** important

<!-- codex: Omitting native `alerts:` does not suppress the Prometheus alert selected by `gatus_results_endpoint_success == 0`. The endpoint will alert after five minutes of expected nightly downtime. Add endpoint-specific suppression for the shutdown window with explicit timezone. -->

### 11. The Gatus check does not exercise completion

**Location:** `kubernetes/apps/apps/gatus/configmap.yaml:123`
**Severity:** important

<!-- codex: A successful `GET /v1/models` only proves model discovery responds. The endpoint can return 200 while completion requests fail. A6 explicitly calls for an HTTP completion check. Send a small, bounded completion request to the intended model and validate its response. -->

### 12. The memory join assumes unique raw input series

**Location:** `kubernetes/apps/infrastructure/monitoring/platform-rules.yaml:45`
**Severity:** important

<!-- codex: The division requires exactly one series per `(namespace,pod,container)` on each side. Duplicate scrape targets or overlapping cAdvisor identities during replacement can produce multiple matches and fail evaluation. Select authoritative inputs and normalize before dividing. -->

### 13. A zero memory limit produces a persistent false alert

**Location:** `kubernetes/apps/infrastructure/monitoring/platform-rules.yaml:47`
**Severity:** important

<!-- codex: The rule excludes absent limit series but not limits reported as zero. Positive working-set divided by zero produces `+Inf`, which passes `> 0.9` indefinitely. Filter the denominator with `> 0` before division. Add separate absent-limit and zero-limit fixtures. -->

### 14. Disappearing telemetry still leaves replication coverage silently green

**Location:** `kubernetes/apps/infrastructure/monitoring/ha-rules.yaml:112`
**Severity:** important

<!-- codex: `up == 0` detects failed scrapes, but if the PodMonitor stops selecting the pods, its `up` series disappear instead. With both database pods still Ready, all three replication expressions can return nothing. Add complementary coverage checks against expected database pods and required replication series. -->

### 15. The fixtures omit required recovery and boundary cases

**Location:** `kubernetes/apps/infrastructure/monitoring/ha-rules.test.yaml:120`
**Severity:** important

<!-- codex: The lag fixture never reduces the failing series below threshold; the exporter fixture never has both targets down. Several `for:` checks sample before and after boundary rather than at it. Add cases including lag recovery, promotion of non-streaming standby to primary, exact threshold equality, and exact pending-to-firing transitions. -->

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
