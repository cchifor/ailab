# Runbook — AgentForge v2 P1 control-plane (agentforge-platform) activation

Activates the `agentforge-platform` control plane (ADR 0019) at `https://agentforge.chifor.me`:
OIDC login → create a Workspace → the CP commits a CR to `cchifor/agentforge-tenants` → Flux
materializes the tenant. This is distinct from `agentforge-activation.md` (the broader P2-unlock
stack: OpenBao/ESO/KEDA/Kata). Plan: `plans/2026-07-22-agentforge-p1-activate-plan.md` (codex-reviewed).

The GitOps scaffolding (DB roles+DSNs, OIDC client, RBAC/SA/Service/NetworkPolicy/admission,
cloudflared route) is already merged. This runbook covers what remains, split across **two PRs** so
activation is a transactional switch:

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

Pinned image: `registry.chifor.me/agentforge/agentforge-platform@sha256:85a4a3c7a3599b20834688c8f2ea060341435d7cba07239d94bf5b00afac374e`
(tag `2776074` = agentforge-platform `origin/main` HEAD `27760744124eb1a800afe5b4b87d06f009d35d3f`).

All `kubectl` uses `--context admin@ai` (or `KUBECONFIG=kubernetes/infra/_out/kubeconfig`).

---

## Ordered activation steps

### 0. Re-verify the pinned image tag still resolves to the approved digest (fail closed)

```sh
curl -sS -o /dev/null -D - \
  -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://registry.chifor.me/v2/agentforge/agentforge-platform/manifests/2776074 \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}'
# MUST equal sha256:85a4a3c7a3599b20834688c8f2ea060341435d7cba07239d94bf5b00afac374e
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
`afp_admin`/`afp_app`. Only the DB is missing. Resolve the **current primary** from cluster status
(do not assume `infra-pg-1`) and run the idempotent `\gexec` one-shot as the peer-auth superuser:

```sh
PRIMARY=$(kubectl --context admin@ai -n databases get cluster infra-pg -o jsonpath='{.status.currentPrimary}')
kubectl --context admin@ai -n agentforge get cm agentforge-db-bootstrap -o jsonpath='{.data.bootstrap\.sql}' \
| kubectl --context admin@ai -n databases exec -i "$PRIMARY" -- psql -v ON_ERROR_STOP=1
# verify (expect: agentforge_platform|afp_admin):
kubectl --context admin@ai -n databases exec "$PRIMARY" -- \
  psql -tAc "select datname, pg_catalog.pg_get_userbyid(datdba) owner from pg_database where datname='agentforge_platform'"
```

### 4. Run the schema/RLS migration (before PR-B, so no post-go-live missing-table errors)

```sh
kubectl --context admin@ai -n agentforge delete job agentforge-db-migrate --ignore-not-found
kubectl --context admin@ai apply -f kubernetes/apps/apps/agentforge/db-migrate.yaml
kubectl --context admin@ai -n agentforge wait --for=condition=complete --timeout=180s job/agentforge-db-migrate \
  || kubectl --context admin@ai -n agentforge logs job/agentforge-db-migrate --tail=50
# verify alembic head (0002_cluster_enrollments) + RLS forced:
kubectl --context admin@ai -n databases exec "$PRIMARY" -- psql -d agentforge_platform -tAc "select version_num from alembic_version"
kubectl --context admin@ai -n databases exec "$PRIMARY" -- psql -d agentforge_platform -tAc \
  "select relname, relrowsecurity, relforcerowsecurity from pg_class where relrowsecurity and relnamespace='public'::regnamespace order by 1"
```

### 5. Merge PR-B (go-live) and verify

```sh
flux --context admin@ai reconcile source git flux-system
flux --context admin@ai reconcile kustomization apps
kubectl --context admin@ai -n agentforge rollout status deploy/agentforge-platform
# assert the running pod is the approved digest:
kubectl --context admin@ai -n agentforge get pod -l app.kubernetes.io/name=agentforge-platform \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
# in-cluster readiness (DB reachable):
kubectl --context admin@ai -n agentforge exec deploy/agentforge-platform -- wget -qO- http://localhost:8080/readyz; echo
```

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
