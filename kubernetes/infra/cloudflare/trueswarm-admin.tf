# The dedicated, locally-managed tunnel is created with cloudflared, outside this
# state. Its credentials live in the private trueswarm-admin SOPS deployment source.
# This module continues to require DNS + Access permissions ONLY.
variable "enable_trueswarm_admin" {
  description = "Provision the dedicated Access gate; enable only after the Authelia clients are ready."
  type        = bool
  default     = false
}

variable "publish_trueswarm_admin" {
  description = "Publish DNS after the internal deployment and isolation checks pass."
  type        = bool
  default     = false
}

variable "trueswarm_admin_tunnel_id" {
  description = "UUID of the tunnel created with cloudflared tunnel create trueswarm-admin; never Terraform-owned."
  type        = string
  default     = "d2452442-efae-4056-ac82-a5c348033971"
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.trueswarm_admin_tunnel_id))
    error_message = "A valid locally-managed tunnel UUID is required."
  }
}

variable "trueswarm_admin_emails" {
  description = "Named administrators; the dedicated Authelia client also enforces MFA and its user allowlist."
  type        = set(string)
  default     = ["chifor@gmail.com"]
  validation {
    condition     = length(var.trueswarm_admin_emails) > 0 && alltrue([for email in var.trueswarm_admin_emails : can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", email))])
    error_message = "At least one explicit administrator email is required."
  }
}

variable "trueswarm_admin_access_client_secret" {
  description = "Dedicated cloudflare-trueswarm-admin OIDC client secret; decrypt from private SOPS on the operator workstation only. Never put estate credentials in Actions."
  type        = string
  sensitive   = true
  default     = ""
}

resource "cloudflare_zero_trust_access_identity_provider" "trueswarm_admin" {
  count      = var.enable_trueswarm_admin ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Trueswarm administrator MFA"
  type       = "oidc"
  config = {
    client_id     = "cloudflare-trueswarm-admin"
    client_secret = var.trueswarm_admin_access_client_secret
    auth_url      = "https://sso.chifor.me/api/oidc/authorization"
    token_url     = "https://sso.chifor.me/api/oidc/token"
    certs_url     = "https://sso.chifor.me/jwks.json"
    scopes        = ["openid", "email", "profile", "groups"]
    pkce_enabled  = true
  }
  lifecycle {
    precondition {
      condition     = length(var.trueswarm_admin_access_client_secret) >= 32
      error_message = "Set the dedicated OIDC client secret before enabling the Access gate."
    }
  }
}

resource "cloudflare_zero_trust_access_policy" "trueswarm_admin" {
  count      = var.enable_trueswarm_admin ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Trueswarm named administrators"
  decision   = "allow"
  include    = [for email in sort(tolist(var.trueswarm_admin_emails)) : { email = { email = email } }]
}

resource "cloudflare_zero_trust_access_application" "trueswarm_admin" {
  count                     = var.enable_trueswarm_admin ? 1 : 0
  account_id                = var.cloudflare_account_id
  name                      = "Trueswarm Administration"
  type                      = "self_hosted"
  domain                    = "trueswarm-admin.chifor.me"
  session_duration          = "8h"
  allowed_idps              = [cloudflare_zero_trust_access_identity_provider.trueswarm_admin[0].id]
  auto_redirect_to_identity = true
  policies                  = [{ id = cloudflare_zero_trust_access_policy.trueswarm_admin[0].id, precedence = 1 }]
}

resource "cloudflare_dns_record" "trueswarm_admin" {
  count      = var.enable_trueswarm_admin && var.publish_trueswarm_admin ? 1 : 0
  zone_id    = var.zone_id
  name       = "trueswarm-admin.chifor.me"
  type       = "CNAME"
  content    = "${var.trueswarm_admin_tunnel_id}.cfargotunnel.com"
  ttl        = 1
  proxied    = true
  comment    = "Trueswarm administration; dedicated tunnel and mandatory MFA Access gate"
  depends_on = [cloudflare_zero_trust_access_application.trueswarm_admin]
}

output "trueswarm_admin_access_audience" {
  description = "Pin this audience in the private admin deployment before publishing DNS."
  value       = try(cloudflare_zero_trust_access_application.trueswarm_admin[0].aud, null)
}
