output "tunnel_cname_records" {
  description = "Managed proxied CNAMEs (hostname -> record name)."
  value       = { for k, r in cloudflare_dns_record.tunnel : k => r.name }
}

# api.chifor.me service-token credentials — set as CF-Access-Client-Id / CF-Access-Client-Secret
# request headers on every api.chifor.me caller. Sensitive: `tofu output -raw api_access_client_secret`.
# null while var.enable_api_access_gate is false (the gate is prepared but not applied).
output "api_access_client_id" {
  description = "CF-Access-Client-Id header value for api.chifor.me callers (null until enable_api_access_gate)."
  value       = one(cloudflare_zero_trust_access_service_token.api[*].client_id)
  sensitive   = true
}

output "api_access_client_secret" {
  description = "CF-Access-Client-Secret header value for api.chifor.me callers (null until enable_api_access_gate)."
  value       = one(cloudflare_zero_trust_access_service_token.api[*].client_secret)
  sensitive   = true
}

# The callback Cloudflare will use for the Authelia IdP. It MUST match a redirect_uri on the
# `cloudflare-access` client in kubernetes/apps/apps/auth/authelia-config.yaml, or Authelia rejects the
# authorization request. Null until var.authelia_access_client_secret is set. Not sensitive — it is the
# public team domain.
#
# `config.redirect_url` is the right traversal for provider v5 (there is no top-level attribute; it has
# been nested since v4.52.0, where the difference was only block-list `config[0]` vs object `config`).
# It may still come back NULL: `config` is one combined schema across every IdP type, and Cloudflare's
# documented OIDC response does not include a callback. If it is null, that is not a failure — compare
# against the expected team-domain callback by hand:
#   https://chifor.cloudflareaccess.com/cdn-cgi/access/callback
output "access_idp_redirect_url" {
  description = "Cloudflare's OIDC callback for the Authelia IdP; must match the client's redirect_uris in Authelia."
  value       = one(cloudflare_zero_trust_access_identity_provider.authelia[*].config.redirect_url)
}

# Each dev-worker terminal's Access AUD tag. The worker-side JWT validator (ansible dev_worker role,
# web_gate.yml) accepts only tokens minted for its own application, so these values are copied into
# ansible/group_vars/dev_workers.yml `dev_worker_web_access_aud`. Not sensitive: every JWT carries it.
output "dev_worker_access_aud" {
  description = "AUD tag per dev-worker Access application, keyed by ansible inventory hostname."
  value       = { for k, app in cloudflare_zero_trust_access_application.dev_worker : "dev-worker-${trimprefix(k, "dw")}" => app.aud }
}
