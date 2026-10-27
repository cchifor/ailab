# One DNS record per edge host, generated from the registry kubernetes/edge/hostnames.yaml
# (ADR 0040, docs/standards/edge-ingress.md section 4). The host's EFFECTIVE mode picks the record:
#
#   tunnel   proxied CNAME -> <tunnel>.cfargotunnel.com   (Cloudflare's edge, then cloudflared)
#   direct   DNS-only CNAME -> the apex chifor.me, whose A record the home gateway's DDNS keeps on
#            the WAN address (the gateway forwards 443 to the edge proxy's VIP)
#   private  DNS-only A -> the VIP 192.168.0.51 (resolves anywhere, reachable only on the LAN/VPN)
#
# Effective mode = force_tunnel ? tunnel : the host's `edge` pin, else edge_default; private hosts
# are always private. The same rule lives in scripts/edge/render.py effective_mode() for the proxy;
# scripts/tests/test_edge_render.py holds the two in step.
#
# A mode change between a CNAME and an A record REPLACES the record (the two types cannot coexist
# on one name): a few seconds of NXDOMAIN. Rehearsed on edge-canary before any real host moved
# (docs/runbooks/edge-ingress.md).
#
# SECURITY (Access-gated hosts): while such a host is in tunnel mode its record must not resolve
# before its Access application exists, so the records keep the depends_on below; a partial or
# failed apply leaves a NEW record absent (fail-safe). The registry refuses to put an Access-gated
# host in direct mode without Authelia in front (render.py validate()).
locals {
  edge          = yamldecode(file("${path.module}/../../edge/hostnames.yaml"))
  tunnel_target = "${local.edge.tunnel_id}.cfargotunnel.com"
  edge_mode = {
    for name, h in local.edge.hosts : name => (
      h.exposure == "private" ? "private" : (
        local.edge.force_tunnel ? "tunnel" : (
          lookup(h, "edge", "default") == "default" ? local.edge.edge_default : h.edge
        )
      )
    )
  }
  edge_records = {
    for name, mode in local.edge_mode : name => {
      type    = mode == "private" ? "A" : "CNAME"
      content = mode == "private" ? local.edge.vip : (mode == "tunnel" ? local.tunnel_target : local.edge.zone)
      proxied = mode == "tunnel"
      ttl     = mode == "tunnel" ? 1 : 60 # 1 = automatic, required while proxied
    }
  }
}

resource "cloudflare_dns_record" "edge" {
  for_each = local.edge_records

  zone_id = var.zone_id
  name    = "${each.key}.${local.edge.zone}"
  type    = each.value.type
  content = each.value.content
  ttl     = each.value.ttl
  proxied = each.value.proxied
  # Free-text note on the record, kept per hostname (the API never returns it unless set): a
  # record adopted with a comment already on it (router, 2026-09-21) would otherwise lose it on
  # the first apply, because a missing attribute means "clear it" for this provider.
  comment = lookup(var.tunnel_record_comments, each.key, null)

  # Create the Access apps (access.tf) BEFORE any record they gate, so an Access-gated host is
  # already gated the moment its name resolves through the tunnel.
  depends_on = [
    cloudflare_zero_trust_access_application.dev_worker,
    cloudflare_zero_trust_access_application.k8s_tools,
    cloudflare_zero_trust_access_application.admin_uis,
    cloudflare_zero_trust_access_application.vault_admin, # /admin gated before vault.chifor.me resolves
    cloudflare_zero_trust_access_application.openbao,
    cloudflare_zero_trust_access_application.dsh,
  ]
}

# The registry and this module must agree on the zone and on the tunnel the CNAMEs target.
check "edge_registry_matches_module" {
  assert {
    condition     = local.edge.zone == "chifor.me" && local.edge.tunnel_id == var.tunnel_id
    error_message = "kubernetes/edge/hostnames.yaml zone/tunnel_id disagree with this module (zone chifor.me, var.tunnel_id)."
  }
}
