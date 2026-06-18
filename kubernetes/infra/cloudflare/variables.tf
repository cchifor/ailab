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
  description = "Subdomains under chifor.me published by the tunnel; each gets a proxied CNAME -> <tunnel>.cfargotunnel.com. Existing records are adopted via imports.tf; NEW ones (status, dw1/dw2/dw3) are created. The dw* hosts are the dev-worker ttyd terminals and are gated by access.tf."
  type        = list(string)
  default     = ["home", "sso", "status", "chat", "grafana", "api", "dw1", "dw2", "dw3", "k8s", "hubble", "proxmox", "qnap", "prometheus", "alertmanager", "ntfy"]
}

variable "registry_ip" {
  description = "LAN IP of the Zot registry LXC (kubernetes/infra/registry-lxc). registry.chifor.me is an A record pointing here, DNS-only (grey-cloud) so Talos nodes resolve it to the LAN IP and pull directly over its LE cert — NOT via the tunnel (Cloudflare can't reach a private IP)."
  type        = string
  default     = "192.168.0.36"
}
