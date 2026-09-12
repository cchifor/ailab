# Runbook: estate credentials in OpenBao (`af/estate/*`)

The operator-plane infrastructure credentials — Proxmox, QNAP, Cloudflare, the zot registry, Gitea
runner registration, the GitHub App key — escrowed in OpenBao under a **seed-wins** durability
contract: the same one the dev-worker subtree uses, and **not** the one the AgentForge `operator/*`
paths use (those are create-if-absent — the live vault wins there; canonical side-by-side in
`docs/runbooks/openbao-recovery.md` § "The seed-ownership contract").
Backfilled 2026-08-31 from the full credential audit; `strive-realm` added 2026-09-12.
The vault itself: `docs/runbooks/openbao-recovery.md`. The dev-worker consumption plane (which does
NOT read these paths): `docs/runbooks/openbao-dev-workers.md`.

## Scope — what is here, and what deliberately is not

`af/estate/*` exists for credentials whose only prior homes were **gitignored plaintext on the
operator workstation** (`.env`, `terraform.tfvars`) or an ansible SOPS file — i.e. the ones a lost
laptop, or an audit question, could not answer for. It is a durable escrow and the rotation source
of truth; the CONSUMERS still read their historical locations (see the multi-home table) until they
are individually migrated.

Deliberately **not** mirrored here:

- **Flux SOPS Secrets under `kubernetes/apps/**`** (gitea admin/oauth/db/metrics, litellm keys,
  cert-manager token, cloudflared creds, deploy keys, cloud-power, trident backend, …) — Flux+SOPS
  is their system of record, already encrypted in git; a second home would add rotation
  split-brains, not remove them.
- **`operator/*` and `tenants/*`** — AgentForge's paths, owned by its own provisioning
  (ADR 0019; `openbao-recovery.md` path classes).
- **The unseal key and the breakglass token** — they cannot live inside the vault they open.

> **The one named exception (2026-09-12): `keycloak_admin_password`.** It *is* a Flux+SOPS
> Secret — in the **platform** repo (`deploy/secrets/ailab/`), not this one — and it was
> escrowed anyway, on an explicit operator decision: break-glass reach into the `strive` realm
> without needing the platform repo's age key. It therefore has exactly the second home the first
> bullet above exists to prevent.
>
> **The failure mode is a stale escrow, not a reverted credential** — the obvious guess is the
> wrong one. Nothing reads `af/estate/*` back into Keycloak or into the platform Secret (see
> **Access** below: nothing grants `estate/strive-realm` specifically — the single estate
> grant that does exist is on `estate/litellm`), so the provision Job
> only ever writes the VAULT copy. Rotate the admin password in the platform SOPS file alone
> and Keycloak is correct, the live `keycloak-secrets` is correct, and this Job quietly keeps
> restoring the OLD password into `af/estate/strive-realm` every day. The break-glass copy is
> then wrong at precisely the moment it is reached for — and nothing anywhere goes red.
> Rotate both or neither. Nothing else on this page is a Flux+SOPS mirror.

## Layout

Mount `af` (KV v2), prefix `estate/`, one path per system:

