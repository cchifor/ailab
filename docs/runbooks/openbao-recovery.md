# Runbook — OpenBao privileged recovery & seal rotation (wipe + re-bootstrap ceremony)

**When to use:** (a) a policy/role must change that NO existing token can manage (the operator-
provisioner deliberately cannot rewrite its own policy); (b) the shamir seal must be rotated
(exposure); (c) raft/etcd loss. **OpenBao 2.5.5 disables BOTH `generate-root` AND `operator rekey`
(HTTP 405)** — this ceremony is the ONLY privileged path. Proven 2026-07-21, and twice on
2026-07-24 (~4 min each, zero broker restarts).

Context: `kubectl --context admin@ai`, ns `openbao`. The bootstrap Jobs run the **orchestrator
image** — its `provisioner/bootstrap.py` defines every policy/role/seed that re-materializes.

## The seed-ownership contract (canonical — every other file points here)

**Seeds are a bootstrap FLOOR, not a mirror.** Stated once, here; nowhere else in this repo should
restate it, only link to it.

1. **Empty path → the seed installs the value.** That is what makes a wipe self-heal, and it is the
   whole job of seeding. Against an *empty* vault every seeder in the estate behaves identically, so
   nothing in the ceremony below depends on the rules that follow.
2. **Live path → the live vault wins, per key.** `_apply_operator_seeds` is **create-if-absent**: a
   key already present in the KV document keeps its live value, and a seed value that differs is
   **reported as drift**, never silently resolved. (It used to be `{**existing, **values}` —
   seed-wins — which is how a stale `tenants/*` fragment reverted a corrected credential on every
   provision run and took down two worker pools plus the dispatcher.)
3. **The document is never replaced.** Every seeder here is a read-modify-write, never a KV-v2 full
   replace, so **any key the seed does not mention survives untouched** — unconditionally, and
   independently of (2). This is what protects `AF_CAPABILITY_SIGNING_KEY` / `AF_CAPABILITY_KID`,
   which the keypair lifecycle mirrors into the same tenant document. "Add-only" named *only* this
   guarantee; it never named precedence.

**Rule (2) is the OPERATOR seed only.** The estate runs three independent seeders and they do not
share a precedence — check which one owns a path before reasoning about it:

| Seeder | Engine | Precedence on a key that is already LIVE |
|---|---|---|
| `operator-seeds.sops.yaml` → `_apply_operator_seeds` (Job `openbao-provision`, orchestrator image) | Python, **create-if-absent** | **live vault wins**; divergence is reported as drift |
| `devworker-seeds.sops.yaml` → Job `openbao-devworker-provision` | shell `bao kv patch` | **seed wins** — overwrites live KV on every daily run |
| `estate-seeds.sops.yaml` → Job `openbao-estate-provision` | shell `bao kv patch` | **seed wins** — overwrites live KV on every daily run |

**Rotation: refresh the seeds file in the same change, for all three — but for different reasons.**

- *operator seeds*: the seed can no longer revert your rotation, but it is still the **DR floor**. A
  stale fragment restores a **revoked** credential after a wipe, and reports as drift on every
  provision run until it is refreshed. `agentforge-bootstrap --seeds-out` (requires
  `--write-openbao`) emits the fragment; that command **rotates on every invocation**.
- *dev-worker / estate seeds*: unchanged and literal — a vault-only rotation is overwritten back to
  the old value within a day.

