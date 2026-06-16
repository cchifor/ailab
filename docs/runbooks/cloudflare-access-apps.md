# Creating Cloudflare Access apps in the Zero Trust dashboard - ailab

This guide walks you through creating Cloudflare **Access** applications (self-hosted) for the
public hostnames served by the `ailab` Cloudflare Tunnel (`f93d9a6a-5172-43d3-8bef-13460ea7607b`).
It is written for the current (2025-2026) Cloudflare One / Zero Trust dashboard, and tailored to this
lab's identity model.

## What Access does at the edge

Cloudflare Access is an identity gate that runs **in front of your origin, at Cloudflare's edge**.
When a request hits a protected hostname, Cloudflare intercepts it *before* it reaches your tunnel
and forces the visitor to authenticate (email one-time PIN, Google/GitHub, or an OIDC/SAML IdP).
On success Access issues a signed `CF_Authorization` JWT cookie, and only then does the request flow
through the tunnel to your service. A tunnel hostname **with no Access policy is reachable by anyone
on the internet**; an Access app is what makes a public hostname private.

## The lab-specific decision up front (read this first)

This lab already runs **Authelia** as an OIDC IdP (`sso.chifor.me`), with several apps gated by it.
That changes what you should (and must not) do with Access:

- **`home.chifor.me` is ALREADY SSO-gated** via `oauth2-proxy` -> Authelia at the edge. Putting an
  Access app on it is **optional and generally not worth it** - it would force a **double login**
  (Access prompt, then Authelia prompt). Skip it, or - only if you want a single unified prompt -
  use Authelia as the Access IdP (Part 3).
- **`sso.chifor.me` MUST be excluded.** It *is* the Authelia identity provider. Every other app
  redirects to it and calls it server-side. Gating the IdP behind Access creates a redirect loop and
  **breaks every OIDC flow in the lab.** Never create an Access app for `sso.chifor.me`.
- **`api.chifor.me` is a machine API** (LiteLLM, OpenAI-compatible). An interactive Access login is a
  browser HTML redirect and would break programmatic clients. Use a **Service Token** or **Bypass**,
  or leave it master-key-authed with no Access app - **never** an interactive (email/IdP) policy.

The clean rule for this lab: **one gate per host.** Where Authelia already gates a host, don't stack
an interactive Access app on top of it.

---

## Prerequisites

1. You are logged into **`https://one.dash.cloudflare.com`** and have selected the account that owns
   the `chifor.me` zone, with the **Zero Trust** area accessible in the left nav.
2. The **`ailab` cloudflared tunnel** exists (`f93d9a6a-5172-43d3-8bef-13460ea7607b`) and is healthy.
3. **A DNS record exists for each hostname** you intend to protect (a proxied `CNAME` to
   `<TUNNEL_ID>.cfargotunnel.com`). Access only enforces on a hostname that actually resolves and
   routes to the tunnel. As of 2026-06-16 all six tunnel CNAMEs (home/sso/status/chat/grafana/api)
   exist and are **managed as code** by the OpenTofu module `kubernetes/infra/cloudflare/` (`dns.tf`) —
   add new hostnames there. The manual equivalent is `cloudflared tunnel route dns ailab <host>.chifor.me`.
   The Access application's domain **must exactly match** a tunnel public hostname / its DNS record.

---

## Part 1 - One-time setup: pick a login method

Do this once. It defines *how* a human proves who they are at the Access prompt. You can enable more
than one method and select per-app which are accepted.

### Recommended for a single user: One-time PIN (email OTP)

For one operator this is the simplest, lowest-maintenance choice: **no IdP wiring, no client secret,
no exposed service.** The user types their email on the Access login page, clicks **"Send login code"**
*(verify the exact button label in your dashboard)*, receives a PIN by email, and pastes it back. The
only configuration is putting your email into an Access policy (Part 2).

- One-time PIN is **on by default and always available** - there is usually nothing to enable.
- A PIN expires **10 minutes** after it is requested and is **single-use**.
- To confirm/inspect it: **Zero Trust > Settings > Authentication** (newer dashboards:
  **Settings > Authentication > Login methods**, or **Integrations > Identity providers**) - you
  should see **One-time PIN** listed as an accepted login method *(verify in your dashboard)*.

