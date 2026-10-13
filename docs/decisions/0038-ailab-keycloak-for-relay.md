# ADR 0038 — Relay signs in through a new ailab-owned Keycloak at `id.chifor.me`

**Status:** ACCEPTED (2026-10-13), owner-directed.

Relay's v0.5.2 release made it an OIDC relying party. Relay's own docs and the dev-worker-4 agent
assumed the platform's Keycloak (`auth.strive.place`, realm `strive`). Offered that option, the owner
chose instead: "Use a new keycloak instance with a new realm. Enable signup and sign in. google idp
will be implemented later." The first Relay member is the owner's account.

**Relates to:** ADR 0036, which put Forge in a dedicated `forge` realm on the platform Keycloak and
rejected "a separate ailab-owned Keycloak (e.g. `id.chifor.me`)" for a single consumer. Its
"Revisit when" names a second ailab application needing Keycloak. This is that revisit, made for
Relay, and it does not move Forge. Plan: `plans/2026-10-13-relay-keycloak-oidc-plan.md`.
Runbook: `docs/runbooks/keycloak.md`.

## Context

What Relay v0.5.2 needs (`server/config.ts`, `server/auth.ts` at `39767c6`):

- An HTTPS issuer with discovery.
- A confidential client: authorization code with PKCE S256, state and nonce, scope
  `openid profile email`.
- Optionally, a verified-email requirement. It is on by default.

Relay binds membership to the immutable `issuer|sub` pair. It never links an account by email.
Membership is provisioned by an operator (`dist/scripts/member.js`), so signing in grants nothing by
itself.

Why not the `strive` realm:

- The realm belongs to cchifor/platform. It holds the Strive platform's public users and
  self-registration.
- Platform's own guidance (`infra/keycloak-realms/README.md`) is to keep other applications in their
  own realms.
- The platform synchronizer handles only that realm's existing clients. It has no declarative path
  for a new client there.

## Decision

1. **A new Keycloak instance in ailab.** Namespace `keycloak`, Flux-managed
   (`kubernetes/apps/apps/keycloak/`), upstream `quay.io/keycloak/keycloak:26.7.5` pinned by digest.
   - Public origin `https://id.chifor.me` through the existing Cloudflare tunnel, with no Access
     gate: it is its own login.
   - Its database is a `keycloak` DB on the shared CNPG `infra-pg` (ADR 0016 pattern), so the nightly
     `infra-pg-dump` covers it.
2. **One realm per application.** The `relay` realm, issuer `https://id.chifor.me/realms/relay`,
   with a confidential `relay` client:
   - authorization code only, with PKCE S256;
   - an exact redirect `https://relay.chifor.me/api/auth/callback` and web origin
     `https://relay.chifor.me`;
   - no implicit, direct-grant or service-account flows.

   A later application gets its own realm here. It does not get a client in `relay`.
3. **Self-service sign-up and username/password sign-in are enabled.** Email is the username. The
   password policy is `length(12) and notUsername and notEmail`, and brute-force detection is on.
   **There is no SMTP relay in the estate**, so email verification and password reset are off.
4. **Operator-verified email.** Relay keeps `OIDC_REQUIRE_EMAIL_VERIFIED=1`. An account gets into
   Relay only after an operator has:
   - confirmed the person out of band;
   - recorded the exact Keycloak user ID (= `sub`);
   - set `emailVerified=true` on that ID;
   - bound `https://id.chifor.me/realms/relay|<id>` with `member.js`.

   A stranger's sign-up gets a Keycloak account and nothing in Relay.
5. **Abuse posture at launch.** The controls are brute-force detection, the password policy, the
   runbook's recent-registrations watch, and Relay's membership gate. There is no captcha and no
   Cloudflare rate limit yet. Revisit when the Google IdP lands, or as soon as sign-up spam is seen.
6. **Admin surface.**
   - The admin console and the `master` realm are never served publicly. On `id.chifor.me`,
     cloudflared publishes an **allowlist**, `^/realms/relay(/|$)` and `^/resources/`, and refuses
     any path containing `..` or `;` before that. Everything else is 404.
   - Why an allowlist and not a deny-list on `/admin`: cloudflared matches the decoded path
     literally, while Keycloak's HTTP layer normalizes it. Keycloak 26.7.5 was observed rejecting
     `//`, `..` and `%2e%2e` paths with 400, but the boundary must not depend on that.
   - Each new realm on this host adds its own allow line.
   - The console is reached through `kubectl port-forward` only (`KC_HOSTNAME_ADMIN=
     http://localhost:8080`).
   - The bootstrap admin stays as the break-glass master admin. Its system of record is the SOPS
     Secret, so it is not escrowed in OpenBao (`openbao-estate-credentials.md` keeps SOPS-held
     values out of `af/estate`).
7. **Realm source of truth.** `realm-relay.json` is imported only while the realm does not exist.
   Every later change is a kcadm change, written back to the JSON in the same PR. The runbook carries
   a drift check.
8. **Google later.** It will be added as a broker in the `relay` realm. That needs a Google OAuth
   redirect `https://id.chifor.me/realms/relay/broker/google/endpoint` and a public-443 egress rule.
   Relay then sets `OIDC_IDP_HINT=google`. The issuer, and so every existing membership, is
   unchanged.

## Consequences

- **Positive:**
  - Relay's identity no longer depends on the platform's Keycloak, its upgrades or its realm
    automation, or on the `strive.place` host.
  - The realm boundary keeps Strive users and Relay users apart.
  - The issuer is fixed before any membership binds to it.
- **Negative:**
  - One more stateful service to run: upgrades, a database, and backup coverage through infra-pg.
  - The stock image auto-builds at start, so `readOnlyRootFilesystem` is false. This is a recorded
    exception; a custom optimized image would remove it.
  - With no SMTP, nobody can reset a password themselves. An operator resets it.
- **Risks:**
  - *Open registration on a public origin.* Mitigated as in decision 5, and it grants nothing in
    Relay without decision 4.
  - *Import-once drift.* The realm JSON and the live realm can diverge silently; mitigated by the
    runbook's drift check and the write-back rule.
  - *Single replica, local cache.* An outage of this pod blocks new Relay sign-ins. Existing Relay
    sessions (up to 8 h) keep working.

## Revisit when

- Google sign-in is added (decision 8), or sign-up abuse is observed (decision 5).
- SMTP becomes available: turn on `verifyEmail` and reset, and reconsider decision 4.
- A third application needs an identity provider, or HA becomes a requirement (cache and replicas).
