# Adopt the pre-existing tunnel CNAMEs (created earlier via `cloudflared tunnel route dns`) into Terraform
# state so `apply` manages them in place without recreating. Import id format: <zone_id>/<record_id>.
# Record ids captured from the Cloudflare API on 2026-06-16. These import blocks are inert after the
# first successful apply (safe to keep or remove).
import {
  to = cloudflare_dns_record.edge["home"]
  id = "c967ce7dbbf43b1d7599eb4d213efa57/50c9b34766743136158560103f104794"
}
import {
  to = cloudflare_dns_record.edge["sso"]
  id = "c967ce7dbbf43b1d7599eb4d213efa57/327c79126356c9332f15c3d80f9fee3c"
}
import {
  to = cloudflare_dns_record.edge["status"]
  id = "c967ce7dbbf43b1d7599eb4d213efa57/163bf89a8f88a234a84005e2c1042ba8"
}
import {
  to = cloudflare_dns_record.edge["chat"]
  id = "c967ce7dbbf43b1d7599eb4d213efa57/4495a1628deb8e1ff9fdde05d84dc865"
}
import {
  to = cloudflare_dns_record.edge["grafana"]
  id = "c967ce7dbbf43b1d7599eb4d213efa57/32a863e3193c8f43fb521d20c9f6c25e"
}
import {
  to = cloudflare_dns_record.edge["api"]
  id = "c967ce7dbbf43b1d7599eb4d213efa57/6bad35ac1be61db6cb59c4db720abb37"
}
