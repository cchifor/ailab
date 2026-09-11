# Cloudflare Access for the dev-worker web terminals (dw1/dw2/dw3.chifor.me).
#
# ttyd is a PASSWORDLESS-SUDO shell, so every dev-worker hostname MUST be gated: each gets a
# self_hosted Access application with an interactive Allow policy locked to allow_email. dns.tf
# `depends_on` these apps, so Access is enforcing BEFORE the hostname ever resolves — there is no
# window where dwN.chifor.me reaches the shell unauthenticated. (The status/api scaffolding stays
# opt-in in access.tf.example; this file activates only the dev-worker apps.)

variable "allow_email" {
  description = "Cloudflare Access allow-list email for the interactively-gated dev-worker terminals. This is the ONLY identity allowed to the sudo shells — keep it correct."
  type        = string
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.allow_email))
    error_message = "allow_email must be a valid email address (the sole identity allowed to the dev-worker shells)."
  }
}

# One reusable Allow policy (the operator's email) shared by all three dev-worker apps.
resource "cloudflare_zero_trust_access_policy" "allow_me" {
  account_id = var.cloudflare_account_id
  name       = "Allow ${var.allow_email}"
  decision   = "allow"
  include    = [{ email = { email = var.allow_email } }]
}

# ─── Identity: Authelia as a generic OIDC IdP for Access ──────────────────────────────────────────
#
# Inverts the usual relationship: the EDGE GATE becomes a relying party of the in-cluster IdP. Until
# now Access's only login method was an emailed one-time PIN (verified against the API: the account had
# exactly one identity provider, type `onetimepin`), so every Access re-auth meant fetching a PIN out of
# a mailbox — several times a day, because the sensitive apps below deliberately hold 30m sessions.
#
# With this IdP the Access-gated hosts delegate to Authelia and inherit its passkey (Windows Hello) —
# and, more importantly, they inherit its SESSION: the Authelia cookie outlives the per-app Access
# token, so an expiring 30m Access session becomes a silent redirect through sso.chifor.me instead of a
# fresh login. That is what makes it safe to KEEP the short windows on Prometheus/Alertmanager/OpenBao
# rather than buying comfort by lengthening them.
#
# DELIBERATELY NOT SET: `allowed_idps` / `auto_redirect_to_identity` on the applications below. Leaving
# them unset means Access offers BOTH this IdP and One-time PIN, costing one click on the login page and
# buying the recovery path: Authelia runs in the very cluster that `proxmox`/`qnap` exist to repair. If
# it were the only way in, a dead Authelia (or a dead infra-pg under it) would lock the operator out of
# the hypervisor UI needed to revive it — the same bootstrap loop that cost 4.5h when Flux sourced from
# the forge it was needed to repair (ADR 0017). One-time PIN stays as the break-glass login, always.
#
# `sso.chifor.me` must therefore STILL never get an Access application — doubly so now, since Access
# itself calls its authorization/token/jwks endpoints. See the per-host table in
# docs/runbooks/cloudflare-access-apps.md.
#
# Gated on var.authelia_access_client_secret being non-empty so `tofu plan` stays clean until the
# operator opts in (same pattern as var.enable_api_access_gate). The endpoints below are not guesses —
# they are Authelia's own discovery document (`/.well-known/openid-configuration`, issuer
# `https://sso.chifor.me`).
resource "cloudflare_zero_trust_access_identity_provider" "authelia" {
  count = var.authelia_access_client_secret != "" ? 1 : 0

  account_id = var.cloudflare_account_id
  name       = "Authelia" # shown on the Access login page as "Sign in with Authelia"
  type       = "oidc"
  config = {
    client_id     = "cloudflare-access" # the client registered in authelia-config.yaml
    client_secret = var.authelia_access_client_secret
    auth_url      = "https://sso.chifor.me/api/oidc/authorization"
    token_url     = "https://sso.chifor.me/api/oidc/token"
    certs_url     = "https://sso.chifor.me/jwks.json"
    scopes        = ["openid", "profile", "email", "groups"]
    pkce_enabled  = true # Authelia's client sets require_pkce: true — both sides must agree
  }
}

