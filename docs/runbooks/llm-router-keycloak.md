# LLM Router Keycloak activation

This is a pending two-phase activation for [llm-router #121](https://git.chifor.me/cchifor/llm-router/issues/121).
The application image is already deployed; Keycloak sign-in is not yet active. Follow
[ADR 0038](../decisions/0038-ailab-keycloak-for-relay.md) and the [Keycloak runbook](keycloak.md).
Do not combine the prerequisite and activation merges: the public issuer and administrator must
be verified before the router attempts OIDC discovery at startup.

## Phase 1: identity and database prerequisites

The prerequisite PR adds an isolated `llm-router` realm, its exact public edge allowlist entry,
the CNPG role declaration and operator-run database bootstrap, and router egress to infra-pg.
It leaves the router's browser authentication in local mode and keeps the deployed image digest.

Before merging, the operator must provision these secret contracts through SOPS/GitOps. The draft
contains references only; no placeholder credentials are safe to deploy. Add the encrypted resources
to the corresponding Kustomizations and verify they are materialized before consumers roll. If needed,
land these secret-only additions in a preparatory PR. Do not widen the worker's cluster permissions.

| Namespace | Secret | Required keys | Meaning |
|---|---|---|---|
| `keycloak` | `llm-router-oidc` | `client-secret` | New confidential client's secret, identical to the router copy |
| `databases` | `infra-pg-llm-router-auth` | `username`, `password` | `kubernetes.io/basic-auth`, username `llm_router_auth`, CNPG-managed role |
| `llm-router` | `llm-router-auth-db` | `database-url` | DSN for `llm_router_auth` at `infra-pg-rw.databases.svc.cluster.local:5432/llm_router_auth` |
| `llm-router` | `llm-router-oidc` | `client-secret`, later `admin-subject` | Matching client secret; exact verified administrator identity added after realm provisioning |

Generate independent client and database credentials; never post values to issues, logs, or shell
arguments. Match the database password with the DSN using the existing secret-management mechanism.
Verify the DSN's TLS settings using the deployed Node/pg driver; if a CA is needed, provide its trust
configuration before activation. The single auth role owns only this auth database, because the
current application performs idempotent DDL at startup. It is not a superuser, role creator, database
creator, or RLS bypass role. SQLite data, provider credentials, and API keys remain in their existing store.

CNPG 1.24 has no Database CRD. On the current primary, the operator runs the idempotent
`kubernetes/apps/databases/llm-router-auth-db-bootstrap.sql` with
`psql -U postgres -v ON_ERROR_STOP=1 -f -` over stdin, using the established peer-auth invocation
in the Keycloak runbook. Verify the existing database owner/role privileges on reruns; the bootstrap
deliberately does not reassign an existing database. CNPG then reconciles the password from its Secret.
Verify connection and migration privileges with the router's own DSN without printing it.
The nightly infra-pg dump enumerates every database; confirm the new database appears in a completed
backup, with restorable auth tables, after activation.

The shared Keycloak pod requires `keycloak/llm-router-oidc` before this PR rolls; absence would also
interrupt Relay login. Its projected import directory keeps the Relay realm and adds
`realm-llm-router.json`. Startup imports only a realm that does not already exist. If the operator
pre-created the realm, reconcile it with kcadm and compare every declared field, including the client
secret by a hash comparison only. Never assume updating the import JSON updates an existing realm.

After the prerequisite merge, verify:

1. Keycloak and the tunnel connectors are ready. Existing Relay discovery/login still work.
2. `https://id.chifor.me/realms/llm-router/.well-known/openid-configuration` returns the exact issuer
   `https://id.chifor.me/realms/llm-router` and advertises PKCE `S256`.
3. The confidential client `llm-router` has only the redirect
   `https://router.chifor.me/auth/callback` and origin `https://router.chifor.me`, requires S256,
   and disables implicit, direct-grant, and service-account flows.
4. Public `/admin`, `/realms/master`, unknown realms, dot-segment paths and semicolon paths still
   return 404. Exercise raw and encoded traversal cases with `curl --path-as-is` as in the Keycloak runbook.
5. The router still advertises `oidc:false, local:true`; existing API-key traffic remains healthy.

Provision the intended first administrator in this realm. Follow ADR 0038's out-of-band identity
verification and mark `emailVerified=true` on the exact user ID. Store
`https://id.chifor.me/realms/llm-router|<that-id>` as `admin-subject` in the router's OIDC Secret.
Do not copy a subject from Relay or resolve membership by email. No SMTP or Google broker is added
by this change; self-registration alone confers no router membership.

Record the prerequisite PR, issuer checks, secret reference names, and confirmation of administrator
provisioning in #121. Never include passwords, tokens, or client secrets.

## Phase 2: activate browser OIDC

Only after those checks, merge the separate router Deployment change setting:

| Setting | Value/source |
|---|---|
| `ROUTER_AUTH_MODE` | `oidc` |
| `ROUTER_AUTH_PUBLIC_URL` | `https://router.chifor.me` |
| `ROUTER_OIDC_ISSUER` | `https://id.chifor.me/realms/llm-router` |
| `ROUTER_OIDC_CLIENT` | `llm-router` |
| `ROUTER_OIDC_REQUIRE_EMAIL_VERIFIED` | `1` |
| `ROUTER_AUTH_CLIENT_ADDRESS_HEADER` | `cf-connecting-ip`, overwritten by the existing trusted edge |
| `ROUTER_AUTH_DATABASE_URL` | `llm-router-auth-db/database-url` |
| `ROUTER_OIDC_SECRET` | `llm-router-oidc/client-secret` |
| `ROUTER_AUTH_ADMIN_SUBJECT` | `llm-router-oidc/admin-subject` |

Keep `ROUTER_ADMIN_TOKEN` for existing bearer clients; OIDC disables its ordinary browser token form.
Keep one replica (`Recreate`). The current OIDC pending state is process-local. No break-glass credential
is introduced automatically; if recovery needs one, explicitly provision and exercise its loopback-only
path through an operator-reviewed change. The rollback below provides the existing local login path.

Verify `/auth/config` reports `oidc:true, local:false`, and `/auth/oidc` redirects to the expected realm
with the exact callback, state, nonce, and PKCE S256. The provisioned administrator must complete login,
see account/settings/theme controls, and sign out successfully. Verify unverified/non-member identities
are refused, callbacks cannot be replayed, cookies are Secure/HttpOnly, and API-key traffic continues.
The change switches browser sessions from SQLite to PostgreSQL, so existing browser sessions require
sign-in again. Their old private conversations belong to the old principal; they are not reassigned.

Healthy `/health` and `/ready` alone do not prove OIDC works. The deployed auth plugin keeps bearer
traffic available when PostgreSQL fails during initialization, but disables browser auth until restart.
It does not automatically retry initialization; restore the database and perform a reviewed restart.
If Keycloak discovery fails while the database is healthy, startup fails. Exercise these failure modes
in an isolated environment, not by interrupting production dependencies.

## Rollback

Revert the phase-2 Deployment environment change via a reviewed PR; keep the image and all existing
API-token/SQLite settings. Do not delete the PostgreSQL database, roles, realm or Secrets during rollback.
Local sign-in returns and pre-cutover SQLite sessions may become valid again until their original expiry;
account for that explicitly if rollback follows an authentication incident. If needed, disable public
access to the new realm in a separate edge PR while retaining the Relay allowlist and path guards.
Verify the router's mode, cookie login, bearer traffic and health after Flux reconciles.
