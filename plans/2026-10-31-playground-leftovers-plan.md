# Playground leftovers (#1505): re-point the reaper, move the boundary canary, delete the playground storage

## Codex Review

- The quoted reaper CAS and deletion sequence is sound for protected jobs, but activation needs evidence that Lease protection covers staging through final import, plus verification that active staging survives.
- `boundary-canary` / `canary` satisfies both sandbox guards; existing agent egress permits broker pod IPs, and tenant-guard does not govern these operator-created canaries. A canary rule on one broker is sound with the namespace selector retained.
- Fix the harness’s existing workspace-override bypass and make the new deny probe fail closed on missing or unhealthy endpoints and missing probe evidence.
- The content-addressed mkdir Job and its existing per-object force annotation are appropriate. Preserve both; `wait: false` means Job execution, storage binding and pruning need explicit verification and ordering.
- `Retain` and `rmdir` protect existing file contents, but neither enforces detachment ordering. Correct PR-B’s directory-recreation rollback and account for retained PV claim UIDs after a partial prune.

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

<!-- codex: Before switching the mount, establish that the deployed orchestrator acquires its Lease before exposing a discoverable staging directory, renews throughout staging/run/quiesce/import, and stops using the directory on loss of ownership; orchestrator-rbac.yaml documents this contract, but the quoted renewal observations establish only part of it. A live staging or import directory without a valid Lease can follow the same absent-Job reclamation path, so CAS correctness alone does not establish lifecycle safety. -->

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

   <!-- codex: Moving the reaper to platform-dev removes the collector that currently removes canary subPath directories; the harness teardown deletes Jobs only, and Job TTL does not delete NFS directories. Specify cleanup of each run's exact disposable directories after its Pods are gone, or document a bounded retention procedure without adding another reaper or widening the production reaper's mount. -->

5. **Broker CNPs.** Delete rule (1), playground/planner, from all five broker CNPs, and fix (1b)'s "additive to the playground admission" comment.
   - Add one canary rule (`trust-class: agent`, `pool: canary`, `org: tenant-zero`, `workspace: boundary-canary`, port 8700) to `broker-openai-codex.yaml` only.

   <!-- codex: Include `k8s:io.kubernetes.pod.namespace: agentforge-sandbox` in the same fromEndpoints matchLabels entry, as every existing sandbox ingress rule does. The four application labels alone do not restrict admission to the namespace where sandbox-guard enforces the pod boundary. -->

   - That broker's upstream login has been revoked since 2026-09-29 and it served 0 tokens in 14 days.
   - The canary holds no capability, so the rule grants reachability only, never authorization. One broker instead of five keeps the surface minimal.
6. **Harness** (`scripts/verify-sandbox-boundary.sh`):
   - `WS` defaults to `boundary-canary`, and the allowlist is `boundary-canary` only, since the playground PVC is going. The platform-dev refusal stays.

   <!-- codex: The current harness has no unconditional platform-dev refusal: `AF_CANARY_DANGEROUS_OVERRIDE=yes` bypasses its workspace allowlist for any name. Remove that bypass for this dedicated canary, or otherwise enforce the stated hard refusal before resolving or mounting a workspace PVC. -->

   - Add a `POOL` variable (`canary`) in place of the hardcoded `planner`.
   - Image: `AF_CANARY_IMAGE` if set. Otherwise the `AF_SANDBOX_IMAGE` of the live orchestrator Deployments in `af-tenant-*` namespaces, read-only, failing if they disagree. The committed digest is the last resort, and the harness says which source it used.

   <!-- codex: Distinguish a successful discovery with no eligible orchestrators from an API/RBAC error or malformed image configuration; those errors must fail instead of selecting the committed fallback. Validate the selected image as digest-pinned and report the contributing namespace/Deployment names so the claimed live-image coverage is auditable. -->

   - Positive control: a Running and Ready pod IP of `AF_CANARY_BROKER` (default `broker-openai-codex`), selected by `app.kubernetes.io/name`.
   - **New deny probe:** a Running pod IP of a broker that does NOT admit the canary (default `broker-anthropic-max1`). The agent tier must fail to reach it, which proves broker ingress admits only the matching identity, not just "no DNS name". The matrix becomes 24 checks.

   <!-- codex: Running alone does not establish a listening broker, so an unhealthy or terminating target could falsely pass this denial check. Require a Ready, non-terminating broker with a valid pod IP distinct from the positive target, verify its health across the probe, and treat missing endpoints or target churn as inconclusive. -->

   <!-- codex: The harness currently counts all agent deny targets as one check, so adding a curl invocation alone leaves the total at 23 and does not prove the new probe ran. Add a dedicated assertion for the exact denied broker-IP evidence and fail on a missing input or missing evidence, as the existing test-tier broker-IP discriminator does. -->

7. **Gated manifests** (`egress-canary.yaml`, `test-egress-canary.yaml`, unlisted): set the labels and PVC to the canary identity, and update their headers.

   <!-- codex: Updating only labels/PVCs leaves the agent manifest's probe script inconsistent with the new harness matrix; mirror the new deny-IP input, probe and fail-closed behavior there. Validate both unlisted manifests explicitly, including server-side admission dry-runs, because the normal kustomize build does not include them. -->