**What this means for the path taxonomy below.** The classes answer *"what restores this path after a
wipe"* — **not** *"who may write it while the estate is running"*. A path can be both **seeded** (so
it comes back) and **owner-managed** (so the seed can never overwrite the owner's value);
`tenants/<org>/<ws>/orchestrator` is exactly that. Collapsing the two questions into one is what made
this runbook and `provision-job.yaml` contradict each other for months.

## What survives a wipe (the corrected invariant)

> **A wipe restores ONLY the KV paths the SOPS `seeds.json` blob declares**, plus the structure the
> provision Job rebuilds (mounts, policies, roles, the `operator/canary` sentinel) — and, since ADR
> 0020, whatever the SEPARATE `openbao-devworker-provision` and `openbao-estate-provision` Jobs
> rebuild from their own `devworker-seeds.sops.yaml` / `estate-seeds.sops.yaml` (see those rows
> below; both Jobs are on their own daily schedule, so their half of the restore can lag by up to a
> day unless you force it). The old belief that
> *"all operator KV re-seeds from SOPS except the codex `auth.json`"* was **FALSE** and cost a ~2-day
> outage on 2026-07-24 (9 ExternalSecrets `SecretSyncedError`, KV returning 404). Everything not in
> `seeds.json` survives **only** because ESO's default `deletionPolicy: Retain` keeps the last-good target
> k8s Secret — the value is gone from the vault and must be re-seeded or re-provisioned by hand.

- **ESO Retain** → every synced k8s Secret survives the wipe; brokers keep serving from their mounts and
  live-reload (no restarts).
- **The seed contract.** `provisioner/bootstrap.py::_apply_operator_seeds` merge-writes
  (**create-if-absent**, **no `options.cas`** — see the contract above) each entry of the decrypted
  `openbao-operator-seeds` blob (`operator-seeds.sops.yaml`, `kind: Secret`, single encrypted
  `stringData.seeds.json`, a flat `{ "<path>": { "<field>": "<value>" } }`) to `af/data/<logical_path>`.
  **Hard constraint — seed only NON-CAS paths:** the broker `oauth` paths are `cas_required=true`
  (contract C3) and a no-CAS seed write would **fail closed** (seeding an oauth path would first require
  `bootstrap.py` to CAS-write). `operator/broker/*/kids` must **NEVER** be seeded regardless of CAS — the
  `registry.json` it holds is rebuilt by the broker keypair lifecycle, so a floor entry would only pin a
  stale public-key registry. `tenants/<org>/<ws>/orchestrator` **IS** seeded (since #230, both tenant
  orchestrator paths) and that is deliberate — but as a **floor only**: it makes the subtree come back
  after a wipe, and it can never overwrite what the control plane or the keypair lifecycle wrote.

### Post-wipe handling, by path class

Read this table as **"what restores this path after a wipe"**. It is not a write-permission table:
which writer owns a path *while the estate is running* is the contract above, and for the operator
seed the answer is always "the live value wins over the seed".

| Class | Paths | Post-wipe action |
|---|---|---|
| **SEED** — self-heals from `seeds.json` | `operator/dispatcher/forge` (`AF_BOT_TOKEN_PLANNER`), `operator/reaper/ledger` (`AF_REAPER_LEDGER_DSN`), `operator/broker/{anthropic/claude-max-1,anthropic/claude-max-2,openai/codex-pro}/ledger` (all three `AF_BROKER_LEDGER_DSN`, one shared `agentforge_broker` DB on infra-pg), `operator/ci/runner-registration` (`token`), `operator/ci/scaler-token` (`token`) | **Nothing** — re-provision restores them. (The 5 broker-ledger + ci paths were added to `seeds.json` on 2026-07-26; before that they were RESCUE.) |
| **RESCUE** — from the Retain'd Secret | `operator/broker/{anthropic/claude-max-1,anthropic/claude-max-2,openai/codex-pro}/oauth` | Re-seed from the Retain'd k8s Secret via the recipe in step 5. **Cannot be seeded** (`cas_required=true`) unless `bootstrap.py` is changed to CAS-write. Codex (`codex-pro/oauth`) **also rotates nightly** (single-use refresh token) so it stays a rescue regardless. |
| **RE-PROVISION** — by owner, never seeded | `operator/broker/*/kids` (registry.json — public key registry) | Rebuilt by the broker **keypair lifecycle**. It is not in `seeds.json` and must not be added (see the hard constraint above). |
| **SEED-FLOOR + OWNER** — seeded, but the seed can never overwrite a live value | `tenants/<org>/<ws>/orchestrator` (bot PATs / `AF_BOT_TOKENS`, `AF_CONTROL_PLANE_TOKEN`, `AF_CAPABILITY_SIGNING_KEY`+`AF_CAPABILITY_KID`) | **Partly automatic, and the split matters.** `_apply_operator_seeds` restores the seeded fragment — the `AF_BOT_TOKENS` map + the CP bearer **as of the last `operator-seeds.sops.yaml` refresh**, so REVOKED if a rotation was never committed with it; re-mint through the **control plane** in that case. The capability keypair is **not** seeded and does not come back with it: `AF_CAPABILITY_SIGNING_KEY`/`AF_CAPABILITY_KID` are restored only by the **keypair lifecycle** (owner action). Expect a short worker-401 window. This row is the reconciliation of the old "`tenants/*` — restore via the control plane, **not** by re-seeding" rule: the path *is* seeded, and the rule survives as **"the seed cannot override a live value"** — enforced now by create-if-absent rather than by everyone remembering. |
| **SEED (dev-worker subtree)** — a SECOND, independent seed path | `dev-workers/common`, `dev-workers/<hostname>` (ADR 0020) | **Nothing for the KV** — the daily `openbao-devworker-provision` Job re-creates the `approle` mount, the six `dev-worker-*` policies/roles, and seed-patches these paths from `devworker-seeds.sops.yaml`. **But every AppRole secret-id is invalidated by the wipe** → re-run the mint ceremony for all six workers (`docs/runbooks/openbao-dev-workers.md` §Activation (e)) or the workers' `bao agent`s log `invalid role or secret ID` forever. |
| **SEED (estate subtree)** — a THIRD, independent seed path | `estate/{proxmox,qnap,cloudflare,registry,gitea,github}` (`docs/runbooks/openbao-estate-credentials.md`) | **Nothing** — the daily `openbao-estate-provision` Job seed-patches these from `estate-seeds.sops.yaml`. Nothing logs in against them (escrow, no policy grants), so there is no credential to re-mint. |

### Privileged consumers to account for before a wipe

The bootstrap Jobs listed in the ceremony below (`openbao-init`, `openbao-provision`,
`provisioner-deploy`) all run the orchestrator image and are covered by the repin in step 1. Two
more privileged consumers do **not** ride that image:

- **`openbao-devworker-provision`** (ns `openbao`, `kubernetes/apps/infrastructure/security/openbao/
  devworker-provision-job.yaml`, ADR 0020) — the daily dev-worker AppRole/KV converger. It runs the
  official `openbao/openbao` image with an inline script ConfigMap and consumes **two** cluster
  objects: Secret `openbao-devworker-seeds` (in git, SOPS — `devworker-seeds.sops.yaml`) and Secret
  **`openbao-breakglass-token`** (**not in git**; data key `root_token`). The secret reference is
  `optional: false`, so a missing breakglass Secret makes the Job go red rather than silently skip.
- **`openbao-estate-provision`** (same directory, `estate-provision-job.yaml`) — the daily
  `af/estate/*` escrow seeder. Identical shape and identical dependencies: the SOPS seeds Secret
  (`openbao-estate-seeds`) plus the breakglass token, `optional: false`.
- **The breakglass token is scoped to the CURRENT vault state — a wipe kills it.** After a fresh
  `openbao-init` the old root token is meaningless, and there is no way to make a new one later:
  2.5.5 disables `generate-root`, `openbao-keys` carries only `cluster_id` + `unseal_key` (no root
  token), and the init Job revokes its own root token when it finishes. So the **only** moment a
  replacement breakglass token can be captured is during the re-bootstrap, while that fresh root
  token still exists — capture it then and rewrite the `openbao-breakglass-token` Secret, or the
  dev-worker subtree stops converging with no privileged path left to fix it.

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
   reaper paths self-heal from `seeds.json` and need nothing; `kids` is owner-restored; the SEED-FLOOR +
   OWNER row — `tenants/*` — comes back from the seed but still needs its keypair-lifecycle half, and a
   control-plane re-mint if the seeded PATs were stale). For each `oauth`: read its Retain'd k8s Secret,
   stage the value + the new provisioner token
   into the pod via `exec -i -- sh -c 'cat > /tmp/f'` (NOT stdin `read` — dies silently under MSYS),
   sha-verify pod==source, then in-pod with the durable operator-provisioner token (Secret
   `openbao-operator-provisioner-token`; the pod listens HTTPS so use `BAO_ADDR=https://127.0.0.1:8200
   BAO_SKIP_VERIFY=true` — MSYS `curl` is Schannel and `--cacert` fails exit 60):
   `bao kv put -cas=<current_version|0> -mount=af operator/broker/<broker>/oauth <field>=@/tmp/f`.
   **The field name is provider-specific** — `CLAUDE_CODE_OAUTH_TOKEN` for the two anthropic
   `claude-max-{1,2}` brokers, `auth.json` for `openai/codex-pro` (the ESO extract copies the whole doc, so
   the wrong key would sync a wrong-shaped Secret). Then re-stamp `bao kv metadata put -cas-required=true
   ...`. Verify the read-back sha equals the
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

This section is about the **operator** seed blob (`operator-seeds.sops.yaml` → `bootstrap.py`). The
`dev-workers/*` and `estate/*` subtrees have their own files and their own Jobs — add fields there
instead (`docs/runbooks/openbao-dev-workers.md` § "Adding a secret",
`docs/runbooks/openbao-estate-credentials.md` § "How it converges"). A path from either, added to
`seeds.json`, would be written by the wrong owner, on the wrong schedule, and under the **wrong
precedence**: those Jobs are seed-wins, this one is create-if-absent (contract above).

Only **non-CAS** paths qualify (never `.../oauth` or `operator/broker/*/kids` — see the hard constraint
above). A `tenants/*` fragment does belong here — it is how that subtree survives a wipe — but purely as
a **floor**: it is kept in step with the live values, never used to push a value into a live vault.
Never hand-edit ciphertext:

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
