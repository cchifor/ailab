# Runbook — internet exposure (Cloudflare Tunnel + Tailscale)

Hybrid model (chosen 2026-06-14; admin UIs moved public 2026-06-18, PR #24 — see ADR 0007):
- **Public** (via Cloudflare Tunnel + Cloudflare Access — no open ports, no port-forwarding). Access
  policies are codified in `kubernetes/infra/cloudflare/access.tf` (every app default-deny → `allow_email`):
  - App surface: `chat` → Open WebUI, `grafana`, `api` → LiteLLM, `home`/`sso`/`status`, `git` (Gitea,
    own auth), `vault` (Vaultwarden, own auth; `/admin` path-gated), `ntfy` (own token auth).
  - Admin / cluster UIs (Access-gated): `k8s` (Headlamp), `hubble`, `dw1/2/3` (dev shells), and — **new
    in #24** — `proxmox`, `qnap`, `prometheus`, `alertmanager`.
- **Private** (via Tailscale mesh): L3 reach to the nodes, LXCs, Talos VMs, the k8s API VIP, and the
  `10.55.0.0/24` storage fabric — for SSH / kubectl / API access that does not go through the web UIs.

> **Threat model for the WAN admin UIs (#24):** Proxmox + QNAP have their OWN logins, so Cloudflare
> Access is defense-in-depth in front of them. **Prometheus + Alertmanager have NO native auth**, so
> Access is the SOLE gate — an Access compromise can read all metrics and, worse, **silence every alert**.
> Today that gate is single-factor (email OTP); these two are given a short Access session as partial
> mitigation. Accepted for now; the hardening roadmap (IdP-backed MFA; replacing origin `noTLSVerify`
> with the pinned LAN CA) is tracked in **ADR 0007**.

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
1. **Define the operator's tags in the ACL FIRST** (admin console → Access Controls). Without this the
   operator crashes: `requested tags [tag:k8s-operator] are invalid or not permitted (400)`. Add:
   ```jsonc
   "tagOwners": {
     "tag:k8s-operator": [],
     "tag:k8s":          ["tag:k8s-operator"],
   },
   // optional: auto-approve the subnet routes so you don't click-approve each one
   // NOTE: use tag:k8s (the tag the operator gives the subnet-router DEVICE), NOT tag:k8s-operator
   // (that's the operator's own tag). autoApprovers matches the advertising device's tag.
   "autoApprovers": {
     "routes": { "192.168.0.0/24": ["tag:k8s"], "10.55.0.0/24": ["tag:k8s"] },
   },
   ```
2. **OAuth client** (Settings → OAuth clients): create one with the **Devices → Write** scope AND the
   **`tag:k8s-operator`** tag attached (tags are set at creation — if your existing client lacks it,
   make a new one). Hand over the **client ID** + **client secret**.
3. After the operator connects, approve the advertised subnet routes on the `ailab-subnet` machine in the
   admin console (skipped if you added the `autoApprovers` above).

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
- Routes are in git (`cloudflared.yaml` ingress rules); **Access policies are codified** in
  `kubernetes/infra/cloudflare/access.tf` (Cloudflare Terraform provider) — every app default-deny gated
  to `allow_email`, with `dns.tf` depending on the apps so Access enforces before each hostname resolves.
- Tailscale gives private L3 reach to the whole `192.168.0.0/24` (nodes, LXCs, Talos VMs, k8s VIP) and the
  `10.55.0.0/24` storage fabric via one subnet-router `Connector` — no per-service config.
