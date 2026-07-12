# Implementation review — agentforge-v2 P2 R-1 (sandbox boundary core) — round 4
<!-- codex-r1-review-status: pending -->

## Summary

- The Kubernetes Lease migration is structurally consistent across all three repos: ownership writes are genuine `resourceVersion` CAS operations, Lease RBAC covers every client call, the HMAC/Secret/OpenBao surface is functionally gone, and the runtime Job shape remains VAP-compatible.
- F1a, F5c’s last-writer-wins defect, the missing image dependency, and the reaper argv defect are CLOSED. F5b is only PARTIALLY-CLOSED because missed renewal deadlines are not detected and renewal does not begin until after synchronous staging.
- A further blocker remains after the pre-apply check: the renewer is stopped before potentially unbounded apply/cleanup, and Lease deletion is unconditional. The Lease can expire and be acquired by the reaper after the check, allowing the orchestrator to continue applying and then delete the reaper’s claim.
- The only remaining HMAC/file-lease references are stale comments in several ailab storage manifests; no live Secret, ExternalSecret, environment variable, OpenBao path, or `.leases` implementation remains.
- The managed review environment permitted static reads and Git inspection but blocked Python/kustomize process execution; the focused unit tests and their CAS interleavings were inspected rather than rerun.

## Findings

### [CLOSED] F1a — tenant reconciliation now permits and constrains ESO resources

**Proof:** `ailab/kubernetes/apps/agentforge-tenants-bootstrap/reconciler-rbac.yaml:49-55` grants the tenant reconciler the namespaced `external-secrets.io` `secretstores` and `externalsecrets` resources with reconciliation verbs. `ailab/kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:41-47` admits only the namespaced `SecretStore` and `ExternalSecret` kinds; lines 99-119 constrain SecretStore to the in-cluster OpenBao vault provider, `af` mount, and Kubernetes authentication; lines 120-149 require `creationPolicy: Owner`, a namespaced SecretStore, and per-tenant mount-relative keys while explicitly rejecting the retired `sandbox/` subtree.

The platform emits only the orchestrator-credentials path: `agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:510-523` renders one ExternalSecret extracting `<org>/<workspace>/orchestrator`. No live VAP allowance for `sandbox/lease-hmac` remains.

### [PARTIALLY-CLOSED] F5b — renewal errors fail closed, but deadline and staging freshness do not

**Location:** `agentforge/src/agentforge/adapters/exec/sandbox.py:553-599`; `agentforge/src/agentforge/adapters/exec/sandbox.py:656-669`; `agentforge/src/agentforge/adapters/exec/sandbox.py:741-748`; `agentforge-platform/src/agentforge_platform/settings.py:131-133`

**Severity:** blocker

The renewer now converts CAS conflicts and all renewal exceptions into `lost_claim` at `sandbox.py:559-568`, rejects holder changes at lines 569-573, and treats a thread that cannot stop within the join timeout as inconclusive at lines 590-599. The pre-apply check rejects that state.

Two required fail-closed cases remain absent:

- The Lease is correctly created before staging at lines 656-662, but the renewer is not started until line 669, after synchronous `stage()` completes. A slow or blocked NFS stage can exceed the Lease duration while a visible job directory exists and no Job is Active. The reaper can then CAS-acquire the expired Lease and remove the directory.
- The renewal loop never checks whether the previous `renewTime + duration` elapsed before a renewal completed. Its default period is `max(duration / 2, 1)` at lines 741-748; because the platform accepts a duration of one second, that configuration schedules the first renewal exactly at the expiry boundary. A delayed renewal can also restore freshness after an unobserved lapse when no reaper happened to acquire it, instead of recording the required missed-deadline loss.

<!-- codex: Start the renewer immediately after Lease creation and before stage(), and track a monotonic renewal deadline independently of whether a late CAS eventually succeeds. A tick or response at/after the previous freshness deadline must permanently mark the claim lost, with Kubernetes request timeouts bounded below the remaining lease window. Add staging-over-duration and delayed-renewal regressions. -->

### [CLOSED] F5c — ownership writes are true apiserver CAS, not last-writer-wins file replacement

**Proof:** `agentforge/src/agentforge/adapters/exec/kube.py:289-325` builds every renewal/acquisition replacement with the observed `metadata.resourceVersion` and calls `replace_namespaced_lease`; HTTP 409 is converted to `LeaseConflict`. Creation uses `create_namespaced_lease` at lines 243-265, so the absent-Lease race also fails conditionally.

The orchestrator creates the Lease before exposing the job directory at `agentforge/src/agentforge/adapters/exec/sandbox.py:648-662`. Its pre-apply GET requires the same holder, the exact resourceVersion returned by its last successful renewal, and freshness at lines 686-695 and 721-739.

The reaper rereads the Lease, rejects a fresh observation, and CAS-acquires an expired observation—or creates only if absent—at `agentforge/src/agentforge/adapters/exec/kube.py:397-412`. It then foreground-deletes the Job, proves both Job and Pods absent, reconfirms the exact acquired holder/resourceVersion, and only then performs `rmtree` at lines 413-429.

