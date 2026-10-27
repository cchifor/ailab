# One DNS record per edge host (ADR 0040, docs/standards/edge-ingress.md section 4). The records come
# from kubernetes/edge/dns-records.json, which scripts/edge/render.py GENERATES from the registry
# kubernetes/edge/hostnames.yaml after validating it (`just edge-render`). This module does not
# re-derive modes: render.py's effective_mode() is the one implementation, shared with the proxy's
# Caddyfile, so DNS and the proxy cannot disagree about a host.
#
#   tunnel   proxied CNAME -> <tunnel>.cfargotunnel.com   (Cloudflare's edge, then cloudflared)
#   direct   DNS-only CNAME -> the apex chifor.me, whose A record the home gateway's DDNS keeps on
#            the WAN address (that A record must stay DNS-only). Resolvers see the CNAME and the
#            apex's A record (Cloudflare flattens only the apex by default). The gateway forwards 443
#            to the edge proxy's VIP.
#   private  DNS-only A -> the VIP 192.168.0.51 (resolves anywhere, reachable only on the LAN/VPN)
#
# BLOCKING GUARDS: preconditions, which fail the plan (a `check` block would only warn). The artifact
# guards run in two places: on the output edge_dns_artifact, which every UNtargeted plan evaluates
# however many records there are (an emptied map has no record to carry them), and on every record,
# which is what a `-target` plan keeps (it prunes an output with no resource ancestors).
#   1. the file carries the sha256 of the registry as committed (a registry change without
#      `just edge-render`, which refuses an invalid registry, never reaches Cloudflare);
#   2. it carries a sha256 over that registry AND its records, recomputed here, so a record edited,
#      added or dropped by hand fails even with the registry hash intact;
#   3. its hostnames are exactly the registry's hosts;
#   4. its zone and tunnel are this module's;
#   5. (per record only) every record has exactly one of the three shapes above.
# The hashes are self-reported by the file. They catch a stale render and an edit made by hand; they
# are not a signature. Parity with the renderer itself is enforced by CI's `render.py --check` on every
# push (it regenerates the file and refuses any difference) and by `just edge-dns-plan/-apply`, which
# run the same check before tofu; the runbook applies from merged commits.
#
# A mode change between a CNAME and an A record REPLACES the record: provider 5.24.0 plans the type
# change as delete-then-create (seen 2026-10-27, `replace_paths: [["type"]]`), and the two types cannot
# coexist on one name, so create_before_destroy would collide: a few seconds of NXDOMAIN. Rehearse it on
# edge-canary before any real host moves (docs/runbooks/edge-ingress.md, "Rehearse a mode change").
#
# SECURITY (Access-gated hosts): while such a host is in tunnel mode its record must not resolve
# before its Access application exists, so the records keep the depends_on below; a partial or
# failed apply leaves a NEW record absent (fail-safe). The registry refuses to put an Access-gated
# host in direct mode without Authelia in front (render.py validate()).
locals {
  edge_registry       = replace(file("${path.module}/../../edge/hostnames.yaml"), "\r", "")
  edge_registry_hosts = keys(yamldecode(local.edge_registry).hosts)
  edge_dns            = jsondecode(file("${path.module}/../../edge/dns-records.json"))
  tunnel_target       = "${var.tunnel_id}.cfargotunnel.com"
}

output "edge_dns_artifact" {
  description = "The sha256 binding kubernetes/edge/dns-records.json to the registry it was rendered from."
  value       = local.edge_dns.binding_sha256

  precondition {
    condition     = local.edge_dns.registry_sha256 == sha256(local.edge_registry)
    error_message = "kubernetes/edge/dns-records.json was not rendered from the committed kubernetes/edge/hostnames.yaml: run `just edge-render` (it validates the registry first) and commit both."
  }
  precondition {
    condition     = local.edge_dns.binding_sha256 == sha256("${local.edge_registry}\n${jsonencode(local.edge_dns.records)}")
    error_message = "The records in kubernetes/edge/dns-records.json are not the ones render.py wrote for this registry (edited, added or removed by hand): run `just edge-render`."
  }
  precondition {
    condition = (length(setsubtract(keys(local.edge_dns.records), local.edge_registry_hosts)) == 0
    && length(setsubtract(local.edge_registry_hosts, keys(local.edge_dns.records))) == 0)
    error_message = "kubernetes/edge/dns-records.json does not hold exactly one record per registry host: run `just edge-render`."
  }
  precondition {
    condition     = local.edge_dns.zone == "chifor.me" && local.edge_dns.tunnel_id == var.tunnel_id
    error_message = "kubernetes/edge/dns-records.json names another zone or tunnel than this module (chifor.me, var.tunnel_id)."
  }
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
      condition     = local.edge_dns.binding_sha256 == sha256("${local.edge_registry}\n${jsonencode(local.edge_dns.records)}")
      error_message = "The records in kubernetes/edge/dns-records.json are not the ones render.py wrote for this registry (edited, added or removed by hand): run `just edge-render`."
    }
    precondition {
      condition = (length(setsubtract(keys(local.edge_dns.records), local.edge_registry_hosts)) == 0
      && length(setsubtract(local.edge_registry_hosts, keys(local.edge_dns.records))) == 0)
      error_message = "kubernetes/edge/dns-records.json does not hold exactly one record per registry host: run `just edge-render`."
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