# self_hosted Access apps are DEFAULT-DENY: a request only reaches the origin (tunnel -> Caddy -> ttyd)
# if it matches an attached allow policy. Everything else gets the Access login page and never the
# shell. The single allow_me policy is the whole allow-list — do not add a `bypass`/`allow` policy here
# without understanding that it would open the shell.
resource "cloudflare_zero_trust_access_application" "dev_worker" {
  for_each = toset(["dw1", "dw2", "dw3", "dw4", "dw5", "dw6"])

  account_id       = var.cloudflare_account_id
  name             = "${each.value} dev-worker terminal"
  type             = "self_hosted"
  domain           = "${each.value}.chifor.me"
  session_duration = "8h" # short-ish for a root shell; re-auth daily
  policies         = [{ id = cloudflare_zero_trust_access_policy.allow_me.id, precedence = 1 }]
}

# Cloudflare Access for the in-cluster k8s tools: k8s.chifor.me = Headlamp (read-only cluster
# explorer; the UI can read Secrets) and hubble.chifor.me = Cilium Hubble UI (network flows). Both
# expose cluster internals, so they get the same default-deny + allow_me gate. dns.tf `depends_on`
# these so Access enforces BEFORE the hostname resolves.
resource "cloudflare_zero_trust_access_application" "k8s_tools" {
  for_each = {
    k8s    = "Headlamp (k8s cluster explorer)"
    hubble = "Hubble UI (Cilium network flows)"
  }

  account_id       = var.cloudflare_account_id
  name             = each.value
  type             = "self_hosted"
  domain           = "${each.key}.chifor.me"
  session_duration = "24h"
  policies         = [{ id = cloudflare_zero_trust_access_policy.allow_me.id, precedence = 1 }]
}

# dsh — DeepSeek Harness agent UI. Same gate as the dev-worker terminals, and for the same reason:
# this runs model-generated tool calls, and SAFETY.md states the project is experimental and
# unaudited and that its own sandbox "cannot protect resources that execution is permitted to use".
#
# Access is doing MORE work here than for the other apps. dsh has a browser-session cookie, but that
# cookie carries NO SSO identity -- it is minted by exchanging a one-time startup token and is bound
# only to the authority. So dsh cannot tell one person from another, and Access is the only
# per-person gate in front of it. session_duration matches the dev-worker terminals rather than the
# 24h k8s-tools window, because the blast radius is comparable to a shell.
resource "cloudflare_zero_trust_access_application" "dsh" {
  account_id       = var.cloudflare_account_id
  name             = "dsh (DeepSeek Harness agent UI)"
  type             = "self_hosted"
  domain           = "dsh.chifor.me"
  session_duration = "8h"
  policies         = [{ id = cloudflare_zero_trust_access_policy.allow_me.id, precedence = 1 }]
}

# Cloudflare Access for the admin UIs now published to the WAN (PR #24; ratified in ADR 0007).
# Proxmox + QNAP have their OWN logins, so Access is defense-in-depth in front of a hypervisor / NAS.
# Prometheus + Alertmanager have NO native auth, so Access is the ONLY thing between the internet and
# your metrics/alerts — and the Alertmanager UI can silence every alert — so they get a tight session
# as partial mitigation. All gated to allow_me. dns.tf `depends_on` these so Access enforces BEFORE the
# hostnames resolve.
#
# HARDENING ROADMAP (ADR 0007): allow_me is single-factor. The IdP half is now codified — see
# `cloudflare_zero_trust_access_identity_provider.authelia` above — so the login can be a passkey
# (Windows Hello) instead of an emailed PIN. Still NOT enforced MFA: Authelia's policy for this client
# is `one_factor`, i.e. a strong single factor, so a `require` rule still has nothing to require. Raise
# the client to `two_factor` in authelia-config.yaml first, then add `require` here — or keep
# Alertmanager on the Tailscale admin mesh.
resource "cloudflare_zero_trust_access_application" "admin_uis" {
  for_each = {
    proxmox      = "Proxmox VE"
    qnap         = "QNAP NAS"
    prometheus   = "Prometheus"
    alertmanager = "Alertmanager"
  }

  account_id = var.cloudflare_account_id
  name       = each.value
  type       = "self_hosted"
  domain     = "${each.key}.chifor.me"
  # No-native-auth UIs (Prometheus/Alertmanager) re-auth every 30m; the own-login hosts (Proxmox/QNAP)
  # get 8h since Access is only their second factor.
  session_duration = contains(["prometheus", "alertmanager"], each.key) ? "30m" : "8h"
  policies         = [{ id = cloudflare_zero_trust_access_policy.allow_me.id, precedence = 1 }]
}