| Path | Fields | Where the value came from | Other live homes (rotation must touch ALL) |
|---|---|---|---|
| `af/estate/proxmox` | `root_password`, `api_token` | `.env` `NODE_ROOT_PASSWORD`; `pve_api_token` in `terraform.tfvars` | root password: `.env` + typed into ai-lxc/registry-lxc tfvars (root@pam gate); api_token: **five** gitignored tfvars files (`tofu/`, `kubernetes/infra{,/agent-nodes,/dev-workers,/runners}`) |
| `af/estate/qnap` | `admin_user`, `admin_password` | `.env` `QNAP_SSH_USER` / `QNAP_ADMIN_PASSWORD` | `.env`; possibly the same account as Trident's `kubernetes/apps/qnap-storage/backend-secret.sops.yaml` — VERIFY before rotating, Trident breaks on the next PVC op otherwise |
| `af/estate/cloudflare` | `api_token` | `.env` `CLOUDFLARE_API_TOKEN` (verified identical to `cloudflare_dns_api_token` in `ansible/secrets/registry.sops.yaml`) | `.env` (tofu provider reads env only); `ansible/secrets/registry.sops.yaml` (certbot on the registry LXC); `kubernetes/apps/infrastructure/cert-manager/cloudflare-api-token.sops.yaml` (DNS-01) — **three consumers, one token** |
| `af/estate/registry` | `ci_password`, `oidc_client_secret` | `ansible/secrets/registry.sops.yaml` | same SOPS file (zot htpasswd + OIDC); the OIDC secret's pbkdf2 **hash** is separately committed in `kubernetes/apps/apps/auth/authelia-config.yaml` |
| `af/estate/gitea` | `runner_registration_token` | `ansible/secrets/gitea-runner.sops.yaml` | same SOPS file (5 VM act_runners); the KEDA pool uses its own `operator/ci/runner-registration` |
| `af/estate/github` | `app_private_key` | `ansible/secrets/github-runner.sops.yaml` | same SOPS file (`github_runner` role → runner VMs) |
| `af/estate/restic` | `nextcloud_password` | `~/work/keys/nextcloud-restic-password.txt` | that file only — it was in **no** SOPS file anywhere (ADR 0021 Step 0) |
| `af/estate/platform` | `hatchet_encryption_master_keyset`, `hatchet_jwt_public_keyset`, `hatchet_jwt_private_keyset`, `hatchet_client_token`, `sendgrid_key` | `~/work/keys/platform.env` | that file only. **NOT** the OpenAI/Anthropic keys in the same file — see the rejected list below |
| `af/estate/oauth` | `gcloud_client_id`, `gcloud_client_secret`, `github_client_id`, `github_client_secret` | `~/work/keys.txt` | that file only |
| `af/estate/strive-realm` | `keycloak_admin_user`, `keycloak_admin_password`, `e2e_worker_usernames`, `e2e_worker_password`, `load_persona_username`, `load_persona_password` | live cluster Secrets in ns `strive-ailab`: `keycloak-secrets/admin-password`, `e2e-secrets/worker-password`, `load-secrets/persona-password` | **the two persona passwords: nowhere else at all** — `e2e-secrets` and `load-secrets` are hand-created (no Flux labels, no owner, in no git repo); **`keycloak_admin_password`: a SECOND home** — the platform repo's `deploy/secrets/ailab/keycloak-secrets.enc.yaml` (Flux SOPS → the live `keycloak-secrets`), so rotation must touch that file AND this seed in the same change, or the escrow silently goes stale (the Job writes the vault only — it cannot revert the live credential) |

**Verified-and-REJECTED candidates (2026-09-10, ADR 0021).** Every `*.sops.yaml` in the repo (57
files) was decrypted and value-hashed before anything was seeded. Three candidates that *looked*
workstation-only turned out not to be, and escrowing them would have created the rotation
split-brain this page exists to prevent:

