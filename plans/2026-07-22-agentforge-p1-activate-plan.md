# AgentForge v2 P1 control-plane activation (agentforge-platform)

## Context

ADR 0019's P1 control plane (`agentforge-platform`, ns `agentforge`) is almost fully
scaffolded and merged on `main` (verified live on the `ai` cluster). The remaining work is
the **activation gap**: pin the real image, wire the Deployment, and fill the two placeholder
bot tokens — so that once merged + the staged operator steps run, a user can OIDC-login at
`https://agentforge.chifor.me` and create a Workspace whose CR commits to
`cchifor/agentforge-tenants` and is materialized by Flux (plain manifests; kro is P2).

**Already done + merged + verified live (NO action needed):**
- infra-pg: `afp_admin` (LOGIN BYPASSRLS) + `afp_app` (LOGIN NOBYPASSRLS) managed roles exist
  live; storage already 10Gi; `infra-pg-afp-admin`/`infra-pg-afp-app` SOPS secrets real.
- `agentforge-db.sops.yaml`: real `AFP_APP_DSN` + `AFP_ADMIN_DSN` (asyncpg → `infra-pg-rw`,
  db `agentforge_platform`); DSN passwords verified byte-coupled to the managed-role secrets.
- `agentforge-oauth.sops.yaml`: real `AFP_OIDC_CLIENT_SECRET`; matching pbkdf2 hash already in
  `auth/authelia-config.yaml` client block `agentforge` (PKCE S256, redirect
  `https://agentforge.chifor.me/api/auth/callback`, scopes openid/profile/groups/email).
- `agentforge-runtime.sops.yaml`: `AFP_SESSION_SECRET` real (64-hex).
- RBAC (`agentforge-cp-readonly` no-secrets + `agentforge-cp-flux-safeops`), SA + Service
  (`:8080`), NetworkPolicy (tunnel-only), admission VAPs — all merged/live.
- `edge/cloudflared.yaml`: `agentforge.chifor.me` → `agentforge-platform.agentforge.svc:8080`
  route already present. **Access-FREE by design** (ADR 0019 §Exposure: the CP does its own
  Authelia OIDC; a CF Access gate would double-login). No Cloudflare Access work.
- `cchifor/agentforge-tenants` Gitea repo exists; Flux `GitRepository` READY; per-tenant
  `Kustomization` present.

**App facts that constrain this plan (from `agentforge-platform` source @ origin/main `2776074`):**
- Settings env prefix `AFP_`; `deployment.yaml` env exactly matches `settings.py` fields.
- CLI: `serve` (default) + `migrate` (= `alembic upgrade head` as `admin_dsn`). Alembic revisions
  `0001_initial`, `0002_cluster_enrollments`.
- `/healthz` returns `{"status":"ok"}` **unconditionally — no DB touch** (both probes use it);
  `/readyz` is the DB check but nothing gates on it. So the pod goes Ready even before the DB is
  migrated → wiring the Deployment into the wait:true `apps` Kustomization will NOT wedge the
  layer on migration state.
- Org row auto-provisions on OIDC login (`sync_principal` → `create_org` from the `af:<org>:<role>`
  groups claim) — no manual org seed needed IF the owner carries `af:tenant-zero:owner`.
- `committer` (tenants token) does contents-API PUT on `cchifor/agentforge-tenants` only;
  `bootstrapper` (bootstrap token) does labels on WORKSPACE repos and must NOT write the tenants
  repo; it is an OFF-critical-path endpoint (create_workspace→commit uses only the tenants token).

## Approach

### Part 1 — GitOps changes (ailab branch `feat/agentforge-p1-activate`, push to `gitea`)

1. **Pin the platform image** in both `deployment.yaml` and `db-migrate.yaml`: replace the
   placeholder `@sha256:0000…0000` with
   `registry.chifor.me/agentforge/agentforge-platform@sha256:85a4a3c7a3599b20834688c8f2ea060341435d7cba07239d94bf5b00afac374e`
   — the manifest digest of tag `2776074`, which is `agentforge-platform` origin/main HEAD (feat
   #8, built 2026-07-22 13:29; newest of the 3 registry builds; verified via registry
   Docker-Content-Digest). Leave `AFP_WORKER_IMAGE` (p1-worker) untouched.

2. **Wire the Deployment** into `apps/agentforge/kustomization.yaml`: add `- deployment.yaml` to
   `resources` and update the header comment (drop the "intentionally not listed / placeholder
   digest" note, keep the db-migrate exclusion note). `db-migrate.yaml` stays OUT (operator
   one-shot; a failing migration Job under wait:true would wedge apps).

3. **Fill the two bot tokens** in `agentforge-runtime.sops.yaml` (SOPS-encrypt, preserve the real
   `AFP_SESSION_SECRET` unchanged): `AFP_TENANTS_BOT_TOKEN` and `AFP_BOOTSTRAP_TOKEN`. Tokens are
   minted in Part 2.

