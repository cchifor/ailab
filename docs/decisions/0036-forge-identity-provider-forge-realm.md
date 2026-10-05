# ADR 0036 — Forge's ailab identity provider is a dedicated `forge` realm in the existing Keycloak

**Status:** ACCEPTED (2026-10-05), owner-directed. The Forge release agent asked for "the ailab OIDC
issuer/client intended for Forge", then for "the operator-approved configuration or its repository
location". Neither existed. The owner was offered four options (below) and chose the dedicated realm
in the existing Keycloak. Asked whether to write it down where the agent can cite it: "do it".
**Not provisioned yet** — see § Status of provisioning.
**Relates to:** ADR 0012 (Authelia SSO), ADR 0034 (rule A4: a change project A needs in project B is
A's PR in B). In cchifor/forge: `docs/ailab.md`, `docs/access.md`,
`deploy/ailab/catalog.template.json`, forge#24 (release publication + the ailab catalog template).

## Context

**What Forge needs from an identity provider** (cchifor/forge `docs/ailab.md`, `docs/access.md`):

- An **HTTPS Keycloak realm** as issuer. Its value goes into every ailab backend catalog
  (`REQUIRED_AILAB_OIDC_ISSUER`), so changing it later changes catalog bytes and voids that
  catalog's qualification.
- A **public browser client `forge-browser`**: authorization code flow, S256 PKCE, explicit
  callback/logout URIs and origins.
- Access tokens with **audience `forge-api`**.
- **Permission scopes**: non-composite realm roles, one per permission, with client scopes mapped to
  them.
- A **role adapter**: a private client-credentials client that reads users/groups and manages
  Forge-owned composite roles and memberships through Keycloak's role-composite and user
  role-mapping admin APIs, plus an eligible-user group (its UUID is config). The adapter's database
  binds to provider endpoint, client ID and group, so a new realm later means a new database and a
  reviewed migration.

Forge's docs already warn that the existing ailab Keycloak "is managed by the Strive platform.
Selecting its issuer does not grant Forge permission to manage Strive roles or users. Provision an
application-specific realm or explicitly bounded client/group/role configuration."

**What ailab has (checked 2026-10-05):**

| | Authelia | Keycloak |
|---|---|---|
| Issuer | `https://sso.chifor.me` (S256 PKCE supported) | `https://auth.strive.place/realms/<realm>` |
| Owner | ailab (`kubernetes/apps/apps/auth/`) | cchifor/platform (ns `strive-ailab`) |
| Forge clients | none | none: realm `strive` serves the platform; `forge` → discovery 404 |
| Role/group admin API | **none**: no service accounts, no roles | yes |
| Public admin path | n/a | `/admin/*` and `/realms/master/*` return 404 at the edge (by design) |
| In-cluster | — | `keycloak.strive-ailab.svc:9180`, behind NetworkPolicy `keycloak` with per-consumer allowances (`e2e-to-keycloak`, `load-personas-to-keycloak`) |

The platform seeds its realm with `deploy/components/keycloak-realm-seed`: a Job that runs
`kcadm create realms` **only if the realm is absent**, followed by a sync Job (`infra/keycloak-sync`)
that reconciles a fixed list of settings on the `strive` realm over the admin API. Both handle
exactly one realm.

## Options offered

1. **A dedicated `forge` realm in the existing Keycloak** — chosen.
2. **A separate ailab-owned Keycloak** (e.g. `id.chifor.me`) brokering login to Authelia. Rejected:
   one more stateful service, database and backup to run for a single consumer.
3. **Authelia directly**: public `forge-browser` + PKCE, audience `forge-api`. Rejected: login works,
   but there is no role adapter. Authelia has no admin API, service accounts or roles, and Forge's
   adapter is written against Keycloak's.
4. **Defer.** Rejected: leaves every ailab catalog without an issuer.

## Decision

1. **Issuer `https://auth.strive.place/realms/forge`.** A new realm `forge` in the strive-ailab
   Keycloak. Forge's users, groups, roles and clients exist only there. Forge does not use the
   `strive` realm, and nothing Forge runs gets a grant outside `forge`. The disposable qualification
   realm from Forge's tests is never imported here.
2. **Browser client `forge-browser`**: public; standard (authorization code) flow only, with no
   implicit and no direct access grants; PKCE S256 required. Redirect URIs, post-logout redirect URIs
   and web origins are explicit per application placement, with no wildcards.
3. **Audience `forge-api`**, added to access tokens by an audience mapper on a client scope assigned
   to `forge-browser`.
4. **Permissions** follow Forge's `docs/access.md`: one non-composite realm role per permission, client
   scopes mapped to them, requested through `forge-browser`.
5. **Role adapter**: a confidential client-credentials client (`forge-access-adapter` in Forge's
   docs). Its service account gets only `forge`-realm user/group read and role/membership management,
   using fine-grained admin permissions where Keycloak allows. There is also a dedicated eligible-user
   group, with service accounts kept out of it. The client secret and group UUID are private runtime
   configuration in an operator-managed Secret; **they are not catalog inputs**. Where the secret is
   escrowed is decided when the realm is provisioned.
6. **The adapter reaches Keycloak in-cluster.** It calls the admin API at
   `keycloak.strive-ailab.svc:9180`, which needs its own allowance in the Keycloak NetworkPolicy. The
   public edge keeps returning 404 for `/admin/*`; this decision does not open it.
7. **Ownership.** The Keycloak instance belongs to cchifor/platform, so the realm is defined there:
   - a seed for `forge` alongside `keycloak-realm-seed`;
   - an update path for existing clients, because the seed only creates a missing realm and redirect
     URIs will change as placements are added;
   - the NetworkPolicy allowance.

   Per ADR 0034 rule A4 this is a **Forge-authored PR to cchifor/platform**, reviewed by platform.
   Nothing in ailab's Flux tree changes. This ADR is ailab's record of the choice.

### Status of provisioning

As of 2026-10-05 the `forge` realm does not exist
(`https://auth.strive.place/realms/forge/.well-known/openid-configuration` → 404). Forge may put the
issuer above into catalog **candidates** now. Qualification against the provider waits until that
discovery URL returns 200 with `"issuer": "https://auth.strive.place/realms/forge"`.

## Consequences

- **Positive:**
  - The realm boundary isolates Forge from Strive users and roles without a new service to run.
  - Forge gets the Keycloak semantics its adapter is built for.
  - The issuer is recorded before catalogs bake it in.
- **Negative:**
  - Forge's sign-in depends on a platform-owned instance: its upgrades, outages and seeding
    behaviour.
  - The issuer sits on the `strive.place` host.
  - The `forge` realm has its own user store. Single sign-on with Authelia needs the realm to broker to
    Authelia as an upstream OIDC provider. That is not decided here; the realm PR may add it.
- **Risks and mitigations:**
  - *The platform seeding tools are single-realm.* The realm PR has to add `forge` without weakening
    the `strive` realm's create-if-absent guard.
  - *Tenant realms.* The platform may create realms at runtime (the seed Job's guard protects them),
    so `forge` is treated as reserved.
  - *Admin endpoint derivation.* If Forge's adapter derives its admin URL from the public issuer, it
    will hit the edge 404. It then needs a separate admin base URL, which is a Forge change.

## Revisit when

- The platform moves, replaces or retires this Keycloak.
- Forge has to stay up independently of the platform's Keycloak.
- A second ailab application needs Keycloak-style roles. A shared ailab identity service could then
  pay for itself (option 2).