| Candidate | Why it is NOT here |
|---|---|
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` (`platform.env`) | **Byte-identical** to `kubernetes/apps/apps/ai/litellm-cloud-keys.sops.yaml`. Flux+SOPS is their system of record. |
| rclone crypt password + salt (`rclone-crypt-escrow.txt`) | The **same secret** that `kubernetes/apps/backup/backup-offsite/rclone-config.sops.yaml` already holds in `rclone obscure` form. Obscure is reversible AES-CTR, not a hash — verified by implementing `reveal`. The plaintext file stays as an **offline** DR artifact. |
| talos-backup age key | **Tier C, not Tier B.** It decrypts etcd snapshots, and etcd holds every k8s Secret — including `openbao-breakglass-token` and `openbao-estate-seeds`. Escrowing it puts, inside the online boundary, a key that decrypts historical copies of that boundary's own root token. General rule: *a credential that decrypts a backup of the boundary is classified by what the backup contains, not by what the credential is for.* |

> **⚠ The workstation copy `~/work/keys/talos-backup-age.key` is ORPHANED.** Its public key is
> `age1te3n4lf…`, but `kubernetes/apps/backup/talos-backup/cronjob.yaml` encrypts to
> `age13ruz38k…` — the key in `kubernetes/infra/_out/talos-backup-age.key`. The workstation copy
> **decrypts nothing**, so it is not a DR artifact and must not be treated as one. Before deleting
> it, establish whether any *retained* snapshot predates a key change and was encrypted to it.

**Access:** **one** policy grants anything under `estate/*`, and it is deliberately narrow —
`af-app-dsh` has `read` on `af/data/estate/litellm` (+ its metadata) and nothing else
(`dsh-provision-job.yaml`, where that grant is pinned into the BASELINE/DESIRED strings so a
policy rewrite cannot silently drop it). Everything else here is root-tokens-only: no dev-worker
AppRole and no ESO SecretStore has a grant, and the `cred` helper is denied by design. Widening
further is a deliberate decision, not a default.

> *Corrected 2026-09-12 — this paragraph previously read "no policy grants `estate/*` to
> anything", which had been false since before `dsh-provision-job.yaml` landed. It was quoted
> back at me by an agent reasoning about its own 403, so the stale absolute was actively
> misleading someone.*

> **Before widening `estate/*` for a workload, project instead.** Two reasons. First, a KV grant
> is **per path, not per field**: `read` on `af/estate/strive-realm` hands over all six fields,
> including the master-realm admin password, when the caller wanted one persona password.
> `estate/litellm` was a clean single-path grant only because it has a single field. Second,
> there is already a sanctioned mechanism: seed the value the consumer actually needs into
> `af/dev-workers/common` or `af/dev-workers/<host>`, which every worker policy already covers,
> so no policy changes at all. `af/dev-workers/codex-auth` is the precedent — a host PROJECTION
> of an estate-class credential — and `af/dev-workers/dev-worker-3.strive_test_{user,password}`
> (the e2e persona, added 2026-09-12) is the second. Projection also keeps the blast radius of a
> compromised worker to the projected field rather than the whole estate path.

> **The vault policy is not the whole boundary, and the difference matters (ADR 0021).** Flux
> decrypts `estate-seeds.sops.yaml` into a **live `openbao-estate-seeds` Secret in ns `openbao`**, so
> anyone who can read Secrets in that namespace — or schedule a workload that mounts one — holds
> every value on this page regardless of what the vault policy says. The real boundary is the
> **union** of (a) the vault policy, (b) Secret-read RBAC in `openbao`, and (c) the root-capable
> vault logins: the never-expiring breakglass token, and the undocumented `auth/userpass` `root`
> user this page already flags below. Treat the vault policy — even stated correctly, as it now is above — as one of
> three locks, not as the lock.
>
> **Testing the denial correctly.** `cred get estate/platform openai_api_key` proves nothing: `cred`
> prefixes every lookup with `dev-workers/`, so that probes `af/dev-workers/estate/platform` and
> fails as *not found* rather than *forbidden*. Ask for the real path with the sink token, and
> distinguish 403 from 404:
>
> ```bash
> BAO_ADDR=https://openbao.lan.chifor.me:30820 BAO_TOKEN="$(cat /run/openbao-agent/token)" \
>   bao kv get -mount=af -format=json estate/platform >/dev/null
> echo "exit=$?"   # non-zero, and stderr must say permission denied — NOT "no value found"
> ```

## How it converges

The daily **`openbao-estate-provision`** Job (`kubernetes/apps/infrastructure/security/openbao/estate-provision-job.yaml`)
authenticates with the breakglass token and `bao kv patch`es each `af/estate/<name>` from the
`<name>.json` keys of Secret `openbao-estate-seeds` (`estate-seeds.sops.yaml`, SOPS in git).

**Seed wins** — literally, on every run, because this Job is a shell `bao kv patch` loop. (Do not
generalise from the AgentForge `operator/*` seed, which is create-if-absent and lets the live vault
win; `docs/runbooks/openbao-recovery.md` § "The seed-ownership contract" has the three seeders
side by side. The header comment inside `estate-seeds.sops.yaml` still claims parity with all of
them and cannot be corrected without re-encrypting that file.) A key present in the seed overwrites
live KV on every run; a key absent from the seed survives. So:

- **Rotating a credential** = change it at the real system, update **every** home in the table
  above, and re-encrypt `estate-seeds.sops.yaml` in the same change — otherwise the vault silently
  reverts to the old value within a day and the table's other homes drift.
- **Adding a field/path** = `sops` edit the seeds file (add a field to a `<name>.json`, or a new
  `<name>.json` for a new `af/estate/<name>`) AND add the pair to the provision script's
  completeness matrix in `estate-provision-job.yaml` — the Job fails closed on any matrix entry a
  seed did not restore, so the two must move together. Merge; to make it converge immediately:
  `kubectl --context admin@ai -n openbao delete job openbao-estate-provision` then reconcile the
  `openbao` Kustomization.
- **After a wipe**: nothing to do — the Job re-seeds the whole subtree (path class in
  `openbao-recovery.md`); there are no logins against these paths, so nothing needs re-minting.

Editing the seeds safely (never through the terminal): from the **main checkout**, current on main —
a stale `.sops.yaml` is how the 2026-08-31 plaintext near-miss happened —

```bash
export SOPS_AGE_KEY_FILE="$(pwd)/kubernetes/infra/_out/age.agekey"
sops --config .sops.yaml edit kubernetes/apps/infrastructure/security/openbao/estate-seeds.sops.yaml
```

## Reading a value (operator ceremony)

Same TLS setup as the dev-worker mint ceremony (`openbao-dev-workers.md` §Activation (e)): the
`openbao` Service port-forward + `BAO_TLS_SERVER_NAME=openbao.openbao.svc`, or the LAN NodePort
`https://openbao.lan.chifor.me:30820` from a host that trusts ailab-root-ca. Authenticate with the
breakglass token **inline, never exported, never echoed**:

```bash
BAO_TOKEN="$(kubectl --context admin@ai -n openbao get secret openbao-breakglass-token \
  -o jsonpath='{.data.root_token}' | base64 -d)" \
  bao kv get -mount=af -field=api_token estate/proxmox | <consumer>
```

Pipe the field straight into its consumer; `wc -c` it if you only need to confirm it exists.

## Workstation cleanup ceremony (ADR 0021 Phase 5)

**Deliberately not automated.** Every step here deletes or revokes key material, and several depend
on a judgement no playbook can make. Run it **after** confirming the new paths are live in the vault
(`estate provision complete` in the Job log, and the pass-1/pass-2 matrix green).

**Escrow does not remove the original.** Every source file listed in the table above still exists on
the workstation after seeding, so it still needs a mode and still counts as a credential home.

### 1. Tighten what stays

```bash
chmod 700 ~/work/keys
chmod 600 ~/work/keys/* ~/work/keys.txt \
          ~/.kube/config ~/.kube/ailab.config \
          ~/.git-credentials ~/.gitea_tok ~/.cc_gitea_issue_token \
          ~/work/home/ProxmoxApiToken.md
chmod 600 ~/work/home/ailab/.env
find ~/work/home/ailab -name '*.tfvars' -exec chmod 600 {} +
```

All of the above were **0644** as of 2026-09-10.

### 2. Delete the redundant copies — each with its own precondition

| File | Precondition — do NOT skip | Then |
|---|---|---|
| `~/work/keys/kubeconfig.txt` | Confirm it is still byte-identical to `~/.kube/ailab.config` (`sha256sum` both). It is a **cluster-admin** credential. | Delete. Recovery is `talosctl kubeconfig`, not this file. |
| `~/work/keys/age.agekey` | Confirm a recoverable **offline** copy of the SOPS age key exists (removable media / password manager). `kubernetes/infra/_out/age.agekey` is on the *same disk* and gitignored — `.gitignore` is not access control, and a same-disk copy is not a backup. | Delete the `~/work/keys` copy only. |
| `~/work/keys/talos-backup-age.key` | **Orphaned — see the warning above.** First establish whether any retained snapshot was encrypted to `age1te3n4lf…`. Separately, verify an offline copy of the *live* key (`kubernetes/infra/_out/talos-backup-age.key`, public `age13ruz38k…`) exists. | Delete only once both are answered. |
| `~/.gitea_cred_tmp`, `~/.cutover_cookie`, `~/.cutover_sess_secret`, `~/.cutover_dump_name` | Grep the estate for each name; these look like 2026-08 cutover scratch. | Delete individually. |

### 3. Reconcile the two Gitea tokens

`~/.gitea_tok` and `~/.cc_gitea_issue_token` hold **different** values (sha256 `bdeea7df…` vs
`3b19e2e6…`). Different is not redundant. In Gitea, identify each token's owner and scopes, and
compare both against the shared `dev_worker_gitea_token`. Update every consumer **before** revoking
anything.

### 4. Rotate what the audit exposed

The reconciliation in ADR 0021 Step 0 had to read `~/work/keys.txt`, and its Google
(`GOCSPX-…`) and GitHub OAuth client secrets were printed to a terminal in the process. They were
already sitting at mode 0644 in plaintext, but treat both as **exposed and due for rotation** — in
the provider console, then in `estate-seeds.sops.yaml` in the same change (seed-wins: rotating one
without the other is reverted within a day).

### 5. Delete `SSO_PASSWORD` from `.env`

Confirm with a repo-wide grep that it still has zero consumers, then remove it rather than escrowing
it.

## Known gaps / follow-ups

- Consumers still read `.env`/tfvars/ansible-SOPS; migrating them (e.g. tofu reading the Proxmox
  token via the vault provider, scripts via a helper) is the second half of this work.
- The Proxmox api_token exists in five tfvars copies; collapsing those to one sourced location
  would shrink the rotation surface from six edits to two.
- The pve/cloud GPU cluster (cloud1–3, 192.168.0.20–.22) has no credential here — it is outside the
  estate's IaC entirely (no tofu root, no ansible group); onboarding it is its own task.
- `.env` `SSO_PASSWORD` has zero consumers in the repo and is not in `.env.example` — confirm and
  delete rather than escrow.
- `auth/userpass` on the vault carries a `root` user (observed 2026-08-31, origin undocumented —
  presumably a 2026-08-30 re-bootstrap artifact). Confirm it is intentional and document or remove
  it; an undocumented login path to the vault defeats the breakglass accounting.
