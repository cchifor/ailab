# Standard: edge ingress (`*.chifor.me`)

**Status:** adopted 2026-10-27 (ADR 0040). Plan and review trail:
`plans/2026-10-27-edge-ingress-plan.md`. Operations: `docs/runbooks/edge-ingress.md`.

This standard defines how the estate's web hostnames reach their backends, from the internet and
from the LAN. It is written so the edge can be rebuilt on new hardware from this repository alone.
Statements with **MUST** are requirements the tooling enforces or the verification proves; a change
that breaks one is a change to this standard and needs an ADR.

## 1. Scope

In scope: every hostname listed in `kubernetes/edge/hostnames.yaml` (the **registry**), all under
`chifor.me`.

Out of scope, and unchanged by this standard:
- `registry.chifor.me`: a DNS-only A record to the zot LXC.
- The `*-lan` NodePorts (`openbao-lan`, `litellm-lan`, `prometheus-lan`, `loki-lan`).
- `trueswarm-admin.chifor.me`: its own tunnel and Access app.
- `strive.place`: its own Cloudflare account and connector.

## 2. Terms

- **Exposure**: `public` (meant to be reachable from the internet) or `private` (LAN and VPN only).
- **Edge mode** (public hosts only):
  - `direct`: the home gateway forwards TCP 443 to the edge proxy, with no third party in the path;
  - `tunnel`: Cloudflare's edge and the `cloudflared` connector carry the traffic to the same proxy.

  A host's **effective mode** is its own `edge` pin, else the registry's `edge_default`; the global
  `force_tunnel` overrides both.
- **Edge proxy**: the Caddy DaemonSet `edge/edge-proxy`, the single front door for both modes.
- **VIP**: the proxy's LAN address, **192.168.0.51**, announced by MetalLB.
- **Trusted sources**: the LAN and VPN ranges allowed to reach private hosts (§5.3).

## 3. The registry

`kubernetes/edge/hostnames.yaml` is the single source of truth. `scripts/edge/render.py` validates
it and fails CI on any violation. Every entry MUST declare:

| Field | Values | Meaning |
|---|---|---|
| `exposure` | `public`, `private` | who may reach it |
| `edge` | `default`, `direct`, `tunnel` | public only |
| `auth` | `native`, `authelia`, `none` | who authenticates people; `none` only for public content or a fixed `respond` backend |
| `access_gated` | bool | relies on Cloudflare Access while in tunnel mode |
| `backend` | URL (+ TLS and Host options) or a fixed response | where requests go |
| `routes` | ordered path rules | exceptions: path guards, splits, restricted paths |
| `lan_dns` | bool | the gateway serves a local record for it |
| `smoke` | expected anonymous outcome | what the smoke test asserts |

Validation MUST reject:
- unknown keys and duplicate names;
- a missing security field (`exposure`, `auth`);
- a private host in tunnel mode;
- an `access_gated` host whose effective mode is `direct`, unless it is `auth: authelia` or private;
- an `access_gated` host with `auth: none`. In tunnel mode such a host is served to every trusted
  source on the direct listener with no Access in front, so it needs `authelia` or `native` auth
  whatever its mode;
- `auth: none` on a private host, unless its backend is a fixed `respond` (no upstream: the canary,
  which must be able to rehearse every mode).

A parity test MUST hold every backend equal to the routing it replaced (the frozen 2026-10-27
cloudflared ingress), except for differences listed in the test with a reason.

## 4. What each mode means

| | DNS (Cloudflare) | Direct listener (:443) | Tunnel listener |
|---|---|---|---|
| public, `direct` | CNAME -> apex, DNS-only | any source | served |
| public, `tunnel` | CNAME -> tunnel, proxied | LAN tier (admin tier if `access_gated`) | served |
| private | A -> VIP, DNS-only | admin tier only | 404 |
| unknown host | none | 404 | 404 |

The apex `chifor.me` A record follows the WAN address through the gateway's DDNS; direct hosts are
CNAMEs to it, so one record moves them all. Direct hosts MUST NOT acquire an AAAA record unless an
IPv6 ingress is designed and tested.

