# Implementation review — agentforge-v2 P2 R-1 (sandbox boundary core) — round 2
<!-- codex-r1-review-status: pending -->

## Summary

- R-1 still does not hold as a self-contained boundary. Finding 1 remains OPEN, Findings 2–4 are PARTIALLY-CLOSED, and Finding 5 remains OPEN.
- The original env-name mismatches, static-PV topology, secret/resource admission controls, fail-closed quiescence timeout, descriptor-bound copying, wall-clock domain, and out-of-subtree lease placement are materially corrected.
- Two new cross-repo blockers prevent a working sandbox: the tenant orchestrator references an undeployed lease Secret, and UID 1000 staging is neither writable by nor importable as the UID 65532 sandbox.
- The reaper is still deliberately excluded from kustomization with an unpullable image. Even if enabled, import-time leases are not periodically renewed and reclaim has no atomic ownership hand-off.
- The largest remaining safety risk is the importer/reaper race: the synchronous import runs over a hard-mounted NFS volume without renewal, while the reaper can decide from an expired lease and reclaim without acquiring ownership or rechecking it.

## Findings

### [OPEN] Finding 1 — rendered configuration and shared-filesystem contract

**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:411; agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:431; agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:445; agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:456; agentforge-platform/tests/unit/test_cross_repo_settings.py:69; agentforge/deploy/orchestrator.Dockerfile:42; agentforge/deploy/orchestrator.Dockerfile:53; agentforge/src/agentforge/adapters/exec/sandbox.py:198; agentforge/src/agentforge/adapters/exec/sandbox.py:205; agentforge/src/agentforge/adapters/exec/sandbox.py:370; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/reaper-lease.sops.yaml:14; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/kustomization.yaml:24

**Severity:** blocker

**Proof:** The literal environment contract is corrected: renderer.py:411-435 emits the real `Settings` names, including `AF_SANDBOX_SERVICE_ACCOUNT`, workspace, lifecycle settings, staging root, expected UID, lease TTL, and HMAC reference. settings.py:61-81 consumes those names, and the old names remain only in negative tests. The tenant PVC is mounted at the staging root at renderer.py:445-450 and is statically bound with `storageClassName: ""` plus `volumeName` at renderer.py:456-465. The three operator PVs use RWX, Retain, NFSv4.0, and the same `10.55.0.254:/pve-nfs/agentforge-sandbox` export at workspace-pvc.yaml:31-44, staging-pv.yaml:30-43, and reaper-storage.yaml:20-33.

The orchestrator’s `secretKeyRef` is not itself an admission violation: sandbox-guard.yaml:205-210 and sandbox-job-guard.yaml:190-193 bind only to `agentforge-sandbox`, while the orchestrator is rendered into an `af-tenant-*` namespace. However, the referenced `af-sbx-lease` Secret is not provisioned there. The renderer references it at renderer.py:431-435, its allowlist contains no Secret GVK at renderer.py:59-73, and its only ExternalSecret targets the separate orchestrator-credentials Secret at renderer.py:503-515. The only declared `af-sbx-lease` is in namespace `agentforge` at reaper-lease.sops.yaml:12-21, and that manifest is itself excluded at kustomization.yaml:24-28. Kubernetes Secret references are namespace-local, so the tenant orchestrator will fail pod startup. The cross-repo test masks this by replacing every `valueFrom` with a synthetic string at test_cross_repo_settings.py:69-82.

The storage is also not writable/importable across the configured identities. The orchestrator image runs as UID 1000 at orchestrator.Dockerfile:42-53; `stage()` uses ordinary `mkdir`, `copytree`, and `copy2` without chowning at sandbox.py:198-210. The sandbox is forced to UID 65532 at sandbox.py:370-378, and the renderer tells the importer to reject every entry not owned by UID 65532 at renderer.py:423-426. A `0777` export root does not change the owner or modes of subsequently created directories/files. Thus normal AUTH_SYS NFS creates UID-1000 staged entries—typically 0755/0644—which the sandbox cannot reliably mutate and which the importer rejects unchanged.

<!-- codex: Provision namespace-local orchestrator and reaper Secret copies from one authoritative key source, and test their actual presence rather than simulating valueFrom. Align all three NFS consumers to one enforced numeric UID/GID or export mapping, normalize every staged entry accordingly, and verify both sandbox writes and reaper deletion on the real NFS mount. -->

### [PARTIALLY-CLOSED] Finding 2 — sandbox admission and resource bounds

**Location:** ailab/kubernetes/apps/infrastructure/agentforge-sandbox/sandbox-guard.yaml:90; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/sandbox-guard.yaml:153; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/sandbox-guard.yaml:162; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/sandbox-job-guard.yaml:90; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/sandbox-job-guard.yaml:144; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/resource-limits.yaml:5

