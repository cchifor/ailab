# Published with an operator-authorized cloudflared login on 2026-09-26.
# Adopt only when both release flags enable the corresponding resource. The
# workstation retains its existing Access state; no replacement state is needed.
import {
  for_each = var.enable_trueswarm_admin && var.publish_trueswarm_admin ? toset(["0286010c0496bb660172f7b89713142d"]) : toset([])
  to       = cloudflare_dns_record.trueswarm_admin[0]
  id       = "${var.zone_id}/${each.value}"
}