Hosts with `lan_dns: true` also get a local record on the gateway resolver, so clients that use it
reach the proxy directly in every mode: a CNAME to one local name for the VIP (`edge-vip.lan`, whose
only record is an A to the VIP). A local A record per host is not enough: the resolver still forwards
AAAA queries for the name, and a dual-stack client then takes Cloudflare's IPv6. A CNAME to an A-only
name in the resolver's own local domain answers AAAA with no data.

## 5. Trust model

### 5.1 Client address

The proxy is the only component that decides a request's client address. It MUST:
- **Direct listener**: take the TCP peer. The Service uses `externalTrafficPolicy: Local`, so the
  peer is the real client.
- **Tunnel listener**: take `CF-Connecting-IP`. That listener is reachable only from the `cloudflared`
  pods (NetworkPolicy on namespace **and** pod label), and Cloudflare overwrites the header. The
  request is refused (400) if the header is missing or is not exactly one IP, or if `Cf-Visitor` is
  missing.
- **Toward every backend and forward-auth**: overwrite `CF-Connecting-IP`, `X-Real-IP` and
  `X-Forwarded-For` with that address, and set `X-Forwarded-Proto https` and `X-Forwarded-Host`.

Backends that trust `CF-Connecting-IP` (llm-router, Relay, Vaultwarden) MUST admit only the proxy
and `cloudflared` in their NetworkPolicy.

### 5.2 Header hygiene

- **Before routing, on both listeners**, remove `Forwarded`, `X-Forwarded-*`, `X-Real-IP`,
  `True-Client-IP`, `Cdn-Loop` and `Remote-*`.
- **On the direct listener**, also remove every `Cf-*` header. The Access JWT therefore exists only on
  the tunnel path.
- **On the tunnel listener**, redirect a request whose `Cf-Visitor` scheme is `http` to https before
  authentication or proxying.
- **In access logs**, redact credentials (`Authorization`, `Cookie`, `Cf-Access-Jwt-Assertion`,
  `Cf-Access-Client-*`, `Cf-Access-Authenticated-User-Email`) and replace query strings.

### 5.3 Private access

Source trust is decided by the **TCP peer only**, never by a header. There are two tiers.

- **LAN**: 192.168.0.0/23, 192.168.50.0/24 (Trusted VLAN), 192.168.99.0/24 (Mgmt VLAN) and the VPN
  range, **minus** the gateway 192.168.0.1 (the NAT-loopback source) and the Talos node addresses.
  A tunnel-mode host that is not `access_gated` is served to the LAN on the direct listener; so is
  the HTTP redirect.
