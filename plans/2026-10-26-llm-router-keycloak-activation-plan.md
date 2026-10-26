<!-- status: done 2026-10-26 (pending the owner's first browser sign-in) -->
# llm-router: Keycloak browser sign-in in production (llm-router#121)

Date: 2026-10-26. A dated record: do not rewrite it later. The current procedure lives in
`docs/runbooks/llm-router.md` and `docs/runbooks/keycloak.md`.

## Owner decisions (2026-10-26)

- Enable it now.
- The new realm has open self-registration, like `relay`. A registration grants nothing without a
  router membership, and the router also requires `email_verified`.
- The operator creates the first administrator (`chifor@gmail.com`) with a one-time password.
- A loopback-only break-glass token is kept in SOPS.

## Starting point

- The live router runs llm-router `2d50a70` (#117) in local auth mode: admin-token browser sign-in
  and SQLite sessions. Bearer API keys are matched before any authenticator.
- `auth` is a required plugin; if it fails to start, the router process exits.
- The #117 OIDC/PostgreSQL code has no integration tests. The two auth test files cover a subject
  formatter and a principal projection.
- The router reaches `https://id.chifor.me` through Cloudflare, on the existing public-443 egress.

## Phases

1. **ailab PR A, prerequisites only (no router behaviour change):**
   - realm `llm-router` and its confidential `llm-router` client (PKCE S256, exact callback
     `https://router.chifor.me/auth/callback`), in the shared `keycloak-realms` ConfigMap;
   - the client secret in `keycloak-secrets`;
   - the edge allow line `^/realms/llm-router(/|$)`;
   - managed role `router_auth` and the one-shot `router-auth-db-bootstrap.sql`;
   - SOPS Secret `llm-router-oidc` (client secret, DB URL with `sslmode=no-verify`, break-glass
     token), not yet referenced;
   - router egress to infra-pg :5432 and a Gatus discovery check.

   Gates:
   - the import rehearsal on the pinned image (done, passed);
   - a fresh `infra-pg-dump` and a saved copy of `relay` before the merge;
   - Relay sign-in and the `relay` realm unchanged after the Keycloak restart.
2. **Live steps:**
   - run the DB bootstrap on the primary;
   - verify the realm, client and secret hash, and the edge matrix;
   - create the administrator (email verified, one-time password delivered in a file only the owner
     reads) and record its `sub`.
3. **llm-router hardening PR**, before any OIDC switch:
   - pool `error` handlers and query/lock timeouts;
   - one single-flight, retried, disposable background init for every PG step, including the admin
     check, that never blocks `/ready`;
   - lazy single-flight OIDC discovery;
   - `/auth/config` reports mode and availability separately, and the SPA refreshes it;
   - no SQLite session fallback when PG is configured;
   - session and audit ordering;
   - the throttle keyed on the client-address header;
   - `Secure` cookies from the configured HTTPS origin;
   - a literal issuer prefix instead of `LIKE`;
   - #120.

   Real-PG and HTTPS-issuer integration tests. Then a Relay image, rolled in local mode.
4. **Isolated end-to-end run:** the hardened release in a throwaway pod, with its own PG sidecar,
   against the real `llm-router` realm, using a disposable test user. It covers:
   - unverified email, non-member and member sign-ins;
   - tampered state, replay and expiry;
   - logout revocation and CSRF;
   - PG loss during runtime and at start.
5. **ailab PR B, the switch:** `ROUTER_AUTH_MODE=oidc` plus the issuer, client, secrets, admin
   subject, public URL, client-address header and break-glass token.
   - Before the merge: a backup and a `validate-live` baseline.
   - After the rollout: `validate-live`, public checks, break-glass via port-forward, and the owner's
     own browser sign-in.
   - Rollback: revert PR B, removing every auth setting, and clear the SQLite `auth.sessions` KV so
     pre-switch local sessions do not come back.

## Codex plan review (gpt-6-astra, 2026-10-26): dispositions

Accepted:
- The three blockers:
  - a PG-dependent admin check outside the recoverable init;
  - pools without an `error` listener (an idle-client error crashes the process), and no query
    timeouts;
  - no integration harness.
- Single-flight retries with disposal, and no SQLite fallback.
- The throttle key.
- The break-glass boundary: loopback limits where a session is issued, not where it is used, and
  break-glass needs PG. During a PG outage, use the bearer admin token. Revoke recovery sessions
  after use.
- `Secure` from the HTTPS origin.
- Escape the issuer `LIKE`.
- Document that router sessions outlive a Keycloak logout or disable, with a revocation procedure.
- Rehearse the import before restarting the shared Keycloak (done).
- A backup and a `relay` capture before PR A.
- Qualify "API keeps serving" by the Recreate gap (the edge holds requests 85 s, image requests
  10 s).
- Rollback must not revive SQLite sessions.
- `/auth/config` mode versus availability.
- Session and audit ordering.
- Use `GET /admin/v1/audit` for the viewer 403 test.
- Fresh flows for each negative test.

Partly accepted:
- **TLS:** encryption with `sslmode=no-verify`, checked in `pg_stat_ssl`. Not CA verification: CNPG's
  CA rotates, so a static copy in another namespace would go stale. The estate's other infra-pg
  clients use plaintext.
- **Restore evidence:** check that the next dump generation contains `router_auth`. A full restore
  drill stays with the estate's backup drill.
- **Real-browser check:** the owner's own sign-in is the real-browser run. The negative cases are
  scripted.

Rejected:
- **A separate migration identity.** One owner role, as for Keycloak. The runtime needs DML on every
  table anyway, the schema file is idempotent, and a second URL would still sit in the same pod.

## Outcome (2026-10-26)

| Phase | Result |
|---|---|
| 1. Prerequisites | ailab #1441, merged 19:56 UTC. The import was rehearsed first. `relay` stayed byte-identical. |
| 2. Live steps | The DB was bootstrapped, with router login over TLS 1.3. The realm, client and secret hash were verified, and the 19-path edge matrix passed. The administrator was created: `83354ded-97b6-417f-8267-566ab816c4fd`. |
| 3. Hardening | llm-router #123, merged after three review rounds: reviewer-codex, reviewer-claude and a separate codex implementation review. It also closes #120. The integration suite runs in CI against a real PostgreSQL. Against the #117 code the suite fails: the process exits on dropped connections, and the router never becomes ready with the issuer down. |
| 4. Isolated end-to-end run | 11/11 against the real realm, twice: on the branch and on the released image `sha256:2e4211a3…`. |
| Hardened image roll (local mode) | ailab #1442, merged 20:58 UTC. Image `registry.chifor.me/llm-router/router@sha256:2e4211a3549b9c98f8896d1e06105c62ed22c7c6803c005acfcead4ba32df6b5` (llm-router `171ff37b`). Public checks 17/17. `validate-live` matched the baseline, with images skipped (cloud2 off at night). |
| 5. Switch | ailab #1443, merged 21:15 UTC; the pod was ready at about 21:16. Post-switch checks and `validate-live` as recorded in the runbook. |

Follow-ups:
- the owner's first sign-in;
- a membership API or UI in llm-router (members other than `ROUTER_AUTH_ADMIN_SUBJECT` need SQL today).
