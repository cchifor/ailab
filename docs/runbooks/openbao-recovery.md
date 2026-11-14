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

- *operator seeds*: **empty (`{}`) since the AgentForge retirement (2026-10-31, ADR 0041)**: every path
  they held was AgentForge's. The mechanism stays (the provision Job mounts the file, and a missing or
  empty file is fatal; `{}` is the explicit "seed nothing"). Should a path ever be added again, the
  seed can no longer revert a rotation, but it is still the **DR floor**: a stale fragment restores a
  **revoked** credential after a wipe, and reports as drift on every provision run until it is refreshed.
- *dev-worker / estate seeds*: unchanged and literal — a vault-only rotation is overwritten back to
  the old value within a day.

> **Open gap — "reported as drift" has no consumer in this repo yet.** Nothing in `kubernetes/` or
> `.gitea/` alerts on, counts, or gates a seed/vault divergence. The report lands in the
> `openbao-provision` Job's own log, which nobody reads on a green Job. (Moot while `operator-seeds` is
> `{}`, and the dev-worker and estate seeders are seed-wins, so they cannot drift.) Until an alert or CI check consumes it, **treat the same-change seeds refresh as the
> only control** — create-if-absent removed the silent *revert*, not the silent *staleness*.

**What this means for the path taxonomy below.** The classes answer *"what restores this path after a
wipe"* — **not** *"who may write it while the estate is running"*. A path can be both **seeded** (so
it comes back) and **owner-managed** (so the seed can never overwrite the owner's value);
`tenants/<org>/<ws>/orchestrator` was exactly that until the AgentForge retirement (2026-10-31). Collapsing the two questions into one is what made
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

- **ESO Retain** → every synced k8s Secret survives the wipe; consumers keep serving from their mounts
  (no restarts).
- **The seed contract.** `provisioner/bootstrap.py::_apply_operator_seeds` merge-writes
  (**create-if-absent**, **no `options.cas`** — see the contract above) each entry of the decrypted
  `openbao-operator-seeds` blob (`operator-seeds.sops.yaml`, `kind: Secret`, single encrypted
  `stringData.seeds.json`, a flat `{ "<path>": { "<field>": "<value>" } }`) to `af/data/<logical_path>`.
  **Hard constraint — seed only NON-CAS paths:** a `cas_required=true` path cannot be seeded, because
  the no-CAS seed write **fails closed** (it would first need `bootstrap.py` to CAS-write). The blob is
  `{}` since the AgentForge retirement (2026-10-31, ADR 0041); every path it held (broker ledgers, CI
  tokens, forge PATs, the platform-dev tenant document) was AgentForge's.

### Post-wipe handling, by path class

Read this table as **"what restores this path after a wipe"**. It is not a write-permission table:
which writer owns a path *while the estate is running* is the contract above, and for the operator
seed the answer is always "the live value wins over the seed".

| Class | Paths | Post-wipe action |
|---|---|---|
| **RETIRED** — AgentForge's operator and tenant paths (2026-10-31, ADR 0041) | `operator/{broker,reaper,ci,conductor,dispatcher}/*`, `tenants/*` and `dev-workers/codex-auth` (the broker ledgers and OAuth documents, the reaper ledger, the CI registration and scaler tokens, the conductor and dispatcher forge PATs, the platform-dev orchestrator document) | **Nothing, and do not restore them.** They were deleted with the retirement, and `operator-seeds.sops.yaml` is now `{}`, so a re-provision brings none of them back. Their consumers are gone, and the Gitea PATs they held were revoked. If an old `seeds.json` fragment ever reappears in that file, it is a regression: delete it rather than letting the bootstrap floor recreate retired credentials. **Expected remnant:** the retained bootstrap image re-stamps `cas_required` on four broker `oauth` paths (`claude-max-1/2/3`, `codex-pro`) on every provision run, leaving metadata-only stubs with no versions and no data. They hold nothing; they go with #1536. |
| **SEED (dev-worker subtree)** — a SECOND, independent seed path | `dev-workers/common`, `dev-workers/<hostname>` (ADR 0020) | **Nothing for the KV** — the daily `openbao-devworker-provision` Job re-creates the `approle` mount, the six `dev-worker-*` policies/roles, and seed-patches these paths from `devworker-seeds.sops.yaml`. **But every AppRole secret-id is invalidated by the wipe** → re-run the mint ceremony for all six workers (`docs/runbooks/openbao-dev-workers.md` §Activation (e)) or the workers' `bao agent`s log `invalid role or secret ID` forever. |
| **SEED (estate subtree)** — a THIRD, independent seed path | `estate/{proxmox,qnap,cloudflare,registry,gitea,github,restic,platform,oauth,strive-realm,cloudlab-win11,unifi}` (`docs/runbooks/openbao-estate-credentials.md` — that page's table is the list; don't trust this copy of it over that one) | **Nothing** — the daily `openbao-estate-provision` Job seed-patches these from `estate-seeds.sops.yaml`, and its fail-closed matrix covers every path/field named here. Nothing logs in against them (escrow), so there is no credential to re-mint. **But the subtree is wider than the seed:** `estate/{litellm,wifi}` are live in KV with no seed and no matrix entry (`estate/unifi` was seeded on 2026-10-27, ADR 0040), so a wipe loses them silently and this row does NOT cover them. `estate/litellm` also has one reader — policy `af-app-dsh`. |
| **PUBLISHER-OWNED** — not seeded, and not restorable by any seeder (ADR 0026) | `dsh/credentials.DSH_CODEX_*`, `litellm/chatgpt.*` — the reviewer-2 seats' access tokens, projected every minute by `dsh-codex-publisher` on that host (`docs/runbooks/dsh.md` § Codex subscriptions) | **LOST until the publisher's next successful minute, and the order matters.** These are access tokens that the reviewer host renews every few days; a seed would put a stale token back daily, so there is none. Sequence: **(0)** restore the k8s-auth role `af-app-dsh` BY HAND first — `bao write auth/kubernetes/role/af-app-dsh bound_service_account_names=dsh-eso bound_service_account_namespaces=dsh token_policies=af-app-dsh token_ttl=1h alias_name_source=serviceaccount_uid` — because the unchanged `openbao-dsh-provision` Job only ASSERTS that role and aborts without it (a pre-existing gap ADR 0026 records rather than fixes; the chatgpt Job needs no such step, it writes its own role). **(1)** the `openbao-chatgpt-provision` and `openbao-dsh-provision` Jobs (both `openbao-breakglass-token`-auth, both in this directory) recreate both policies (`dsh-codex-publisher`, `af-app-litellm`), the k8s-auth role `af-app-litellm`, and both documents with their EMPTY keys — `af/litellm/chatgpt` with its four `CHATGPT_*` fields + `CHATGPT_OPENBAO_CANARY`, `af/dsh/credentials` with ONLY its `DSH_OPENBAO_CANARY`; that document's other operator fields (forge PAT, operator SSH) follow the ceremonies in `docs/runbooks/dsh.md`. **(2)** The AppRole ceremony in `dsh.md` recreates AppRole `dsh-codex-publisher` and mints a NEW role-id/secret-id into `/etc/dsh-codex-publisher/approle.json` on reviewer-2 — the wipe invalidated the old one, exactly as it does the dev-worker secret-ids. **(3)** The publisher's next minute fills both documents. It can only **PATCH** (no put, no create), so (1) MUST come first or every run fails on a 404; and a SOFT-DELETED document is refused (`deletion_time` set on the current version) until an operator `bao kv undelete`s it or the Job's loud-failure branch ("neither readable nor creatable") is resolved. ESO Retain keeps dsh and litellm serving the last-good token throughout — until it expires (~7 days), which is the real deadline. Verify with `dsh_codex_projection_ok == 1` for both documents. |
| **GENERATED-ONCE** — not seeded; a missing document is RE-GENERATED, not restored | `strive/pg-harness.password` — the harness service's `strive-pg` login (`docs/runbooks/infra-pg.md` § strive-pg) | **Restore the OLD value, or the harness loses its database.** The daily `openbao-strive-pg-harness-provision` Job recreates policy + k8s-auth role `af-app-strive-pg-harness` and, finding no document, writes a NEW random password; the `strive-pg-harness-bootstrap` Job in `strive-ailab` then sets the `harness` role to it, and the harness's DSN (the ESO-rendered Secret `strive-pg-harness-dsn`, key `database-url`) no longer matches once it refreshes. Take the password from that DSN (read it before the next ExternalSecret refresh, or from the harness pod) and `bao kv put -mount=af strive/pg-harness password=-` it (stdin) before the Job runs, or `bao kv patch` it after; then follow the rotation order in `docs/runbooks/infra-pg.md` (force-sync both ExternalSecrets, re-run the bootstrap Job, restart the harness). |
| **OPERATOR-OWNED (ceremony)** — not seeded, not generated, never written by a Job | `relay-budget/{allocator,router}.password`, `relay-budget/ca.certificate`, `relay-budget/witness.witness` — the independent Relay worker budget ledger's client files (`docs/runbooks/relay-worker-budget-authority.md` § Credential delivery) | **LOST, and nothing re-creates them.** The daily `openbao-relay-budget-provision` Job (`relay-budget-provision-job.yaml`) recreates policies + k8s-auth roles `af-app-relay-budget` and `af-app-router-budget` only. ESO keeps the last rendered Secrets in `relay` and `llm-router` (Retain), and running consumers keep their mounted files until restarted. Do NOT restore `witness` from a backup without checking it against the ledger's `worker_budget.authority` row: an older witness only fails closed, but a witness must come from the independent recovery evidence, never be guessed. The passwords must equal the ledger roles' current ones; if unknown, withdraw authority and set new ones with `\password` (runbook step 5), then roll both consumers. `ca.certificate` is the public ailab root CA (`ansible/roles/openbao_agent/files/ailab-root-ca.crt`). |
| **TRANSIT** — key material, not KV; recoverable ONLY from a raft snapshot | `transit/keys/workbricks-c1-{jwks-root,release,audit}` (ecdsa-p256, `exportable=false`, `allow_plaintext_backup=false`) — the Workbricks cell c1's signing keys (`workbricks-c1-provision-job.yaml`) | **A wipe without a snapshot restore replaces the keys, and that is not silent damage you can ignore.** The daily `openbao-workbricks-c1-provision` Job re-enables `transit/` and creates the three keys, the four policies and the four k8s-auth roles, so the cell can sign again at once, but with NEW keys: every key attestation, release manifest and audit checkpoint signed before is unverifiable against the new public keys. The keys are non-exportable by design, so the ONLY way to keep them is the raft snapshot (§ Raft snapshot): restore it before the Job runs. If the keys were lost anyway, tell the Workbricks operators (their `operator-requests.md`) before the cell next publishes, so they re-anchor the JWKS root and re-sign the release records. |
| **SYNC-OWNED** — a FOURTH precedence: not seeded, and not restored by any seeder | `dev-workers/<hostname>.helmtest_kubeconfig` (ADR 0021; `tep_kubeconfig` was one until ADR 0037), `dev-workers/<hostname>.platform_kubeconfig` (ADR 0028, same CronJob) and `dev-workers/<hostname>.platform_pg_{user,password,valid_until}` (ADR 0028, a SECOND CronJob: `openbao-platform-pg-sync` in ns `strive-ailab`, which needs the k8s-auth role `platform-pg-sync` from the same daily provision Job and re-creates the Postgres roles itself — so it self-heals from a wipe with no operator action, and a worker missing those fields simply renders no platform credentials) | **LOST until a successful sync, and the order matters.** These are cluster-derived (TokenRequest against the `helmtest-dw<N>` / `platform-dw<N>` ServiceAccounts) and deliberately absent from `devworker-seeds.sops.yaml` — a seeded copy would be re-applied daily by the seed-wins loop and revert every worker to a stale bearer token. Sequence: **(1)** vault + `af` mount + **both** k8s-auth roles and the policy they share — `k8stoken-sync` and `platform-pg-sync`, all three created by the daily `openbao-devworker-provision` Job; **(2)** run **both** syncs and confirm each one's own success line — `openbao-k8stoken-sync` prints `validated N/N fields` (2 fields x live slots, 8/8 for the current four) and `openbao-platform-pg-sync` prints `published N/N slots`; **(3)** re-mint every AppRole secret-id (the row above); **(4)** ONLY THEN restart the workers' agents. Restarting before (2) is not merely early — `error_on_missing_key` + `exit_on_retry_failure` make a missing field exit the agent, so it sits in a restart loop with `~/.git-credentials` unmaintained. **And note what a wipe does NOT change: the agent config on disk.** A worker that had platform access keeps its `platform` stanzas in `/etc/openbao-agent/agent.hcl` across the wipe — the ansible gate only decides what a *future* run writes — so the pg-sync is not optional cleanup here; until it has published, that worker's agent exits on the missing field and takes `~/.git-credentials` with it. If both syncs cannot be restored promptly, stop the agent or remove those stanzas rather than leaving it to restart-loop. |

### Privileged consumers to account for before a wipe

The bootstrap workloads listed in the ceremony below (`openbao-init`, `openbao-unsealer`,
`openbao-provision`) all run the orchestrator image and are covered by the repin in step 1. Four
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
- **`openbao-dsh-provision`** (same directory, `dsh-provision-job.yaml`) — owns policy `af-app-dsh`
  and creates `af/dsh/credentials` once (canary only; the contents are operator-owned). Breakglass
  token, `optional: false`; no seeds Secret.
- **`openbao-chatgpt-provision`** (same directory, `chatgpt-provision-job.yaml`, ADR 0026) — owns
  policies `af-app-litellm` and `dsh-codex-publisher` and the k8s-auth role `af-app-litellm`, and
  creates `af/litellm/chatgpt` once (empty `CHATGPT_*` keys + canary; the contents are the
  reviewer-2 publisher's). Breakglass token, `optional: false`; no seeds Secret. These two are what
  step (1) of the PUBLISHER-OWNED row above depends on.
- **The breakglass token is scoped to the CURRENT vault state — a wipe kills it.** After a fresh
  `openbao-init` the old root token is meaningless, and there is no way to make a new one later:
  2.5.5 disables `generate-root`, `openbao-keys` carries only `cluster_id` + `unseal_key` (no root
  token), and the init Job revokes its own root token when it finishes. So the **only** moment a
  replacement breakglass token can be captured is during the re-bootstrap, while that fresh root
  token still exists — capture it then and rewrite the `openbao-breakglass-token` Secret, or the
  dev-worker subtree stops converging with no privileged path left to fix it.

## The bootstrap Jobs, and disaster notes

Moved here from the AgentForge activation runbook when AgentForge was retired (2026-10-31, ADR 0041). OpenBao's
bootstrap still runs `agentforge` CLI code from the orchestrator image pinned in the three manifests below.
Flux deploys OpenBao (TLS via `openbao-tls`), and these run on their own:
- **`openbao-init`** (`unseal-job.yaml`) initializes and unseals, writing `openbao-keys` (the unseal key and
  `cluster_id`) and `openbao-bootstrap-token` (root). It is idempotent and fail-closed: it never re-inits
  over a live vault, and a `cluster_id` disagreement or a partial death is a hard failure.
- **`openbao-unsealer`** (`unsealer.yaml`, a Deployment) re-unseals after any restart. It reads
  `openbao-keys` only.
- **`openbao-provision`** (`provision-job.yaml`) creates the `af` KV mount, the `kubernetes` auth backend
  (server-SA TokenReview, no static JWT), the base policies, the canary role and seed, and the operator
  seeds (`{}` now). It mints the durable, add-only operator reconcile token into the Secret
  **`openbao-operator-provisioner-token`** (policy `agentforge-operator-provisioner`; the identity every
  later run uses, and the one ceremony step 7 checks). It writes the `openbao-state` sentinel, then
  **revokes root and deletes `openbao-bootstrap-token`**. On that same root path it also mints `openbao-provisioner-token` for the
  retired provisioner; `retired-token-revoker.yaml` removes it (§ "After a re-bootstrap" below).

Verify with `just openbao-status`: the init and provision Jobs Succeeded, `openbao-state.provisioned=true`,
`openbao-bootstrap-token` gone, and the **`openbao-canary` ExternalSecret Ready=True** (the end-to-end
k8s-auth proof).

**Disaster notes:**
- `openbao-init` refuses to re-init if `openbao-keys` exists but the vault is uninitialized (a stale key or a
  lost PV). It fails loudly rather than minting a NEW vault that orphans every stored secret.
- Losing `openbao-keys` after init means an unrecoverable seal unless the key comes back: restore it from the
  escrow copy `kubernetes/infra/openbao-unseal.sops.yaml` (ceremony step 6), or restore the vault PV from a
  backup together with its matching key. There is no re-key path (OpenBao 2.5.5 answers 405 to `operator rekey`, as
  it does to `generate-root`; see "When to use" at the top); otherwise it is the wipe ceremony below.
- The unseal key and the root token (before its revocation) live only in the `openbao` namespace. The
  unsealer reads the unseal key only.
- **Accepted residual:** `openbao-keys` is an ordinary Kubernetes Secret. The cluster runs without etcd
  encryption at rest, so the unseal key is recoverable from a raw etcd or disk read. Encrypting Secrets at rest
  (or Talos disk encryption) would be a separate control-plane rolling change, not done.

## Raft snapshot (before ANY server upgrade or risky change)

A wipe loses KV (above); a raft snapshot does not. Take one before a chart/image bump rolls
`openbao-0` (the StatefulSet is `RollingUpdate` since 2026-09-27, so MERGING the bump is the
roll), and before anything else that touches `/openbao/data`. First taken 2026-09-27 on 2.5.5
before the 2.6.2 roll; it is ~0.7 MB.

```sh
WHY=pre-upgrade   # a word saying why this snapshot exists
OUT=kubernetes/infra/_out/openbao-raft-$(date -u +%Y%m%d)-$WHY.snap   # _out/ is gitignored
umask 077
kubectl --context admin@ai -n openbao get secret openbao-breakglass-token     -o jsonpath='{.data.root_token}' | base64 -d |
  MSYS_NO_PATHCONV=1 kubectl --context admin@ai -n openbao exec -i openbao-0 -c openbao -- sh -c '
    read -r T; export BAO_TOKEN="$T" BAO_ADDR=https://127.0.0.1:8200 BAO_SKIP_VERIFY=true
    bao operator raft snapshot save /tmp/s.snap >/dev/null 2>/tmp/s.err || { cat /tmp/s.err >&2; exit 1; }
    cat /tmp/s.snap; rm -f /tmp/s.snap /tmp/s.err' > "$OUT"
tar tzf "$OUT"   # expect meta.json, state.bin, SHA256SUMS, SHA256SUMS.sealed
```

The token goes pod-ward on stdin, never on a command line or stdout. The snapshot is encrypted
with the barrier key, so it is only restorable together with `openbao-keys` (the unseal share) —
keep the two apart, and never commit either.

**Restore** (same seal): `bao operator raft snapshot restore <file>` from inside `openbao-0` with
the break-glass token (copy the file in with `kubectl cp` first). Add `-force` only when the
running vault's seal no longer matches the snapshot's (i.e. after a wipe re-initialised it) —
it bypasses exactly that consistency check, so the unseal key from the snapshot's era is then
what unseals the restored data. Untested here as of 2026-09-27; prove it on a throwaway before
relying on it. Rolling the IMAGE back after a newer server has written storage is not a proven
rollback — restore the snapshot onto the version that took it instead.

## Ceremony
1. **Repin FIRST** (or the gap you're fixing reproduces): the three bootstrap refs in
   `kubernetes/apps/infrastructure/security/openbao/{unseal-job,unsealer,provision-job}.yaml`
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
5. **Re-seed the RESCUE-class paths: none are left.** That class was the five AgentForge broker `oauth`
   documents, retired with AgentForge on 2026-10-31 (ADR 0041). Go straight to the dev-worker and
   estate Jobs' rows in the table above, then retire the provisioner token the root path re-minted
   (§ "After a re-bootstrap" below; the revoker CronJob does it within 15 minutes).
6. **Re-escrow the NEW seal** in `kubernetes/infra/openbao-unseal.sops.yaml`. The file MUST stay a
   `kind: Secret` manifest with `stringData:` — the SOPS creation rules encrypt ONLY
   Secret-shaped keys; a flat yaml passes through **PLAINTEXT** while sops still adds
   metadata+mac (this caused a real 2026-07-24 leak → seal rotation). Before pushing, assert BOTH
   values start with `ENC[` AND the decrypt round-trip hash matches the live Secret.
7. **Verify**: role probe matrix (positive + negative per role), all ExternalSecrets `SecretSynced`,
   the `openbao-canary` ExternalSecret Ready, `openbao/openbao-operator-provisioner-token` recreated and
   usable (`bao token lookup` with it succeeds), and `openbao/openbao-provisioner-token` absent again.

## Adding a SEED path (making a static value self-heal)

This section is about the **operator** seed blob (`operator-seeds.sops.yaml` → `bootstrap.py`). The
`dev-workers/*` and `estate/*` subtrees have their own files and their own Jobs — add fields there
instead (`docs/runbooks/openbao-dev-workers.md` § "Adding a secret",
`docs/runbooks/openbao-estate-credentials.md` § "How it converges"). A path from either, added to
`seeds.json`, would be written by the wrong owner, on the wrong schedule, and under the **wrong
precedence**: those Jobs are seed-wins, this one is create-if-absent (contract above).

Only **non-CAS** paths qualify (see the hard constraint above), and a fragment here is purely a
**floor**: it is kept in step with the live values, never used to push a value into a live vault.
Never hand-edit ciphertext:

1. `export SOPS_AGE_KEY_FILE="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out/age.agekey"`
   — the key lives in the **main** checkout (`_out/` is gitignored, so it is absent from a worktree).
2. Parse `stringData.seeds.json` → add the entries (preserve the existing ones) → re-serialize.
   **Two levels, two different serializations, and they are easy to swap:** the seeds BLOB is
   `json.dumps(obj, indent=2)` inside a `|` literal block scalar indented by 8; an individual VALUE
   that is itself JSON — `AF_BOT_TOKENS`, say — is a compact `json.dumps(v, separators=(',',':'))`
   **string** inside it. Getting this backwards reformats the whole document, which buries the one
   line that actually changed. **The `|` costs you a trailing newline**: a YAML parser hands back the
   scalar *with* one and `json.dumps` never emits one, so compare against
   `json.dumps(obj, indent=2) + "\n"` if you parsed with YAML, or against `json.dumps(obj, indent=2)`
   if you de-indented the block's lines yourself and dropped the trailing blank. Prove it instead of
   trusting this paragraph: re-serialize the blob *before* editing it and assert the result is
   byte-identical to what you parsed; only then add the key. An assertion that fails here is telling
   you your newline convention is wrong, not that the document is.
3. Re-encrypt. Prefer `sops edit` driven by a **programmatic `EDITOR`** — sops shlex-splits `$EDITOR`
   and appends the decrypted tmpfile as the **LAST** argument, after every argument you configured
   (use forward slashes; shlex eats Windows backslashes). With `EDITOR="python shim.py"` that lands
   at the shim's `sys.argv[1]`, but only because the shim took no arguments of its own — read it as
   `sys.argv[-1]` if yours does. A shim can then fetch the value from its live source itself, so the
   plaintext never
   reaches a shell argv, a terminal, or any file sops does not already manage and delete. The older
   route — write the plaintext manifest out, then `sops -e -i operator-seeds.sops.yaml` — works and
   applies the same rules (generic `.*\.sops\.ya?ml$` → `^(data|stringData)$` + the age recipient),
   but it puts the whole decrypted blob on disk to do it.
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

## After a re-bootstrap: retire the AgentForge provisioner token again

The AgentForge provisioner was retired on 2026-10-31 (plans/2026-10-31-retire-agentforge-plan.md), but OpenBao's own
bootstrap still runs AgentForge code. On the root path of a first bootstrap it mints a long-lived scoped token
into the Secret `openbao/openbao-provisioner-token`. The daily provision run on a provisioned vault never does.
Nothing reads that token any more, and its policy can write tenant policies and k8s-auth roles. So after any
re-bootstrap it must be revoked and deleted. The `openbao-retired-token-revoker` CronJob (`security/openbao/retired-token-revoker.yaml`)
does that automatically every 15 minutes, and `OpenBaoRetiredProvisionerTokenPresent` fires if the Secret is still there after 45. By hand:
1. From a short-lived pod or shell that reads the token from the Secret without printing it, POST
   `v1/auth/token/revoke-self` with it. Any token may revoke itself: expect 204, then 403 from `lookup-self`.
2. `kubectl --context admin@ai -n openbao delete secret openbao-provisioner-token`.
Leave `openbao-operator-provisioner-token` alone: the provision run's operator reconcile uses it.
