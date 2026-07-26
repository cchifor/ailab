# Runbook — OpenBao privileged recovery & seal rotation (wipe + re-bootstrap ceremony)

**When to use:** (a) a policy/role must change that NO existing token can manage (the operator-
provisioner deliberately cannot rewrite its own policy); (b) the shamir seal must be rotated
(exposure); (c) raft/etcd loss. **OpenBao 2.5.5 disables BOTH `generate-root` AND `operator rekey`
(HTTP 405)** — this ceremony is the ONLY privileged path. Proven 2026-07-21, and twice on
2026-07-24 (~4 min each, zero broker restarts).

Context: `kubectl --context admin@ai`, ns `openbao`. The bootstrap Jobs run the **orchestrator
image** — its `provisioner/bootstrap.py` defines every policy/role/seed that re-materializes.

## What survives a wipe (the corrected invariant)

> **A wipe restores ONLY the KV paths the SOPS `seeds.json` blob declares**, plus the structure the
> provision Job rebuilds (mounts, policies, roles, the `operator/canary` sentinel). The old belief that
> *"all operator KV re-seeds from SOPS except the codex `auth.json`"* was **FALSE** and cost a ~2-day
> outage on 2026-07-24 (9 ExternalSecrets `SecretSyncedError`, KV returning 404). Everything not in
> `seeds.json` survives **only** because ESO's default `deletionPolicy: Retain` keeps the last-good target
> k8s Secret — the value is gone from the vault and must be re-seeded or re-provisioned by hand.

- **ESO Retain** → every synced k8s Secret survives the wipe; brokers keep serving from their mounts and
  live-reload (no restarts).
- **The seed contract.** `provisioner/bootstrap.py::_apply_operator_seeds` merge-writes (add-only, **no
  `options.cas`**) each entry of the decrypted `openbao-operator-seeds` blob (`operator-seeds.sops.yaml`,
  `kind: Secret`, single encrypted `stringData.seeds.json`, a flat `{ "<path>": { "<field>": "<value>" } }`)
  to `af/data/<logical_path>`. **Hard constraint — seed only NON-CAS paths:** the broker `oauth` paths are
  `cas_required=true` (contract C3) and a no-CAS seed write would **fail closed**. Never add a `.../oauth`
  path (nor `.../kids` or `tenants/*`) to `seeds.json` unless `bootstrap.py` is first changed to CAS-write.

### Post-wipe handling, by path class

| Class | Paths | Post-wipe action |
|---|---|---|
| **SEED** — self-heals from `seeds.json` | `operator/dispatcher/forge` (`AF_BOT_TOKEN_PLANNER`), `operator/reaper/ledger` (`AF_REAPER_LEDGER_DSN`), `operator/broker/{anthropic/claude-max-1,anthropic/claude-max-2,openai/codex-pro}/ledger` (all three `AF_BROKER_LEDGER_DSN`, one shared `agentforge_broker` DB on infra-pg), `operator/ci/runner-registration` (`token`), `operator/ci/scaler-token` (`token`) | **Nothing** — re-provision restores them. (The 5 broker-ledger + ci paths were added to `seeds.json` on 2026-07-26; before that they were RESCUE.) |
| **RESCUE** — from the Retain'd Secret | `operator/broker/{anthropic/claude-max-1,anthropic/claude-max-2,openai/codex-pro}/oauth` | Re-seed from the Retain'd k8s Secret via the recipe in step 5. **Cannot be seeded** (`cas_required=true`) unless `bootstrap.py` is changed to CAS-write. Codex (`codex-pro/oauth`) **also rotates nightly** (single-use refresh token) so it stays a rescue regardless. |
| **RE-PROVISION** — by owner | `operator/broker/*/kids` (registry.json — public key registry), `tenants/<org>/<ws>/orchestrator` (bot PATs + capability signing key) | `kids` via the broker **keypair lifecycle**; `tenants/*` via the **control plane** — not by re-seeding. Expect a short worker-401 window. |

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
5. **Re-seed the RESCUE-class paths** — all three broker `oauth` (the SEED-class ledger/ci/dispatcher/
   reaper paths self-heal from `seeds.json` and need nothing; the RE-PROVISION-class paths are owner-
   restored). For each `oauth`: read its Retain'd k8s Secret, stage the value + the new provisioner token
   into the pod via `exec -i -- sh -c 'cat > /tmp/f'` (NOT stdin `read` — dies silently under MSYS),
   sha-verify pod==source, then in-pod with the durable operator-provisioner token (Secret
   `openbao-operator-provisioner-token`; the pod listens HTTPS so use `BAO_ADDR=https://127.0.0.1:8200
   BAO_SKIP_VERIFY=true` — MSYS `curl` is Schannel and `--cacert` fails exit 60):
   `bao kv put -cas=<current_version|0> -mount=af operator/broker/<broker>/oauth auth.json=@/tmp/f`
   then re-stamp `bao kv metadata put -cas-required=true ...`. Verify the read-back sha equals the
   broker's `/readyz` `credential_generation` (:8700), force-annotate the matching `broker-*-oauth`
   ExternalSecret (`force-sync="$(date +%s)"`), confirm `SecretSynced=True`, and delete `/tmp/f`.
   **The `bao kv put` is classifier-BLOCKED (shared-infra mutation) — the OPERATOR runs the ready
   script.** Codex (`codex-pro/oauth`) additionally rotates nightly, so rescue it every wipe or the
   refresh chain dies at the next expiry.
