# registry.chifor.me -> the Zot registry LXC on the LAN (kubernetes/infra/registry-lxc).
# DNS-only (grey-cloud, proxied=false): a private A record so the Talos nodes (same resolver) and
# CI reach the registry DIRECTLY on the LAN over its Let's Encrypt cert. Do NOT proxy — Cloudflare
# can't route to a 192.168.x address, and the registry must stay off the public tunnel.
resource "cloudflare_dns_record" "registry" {
  zone_id = var.zone_id
  name    = "registry.chifor.me"
  type    = "A"
  content = var.registry_ip
  ttl     = 300
  proxied = false
}
