# Runbook — AgentForge v2 P1 control-plane (agentforge-platform) activation

Activates the `agentforge-platform` control plane (ADR 0019) at `https://agentforge.chifor.me`:
OIDC login → create a Workspace → the CP commits a CR to `cchifor/agentforge-tenants` → Flux
materializes the tenant. This is distinct from `agentforge-activation.md` (the broader P2-unlock
stack: OpenBao/ESO/KEDA/Kata). Plan: `plans/2026-07-22-agentforge-p1-activate-plan.md` (codex-reviewed).

The GitOps scaffolding (DB roles+DSNs, OIDC client, RBAC/SA/Service/NetworkPolicy/admission,
cloudflared route) is already merged. This runbook covers what remains, split across **two PRs** so
activation is a transactional switch:

> **Freshness note:** `- deployment.yaml` is **already merged into `apps/agentforge/kustomization.yaml`
> on `main`** — the PR-A/PR-B split below describes how the two pending changes were originally staged;
> re-check the kustomization before assuming PR-B (go-live) is still unmerged.

- **PR-A (prerequisites, safe to merge anytime):** pins the CP image digest in `deployment.yaml` +
  `db-migrate.yaml` (+ the optional CNPG init-container digest), switches the Deployment readiness
  probe to `/readyz`, and seeds `af:tenant-zero:owner` onto the owner in `authelia-secret.sops.yaml`.
  `deployment.yaml` is **still excluded** from the kustomization, so **merging PR-A deploys nothing.**
- **PR-B (go-live switch):** the single line `- deployment.yaml` in
  `apps/agentforge/kustomization.yaml`. **Do NOT merge PR-B until** the bot tokens are minted + the
  DB is created + migrated (steps 2–4). Merging PR-B is what brings the CP up.

`/readyz` runs `SELECT 1` on the admin DSN, so the Deployment (under the wait:true `apps`
Kustomization) only reports Ready once the DB is reachable + migrated — the real go-live gate.
`/healthz` is unconditional and is used for liveness only.

