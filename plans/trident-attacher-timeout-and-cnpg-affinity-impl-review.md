# Implementation review — trident-attacher-timeout-and-cnpg-affinity — round 1

<!-- codex-impl-review-status: finalized -->

## Findings

### Pod label selector uses labels instead of annotations (CRITICAL)

**Resolution (opus):** ACCEPTED and fixed in `66cd8fb9` — airlock labels the pod with `airlock.strive.io/tenant` and `sandbox-id` and carries `app-id` as an annotation (verified on a live pod); `find_pod()` now selects on the tenant label and filters the app-id annotation in jq; mock suite 8/8 after the change.

**Location:** scripts/airlock-recycle-sandbox.sh:142, :181

**Severity:** blocker

The script uses '-l "airlock.strive.io/app-id=APP"' as a Kubernetes label selector. However, examining platform code (deploy/components/apps/api/v1/endpoints/apps.py), Airlock stores the app-id as a pod annotation, not a label. This causes the verification phase to find zero pods matching the selector, and the loop waits until POLL_TIMEOUT (900s by default) then exits with code 7 (verify failed).

**Fix:** Change the kubectl calls to select by annotation. The annotation name must match Airlock's constant (check the platform codebase for ANN_APP_ID).

### Kyverno policy foreach/precondition structure correct

**Location:** kubernetes/apps/storage-policies/trident-attacher-timeout.yaml:63–83

**Severity:** nit (informational)

The nested foreach structure is correct. The outer loop iterates request.object.spec.template.spec.containers (original array), the inner loop iterates element.args with a fallback for containers without args. Both preconditions are in the inner loop because Kyverno forbids mixing declarations in a nested foreach. The indices are correct: elementIndex0 for the container position, elementIndex1 for the arg position. The JSON patch replaces ONE value, leaving every other container and argument byte-identical. This matches the plan.

### Token security in recycle script — correct

**Location:** scripts/airlock-recycle-sandbox.sh:64–71, :81–84

**Severity:** nit (informational)

Token handling is secure and follows the plan exactly. Token is read from --token-fd N (file descriptor redirection) or hidden prompt (read -rs), never from argv, never exported to env. The token passes to curl via stdin as --config -, so it appears only in the Authorization header, never in argv or redirected to stderr. Error bodies strip the token field. No issues.

### Mock test coverage includes both 202 and 200/204 contracts

**Location:** scripts/tests/airlock-recycle-mock.py:143–152

**Severity:** nit (informational)

Test cases cover both async (202 AppOperationOut with poll) and sync (200 AppOut / 204 no-body) contracts. Cases: "happy-202" (202 contract), "sync-contract" (204 teardown / 200 deploy), "wrong-tenant" (preflight reject), "auth-before" (401 on GET), "auth-after" (401 on deploy POST), "teardown-fails" (operation fails, no deploy), "deploy-timeout" (operation polling times out), "happy-202 --resume-deploy" (skip teardown). Each case asserts exit code, required phases, forbidden POST paths, and that the token never leaks outside the Authorization header. 8/8 core paths covered.

### Flux Kustomization wiring correct

**Location:** kubernetes/apps/clusters/ai/storage-policies.yaml

**Severity:** nit (informational)

The storage-policies Kustomization is in flux-system, depends on platform-kyverno (blocking synchronization), has wait: true and retryInterval: 1m. The sourceRef points to flux-system GitRepository, path is ./kubernetes/apps/storage-policies. Comment explains the cycle prevention correctly. No issues.

### Platform CNPG affinity block added with comment preserved

**Location:** C:/Users/chifo/work/platform-wt-affinity, deploy/components/cnpg-cluster/cluster.yaml (HEAD)

**Severity:** nit (informational)

The header comment now says "Anti-affinity is REQUIRED per hostname — see the affinity block below for why the earlier preferred decision was reversed. No off-cluster backup." The "No off-cluster backup" comment is preserved. The affinity block is added with enablePodAntiAffinity: true, podAntiAffinityType: required, topologyKey: kubernetes.io/hostname. This matches the plan exactly. Correct.

### Kyverno test fixtures comprehensive

**Location:** kubernetes/apps/storage-policies/tests/run.py:120–184

**Severity:** nit (informational)

The test harness generates 11 fixture scenarios: (1) stock 60s to 600s mutation, (2) already-600s (idempotent no-op), (3) containers reordered, (4) args reordered, (5) duplicate --timeout flags (both replaced), (6) absent flag (no-op), (7) split --timeout 60s (no-op), (8) no args at all (no-op), (9) sidecar renamed (no-op), (10) other Deployment name (unmatched), (11) other namespace (unmatched). Each case runs through the Kyverno CLI test harness and asserts the expected result. The expected_mutation() function confirms all other containers are byte-identical and only the attacher's --timeout=* is replaced. Thorough and matches the plan.

### Runbook documentation complete

**Location:** docs/runbooks/qnap-storage-setup.md (section 9), docs/runbooks/node-maintenance.md (sections 1b–1c, post-checks)

**Severity:** nit (informational)

The qnap-storage-setup.md section 9 explains the O(n^2) sweep, gives the integration post-check, explains the failurePolicy:Ignore choice, covers operator interaction and rollback, and defines stale transactions precisely. node-maintenance.md adds pre-checks and post-checks. Sandbox recycle path mentioned. Correct and complete.

## Diff stat

 docs/runbooks/node-maintenance.md                  |  17 ++
 docs/runbooks/qnap-storage-setup.md                |  69 ++++++++
 kubernetes/apps/clusters/ai/storage-policies.yaml  |  22 +++
 .../apps/storage-policies/kustomization.yaml       |   5 +
 .../storage-policies/tests/base-deployment.json    |  93 ++++++++++
 kubernetes/apps/storage-policies/tests/run.py      | 189 ++++++++++++++++++++
 .../storage-policies/trident-attacher-timeout.yaml |  83 +++++++++
 scripts/airlock-recycle-sandbox.sh                 | 190 +++++++++++++++++++++
 scripts/tests/airlock-recycle-mock.py              | 188 ++++++++++++++++++++
 9 files changed, 856 insertions(+)