### Part 2 — Token minting (live Gitea; reversible; off the shared infra-pg)

PATs in Gitea are user+scope (not per-repo), so per-repo isolation = dedicated bot users:

- **`agentforge-cp-bot`** (tenants committer): `gitea admin user create --restricted --username
  agentforge-cp-bot --random-password --must-change-password=false` → add as **write collaborator
  on ONLY `cchifor/agentforge-tenants`** → verify the collaborator grant took (GET collaborators)
  → mint token **scope `write:repository`** (contents PUT needs write:repository). It is a
  restricted user with exactly one write repo, so the token cannot touch `cchifor/ailab`.
- **`agentforge-bootstrap-bot`** (workspace labels): same restricted create, **NO repo grant by
  default** → mint token **scope `write:issue`** (Gitea label create is under the issue unit). The
  per-workspace write-collaborator grant is a documented per-workspace step; until then the
  bootstrap endpoint 502s, which does not block the create→commit→materialize DoD.

Capture each token to a `umask 077` mode-600 file, never echo the value, SOPS-encrypt into
`agentforge-runtime.sops.yaml`, shred the temp files, and verify `sops --decrypt` round-trips +
`git check-ignore` is clean (no `_out/`, no plaintext) before commit.

If the permission classifier blocks headless user creation, fall back: leave the placeholders,
mark the token step **gated**, and document the exact create/collaborator/mint commands in the
runbook instead.

### Part 3 — Staged operator runbook (NOT executed headless; item 4)

Append an "Item 1 — agentforge-platform activation" section to the scratchpad
`HEADLESS-PENDING.md`, ordered relative to the user-gated merge:

1. Create the `agentforge_platform` DATABASE as superuser via peer-auth exec into `infra-pg-1`
   (`\gexec` idempotent one-shot from the `db-migrate.yaml` bootstrap ConfigMap; roles already
   exist, so only the DB is created). Read-only precheck: DB currently absent (`agentforge_broker`
   present, `agentforge_platform` not).
2. Run the migration one-shot: `kubectl apply -f apps/agentforge/db-migrate.yaml` then
   `kubectl -n agentforge wait --for=condition=complete job/agentforge-db-migrate` (alembic
   0001+0002). Uses the pinned image + `AFP_ADMIN_DSN` (already live).
3. Seed `af:tenant-zero:owner` onto user `chifor` in `authelia-secret.sops.yaml`
   (`users_database.yml`; current groups `['admins','openwebui-admin']`), re-encrypt, roll
   `deploy/authelia`.
4. Merge the ailab PR (user-gated) → `flux reconcile source git flux-system` + `flux reconcile
   kustomization apps` (flux source is behind gitea/main) → Deployment comes up Ready on /healthz.
5. Verify: `https://agentforge.chifor.me` loads; OIDC login; `GET /api/me` shows tenant-zero:owner;
   create a Workspace → confirm a commit lands under `tenants/` in `cchifor/agentforge-tenants` →
   Flux materializes the tenant namespace.

## Critical files

- `kubernetes/apps/apps/agentforge/deployment.yaml` — pin CP image digest.
- `kubernetes/apps/apps/agentforge/db-migrate.yaml` — pin migrate image digest (stays out of kustomization).
- `kubernetes/apps/apps/agentforge/kustomization.yaml` — add `- deployment.yaml`.
- `kubernetes/apps/apps/agentforge/agentforge-runtime.sops.yaml` — fill both bot tokens (SOPS).
- `<scratchpad>/HEADLESS-PENDING.md` — append the staged operator runbook (Part 3).

## Verification

Static (headless, pre-merge):
- `kubectl kustomize kubernetes/apps/apps/agentforge` builds clean and now includes the Deployment
  with the pinned digest (grep for `@sha256:85a4a3c7`, assert no `@sha256:0000`).
- `sops --decrypt agentforge-runtime.sops.yaml` round-trips; both token values are non-placeholder
  (length + not in the known-placeholder set) without printing them; `AFP_SESSION_SECRET`
  unchanged (sha unchanged).
- Registry re-confirm: `85a4a3c7…` resolves for tag `2776074` (Docker-Content-Digest) at check
  time (guard the stale-tag race).
- `git check-ignore` clean; no plaintext secret, no `_out/`, in the diff.
- Gitea (post-mint, read-only): GET `/repos/cchifor/agentforge-tenants/collaborators` includes
  `agentforge-cp-bot` (write); `agentforge-bootstrap-bot` has no repo write.

Live (staged in runbook, NOT run headless): DB create, migration Job complete, OIDC-group seed +
authelia roll, then the end-to-end login → create-workspace → tenants-commit → Flux-materialize
proof.

<!-- codex-review-status: pending -->