### Optional upgrade: Authelia as a generic OIDC IdP

Only adopt this if you want **one unified SSO prompt** shared across the edge (Access) and your origin
apps - i.e. so the user logs into Authelia once and Access silently passes through. For a solo
operator it is usually **not worth it**: it requires a publicly reachable Authelia, a managed client
secret, and PKCE config, just to avoid typing an emailed code. (See Part 3 for where it genuinely pays
off: `home.chifor.me`.)

Broad steps if you do it:

1. In Authelia, register an OIDC client for Cloudflare with redirect URI
   `https://<your-team-name>.cloudflareaccess.com/cdn-cgi/access/callback`
   (`require_pkce: true`, `pkce_challenge_method: S256`, scopes `openid profile email`). Your team name
   is under **Settings > Custom Pages / Team domain** *(verify the exact label in your dashboard)*.
2. In Cloudflare: **Settings > Authentication > Login methods > Add new > OpenID Connect** (newer:
   **Integrations > Identity providers > Add new**), then fill:

   | Field | Value |
   |---|---|
   | Name | `Authelia` |
   | App ID (client_id) | `cloudflare` |
   | Client secret | the plaintext secret (Authelia stores its hash) |
   | Auth URL | `https://sso.chifor.me/api/oidc/authorization` |
   | Token URL | `https://sso.chifor.me/api/oidc/token` |
   | Certificate (JWKS) URL | `https://sso.chifor.me/api/oidc/jwks` |
   | Proof Key for Code Exchange (PKCE) | **Enable** |
   | OIDC Claims | `preferred_username`, `email` |

   **Hard requirement:** Cloudflare's servers must reach those `sso.chifor.me` endpoints over the
   public internet, and `sso.chifor.me` must **never** have an Access app in front of it (see Part 3).

You can keep One-time PIN enabled alongside Authelia, so OTP remains a fallback.

---

## Part 2 - Worked example: a self-hosted Access app for `grafana.chifor.me`

> Note for this lab: per ADR 0012, the *recommended* end state for `grafana` is actually to **relax /
> bypass Access** and let Authelia be the single gate (see Part 3), because Grafana is already an
> Authelia OIDC client. This Part is the canonical "how to build a self-hosted Allow app" walkthrough -
> use it verbatim for a host you genuinely want gated by Access (e.g. a private `status.chifor.me`),
> and use it as the reference for the per-host matrix below.

### Navigate to the create flow

1. Go to **`https://one.dash.cloudflare.com`** and select your account.
2. Left nav: open **Zero Trust**.
3. Go to **Access controls > Applications**. *(The path is no longer `Access > Applications`; it was
   reorganized under an "Access controls" group.)*
4. Click **Create new application**. *(Older UI called this "Add an application" - verify the current
   label in your dashboard.)*
5. Choose the application type **Self-hosted and private**.
6. Within that flow, select **Add public hostname** - this is what makes it an internet-reachable
   self-hosted app (rather than a private/WARP-only app).

### Fill in the application form

7. **Application name:** `Grafana` (free text; this is the label in the App Launcher and in logs).
8. **Session Duration:** `1 week` (suggestion). For a single-user homelab a long global session means
   fewer re-prompts; shorten it only for genuinely sensitive apps. *(The dropdown ranges from
   "No duration, expires immediately" up to roughly 1 month.)*
9. **Public hostname** - this is the host being protected:
   - **Subdomain:** `grafana`
   - **Domain:** select `chifor.me` from the dropdown (it must be an active zone in this account).
   - **Path:** leave empty (protect the whole host).

   This hostname **must match** the tunnel public hostname / DNS record for `grafana.chifor.me`.
   (To protect more hosts in the same app you would use **Add public hostname** again - not needed
   here, one app per host is cleaner for this lab.)
10. **Identity providers:** select the method(s) you set up in Part 1. For OTP-only, ensure
    **One-time PIN** is ticked. If you wired Authelia and want *only* it, select **Authelia** and turn
    on **Apply instant authentication** (this skips the Cloudflare identity-chooser page when exactly
    one IdP is selected). *(The "Accept all available identity providers" toggle also exists - leave it
    off so you control which methods apply; verify the exact label in your dashboard.)*
