# ADR 0040 — Self-hosted edge ingress, with the Cloudflare tunnel as a per-host fallback

**Status:** ACCEPTED (2026-10-27), owner-directed: *"A by default but also keep C so we can switch
across them. On A, document it in ailab repository and as a standard / specification in docs, and
automate everything so we can easily install it in a new infrastructure or reinstall it if
needed."* Constraint stated the same day: *"I don't want to pay anything"* (no VPS).

**Supersedes** the "Internet exposure" section of ADR 0007 (Cloudflare Tunnel as the only public
path, with Cloudflare Access gating the admin UIs). ADR 0007's storage and observability sections
stand. **Standard:** `docs/standards/edge-ingress.md`. **Plan with the Codex review trail:**
`plans/2026-10-27-edge-ingress-plan.md`. **Runbook:** `docs/runbooks/edge-ingress.md`.

## Context

Every web hostname was published only through the Cloudflare tunnel, so callers on the LAN and in the
cluster left the house and came back. Measured 2026-10-27, `router.chifor.me/health` took 65-80 ms
from dev-worker-1 through Cloudflare's Bucharest PoP, against about 1 ms to a LAN host. The larger
costs were not latency:

- Cloudflare terminates TLS, so it reads every prompt, every line of code the agents send, and
  the bearer keys.
- Internal LLM access failed with the WAN even for local models.
- The edge's 100 s origin timeout and bot checks applied to internal calls.
- All LAN callers shared one address in per-client limits.

The home connection has a public IPv4 owned by the UniFi gateway (no CGNAT), and the gateway's DDNS
already keeps `chifor.me` on it. Free alternatives were compared:
- A VPS relay (Pangolin, or an SNI relay over WireGuard) was ruled out by the no-cost constraint.
  Oracle's free tier reclaims idle instances.
- Pangolin also decrypts on the relay.
- Tailscale Funnel has no custom domain.

## Decision

1. **One front door for both paths.** An in-cluster Caddy proxy (`edge/edge-proxy`) serves every
   in-scope hostname, on the VIP 192.168.0.51 for the LAN and the gateway's port forward, and on a
   tunnel-only listener for `cloudflared`.
2. **A registry decides each host's mode.** `kubernetes/edge/hostnames.yaml` declares exposure,
   edge mode and authentication per host. The Caddy config, the Cloudflare DNS records and the
   gateway's local DNS are generated or reconciled from it.
3. **Direct is the target default and the tunnel stays.** Hosts start in `tunnel` and move to
   `direct` one at a time after verification. `edge: tunnel` (one host) or `force_tunnel` (all)
   returns them, and Caddy serves every public host on the tunnel listener in every mode.
4. **Admin surfaces become private.** They are reachable from the LAN and VPN only, authorised by
   the TCP peer, and the VPN is the gateway's WireGuard server. Until that VPN works they stay
   tunnel + Access from outside.
5. **Authentication does not depend on the mode.** Hosts without per-person auth get Authelia
   forward-auth, admins only, before any LAN exposure.
6. **Caddy, not Traefik.** The estate's apps trust `CF-Connecting-IP`. Caddy computes the client
   address per listener (`trusted_proxies`, `client_ip_headers`) and overwrites that header for
   every backend; Traefik needs a third-party plugin for the tunnel path.
7. **MetalLB, not Cilium L2 announcements.** Cilium documents its L2 announcements as incompatible
   with `externalTrafficPolicy: Local`: the lease ignores whether the node has a ready endpoint.
   MetalLB elects among nodes with a ready endpoint, which keeps both client addresses and
   failover. Cilium is not changed.
8. **The gateway is code.** `scripts/edge/gateway.py` reconciles the UniFi forward, the local DNS
   records and the DHCP range. It owns only objects whose IDs it has recorded, and its plan and apply
   are bound by a hash. DDNS stays a read-only check.

## Consequences

- **What leaves Cloudflare**: LAN callers (dev-workers, reviewers) reach hosts in about 1-2 ms, and
  Cloudflare no longer sees that traffic. For direct hosts it sees no traffic at all.
- **What the internet reaches**: the control-plane nodes, through the gateway's TCP 443 forward,
  with no upstream volumetric protection or WAF. The bounds are resource limits and header
  timeouts; the remedy is tunnel mode per host or globally. CrowdSec is a separate, optional
  decision.
- **Connection interruptions**: a change of announcing node, a proxy rollout, or a certificate
  reload more than 5 minutes old ends open connections, and clients reconnect.
- **Unverified LAN origins**: Proxmox, QNAP and the dev-worker origins are still not verified by
  certificate. ADR 0007's open item carries over.
- **Offline**: the guarantee covers LAN clients on the gateway resolver using bearer-token APIs;
  in-cluster callers keep public DNS.
- **Recovery**: needs no proxy. LAN/VPN users reach Proxmox, QNAP and the dev-workers on their
  addresses, and a fix reaches Gitea through a `kubectl port-forward`.
- **Inventory fixes this work made** (2026-10-27): two dead WAN forwards removed (443 -> .250 and
  22 -> .68), and the DHCP pool moved to start at .52. It had started at .50, colliding with
  cloud-ci-11's static address. The `Home` network is a /23.
