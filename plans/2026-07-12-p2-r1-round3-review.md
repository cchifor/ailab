# Implementation review — agentforge-v2 P2 R-1 (sandbox boundary core) — round 3
<!-- codex-r1-review-status: pending -->

## Summary

- The Job/VAP shape, UID 65532 contract, watch-based quiescence, full-mode importer check, and successful lease-file cleanup are now internally consistent. F1b, F2, F3, F4, and the round-2 lease-file leak are CLOSED.
- F1a is only PARTIALLY-CLOSED: the platform really renders and internally allowlists the lease ExternalSecret, but the live tenant reconciler RBAC and tenant VAP reject every SecretStore and ExternalSecret.
- F5b is PARTIALLY-CLOSED and F5c remains OPEN. Renewal covers a normally slow import, but renewal failures are not surfaced, and the purported claim CAS is a last-writer-wins replace with several destructive interleavings.
- Two additional activation blockers remain in reviewed core artifacts: the orchestrator/reaper image omits its Kubernetes client dependency, and the reaper Deployment invokes a nonexistent bare `reaper` executable. These are code/manifest defects, not objections to the agreed image gate.

## Findings

### [PARTIALLY-CLOSED] F1a — the lease ExternalSecret is rendered, but the live tenant path cannot apply it

**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:62; agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:557; agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:591; agentforge-platform/tests/unit/test_cross_repo_settings.py:88; agentforge-platform/tests/unit/test_cross_repo_settings.py:145; ailab/kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:36; ailab/kubernetes/apps/agentforge-tenants-bootstrap/reconciler-rbac.yaml:25

**Severity:** blocker

**Proof:** The platform half is real: renderer.py:557-566 emits a tenant-namespace `ExternalSecret`, targets `af-sbx-lease` with `creationPolicy: Owner`, and extracts `sandbox/lease-hmac`. ExternalSecret is in the renderer GVK allowlist at renderer.py:62-75 and is field-checked at renderer.py:652-672. The cross-repo test constructs the real agentforge `Settings` at test_cross_repo_settings.py:88-120 and separately asserts the emitted ExternalSecret and matching `secretKeyRef.name` at test_cross_repo_settings.py:145-163.

The cluster-side tenant path is still P1-only. tenant-guard.yaml:36-43 allows core, apps, RBAC, networking, and Cilium GVKs but no `external-secrets.io` SecretStore or ExternalSecret, so the fail-closed VAP denies both rendered ESO objects. Independently, reconciler-rbac.yaml:25-48 grants no `external-secrets.io` resources, so Flux cannot create them. Consequently the namespace-local `af-sbx-lease` Secret still cannot materialize through the real reconciliation path.

<!-- codex: Add namespaced SecretStore and ExternalSecret resources to the tenant reconciler RBAC and to the tenant VAP GVK allowlist, with strict pins for the rendered store identity, targets, creationPolicy, per-tenant credentials key, and shared `sandbox/lease-hmac` key. Add a contract test against this operator-side policy, not only the renderer’s in-process allowlist. -->

### [CLOSED] F1b — UID/GID 65532 is unified across every NFS consumer

**Proof:** agentforge/deploy/orchestrator.Dockerfile:44-57 creates and runs the orchestrator as `65532:65532`, with `/app`, state, and jobs paths chowned accordingly; the p1-worker target restores the same identity at line 91. agentforge/deploy/sandbox.Dockerfile:45-51 does the same for the sandbox image. agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:422-434 overrides the P2 orchestrator pod to `runAsUser`, `runAsGroup`, and `fsGroup` 65532 with `runAsNonRoot`, and line 456 renders importer expected UID 65532. agentforge/src/agentforge/adapters/exec/sandbox.py:132-133 and :446-486 pin the sandbox pod/container identity. ailab/kubernetes/apps/infrastructure/agentforge-sandbox/reaper-deployment.yaml:57-62 pins the reaper to the same identity. agentforge/src/agentforge/main.py:75-83 passes the rendered expected UID into the importer.

### [CLOSED] F2 — both VAPs require the exact per-profile home/workspace/broker shape

**Proof:** ailab/kubernetes/apps/infrastructure/agentforge-sandbox/sandbox-guard.yaml:92-110 requires `workspace` and `home`, with exactly two volumes for `test` and exactly three including `broker-cap` for `agent`; lines 144-159 impose the matching exact mount counts and paths. sandbox-job-guard.yaml:91-109 and :138-153 mirror those requirements on the Job template. agentforge/src/agentforge/adapters/exec/sandbox.py:411-426 emits exactly those sets: workspace at `/workspace` with `subPath == job_id`, home at `/home/nonroot` with `1Gi`, and the agent-only read-only broker-cap mount at `/var/run/af/broker` with `1Mi`. Both limits are below their VAP ceilings.

