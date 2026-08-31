# Runbook: estate credentials in OpenBao (`af/estate/*`)

The operator-plane infrastructure credentials — Proxmox, QNAP, Cloudflare, the zot registry, Gitea
runner registration, the GitHub App key — escrowed in OpenBao with the same seed-wins durability
contract as the AgentForge operator paths. Backfilled 2026-08-31 from the full credential audit.
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

**Access:** no policy grants `estate/*` to anything — not the dev-worker AppRoles, not ESO. The
dev-worker `cred` helper gets a permission error here by design. Readers are root-level tokens only
(the ceremony below). Widening access (e.g. a scoped operator token, an ESO consumer) is a
deliberate follow-up decision, not a default.

## How it converges

The daily **`openbao-estate-provision`** Job (`kubernetes/apps/infrastructure/security/openbao/estate-provision-job.yaml`)
authenticates with the breakglass token and `bao kv patch`es each `af/estate/<name>` from the
`<name>.json` keys of Secret `openbao-estate-seeds` (`estate-seeds.sops.yaml`, SOPS in git).

**Seed wins.** A key present in the seed overwrites live KV on every run; a key absent from the seed
survives. So:

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
