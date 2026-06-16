# Proxied CNAMEs for every tunnel hostname -> <tunnel>.cfargotunnel.com. This is the IaC home for the
# records previously created imperatively via `cloudflared tunnel route dns`. The pre-existing records
# (home/sso/chat/grafana/api) are adopted via import (imports.tf) so apply does NOT recreate them; the
# only record this CREATES is the one still missing (status.chifor.me, which is currently 530-ing).
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
}
