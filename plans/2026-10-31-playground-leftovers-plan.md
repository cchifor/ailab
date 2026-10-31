# Playground leftovers (#1505): re-point the reaper, move the boundary canary, delete the playground storage

## Context

The playground retirement (#1503, `plans/2026-10-30-retire-playground-plan.md`) kept three things on purpose. #1505 tracks them.

**1. The reaper still mounts the playground staging subdir.** `KubernetesReaperBackend.list_job_ids` lists 32-char alnum dirs under its one staging mount. That mount is `af-sbx-reaper-tenant-zero-playground` (NFS `/pve-nfs/agentforge-sandbox/tenant-zero/playground`), which is now empty. So platform-dev has never been reaped. Measured 2026-10-31:
- `tenant-zero/platform-dev` staging holds 25 job dirs, all 74–87 days old, about 416 MB in total (six of them 55–64 MB).
- `.deps` and `.deps-staging` are not 32-char alnum, so the reaper never lists them.
- Seven of those dirs still have `af-sbx-<id>` Leases. Their holders are dead `af-orch-platform-dev-delivery-*` pods, and each renewTime is from 2026-08-16/17 with a 300 s duration, so all seven are expired. The other 18 dirs have no Lease.
- There are no Jobs or Pods in `agentforge-sandbox`.
- Every file is owned 65532:65532, and the subdir root is 0777. The reaper runs as 65532 with fsGroup 65532.

**Precondition from #1505: does platform-dev's orchestrator CAS-renew its Leases?** Yes:
- Each of the seven Leases has `renewTime` 300–3601 s after its `creationTimestamp`, so it was renewed while its job ran.
- In code, `sandbox.py` `_LeaseRenewer` calls `renew_lease(snapshot)` carrying the observed `resourceVersion`.
- The running reaper image (`p1-worker@sha256:3447…`) has the same `reclaim` as main, checked in-pod:
  - it CAS-acquires (create, or `acquire_lease` on the observed resourceVersion);
  - it foreground-deletes the Job, then waits until the Job and its Pods are absent;
  - it re-confirms its holder and resourceVersion, and only then runs rmtree and deletes the Lease.

  An absent Job is a 404 that `delete_job` swallows.
- The playground subdir being empty is itself evidence: the reaper reaped the earlier canary runs' subPath dirs (`agent…`, `testc…`, each 32-char alnum) on exactly this absent-Lease, absent-Job path.

**2. The boundary canary still uses the playground identity.** `scripts/verify-sandbox-boundary.sh` and the gated `egress-canary.yaml` and `test-egress-canary.yaml` label their pods `tenant-zero/playground/planner` and mount `af-sbx-ws-tenant-zero-playground`. The agent tier's positive control reaches a broker through the playground ingress rule, rule (1), which is still in all five broker CNPs.
- The sandbox-guard VAP requires the workspace PVC to be `af-sbx-ws-<org>-<workspace>`. A canary on platform-dev's labels would therefore mount platform-dev's live workspace, which is forbidden.
- The harness also resolves the sandbox image from `af-tenant-tenant-zero-playground`. That namespace is gone, so it silently falls back to the committed digest `aa49b8…`, while platform-dev runs `ea5fe5…`. The 23/23 run on 2026-10-30 tested the stale image.

**3. Storage.** `reaper-storage.yaml` and `workspace-pvc.yaml`, the two playground PV/PVC pairs, can only go once 1 and 2 are done.

**Out of scope:**
- #1505 item 4, the seat templates. It is agentforge#420 item 1, handled in its own agentforge PR.
- The `tenant-guard` rule 16 and `tenant-reserved-guard` reservation of the playground name. It stays.

## Approach

Two PRs, in order. PR-B opens only after PR-A is verified live, so PR-A's rollback (revert, which points the reaper back at the playground PVC) stays possible until then.

### PR-A: re-point the reaper, move the canary, replace the broker rule

1. **Reaper storage.** Add `platform-dev-reaper-storage.yaml`: a static PV and PVC `af-sbx-reaper-tenant-zero-platform-dev` in the `agentforge` namespace, at NFS `/pve-nfs/agentforge-sandbox/tenant-zero/platform-dev`.
   - Copy the playground pair's shape: Retain, `claimRef` pre-bind, `nfsvers=4.0,hard,nconnect=4`, the velero exclude label, `role: sandbox-reaper`.
   - List it in `kustomization.yaml`. Keep `reaper-storage.yaml` listed for now.
2. **Reaper mount.** In `reaper-deployment.yaml`, set `claimName: af-sbx-reaper-tenant-zero-platform-dev`, and rewrite the header and mount comments.
   - `strategy: Recreate` means the old pod exits before the new one mounts.
   - Env is unchanged: `AF_SANDBOX_STAGING_ROOT=/workspace/agentforge/sandbox` matches platform-dev's orchestrator.
   - Document the limit: one reaper reaps one workspace subdir. A second workspace needs a second reaper or a multi-root reaper.
3. **Canary identity:** org `tenant-zero`, workspace `boundary-canary`, pool `canary`. Every value is a dns-slug, so sandbox-guard admits it. No tenant namespace, orchestrator or seat exists for it.
4. **Canary storage.** Add `boundary-canary-workspace-pvc.yaml`: a PV and PVC `af-sbx-ws-tenant-zero-boundary-canary` in `agentforge-sandbox`, at NFS `/pve-nfs/agentforge-sandbox/tenant-zero/boundary-canary`, shaped like `platform-dev-workspace-pvc.yaml`.
   - The subdir is created by extending the existing parent-export mkdir Job (`platform-dev-nfs-provisioner-job.yaml`). Its script loops over `platform-dev` and `boundary-canary`, running `mkdir -p` and `chmod 0777` for each.
   - Recompute the content-addressed suffix. `scripts/check-inline-hashes.py` already gates it, and Flux prunes the superseded Job.
5. **Broker CNPs.** Delete rule (1), playground/planner, from all five broker CNPs, and fix (1b)'s "additive to the playground admission" comment.
   - Add one canary rule (`trust-class: agent`, `pool: canary`, `org: tenant-zero`, `workspace: boundary-canary`, port 8700) to `broker-openai-codex.yaml` only.
   - That broker's upstream login has been revoked since 2026-09-29 and it served 0 tokens in 14 days.
   - The canary holds no capability, so the rule grants reachability only, never authorization. One broker instead of five keeps the surface minimal.
6. **Harness** (`scripts/verify-sandbox-boundary.sh`):
   - `WS` defaults to `boundary-canary`, and the allowlist is `boundary-canary` only, since the playground PVC is going. The platform-dev refusal stays.
   - Add a `POOL` variable (`canary`) in place of the hardcoded `planner`.
   - Image: `AF_CANARY_IMAGE` if set. Otherwise the `AF_SANDBOX_IMAGE` of the live orchestrator Deployments in `af-tenant-*` namespaces, read-only, failing if they disagree. The committed digest is the last resort, and the harness says which source it used.
   - Positive control: a Running and Ready pod IP of `AF_CANARY_BROKER` (default `broker-openai-codex`), selected by `app.kubernetes.io/name`.
   - **New deny probe:** a Running pod IP of a broker that does NOT admit the canary (default `broker-anthropic-max1`). The agent tier must fail to reach it, which proves broker ingress admits only the matching identity, not just "no DNS name". The matrix becomes 24 checks.
7. **Gated manifests** (`egress-canary.yaml`, `test-egress-canary.yaml`, unlisted): set the labels and PVC to the canary identity, and update their headers.
8. **Docs:**
   - `docs/runbooks/sandbox-boundary-canary.md`: the identity, the broker, the image resolution and the new probe.
   - ADR 0019's 2026-10-30 update: the canary and the reaper are no longer "kept on purpose".
   - The comments in `kustomization.yaml`.

**PR-A verification:**
- Flux `agentforge-sandbox` and `agentforge-broker` are Ready at the merge revision, and the new PVs and PVCs are Bound.
- The new mkdir Job is Completed and its log shows both subdirs at mode 777.
- The reaper pod has been replaced and mounts the new PVC.
- Within about 5 passes (60 s interval): the seven Leases are gone, the 25 job dirs are gone, and `.deps` and `.deps-staging` are intact, checked read-only from the platform-dev orchestrator pod.
- The reaper has no errors or restarts, and platform-dev's orchestrator is unaffected (Ready, no restarts).
- `scripts/verify-sandbox-boundary.sh` is GREEN 24/24 and reports the image source as platform-dev's orchestrator.
- No new alerts for 30 minutes.

### PR-B: delete the playground storage (only after PR-A is verified)

1. **Pre-check (live):** no pod mounts `af-sbx-reaper-tenant-zero-playground` or `af-sbx-ws-tenant-zero-playground` (`kubectl get pods -A -o json` over `.spec.volumes[].persistentVolumeClaim`), and the playground subdir is still empty (read through the parent-export mkdir Job or a reaper exec).
2. Delete `reaper-storage.yaml` and `workspace-pvc.yaml`, and unlist both. Flux prunes the PVCs and PVs. With Retain, deleting the PV never touches the NFS data.
3. **Remove the empty NFS subdir from the same mkdir Job:** `if [ -d "$P" ]; then rmdir "$P"; fi`. `rmdir` refuses a non-empty dir, so under `set -e` a non-empty playground dir fails the Job (KubeJobFailed) instead of losing data. Recompute the suffix.
4. Docs: drop the remaining "kept on purpose" storage notes (ADR 0019, the runbook) and close #1505.

**PR-B verification:** both PVs and PVCs are gone, the Job has Completed, the subdir is absent, and the reaper and canary are still green. Run the canary again.

## Critical files

- `kubernetes/apps/infrastructure/agentforge-sandbox/{kustomization,reaper-deployment,platform-dev-nfs-provisioner-job,egress-canary,test-egress-canary}.yaml` (edit)
- `kubernetes/apps/infrastructure/agentforge-sandbox/{platform-dev-reaper-storage,boundary-canary-workspace-pvc}.yaml` (new, PR-A)
- `kubernetes/apps/infrastructure/agentforge-sandbox/{reaper-storage,workspace-pvc}.yaml` (delete, PR-B)
- `kubernetes/apps/infrastructure/agentforge-broker/broker-*.yaml` (five CNPs)
- `scripts/verify-sandbox-boundary.sh`
- `docs/runbooks/sandbox-boundary-canary.md`, `docs/decisions/0019-agentforge-v2-control-plane.md`

## Verification

Each PR is verified as in its section. CI also runs `scripts/check-inline-hashes.py`, kubeconform and the kustomize builds.

**Rollback:**
- PR-A is a revert: the reaper goes back to the playground PVC, which still exists until PR-B, and the canary goes back to the playground identity and rule.
- PR-B's rollback re-adds the manifests, and the mkdir Job recreates the subdir.

<!-- codex-review-status: pending -->
