# Runbook — internet exposure (Cloudflare Tunnel + Tailscale)

Hybrid model (chosen 2026-06-14):
- **Public** (via Cloudflare Tunnel + Cloudflare Access SSO — no open ports, no port-forwarding):
  `chat.chifor.me` → Open WebUI, `grafana.chifor.me` → Grafana, `api.chifor.me` → LiteLLM (OpenAI API).
- **Private** (via Tailscale mesh): the nodes, LXCs, QNAP, Proxmox UIs, and the k8s API — reachable
  from your own Tailscale devices through a subnet router. No public surface.

IaC is scaffolded under `kubernetes/apps/apps/edge/` (cloudflared + Tailscale) and
`kubernetes/apps/apps/ai/litellm-secret.sops.yaml` (API master key). It is **committed but NOT yet
wired into Flux** (`apps/kustomization.yaml` does not list `edge`) so the live cluster is unaffected
until the credentials below exist. Once you complete the steps and hand over the tokens, the operator
(me) creates the SOPS secrets, adds `- edge` to the kustomization, applies, and validates.

---

## What you set up (one-time) and hand over

### A. Cloudflare (domain + tunnel + access)
Prereq: **`chifor.me` is a zone on your Cloudflare account** (its nameservers point to Cloudflare).

1. Install `cloudflared` locally and create a **named tunnel** (this is the IaC-friendly "locally-managed"
   tunnel — routes live in git, not the dashboard):
   ```bash
   cloudflared tunnel login                      # browser auth, pick the chifor.me zone
   cloudflared tunnel create ailab               # -> prints a Tunnel ID (UUID) + writes a credentials JSON
   #   credentials file: ~/.cloudflared/<TUNNEL_ID>.json
   cloudflared tunnel route dns ailab chat.chifor.me
   cloudflared tunnel route dns ailab grafana.chifor.me
   cloudflared tunnel route dns ailab api.chifor.me
   ```
2. **Cloudflare Access** (SSO gate) — in the Zero Trust dashboard, add a self-hosted Access application
   for each hostname (chat/grafana/api.chifor.me) with a policy allowing your email (or Google/GitHub IdP).
   This is what authenticates public visitors before they reach the tunnel.
   - Note: `api.chifor.me` is for programmatic OpenAI calls — gate it with a **service token** (Access)
     *and* it also requires the LiteLLM master key (below). For browser SSO use chat/grafana.

**Hand over:** the **Tunnel ID** (UUID) and the **credentials JSON** file contents
(`~/.cloudflared/<TUNNEL_ID>.json`). The JSON is a secret → it goes into a SOPS-encrypted secret.

### B. Tailscale (private admin mesh)
1. In the Tailscale admin console → Settings → **OAuth clients** → generate a client with scopes
   `devices:write` (+ the `tag:k8s-operator` and `tag:k8s` ACL tags). Define those tags in your Tailscale
   ACL policy if not present.
2. (Optional) Pre-authorize the subnet routes `192.168.0.0/24` and `10.55.0.0/24` in the admin console,
   or approve them after the subnet router connects.

**Hand over:** the OAuth **client ID** and **client secret**.

---

## What the operator does once you hand over the tokens

1. **SOPS secrets** (encrypted to the repo's age recipient, committed):
   - `kubernetes/apps/apps/edge/cloudflared-creds.sops.yaml` — Secret `cloudflared-creds` (ns `edge`),
     key `credentials.json` = the tunnel JSON.
   - `kubernetes/apps/apps/edge/tailscale-oauth.sops.yaml` — Secret `operator-oauth` (ns `tailscale`),
     keys `client_id` / `client_secret`.
   - `kubernetes/apps/apps/ai/litellm-secret.sops.yaml` — already generated (random master key); wire it
     into `litellm` (env `LITELLM_MASTER_KEY`) + Open WebUI (`OPENAI_API_KEY`) so `api.chifor.me` is authed.
2. Fill the **Tunnel ID** into `kubernetes/apps/apps/edge/cloudflared.yaml` (ConfigMap `config.yaml`).
3. Wire `- edge` into `kubernetes/apps/apps/kustomization.yaml`, add the master-key secret to the `ai`
   kustomization, `git push`, `flux reconcile`.
4. **Validate:** `cloudflared` pods Ready + tunnel "HEALTHY" in the CF dashboard; `https://chat.chifor.me`
   loads Open WebUI behind Access; `https://grafana.chifor.me` loads Grafana; `api.chifor.me/v1/models`
   returns the models with the master key; Tailscale `Connector` shows the subnet routes; you can reach
   `192.168.0.2` (Proxmox) and the k8s API over Tailscale.

---

## Notes / caveats
- **`cloudflared` image is on Docker Hub** (`cloudflare/cloudflared`) — heed the Docker Hub rate-limit
  follow-up (`docs/k8s-followups.md` #3); pre-pull or add a mirror if the pull 429s.
- The cloudflared tunnel egresses **outbound only** (no inbound ports) — it dials Cloudflare, so it works
  behind NAT with no router config.
- Routes are in git (`cloudflared.yaml` ingress rules); Access policies live in the Cloudflare dashboard
  (can be codified later via the Cloudflare Terraform provider + a CF API token if desired).
- Tailscale gives private L3 reach to the whole `192.168.0.0/24` (nodes, LXCs, Talos VMs, k8s VIP) and the
  `10.55.0.0/24` storage fabric via one subnet-router `Connector` — no per-service config.