# Vaultwarden /admin — PATH-SCOPED Access. The apex vault.chifor.me is deliberately Access-FREE so the
# Bitwarden native clients (/api, /identity, /notifications/hub) authenticate machine-to-machine (they
# can't do the Access browser SSO). Only /admin* — the dangerous server-config surface, which native
# clients never touch — is gated to allow_me. More-specific path wins; the apex stays open.
resource "cloudflare_zero_trust_access_application" "vault_admin" {
  account_id       = var.cloudflare_account_id
  name             = "Vaultwarden /admin"
  type             = "self_hosted"
  domain           = "vault.chifor.me/admin"
  session_duration = "24h"
  policies         = [{ id = cloudflare_zero_trust_access_policy.allow_me.id, precedence = 1 }]
}

# openbao.chifor.me — the OpenBao UI (kubernetes/apps/infrastructure/security/openbao). This is the
# root of the estate's secret trust: every ExternalSecrets SecretStore and every agentforge workload
# reads from this instance, and a UI session carries an OpenBao token.
#
# Gated to allow_me at the FULL apex (no path scoping). Unlike Vaultwarden and Gitea there is nothing
# to break by gating everything: the only machine clients of OpenBao are IN-CLUSTER and reach it over
# the ClusterIP Service, which never traverses Cloudflare — so no non-browser caller can be affected by
# this application. The hostname serves a browser UI and nothing else.
#
# DOUBLE LOGIN IS EXPECTED: Access authenticates the human at the CF edge, then OpenBao's own UI asks
# for a token. That is deliberate defense-in-depth for a secret store — do NOT "fix" it by removing
# this app. It also departs from the Access-free convention used for own-auth apps (Gitea, Vaultwarden
# apex, AgentForge CP); the departure is justified by the blast radius of this particular UI.
#
# 30m session — the same tight window given to the no-native-auth UIs above. OpenBao's own token login
# is the second factor, but an Access session on this hostname is the outer door to every secret in the
# estate, so it is not left open for 8h/24h.
resource "cloudflare_zero_trust_access_application" "openbao" {
  account_id       = var.cloudflare_account_id
  name             = "OpenBao (secrets manager)"
  type             = "self_hosted"
  domain           = "openbao.chifor.me"
  session_duration = "30m"
  policies         = [{ id = cloudflare_zero_trust_access_policy.allow_me.id, precedence = 1 }]
}

# api.chifor.me — the LiteLLM OpenAI-compatible proxy. It is a MACHINE API (the Strive platform +
# programmatic clients), so it gets a SERVICE TOKEN / non_identity policy — an interactive email/IdP
# gate would break non-browser callers. The LITELLM_MASTER_KEY stays the app-level auth; this adds a
# Cloudflare-edge gate so a leaked master key alone can't reach the (paid) cloud models registered in
# litellm-config. (See docs/runbooks/cloudflare-access-apps.md and the api.chifor.me spend cap in
# kubernetes/apps/apps/ai/litellm.yaml `max_budget`.)
#
# ⚠️ Gated behind var.enable_api_access_gate (default FALSE) so `tofu plan` stays clean until you opt
# in — these resources are prepared but NOT applied. BEFORE flipping the flag to true + `tofu apply`:
# wire the token into EVERY api.chifor.me caller (the Strive platform and any script) as the
# CF-Access-Client-Id + CF-Access-Client-Secret request headers, or they get a 401 at the edge. Then
# retrieve the values with `tofu output -raw api_access_client_secret` (+ _client_id).
resource "cloudflare_zero_trust_access_service_token" "api" {
  count      = var.enable_api_access_gate ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "api-chifor-me"
}

resource "cloudflare_zero_trust_access_policy" "api_svc" {
  count      = var.enable_api_access_gate ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Allow api service token"
  decision   = "non_identity"
  include    = [{ service_token = { token_id = cloudflare_zero_trust_access_service_token.api[0].id } }]
}

resource "cloudflare_zero_trust_access_application" "api" {
  count            = var.enable_api_access_gate ? 1 : 0
  account_id       = var.cloudflare_account_id
  name             = "api.chifor.me"
  type             = "self_hosted"
  domain           = "api.chifor.me"
  session_duration = "24h"
  policies         = [{ id = cloudflare_zero_trust_access_policy.api_svc[0].id, precedence = 1 }]
}
