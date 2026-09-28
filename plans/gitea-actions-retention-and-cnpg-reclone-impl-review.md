# Implementation review — gitea-actions-retention-and-cnpg-reclone — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Two blockers prevent normal operation: the default PromQL expression is corrupted by shell expansion, and the probe serializes booleans differently from what the reclone script accepts.
- The dry-run defaults, restart policies, credential transport, and restricted container settings follow the plan. Moving scripts beside their kustomizations is justified; removing unused Cluster `list` permission is reasonable. The incorrect `--keep-pvc` comment is corrected at `kubernetes/apps/databases/infra-pg.yaml:70`.
- Important gaps remain in JSON failure handling, gate timing, annotation guards, recovery verification, bounded waits, and drill setup.
- Existing mocks cover rerun revalidation skips, marker verification, marker blocking, and stuck-marker escalation. Missing cases include real probe serialization, actual PromQL validation, and several negative guard scenarios.
- Read-only checks confirmed the shell-expansion and pipeline defects, invalid example YAML, and successful shell syntax checks. The reported mock-suite and server-dry-run passes were not rerun; filesystem restrictions prevent saving this review to `plans/gitea-actions-retention-and-cnpg-reclone-impl-review.md`.

## Findings

### Default PromQL expression is corrupted by shell expansion

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:30`  
**Severity:** blocker

<!-- codex: The nested quotes and unescaped closing brace inside `${GATE_QUERY:=...}` produce `count(ALERTS{alertstate=firing,alertname=~CNPG.*|PostgresReplica.*|PostgresReplicationLagHigh`, as reproduced under sh; real Prometheus rejects this expression, so every live execution pauses before deleting. Assign the default using safe literal quoting and make the mock validate the decoded query parameter, which it currently ignores. -->

### Probe boolean serialization prevents every reclone execution

**Location:** `kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml:140`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:44`; `scripts/tests/cnpg-lost-slot-reclone-mock.py:128`  
**Severity:** blocker

<!-- codex: PostgreSQL concatenation converts booleans to text `true`/`false`, so the probe writes `in_recovery=false` and slot activity values `true`/`false`, while the consumer accepts only `f`/`t`; consequently even a successful primary probe exits 1 as malformed, and recovery cannot produce the promised exit 3. Serialize both boolean fields explicitly with CASE expressions or consistently accept their text representation, and test output from the actual probe SQL instead of only handwritten t/f fixtures. -->

### POSIX pipelines hide jq failures

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:69`; `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:98`; `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:109`; `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:124`  
**Severity:** important

<!-- codex: Each `jq ... | tr -d '\r'` pipeline reports tr's successful status under POSIX sh, making the malformed-JSON handlers ineffective: a bad page can become an empty candidate list and a bad revalidation response becomes an ordinary skip, both allowing exit 0 contrary to the failure policy. Check jq's status separately before removing carriage returns, validate required response fields, and add malformed-body tests for discovery, listing, and revalidation without relying on non-POSIX pipefail. -->

### Unexpected Prometheus responses can open the gate

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:55`  
**Severity:** important

<!-- codex: An HTTP 200 response containing `{}` or `{"status":"error"}` becomes zero firing alerts through `// "0"` and permits deletion; this was reproduced with the current selector. Validate the Prometheus success envelope, vector structure, and sample value before accepting it, while preserving a valid empty vector as the no-alert case; unknown response shapes must pause with exit 2. -->

### Gate checks miss the final deletion batch

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:86`; `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:120`  
**Severity:** important

<!-- codex: The periodic gate runs before the next candidate rather than after each GATE_EVERY deletions, so the configured 25-delete canary never performs its required post-batch check when its budget is exhausted; the initial check can also become stale during repository/page scanning before the first DELETE. Check immediately before the first mutation and after each completed batch, including the final batch, with a test where MAX_DELETES_PER_RUN equals GATE_EVERY and alerts start firing at that boundary. -->

### Annotation guards fail open and are not protected against concurrent Jobs

**Location:** `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:39`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:58`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:121`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:125`  
**Severity:** important

<!-- codex: cluster_field suppresses API errors, making failed marker or last-reclone reads indistinguishable from absent annotations, and the final guard refresh omits both annotations before overwriting the marker; transient failures or overlapping manual Jobs can therefore bypass exclusion and cooldown checks despite CronJob concurrencyPolicy. Read and validate a complete Cluster response, recheck both annotations before mutation, and acquire the marker with a resourceVersion precondition so competing executions cannot overwrite it. -->

### Marker verification can succeed without a healthy replacement slot

**Location:** `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:63`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:74`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:90`  
**Severity:** important

<!-- codex: any_lost starts at zero and all_physical_active at one, so an empty matching slot set passes verification; an active slot with wal_status=unreserved also passes because only lost is rejected, and the loop never requires a slot matching the replacement instance. Require the expected replica slots to exist with active=t and wal_status=reserved before clearing the marker and stamping last-reclone, with negative tests for missing slots and non-reserved WAL states. -->

### The deletion wait does not enforce its 150-second bound

**Location:** `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:33`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.sh:132`  
**Severity:** important

<!-- codex: The loop checks its deadline only between kubectl calls, which have no explicit request timeout, so an unresponsive API can hold execution beyond DELETE_WAIT_SECONDS until the separate 280-second Job deadline; API failures during polling are also treated as evidence that objects disappeared. Bound requests by the remaining wait budget and distinguish NotFound from transport/authorization failures, leaving the marker and returning failure when disappearance cannot be established. -->

### The disposable drill needs separate connection and authorization wiring

**Location:** `docs/runbooks/infra-pg.md:64`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml:56`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml:133`; `kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml:185`; `scripts/tests/fixtures/cnpg-reclone-drill.yaml:55`  
**Severity:** important

<!-- codex: Changing CLUSTERS and PGHOST for the planned drill still leaves the Job using infra-pg credentials, infra-pg's CA, and a Role that cannot read or patch reclone-drill, so the documented template adaptation cannot exercise the recovery path. Provide an exact disposable Job recipe with drill-specific credentials, CA, and narrowly scoped ServiceAccount/Role, and apply the isolation policy after establishing baseline replication instead of applying it together with the Cluster. -->

### Promised edge-case coverage is incomplete

**Location:** `scripts/tests/gitea-actions-run-retention-mock.py:235`; `scripts/tests/gitea-actions-run-retention-mock.py:251`; `scripts/tests/cnpg-lost-slot-reclone-mock.py:248`; `scripts/tests/cnpg-lost-slot-reclone-mock.py:254`  
**Severity:** important

<!-- codex: The retention “boundary” fixture is deliberately 1,800 seconds newer than the cutoff and the authorization scenario returns 403 rather than exercising 401; reclone tests remove only the optional WAL PVC and test only a primary change during final revalidation, leaving missing data/all PVCs and phase/role changes uncovered. Add deterministic cutoff equality tests, explicit 401 paths, missing-PVC refusal cases, final phase/role transitions, and marker verification failures while retaining the already-present rerun, marker-clearing, and stuck-marker tests. -->

### Token setup example is invalid YAML and contains broken commands

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-retention.sops.yaml.example:14`; `kubernetes/apps/apps/gitea/gitea-actions-retention.sops.yaml.example:22`; `kubernetes/apps/apps/gitea/gitea-actions-retention.sops.yaml.example:25`  
**Severity:** important

<!-- codex: The uncommented token instruction on line 25 makes the example invalid YAML, confirmed by parsing, while the user-creation and token-mint commands contain embedded `#` characters after `kubectl ... --` that comment out the Gitea command when copied. Supply a valid stringData placeholder and properly formatted, executable setup commands so copying, editing, and encrypting the example works as documented. -->

### The UTC exclusion window is not encoded in the CronJob

**Location:** `kubernetes/apps/apps/gitea/actions-run-retention.yaml:57`  
**Severity:** important

<!-- codex: The schedule promises to exclude 01:00–02:30Z but omits spec.timeZone, so Kubernetes interprets it in the controller manager's local timezone and a non-UTC controller can schedule deletions inside that window. Set timeZone: Etc/UTC to make the exclusion independent of controller configuration and daylight-saving changes. -->

### Remove the unused gate-disable switch

**Location:** `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:31`; `kubernetes/apps/apps/gitea/gitea-actions-run-retention.sh:51`  
**Severity:** nit

<!-- codex: GATE_DISABLED is described as test-only, but the committed tests use a mock Prometheus and never enable this switch, leaving an unused production path that bypasses the replication gate completely. Remove it or justify and explicitly test the additional operational contract. -->

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