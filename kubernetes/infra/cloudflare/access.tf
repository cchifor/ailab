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