11. Leave **Authenticate with Cloudflare One Client** off (no WARP in this lab) and leave
    **Independent MFA** off unless you want a second factor on this app specifically.
12. **Additional settings** tab - **App Launcher customization:** optionally enable showing this app in
    the App Launcher and set a logo URL, so `grafana` appears on your `*.cloudflareaccess.com` launcher
    page. *(The granular sub-toggles such as "Show application in App Launcher" live in this section -
    verify exact wording in your dashboard.)* The other Additional settings (custom block page, CORS,
    cookie SameSite/HTTP-Only/binding-cookie, "401 Response for Service Auth policies") can stay at
    their defaults for an interactive browser app.
13. Continue to the **policies** step.

### Attach the Allow policy

All Access apps are **deny-by-default**, so you need at least one **Allow** policy. Policies are now
first-class **reusable** objects, managed separately from applications and attachable to any number of
apps. (Legacy single-app policies cannot be added to newly created applications, so new apps use
reusable policies.)

14. In the app's **Access policies** section, either **Select existing policies** (to attach a reusable
    policy you already made) or **Add a policy** to create one. To create it standalone instead, go to
    **Zero Trust > Access controls > Policies > Add a policy**.
15. Configure the policy:
    - **Policy name:** `allow-chifor-email`
    - **Action:** `Allow`
    - **Session duration:** leave as default (inherits the app's 1 week), or override.
    - **Rules - Include row:** Selector **Emails** = `chifor@gmail.com`.
      *(Include = OR / "match any one". This single email is the entire allow-list.)*
16. Save the policy, ensure it is attached to the application, then finish with **Create** / **Save**.

That is a complete gate: only `chifor@gmail.com`, after passing OTP (or Authelia), reaches Grafana.

---

## Part 3 - Per-hostname matrix (what to actually do for each host)

Source of truth: `kubernetes/apps/apps/edge/cloudflared.yaml` (tunnel
`f93d9a6a-5172-43d3-8bef-13460ea7607b`). These six are the only public hostnames in the tunnel
(plus the catch-all `http_status:404`).

| Hostname | Backing service | Current auth | Recommended Access action | Why |
|---|---|---|---|---|
| `home.chifor.me` | oauth2-proxy -> Homepage | Authelia SSO via oauth2-proxy (OIDC client `homepage`) | **No Access app** (skip) - *or* use Authelia-as-Access-IdP for one prompt | Edge path already terminates at oauth2-proxy -> Authelia; an Access app = redundant second login. (k8s-followups #12) |
| `sso.chifor.me` | Authelia (`:9091`) | This **is** the Authelia OIDC IdP (`one_factor`) | **MUST NOT get an Access app** | It is the identity provider everyone redirects to and calls server-side; gating it behind Access breaks every OIDC flow. (ADR 0012; k8s-followups #9) |
| `status.chifor.me` | Gatus status page | None - public; CNAME now exists (tofu-managed) | **Leave public** (typical for a status page), *or* add an **Allow** policy (your email) if it should be private | Status pages are commonly intentionally public. Only gate if you don't want uptime data exposed. (k8s-followups #11) |
| `chat.chifor.me` | Open WebUI | Authelia OIDC client `open-webui` (PKCE/S256) | **Relax / Bypass Access** so Authelia is the single gate (or accept a double prompt) | Open WebUI is already an Authelia OIDC client; ADR 0012 intent is one prompt via Authelia. |
| `grafana.chifor.me` | Grafana | Authelia OIDC client `grafana` (PKCE/S256; group->role `admins`->Admin) | **Relax / Bypass Access** so Authelia is the single gate (or accept a double prompt) | Grafana is already an Authelia OIDC client; ADR 0012 calls for a single Authelia prompt. Grafana keeps a break-glass local admin. |
| `api.chifor.me` | LiteLLM (`:4000`) | LiteLLM master key (OpenAI-compatible machine API) | **Bypass / Service Token - do NOT use interactive (email/IdP) Access**; or leave as-is behind the master key | Machine API; an interactive Access login would break OpenAI-compatible clients. (ADR 0012; internet-exposure runbook) |

### Explicit DO-NOT list

- **`sso.chifor.me` - no Access app, ever.** It is the IdP. An Access gate on it deadlocks the OIDC
  flow (Access needs the IdP's `/api/oidc/authorization`, `/api/oidc/token`, `/api/oidc/jwks` to be
  reachable *without* a gate). If you ever feel you must touch it, the only safe option is a **Bypass** policy on its auth
  paths - but the simplest correct answer is to leave it with no Access application.
- **`api.chifor.me` - no interactive (email/IdP) Access policy.** It is a programmatic API.
  - Preferred for this lab: **leave it master-key-authed with no Access app** (the LiteLLM master key
    is the gate), **or**
  - add a **Service Token**: create the token under **Access controls > Service credentials >
    Service Tokens**, set the app's policy **Action = Service Auth** with an **Include** row using the
    **Service Token** selector, and enable **401 Response for Service Auth policies** on the app so
    non-browser clients get a clean `401` instead of an HTML login page. Clients then send
    `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers. **Bypass** is the other option but it
    disables all Access enforcement *and logging* for that traffic.
- **`home.chifor.me` - skip the Access app.** It is already locked by oauth2-proxy -> Authelia. Adding
  Access = double login. The *only* reason to add one is the single-prompt upgrade: set the Access app
  to use **Authelia as its OIDC IdP** (Part 1), so both layers share the same Authelia session and the
  second pass is a silent SSO. Otherwise, do nothing here.
- **`chat.chifor.me` / `grafana.chifor.me` - do not add an interactive Access app on top of Authelia.**
  They are already Authelia OIDC clients. If an Access app already exists on these from earlier setup,
  set its policy to **Bypass** (or remove the app) so Authelia stays the single gate. (This supersedes
  the older `internet-exposure.md` runbook guidance to add a self-hosted Access app for chat/grafana.)

> The older `docs/runbooks/internet-exposure.md` predates the SSO work and still says to add a
> self-hosted Access app for chat/grafana/api. ADR 0012 + k8s-followups #9/#12 supersede that for
> chat/grafana (relax/bypass) and for home (no app). The runbook's `api.chifor.me` guidance
> (service token + master key, not interactive) is still correct.

---

## Part 4 - Verify

For any host where you created an **Allow** Access app (e.g. a private `status.chifor.me`, or
`grafana` if you chose to gate it):

1. Open a **new incognito / private browser window** (so no existing `CF_Authorization` cookie or app
   session is reused).
2. Navigate to the hostname, e.g. `https://grafana.chifor.me`.
3. Confirm you are stopped by the **Cloudflare Access challenge** - the Access login page (email entry
   for One-time PIN, or your IdP chooser) appears *before* the app loads.
4. Authenticate: enter `chifor@gmail.com`, click **"Send login code"** *(verify the exact button label
   in your dashboard)*, paste the emailed PIN (or complete the Authelia login if you wired it).
5. Confirm you are redirected through to the app and the real UI loads. Reaching the app proves the
   Allow policy matched your email and the tunnel routing is correct.

If you instead see the app **without** any challenge, the hostname has no Allow policy (or a Bypass is
in effect) - expected for `status` (public), `chat`/`grafana` (Authelia gate), and `api`. If you get a
`530`/DNS error, the tunnel public hostname or its DNS CNAME is missing (Prerequisites step 3).

---

## Codify in IaC — module now exists

The OpenTofu module **`kubernetes/infra/cloudflare/`** (provider `cloudflare/cloudflare` v5, local
state) now manages the edge as code:

- **DNS** (`dns.tf`) — the six proxied tunnel CNAMEs, **adopted via import** (`imports.tf`, done
  2026-06-16, 0 changes). Add/remove hostnames here.
- **Access** (`access.tf.example`) — ready-to-activate `cloudflare_zero_trust_access_application` +
  `cloudflare_zero_trust_access_policy` + `cloudflare_zero_trust_access_service_token` scaffolding
  (reusable policies attached to apps by id). Rename to `access.tf` and `apply` to gate `status` or
  protect `api` with a service token. The token (DNS + Access scopes) is supplied via the
  `CLOUDFLARE_API_TOKEN` env var; `terraform.tfvars`/state are gitignored.

The tunnel **ingress** stays locally-managed in `cloudflared.yaml` (git+Flux) and is deliberately NOT
owned by Terraform. See `kubernetes/infra/cloudflare/README.md` and ADR 0001/0012.
