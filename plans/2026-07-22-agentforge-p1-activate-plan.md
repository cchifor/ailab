# AgentForge v2 P1 control-plane activation (agentforge-platform)

## Codex review round 1 — dispositions (finalized)

Codex (gpt-5.6-sol, plan-review) raised 5 highs + several markers. Dispositions:

- **ACCEPT — transactional ordering (blocker):** split into TWO stacked PRs. **PR-A
  (prerequisites)** = image-digest pins + both bot tokens + the Authelia owner-group seed, with
  `deployment.yaml` still EXCLUDED from the kustomization (so nothing goes live on merge).
  **PR-B (go-live)** = the single `- deployment.yaml` line, held until the DB is created +
  migrated + verified. If token minting fails, PR-B stays un-mergeable (the fallback gates the
  whole activation, not just token population).
- **ACCEPT — /healthz false-readiness:** change the Deployment **readinessProbe to `/readyz`**
  (the DB `SELECT 1` check) and keep **livenessProbe on `/healthz`** (process-alive). Standard
  split: a DB blip removes the pod from endpoints without killing it. Combined with "DB created
  before PR-B merges", the pod reports Ready only when it can actually serve.
- **PUSHBACK→resolved (separate Flux Kustomization):** codex's "preferred" isolation (a dedicated
  `agentforge-platform` Flux Kustomization dependsOn `databases`) is over-engineering for a P1
  single-namespace CP; codex's own "minimum acceptable alternative" (activation in a separate
  draft PR) is adopted via the PR-A/PR-B split + `/readyz` readiness + DB-before-go-live ordering.
- **ACCEPT — image provenance:** verified before pinning — tag `2776074` → digest
  `85a4a3c7…` (equality MATCH), OCI index with a real `linux/amd64` manifest, config
  `Entrypoint=[agentforge-platform] Cmd=[serve --port 8080]` (matches Dockerfile), `AFP_WEBAPP_DIST`
  set (SPA bundled). Full source SHA recorded: `27760744124eb1a800afe5b4b87d06f009d35d3f`
  (origin/main HEAD). OCI `image.revision` labels are NOT set by the platform CI — binding rests on
  the CI's tag==commit-sha convention + verified tag→digest; re-checked immediately before commit
  and to be re-checked before the PR-B merge (fail closed if the tag moved).
- **PUSHBACK→resolved (`--restricted` "undocumented"):** verified against the INSTALLED binary —
  `gitea admin user create --help` lists `--restricted   Make a restricted user account`. Also add
  `--email` (bots.local) + `--random-password` (suppress stdout). Scope names confirmed valid on
  the installed Gitea (help examples show `write:repository`, `read:issue`).
- **ACCEPT — CNPG init-container mutable tag:** pin the (optional/unused) `db-migrate.yaml`
  bootstrap init container `ghcr.io/cloudnative-pg/postgresql:17.10` →
  `@sha256:916d505d999e6bb85ead15dc2b965b9f968a9eb09e6d9c2e59e50a174f785e71` (the digest infra-pg
  runs live).
