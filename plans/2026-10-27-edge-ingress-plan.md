# Edge ingress: a self-hosted front door, with the Cloudflare tunnel kept as a switchable fallback

## Codex Review

- The registry, separate ingress listeners, explicit private-host denial, and preservation of backend routing are sound foundations.
- Block WAN activation until the phase ordering prevents Access bypass, private path rules are explicit, and every new ingress path sanitizes trusted identity headers.
- A control-plane DaemonSet does not establish safe Cilium L2/Local failover; endpoint failures, the hand upgrade, and certificate reloads need stronger safeguards and measured acceptance criteria.
- DNS changes are not a zero-window rollback mechanism: record-type transitions, cached answers, local overrides, and the shared Caddy dependency need separate recovery procedures.
- Narrow UniFi ownership beyond hostname suffixes, complete the backend and NetworkPolicy inventory, and verify authentication, cold-DNS operation, source attribution, and failure recovery before migration.

## Context

Every `*.chifor.me` hostname is published only through the Cloudflare tunnel (`cloudflared`,
`kubernetes/apps/apps/edge/cloudflared.yaml`), so LAN and in-cluster callers leave the house and
come back. Measured 2026-10-27 from dev-worker-1: `router.chifor.me/health` takes 65-80 ms through
Cloudflare's Bucharest PoP (`cf-ray ...-OTP`) against about 1 ms to a LAN host. Latency is the
smallest cost. Cloudflare terminates TLS, so it sees every prompt, every line of code Codex sends,
and the bearer keys. Internal LLM access dies with the WAN, even for the local-model routes. Edge
limits (100 s origin timeout, bot checks) apply to internal calls, and every LAN caller shares the
home's one public IP in the router's per-address limits.

<!-- codex: The universal hostname claim contradicts kubernetes/infra/cloudflare/registry.tf, which already publishes registry.chifor.me directly to .36, and the private OpenBao path documented in openbao-lan.yaml. Scope this migration to the enumerated tunnel hosts and preserve those existing exceptions. -->

The owner decided (2026-10-27):

- **Mode A, "direct", is the default**: the home gateway forwards TCP 443 to a reverse proxy in
  ailab. No Cloudflare in the data path.
- **Mode C, "tunnel", stays available** and can be selected per hostname (or globally) in git, so
  the estate can fall back to Cloudflare in one commit.
- Admin surfaces become **private**: reachable from the LAN and the VPN only.
- Everything is automated and documented as a standard, so the edge can be installed on new
  infrastructure or rebuilt from the repo alone.
- Free only: no VPS.

### Verified facts this plan relies on (2026-10-27)

- **Gateway**: UniFi UXG (model `UXGA6AA`, WAN `Internet UXG Fiber`, PPPoE) plus a second WAN
  `Internet 2` (DHCP). Managed by UniFi Network **10.6.109** on a Cloud Key Gen2 Plus at
  **192.168.1.152** (UniFi OS 6.0.11, site `default`).
- **No CGNAT**: the public IPv4 answers from the LAN at hop 1 with TTL 64, so it sits on the UXG.
- **DDNS**: the UXG's built-in Cloudflare DDNS keeps the apex `chifor.me` (and `chifor.net`) A record
  on the current WAN IP (interface `wan`).
- **LAN**: network `Home` is **192.168.0.0/23** (gateway .1), not the /24 `docs/network-plan.md`
  states. Other UniFi networks: Lab 192.168.2.0/24, Smarthome .20, Automation .25, Appliances .30,
  Cameras .40, Trusted .50, Guest .60, Mgmt .99 (all /24, VLAN-tagged).
