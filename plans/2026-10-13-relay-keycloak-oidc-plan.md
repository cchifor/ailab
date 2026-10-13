# Relay v0.5.2 sign-in through a new ailab-owned Keycloak

## Context

Relay (`relay.chifor.me`, namespace `relay`) is the agent control plane that dev-worker-4's Codex
agent builds. Live is v0.3.5 (image `sha256:25640aaf…`), still on the local admin-token login.
v0.5.2 (main `39767c6`, PR #140) makes Relay an OIDC relying party. It uses authorization code with
PKCE S256, state, nonce, issuer discovery and scope `openid profile email`, and keys membership on
the immutable `issuer|sub`, never on email. The worker asked the operator for an IdP, a client, a
secret, a first member, a release, a Flux PR and acceptance. The ask is in
`/workspace/archive/relay-agent-control-plane-operator-requests.txt` on dw4, under "OPERATOR
QUESTIONS — AUTHENTICATION DEPLOYMENT — 2026-10-13".

**Owner decision (2026-10-13), which replaces the worker's assumption:** do NOT use the platform's
Keycloak (`auth.strive.place`, realm `strive`). Run **a new Keycloak instance with a new realm**,
with **self-service sign-up and username/password sign-in**. Google IdP comes later. The first Relay
member is **chifor@gmail.com**, role admin.

Facts checked on 2026-10-13:

- **Relay v0.5.2 config** (`server/config.ts`, `server/auth.ts`):
  - OIDC mode needs `OIDC_ISSUER` (HTTPS, with no query or credentials), `OIDC_CLIENT_ID`,
    `OIDC_CLIENT_SECRET`, and an HTTPS exact-origin `RELAY_URL`.
  - `OIDC_IDP_HINT` is optional. `OIDC_PROVIDER_NAME` defaults to "Keycloak".
    `OIDC_REQUIRE_EMAIL_VERIFIED` defaults to on.
  - OIDC mode only disables `POST /api/auth/login`, the admin-token browser login. Local mode with
    the same image is therefore a working rollback.
  - With no active restore fence, startup needs no recovery-generation file.
- **Release `v0.5.2`** (Gitea release 2124) was created by the operator at `39767c6`. Tag workflow
  run 84067 **succeeded**. It published the assets and the receipt `deployment-images-84067.json`.
- **There is no SMTP relay in the estate.** The Authelia, Vaultwarden and QNAP runbooks all say so.
  Keycloak therefore cannot send verification or reset mail.
- **Shared Postgres is CNPG `infra-pg`** (namespace `databases`, CNPG 1.24.1). The pattern has
  three parts:
  - a `managed.roles` entry;
  - an `infra-pg-<app>` SOPS secret listed in `databases/kustomization.yaml`;
  - the DATABASE, created by an operator one-shot through CNPG peer auth
    (`gitea-db-bootstrap.yaml`). The litellm Job variant must NOT be copied: it exits green having
    created nothing. A `Database` CR is unsupported on this operator.

  `infra-pg-dump-cronjob` enumerates databases, so a new DB is backed up automatically.
- **Image.** `quay.io` pulls work in the cluster. Keycloak `26.7.5` is
  `quay.io/keycloak/keycloak@sha256:37dbaf6f0722c9ec246335f36e1ef8b2e6cb960f7c27e0d8c615121a3d475a85`
  (2026-09-30). 26.8.0 is skipped because it is only 3 days old.
- **Public exposure** has two parts: the Cloudflare tunnel ingress (`apps/edge/cloudflared.yaml`,
  applied by Flux) and a proxied CNAME from `kubernetes/infra/cloudflare` `tunnel_hostnames`
  (OpenTofu, applied by hand). Relay already egresses to public 443, so it can reach an issuer
  published through the tunnel.
- **ADR 0036** rejected "a separate ailab-owned Keycloak (e.g. `id.chifor.me`)" for Forge alone. Its
  "Revisit when" section names a second ailab application needing Keycloak. The owner's call today
  is that revisit, and it is recorded as a new ADR.

## Approach

### Names

These are fixed here: changing them later changes the issuer that memberships bind to.

- Host `id.chifor.me`. Realm `relay`, so the issuer is `https://id.chifor.me/realms/relay`.
- Client `relay` (confidential). Namespace `keycloak`.
- One realm per application, as ADR 0036 established. A later app gets its own realm on this
  instance.

### PR 1 (ailab): the Keycloak instance

1. **ADR 0038**, "ailab Keycloak (`id.chifor.me`) as Relay's identity provider". Owner-directed
   2026-10-13. It covers:
   - Why not `strive`: it is a platform-owned realm with public Strive users, and the platform
     README says to keep other apps in their own realms.
   - One realm per app. Google comes later.
   - Sign-up is open with no email verification, plus the abuse decision and the
     operator-verified-email rule (both below).
   - The relationship to ADR 0036: this revisits its option 2.
2. **Database** (`kubernetes/apps/databases/`):
   - `infra-pg.yaml` `managed.roles` gets `keycloak` (login; no createdb, createrole or superuser),
     with `passwordSecret: infra-pg-keycloak`.
   - `infra-pg-keycloak.sops.yaml` (`kubernetes.io/basic-auth`, username `keycloak`), **added to
     `databases/kustomization.yaml`**.
   - `keycloak-db-bootstrap.sql`: a plain SQL file, not a manifest, and not in the kustomization. It
     is idempotent: `\gexec` `CREATE ROLE keycloak LOGIN` if absent, then `CREATE DATABASE keycloak
     OWNER keycloak`. It is run once with the proven gitea invocation:
     `kubectl --context admin@ai -n databases exec -i infra-pg-1 -c postgres -- psql -U postgres -v ON_ERROR_STOP=1 -f - < kubernetes/apps/databases/keycloak-db-bootstrap.sql`,
     on whichever pod is primary (check `cnpg status` / the `-rw` endpoint first).
3. **App** (`kubernetes/apps/apps/keycloak/`, added to `apps/kustomization.yaml`):
   - **`namespace.yaml`**: PSA `restricted` enforce, labelled like the other apps.
   - **`keycloak-secrets.sops.yaml`**. Keys:
     - `KC_BOOTSTRAP_ADMIN_USERNAME`, `KC_BOOTSTRAP_ADMIN_PASSWORD`;
     - `KC_DB_PASSWORD`, byte-identical to `infra-pg-keycloak`;
     - `RELAY_OIDC_CLIENT_SECRET`, random 48-byte base64url.

     Values are generated locally, never printed, and SOPS-encrypted under the generic
     `.*\.sops\.ya?ml` rule (`stringData`).
   - **`realm-relay.json`** (ConfigMap via `configMapGenerator`), mounted at
     `/opt/keycloak/data/import` and imported with `--import-realm`.
     - Import is **create-if-absent and runs only while the realm does not exist**. Later JSON edits
       never reach a live realm.
     - Realm settings: `registrationAllowed: true`, `registrationEmailAsUsername: true`,
       `loginWithEmailAllowed: true`, `duplicateEmailsAllowed: false`.
     - No SMTP, so `verifyEmail: false` and `resetPasswordAllowed: false`.
     - Also `rememberMe: false`, `sslRequired: external`, `bruteForceProtected: true` (default
       thresholds), and `passwordPolicy: length(12) and notUsername and notEmail`.
     - The `relay` client:
       - `publicClient: false`, `clientAuthenticatorType: client-secret`,
         `secret: "${RELAY_OIDC_CLIENT_SECRET}"` (Keycloak's env placeholder substitution, applied
         at that one import);
       - `standardFlowEnabled: true`; `implicitFlowEnabled`, `directAccessGrantsEnabled` and
         `serviceAccountsEnabled` all false;
       - `redirectUris: ["https://relay.chifor.me/api/auth/callback"]`,
         `webOrigins: ["https://relay.chifor.me"]`;
       - `attributes: {"pkce.code.challenge.method": "S256"}`.
     - No users, no IdPs, no other clients.
   - **`keycloak.yaml` Deployment**: 1 replica, `strategy: Recreate` (local cache, one writer),
     image pinned by digest, args `start --import-realm`, `automountServiceAccountToken: false`.
     Env:
     - `KC_DB=postgres`, `KC_DB_URL_HOST=infra-pg-rw.databases.svc.cluster.local`,
       `KC_DB_URL_DATABASE=keycloak`, `KC_DB_USERNAME=keycloak`;
     - `KC_HOSTNAME=https://id.chifor.me`, `KC_HOSTNAME_ADMIN=http://localhost:8080` (the admin
       console works only through a port-forward);
     - `KC_HTTP_ENABLED=true`, `KC_PROXY_HEADERS=xforwarded`, `KC_CACHE=local`,
       `KC_HEALTH_ENABLED=true`.

     **No metrics.** Nothing would scrape them yet; a later monitoring PR can enable them together
     with a scrape path.

     Ports: `http` 8080 and a named `management` 9000. All three probes use
     `management` (`/health/started`, `/health/ready`, `/health/live`).

     Security context: runAsNonRoot, uid/gid 1000, drop ALL, no privilege escalation, seccomp
     RuntimeDefault. `readOnlyRootFilesystem: false` is a recorded exception: the stock image
     auto-builds for `KC_DB=postgres` into `/opt/keycloak/lib/quarkus` at start, and a custom
     optimized image is out of scope.

     Resources: requests 250m/768Mi, limits 2 CPU/1536Mi.
   - **`service.yaml`**: port 80 → `http` 8080 only. The management port is not in the Service.
   - **`network-policy.yaml`**: pod selector `app.kubernetes.io/name: keycloak`.
     - Ingress: TCP 8080 only from namespace `edge`, pods `app.kubernetes.io/name: cloudflared`.
     - Egress: kube-system DNS 53 (UDP and TCP), and TCP 5432 to namespace `databases`, pods of
       `cnpg.io/cluster: infra-pg`.
     - Kubelet probes are host traffic, which Cilium allows.
4. **Edge** (`apps/edge/cloudflared.yaml`): add `id.chifor.me` rules in this order, before the
   file's final global catch-all:
   - `path: ^/admin(/|$)` → `http_status:404`;
   - `path: ^/realms/master(/|$)` → `http_status:404`;
   - the host catch-all `service: http://keycloak.keycloak.svc.cluster.local:80`.

   No Cloudflare Access gate: Keycloak is its own login, the same as `relay` and `git`. Bump
   `chifor.me/config-revision`.
5. **DNS** (`kubernetes/infra/cloudflare/variables.tf`): add `id` to `tunnel_hostnames`.
   - Before applying, confirm that `id.chifor.me` does not resolve publicly, and that the operator
     state is the existing one, not an empty one.
   - `tofu plan` must show exactly one CNAME create. Then `tofu apply`.
6. **Monitoring**: a Gatus endpoint for
   `https://id.chifor.me/realms/relay/.well-known/openid-configuration` checking 200 plus an issuer
   match, the same shape as the existing "Strive Keycloak OIDC discovery" check.
7. **Runbook** `docs/runbooks/keycloak.md`. It covers:
   - **Admin access**: `kubectl --context admin@ai -n keycloak port-forward deploy/keycloak
     8080:8080`, then `http://localhost:8080/admin/`. The `KC_BOOTSTRAP_ADMIN_*` env creates a
     **temporary** master admin only at the first start, while master has no admin. Changing that
     Secret later neither changes nor recovers the account. The permanent named admin is created and
     the temporary one deleted with the exact kcadm commands.
   - **Realm changes and secret rotation**: the import never re-applies. Every live change is a
     kcadm/Admin-API change that is verified and written back to `realm-relay.json` in the same PR.
     The JSON holds settings only, never users, hashes or real secrets.
   - **Ordered client-secret rotation**:
     1. Generate a new secret.
     2. kcadm `update clients/<id> -s secret=…`.
     3. Verify with `get clients/<id>/client-secret`, comparing sha256.
     4. Update `relay-secrets` `OIDC_CLIENT_SECRET` and `keycloak-secrets` together (the Keycloak
        copy only matters for a re-import) in one PR, so Relay restarts.
     5. Rollback: kcadm back to the previous value, which is kept in OpenBao until the new one is
        verified.
   - **Drift check**: a kcadm read of the realm and client fields the JSON declares, diffed against
     the JSON.
   - **Abuse watch**: list users created in the last N days, and handle brute-force lockouts.
   - **Member provisioning, backup/restore** (infra-pg dumps), and the edge blocks.
8. **Escrow**: `af/estate/keycloak` in the OpenBao seeds file (bootstrap admin, and the relay client
   secret), together with its completeness-matrix entry in `estate-provision-job.yaml`, following
   `openbao-estate-credentials.md`.

### PR 2 (ailab): Relay v0.5.2 in LOCAL mode

This exercises the migration and the rollback state on their own. It is opened now, since the image
exists and it does not depend on Keycloak.

- In `relay/relay.yaml`, both the `migrate` init container and the `relay` container get the
  v0.5.2 digest.
  - The digest is read from `deployment-images-84067.json` and cross-checked against the registry
    manifest digest.
  - No auth env yet: `RELAY_AUTH_MODE` stays unset, which means local.
- Before the merge, run the infra-pg/relay nightly dump job once by hand and record its result.
- After reconcile, record:
  - the per-rollout `backup-database` and `backup-artifacts` init results and the generated
    `relay-pre-migration-backup.json` receipt name;
  - `migrate` success;
  - Relay 1/1 Ready on the new digest;
  - `/health` and `/ready` returning 200;
  - an admin-token sign-in returning 200. This proves the rollback configuration works.

### PR 3 (ailab): enable OIDC on Relay

Merged only after PR 1 is Ready, the realm import is observed, discovery returns the exact issuer,
and the client secret is verified.

- `relay/relay.yaml`: the `relay` env gains `RELAY_AUTH_MODE=oidc`,
  `OIDC_ISSUER=https://id.chifor.me/realms/relay`, `OIDC_CLIENT_ID=relay`,
  `OIDC_PROVIDER_NAME=Relay ID` and `OIDC_REQUIRE_EMAIL_VERIFIED=1`. There is no `OIDC_IDP_HINT`.
- `relay/relay-secrets.sops.yaml`: add `OIDC_CLIENT_SECRET`, byte-identical to Keycloak's
  `RELAY_OIDC_CLIENT_SECRET`. `RELAY_ADMIN_TOKEN` stays, for rollback.
- `docs/runbooks/relay.md`: an auth section covering the issuer, the member procedure and rollback.
  - **Auth rollback** = revert PR 3, which returns to the PR 2 state already proven above.
  - **Image rollback to v0.3.5**, in order:
    1. `flux suspend kustomization apps` (or the relay one).
    2. Scale `deploy/relay` to 0.
    3. `pg_restore` the pre-migration dump into a fresh database as `relay_migrator`, following
       the runbook's existing restore steps.
    4. A PR that pins v0.3.5. Its `migrate` is then a no-op on the v0.3.5 schema.
    5. Resume Flux.

    This is documented but **not rehearsed here**. The restore rehearsal is the worker's request 3
    and stays owner-pending.

### Email-verification and abuse policy (no SMTP)

- **Sign-up is open.** A new account has `email_verified=false`. Relay keeps
  `OIDC_REQUIRE_EMAIL_VERIFIED=1`, and its auth request includes the `email` scope, so the claim
  arrives.
- **A Keycloak account reaches Relay only when an operator has done both of these:**
  - **(a)** confirmed the person out of band, then read and recorded the exact Keycloak user ID
    (= `sub`), username and email;
  - **(b)** set `emailVerified=true` on that user ID and bound
    `https://id.chifor.me/realms/relay|<that id>` with `member.js`.

  A lookup by email alone is never used to bind.
- **Abuse decision for this release:**
  - The mitigations are Keycloak brute-force detection, the password policy, the runbook's
    recent-registrations watch, and Relay's membership gate. A stranger's sign-up therefore gets a
    Keycloak account and nothing in Relay.
  - No captcha or Cloudflare rate limit at launch.
  - Revisit when Google IdP lands, or if sign-up spam is observed. The ADR records this.

### Operator steps (in order)

1. **Merge PR 1 and bring Keycloak up.**
   1. Merge PR 1.
   2. Run the DB one-shot.
   3. `tofu apply` the CNAME.
   4. Verify:
      - Keycloak is Ready, and all three management probes return 200 (port-forward 9000);
      - the log says the realm `relay` was imported;
      - discovery returns 200 with the exact issuer and `S256` in
        `code_challenge_methods_supported`;
      - the kcadm client read matches the JSON (flows, redirect URI, origin, `attributes` PKCE);
      - the client-secret sha256 equals the Secret's sha256;
      - `https://id.chifor.me/admin/` and `/realms/master/` return 404 at the edge, and
        `/realms/relay/account` returns 200;
      - the issuer and redirect URLs in the login page are `https://id.chifor.me` (forwarded
        proto/host honoured);
      - the admin console works over the port-forward, without redirects to the public host.
2. **Replace the temporary admin.** Create the permanent named master admin, delete the temporary
   one, and escrow the permanent admin's password (seeds-file update in a follow-up PR).
3. **Merge PR 2 (v0.5.2, local).** Record its evidence, listed above.
4. **The owner registers.** The owner signs up as chifor@gmail.com at
   `https://id.chifor.me/realms/relay/account` → Register, then tells the operator. The operator
   then:
   1. reads the user by username, records its ID, and confirms the email with the owner;
   2. sets `emailVerified=true`;
   3. runs `kubectl exec deploy/relay -c relay -- node dist/scripts/member.js --subject
      'https://id.chifor.me/realms/relay|<id>' --name 'Constantin Chifor' --role admin`.
5. **Merge PR 3 (OIDC).** Then run acceptance and record UTC timestamps, all evidence nonsecret:
   - Relay pod init statuses; 1/1 Ready; `/health` and `/ready` return 200; `/api/auth/config`
     reports oidc.
   - Relay itself fetches discovery and JWKS and exchanges the code: shown by the owner's
     successful sign-in, and by logs with no egress errors.
   - **The owner signs in.** The session's user row has `subject` = the exact `issuer|<id>`
     recorded in step 4, and the ID token claims carry the expected `sub`, `iss`, `email` and
     `email_verified=true`. These are read in a browser devtools session by the owner, or with a
     test-only check. Only the hash of the sub is recorded.
   - **A throwaway sign-up is denied, with no session.**
   - **The throwaway is then bound as a member while `emailVerified=false`, and is rejected.** It
     is then removed from Relay, and the user is **deleted in Keycloak** (verified by a kcadm user
     lookup returning nothing).
   - **Logout** removes the Relay session.
   - `POST /api/auth/login` returns 404.
   - A tampered callback state creates no session.
   - The session cookie is `Secure; HttpOnly; SameSite`.
6. **Answer the dw4 handoff file.**
   - Questions 1–7. Q1 and Q2 changed by owner decision: a new instance, and no Google yet.
   - Request 2: resolved.
   - Requests 1 and 6, as described in "Older requests".

### Older requests in the same file (quick wins only)

- **Req 2** (reviewer convergence): resolved. Both hosts list relay, llm-router and dsh-acp-plus,
  and the services are active. Record it.
- **Req 6** (runner "permission denied"):
  - Root cause: act_runner v0.6.1 re-copies `actions/checkout` when a job uses it twice (dsh
    `conformance.yaml` lines 29 and 39).
  - The fix is a worker-side dsh workflow change (the second checkout becomes a `git` step),
    recorded for dw4.
  - The runner upgrade goes to an ailab issue, not this plan.
- **Req 1** (dsh `RELAY_SOURCE_READ_TOKEN`): needs a new owner-created read-only bot. Recorded as
  owner-pending with the recommended shape: bot `relay-source-reader`, a team with read on
  `cchifor/relay` only, a `read:repository` token, and OpenBao escrow. It is not done in this plan.
- `cc` linker and obsolete CI runs: resolved. Record them.

## Critical files

- `docs/decisions/0038-ailab-keycloak-for-relay.md` (new)
- `kubernetes/apps/databases/{infra-pg.yaml,infra-pg-keycloak.sops.yaml,kustomization.yaml,keycloak-db-bootstrap.sql}`
- `kubernetes/apps/apps/keycloak/*` (new) and `kubernetes/apps/apps/kustomization.yaml`
- `kubernetes/apps/apps/edge/cloudflared.yaml`, `kubernetes/infra/cloudflare/variables.tf`
- `kubernetes/apps/apps/gatus/configmap.yaml`
- `kubernetes/apps/infrastructure/security/openbao/estate-provision-job.yaml` + the seeds file
- `docs/runbooks/keycloak.md` (new), `docs/runbooks/relay.md`
- PR 2/3: `kubernetes/apps/apps/relay/{relay.yaml,relay-secrets.sops.yaml}`

## Verification

- **Pre-merge:**
  - `kustomize build` of `apps/keycloak`, `apps/edge` and `databases`. Check the rendered
    NetworkPolicy selectors and ports, and the Service and container port names.
  - The repo's CI manifest checks.
  - The realm JSON parses, and the client has `attributes."pkce.code.challenge.method" == "S256"`.
  - `cloudflared tunnel ingress validate` on the rendered config, plus `cloudflared tunnel ingress
    rule` for `https://id.chifor.me/admin/`, `/admin/realms`, `/realms/master/`, `/realms/relay/`,
    `/`, and an unrelated host.
  - `terraform fmt -check` and `tofu plan` for cloudflare: exactly one create.
  - A `sops -d` round trip on each new or changed Secret: keys present, values non-empty, the
    cross-copies byte-identical by sha256, never printed.
  - The Gatus config validates.
  - Repo CI green and both reviewer approvals.
- **Live:** "Operator steps" 1–5. After reconcile, confirm that Flux decrypted and created
  `infra-pg-keycloak` (databases) and `keycloak-secrets` (keycloak), and that CNPG reports the
  `keycloak` role reconciled.
- **Rollback:** the auth rollback is proven by PR 2's local-mode evidence. The image rollback is
  documented but not rehearsed (request 3). PR 1 is additive and can stay.

<!-- codex-review-status: complete -->
