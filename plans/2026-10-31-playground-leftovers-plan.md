# Playground leftovers (#1505): re-point the reaper, move the boundary canary, delete the playground storage

## Context

The playground retirement (#1503, `plans/2026-10-30-retire-playground-plan.md`) kept three things on purpose. #1505 tracks them.

**1. The reaper still mounts the playground staging subdir.** `KubernetesReaperBackend.list_job_ids` lists 32-char alnum dirs under its one staging mount. That mount is `af-sbx-reaper-tenant-zero-playground` (NFS `/pve-nfs/agentforge-sandbox/tenant-zero/playground`), which is now empty. So platform-dev has never been reaped. Measured 2026-10-31:
- `tenant-zero/platform-dev` staging holds 24 job dirs, all 74–87 days old, about 416 MB in total.
- `.deps` and `.deps-staging` are not 32-char alnum, so the reaper never lists them.
- Seven of those dirs still have `af-sbx-<id>` Leases. Their holders are dead `af-orch-platform-dev-delivery-*` pods, and each renewTime is from 2026-08-16/17 with a 300 s duration, so all seven are expired. The other 17 dirs have no Lease.
- There are no Jobs or Pods in `agentforge-sandbox`.
- Every file is owned 65532:65532, and the subdir root is 0777. The reaper runs as 65532 with fsGroup 65532.

**These are disposable.** They are per-job staging copies of jobs whose orchestrators died in August. Their output was never imported, and their raw NFS volumes are velero-excluded. Reclaiming them is exactly the reaper's purpose, so no recovery copy is taken.

**Lifecycle safety, checked in the DEPLOYED images:**
- **Orchestrator.** The platform-dev orchestrator image (`orchestrator@sha256:ed8c…`, inspected in-pod) has `SandboxExecutor.run` call `create_lease`, then `_start_renewer`, then `self._fs.stage(…)`, in that order.
  - So a job dir is never visible without a Lease.
  - The renewer runs through stage, run, quiesce, import, apply-back and cleanup, and stops only in `finally`.
  - `_LeaseRenewer` handles `LeaseConflict` and marks the lease lost, so the importer aborts before apply-back once ownership is gone.
  - The reaper never lists `<id>.import`, the one unfenced dir.
  - The live Leases agree: each `renewTime` is 300–3601 s after its creation.
- **Reaper.** The reaper image (`p1-worker@sha256:3447…`, inspected in-pod) runs the same `reclaim`:
  - it CAS-acquires (create, or `acquire_lease` on the observed resourceVersion);
  - it foreground-deletes the Job, then waits until the Job and its Pods are absent;
  - it re-confirms its holder and resourceVersion, and only then runs rmtree and deletes the Lease.

  A 404 on `delete_job` is swallowed.
- The playground subdir being empty is itself evidence: the reaper reclaimed the earlier canary runs' subPath dirs on exactly this absent-Lease, absent-Job path.

**2. The boundary canary still uses the playground identity.** `scripts/verify-sandbox-boundary.sh` and the gated `egress-canary.yaml` and `test-egress-canary.yaml` label their pods `tenant-zero/playground/planner` and mount `af-sbx-ws-tenant-zero-playground`. The agent tier's positive control reaches a broker through the playground ingress rule, rule (1), which is still in all five broker CNPs.
- sandbox-guard requires the workspace PVC to be `af-sbx-ws-<org>-<workspace>`. A canary on platform-dev's labels would therefore mount platform-dev's live workspace, which is forbidden.
- The harness resolves the sandbox image from `af-tenant-tenant-zero-playground`. That namespace is gone, so it silently falls back to the committed `aa49b8…`, while platform-dev runs `ea5fe5…`.
- Its workspace allowlist can be bypassed with `AF_CANARY_DANGEROUS_OVERRIDE=yes`.

**3. Storage.** `reaper-storage.yaml` and `workspace-pvc.yaml`, the two playground PV/PVC pairs, can only go once 1 and 2 are done.

**Out of scope:**
- #1505 item 4, the seat templates. It is agentforge#420 item 1, handled in its own agentforge PR.
- The playground name reservation (tenant-guard rule 16 and tenant-reserved-guard). It stays.

## Approach

Three PRs, in order, each verified live before the next opens.

### PR-A: re-point the reaper, move the canary, replace the broker rule

1. **Reaper storage.** Add `platform-dev-reaper-storage.yaml`: a static PV and PVC `af-sbx-reaper-tenant-zero-platform-dev` in the `agentforge` namespace, at NFS `/pve-nfs/agentforge-sandbox/tenant-zero/platform-dev`.
   - Copy the playground pair's shape: Retain, `claimRef` pre-bind, `nfsvers=4.0,hard,nconnect=4`, the velero exclude label, `role: sandbox-reaper`.
   - List it. `reaper-storage.yaml` stays listed, for rollback.
2. **Reaper mount.** In `reaper-deployment.yaml`, set `claimName: af-sbx-reaper-tenant-zero-platform-dev`, and rewrite the header and mount comments. `Recreate` means the old pod exits first. Env is unchanged: `AF_SANDBOX_STAGING_ROOT` matches platform-dev's orchestrator. Document that one reaper reaps one workspace subdir.
3. **Canary identity:** org `tenant-zero`, workspace `boundary-canary`, pool `canary`. Every value is a dns-slug, so both sandbox guards admit it. The agent egress CNP is label-generic. No tenant namespace, orchestrator or seat exists for it, and tenant-guard does not govern these operator-created pods.
4. **Canary storage.** Add `boundary-canary-workspace-pvc.yaml`: a PV and PVC `af-sbx-ws-tenant-zero-boundary-canary` in `agentforge-sandbox`, at `/pve-nfs/agentforge-sandbox/tenant-zero/boundary-canary`, shaped like `platform-dev-workspace-pvc.yaml`.
   - Extend the parent-export mkdir Job's script (`platform-dev-nfs-provisioner-job.yaml`, which Flux re-runs about hourly after its 1 h TTL):
     - `mkdir -p` and `chmod 0777` for `platform-dev` and for `boundary-canary`;
     - **canary retention:** `rmdir` each direct child of the canary dir older than 120 minutes. `rmdir` is non-recursive, so it removes only empty canary subPath dirs (the canary writes nothing), and a non-empty one is logged and kept.
   - Keep the per-object force annotation. Recompute the content-addressed suffix; `check-inline-hashes.py` gates it.
5. **Broker CNPs.** Delete rule (1), playground/planner, from all five broker CNPs, and fix (1b)'s comment.
   - Add one canary rule to `broker-openai-codex.yaml` only. Its single `fromEndpoints` entry carries `k8s:io.kubernetes.pod.namespace: agentforge-sandbox`, `trust-class: agent`, `pool: canary`, `org: tenant-zero` and `workspace: boundary-canary`, on port 8700.
   - That broker's upstream login has been revoked since 2026-09-29, and it served 0 tokens in 14 days.
   - The canary holds no capability, so the rule grants reachability only, never authorization.
6. **Harness** (`scripts/verify-sandbox-boundary.sh`):
   - `WS` is fixed to `boundary-canary`, and any other value is refused. `AF_CANARY_DANGEROUS_OVERRIDE` is removed, so no platform-dev or other path exists.
   - `POOL=canary` replaces the hardcoded `planner`.
   - **Image:**
     - `AF_CANARY_IMAGE` if set.
     - Otherwise the `AF_SANDBOX_IMAGE` of the orchestrator Deployments in `af-tenant-*` namespaces. A kubectl error, a non-digest value or disagreeing images FAIL (exit 2).
     - Only an empty discovery falls back to the committed digest.
     - It prints the source: the namespace/Deployment names, or "committed fallback".
   - **Positive control:** a Ready, non-terminating pod IP of `AF_CANARY_BROKER` (default `broker-openai-codex`).
   - **New deny probe:** a Ready, non-terminating pod IP of `AF_CANARY_DENY_BROKER` (default `broker-anthropic-max1`), distinct from the positive IP. It is injected as `AF_CANARY_DENY_BROKER_IP`, and the agent tier must fail to reach it.
     - No such pod means FAIL.
     - After the agent canary finishes, the deny pod must still be Ready with the same IP, or the run is INCONCLUSIVE (FAIL).
     - A dedicated assertion needs the exact `ok (blocked): denied-broker-ip` line, so a missing line FAILS. The matrix becomes 24 checks.
   - **Test seams:** `AF_CANARY_KUBECTL`, and `AF_CANARY_PREFLIGHT_ONLY=1`, which stops after workspace, image and broker resolution and prints them.
7. **Gated manifests** (`egress-canary.yaml`, `test-egress-canary.yaml`, unlisted): set the canary identity and PVC. Mirror the new `AF_CANARY_DENY_BROKER_IP` input, probe and fail-closed rule in the agent manifest's script.
8. **Tests:** `scripts/tests/test_verify_sandbox_boundary.py`, which runs the script with a fake kubectl and `PREFLIGHT_ONLY`, and runs `bash -n`. Cases:
   - a non-canary workspace is refused before any kubectl call;
   - a discovery kubectl error exits 2;
   - disagreeing images exit 2;
   - a non-digest image exits 2;
   - an empty discovery takes the committed fallback, and says so;
   - a missing positive or deny broker pod gives no GREEN;
   - deny and positive resolving to the same IP is refused.
9. **Docs:**
   - `docs/runbooks/sandbox-boundary-canary.md`: the identity, the brokers, the image source, the new probe, retention.
   - ADR 0019's 2026-10-30 update.
   - The comments in `kustomization.yaml`.

**PR-A verification:**
- **Pre-merge:**
  - Refresh the per-ID inventory (the dirs, the Leases with holder and renewTime, Jobs and Pods) and save it.
  - Server-side dry-run of both gated manifests: they must be admitted.
  - Structural diff of each broker CNP's ingress, old against new: every rule other than the removed playground rule and the added canary rule is byte-identical, which preserves each broker's own platform-dev selectors and ports.
- **Post-merge:**
  - Flux `agentforge-sandbox` and `agentforge-broker` are Ready at the merge revision, and the PVs and PVCs are Bound.
  - The new mkdir Job is Completed, and its log shows both dirs at mode 777.
  - The five CNPs are updated, and Cilium raised no policy import errors in the agent logs.
  - The reaper pod is the new one, mounts the new PVC, and logs no errors.
  - **Protection test.** Create a fresh Lease `af-sbx-<test-id>` (holder `reaper-protect-test`, renewTime now, duration 900 s) and its dir, through the reaper pod's mount.
    - Wait 3 reaper passes, then assert that the dir and the Lease both survive.
    - Delete the Lease and assert the next passes reclaim the dir. That is the full cycle end-to-end.
  - The saved stale set (7 Leases, 24 dirs) is reclaimed, ID by ID. `.deps` and `.deps-staging` are intact.
  - platform-dev's orchestrator is Ready with no restarts.
  - The canary is GREEN 24/24, with the image source reported as platform-dev's orchestrator.
  - **Mutation run:** `AF_CANARY_DENY_BROKER=broker-openai-codex` (the admitted broker) must FAIL on the deny assertion.
  - No new alerts for 30 minutes.

### PR-B1: delete the playground PV/PVC manifests (only after PR-A is verified)

1. **Pre-check:**
   - No pod in any namespace references `af-sbx-reaper-tenant-zero-playground` or `af-sbx-ws-tenant-zero-playground`.
   - No controller (Deployment/Job template) references them.
   - A read-only inspection pod in `agentforge` mounts the parent-export PVC with `readOnly: true` and lists `/export/tenant-zero/playground`. A mount or read failure FAILS the pre-check, and is never read as "empty".
2. Delete `reaper-storage.yaml` and `workspace-pvc.yaml`, and unlist both. Docs: the ADR 0019 and runbook storage notes.
3. **Verify:**
   - Wait explicitly until both PVCs and both PVs are absent. If one is held, resolve the reference; never strip protection finalizers, and never set Kustomization-wide force.
   - The reaper and the canary are still green.

### PR-B2: remove the empty playground NFS subdir (only after PR-B1's prune is verified)

1. Add `P=/export/tenant-zero/playground; if [ -d "$P" ]; then rmdir "$P"; fi` to the mkdir Job script.
   - The path is fixed, and the `rmdir` is atomic and non-recursive.
   - A non-empty dir fails the Job (KubeJobFailed) instead of losing data. It is idempotent on the hourly re-runs.
   - Recompute the suffix.
2. **Verify:** the Job has Completed, and the read-only inspection pod shows the dir absent. Then close #1505.

## Critical files

- `kubernetes/apps/infrastructure/agentforge-sandbox/{kustomization,reaper-deployment,platform-dev-nfs-provisioner-job,egress-canary,test-egress-canary}.yaml` (edit)
- `kubernetes/apps/infrastructure/agentforge-sandbox/{platform-dev-reaper-storage,boundary-canary-workspace-pvc}.yaml` (new, PR-A)
- `kubernetes/apps/infrastructure/agentforge-sandbox/{reaper-storage,workspace-pvc}.yaml` (delete, PR-B1)
- `kubernetes/apps/infrastructure/agentforge-broker/broker-*.yaml` (five CNPs)
- `scripts/verify-sandbox-boundary.sh`, `scripts/tests/test_verify_sandbox_boundary.py` (new)
- `docs/runbooks/sandbox-boundary-canary.md`, `docs/decisions/0019-agentforge-v2-control-plane.md`

## Verification

Each PR is verified as in its section. CI also runs `scripts/check-inline-hashes.py`, the kubeconform/kustomize builds and the new harness tests.

**Rollback:**
- **PR-A:** revert. The reaper goes back to the playground PVC, which still exists until PR-B1, and the canary goes back to its identity and rule. A revert does not restore staging dirs the reaper already reclaimed; they were disposable, as above.
- **PR-B1:** re-add the manifests. If a surviving Retain PV is `Released` with the deleted PVC's UID in its `claimRef`, rebind it per the documented procedure, and only once the old claim and its consumers are gone.
- **PR-B2:** revert the rmdir, AND temporarily add `mkdir -p` and `chmod 0777` of `tenant-zero/playground` back to the Job script. A plain revert does not recreate the dir. Verify the dir exists before any later PR-A rollback switches consumers back.

<!-- codex-review-status: finalized -->