- **Already fixed today by API, with backups**: the dead WAN forwards 443 -> 192.168.0.250 (`k8s`)
  and 22 -> 192.168.0.68 (`git`) are deleted (nothing answered at either; both addresses were in the
  DHCP pool). The `Home` DHCP range now starts at **192.168.0.52** (was .50, which collided with
  cloud-ci-13's static .50), which frees **192.168.0.51** (no online client, no reservation).
  Remaining forwards: WAN 9443 -> .160:443 and WAN 80 -> .160:80 (Nextcloud, alive).
  <!-- codex: The repo disagrees on the affected VM: docs/network-plan.md assigns .50 to cloud-ci-11 and .12 to cloud-ci-13, and records the previous DHCP start as .51. Retain today's measured gateway state, but reconcile these inventory discrepancies explicitly. -->
- **UniFi API**: the key at OpenBao `af/estate/unifi` (field `api_token`, 32 chars, hand-placed
  2026-09-08, no seed: a vault wipe loses it) works on the official integration API
  (`/proxy/network/integration/v1/*`), the classic API (`/proxy/network/api/s/default/rest/
  {portforward,networkconf,dynamicdns}`) and v2 static DNS (`/proxy/network/v2/api/site/default/
  static-dns`, 60 `*.lan` records today).
- **LAN resolvers**: ailab VMs resolve through 1.1.1.1/9.9.9.9 (cloud-init/netplan), not the gateway.
- **Cluster**: Talos; Cilium **1.19.8** hand-installed with Helm (`kubernetes/infra/bootstrap/
  cilium-values.yaml`; the live release carries `upgradeCompatibility: "1.18"`), kube-proxy
  replacement, vxlan, default SNAT LB mode. `l2announcements` and `externalIPs` are off; the
  `CiliumLoadBalancerIPPool` and `CiliumL2AnnouncementPolicy` CRDs exist; no pools. Pod CIDR
  10.244.0.0/16. Control-plane nodes (.41-.43) are untainted; agent nodes carry `dedicated=agent`.
- **cloudflared** routes about 30 hostnames one by one to Services or LAN IPs. Cloudflare Access gates
  dw1-4, k8s, hubble, dsh, proxmox, qnap, prometheus, alertmanager, openbao and `vault/admin`
  (`kubernetes/infra/cloudflare/access.tf`; the `api` gate is off). `trueswarm-admin` has its own
  tunnel and Access app and is out of scope. The `strive.place` rules in cloudflared.yaml are
  vestigial (strive rides `cloudflared-strive` on another Cloudflare account).
- **Apps that trust Cloudflare headers**: llm-router (`cf-connecting-ip` for api-keys and
  server-registration limits; `cf-ray` turns on the 90 s image deadline), Vaultwarden
  (`IP_HEADER=CF-Connecting-IP`), the dev-worker Caddy gate (`Cf-Access-Jwt-Assertion` selects the
  JWT path, otherwise Basic + cookie; WebSocket Origin must be `https://dwN.chifor.me`).
  <!-- codex: This inventory omits Relay's RELAY_CLIENT_ADDRESS_HEADER=cf-connecting-ip and the router's ROUTER_AUTH_CLIENT_ADDRESS_HEADER, which also controls its loopback-only break-glass check. Header provenance is therefore an authentication boundary, not merely a rate-limit concern. -->
- **NetworkPolicies that admit only cloudflared**: agentforge, dsh (+ searxng-auth), headlamp,
  keycloak, llm-router-edge, relay.
- **Authelia 4.39.28** (forward-auth endpoint `/api/authz/forward-auth`), cookie domain `chifor.me`,
  default policy `one_factor`. cert-manager has ClusterIssuer `letsencrypt-cloudflare` (DNS-01).
- **Tailscale** subnet router (a pod) advertises 192.168.0.0/24 and SNATs, so tailnet traffic reaches
  in-cluster VIPs from a pod IP.
- **Images**: the zot docker.io allowlist covers `library/**` (the official `caddy` image); PyYAML is
  present on the CI runners.

## Approach

### 1. One registry, one proxy, three modes

**Registry** `kubernetes/edge/hostnames.yaml` is the single source of truth. Each entry names a host
under `chifor.me` and declares:

- `exposure`: `public` (internet-reachable) or `private` (LAN and VPN only);
- `edge` (public only): `default` (follow the global `edge_default`), `direct` or `tunnel`;
- `access_gated: true` when the host relies on Cloudflare Access while it is in tunnel mode;
- `backend`: an in-cluster Service URL or a LAN URL, with optional TLS settings
  (`insecure_skip_verify`, `ca_file`, `server_name`) and `host_header`;
- `auth`: `none` or `authelia` (forward-auth, optional group);
- `routes`: optional path-specific rules (Keycloak's path guard, Trueswarm media, Vaultwarden
  `/admin` as private).

<!-- codex: Define a validated schema and an explicit initial entry for every migrated host, including its authentication requirement and path-rule precedence. Reject duplicate or unknown hosts, missing security fields, and Access-dependent direct exposure without a replacement gate rather than allowing permissive defaults. -->

Global settings: `edge_default` (`direct`), the VIP (`192.168.0.51`), the trusted source ranges for
private hosts, the tunnel id.

Everything else is generated or reconciled from it:

| Consumer | How |
|---|---|
| Caddy config | `scripts/edge/render.py` writes `kubernetes/apps/apps/edge/proxy/Caddyfile` (committed; CI fails on drift) |
| Cloudflare DNS | `kubernetes/infra/cloudflare` reads the YAML with `yamldecode` |
| UniFi local DNS + port forward | `scripts/edge/gateway.py` (`just edge-gateway-plan` / `-apply`) |
| cloudflared | one catch-all rule to the proxy; no per-host list any more |

**DNS per mode** (Cloudflare, `chifor.me` zone):

| Host | Record |
|---|---|
| public + direct | CNAME `<name>` -> `chifor.me`, DNS-only, TTL 60 (the apex follows the WAN IP via the UXG's DDNS) |
| public + tunnel | CNAME `<name>` -> `<tunnel>.cfargotunnel.com`, proxied (today's record) |
| private | A `<name>` -> 192.168.0.51, DNS-only (resolves anywhere, reachable only on the LAN/VPN) |

<!-- codex: Keep a stable Terraform resource identity per hostname and test CNAME-to-A and A-to-CNAME changes with the actual provider version; moved blocks only change state addresses. If replacement is required, CNAME and A cannot coexist at the same name, so create-before-destroy is unsafe and the deletion/creation gap needs an explicit recovery procedure. -->
<!-- codex: Verify the apex's effective A and AAAA answers, proxy status, and DDNS propagation: a CNAME inherits the target's addressing, and its 60-second TTL does not shorten the apex record's TTL. Either define and test IPv6 ingress/firewall behavior or ensure direct names do not acquire an unintended IPv6 path. -->

The UXG also gets a local A record for every registry host -> 192.168.0.51, so any client that uses
the gateway resolver goes straight to the proxy whatever the public mode. Clients on public resolvers
reach direct hosts through the UXG's NAT loopback (verified in phase 3; contingency below).

<!-- codex: Successful hairpin access still collapses LAN callers to the gateway address under the stated SNAT behavior, preserving the shared rate-limit problem. Public resolvers also remain a WAN dependency on cache misses, so local DNS for VMs and cluster callers is required to achieve the stated offline and client-attribution goals, not merely a fallback for failed loopback. -->

### 2. The proxy: Caddy, as a DaemonSet on the control-plane nodes

**Caddy rather than Traefik** (a change from the chat design, for one reason): today's apps trust
`CF-Connecting-IP`. Caddy has per-listener `trusted_proxies` and `client_ip_headers`, so it can
compute the verified client IP on both paths and set `CF-Connecting-IP`, `X-Real-IP` and
`X-Forwarded-For` to it, which keeps llm-router and Vaultwarden working unchanged in both modes.
Traefik cannot copy a header value without a third-party plugin. The repo already runs Caddy
(llm-router-edge, the dev-worker gates). Cost: CrowdSec needs a custom Caddy build (phase 8).

<!-- codex: Caddy is plausible, but this comparison assumes retaining the Cloudflare-specific application header is mandatory; compare that implementation with configuring downstream apps to consume a canonical client-IP header. Demonstrate the chosen Caddy version's listener-scoped trust and header behavior in a working fixture before making this the decisive architectural argument. -->

`edge-proxy` (namespace `edge`), official `caddy` image pinned by digest, DaemonSet on the three
control-plane nodes, non-root, read-only root filesystem.

<!-- codex: Reuse the runtime requirements documented in llm-router/edge.yaml: writable /data and /config, explicit UID/GID, and the pinned image's NET_BIND_SERVICE file-capability constraint. Its existing comment records that dropping every capability prevents that Caddy binary from executing, even on an unprivileged port. -->

Listeners:

- **:8443 "direct"** (TLS, wildcard cert), exposed as 192.168.0.51:443. It removes any
  client-supplied `CF-*`, `Cdn-Loop`, `True-Client-IP`, `X-Real-IP` and `X-Forwarded-*` headers, and
  sets `CF-Connecting-IP`, `X-Real-IP` and `X-Forwarded-For` to the TCP peer. The peer is the real
  client, because the Service uses `externalTrafficPolicy: Local`.
  <!-- codex: Local can preserve the source presented to Kubernetes; it cannot undo UXG hairpin/WAN SNAT or Tailscale SNAT. Verify the peer and downstream header independently on each path, and never derive private authorization from a forwarded header. -->
- **:8081** (HTTP), exposed as 192.168.0.51:80. LAN-only redirect to https. WAN 80 stays Nextcloud's.
  <!-- codex: Specify the source restriction and external redirect port explicitly, and disable or constrain Caddy's automatic HTTP redirects and HTTP/3 advertisement to match the TCP-only exposure. Otherwise implicit listeners or redirects to :8443 can diverge from this listener contract. -->
- **:8080 "tunnel"** (plain HTTP), ClusterIP only, and a NetworkPolicy admits only the cloudflared
  pods. `trusted_proxies 10.244.0.0/16` plus `client_ip_headers CF-Connecting-IP`, so the client IP
  comes from Cloudflare's header. The same three headers are then set to it, and `cf-ray` and the
  Access JWT pass through.
  <!-- codex: The entire pod CIDR is trusted here, so the port-specific NetworkPolicy is the actual security boundary: require namespace AND pod selectors, and prove unrelated edge pods, especially cloudflared-strive, cannot connect. Define fail-closed handling for missing, malformed, or multiple CF-Connecting-IP values instead of silently treating a proxy address as a verified client. -->
  <!-- codex: Define canonical X-Forwarded-Proto and X-Forwarded-Host on both paths, including forward-auth requests; the tunnel's immediate transport is HTTP while the public origin is HTTPS. Strip untrusted Forwarded and identity headers such as Remote-* before routing, and test the actual upstream headers rather than only rendered configuration. -->
- **:8082** `/healthz` for probes; **:9180** `/metrics`; the admin API on localhost:2019 only.

Routing rules (the security core, each one a test case):

1. **Public host on :8443**: served to any source, except `access_gated` hosts while in tunnel
   mode, which are LAN/VPN-only on :8443 (no direct bypass of Access).
   <!-- codex: Blocker: switching an Access-dependent host to direct removes its only per-person gate on dsh, Prometheus, Alertmanager, Hubble, and Headlamp until P7; DNS mode must not determine whether authentication is required. The LAN exception also bypasses today's Access gate before Authelia is installed, so replacement authentication must precede enabling this route. -->
2. **Public host on :8080**: served (in any mode, so a DNS flip in either direction has no window).
3. **Private host on :8443**: served only when the TCP peer is in the trusted ranges:
   192.168.0.0/23 **minus 192.168.0.1**, plus Trusted 192.168.50.0/24, Mgmt 192.168.99.0/24 and
   the VPN 192.168.70.0/24. The gateway is excluded because it is the source address of
   NAT-loopback traffic, so a bug that SNATed WAN traffic could never open a private host. Private
   hosts never need loopback: their A record points at the VIP. Anything else gets 403.
   <!-- codex: Excluding .1 protects against that specific translated source, not every possible gateway/node SNAT address; the claim that WAN SNAT could never open a private host is too strong. Test routed VLAN, pod-to-VIP, and VPN paths and constrain gateway forwarding so untrusted traffic cannot emerge as another trusted LAN address. -->
   <!-- codex: The stated Tailscale path arrives from a pod IP and therefore fails this allowlist, while WireGuard does not exist until P7. Provide a narrowly authenticated VPN path before privatization without admitting the whole pod CIDR, and verify WireGuard preserves 192.168.70.0/24 rather than translating it to the excluded gateway. -->
4. **Private host on :8080**: 404, never served through the tunnel.
   <!-- codex: Extend these rules explicitly to private paths on public hosts: vault/admin must be source-restricted on :8443 and denied on :8080 before the public vault route matches. Cover /admin, descendants, encoded and normalized variants, and all methods so the generic public-host rule cannot expose today's Access-protected surface. -->
5. **Unknown host**: 404 on every listener.
6. `auth: authelia`: `forward_auth` to `authelia.auth.svc.cluster.local:9091`, URI
   `/api/authz/forward-auth`, copying the `Remote-*` headers.
   <!-- codex: Specify explicit copied identity headers, fail-closed behavior, and handler ordering so denial/authentication always runs before reverse_proxy; the dev-worker template already documents Caddy's directive-reordering trap. Authelia needs an admins allow rule followed by a matching deny rule, otherwise non-admin users can fall through to the existing one_factor default. -->

Backends keep today's semantics:

<!-- codex: Add a machine-checked parity inventory for all 28 chifor.me hosts in cloudflared.yaml, including hostname, ordered paths, destination, Host/SNI, TLS verification, and auth. In particular, home and search must retain their oauth2-proxy backends, router must retain llm-router-edge, and dsh must retain its public Host rather than being sent directly to an ungated or incompatible backend. -->

- **LAN TLS backends** (proxmox .2:8006, qnap .1.225, dw1-4 .8-.11) skip verification, as
  cloudflared's `noTLSVerify` does today. dw hosts keep `Host: dwN.chifor.me` and SNI `dwN.chifor.me`
  (the dev-worker site already answers that name, and its Origin check expects it).
  <!-- codex: The dw Host/SNI behavior is an intentional change from cloudflared.yaml's IP-valued httpHostHeader, although the template supports the public names. Test JWT validation, Basic/cookie fallback, WebSocket upgrades, and file uploads; retain ADR 0007's unresolved origin-CA pinning risk because skip verification still exposes credentials to LAN interception. -->
- **OpenBao** verifies with the `openbao-tls` CA already issued into `edge`, using server name
  `openbao.openbao.svc.cluster.local`.
  <!-- codex: Preserve the actual upstream openbao-ui.openbao.svc.cluster.local:8200, distinct from the verification name, and mount ca.crt from the edge Secret. cloudflared.yaml deliberately selects the UI Service to remain reachable while OpenBao is sealed, which needs its own acceptance test. -->
- **Keycloak** (`id`) keeps its path guard: it refuses `..`, `;` and their encoded forms in the raw
  URI, allows `/realms/relay`, `/realms/llm-router` and `/resources/`, and returns 404 for anything
  else.
  <!-- codex: Preserve the realm boundary (/ or end-of-path), not a loose prefix such as /realms/relay*, and specify the exact raw/decoded URI representation used by the matcher. Exercise encoded separators, mixed-case escapes, double encoding, repeated slashes, and dot-segment normalization against a real backend because Caddy's matching semantics differ from cloudflared's. -->
- **Trueswarm** keeps its media path split.
  <!-- codex: Record the exact existing anchored split, ^/(media/[^/]+|art/[^/]+|api/v1/uploads/[^/]+/content)$, to trueswarm-muse, with every other path going to trueswarm. A broad media prefix would change the isolation boundary and miss the ticket-bound upload route. -->

Streams have no proxy timeouts (SSE, WebSockets, git pushes); `grace_period 30s` on shutdown.

<!-- codex: Distinguish upstream streaming timeouts from bounded client header reads, body limits, and connection/resource budgets before exposing the control-plane nodes to WAN traffic. A 30-second shutdown grace also interrupts longer operations and is shorter than llm-router-edge's existing 90-second grace; define acceptable interruption and Kubernetes termination timing. -->

**Address**: Service `edge-proxy` of type LoadBalancer, IP from `CiliumLoadBalancerIPPool` `edge`
(192.168.0.51/32, selected by the Service's label), `externalTrafficPolicy: Local`,
`CiliumL2AnnouncementPolicy` restricted to the control-plane nodes. Every announcing node runs a
proxy pod, which is what makes Local safe here. Cilium's documented limitation is announcing on
nodes without a local endpoint. A pod restart on the announcing node drops new connections for a few
seconds (DaemonSet `maxUnavailable: 1`). Accepted and documented.

<!-- codex: Blocker: L2 lease election is not driven by local endpoint readiness, so placing a DaemonSet on eligible nodes does not remove the L2/Local incompatibility or bound downtime to seconds. A failed rollout or unhealthy Caddy can leave the healthy Cilium lease holder announcing a node with no usable endpoint indefinitely; prove exact-version behavior or choose an endpoint-aware announcement design. -->
<!-- codex: Specify serviceSelector, loadBalancerIPs, the LAN interface matching Cilium's selected devices, lease timing, and the expected failover budget. Test pod failure separately from Cilium/node failure, including UXG ARP convergence and existing connections; a VIP has one announcing node at a time, not three active ingress capacity shares. -->

**Cilium values** (hand `helm upgrade`, the path the repo already documents, same chart version,
`--set upgradeCompatibility=1.18` exactly as live): `l2announcements.enabled: true`,
`externalIPs.enabled: true`, `k8sClientRateLimit: {qps: 10, burst: 20}`. The render of the new
release is diffed against `helm get manifest` before applying, and must differ only in those keys.
Then roll the cilium DaemonSet (one node at a time) and the operator.

<!-- codex: The repo's cilium-values.yaml and talos-upgrade.md prescribe upgradeCompatibility=1.16, explicitly documenting why live 1.18 differs; preserving live 1.18 here is a deliberate exception, not the documented command. Use a quoted string value, pin 1.19.8, archive the live values/manifest/revision, and define direct LAN/Talos recovery and a tested Helm rollback before changing the estate-wide datapath. -->
<!-- codex: A LoadBalancer VIP does not require enabling spec.externalIPs support; leave that cluster-wide capability off unless a concrete requirement justifies it. The repo already treats externalIPs as an address-hijacking risk in Workbricks admission guards. -->
<!-- codex: Enabling L2 announcements changes rendered RBAC and controller configuration, so a manifest diff cannot literally differ only in three values keys. Enumerate expected rendered changes, size API rate limits for leases, and verify the Helm rollout strategy itself enforces sequential agent updates rather than scheduling an uncontrolled rollout followed by a second restart. -->

**Certificate**: cert-manager `Certificate` `edge-wildcard` (`*.chifor.me`), issuer
`letsencrypt-cloudflare`. Caddy does not watch certificate files, so a sidecar (same image) hashes
the mounted cert every 5 minutes and runs `caddy reload --force` against the localhost admin API on
change.

<!-- codex: Specify directory mounts without subPath, cert-and-key consistency, permissions, the shared Caddyfile/adapter, and retry behavior that advances the remembered hash only after a successful reload. Force renewal and verify the certificate actually served by every replica, including malformed updates and sidecar failure, with reload-failure and expiry alerts. -->
<!-- codex: Test active streams during forced reloads: Caddy can close WebSockets when their configuration unloads, so certificate rotation can cause synchronized disconnects even without a pod restart. Define stream-close/drain behavior rather than assuming the shutdown grace covers configuration reloads. -->

**Config rollout**: the Caddyfile is a `configMapGenerator` output **with the namespace set**
(the estate's Flux-stall trap). The hash suffix rolls the DaemonSet on every config change.

**Observability**: JSON access logs to stdout (Loki already collects pod logs), a PodMonitor on
:9180, and an alert `EdgeProxyDown`. A smoke script, `scripts/edge/smoke.py`, checks every registry
host through the VIP (`--resolve`) and expects the right status per exposure and source.

<!-- codex: A healthy process or metrics endpoint does not prove VIP reachability, correct authorization, or tunnel availability; add independent direct/tunnel probes and alerts for L2 lease trouble, sustained upstream errors, and certificate reload failure. Verify the PodMonitor selector and metrics-port policy, and redact sensitive query parameters or headers from the new central access logs. -->

### 3. The gateway reconciler

`scripts/edge/gateway.py` (stdlib only) plus `kubernetes/edge/gateway.yaml` (the UniFi-specific half
of the standard): controller URL, site, WAN interface, VIP. `plan` prints a diff; `apply` makes only
the declared changes and re-reads to verify. It owns exactly:

<!-- codex: Python's standard library cannot parse general YAML, so define a constrained JSON-compatible input or declare the PyYAML dependency on operator machines as well as CI. Specify TLS verification, endpoint/version handling, pagination, and schema validation for the three different UniFi APIs before permitting mutations. -->

1. **The port forward** `ailab-edge-https`: WAN TCP 443 -> 192.168.0.51:443. Matched by name; other
   forwards (Nextcloud) are never touched.
   <!-- codex: A display name is not sufficient ownership: require an adopted object ID/marker and fail on duplicate names, an unexpected WAN/site, or any existing conflicting 443 forward. Verify the associated firewall and hairpin behavior, preserve unrelated fields, and test that both Nextcloud forwards remain unchanged. -->
2. **Static DNS records** whose key ends in `.chifor.me`: one A record per registry host ->
   192.168.0.51. Stale `.chifor.me` keys are deleted; every other key (the 60 `*.lan` records) is
   ignored.
   <!-- codex: Blocker: owning every .chifor.me suffix contradicts the stated scope and can delete unrelated local records for registry.chifor.me, trueswarm-admin.chifor.me, or other systems. Delete only explicitly adopted records identified by durable ownership metadata/IDs, reject collisions, and abort destructive reconciliation on an empty, invalid, or incomplete registry. -->
3. **The `Home` DHCP range start** (`192.168.0.52`), read-modify-write of only that field.
   <!-- codex: A full networkconf read-modify-write can overwrite concurrent changes even if the desired diff contains one field. Require a stable network ID, preconditions on subnet and old value, concurrency detection or conditional updates, and a backup/restore procedure that leaves every unrelated field intact. -->
4. **The DDNS entries** for `chifor.me` and `chifor.net`: asserted present. If one is missing, it is
   created with the Cloudflare token from OpenBao `af/estate/cloudflare`.
   <!-- codex: The repo's Cloudflare provider documents a token scoped to chifor.me; authorization to update chifor.net is not established here. Keep the existing entries read-only unless their exact provider payload, token scope, WAN binding, and ownership have been verified, and redact credentials from diffs and backups. -->
5. **The WireGuard server** (phase 7).
   <!-- codex: Successful access to the listed APIs does not establish a supported WireGuard provisioning API or its ownership model. Define the server/peer IDs, key handling, allowed routes, UDP firewall rule, DNS, revocation, and source-preservation tests as a separate deliverable before making private access depend on it. -->

The API key comes from `UNIFI_API_KEY`, falling back to the operator ceremony documented in
`docs/runbooks/openbao-estate-credentials.md` (breakglass token inline, never printed). The UniFi key
is added to the estate seeds so a vault wipe no longer loses it.

<!-- codex: The cited runbook requires updating both estate-seeds.sops.yaml and estate-provision-job.yaml's completeness matrix when adding a path, and neither appears in Critical files. Include an offline SOPS-based credential recovery path: gateway repair must remain possible when OpenBao or the cluster is unavailable. -->

Why not the community UniFi OpenTofu provider: adopting the `Home` network would make tofu own every
attribute of the LAN that carries the whole house, for one field. The reconciler's blast radius is
the fields listed above.

<!-- codex: Reading back a failed multi-step apply is not rollback; define operation ordering, checkpoints, idempotent retries, and restoration after failure between DNS, DHCP, and forwarding changes. Bind apply to the reviewed plan/current-state preconditions so a stale plan cannot silently authorize newly discovered deletions. -->

### 4. App-side changes

- **NetworkPolicies** that admit cloudflared also admit `edge-proxy` pods (cloudflared stays allowed
  until the cleanup phase).
  <!-- codex: Ingress additions are insufficient: relay/network-policy.yaml allows only public TCP 443, so the proposed HTTP router call remains blocked without an egress rule to llm-router-edge's target port 8080. Also audit OIDC egress for Relay, llm-router, and searxng-auth: their public-443 rules exclude private ranges, so split DNS for id/sso can break authentication unless narrowly scoped proxy egress is added. -->
- **relay** calls the router in-cluster (`http://llm-router-edge.llm-router.svc.cluster.local/v1`)
  instead of `https://router.chifor.me/v1`, and llm-router-edge's policy admits relay.
  <!-- codex: Blocker: llm-router-edge currently forwards CF-Connecting-IP unchanged, so admitting Relay gives it a path to forge the router's trusted address, including 127.0.0.1 for the break-glass source check. Give internal callers a separate sanitizing listener/path or route them through the new proxy, and preserve trusted forwarded values only for explicitly authorized edge sources. -->
- **Authelia** rules for private hosts without native per-person auth (prometheus, alertmanager,
  hubble, k8s, dsh): `admins` group only, deny everyone else. proxmox, qnap, openbao and the dw
  terminals keep their own logins and are not put behind Authelia (they are the recovery path when
  the cluster is broken).
  <!-- codex: Native login removes the Authelia dependency but not the dependency on the in-cluster Caddy front door. Document and test VPN-to-origin LAN addresses/names for Proxmox, QNAP, and dev-workers while Caddy and the cluster are down; OpenBao itself cannot be a cluster-independent recovery surface. -->
- **No change** needed for llm-router or Vaultwarden: the proxy sets `CF-Connecting-IP`.
  <!-- codex: Setting this header is safe only if other callers cannot supply it directly; Vaultwarden's manifests contain no ingress NetworkPolicy establishing that boundary. Close or explicitly account for alternate origin paths, and update the router/Relay trust documentation and tests to reflect the new proxy chain. -->

### 5. Phases (each one a PR, each reversible)

| Phase | Change | Public effect |
|---|---|---|
| P1 | This plan, the standard (`docs/standards/edge-ingress.md`), ADR 0040 (replaces ADR 0007's exposure section), runbook `docs/runbooks/edge-ingress.md`, network-plan corrections (/23, DHCP .52, VIP .51, forwards) | none |
| P2 | Registry, renderer, tests, CI drift check, `edge-proxy` manifests, certificate, Cilium pool + L2 policy, Cilium values. Then the hand Cilium upgrade and a LAN check of .51 | none (LAN-only VIP) |
| P3 | Gateway reconciler, `just` recipes, estate seed for the UniFi key; apply: port forward 443 -> .51 and the local DNS records. Verify NAT loopback and the outside view | 443 open: serves public hosts only; private and Access-gated hosts get 403 from WAN |
| P4 | Cloudflare module reads the registry; `moved` blocks; every host still mapped to today's record | `tofu plan` must show moves only |
| P5 | cloudflared becomes a catch-all to edge-proxy :8080; NetworkPolicies admit edge-proxy; relay goes in-cluster | tunnel traffic now flows through Caddy |
| P6 | Canary `status` -> direct, then `router`, then `edge_default: direct` for the rest | DNS for public hosts points home |
| P7 | WireGuard server on the UXG (reconciler + SOPS-held peers), Authelia admin rules, admin hosts -> private | admin UIs leave the internet |
| P8 | CrowdSec: custom Caddy build with the bouncer, LAPI in-cluster | WAF-like protection |
| P9 | Cleanup: Access apps and DNS for private hosts, cloudflared entries in app NetworkPolicies, vestigial strive rules | none |

<!-- codex: P2 is not guaranteed to have no public effect because the Cilium change affects today's tunnel and every existing workload; isolate it behind health gates and its own rollback. On a rebuild, DHCP exclusion/reservation must also precede VIP advertisement rather than relying on today's manual fix occurring before P2. -->
<!-- codex: P2/P3 require proxy-to-backend NetworkPolicy allowances that are deferred to P5, and P3 local DNS redirects clients immediately, so several hosts will break before the tunnel migration. Install and verify additive policies first, then migrate local DNS only for validated hosts. -->
<!-- codex: Blocker: pin all existing hosts to tunnel initially, install admin gates/private restrictions before WAN forwarding, and move P7's security prerequisites ahead of P6. Opening 443 already makes every enabled direct route reachable with chosen SNI/Host, so changing status DNS alone is not a security canary and P3's claimed Access denial is incompatible with an initial direct default. -->
<!-- codex: P4's moves-only requirement must preserve existing TTLs, proxy flags, comments, imports, outputs, and Access dependencies using the existing operator-owned local state; a fresh worktree lacks that gitignored state. Reconcile P5's additive policies and proxy readiness before rolling cloudflared, including its explicit config-revision bump, and retain the old routing config for recovery. -->
<!-- codex: P8 adds a custom image supply chain and another runtime dependency; separate it from the minimum ingress migration and define bouncer failure behavior before adoption. CrowdSec does not replace Cloudflare's upstream volumetric protection, so explicitly accept the WAN/control-plane exposure and establish resource limits before P3. -->
<!-- codex: P9 must preserve the private A records promised by the DNS table, and remove Access gates only after every old connector/configuration can no longer serve the protected origin. Cleaning up cloudflared's backend permissions also removes the old direct-to-backend rollback path, so this phase needs an explicit rollback-retention decision. -->

**Rollback**: any public host returns to the tunnel by setting `edge: tunnel` (or
`edge_default: tunnel`) and running `tofu apply`. Caddy keeps serving every public host on :8080, so
there is no window. The forward can be removed with `just edge-gateway-apply` after setting
`port_forward.enabled: false`.

<!-- codex: DNS rollback is neither instantaneous nor window-free: cached direct answers and existing connections remain, and restricting an Access-gated direct listener before those caches expire deliberately breaks old connections. Sequence the Caddy/Flux policy and DNS changes, account for actual TTLs, and keep the forward until the chosen drain period ends unless this is an emergency security shutdown. -->
<!-- codex: edge_default=tunnel does not override hosts explicitly pinned direct, and gateway local A records continue sending LAN clients to Caddy regardless of public mode. Define a rollback operation that inventories both exceptions and local overrides rather than promising a global switch from one setting. -->
<!-- codex: After P5 the tunnel and direct paths share Caddy, its configuration, certificates/mounts, and cluster networking, so switching DNS cannot recover a proxy or Cilium failure. Retain and rehearse a separate rollback to cloudflared's original backend routes and required NetworkPolicies using an operator path independent of the affected front door. -->

**WAN failover**: the apex DDNS follows the `wan` interface only. If the UXG fails over to
`Internet 2`, direct hosts are unreachable from outside until the primary returns. The documented
response is `edge_default: tunnel`. Accepted.

<!-- codex: Rehearse this response with the primary WAN actually unavailable: connectors must reconnect over Internet 2, and the operator must still reach the repo, credentials, Cloudflare API, and cluster. Specify detection and recovery-time expectations including DNS caching and explicitly pinned direct hosts. -->

**NAT-loopback contingency**: if the UXG does not loop back, LAN clients on public resolvers cannot
reach direct hosts by name. Then the ailab VMs switch their resolver to the gateway (netplan via
ansible), which serves the local records. This is decided in P3 by measurement, not assumed.

<!-- codex: Include Talos/CoreDNS and persistent provisioning values, not only existing VM netplan: kubernetes/infra/variables.tf and agent-nodes/variables.tf also default to public resolvers. Verify resolver/rebind protection accepts private answers, and that cold-cache local inference and required identity flows work with the WAN disconnected. -->

## Critical files

- `kubernetes/edge/hostnames.yaml`, `kubernetes/edge/gateway.yaml`: new, the registry and the
  gateway contract.
- `scripts/edge/render.py`, `scripts/edge/gateway.py`, `scripts/edge/smoke.py`,
  `scripts/tests/test_edge_render.py`, `scripts/tests/test_edge_gateway.py`: new.
- `kubernetes/apps/apps/edge/proxy/` (DaemonSet, Services, ConfigMap generator, Certificate,
  NetworkPolicy, Cilium pool + L2 policy, PodMonitor, alert), wired into `edge/kustomization.yaml`.
- `kubernetes/infra/bootstrap/cilium-values.yaml`: three keys.
- `kubernetes/infra/cloudflare/dns.tf`, `variables.tf`: registry-driven records, `moved` blocks.
- `kubernetes/apps/apps/edge/cloudflared.yaml`: catch-all ingress.
- NetworkPolicies in agentforge, dsh, headlamp, keycloak, llm-router (edge), relay; `relay.yaml`.
- `kubernetes/apps/apps/auth/configuration.yml`: admin rules.
- `justfile`: `edge-render`, `edge-gateway-plan`, `edge-gateway-apply`, `edge-smoke`.
- `.gitea/workflows/manifests.yaml`: render `--check` and the unit tests.
- Docs: `docs/standards/edge-ingress.md`, `docs/decisions/0040-*.md`, `docs/runbooks/edge-ingress.md`,
  `docs/network-plan.md`, `docs/runbooks/internet-exposure.md` (pointer), CLAUDE.md inventory row.

## Verification

- **Unit tests**:
  - Renderer: every routing rule above, header stripping, private allowlist with the gateway
    excluded, encoded traversal guard for `id`, unknown host 404, deterministic output.
  - Reconciler: plan/apply against recorded API fixtures (never touches unowned objects; DHCP
    read-modify-write keeps all other fields).
- **CI**: `render.py --check`, `caddy validate` of the generated file in the pinned image, the
  existing kustomize + kubeconform gate.
  <!-- codex: Render assertions and caddy validate do not establish runtime authorization or header provenance; run the pinned Caddy against echo/auth fixtures through both listeners. Include spoofed and duplicate client-IP/identity headers, Access JWT handling, private paths, unknown and mismatched SNI/Host, auth-service failure, and negative NetworkPolicy probes. -->
- **P2 live**:
  - .51 answers on the LAN with the wildcard cert.
  - `smoke.py` from a dev-worker: public hosts 2xx/3xx; private hosts 200 from the LAN, 403 from a
    pod (a non-trusted source).
    <!-- codex: Generic status classes are inadequate: a redirect can conceal a broken login loop, a public API may correctly return 401, and a private host should still demand its configured login on the LAN. Define per-host routes, expected authentication outcomes, and assertions for authorized users, non-admin users, anonymous users, each VLAN, Tailscale, WireGuard, and an ordinary pod. -->
  - Caddy logs show the real client IP.
  <!-- codex: Add announcing-node Caddy failure, failed-image rollout, readiness loss, node shutdown, Cilium restart, and API/lease disruption tests with bounded recovery targets and backend-observed client IPs. Also test forced certificate renewal and config reload during SSE, WebSockets, and a long upload before public cutover. -->
- **P3 live**:
  - NAT loopback: from a dev-worker,
    `curl --resolve router.chifor.me:443:<public IP> https://router.chifor.me/health`.
  - The outside view: an external HTTP check service (for example check-host.net) against a public
    host on the public IP. Caddy's log must show the probe's own address, which proves the UXG does
    not SNAT WAN traffic.
  - `curl -H 'Host: proxmox.chifor.me'` against the public IP from outside -> 403.
  <!-- codex: Use a controlled external client with HTTPS --resolve so SNI and Host both select the intended site; a bare public-IP request or HTTP check can hit the wrong TLS site or Nextcloud on port 80. Verify every private/Access-gated host and vault/admin, including forged headers, and correlate socket peer plus backend identity rather than treating one probe as proof for all gateway paths. -->
- **P4**: `tofu plan` shows only `moved` entries.
  <!-- codex: Also rehearse actual mode changes and reversals, especially CNAME/A transitions and partial apply failure, using the selected Cloudflare provider version. A moves-only refactor does not validate the later operations that change DNS behavior. -->
- **P5**: every host through the tunnel (curl the public names while they are still proxied).
  Router API key requests are attributed to the caller's IP, not to the proxy. The dw terminal
  through Access still works.
  <!-- codex: Assert login and authorization behavior, not just reachability: test unauthenticated denial, authorized sessions, native Vaultwarden clients, OIDC discovery/token exchange, Keycloak forbidden paths, Trueswarm media/uploads, and the router's break-glass source restriction. Verify direct traffic loses cf-ray while tunnel traffic retains the intended image deadline, and exercise Relay's internal call separately. -->
- **P6**: per host, DNS answer, cert, status from outside and inside; latency from a dev-worker back
  to about 1-2 ms plus TLS.
- **P7**: a WireGuard client from outside reaches a private host; the same host from outside without
  VPN fails (no route); Authelia denies a non-admin user.
  <!-- codex: Private DNS returning an RFC1918 address is not evidence of isolation: repeat the WAN test with the home public IP forced through --resolve and expect denial from Caddy. Include VPN peer revocation and recovery access with Caddy/Authelia unavailable, then rehearse the documented rollback before removing old gates. -->

<!-- codex-review-status: complete -->