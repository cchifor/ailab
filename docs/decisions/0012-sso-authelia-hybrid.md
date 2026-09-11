# ADR 0012 — Self-hosted SSO: Authelia (OIDC) hybrid

**Status:** ACCEPTED + DEPLOYED (2026-06-15) — Authelia live + OIDC-verified; Grafana + Open WebUI wired;
**end-to-end browser SSO pending the user's Cloudflare DNS/Access step (below).**
**Relates to:** ADR 0007 (exposure: Cloudflare Access + Tailscale).

## Context
Grafana and Open WebUI each had their own login; "log in once" was the goal. The lab already has two
partial SSO layers — Cloudflare Access (public edge) and Tailscale (private mesh) — but neither gives a
single in-app identity across both paths. A multi-agent eval + judge picked **self-host Authelia (OIDC)**
over Authentik (too heavy: ~1.5–2 GiB + Postgres vs the tight ADR 0009 budget) and over Cloudflare-Access-
only (SaaS, public-only, header-spoof risk). The cluster has **no ingress controller / no Gateway API**
and cloudflared can't do forward-auth — so the design uses **native OIDC redirect** (no proxy needed),
not Authelia's forward-auth mode.

## Decision
- **Authelia 4.39** in ns `auth` — single Go binary (~64–192 Mi), file user store (SOPS), local SQLite on
  a `qnap-iscsi` PVC, OIDC provider at **`sso.chifor.me`** (published via cloudflared). Config = a readable
  ConfigMap + a SOPS `oidc-jwks.yml` fragment (RSA issuer key); 4 runtime secrets via `*_FILE` env.
- **Grafana** = OIDC client (`grafana.ini auth.generic_oauth`, PKCE/S256); **Open WebUI** = OIDC client
  (`OAUTH_*` env). Client secrets in their own SOPS secrets. Group→role: Authelia `admins`→Grafana Admin,
  `openwebui-admin`→Open WebUI admin. **Both keep a local break-glass login.**
- **LiteLLM stays master-key** (programmatic OpenAI endpoint, not a browser OIDC client) — correct, not a gap.
- **Cloudflare Access kept** as the outer edge gate; `sso.chifor.me` must NOT be behind Access (it is the
  IdP). For a single prompt, relax Access on grafana/chat and let Authelia gate.

## Deployment gotchas (hard-won — all fixed; the startup failures were silent)
Authelia aggregates startup-check errors and logs **no per-error detail**, which masked these:
1. **`command: ["authelia"]`** — the image entrypoint does `exec "$@"`; passing bare `--config` broke it.
2. **`enableServiceLinks: false`** — the Service named `authelia` injects `AUTHELIA_PORT`/`AUTHELIA_SERVICE_*`
   env, which Authelia parses as config (`AUTHELIA_PORT`→deprecated `port`→conflicts with `server.address`).
3. **`ntp.disable_startup_check: true`** — the pod has no UDP/123 egress; the NTP check is fatal by default
   (this was the silent killer).
4. **`readOnlyRootFilesystem: false`** — Authelia writes outside `/data`+`/tmp` at startup.
5. Live `kubectl patch` created field-manager conflicts vs Flux SSA → had to delete+recreate the
   Deployment/ConfigMap for Flux to apply the corrected spec.
Verified: `/api/health` 200; OIDC discovery (with `X-Forwarded-Proto: https`, `Host: sso.chifor.me`) returns
issuer `https://sso.chifor.me` + authorization/token/userinfo/jwks endpoints + PKCE S256.

## Remaining — user-side Cloudflare (only the user can do these)
1. **DNS:** `cloudflared tunnel route dns ailab sso.chifor.me` (the in-cluster route is already in git).
   The whole flow is gated on this — Grafana/Open WebUI call `https://sso.chifor.me` server-side too.