**Severity:** important

**Proof:** Both policies correctly reject any `envFrom` and any `env[].valueFrom` at sandbox-guard.yaml:153-161 and sandbox-job-guard.yaml:144-149. Both require CPU, memory, and ephemeral-storage requests and limits with floors, ceilings, and request≤limit at sandbox-guard.yaml:162-180 and sandbox-job-guard.yaml:150-167. ResourceQuota and LimitRange are active through kustomization.yaml:16 and defined at resource-limits.yaml:5-52. The Job guard now mirrors the container, pod security-context, volume, mount, environment, resource, lifecycle, and scheduler constraints.

The required home volume is still optional. Both volume expressions allow `size(volumes) >= 1`, require only `workspace`, and merely validate `home` if present at sandbox-guard.yaml:90-104 and sandbox-job-guard.yaml:90-105. The mount expressions similarly require only the workspace mount at sandbox-guard.yaml:136-146 and sandbox-job-guard.yaml:132-143. A one-volume/one-mount template therefore passes both policies without the bounded home `emptyDir`, contrary to the fix contract’s exact shape.

The remaining requested perimeter checks are sound: orchestrator-rbac.yaml:25-38 grants no `pods/create`, while networkpolicy.yaml:15-18 and cilium-egress.yaml:18-25 place the sandbox namespace and test profile under zero egress.

<!-- codex: Require exactly one `home` emptyDir and matching `/home/nonroot` mount in both policies, with profile-dependent exact volume/mount counts; keep the existing sizeLimit checks on that mandatory object. -->

### [PARTIALLY-CLOSED] Finding 3 — quiescence fails closed but does not use the contracted watch proof

**Location:** agentforge/src/agentforge/adapters/exec/sandbox.py:526; agentforge/src/agentforge/adapters/exec/sandbox.py:531; agentforge/src/agentforge/adapters/exec/sandbox.py:550; agentforge/src/agentforge/adapters/exec/sandbox.py:612; agentforge/src/agentforge/adapters/exec/kube.py:58; agentforge/src/agentforge/adapters/exec/kube.py:108

**Severity:** important

**Proof:** The dangerous timeout behavior is fixed. `_quiesce()` performs a foreground Job deletion at sandbox.py:622-624, returns only after both the Job and labeled Pods are absent at sandbox.py:627-630, and raises `QuiescenceTimeoutError` at sandbox.py:631-635. The caller imports only after that return at sandbox.py:531-538. Its `quiesced` flag remains false on failure, so the `finally` block does not remove the job or import directories at sandbox.py:550-557.

The required watch implementation is absent. `_quiesce()` explicitly describes and performs polling at sandbox.py:615-630. `KubernetesJobClient` exposes status reads and Pod lists at kube.py:58-109 but no watch operation or resource-version handling. Poll/list errors fail closed by propagation, so this is safer than round 1, but it does not implement or test the authoritative watch-closure/RV-loss contract. The separate requirement to maintain a valid lease through import also remains false under Finding 5.

<!-- codex: Replace the polling seam with list-plus-watch for both Job and labeled Pods, restarting from a fresh list/resourceVersion after EOF or 410/RV loss and treating every inconclusive closure as failure. -->

### [PARTIALLY-CLOSED] Finding 4 — descriptor-bound importer

**Location:** agentforge/src/agentforge/adapters/exec/import_validator.py:307; agentforge/src/agentforge/adapters/exec/import_validator.py:338; agentforge/src/agentforge/adapters/exec/import_validator.py:445; agentforge/src/agentforge/adapters/exec/import_validator.py:531; agentforge/src/agentforge/adapters/exec/import_validator.py:562; agentforge/src/agentforge/adapters/exec/import_validator.py:637; agentforge/src/agentforge/adapters/exec/sandbox.py:227

**Severity:** important

**Proof:** Files and directories are opened descriptor-relatively, then checked with `fstat()` and fd-based `listxattr()` at import_validator.py:531-547 and import_validator.py:562-575. Actual copied bytes enforce both per-file and cumulative caps at import_validator.py:637-657. Directories, files, and symlinks share the total-entry bound at import_validator.py:445-466. The Linux-capable path refuses the degraded walker by default at import_validator.py:398-406. `.af-result.json` is opened using `O_NOFOLLOW`, checked with `fstat`, and bounded at sandbox.py:227-246.

The ENOTSUP/EOPNOTSUPP carve-out at import_validator.py:307-335 is sound for this NFSv4.0 channel: it is narrowly based on the filesystem syscall result, while every successfully listed xattr causes immediate rejection. No `getxattr()` is needed because values are never accepted or copied; presence alone rejects the entry. Through the same mount, a sandbox cannot create an xattr when the filesystem reports the entire operation unsupported.