6. **Re-escrow the NEW seal** in `kubernetes/infra/openbao-unseal.sops.yaml`. The file MUST stay a
   `kind: Secret` manifest with `stringData:` — the SOPS creation rules encrypt ONLY
   Secret-shaped keys; a flat yaml passes through **PLAINTEXT** while sops still adds
   metadata+mac (this caused a real 2026-07-24 leak → seal rotation). Before pushing, assert BOTH
   values start with `ENC[` AND the decrypt round-trip hash matches the live Secret.
7. **Verify**: role probe matrix (positive + negative per role), `cas_required` on all broker
   oauth keys, all ExternalSecrets `SecretSynced`, broker generations unchanged, a manual
   `af-codex-refresh` Job succeeds and stamps the status custom-metadata.

## Adding a SEED path (making a static value self-heal)

Only static, **non-CAS** infra values qualify (never `.../oauth`, `.../kids`, or `tenants/*` — see the
hard constraint above). Never hand-edit ciphertext:

1. `export SOPS_AGE_KEY_FILE="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out/age.agekey"`
   — the key lives in the **main** checkout (`_out/` is gitignored, so it is absent from a worktree).
2. `sops -d operator-seeds.sops.yaml` → parse `stringData.seeds.json` → add the entries (preserve the
   existing ones) → re-serialize **compact** (`json.dumps(obj, separators=(',',':'))`, no trailing newline).
3. Overwrite the file with the plaintext `kind: Secret` manifest and re-encrypt **in place**:
   `sops -e -i operator-seeds.sops.yaml` (the generic `.*\.sops\.ya?ml$` rule applies
   `^(data|stringData)$` + the age recipient).
4. **Pre-push verification (SOPS shape trap — caused a real 2026-07-24 leak):** the creation rules encrypt
   ONLY Secret-shaped keys, so a flat YAML would pass through **PLAINTEXT** while sops still adds
   metadata+mac (a naive `grep -c 'ENC\['` and a decrypt round-trip both still look "fine"). Assert
   **every** value line is `ENC[...]` (`grep -E '^ +seeds\.json: ENC\['`); decrypt round-trip and
   `json.loads` the blob → assert the exact expected path set + cross-check each value's `sha256[:16]`
   against its live source Secret (**never print the value** — shas only); `git diff` must touch only the
   `.sops.yaml` (+ this doc) with no plaintext.

## If a pushed secret leaks anyway
Delete the branch → in the gitea pod
`cd /data/git/gitea-repositories/<org>/<repo>.git && git gc --prune=now` → verify
`git cat-file -e <sha>` fails → **rotate the exposed secret regardless** (for the seal: this
ceremony, steps 2–6).
