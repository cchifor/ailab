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

# self_hosted Access apps are DEFAULT-DENY: a request only reaches the origin (tunnel -> Caddy -> ttyd)
# if it matches an attached allow policy. Everything else gets the Access login page and never the
# shell. The single allow_me policy is the whole allow-list — do not add a `bypass`/`allow` policy here
# without understanding that it would open the shell.
resource "cloudflare_zero_trust_access_application" "dev_worker" {
  for_each = toset(["dw1", "dw2", "dw3"])

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

# Cloudflare Access for the admin UIs now published to the WAN (PR #24; ratified in ADR 0007).
# Proxmox + QNAP have their OWN logins, so Access is defense-in-depth in front of a hypervisor / NAS.
# Prometheus + Alertmanager have NO native auth, so Access is the ONLY thing between the internet and
# your metrics/alerts — and the Alertmanager UI can silence every alert — so they get a tight session
# as partial mitigation. All gated to allow_me. dns.tf `depends_on` these so Access enforces BEFORE the
# hostnames resolve.
#
# HARDENING ROADMAP (ADR 0007): allow_me is single-factor (email OTP). For real MFA on the
# no-native-auth UIs, wire an IdP that enforces MFA (e.g. Authelia-as-Access-IdP — see
# docs/runbooks/cloudflare-access-apps.md Part 1/3) and add a `require` rule, OR keep Alertmanager on
# the Tailscale admin mesh. Not codified here: email OTP is the only login method until an IdP exists,
# so adding a `require` now would lock out the sole identity.
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

# api.chifor.me — the LiteLLM OpenAI-compatible proxy. It is a MACHINE API (the Strive platform +
# programmatic clients), so it gets a SERVICE TOKEN / non_identity policy — an interactive email/IdP
# gate would break non-browser callers. The LITELLM_MASTER_KEY stays the app-level auth; this adds a
# Cloudflare-edge gate so a leaked master key alone can't reach the (paid) cloud models registered in
# litellm-config. (See docs/runbooks/cloudflare-access-apps.md and the api.chifor.me spend cap in
# kubernetes/apps/apps/ai/litellm.yaml `max_budget`.)
#
# ⚠️ BEFORE `tofu apply`-ing this: wire the token into EVERY api.chifor.me caller (the Strive platform
# and any script) as the CF-Access-Client-Id + CF-Access-Client-Secret request headers, or they get a
# 401 at the edge. Retrieve the values with `tofu output -raw api_access_client_secret` (+ _client_id).
resource "cloudflare_zero_trust_access_service_token" "api" {
  account_id = var.cloudflare_account_id
  name       = "api-chifor-me"
}

resource "cloudflare_zero_trust_access_policy" "api_svc" {
  account_id = var.cloudflare_account_id
  name       = "Allow api service token"
  decision   = "non_identity"
  include    = [{ service_token = { token_id = cloudflare_zero_trust_access_service_token.api.id } }]
}

resource "cloudflare_zero_trust_access_application" "api" {
  account_id       = var.cloudflare_account_id
  name             = "api.chifor.me"
  type             = "self_hosted"
  domain           = "api.chifor.me"
  session_duration = "24h"
  policies         = [{ id = cloudflare_zero_trust_access_policy.api_svc.id, precedence = 1 }]
}