Pinned image: `registry.chifor.me/agentforge/agentforge-platform@sha256:e8cce2ecbf14695796d5cd3f86daf6306f404174e0935187d558366803259094`
(tag `276ccad` = agentforge-platform `origin/main` HEAD `276ccad857…`, PR #18). This is whatever is
CURRENTLY pinned in `deployment.yaml` — re-check there if this note has gone stale; step 0 below
self-defaults off the live manifest so it never needs to be re-verified against a copy-pasted digest.

All `kubectl` uses `--context admin@ai` (or `KUBECONFIG=kubernetes/infra/_out/kubeconfig`).

---

## Ordered activation steps

### 0. Re-verify the pinned image tag still resolves to the approved digest (fail closed)

```sh
just pin-verify agentforge-platform 276ccad
# scripts/verify-image-digest.sh: HEADs the registry manifest and PASS/FAILs against the digest
# self-defaulted from the CURRENT deployment.yaml pin (no copy-pasted digest to go stale here — pass
# an explicit 3rd arg, e.g. `just pin-verify agentforge-platform 276ccad sha256:...`, to check against
# something other than what's live-pinned).
# If it moved, re-pin deployment.yaml + db-migrate.yaml to the new digest (re-verify provenance) first.
```

### 1. Merge PR-A (prerequisites)

Safe: no CP is deployed (deployment.yaml still excluded). Flux applies the image pins (inert until
PR-B), the `/readyz` probe change (inert), and the Authelia owner-group seed.

```sh
flux --context admin@ai reconcile source git flux-system
flux --context admin@ai reconcile kustomization apps
# roll Authelia so it reloads the file-based user DB with the new group:
kubectl --context admin@ai -n auth rollout restart deploy/authelia
kubectl --context admin@ai -n auth rollout status  deploy/authelia
```

### 2. Mint the two Gitea bot tokens + fill the SOPS secret (GATED)

> Gitea PATs are **user+scope**, not per-repo, so per-repo isolation = **dedicated restricted bot
> users**. `agentforge-runtime.sops.yaml` ships with the two token values as **placeholders**;
> replace them with freshly-minted tokens. This mutates Gitea and was intentionally NOT run headless
> (the auto-mode classifier blocks in-pod user creation). **PR-B must not merge until this is done.**

Create the users + grant the tenants-repo collaborator (run inside the gitea pod). Emits only
non-secret status; it mints a transient site-admin token to add the collaborator and revokes it:

```sh
kubectl --context admin@ai -n gitea exec -i deploy/gitea -- sh <<'SH'
set -u
API=http://localhost:3000/api/v1
mk(){ u="$1"; gitea admin user list 2>/dev/null | awk '{print $2}' | grep -qx "$u" && { echo "$u EXISTS"; return; }
  gitea admin user create --restricted --username "$u" --email "$u@bots.local" \
    --random-password --must-change-password=false >/dev/null 2>&1 && echo "$u CREATED" || echo "$u CREATE-FAIL"; }
mk agentforge-cp-bot
mk agentforge-bootstrap-bot
ADMTOK=$(gitea admin user generate-access-token --raw -u gitea_admin -t afp-collab-tmp --scopes all 2>/dev/null)
curl -s -o /dev/null -w 'collab-add HTTP %{http_code}\n' -X PUT -H "Authorization: token $ADMTOK" \
  -H 'Content-Type: application/json' -d '{"permission":"write"}' \
  "$API/repos/cchifor/agentforge-tenants/collaborators/agentforge-cp-bot"
curl -s -H "Authorization: token $ADMTOK" "$API/repos/cchifor/agentforge-tenants/collaborators" \
  | tr ',' '\n' | grep -E '"login"|"permission"'
curl -s -o /dev/null -w 'adm-token-revoke HTTP %{http_code}\n' -X DELETE \
  -H "Authorization: token $ADMTOK" "$API/users/gitea_admin/tokens/afp-collab-tmp"
SH
```

Mint the two scoped tokens into **mode-600 files outside the repo** (never echo the value / never in
argv). `--raw` prints only the token:

```sh
umask 077
kubectl --context admin@ai -n gitea exec deploy/gitea -- \
  gitea admin user generate-access-token --raw -u agentforge-cp-bot \
  -t cp-tenants --scopes write:repository > /tmp/.afp_cp_tok
kubectl --context admin@ai -n gitea exec deploy/gitea -- \
  gitea admin user generate-access-token --raw -u agentforge-bootstrap-bot \
  -t bootstrap-labels --scopes write:issue > /tmp/.afp_boot_tok
```

Fill `kubernetes/apps/apps/agentforge/agentforge-runtime.sops.yaml` WITHOUT putting the token on a
command line (build a plaintext copy from the token files, then encrypt in place):

```sh
export SOPS_AGE_KEY_FILE=kubernetes/infra/_out/age.agekey
F=kubernetes/apps/apps/agentforge/agentforge-runtime.sops.yaml
sops --decrypt "$F" > /tmp/.afp_rt.yaml
python - <<'PY'
import yaml
p="/tmp/.afp_rt.yaml"; d=yaml.safe_load(open(p,"rb")); sd=d["stringData"]
sd["AFP_TENANTS_BOT_TOKEN"]=open("/tmp/.afp_cp_tok").read().strip()
sd["AFP_BOOTSTRAP_TOKEN"]=open("/tmp/.afp_boot_tok").read().strip()
open(p,"wb").write(yaml.safe_dump(d,sort_keys=False,allow_unicode=True).encode())
PY
cp /tmp/.afp_rt.yaml "$F"
sops --encrypt --in-place "$F"
rm -f /tmp/.afp_cp_tok /tmp/.afp_boot_tok /tmp/.afp_rt.yaml
git diff --stat "$F"     # confirm only this file; values are ENC[...]
```

Commit this `agentforge-runtime.sops.yaml` change **onto the PR-B branch itself** (the same PR as the
`- deployment.yaml` line) so the tokens and the Deployment merge **atomically**. This is required:
the pod captures `AFP_TENANTS_BOT_TOKEN`/`AFP_BOOTSTRAP_TOKEN` as env at startup, so it must never
start with placeholders (with placeholders `/readyz` still passes — it only checks the DB — but
create-workspace→tenants-commit then fails on a bad token). A live `kubectl` edit of the live Secret
is NOT a substitute — Flux reverts it to the committed ciphertext on the next reconcile.

Negative checks (least privilege): both bots `restricted` + non-admin; `agentforge-cp-bot` is a
**write** collaborator on **only** `cchifor/agentforge-tenants` (no write to `cchifor/ailab`, no
repo/org create); `agentforge-bootstrap-bot` has **no** repo write (its per-workspace collaborator
grant is added when a workspace repo is connected — bootstrap is off the create→commit path).

### 3. Create the `agentforge_platform` database (roles already exist)

`postInitSQL` does not run on the already-bootstrapped infra-pg; `managed.roles` already created
`afp_admin`/`afp_app`. Only the DB is missing. `scripts/af-db.sh init` resolves the **current
primary** from cluster status (never assumes `infra-pg-1`), pipes the `agentforge-db-bootstrap`
ConfigMap's `bootstrap.sql` through the idempotent `\gexec` one-shot as the peer-auth superuser, then
verifies the DB exists + `afp_admin` BYPASSRLS + `afp_app` NOBYPASSRLS:

```sh
just af-db-init
```

### 4. Run the schema/RLS migration (before PR-B, so no post-go-live missing-table errors)

`scripts/af-db.sh migrate` deletes + re-applies `db-migrate.yaml`, waits for completion (dumping the
last 40 log lines and exiting non-zero on failure), then prints `alembic_version` and verifies
`pg_class.relforcerowsecurity` is set on every RLS table:

```sh
just af-db-migrate
```

### 5. Merge PR-B (go-live) and verify

```sh
flux --context admin@ai reconcile source git flux-system
flux --context admin@ai reconcile kustomization apps
just af-cp-smoke
```

`scripts/af-cp-smoke.sh` waits for the rollout, asserts the running pod's `imageID` digest matches the
pin in `deployment.yaml`, hits the in-pod `/readyz`, and hits the external `/healthz` over the
cloudflared tunnel — one PASS/FAIL summary, non-zero on any failure. (Uses `AF_KUBE_CONTEXT` to
override `kubectl --context`; empty = current context, i.e. `admin@ai` if that's already selected.)

End-to-end (browser): `https://agentforge.chifor.me` loads → OIDC login (chifor) → `GET /api/me`
shows `tenant-zero: owner` → create a uniquely-named disposable Workspace → a commit appears under
`tenants/` in `cchifor/agentforge-tenants` → the `agentforge-tenants` Flux Kustomization materializes
the tenant namespace. Then delete the test workspace + its tenants-repo commit to leave no drift.

---

## Rollback

- **CP:** revert PR-B (remove `- deployment.yaml`) → Flux prunes the Deployment; SA/Service/RBAC
  remain (harmless).
- **Tokens:** in the gitea pod, `gitea admin user delete --username agentforge-cp-bot` /
  `agentforge-bootstrap-bot` (revokes their PATs), or `DELETE /api/v1/users/<user>/tokens/<name>`.
  Restore the placeholders in the SOPS secret.
- **Authelia group:** revert the `authelia-secret.sops.yaml` change and roll Authelia.
- **DB:** leave `agentforge_platform` in place unless a reviewed schema teardown exists; the
  `afp_admin`/`afp_app` roles are shared-managed — do not drop.

## Notes / gotchas

- ailab pushes go to the `gitea` remote (`git.chifor.me/cchifor/ailab`); Flux reconciles from
  in-cluster Gitea. `origin` (GitHub) is a backup mirror.
- The CP fails **shut** on an empty OIDC `groups` claim — the `af:tenant-zero:owner` seed (step 1)
  is what lets the owner in. Org rows auto-provision on first login from the groups claim.
- Access-FREE by design (ADR 0019): the CP does its own Authelia OIDC; no Cloudflare Access app.

## Day-2 — LLM subscriptions operations (WS4, 2026-07-24)

The Settings → Subscriptions page drives these; the CP holds NO delete/read-data privilege, so the
following stay OPERATOR steps:

- **Rotate (paste a new token/auth.json)**: fully self-service in the UI (CAS write → wait for the
  ESO sync (≤1h) → broker `credential_generation` match). To force an immediate sync:
  `kubectl --context admin@ai -n agentforge-broker annotate externalsecret <name> force-sync=$(date +%s) --overwrite`.
- **Refresh now (codex only)**: UI button creates a Job from CronJob `af-codex-refresh` (VAP-pinned).
- **Add account**: UI CAS-writes the cred, then opens an ailab PR via `agentforge-infra-bot`
  (AGit). Review + approve (reviewer-bot) + merge per the protected-main flow; the operation
  tracker advances to `active` once Flux/ESO/broker all report.
- **Remove account**: UI blocks while any workspace config or deployed render references the
  account, then opens the manifest-removal PR. AFTER merge+prune, the KV soft-delete is manual:
  `bao kv delete -mount=af operator/broker/<provider>/<account>/oauth` (provisioner token cannot
  and should not do this — deliberate).
- **Claude expiry**: not derivable in-cluster (opaque ~1yr `claude setup-token`). Keep the
  operator expiry note in the UI current when you rotate.
- **Bot/token inventory**: `agentforge-infra-bot` (READ on ailab; token = SOPS
  `AFP_INFRA_BOT_TOKEN`, ns agentforge) · `agentforge-reviewer-bot` (write collaborator, approvals
  only) · `agentforge-cp-bot`/`agentforge-bootstrap-bot` (tenants commits / label bootstrap).
  Rotate any of them with `gitea admin user generate-access-token` in the gitea pod + SOPS update.
