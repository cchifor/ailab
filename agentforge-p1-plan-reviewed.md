# AgentForge v2 P1 control-plane activation (agentforge-platform)

## Codex Review

- Blocker: activation and prerequisites are not transactionally ordered. Keep the Deployment out of the first merge; merge secrets/OIDC changes, create and migrate the database, verify Alembic/RLS, then use a second activation PR to add the Deployment.
- High: `/healthz` makes the Deployment—and therefore the shared `wait: true` apps Kustomization—Ready without a usable database. Use `/readyz` for readiness in an isolated AgentForge Kustomization, retaining `/healthz` for liveness; otherwise make `/readyz` an explicit rollout gate. Flux evaluates Deployment rollout health when `wait: true` is enabled. [Flux Kustomization documentation](https://fluxcd.io/flux/components/kustomize/kustomizations/)
- High: the proposed digest check proves only a tag-to-digest observation, not that the image contains commit `2776074`. Verify the full Git SHA/build provenance, manifest media type, target platform, digest pullability, and tag equality immediately before merge. The optional migration init container also remains mutable at `:17.10`. [OCI Distribution Specification](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)
- High: the token scopes are plausible—labels fall under `write:issue`, contents PUT under `write:repository`—but must be checked against the installed Gitea version and its Swagger. The documented user-create CLI does not expose `--restricted`, and the proposed command omits required email/password handling. [Gitea token scopes](https://docs.gitea.com/development/oauth2-provider), [Gitea CLI](https://docs.gitea.com/1.25/administration/command-line)
- High: token minting must not expose credentials through headless command output, process arguments, environment variables, shell history, tool logs, or the worktree. `shred` is not reliable on SSD/CoW filesystems. If secure minting or encryption cannot be completed, activation—not merely token population—must remain gated.

## Context

ADR 0019's P1 control plane (agentforge-platform, ns agentforge) is almost fully scaffolded and merged on main. The remaining work is the activation gap: pin the real image, wire the Deployment, and fill the two placeholder bot tokens.

Already done + merged + verified live (NO action needed):
<!-- codex: critique — “NO action needed” is too absolute for live prerequisites. Recheck role attributes, Secret presence, Flux readiness, route state, and DSN authentication immediately before migration because these facts can drift after plan authoring. -->
- infra-pg: afp_admin (LOGIN BYPASSRLS) + afp_app (LOGIN NOBYPASSRLS) managed roles exist live
- agentforge-db.sops.yaml: real AFP_APP_DSN + AFP_ADMIN_DSN
- agentforge-oauth.sops.yaml: real AFP_OIDC_CLIENT_SECRET with matching pbkdf2 hash
- agentforge-runtime.sops.yaml: AFP_SESSION_SECRET real (64-hex)
- RBAC, SA, Service, NetworkPolicy, admission VAPs all merged/live
- edge/cloudflared.yaml: agentforge.chifor.me route already present
- cchifor/agentforge-tenants Gitea repo exists; Flux GitRepository READY

App facts (from agentforge-platform source @ origin/main 2776074):
<!-- codex: critique — Use and record the full commit SHA. A seven-character SHA and “origin/main” are mutable/ambiguous identifiers and are insufficient to bind the deployed artifact to reviewed source. -->
- Settings env prefix AFP_; deployment.yaml env matches settings.py fields
- CLI: serve (default) + migrate (alembic upgrade head as admin_dsn)
- /healthz returns {"status":"ok"} unconditionally — no DB touch (readiness+liveness use it)
- /readyz is the DB check but nothing gates on it; pod goes Ready before DB migrated
<!-- codex: critique — This is the central unsafe assumption. Kubernetes and Flux will report a successful rollout while every DB-backed path can fail. Keep /healthz for liveness, but either change readiness to /readyz in a dedicated AgentForge Kustomization or require an explicit /readyz check before declaring activation complete. -->
- Org row auto-provisions on OIDC login (sync_principal -> create_org from groups claim)
- committer (tenants token) does contents-API PUT on cchifor/agentforge-tenants only
- bootstrapper (bootstrap token) does labels on WORKSPACE repos only; OFF-critical-path endpoint
<!-- codex: critique — Verify that bootstrap failure truly cannot roll back or fail workspace creation in the exact pinned image; source-level intent is not sufficient for this end-to-end failure mode. -->

## Approach

### Part 1 — GitOps changes (ailab branch feat/agentforge-p1-activate, push to gitea)

1. Pin the platform image in both deployment.yaml and db-migrate.yaml: replace @sha256:0000...0000 with registry.chifor.me/agentforge/agentforge-platform@sha256:85a4a3c7a3599b20834688c8f2ea060341435d7cba07239d94bf5b00afac374e — the manifest digest of tag 2776074 (agentforge-platform origin/main HEAD, feat #8, built 2026-07-22 13:29; newest of 3 registry builds; verified via Docker-Content-Digest). Leave AFP_WORKER_IMAGE (p1-worker) untouched.
<!-- codex: critique — “Newest of 3” is not a correctness criterion, and Docker-Content-Digest alone does not prove source provenance. Resolve with an OCI-aware client using the correct Accept media types; verify tag 2776074 equals this exact digest, the digest reference itself is pullable, linux/amd64 or every required node platform exists, the image config/attestation identifies the full reviewed commit, and the expected CLI/migrations exist. Repeat immediately before merge and fail closed if the tag moved. -->
<!-- codex: critique — db-migrate.yaml also runs ghcr.io/cloudnative-pg/postgresql:17.10 as an init container even when no superuser DSN is supplied. Pin that image by digest or remove the unused optional bootstrap init container; otherwise the one-shot path still executes mutable code. -->

2. Wire the Deployment into apps/agentforge/kustomization.yaml: add `- deployment.yaml` to resources. db-migrate.yaml stays OUT (operator one-shot; failing migration Job under wait:true would wedge apps).
<!-- codex: critique — Keeping the Job out of the shared apps Kustomization avoids one failure mode, but adding a DB-blind Deployment creates false readiness. Preferred: isolate the Deployment in an agentforge-platform Flux Kustomization depending on databases, with /readyz readiness. Minimum acceptable alternative: keep this activation line in a separate, draft PR until migration and /readyz preconditions are proven. -->

3. Fill the two bot tokens in agentforge-runtime.sops.yaml (SOPS-encrypt, preserve AFP_SESSION_SECRET unchanged): AFP_TENANTS_BOT_TOKEN and AFP_BOOTSTRAP_TOKEN. Tokens minted in Part 2.
<!-- codex: critique — Part 1 depends on Part 2, so the stated ordering is internally inconsistent. Mint and validate tokens before editing the SOPS file, or explicitly describe these parts as parallel preparation rather than an execution sequence. -->

### Part 2 — Token minting (live Gitea; reversible; off the shared infra-pg)

<!-- codex: critique — Account, collaborator, and token mutations are reversible only with an explicit rollback: revoke token by recorded token name/ID, remove collaborator/team membership, and disable or delete the bot account. Never record the token itself. -->
PATs in Gitea are user+scope (not per-repo), so per-repo isolation = dedicated bot users:
- agentforge-cp-bot (tenants committer): gitea admin user create --restricted -> add as write collaborator on ONLY cchifor/agentforge-tenants -> verify GET collaborators -> mint token scope write:repository (contents PUT needs write:repository). Restricted user with exactly one write repo.
<!-- codex: critique — The documented Gitea CLI user-create command requires email/password inputs and does not document a --restricted flag. Check `gitea --version` and `gitea admin user create --help` inside the installed container. If unsupported, create/patch through the operator-controlled admin API/UI with restricted=true, max_repo_creation=0, allow_create_organization=false, a discarded random password, no SSH keys, and no org/team memberships. -->
<!-- codex: critique — write:repository is a broad API-route scope that includes destructive repository operations; repo ACL—not the PAT—provides the final boundary. Verify push=true/admin=false, branch protection permits only the intended contents PUT, and negative tests deny another private repo, collaborator administration, repository creation, and issue/label writes. -->
- agentforge-bootstrap-bot (workspace labels): restricted create, NO repo grant by default -> mint token scope write:issue (Gitea label create is under issue unit). Per-workspace write-collaborator grant is documented step; until then bootstrap endpoint 502s (does not block create->commit->materialize DoD).
<!-- codex: critique — write:issue is the correct scope family for label routes, but the repository must also grant Issues-unit write permission. Confirm the exact installed Swagger operation and test both a permitted workspace label mutation and denial of repository contents/Git push. Do not assume the application maps every Gitea authorization failure to 502; verify the expected status from the pinned image. -->
<!-- codex: critique — The plan leaves the mechanism that grants each future workspace repository unresolved. Document who owns those repositories, whether they are individual or organization repos, who adds the bot, and when that happens relative to calling bootstrap. -->

Capture each token to umask-077 mode-600 file, never echo value, SOPS-encrypt into agentforge-runtime.sops.yaml, shred temp files, verify sops --decrypt round-trips + git check-ignore clean before commit. If permission classifier blocks headless user creation, fall back: leave placeholders, mark step gated, document exact commands in runbook.
<!-- codex: critique — Avoid persistent plaintext entirely where possible. Minting through kubectl exec sends the one-time token to stdout, which can enter terminal/tool logs. Also avoid token-bearing command arguments, environment variables, shell tracing, clipboard history, and files inside the worktree. If a temporary file is unavoidable, place it outside the repo on an encrypted local filesystem with restrictive ACLs and delete it normally; `shred` cannot guarantee erasure on SSD/CoW storage. -->
<!-- codex: critique — `git check-ignore clean` is not a meaningful cleanliness test. Use git status with all untracked files, inspect staged changes, run a secret scanner on the staged diff/worktree, and verify each stringData value remains ENC[...] without decrypting it to logs. Compare only boolean/length/hash results for decrypted values. -->
<!-- codex: critique — If tokens remain placeholders, do not add deployment.yaml or merge an activation change: create->commit cannot satisfy the stated DoD. The fallback must gate the entire activation. -->

### Part 3 — Staged operator runbook (NOT executed headless; item 4)

Append "Item 1 — agentforge-platform activation" to scratchpad HEADLESS-PENDING.md, ordered relative to user-gated merge:

1. Create agentforge_platform DATABASE as superuser via peer-auth exec into infra-pg-1 (\gexec idempotent; roles exist, only DB created). Precheck: DB currently absent.
<!-- codex: critique — Do not hard-code infra-pg-1; after CNPG failover it may be a read-only replica. Discover the current primary using CNPG labels/status immediately before exec. The idempotent CREATE only handles absence: if the DB already exists, assert owner=afp_admin, role LOGIN/BYPASSRLS attributes, and required connection privileges rather than silently accepting drift. -->

2. Run migration one-shot: kubectl apply -f apps/agentforge/db-migrate.yaml then wait complete (alembic 0001+0002). Uses pinned image + AFP_ADMIN_DSN (already live). RUN THIS BEFORE MERGE so post-merge workspace-create doesn't hit missing tables.
<!-- codex: critique — A fixed-name Job is not safely rerunnable: an existing completed/failed Job may prevent execution or make an image/template update immutable. Preflight the Job, inspect and remove an old instance deliberately, or generate a unique run name. Add an explicit timeout and failure path that captures describe/logs without secrets. -->
<!-- codex: critique — “Complete” is insufficient. Query alembic_version for the exact expected head, confirm required tables/policies exist, assert RLS is enabled/forced as designed, test afp_app authentication and denial boundaries, and record the executed pod’s imageID. -->

3. Seed af:tenant-zero:owner onto user chifor in authelia-secret.sops.yaml (users_database.yml; current groups ['admins','openwebui-admin']), re-encrypt, roll deploy/authelia.
<!-- codex: critique — This is a GitOps file change missing from Part 1 and Critical files. It cannot be safely “re-encrypt[ed] and roll[ed]” before the PR is merged unless the runbook decrypts/applies a live Secret out of band, which creates leak and Flux-reversion risks. Put the OIDC group change in an earlier safe PR while deployment.yaml remains omitted, let Flux apply it, roll Authelia, and verify existing OIDC clients plus the groups claim before activation. -->

4. Merge the ailab PR (user-gated) -> flux reconcile source git flux-system + flux reconcile kustomization apps (flux source behind gitea/main) -> Deployment Ready on /healthz.
<!-- codex: critique — Runbook ordering does not prevent an accidental early merge. Split activation into a second PR/commit containing the sole `- deployment.yaml` change, held draft until a migration receipt is recorded. Reconcile with the source and verify apps.status.lastAppliedRevision equals the intended merge SHA; “Ready on /healthz” must not be treated as database readiness. -->

5. Verify: https://agentforge.chifor.me loads; OIDC login; GET /api/me shows tenant-zero:owner; create Workspace -> confirm commit in cchifor/agentforge-tenants -> Flux materializes tenant namespace.
<!-- codex: critique — Add an in-cluster /readyz=200 check before public traffic, verify pod imageID equals the approved digest, and inspect logs for migration/auth errors. Use a uniquely named disposable workspace, verify the exact tenants Git revision and Flux Kustomization revision, then document cleanup/rollback so the test does not leave an unintended namespace or desired-state commit. -->

## Critical files
- kubernetes/apps/apps/agentforge/deployment.yaml — pin CP image digest
- kubernetes/apps/apps/agentforge/db-migrate.yaml — pin migrate image digest (stays out of kustomization)
- kubernetes/apps/apps/agentforge/kustomization.yaml — add `- deployment.yaml`
- kubernetes/apps/apps/agentforge/agentforge-runtime.sops.yaml — fill both bot tokens (SOPS)
- <scratchpad>/HEADLESS-PENDING.md — append staged operator runbook (Part 3)
<!-- codex: critique — This list omits authelia-secret.sops.yaml and, if the safer isolation design is adopted, the cluster-level agentforge-platform Flux Kustomization plus its parent kustomization entry. -->

## Verification

Static (headless, pre-merge):
- kubectl kustomize kubernetes/apps/apps/agentforge builds clean and includes Deployment with pinned digest
<!-- codex: critique — Parse the rendered output and assert both platform containers use the exact same approved digest, no sha256:000... placeholder remains, no mutable tag exists in the migration path, and AFP_WORKER_IMAGE remains unchanged. A successful YAML build alone proves none of these. -->
- sops --decrypt agentforge-runtime.sops.yaml round-trips; both token values non-placeholder without printing; AFP_SESSION_SECRET unchanged
<!-- codex: critique — Define a non-leaking check: print only pass/fail, lengths, and one-way hashes; never allow decrypted YAML into CI/tool output. Also run `sops filestatus` and statically assert all stringData payloads are ENC[...] values. -->
- Registry re-confirm: 85a4a3c7... resolves for tag 2776074 at check time
<!-- codex: critique — Make this equality explicit and run it twice: tag digest == approved digest immediately before commit and immediately before merge; digest reference HEAD/GET succeeds; media type/platform is expected; full source revision/provenance matches; exact digest passes a CLI smoke test. Treat any moved tag or unverifiable provenance as a hard stop. -->
- git check-ignore clean; no plaintext secret, no _out/, in diff
<!-- codex: critique — Replace with git status --short --untracked-files=all, staged-diff inspection, `git diff --check`, and a secret scan of staged/worktree content. Ensure the SOPS age private key, decrypted files, token prefix/value, shell transcripts, and temporary output are absent; removing a leaked file from the final diff does not remove it from Git history or external logs. -->
- Gitea (post-mint, read-only): GET /repos/cchifor/agentforge-tenants/collaborators shows agentforge-cp-bot (write); agentforge-bootstrap-bot has no repo write
<!-- codex: critique — Also verify both accounts are restricted, non-admin, unable to create repos/orgs, have no SSH keys or unexpected org/team/inherited access, and expose only the intended PAT scope. Add positive and negative API authorization tests; collaborator listing alone does not establish least privilege. -->

Live (staged in runbook, NOT run headless): DB create, migration Job complete, OIDC-group seed + authelia roll, then end-to-end login -> create-workspace -> tenants-commit -> Flux-materialize proof.
<!-- codex: critique — Add hard gates for exact Alembic head/RLS checks, /readyz=200, actual pod imageID, intended Flux revision, NetworkPolicy path validation, and token negative tests. Define rollback: remove/suspend the Deployment, revoke both PATs, remove collaborator grants, and preserve database state unless a separately reviewed schema rollback exists. -->

<!-- codex-review-status: complete -->