- **Admin**: the LAN **minus the lab statics block** 192.168.0.0–.51, plus named exceptions (the
  owner's Windows VM .38). That block is infrastructure and automation: the dev-workers, CI runners,
  reviewers and cloudlab runners run agent- and PR-written code, and must not reach admin surfaces
  even with the right Host. People's devices take DHCP addresses (.52 and up) or sit on the Trusted
  and Mgmt VLANs. **Private hosts, `access_gated` hosts in tunnel mode and restricted paths** are
  served only to this tier.

The registry (`trusted_sources`, `admin_sources`) holds both lists, and the renderer turns each tier
into explicit CIDRs. Every new path into the LAN (a VLAN, a VPN, a loopback) MUST be measured before it
is trusted, and a new lab static MUST land inside `.2`–`.51` (docs/network-plan.md), which keeps it out
of the admin tier by construction.

### 5.4 Authentication

Authentication never depends on the edge mode.

- **`authelia` hosts**: run forward-auth (`/api/authz/forward-auth`) before proxying, and fail closed
  (502) if Authelia is down. Each such domain has an `admins` allow rule followed by a deny rule.
- **`native` hosts**: rely on the application's own login.
- **Recovery surfaces**: Proxmox, QNAP and the dev-worker terminals MUST stay reachable on their LAN
  addresses without the proxy.

## 6. Components

| Component | Contract |
|---|---|
| Edge proxy | Caddy (pinned digest), DaemonSet on the control-plane nodes. Listeners: 8443 TLS, 8081 redirect, 8080 tunnel (ClusterIP only), 8082 health (503 while `/config/drain` exists), 9180 metrics. No HTTP/3, no proxy response timeouts, `stream_close_delay 5m`, `grace_period 90s` |
| Address | MetalLB L2, `loadBalancerClass: metallb.io/metallb`, pool `edge` = the VIP only (`autoAssign: false`), advertised from control-plane nodes only. `externalTrafficPolicy: Local`, because MetalLB elects the announcing node among nodes with a ready endpoint. The Service names the pool and the IP explicitly |
| Certificate | cert-manager wildcard `*.chifor.me` (DNS-01). A sidecar reloads Caddy only for a parseable, matching cert+key pair, and alerts on failure or near expiry |
| Gateway reconciler | `scripts/edge/gateway.py`. Owns only the objects recorded in `kubernetes/edge/gateway-state.yaml`. Plan/apply bound by hash; aborts on partial reads, collisions and drift |
| Cloudflare | DNS records from the registry (`kubernetes/infra/cloudflare`). Access stays only where `access_gated` hosts remain in tunnel mode |
| cloudflared | One catch-all route to the proxy's tunnel listener |

## 7. Gateway contract

Any gateway that fronts this estate MUST provide:

1. A **TCP 443 forward** from the WAN address to the VIP that **preserves the client's source
   address** (no SNAT on inbound WAN traffic).
2. **NAT loopback** for LAN clients that resolve the public address.
3. A **local DNS** resolver able to serve per-host CNAMEs to a local A-only name, answering AAAA for
   them with no data (no upstream IPv6 for a name served locally).
4. **DDNS** that keeps the apex A record on the WAN address.
5. A **DHCP pool that excludes the VIP**.
6. Later (P7): a **WireGuard server** whose clients keep their own source range, so they can be
   trusted.

The UniFi implementation, with its fixed IDs and owned objects, is `kubernetes/edge/gateway.yaml`.
Another vendor needs another reconciler with the same plan/apply guarantees.

## 8. Installing on new infrastructure

Order matters. Each step is idempotent:

1. **Gateway DHCP**: `just edge-gateway-plan --only dhcp`, then `just edge-gateway-apply --only dhcp
   --plan-hash <hash>`. Then confirm the VIP has no lease and answers no ARP.
2. **Flux**: three standalone Kustomizations (`flux get ks` lists each). `metallb` (after
   `infrastructure`) installs MetalLB, and `metallb-config` (after `metallb`) creates the pool and
   advertisement. `edge-proxy` deploys the proxy after `apps` only. That order carries the Authelia
   guarantee because of two preconditions, both of which must hold: `apps` has `wait: true`, so it
   is Ready only once its Deployments have rolled out; and Authelia's configuration is a hash-named
   generated ConfigMap that Authelia reads only at startup, so a rule change renames it and rolls
   every replica. Together they put the admins-only rules on every replica before the proxy can
   forward to those hosts. `edge-proxy` deliberately does not depend on `metallb-config`: a
   Service created before the pool exists stays pending until MetalLB assigns the VIP, and the
   proxy's updates (from P5, the tunnel path's) do not wait on MetalLB's health. Assert the
   Service's assigned address equals the VIP.
3. **Smoke**: `just edge-smoke` passes on the LAN through the VIP.
4. **Gateway, the rest**: local DNS records (hosts with `lan_dns`), then the 443 forward:
   `just edge-gateway-plan`, then `just edge-gateway-apply --plan-hash <hash>`.
5. **Cloudflare DNS**: `tofu apply` in `kubernetes/infra/cloudflare`.
6. **cloudflared**: catch-all to the tunnel listener.

## 9. Verification required before a host goes direct

A host goes direct only when:

- the runtime fixture and unit tests pass for its rules;
- the smoke test passes on the LAN with its registry `smoke` outcome;
- the canary has proven from outside that WAN sources are preserved;
- for `authelia` hosts, an admin is let in and a non-admin is refused.

The runbook records each run.

## 10. Accepted risks

- **WAN exposure**: WAN traffic reaches the control-plane nodes with no upstream volumetric
  protection. The bounds are per-pod limits and header timeouts and sizes; the remedy is tunnel
  mode.
- **Interrupted connections**: a change of announcing node, a pod rollout, or a certificate reload
  more than 5 minutes old ends open connections, and clients reconnect.
- **Unverified LAN origins**: the Proxmox, QNAP and dev-worker origins are not verified by
  certificate (ADR 0007's open item).
- **Offline guarantee**: it covers only LAN clients on the gateway resolver using bearer-token APIs.
- **Concurrent edits**: UniFi has no conditional update, so a concurrent controller edit between
  read and write can be lost. Network-config changes run in a declared window.
