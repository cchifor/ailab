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
