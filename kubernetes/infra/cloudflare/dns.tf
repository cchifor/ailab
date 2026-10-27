# One DNS record per edge host (ADR 0040, docs/standards/edge-ingress.md section 4). The records come
# from kubernetes/edge/dns-records.json, which scripts/edge/render.py GENERATES from the registry
# kubernetes/edge/hostnames.yaml after validating it (`just edge-render`). This module does not
# re-derive modes: render.py's effective_mode() is the one implementation, shared with the proxy's
# Caddyfile, so DNS and the proxy cannot disagree about a host.
#
#   tunnel   proxied CNAME -> <tunnel>.cfargotunnel.com   (Cloudflare's edge, then cloudflared)
#   direct   DNS-only CNAME -> the apex chifor.me, whose A record the home gateway's DDNS keeps on
#            the WAN address (Cloudflare flattens it, so resolvers see A answers). The gateway
#            forwards 443 to the edge proxy's VIP.
#   private  DNS-only A -> the VIP 192.168.0.51 (resolves anywhere, reachable only on the LAN/VPN)
#
# BLOCKING GUARDS: lifecycle preconditions, which fail the plan (a `check` block would only warn).
#   1. the file carries the sha256 of the registry as committed, so a hand edit, or a registry change
#      without `just edge-render` (which refuses an invalid registry), never reaches Cloudflare;
#   2. its zone and tunnel are this module's;
#   3. every record has exactly one of the three shapes above.
#
# A mode change between a CNAME and an A record REPLACES the record (the two types cannot coexist
# on one name, so create_before_destroy would collide): a few seconds of NXDOMAIN. Rehearse it on
# edge-canary before any real host moves (docs/runbooks/edge-ingress.md, "Rehearse a mode change").
#
# SECURITY (Access-gated hosts): while such a host is in tunnel mode its record must not resolve
# before its Access application exists, so the records keep the depends_on below; a partial or
# failed apply leaves a NEW record absent (fail-safe). The registry refuses to put an Access-gated
# host in direct mode without Authelia in front (render.py validate()).
locals {
  edge_registry = replace(file("${path.module}/../../edge/hostnames.yaml"), "\r", "")
  edge_dns      = jsondecode(file("${path.module}/../../edge/dns-records.json"))
  tunnel_target = "${var.tunnel_id}.cfargotunnel.com"
}

resource "cloudflare_dns_record" "edge" {
  for_each = local.edge_dns.records

  zone_id = var.zone_id
  name    = "${each.key}.${local.edge_dns.zone}"
  type    = each.value.type
  content = each.value.content
  ttl     = each.value.ttl
  proxied = each.value.proxied
  # Free-text note on the record, kept per hostname (the API never returns it unless set): a
  # record adopted with a comment already on it (router, 2026-09-21) would otherwise lose it on
  # the first apply, because a missing attribute means "clear it" for this provider.
  comment = lookup(var.tunnel_record_comments, each.key, null)

  lifecycle {
    precondition {
      condition     = local.edge_dns.registry_sha256 == sha256(local.edge_registry)
      error_message = "kubernetes/edge/dns-records.json was not rendered from the committed kubernetes/edge/hostnames.yaml: run `just edge-render` (it validates the registry first) and commit both."
    }
    precondition {
      condition     = local.edge_dns.zone == "chifor.me" && local.edge_dns.tunnel_id == var.tunnel_id
      error_message = "kubernetes/edge/dns-records.json names another zone or tunnel than this module (chifor.me, var.tunnel_id)."
    }
    precondition {
      condition = (
        (each.value.mode == "tunnel" && each.value.type == "CNAME" && each.value.proxied && each.value.content == local.tunnel_target && each.value.ttl == 1)
        || (each.value.mode == "direct" && each.value.type == "CNAME" && !each.value.proxied && each.value.content == local.edge_dns.zone)
        || (each.value.mode == "private" && each.value.type == "A" && !each.value.proxied && each.value.content == local.edge_dns.vip)
      )
      error_message = "kubernetes/edge/dns-records.json: ${each.key} has no valid tunnel, direct or private record shape."
    }
  }

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