The remainder of the generated Job also matches both policies: sandbox.py:403-409 supplies the required labels; :446-490 supplies UID/GID 65532, restricted container security, tokenless SA, `runtimeClassName`, grace period, resources, and volumes; :463-473 supplies the required name and Job lifecycle knobs.

### [CLOSED] F3 — quiescence now fails closed through list-plus-watch

**Proof:** agentforge/src/agentforge/adapters/exec/kube.py:123-137 obtains a fresh Pod-list resourceVersion together with current Job/Pod state. kube.py:139-179 watches labeled Pods from that RV, folds additions/deletions, rereads the Job on events, converts HTTP 410 to `WatchExpired`, and propagates other failures. agentforge/src/agentforge/adapters/exec/sandbox.py:778-810 foreground-deletes the Job, accepts only the explicit `Job absent AND Pod set empty` state, relists after 410 or clean EOF, and raises `QuiescenceTimeoutError` on an inconclusive deadline. Import begins only after `_quiesce()` returns at sandbox.py:665-670. Regression coverage for ordering, RV restart, and timeout-without-import/cleanup exists at tests/unit/test_sandbox_executor.py:407-447 and :554-585. No fail-open path remains.

### [CLOSED] F4 — pre-open/post-open comparison includes all permission and special-mode bits

**Proof:** agentforge/src/agentforge/adapters/exec/import_validator.py:338-358 compares inode/device, file type, `stat.S_IMODE(st_mode)`, link count, and owner between the pre-open `lstat` and post-open `fstat`. `S_IMODE` includes ordinary permission bits plus setuid, setgid, and sticky bits. tests/unit/test_import_validator.py:501-512 covers both ordinary permission drift and setuid drift.

### [PARTIALLY-CLOSED] F5b — renewal spans import, but renewal failure is not fail-closed

**Location:** agentforge/src/agentforge/adapters/exec/sandbox.py:539; agentforge/src/agentforge/adapters/exec/sandbox.py:568; agentforge/src/agentforge/adapters/exec/sandbox.py:580; agentforge/src/agentforge/adapters/exec/sandbox.py:655; agentforge/tests/unit/test_sandbox_executor.py:684

**Severity:** blocker

**Proof:** The intended happy path is implemented. `_LeaseRenewer` is a daemon thread at sandbox.py:539-574, starts before Job creation at :655-662, remains active across quiescence, import, result parsing, and apply-back, and is stopped in `finally` at :688-689. The slow synchronous-import regression test at test_sandbox_executor.py:684-723 demonstrates repeated renewal while the event loop is blocked.

The thread does not catch an exception from `_renew()` at sandbox.py:568-574. A transient or blocking lease write can therefore kill or strand the thread without setting `lost_claim`. `stop()` waits only ten seconds at :580-582 and does not report that the daemon remains alive; cleanup can proceed while that thread may later complete another lease write. The final ownership check also matches owner/epoch but not freshness, so an expired orchestrator lease left by a dead renewal thread can still authorize apply-back while the reaper is eligible to claim it.

<!-- codex: Treat every renewal exception, missed renewal deadline, or non-terminating renewal thread as lost/inconclusive ownership and abort before apply or cleanup. Track the last successful renewal and require a fresh persisted claim; cleanup must not unlink the lease while a renewal write can still complete. -->

### [OPEN] F5c — atomic ownership hand-off is still a last-writer-wins race

**Location:** agentforge/src/agentforge/adapters/exec/reaper.py:84; agentforge/src/agentforge/adapters/exec/reaper.py:143; agentforge/src/agentforge/adapters/exec/kube.py:248; agentforge/src/agentforge/adapters/exec/kube.py:269; agentforge/src/agentforge/adapters/exec/sandbox.py:568; agentforge/src/agentforge/adapters/exec/sandbox.py:647; agentforge/src/agentforge/adapters/exec/sandbox.py:675; agentforge/src/agentforge/adapters/exec/sandbox.py:688

**Severity:** blocker

**Proof:** `DiskLeaseStore.write_lease()` at reaper.py:143-154 and the orchestrator writer use atomic `os.replace`, but atomic replacement is not compare-and-swap: it is a last-writer-wins register. Reaper.run_once() reads eligibility at reaper.py:84-90, but `KubernetesReaperBackend._claim()` later rereads whatever lease is current and unconditionally overwrites it at kube.py:269-286. If the orchestrator renewed to a fresh lease between those operations, the reaper still steals it; `_claim()` never rechecks expiry or compares against the lease that justified reclaim.