2. **Access:** create NO Access app for `sso.chifor.me` (it's the IdP); for a single prompt, set
   grafana/chat Access to bypass and let Authelia gate (or accept a double prompt).
3. **Test:** browse to grafana.chifor.me / chat.chifor.me → "Sign in with Authelia" → log in as the lab
   user → land in the app. Break-glass: Grafana local admin; Open WebUI local login.

## Consequences
- One login (optionally 2FA later — currently `one_factor`) across Grafana + Open WebUI, over both the
  Cloudflare-public and Tailscale-private paths (same issuer).
- Authelia is a single-replica login SPOF (RWO iSCSI, Recreate); existing sessions survive an outage;
  `platform-normal` priority so it never preempts etcd/monitoring. Break-glass logins remain.
  - **Update (2026-07-06, ADR 0016):** no longer true — Authelia runs **2 replicas** backed by
    infra-pg Postgres (storage) + auth-valkey (shared sessions); the PVC/SQLite/Recreate constraint
    is gone. Sessions are now server-side in auth-valkey (accepted-ephemeral: a valkey bounce = one
    estate-wide re-login).
    **Cutover constraint (one-time, order-sensitive):** the SQLite→Postgres switch does NOT migrate
    data, and Authelia mints new OIDC opaque identifiers (`sub`) on demand — Gitea links accounts by
    `sub`, so skipping this silently unlinks Gitea accounts. BEFORE merging the switch, from the old
    pod: `kubectl --context admin@ai exec -n auth deploy/authelia -- authelia storage user
    identifiers export --file /tmp/ids.yml --config /config/configuration.yml`, then `kubectl cp` it
    out. AFTER the new pods are up and BEFORE anyone logs in: `kubectl cp` it into a new pod and run
    `authelia storage user identifiers import --file ... --config /config/configuration.yml`.
- **Passkeys + session lifetimes (2026-09-11).** `webauthn.enable_passkey_login` — a WebAuthn credential
  (on Windows: **Windows Hello**, face/PIN) is accepted *instead of* username+password and satisfies the
  `one_factor` default policy on its own; the password form stays as the fallback, so this adds a login
  method rather than replacing one. RP ID = the PORTAL hostname `sso.chifor.me` (Authelia derives it from
  the request origin, not from the cookie domain); ONE credential still covers every app, but via the
  `chifor.me` session cookie the portal issues, not via the RP ID. Credentials live in
  `webauthn_credentials` on infra-pg (the ADR 0016 move to Postgres is what makes them replica-safe).
  Registration needs an elevated session whose One-Time Code goes out through the `notifier` — which is
  `filesystem`, there being no SMTP relay — hence `code_lifespan: 15 minutes` and the
  read-it-out-of-the-pod ceremony in `docs/runbooks/passkeys.md`.
  Because a passkey is now the whole login, `selection_criteria.user_verification` is pinned to `required`
  — at the `preferred` default an authenticator may skip user verification, so a PIN-less roaming key could
  have satisfied `one_factor` on possession alone, verifying no factor at all. Pinned while
  `webauthn_credentials` was still empty, so no credential had to be re-enrolled.
  The session cookie moved from `1 hour` / `inactivity: 5 minutes` to **12h / 8h idle**: five idle minutes
  was logging the operator out several times a day. `remember_me` went to **`-1` (disabled)** in the same
  change, because Authelia exempts a remembered session from `inactivity` altogether and gives it the
  `remember_me` lifetime in place of `expiration` — leaving it at the old `1 month` would have made a
  month-long, idle-immune session the real ceiling instead of 12h, and would have left credential
  revocation unable to cut existing access. Disabling it is what makes 12h true for every session; the
  cost is one login a day, which passkeys reduce to a face scan. To cut sessions immediately, bounce
  auth-valkey.
  This also retires the "revisit Authentik if passkeys are needed" clause: 4.39 has them natively.
- Future: raise to `two_factor` where it is worth the friction (passkeys make this cheap now); add more
  apps as OIDC clients; **wire Authelia as the Cloudflare Access OIDC IdP** so the Access-gated hosts stop
  falling back to emailed one-time PINs and inherit the same passkey (ADR 0007 hardening step; Access
  itself is already codified in `kubernetes/infra/cloudflare/access.tf`).
