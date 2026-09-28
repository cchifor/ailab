# Implementation review — gitea-actions-retention-and-cnpg-reclone — round 1

<!-- codex-impl-review-status: complete -->

## Findings

All thirteen findings were accepted. Each resolution names the commit that carries it; the two blockers
were confirmed by reproduction before the fix (the PromQL one under `sh -c`, the boolean one on the
disposable drill cluster, where it had already been caught and fixed in `b0039551` while the review ran).

### Default PromQL expression is corrupted by shell expansion

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:30`
**Severity:** blocker

Resolution: the default is now assigned as a single-quoted literal (`[ -n "${GATE_QUERY:-}" ] || GATE_QUERY='count(ALERTS{…})'`) instead of inside `${VAR:=…}`; the mock records the decoded `query` parameter and asserts it equals the expected PromQL on every gate call.

### Probe boolean serialization prevents every reclone execution

**Location:** `kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml:140`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:44`; `scripts/tests/cnpg-lost-slot-reclone-mock.py:128`
**Severity:** blocker

Resolution: fixed in `b0039551` (found on the drill cluster before this review landed): both `true/false` and bare `t/f` are accepted; the mock fixtures now use the real probe's `true/false` shape and one scenario keeps `t/f`; the drill exercised the actual probe SQL end to end.

### POSIX pipelines hide jq failures

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:69`, `:98`, `:109`, `:124`
**Severity:** important

Resolution: `jqr()` runs jq to a file, propagates jq's own status, and strips CRs afterwards; every jq call goes through it and a failure is `fail` (exit 1) for discovery, listing, revalidation and `total_count` validation. Mock scenarios `malformed-orgs`, `malformed-list`, `malformed-reval` assert exit 1 and no deletions.

### Unexpected Prometheus responses can open the gate

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:55`
**Severity:** important

Resolution: the gate accepts only `status == "success"` with `resultType == "vector"`; an empty vector is the no-alert case, one sample's value is the count, anything else (including `{}`, an error envelope, non-JSON, a scalar) pauses with exit 2. Four mock scenarios cover those bodies.

### Gate checks miss the final deletion batch

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:86`, `:120`
**Severity:** important

Resolution: the gate runs immediately before the first DELETE (after scanning) and after every `GATE_EVERY` completed deletions, the final batch included; the mock has `MAX_DELETES_PER_RUN == GATE_EVERY == 3` with alerts starting to fire at the third deletion (3 deleted, exit 2) plus the mid-run variant, and asserts the exact number of gate calls in the basic scenario.

### Annotation guards fail open and are not protected against concurrent Jobs

**Location:** `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:39`, `:58`, `:121`, `:125`
**Severity:** important

Resolution: the Cluster is read once as JSON (`read_cluster`: any API failure or non-JSON is a failure, never "absent annotations"); the pre-mutation re-read compares primary, phase, pod role AND both annotations; the marker is taken with `kubectl annotate --resource-version=<rv>` so a competing execution's write is a Conflict and the job aborts before any delete. Mock scenarios: cluster read failure, marker appearing before mutation, resourceVersion conflict.

### Marker verification can succeed without a healthy replacement slot

**Location:** `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:63`, `:74`, `:90`
**Severity:** important

Resolution: evaluation is now instance-driven: every replica in `.status.instanceNames` must have its expected slot present, physical, `active` and `reserved` for the marker to clear (and instances must equal `spec.instances` with the old instance gone). Mock scenarios: replacement slot missing, unreserved, inactive — marker stays.

### The deletion wait does not enforce its 150-second bound

**Location:** `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:33`, `:132`
**Severity:** important

Resolution: every kubectl call carries `--request-timeout=20s`; `present()` distinguishes present / gone (NotFound, or a same-named object with a different UID) / unknown (API error), and unknown never counts as gone; at the deadline the job exits 1 with the marker left. Mock scenario `wait-api-error`.

### The disposable drill needs separate connection and authorization wiring

**Location:** `docs/runbooks/infra-pg.md:64`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml`; `scripts/tests/fixtures/cnpg-reclone-drill.yaml:55`
**Severity:** important

Resolution: `scripts/tests/fixtures/build-reclone-drill-job.py` builds a throwaway `reclone-drill-*` SA / Role (scoped to the drill Cluster) / RoleBinding / ConfigMap / Job from the real CronJob template with the drill cluster's credentials and CA; the isolating CiliumNetworkPolicy moved to `cnpg-reclone-drill-isolate.yaml`, applied only after baseline replication; the fixture header carries the exact order; the runbook points at it. This is what the 2026-09-28 drill actually ran.

### Promised edge-case coverage is incomplete

**Location:** `scripts/tests/gitea-actions-run-retention-mock.py`; `scripts/tests/cnpg-lost-slot-reclone-mock.py`
**Severity:** important

Resolution: retention — the cutoff is pinned through `RETENTION_NOW`, with runs at exactly the cutoff (kept), one second older (deleted) and one second newer (kept); explicit 401 and 403; malformed bodies. Reclone — no PVC at all (refused), phase change / role change / marker appearing / resourceVersion conflict at the final re-read (aborted), verification failures (missing, unreserved, inactive replacement slot), API error during the wait. 33 + 35 scenarios pass.

### Token setup example is invalid YAML and contains broken commands

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-retention.sops.yaml.example`
**Severity:** important

Resolution: the example is valid YAML (`stringData.token: REPLACE_ME`, parsed in the check) and every command is one line with no trailing shell comment.

### The UTC exclusion window is not encoded in the CronJob

**Location:** `kubernetes/apps/apps/gitea/actions-run-retention.yaml:57`
**Severity:** important

Resolution: `timeZone: Etc/UTC` on both CronJobs.

### Remove the unused gate-disable switch

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:31`, `:51`
**Severity:** nit

Resolution: removed; the mock always serves the gate.

## Diff stat

 docs/runbooks/infra-pg.md                          |  92 +++++++
 .../apps/apps/gitea/actions-run-retention.yaml     | 120 ++++++++
 .../apps/gitea/gitea-actions-retention.sops.yaml   |  31 +++
 .../gitea-actions-retention.sops.yaml.example      |  28 ++
 .../apps/apps/gitea/gitea-actions-run-retention.sh | 146 ++++++++++
 kubernetes/apps/apps/gitea/kustomization.yaml      |  11 +
 .../apps/databases/cnpg-lost-slot-reclone.sh       | 151 ++++++++++
 .../apps/databases/cnpg-lost-slot-reclone.yaml     | 187 +++++++++++++
 .../apps/databases/infra-pg-slotwatch.sops.yaml    |  35 +++
 kubernetes/apps/databases/infra-pg.yaml            |  19 +-
 kubernetes/apps/databases/kustomization.yaml       |  10 +
 scripts/tests/cnpg-lost-slot-reclone-mock.py       | 270 ++++++++++++++++++
 scripts/tests/fixtures/cnpg-reclone-drill.yaml     |  74 +++++
 scripts/tests/gitea-actions-run-retention-mock.py  | 306 +++++++++++++++++++++
 14 files changed, 1478 insertions(+), 2 deletions(-)
