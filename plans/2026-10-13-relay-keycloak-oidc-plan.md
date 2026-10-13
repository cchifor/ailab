# Relay v0.5.2 sign-in through a new ailab-owned Keycloak

## Codex Review

- The plan correctly follows the repository’s CNPG managed-role plus one-shot database pattern, preserves Relay’s backup gate, pins images, and includes useful edge and acceptance checks.
- The Keycloak deployment has several integration gaps: client-secret rotation cannot be performed by restarting pods, the realm JSON must encode PKCE as a client attribute, and the new database Secret must be included in the databases Kustomization.
- The admin hostname, management port, forwarded-header trust, and Cloudflare path rules need explicit live verification.
- Open registration without email verification is an intentional policy, but the plan needs abuse controls and a precise operator procedure for confirming the correct immutable subject.
- Rollback is directionally sound, but the plan should gate the Relay PR on a healthy Keycloak and document the actual forward-migration restore procedure.

## Context

Relay (`relay.chifor.me`, namespace `relay`) is the agent control plane that dev-worker-4's Codex
agent builds. Live is v0.3.5 (image `sha256:25640aaf…`), still on the local admin-token login.
v0.5.2 (main `39767c6`, PR #140) makes Relay an OIDC relying party: authorization code + PKCE S256,
state, nonce, issuer discovery, and membership keyed on the immutable `issuer|sub` (never on email).
The worker asked the operator for an IdP, a client, a secret, a first member, a release, a Flux PR
and acceptance (`/workspace/archive/relay-agent-control-plane-operator-requests.txt` on dw4,
"OPERATOR QUESTIONS — AUTHENTICATION DEPLOYMENT — 2026-10-13").

**Owner decision (2026-10-13), which replaces the worker's assumption:** do NOT use the platform's
Keycloak (`auth.strive.place`, realm `strive`). Run **a new Keycloak instance with a new realm**,
with **self-service sign-up and username/password sign-in**. Google IdP comes later. The first Relay
member is **chifor@gmail.com** (role admin).

Facts checked on 2026-10-13:

- Relay v0.5.2 config (`server/config.ts`): OIDC mode needs `OIDC_ISSUER` (HTTPS, no query or
  credentials), `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, an HTTPS exact-origin `RELAY_URL`.
  `OIDC_IDP_HINT` is optional, `OIDC_PROVIDER_NAME` defaults to "Keycloak", and
  `OIDC_REQUIRE_EMAIL_VERIFIED` defaults to on. OIDC mode only disables `POST /api/auth/login`, the
  admin-token browser login, so setting `RELAY_AUTH_MODE=local` again is a working rollback. Without
  an active restore fence, startup needs no recovery-generation file.
- Release `v0.5.2` (Gitea release 2124) was created by the operator at `39767c6` on 2026-10-13. Tag
  workflow run 84067 qualifies, publishes the assets and pushes the immutable image, then writes a
  `deployment-images-<run>.json` receipt to the release.
- There is **no SMTP relay in the estate** (Authelia, Vaultwarden and QNAP runbooks all say so), so
  Keycloak cannot send verification or reset mail.
- Shared Postgres is CNPG `infra-pg` (namespace `databases`, CNPG 1.24.1). Its pattern: a
  `managed.roles` entry plus `infra-pg-<app>` SOPS secret, and the DATABASE from an operator
  one-shot SQL (`litellm-db-bootstrap.yaml`), because a `Database` CR is unsupported here.
  `infra-pg-dump-cronjob` enumerates databases, so a new DB is backed up automatically.
- `quay.io` pulls work in the cluster (oauth2-proxy, openbao). Keycloak `26.7.5` =
  `quay.io/keycloak/keycloak@sha256:37dbaf6f0722c9ec246335f36e1ef8b2e6cb960f7c27e0d8c615121a3d475a85`
  (2026-09-30; 26.8.0 is 3 days old, so it is skipped).
- Public exposure: the Cloudflare tunnel ingress (`apps/edge/cloudflared.yaml`, Flux) plus a proxied
  CNAME from `kubernetes/infra/cloudflare` `tunnel_hostnames` (OpenTofu, applied by hand).
  Relay already egresses to public 443, so it reaches a tunnel-published issuer.
- ADR 0036 rejected "a separate ailab-owned Keycloak (e.g. `id.chifor.me`)" for Forge alone. Its
  "Revisit when" names a second ailab application needing Keycloak. The owner's call today is that
  revisit, recorded as a new ADR.

## Approach

### Names (fixed here; changing them later changes the issuer that memberships bind to)

- Host `id.chifor.me`. Realm `relay`, so issuer `https://id.chifor.me/realms/relay`. Client
  `relay` (confidential). Namespace `keycloak`.
- One realm per application, as ADR 0036 established. A later app gets its own realm on this
  instance.

### PR 1 (ailab): the Keycloak instance

1. **ADR 0038**, "ailab Keycloak (`id.chifor.me`) as Relay's identity provider". Owner-directed
   2026-10-13. Covers: why not `strive` (platform-owned realm with public Strive users; platform
   README says keep other apps in their own realms); one realm per app; sign-up open with no email
   verification; Google later; the operator-verified-email rule (below). It relates to ADR 0036,
   whose option 2 this revisits.
2. **Database** (`kubernetes/apps/databases/`):
   - `infra-pg.yaml` `managed.roles` gets `keycloak` (login, no createdb/createrole/superuser), with
     `passwordSecret: infra-pg-keycloak`.
   - `infra-pg-keycloak.sops.yaml` (`kubernetes.io/basic-auth`, username `keycloak`).
   - `keycloak-db-bootstrap.yaml`: a ConfigMap plus Job, deliberately NOT in `kustomization.yaml`
     (same as litellm). Idempotent `\gexec` `CREATE DATABASE keycloak OWNER keycloak`, run once by
     option A (`kubectl exec infra-pg-1 -- psql`).
<!-- codex: Add `infra-pg-keycloak.sops.yaml` to `kubernetes/apps/databases/kustomization.yaml`; otherwise Flux never creates the Secret consumed by `managed.roles`, and the CNPG role reconciliation cannot complete. -->
<!-- codex: Follow the repository’s proven `gitea-db-bootstrap.yaml` operator path: execute the SQL with `-c postgres -U postgres` through CNPG peer authentication on the primary. A Job that optionally reads `infra-pg-superuser` can exit successfully without creating anything, as the existing `litellm` pattern documents. -->
3. **App** (`kubernetes/apps/apps/keycloak/`, added to `apps/kustomization.yaml`):
   - `namespace.yaml`: PSA `restricted` enforce, labels like the other apps.
   - `keycloak-secrets.sops.yaml`: `KC_BOOTSTRAP_ADMIN_USERNAME`,
     `KC_BOOTSTRAP_ADMIN_PASSWORD`, `KC_DB_PASSWORD` (byte-identical to
     `infra-pg-keycloak`), `RELAY_OIDC_CLIENT_SECRET` (random
     48-byte base64url). Generated locally, never printed, SOPS-encrypted with the repo age rule.
   - `realm-relay.json` ConfigMap, mounted at `/opt/keycloak/data/import`, imported with
     `--import-realm`. Import is create-if-absent and never overwrites a live realm. It sets:
     `registrationAllowed: true`, `registrationEmailAsUsername: true`,
     `loginWithEmailAllowed: true`, `duplicateEmailsAllowed: false`, `verifyEmail: false` and
     `resetPasswordAllowed: false` (no SMTP), `rememberMe: false`, `sslRequired: external`,
     `bruteForceProtected: true`, and
     `passwordPolicy: length(12) and notUsername and notEmail`.
     The `relay` client: `publicClient: false`, client-secret auth with
     `secret: "${RELAY_OIDC_CLIENT_SECRET}"` (Keycloak's env placeholder substitution at import),
     `standardFlowEnabled: true`, implicit, direct-grant and service-account flows off,
     `redirectUris: ["https://relay.chifor.me/api/auth/callback"]`,
     `webOrigins: ["https://relay.chifor.me"]`, and
     `pkce.code.challenge.method: S256`.
     No users, no IdPs.
<!-- codex: Encode PKCE in the Keycloak realm export’s client `attributes` map as `"pkce.code.challenge.method": "S256"` and verify it with `kcadm`; a top-level client property with that name may be ignored during import. -->
<!-- codex: `--import-realm` skips an already-existing realm, so the placeholder is resolved only on the initial import. The runbook must use the Admin API/kcadm for subsequent client-secret rotation and realm changes; updating both Kubernetes Secrets and restarting does not change the existing Keycloak client. -->
   - `keycloak.yaml` Deployment: 1 replica, `strategy: Recreate` (local cache, one writer), image
     pinned by digest, args `start --import-realm`. Env:
     - `KC_DB=postgres`, `KC_DB_URL_HOST=infra-pg-rw.databases.svc.cluster.local`,
       `KC_DB_URL_DATABASE=keycloak`, `KC_DB_USERNAME=keycloak`;
     - `KC_HOSTNAME=https://id.chifor.me`, `KC_HOSTNAME_ADMIN=http://localhost:8080` (admin console
       reachable only through a port-forward);
     - `KC_HTTP_ENABLED=true`, `KC_PROXY_HEADERS=xforwarded`, `KC_CACHE=local`;
     - `KC_HEALTH_ENABLED=true`, `KC_METRICS_ENABLED=true`.
     Probes use management port 9000 (`/health/started|ready|live`). Security context: runAsNonRoot,
     uid 1000, drop ALL, no privilege escalation, seccomp RuntimeDefault. `readOnlyRootFilesystem:
     false` is a recorded exception: the stock image auto-builds for `KC_DB=postgres` into
     `/opt/keycloak/lib/quarkus` at start, and building a custom optimized image is out of scope.
     Requests 250m/768Mi, limits 2 CPU/1536Mi, no service-account token.
<!-- codex: Verify that `KC_HOSTNAME_ADMIN=http://localhost:8080` works through the intended browser port-forward and that the console does not redirect API calls to the public edge, where `/admin` is blocked. Also verify Cloudflare overwrites X-Forwarded headers and that direct pod access is not possible outside the allowed edge path. -->
<!-- codex: Declare a named management container port (or explicitly document the numeric probe contract) and test all three 9000 endpoints. Metrics are enabled but the Service exposes only 8080 and the plan adds no ServiceMonitor or management-port scrape path; either add the required monitoring wiring and NetworkPolicy allowance or remove `KC_METRICS_ENABLED`. -->
   - `service.yaml`: http 8080 only (9000 is not in the Service).
   - `network-policy.yaml`: ingress 8080 from `edge`/cloudflared only. Egress DNS (kube-system 53)
     plus `databases` infra-pg 5432 only.
<!-- codex: Specify the exact namespace and pod labels in the policy and test a tunneled request plus a denied non-edge request. If management metrics are retained, monitoring also needs an explicit path to port 9000; otherwise the enabled metrics endpoint is unused. -->
4. **Edge** (`apps/edge/cloudflared.yaml`): `id.chifor.me` rules, with `path: ^/admin(/|$)` and
   `path: ^/realms/master(/|$)` mapped to `http_status:404` BEFORE the catch-all
   `service: http://keycloak.keycloak.svc.cluster.local:8080`. No Cloudflare Access gate, because
   Keycloak is its own login, the same as `relay` and `git`. Bump the tunnel revision annotation the
   way existing hostname additions do.
<!-- codex: Validate the actual rendered ingress with `cloudflared tunnel ingress validate` and `cloudflared tunnel ingress rule` for `/admin/`, `/admin/realms`, `/realms/master/`, `/realms/relay/`, and an unrelated path. The host-specific 404 rules must precede the host catch-all and the file must retain its final global catch-all. -->
5. **DNS** (`kubernetes/infra/cloudflare/variables.tf`): add `id` to `tunnel_hostnames`. Operator
   runs `tofu plan`, which must show exactly one CNAME create, then `tofu apply` after merge.
<!-- codex: Run the plan against the existing Cloudflare state and confirm the `id` record is absent or imported before applying; an empty or stale state can produce duplicate-record or destructive changes despite the expected one-create plan. -->
6. **Monitoring**: a Gatus endpoint for
   `https://id.chifor.me/realms/relay/.well-known/openid-configuration` (200 plus issuer match), in
   the existing `gatus/configmap.yaml` style.
7. **Runbook** `docs/runbooks/keycloak.md`: admin access (`kubectl port-forward` + bootstrap admin,
   which is replaced by a named permanent admin after first login), realm drift (the import never
   updates; later changes go through kcadm and are written back to `realm-relay.json` in the same PR),
   member provisioning, rotation of the client secret (both Secrets in one PR, then restart both
   pods), backup/restore (infra-pg dumps), and the edge blocks.
<!-- codex: Clarify that the bootstrap admin variables create a temporary master-realm account only on the initial startup while the master realm is absent; changing those Secret values later does not recover or replace the account. Document the exact port-forward target and the permanent-admin creation/removal command. -->
<!-- codex: Replace the client-secret rotation instruction with an ordered kcadm/API update of the Keycloak client, verification of the new secret, then Relay Secret update and restart. Include a rollback secret procedure because the realm import will not apply the changed JSON. -->
<!-- codex: “Written back to realm-relay.json” is source-of-truth bookkeeping, not reconciliation. Add a drift check that compares the live realm/client settings with the reviewed JSON and explicitly excludes live users, password hashes, and generated secrets from the file. -->

### PR 2 (ailab): Relay cutover to v0.5.2 + OIDC

Opened after run 84067 publishes the image. The digest is taken from the release's
`deployment-images-*.json` receipt and cross-checked against the registry manifest.

- `relay/relay.yaml`: both the `migrate` init container and the `relay` container get the v0.5.2
  digest. `relay` env gains `RELAY_AUTH_MODE=oidc`,
  `OIDC_ISSUER=https://id.chifor.me/realms/relay`, `OIDC_CLIENT_ID=relay`,
  `OIDC_PROVIDER_NAME=Relay ID`, `OIDC_REQUIRE_EMAIL_VERIFIED=1`. No `OIDC_IDP_HINT`.
- `relay/relay-secrets.sops.yaml`: add `OIDC_CLIENT_SECRET`, byte-identical to Keycloak's
  `RELAY_OIDC_CLIENT_SECRET`. Keep `RELAY_ADMIN_TOKEN` (rollback).
- `docs/runbooks/relay.md`: an auth section with the issuer, rollback (`RELAY_AUTH_MODE=local` PR),
  and the member procedure.
- The existing init chain is the backup gate: `migrate` runs only when
  `/tmp/relay-pre-migration-backup.json` exists. Before merging, also run the nightly dump job once
  by hand and record its result.
<!-- codex: Make PR 2 merge/reconcile conditional on PR 1 being Ready, the realm import being observed, discovery returning the exact issuer, and the client secret being verified. Otherwise Relay can enter OIDC mode while its issuer or client is unavailable. -->
<!-- codex: The manually triggered nightly dump is useful evidence but does not replace the deployment’s per-rollout `backup-database` and `backup-artifacts` init chain. Record both the dump result and the generated pre-migration receipt before accepting the cutover. -->
<!-- codex: Add an explicit no-write/cutover window and a tested procedure for restoring the pre-migration generation if the v0.3.5 image must be restored; the current text assumes a dump exists but does not say how to stop reconciliation, restore it, and prevent Relay from immediately migrating again. -->

### Email verification policy (no SMTP)

Sign-up stays open, and a new account has `email_verified=false`. Relay keeps
`OIDC_REQUIRE_EMAIL_VERIFIED=1`. So an account gets into Relay only when an operator has (a) bound
its `issuer|sub` with `member.js` AND (b) set `emailVerified=true` in Keycloak after confirming the
person out of band. A stranger's sign-up gets a Keycloak account and nothing in Relay.
<!-- codex: Open registration with no email verification permits account-spam, username/email impersonation, and brute-force pressure even though Relay membership is gated. Add an explicit rate-limit/abuse-monitoring decision and state that operators must verify the exact Keycloak user ID, email, and immutable subject before binding membership. -->
<!-- codex: Verify that the Relay authorization request asks for the scopes/claims needed for `email_verified`, and test the token claim after toggling the Keycloak user’s `emailVerified` flag; otherwise the policy may depend on a claim Relay never receives. -->

### Operator steps (after both PRs merge and reconcile)

1. Run the DB bootstrap one-shot. Check that Keycloak starts, imports the realm, that discovery
   returns 200 with the exact issuer, and that the client secret matches (compare sha256 of both
   Secrets, never print values).
2. Log into the admin console through a port-forward with the bootstrap admin. Create a permanent
   named admin in `master` and remove the temporary one (KC 26 warns while it exists). Escrow
   the admin credential in OpenBao (`af/estate/keycloak-admin`) through the estate-credentials route.
3. The owner signs up as chifor@gmail.com at `https://relay.chifor.me` → "Sign in with Relay ID" →
   Register. The operator confirms the account with the owner, sets `emailVerified=true`, and runs
   `kubectl exec deploy/relay -c relay -- node dist/scripts/member.js --subject
   'https://id.chifor.me/realms/relay|<sub>' --name 'Constantin Chifor' --role admin`.
4. Acceptance, recorded with UTC timestamps (all evidence nonsecret):
   - the owner signs in;
   - a second throwaway sign-up is denied with no session;
   - a member with `emailVerified=false` is rejected;
   - logout removes the Relay session;
   - `POST /api/auth/login` returns 404;
   - a tampered callback (bad state) creates no session;
   - `/health` and `/ready` return 200;
   - pod init statuses are recorded;
   - `https://id.chifor.me/admin/` and `/realms/master/` return 404 at the edge.
   Then delete the throwaway account.
<!-- codex: Add a positive/negative test that the owner’s token contains the expected `sub`, issuer, email, and `email_verified=true`, and that the member command binds that exact subject rather than an email-derived lookup. Also verify the throwaway account is deleted from Keycloak, not merely absent from Relay. -->
5. Answer questions 1–7 in the dw4 handoff file, noting that Q1/Q2 changed by owner decision (new
   instance, no Google yet). Record request 2 as resolved, and requests 1/6 as described under
   "Older requests".

### Older requests in the same file (quick wins only)

- **Req 2** (reviewer convergence): resolved. Both hosts list relay, llm-router and dsh-acp-plus;
  services are active. Record it.
- **Req 6** (runner "permission denied"): root cause is act_runner v0.6.1 re-copying
  `actions/checkout` when a job uses it twice (dsh `conformance.yaml` lines 29/39). The fix is a
  worker-side dsh workflow change (second checkout via a `git` step), recorded for dw4. The runner
  upgrade becomes an ailab issue, not part of this plan.
- **Req 1** (dsh `RELAY_SOURCE_READ_TOKEN`): needs a new owner-created read-only bot. Recorded as
  owner-pending with the recommended shape (bot `relay-source-reader`, team with read on
  `cchifor/relay` only, token `read:repository`, OpenBao escrow). Not done in this plan.
- `cc` linker and obsolete CI runs: resolved. Record them.

## Critical files

- `docs/decisions/0038-ailab-keycloak-for-relay.md` (new)
- `kubernetes/apps/databases/{infra-pg.yaml,infra-pg-keycloak.sops.yaml,keycloak-db-bootstrap.yaml}`
- `kubernetes/apps/apps/keycloak/*` (new) and `kubernetes/apps/apps/kustomization.yaml`
- `kubernetes/apps/apps/edge/cloudflared.yaml`, `kubernetes/infra/cloudflare/variables.tf`
- `kubernetes/apps/apps/gatus/configmap.yaml`
- `docs/runbooks/keycloak.md` (new), `docs/runbooks/relay.md`
- PR 2: `kubernetes/apps/apps/relay/{relay.yaml,relay-secrets.sops.yaml}`

## Verification

- Pre-merge: `kustomize build` of `apps/keycloak` and `databases`; `kubeconform`/the repo's CI
  manifest checks; `terraform fmt -check` and `tofu plan` for cloudflare (exactly one create);
  `sops -d` round-trip check on each new Secret (keys present, values non-empty, never printed);
  realm JSON parses; Gatus config validates; repo CI green; both reviewer approvals.
<!-- codex: Add `kustomize build`/schema checks for the rendered NetworkPolicy and Service ports, validate the exact realm client attribute shape, and run `cloudflared tunnel ingress validate` plus representative `cloudflared tunnel ingress rule` checks. Confirm the database Secret is included in the databases Kustomization and that Flux decryption produces both namespace-local Secrets. -->
- Live: the steps under "Operator steps". Keycloak pod Ready; the import logs say "Realm 'relay'
  imported"; discovery 200 with the exact issuer and S256 in
  `code_challenge_methods_supported`; the `relay` client read via kcadm matches the JSON (flows,
  redirect, origin, PKCE); edge 404s; Relay 1/1 Ready on the v0.5.2 digest; acceptance list complete.
<!-- codex: Add management-port probe results, a browser port-forward admin-console test, forwarded-proto/secure-cookie evidence, and an explicit check that Relay can reach discovery/token/JWKS over its allowed egress. If metrics remain enabled, verify the intended scrape path. -->
- Rollback: an auth-only rollback keeps the v0.5.2 image and sets `RELAY_AUTH_MODE=local`, which
  brings the admin-token login back (that token stays in `relay-secrets`). Going back to the v0.3.5
  image also means restoring the pre-migration dump, because v0.3.5→v0.5.2 migrations are
  forward-only. PR 1 is additive and can stay.
<!-- codex: Test the auth-only rollback in a disposable or controlled window, including a fresh pod restart and successful admin-token login. For an image rollback, name the exact suspension/restore/reconcile order and verify the restored database is not immediately migrated again. -->

<!-- codex-review-status: complete -->