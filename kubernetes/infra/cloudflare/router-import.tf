# Created through the scoped Cloudflare DNS API during the initial router deployment.
# Adopt it into the existing operator-owned local state; do not recreate the zone/tunnel.
import {
  to = cloudflare_dns_record.tunnel["router"]
  id = "c967ce7dbbf43b1d7599eb4d213efa57/c54e2ae4bd38fa150c2f5928967478ae"
}