The fake models monotonically advancing resourceVersions and stale-write conflicts at `agentforge/tests/unit/lease_fakes.py:46-108`. The required RV-k/RV-k+1 interleaving is covered at `agentforge/tests/unit/test_lease.py:115-124`; renewer conflict handling is covered at `agentforge/tests/unit/test_sandbox_executor.py:546-557`, and acquisition during import is shown to abort before apply and leave the reaper as sole owner at lines 672-694. `DiskLeaseStore`, `.leases`, owner/epoch payloads, and file-lease operations are absent from agentforge production and test sources.

### [CLOSED] NEW-image-dep — the runtime and p1-worker image contain `kubernetes_asyncio`

**Proof:** `agentforge/deploy/orchestrator.Dockerfile:32-35` runs `uv sync --frozen --no-dev --extra sandbox`, installing the optional dependency declared at `agentforge/pyproject.toml:19-22`. The runtime copies that environment, and the p1-worker inherits the runtime target at `agentforge/deploy/orchestrator.Dockerfile:75`.

### [CLOSED] NEW-reaper-args — the Deployment invokes the installed CLI correctly

**Proof:** `ailab/kubernetes/apps/infrastructure/agentforge-sandbox/reaper-deployment.yaml:63-70` supplies `args: ["agentforge", "reaper"]`. This composes correctly with `ENTRYPOINT ["tini", "--"]` at `agentforge/deploy/orchestrator.Dockerfile:62` and the `agentforge` console entry point declared at `agentforge/pyproject.toml:24-25`.

### [CLOSED] Cross-repo Lease settings, RBAC, HMAC retirement, and Job/VAP contract are aligned

**Proof:** The real client makes only `create_namespaced_lease`, `read_namespaced_lease`, `replace_namespaced_lease`, and `delete_namespaced_lease` calls at `agentforge/src/agentforge/adapters/exec/kube.py:243-337`. The orchestrator Lease rule grants `create/get/update/delete` at `ailab/kubernetes/apps/infrastructure/agentforge-sandbox/orchestrator-rbac.yaml:39-45`; the reaper has the identical required verbs at `ailab/kubernetes/apps/infrastructure/agentforge-sandbox/reaper-rbac.yaml:42-48`. Neither client lists or watches Leases, so no verb is missing.

The platform emits `AF_LEASE_DURATION_S` at `agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:438-443`; agentforge consumes it, with `AF_LEASE_TTL_S` retained only as a compatibility alias, at `agentforge/src/agentforge/infra/settings.py:78-86`. The cross-repo test constructs the real worker Settings and verifies `lease_duration_s` at `agentforge-platform/tests/unit/test_cross_repo_settings.py:85-117`, and asserts that no lease ExternalSecret or HMAC environment variable is rendered at lines 145-155.

The lease refactor did not change `build_job_manifest`; its current exact Job/pod shape remains at `agentforge/src/agentforge/adapters/exec/sandbox.py:371-472`, matching the previously closed sandbox Pod and Job VAP contracts.

### The pre-apply freshness check does not fence apply/cleanup, and deletion can erase a newer claim

**Location:** `agentforge/src/agentforge/adapters/exec/sandbox.py:686-707`; `agentforge/src/agentforge/adapters/exec/kube.py:327-337`; `agentforge/src/agentforge/adapters/exec/kube.py:397-429`

**Severity:** blocker

<!-- codex: The orchestrator stops renewal, performs one instantaneous freshness GET, then runs potentially unbounded NFS apply/cleanup and finally deletes the Lease by name without a resourceVersion precondition. A Lease with little remaining freshness can expire after the GET; the reaper can CAS-acquire it while apply_back is running, after which the orchestrator still applies/removes files and its blind delete can delete the reaper's newer Lease. Fence the complete critical section: keep ownership valid through shared-tree cleanup, use resourceVersion-preconditioned deletion, and make the final apply transition atomic/fenced or establish a CAS-held completion state whose validity cannot expire during apply. Add a regression for pre-check-at-expiry-minus-epsilon → reaper acquire → orchestrator cleanup/delete. -->

### Stale file-lease and lease-Secret descriptions remain in storage-manifest comments

**Location:** `ailab/kubernetes/apps/infrastructure/agentforge-sandbox/reaper-storage.yaml:6-11`; `ailab/kubernetes/apps/infrastructure/agentforge-sandbox/workspace-pvc.yaml:10-13`; `ailab/kubernetes/apps/infrastructure/agentforge-sandbox/staging-pv.yaml:15-18`; `ailab/kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:13-15`

**Severity:** nit

<!-- codex: These comments still describe `<staging_root>/.leases/<job-id>`, a lease Secret, or a “shared-key shape,” although the corresponding wiring is gone. Update them to describe only the shared job directories and the RBAC-gated Kubernetes Lease so future operators do not recreate obsolete HMAC/Secret prerequisites. -->

## Verdict

The R-1 boundary core is not yet sound to proceed to R-2. The Kubernetes Lease replacement is a genuine CAS implementation, its API calls and RBAC are aligned, the HMAC surface is functionally removed, F1a and both activation defects are closed, and the gated reaper/live NFS proof remain valid image-build prerequisites rather than findings. However, ownership is not continuously fresh from staging through the final critical section: missed renewal deadlines can be healed silently, staging occurs before renewal begins, and the post-check apply/cleanup path can outlive freshness and blindly delete a newer reaper claim. Those are code-level lease-safety blockers that must be closed before carrying only reaper enablement and the live integration proof as v1.1-flip prerequisites.