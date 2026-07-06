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
- Future: raise to `two_factor` (TOTP/WebAuthn); codify Cloudflare Access (CF Terraform provider);
  add more apps as OIDC clients; revisit Authentik only if central RBAC/passkeys/outpost are needed.
