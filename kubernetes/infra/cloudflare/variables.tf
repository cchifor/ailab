variable "cloudflare_account_id" {
  description = "Cloudflare account id that owns the Zero Trust org + the chifor.me zone."
  type        = string
}

variable "zone_id" {
  description = "Cloudflare zone id for chifor.me."
  type        = string
}

variable "tunnel_id" {
  description = "The locally-managed cloudflared tunnel UUID. Owned by kubernetes/apps/apps/edge/cloudflared.yaml (git+Flux) — NOT created here; used only to build the CNAME targets."
  type        = string
  default     = "f93d9a6a-5172-43d3-8bef-13460ea7607b"
}

variable "tunnel_hostnames" {
  description = "Subdomains under chifor.me published by the tunnel; each gets a proxied CNAME -> <tunnel>.cfargotunnel.com. Existing records are adopted via imports.tf; the only NEW one is whatever isn't created yet (status)."
  type        = list(string)
  default     = ["home", "sso", "status", "chat", "grafana", "api"]
}
