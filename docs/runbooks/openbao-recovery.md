# Runbook — OpenBao privileged recovery & seal rotation (wipe + re-bootstrap ceremony)

**When to use:** (a) a policy/role must change that NO existing token can manage (the operator-
provisioner deliberately cannot rewrite its own policy); (b) the shamir seal must be rotated
(exposure); (c) raft/etcd loss. **OpenBao 2.5.5 disables BOTH `generate-root` AND `operator rekey`
(HTTP 405)** — this ceremony is the ONLY privileged path. Proven 2026-07-21, and twice on
2026-07-24 (~4 min each, zero broker restarts).

Context: `kubectl --context admin@ai`, ns `openbao`. The bootstrap Jobs run the **orchestrator
image** — its `provisioner/bootstrap.py` defines every policy/role/seed that re-materializes.

## Invariants that make the wipe safe
- ESO ExternalSecrets use `deletionPolicy: Retain` → every synced k8s Secret survives; brokers
  keep serving from their mounts and live-reload (no restarts).
- All operator KV values re-seed from SOPS (`openbao-operator-seeds`) — **EXCEPT the codex
  `auth.json`**, whose refresh chain rotates nightly (single-use refresh tokens). It MUST be
  rescued from the retained Secret after every wipe (step 5) or the chain dies at the next expiry.
- Tenant KV (`af/data/tenants/*`) is re-provisioned by the provisioner; expect a short worker-401
  window.

## Ceremony
1. **Repin FIRST** (or the gap you're fixing reproduces): the three bootstrap refs in
   `kubernetes/apps/infrastructure/security/openbao/{unseal-job,provision-job,provisioner-deploy}.yaml`
   must point at the orchestrator digest whose `bootstrap.py` contains the desired roles/policies.
   PR → approve → merge; reconcile the flux source (immutable-Job warnings are expected).
2. **Scale down + (optional) backup**: `kubectl -n openbao scale sts openbao --replicas=0` → wait
   pod gone. For a backup, mount PVC `data-openbao-0` in a throwaway busybox pod and tar `/data`
   — **extract with `exec ... -- sh -c 'cat file'` + local redirect, NEVER `kubectl cp` (MSYS path
   mangling produced a 0-byte "backup" once); verify the archive with `tar tzf` CHECKING ITS EXIT
   CODE, not a piped `head`'s.**
3. **Wipe + clear the bootstrap gates** (throwaway pod: `rm -rf /data/*`), then delete:
   Secrets `openbao-keys openbao-provisioner-token openbao-operator-provisioner-token
   openbao-canary`, ConfigMap `openbao-state`, Jobs `openbao-init openbao-provision`.
4. **Bring back**: scale sts to 1; annotate the `openbao` Flux Kustomization to reconcile → the
   Jobs recreate on the pinned image: `openbao-init` (fresh init = NEW unseal key + cluster_id,
   writes `openbao-keys`) then `openbao-provision` (mount, k8s-auth, ALL policies/roles, seeds,
   `cas_required` stamps, tokens). Wait both `succeeded=1` (provision may retry 1-2×).
5. **Rescue the codex auth.json** (ALWAYS): stage the retained value + the new provisioner token
   into the pod via `exec -i -- sh -c 'cat > /tmp/f'` (NOT stdin `read` — dies silently under
   MSYS), then in-pod:
   `bao kv put -cas=<current_version|0> -mount=af operator/broker/openai/codex-pro/oauth auth.json=@/tmp/f`
   and re-stamp `bao kv metadata put -cas-required=true ...`. Verify the round-trip sha equals the
   codex broker's `/readyz` `credential_generation`, then force-annotate the
   `broker-openai-codex-oauth` ExternalSecret and confirm `SecretSynced=True`.
6. **Re-escrow the NEW seal** in `kubernetes/infra/openbao-unseal.sops.yaml`. The file MUST stay a
   `kind: Secret` manifest with `stringData:` — the SOPS creation rules encrypt ONLY
   Secret-shaped keys; a flat yaml passes through **PLAINTEXT** while sops still adds
   metadata+mac (this caused a real 2026-07-24 leak → seal rotation). Before pushing, assert BOTH
   values start with `ENC[` AND the decrypt round-trip hash matches the live Secret.
7. **Verify**: role probe matrix (positive + negative per role), `cas_required` on all broker
   oauth keys, all ExternalSecrets `SecretSynced`, broker generations unchanged, a manual
   `af-codex-refresh` Job succeeds and stamps the status custom-metadata.

## If a pushed secret leaks anyway
Delete the branch → in the gitea pod
`cd /data/git/gitea-repositories/<org>/<repo>.git && git gc --prune=now` → verify
`git cat-file -e <sha>` fails → **rotate the exposed secret regardless** (for the seal: this
ceremony, steps 2–6).