The pre-open comparison is nevertheless incomplete. `_verify_unchanged()` compares inode/device, file type, link count, and owner at import_validator.py:338-353, but never compares the complete `st_mode` requested by the fix contract. A permission/special-mode transition that retains the same inode and type is evaluated only from the post-open snapshot rather than being rejected as metadata drift. Destination normalization prevents an obvious metadata-copy escape, but the promised check/open invariant and its regression coverage are not complete.

<!-- codex: Compare the full relevant `st_mode` value between pre-open lstat and post-open fstat and add a mode-drift regression test alongside the inode/type/nlink/owner tests. -->

### [OPEN] Finding 5 — lease/reaper hand-off is not deployed or race-free

**Location:** ailab/kubernetes/apps/infrastructure/agentforge-sandbox/kustomization.yaml:20; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/kustomization.yaml:24; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/reaper-deployment.yaml:12; ailab/kubernetes/apps/infrastructure/agentforge-sandbox/reaper-deployment.yaml:65; agentforge/src/agentforge/adapters/exec/sandbox.py:494; agentforge/src/agentforge/adapters/exec/sandbox.py:535; agentforge/src/agentforge/adapters/exec/sandbox.py:572; agentforge/src/agentforge/adapters/exec/reaper.py:77; agentforge/src/agentforge/adapters/exec/kube.py:165

**Severity:** blocker

**Proof:** Wall-clock consistency and placement are fixed: the executor defaults to `time.time()` at sandbox.py:494-498, the reaper is constructed with `time.time` at main.py:419-426, and leases live at `<staging_root>/.leases/<job_id>` at lease.py:33-51. Writes use temp-plus-`os.replace` at sandbox.py:212-221. The dormant deployment is correctly placed in trusted namespace `agentforge` at reaper-deployment.yaml:25-29, its Secret reference names a Secret in that namespace, its RBAC is limited to Job/Pod read/delete at reaper-rbac.yaml:22-39, and reaper-netpol.yaml:13-40 allows API/DNS egress outside the sandbox zero-egress policy.

It is not deployed. kustomization.yaml:24-28 explicitly excludes both the Secret and Deployment, and reaper-deployment.yaml:64-65 still uses an all-zero placeholder digest. R-1 therefore has no controller reclaiming expired Job directories.

Renewal is also not periodic through import. `_await_terminal()` renews only while polling the running Job at sandbox.py:572-590. After quiescence, `run()` writes the lease once at sandbox.py:535-538, then performs synchronous import, result parsing, and apply-back without another renewal. The backing PVs use `hard` NFS mounts, so an import can block beyond the 900-second TTL. The reaper may then see an inactive Job and expired lease while the importer is still live.

Finally, reclaim does not acquire ownership atomically. `Reaper.run_once()` reads `job_active`, then the lease, then calls `reclaim()` at reaper.py:77-90. The backend immediately deletes the Job at kube.py:165-175 without claiming the lease slot or rechecking whether the orchestrator renewed it. Waiting for Pod disappearance before `rmtree` protects against a live sandbox writer, but not against racing a live importer after the Job has already quiesced.

<!-- codex: Pin and enable the reaper Deployment and both namespace-local Secret copies, renew ownership concurrently for the entire run/quiesce/import/apply interval, and make reclaim atomically claim the lease before rechecking lease, Job, and Pod state. The importer must detect a lost claim and abort before apply-back. -->

### [NEW] Successful runs do not remove their owner-lease file

**Location:** agentforge/src/agentforge/adapters/exec/sandbox.py:271; agentforge/src/agentforge/adapters/exec/sandbox.py:550

**Severity:** important

**Proof:** Successful cleanup calls `self._fs.remove(lease_file)` at sandbox.py:554-557, but `RealSandboxFS.remove()` always invokes `shutil.rmtree(path, ignore_errors=True)` at sandbox.py:271-272. `lease_file` is a regular file, so the error is suppressed and the lease remains under `.leases`. The reaper enumerates job directories rather than orphan lease files at reaper.py:120-128, so these files accumulate permanently.

<!-- codex: Use a dedicated atomic file unlink for lease cleanup, or make `remove()` distinguish directories from regular files; add a real-filesystem success-path assertion that `.leases/<job_id>` is gone. -->

## Verdict

R-1 is not sound enough to proceed to R-2. The admission and importer fixes substantially reduce the original attack surface, but the currently rendered deployment cannot obtain its lease key or perform a valid UID hand-off, the reaper is absent, and the lease protocol can still race a long or blocked import. Close those blockers, implement the required watch and exact metadata/home constraints, then rerun a real NFS-backed env→Settings→Job→VAP→quiesce/import/reaper integration proof before beginning the broker/credential split.