8. **Docs:**
   - `docs/runbooks/sandbox-boundary-canary.md`: the identity, the broker, the image resolution and the new probe.
   - ADR 0019's 2026-10-30 update: the canary and the reaper are no longer "kept on purpose".
   - The comments in `kustomization.yaml`.

**PR-A verification:**
- Flux `agentforge-sandbox` and `agentforge-broker` are Ready at the merge revision, and the new PVs and PVCs are Bound.
- The new mkdir Job is Completed and its log shows both subdirs at mode 777.
- The reaper pod has been replaced and mounts the new PVC.
- Within about 5 passes (60 s interval): the seven Leases are gone, the 25 job dirs are gone, and `.deps` and `.deps-staging` are intact, checked read-only from the platform-dev orchestrator pod.

<!-- codex: Refresh and record the per-ID directory/Lease/Job/Pod inventory immediately before activation, then verify removal of that identified stale set rather than treating the dated count as a current inventory. Also verify that a job with a renewing Lease retains its staging directory across reaper passes and through import; successful orphan deletion alone does not test protection of live work. -->

- The reaper has no errors or restarts, and platform-dev's orchestrator is unaffected (Ready, no restarts).

<!-- codex: Ready/no restarts does not demonstrate that the five broker policy edits preserve platform-dev's access, and a successful canary on its own exception cannot establish that either. Compare the effective policies against the existing per-broker platform-dev selectors and ports, preserving their current differences, and verify the production broker path through existing workload evidence without mounting production storage in the canary. -->

- `scripts/verify-sandbox-boundary.sh` is GREEN 24/24 and reports the image source as platform-dev's orchestrator.
- No new alerts for 30 minutes.

### PR-B: delete the playground storage (only after PR-A is verified)

1. **Pre-check (live):** no pod mounts `af-sbx-reaper-tenant-zero-playground` or `af-sbx-ws-tenant-zero-playground` (`kubectl get pods -A -o json` over `.spec.volumes[].persistentVolumeClaim`), and the playground subdir is still empty (read through the parent-export mkdir Job or a reaper exec).

   <!-- codex: After PR-A the reaper mounts platform-dev and cannot inspect the sibling playground directory, while a Completed or TTL-deleted mkdir Job is not an executable inspection target. Specify a read-only inspection using the parent-export PVC and treat an unavailable mount or failed read as a failed pre-check, never as an empty directory. -->

2. Delete `reaper-storage.yaml` and `workspace-pvc.yaml`, and unlist both. Flux prunes the PVCs and PVs. With Retain, deleting the PV never touches the NFS data.

   <!-- codex: Explicitly wait for both PVCs and both PVs to disappear: PVC/PV protection can leave deletions pending while references remain, including terminating Pods. Recheck consumers and any controllers that could recreate them, and resolve references instead of stripping protection finalizers or enabling Kustomization-wide force. -->

3. **Remove the empty NFS subdir from the same mkdir Job:** `if [ -d "$P" ]; then rmdir "$P"; fi`. `rmdir` refuses a non-empty dir, so under `set -e` a non-empty playground dir fails the Job (KubeJobFailed) instead of losing data. Recompute the suffix.

   <!-- codex: The new Job can execute before pruning finishes because this Flux Kustomization uses wait:false; numbered steps in one commit do not enforce detachment, and its TTL also makes the cleanup recur when Flux recreates the Job. Keep the atomic, non-recursive rmdir with P explicitly fixed to /export/tenant-zero/playground, but enable it only after a separate verified prune gate: Retain and Kubernetes protection finalizers do not prevent directory removal through the parent-export mount. -->

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

<!-- codex: These gates do not exercise the modified shell harness's discovery and failure handling. Add bash syntax validation and focused offline cases for forbidden workspace overrides, image-discovery errors/disagreement, unavailable broker endpoints, and missing deny-probe evidence so those failures cannot produce GREEN. -->

**Rollback:**
- PR-A is a revert: the reaper goes back to the playground PVC, which still exists until PR-B, and the canary goes back to the playground identity and rule.

<!-- codex: This restores configuration, not directories already removed by the reaper; the raw NFS volumes are explicitly excluded from Velero backup. State that distinction and classify the stale contents as disposable before activation, or capture a recovery copy first if their recovery is required. -->

- PR-B's rollback re-adds the manifests, and the mkdir Job recreates the subdir.

<!-- codex: Reverting PR-B restores PR-A's mkdir script, which creates only platform-dev and boundary-canary, so it does not recreate playground. Add an explicit rollback action to recreate /export/tenant-zero/playground with mode 0777 and verify the restored storage before any later PR-A rollback switches consumers back. -->

<!-- codex: A rollback during a partial prune may encounter a surviving Retain PV in Released state whose claimRef still contains the deleted PVC's UID, preventing the replacement claim from binding. Inspect that state and use the documented rebind procedure only after the old claim and consumers are gone; fully deleted and recreated PV/PVC pairs do not need this repair. -->

<!-- codex-review-status: complete -->