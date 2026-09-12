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
  description = "Subdomains under chifor.me published by the tunnel; each gets a proxied CNAME -> <tunnel>.cfargotunnel.com. Existing records are adopted via imports.tf; NEW ones (status, dw1-dw6, agentforge, openbao, search) are created. The dw* hosts are the dev-worker ttyd terminals (2 per node) and are gated by access.tf. agentforge is Access-FREE (own Authelia OIDC, ADR 0019). openbao is the secret-store UI and IS Access-gated (access.tf `openbao`) despite having its own token login — see that resource for why. dsh is the DeepSeek Harness agent UI and IS Access-gated (access.tf `dsh`): it executes model-generated tool calls, and its own session cookie carries no SSO identity, so Access is the only per-person gate. search is the SearXNG web UI and is Access-FREE, gated instead by its own oauth2-proxy -> Authelia (kubernetes/apps/apps/dsh/searxng-auth.yaml), the same shape as home.chifor.me."
  type        = list(string)
  default     = ["home", "sso", "status", "chat", "grafana", "api", "dw1", "dw2", "dw3", "dw4", "dw5", "dw6", "k8s", "hubble", "proxmox", "qnap", "prometheus", "alertmanager", "ntfy", "git", "vault", "agentforge", "openbao", "dsh", "search"]
}

variable "registry_ip" {
  description = "LAN IP of the Zot registry LXC (kubernetes/infra/registry-lxc). registry.chifor.me is an A record pointing here, DNS-only (grey-cloud) so Talos nodes resolve it to the LAN IP and pull directly over its LE cert — NOT via the tunnel (Cloudflare can't reach a private IP)."
  type        = string
  default     = "192.168.0.36"
}

variable "enable_api_access_gate" {
  description = "Create the Cloudflare Access service-token gate on api.chifor.me (access.tf). Keep FALSE until the CF-Access-Client-Id/Secret headers are wired into EVERY api.chifor.me caller (the Strive platform + any script) — otherwise they 401 at the edge. Left false so `tofu plan` stays clean; flip to true, `tofu apply`, then read the creds via `tofu output`."
  type        = bool
  default     = false
}

variable "authelia_access_client_secret" {
  description = "PLAINTEXT OIDC client secret for the `cloudflare-access` client in Authelia (kubernetes/apps/apps/auth/authelia-config.yaml carries only its pbkdf2 hash). Set it in terraform.tfvars (gitignored); it is also escrowed at af/estate/cloudflare, field access_oidc_client_secret. EMPTY (the default) leaves the Authelia identity provider uncreated so `tofu plan` stays clean until you opt in — the same pattern as enable_api_access_gate. Access keeps One-time PIN either way."
  type        = string
  sensitive   = true
  default     = ""
  # nullable = false, because the gate is `!= ""` and an explicit `null` would pass it: `null != ""`
  # is true, so a null would select one instance and hand the provider an empty client secret. With a
  # non-null default, `nullable = false` makes an explicit null resolve to "" and disable the IdP,
  # which is the safe reading of "no secret".
  nullable = false
}