A second unsafe interleaving remains between the orchestrator’s separate ownership read and renewal write at sandbox.py:568-574 and the reaper’s final check followed by `rmtree` at kube.py:261-265:

1. The renewer reads orchestrator/epoch 0.
2. The reaper writes and confirms reaper/epoch 1.
3. The reaper’s final `_claim_held` reads epoch 1.
4. The renewer writes orchestrator/epoch 0 after that check.
5. The reaper executes `rmtree`.
6. The importer reads its restored epoch-0 lease at sandbox.py:675-681 and applies the already-imported tree.

Thus the epoch can move backwards and both sides can believe they won. The single pre-apply check does not serialize apply-back, and the `finally` cleanup at sandbox.py:688-697 does not reverify ownership after apply.

There is also an unleased staging window: `run()` exposes the 32-character job directory through `stage()` at sandbox.py:647 before creating the first lease at :650. The reaper lists such directories at reaper.py:121-129 and treats an absent lease plus inactive Job as reclaimable at :34-46, so it can claim and remove a directory while the orchestrator is still staging it.

<!-- codex: Replace write-then-confirm with a genuinely conditional ownership primitive, such as Kubernetes Lease resourceVersion CAS or an NFS-safe exclusive claim protocol that prevents an orchestrator renewal from overwriting a reaper epoch. CAS the exact expired observation, recheck Job activity under the acquired claim, establish and renew ownership before exposing/staging the job directory, and hold or revalidate that ownership across apply and cleanup. -->

### [CLOSED] NEW from round 2 — successful runs unlink the lease file

**Proof:** agentforge/src/agentforge/adapters/exec/sandbox.py:332-348 distinguishes real directories from regular files/symlinks and uses `os.unlink` for the latter. The real-filesystem end-to-end regression at tests/unit/test_sandbox_executor.py:857-889 asserts that `.leases/<job_id>` is absent after a successful run.

### [NEW] The orchestrator and reaper image omits the Kubernetes client dependency

**Location:** agentforge/pyproject.toml:19; agentforge/deploy/orchestrator.Dockerfile:22; agentforge/src/agentforge/main.py:72; agentforge/src/agentforge/main.py:412

**Severity:** blocker

The only declaration of `kubernetes-asyncio` is the optional `sandbox` extra at pyproject.toml:19-22. The image build runs `uv sync --frozen --no-dev` at orchestrator.Dockerfile:22-32 without `--extra sandbox`, so that dependency is not installed in either the runtime or inherited p1-worker target. Both the P2 executor path at main.py:72-76 and reaper path at :412-420 instantiate `KubernetesJobClient`, which imports `kubernetes_asyncio`; the built image will fail before performing Kubernetes work.

<!-- codex: Build the orchestrator/p1-worker venv with the sandbox extra, for example `uv sync --frozen --no-dev --extra sandbox`, and add an image-level smoke test that imports `kubernetes_asyncio` and constructs the P2 CLI path. -->

### [NEW] The gated reaper Deployment invokes a nonexistent executable

**Location:** agentforge/deploy/orchestrator.Dockerfile:57; agentforge/deploy/orchestrator.Dockerfile:59; agentforge/pyproject.toml:24; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/reaper-deployment.yaml:63

**Severity:** blocker

The image entrypoint is `["tini", "--"]` and its installed console command is `agentforge` at pyproject.toml:24-26. reaper-deployment.yaml:63-67 overrides the image CMD with `args: ["reaper"]`, producing `tini -- reaper`, but no `reaper` executable is installed. Pinning the digest and uncommenting the Deployment would therefore enter a crash loop rather than start `agentforge reaper`.

<!-- codex: Change the container args to `["agentforge", "reaper"]` or provide an equivalent explicit command, and assert the final OCI entrypoint-plus-args composition in a manifest/image smoke test. -->

## Verdict

The R-1 boundary core is not yet sound to proceed to R-2. The tightened Job/VAP contract, identity alignment, quiescence proof, importer metadata binding, ESO key shape, and lease-file cleanup are correct, and the gated reaper enablement plus live NFS integration proof remain legitimate post-image-build prerequisites rather than findings. However, the tenant reconciliation path currently cannot apply the ESO resources, the lease protocol still permits importer/reaper double-ownership and unleased staging, and the reviewed image/Deployment cannot start the Kubernetes executor or reaper without further code and manifest changes. These are remaining core blockers, not consequences of the expected image gate.