# Proxied CNAMEs for every tunnel hostname -> <tunnel>.cfargotunnel.com. Pre-existing records
# (home/sso/chat/grafana/api) are adopted via import (imports.tf) so apply does NOT recreate them;
# NEW records created here are status.chifor.me and the dev-worker terminals dw1/dw2/dw3.chifor.me.
#
# SECURITY (dev-worker shells): a dwN host only reaches the passwordless-sudo ttyd shell once its
# CNAME RESOLVES — which happens ONLY here. The depends_on below forces the Access apps (access.tf)
# to exist FIRST, so Access is already enforcing the instant a dwN name resolves; and any partial or
# failed apply leaves the CNAME absent (fail-safe — no exposure). The cloudflared ingress rule alone
# is inert until a name resolves, so the order in which Flux syncs the ingress does not matter.
locals {
  tunnel_target = "${var.tunnel_id}.cfargotunnel.com"
}

resource "cloudflare_dns_record" "tunnel" {
  for_each = toset(var.tunnel_hostnames)

  zone_id = var.zone_id
  name    = "${each.key}.chifor.me"
  type    = "CNAME"
  content = local.tunnel_target
  ttl     = 1    # 1 = automatic (required while proxied)
  proxied = true # tunnel CNAMEs must be proxied

  # Create the dev-worker Access apps (access.tf) BEFORE any of these CNAMEs, so dwN.chifor.me is
  # already Access-gated the moment it resolves — never an unauthenticated window to the ttyd shell.
  # No-op ordering for the other (already-imported) records.
  depends_on = [
    cloudflare_zero_trust_access_application.dev_worker,
    cloudflare_zero_trust_access_application.k8s_tools,
  ]
}