- **ACCEPT — Authelia seed is a GitOps file, not a live edit:** a live `kubectl edit` of the
  Flux-owned `authelia-secret.sops.yaml` would be reverted on the next reconcile. Move it INTO
  PR-A as a committed SOPS change (add `af:tenant-zero:owner` to `chifor`), applied by Flux on
  merge; the only live step is rolling `deploy/authelia`. (Deviates from the coordinator's "stage
  the OIDC group" — flagged in the report; this is the Flux-correct way.)
- **ACCEPT — secret hygiene:** mint via `generate-access-token --raw` redirected to a `umask 077`
  file OUTSIDE the repo (scratchpad), never in argv/env; build the plaintext Secret YAML from those
  files and `sops --encrypt` it (token value never on a command line); `rm` the temp files (shred
  is moot — the token is committed encrypted-at-rest and is revocable). Verify the committed secret
  is all `ENC[...]` and carries no plaintext token, via a non-leaking check (lengths/one-way hashes
  only) + `git status --porcelain` + staged-diff grep.
- **ACCEPT — runbook hardening:** discover the CNPG primary by label (not hard-coded `infra-pg-1`);
  `kubectl delete job --ignore-not-found` preflight before re-applying the migration Job; verify
  `alembic_version` == head + expected tables + RLS enabled/forced (not just Job `complete`); use a
  uniquely-named disposable test workspace with documented cleanup; add `/readyz==200` + pod
  `imageID` == approved digest checks; add rollback (revoke PATs, remove collaborator, remove/suspend
  Deployment).

## Context

ADR 0019's P1 control plane (`agentforge-platform`, ns `agentforge`) is almost fully scaffolded
and merged on `main` (verified live on the `ai` cluster). The remaining work is the **activation
gap**: pin the real image, wire the Deployment, and fill the two placeholder bot tokens — so that
once merged + the staged operator steps run, a user can OIDC-login at `https://agentforge.chifor.me`
and create a Workspace whose CR commits to `cchifor/agentforge-tenants` and is materialized by Flux
(plain manifests; kro is P2). Live facts below were re-confirmed immediately before implementation
and MUST be re-checked before the go-live merge (they can drift).

**Already done + merged + verified live (re-check before migration; can drift):**
- infra-pg: `afp_admin` (LOGIN BYPASSRLS) + `afp_app` (LOGIN NOBYPASSRLS) managed roles live;
  storage 10Gi; `infra-pg-afp-admin`/`infra-pg-afp-app` SOPS secrets real. DB `agentforge_platform`
  ABSENT (reconfirmed) → created in the staged runbook.
- `agentforge-db.sops.yaml`: real `AFP_APP_DSN`/`AFP_ADMIN_DSN` (asyncpg → `infra-pg-rw`, db
  `agentforge_platform`); DSN passwords verified byte-coupled to the managed-role secrets.
- `agentforge-oauth.sops.yaml`: real `AFP_OIDC_CLIENT_SECRET`; matching pbkdf2 hash already in
  `auth/authelia-config.yaml` client `agentforge` (PKCE S256, redirect `.../api/auth/callback`,
  scopes openid/profile/groups/email).
- `agentforge-runtime.sops.yaml`: `AFP_SESSION_SECRET` real (64-hex).
- RBAC, SA, Service (:8080), NetworkPolicy (tunnel-only), admission VAPs — merged/live.
- `edge/cloudflared.yaml`: `agentforge.chifor.me` → `agentforge-platform.agentforge.svc:8080`
  present. Access-FREE by design (ADR 0019 §Exposure).
- `cchifor/agentforge-tenants` Gitea repo exists; Flux `GitRepository` READY; per-tenant
  `Kustomization` present.

**App facts (agentforge-platform source @ `27760744…`, verified against the built image):**
- Env prefix `AFP_`; `deployment.yaml` env matches `settings.py`.
- CLI: `serve` (default) + `migrate` (= `alembic upgrade head` as admin_dsn). Alembic revisions
  `0001_initial`, `0002_cluster_enrollments`.
- `/healthz` = unconditional `{"status":"ok"}` (liveness); `/readyz` = DB `SELECT 1` (readiness).
- Org row auto-provisions on OIDC login (`sync_principal`→`create_org` from `af:<org>:<role>`),
  IFF the owner carries `af:tenant-zero:owner` (else the CP fails shut).
- `committer` (tenants token) → contents-API PUT on `cchifor/agentforge-tenants` only;
  `bootstrapper` (bootstrap token) → labels on WORKSPACE repos; OFF the create→commit path
  (`create_workspace` uses only the tenants token).

## Approach

### PR-A — prerequisites (branch `feat/agentforge-p1-activate`, push to `gitea`)

1. **Pin the platform image** in `deployment.yaml` + `db-migrate.yaml` (migrate container):
   `registry.chifor.me/agentforge/agentforge-platform@sha256:85a4a3c7a3599b20834688c8f2ea060341435d7cba07239d94bf5b00afac374e`.
   Also pin the `db-migrate.yaml` bootstrap init container to the CNPG digest above. Leave
   `AFP_WORKER_IMAGE` untouched. `deployment.yaml` stays OUT of the kustomization.
2. **Readiness → `/readyz`** in `deployment.yaml` (liveness stays `/healthz`).
3. **Fill both bot tokens** in `agentforge-runtime.sops.yaml` (preserve `AFP_SESSION_SECRET`).
4. **Seed `af:tenant-zero:owner`** onto `chifor` in `authelia-secret.sops.yaml` (`users_database.yml`;
   current groups `['admins','openwebui-admin']`), re-encrypt.

### PR-B — go-live switch (branch `feat/agentforge-p1-go-live`, stacked on PR-A)

5. Add `- deployment.yaml` to `apps/agentforge/kustomization.yaml` (the only change). Draft/hold
   until DB created + migrated + verified.

### Token minting (live Gitea; reversible; off shared infra-pg)

Dedicated restricted bot users (PATs are user+scope, not per-repo):
- **`agentforge-cp-bot`**: `gitea admin user create --restricted --email agentforge-cp-bot@bots.local
  --random-password --must-change-password=false --username agentforge-cp-bot` → add write
  collaborator on ONLY `cchifor/agentforge-tenants` → verify collaborator grant → mint token scope
  `write:repository`.
- **`agentforge-bootstrap-bot`**: same restricted create, NO repo grant → mint token scope
  `write:issue`. Per-workspace collaborator grant is a documented per-workspace step.

Rollback (record token NAMES, never values): revoke by token name, remove collaborator, delete the
bot user.

### Staged operator runbook (item 4 — NOT executed headless)

Appended to scratchpad `HEADLESS-PENDING.md`, ordered vs the user-gated merges:
1. Discover primary (`-l cnpg.io/instanceRole=primary`); create `agentforge_platform` DB as
   superuser peer-auth `\gexec` (idempotent; roles exist). If DB already exists, assert
   owner=afp_admin.
2. Merge **PR-A** → Flux applies tokens + authelia group; roll `deploy/authelia`.
3. `kubectl delete job agentforge-db-migrate --ignore-not-found` → `kubectl apply -f db-migrate.yaml`
   → wait complete → verify `alembic_version`==head + tables + RLS forced. (Runs before PR-B so a
   post-go-live workspace-create never hits missing tables.)
4. Merge **PR-B** → `flux reconcile source git flux-system` + `flux reconcile kustomization apps` →
   Deployment Ready on `/readyz`; assert pod `imageID`==approved digest.
5. Verify: `https://agentforge.chifor.me` loads; OIDC login; `GET /api/me` shows tenant-zero:owner;
   create a uniquely-named disposable Workspace → commit lands under `tenants/` in
   `cchifor/agentforge-tenants` → Flux materializes the tenant ns; then clean up the test workspace.

## Critical files

- `apps/agentforge/deployment.yaml` — image digest + readiness→/readyz (PR-A).
- `apps/agentforge/db-migrate.yaml` — migrate image digest + CNPG init-container digest (PR-A).
- `apps/agentforge/agentforge-runtime.sops.yaml` — both bot tokens (PR-A).
- `apps/auth/authelia-secret.sops.yaml` — owner group seed (PR-A).
- `apps/agentforge/kustomization.yaml` — add `- deployment.yaml` (PR-B).
- `<scratchpad>/HEADLESS-PENDING.md` — staged operator runbook.

## Verification

Static (headless):
- `kubectl kustomize apps/agentforge`: PR-A build UNCHANGED count (Deployment absent); PR-B build
  includes the Deployment with digest `85a4a3c7`; assert no `@sha256:0000`, no mutable tag in the
  migrate path, `AFP_WORKER_IMAGE` unchanged.
- Non-leaking secret check: `sops --decrypt` round-trips; token values non-placeholder
  (length/one-way hash only, never printed); `AFP_SESSION_SECRET` sha unchanged; committed file all
  `ENC[...]`; `git status --porcelain` clean; staged-diff carries no plaintext token / `_out/` / age key.
- Registry equality re-checked (tag 2776074==digest) at commit time and before PR-B merge.
- Gitea negative tests (post-mint, read-only): both bots restricted+non-admin; `agentforge-cp-bot`
  write on tenants repo only (no ailab write, no repo/org create); `agentforge-bootstrap-bot` no
  repo write.

Live (staged): DB create; migration Job complete + alembic head + RLS forced; authelia roll;
end-to-end login → create-workspace → tenants-commit → Flux-materialize; rollback documented.

<!-- codex-review-status: finalized -->
