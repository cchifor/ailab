# Edge ingress: a self-hosted front door, with the Cloudflare tunnel kept as a switchable fallback

## Codex Review

- The round-1 fixes for initial tunnel mode, mode-independent admin authentication, Relay isolation, backend parity, and read-only DDNS are accepted.
- MetalLB L2 with `externalTrafficPolicy: Local` is sound in principle with Cilium's VXLAN/SNAT setup: [Cilium preserves external sources for local endpoints](https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/#client-source-ip-preservation), and [MetalLB considers endpoints in its election](https://metallb.io/concepts/layer2/). Allocation, bootstrap ordering, and failure behavior still need the checks below.
- Remaining security gaps concern tunnel HTTP scheme handling, Access-token logging, and verification of path normalization and source exclusions.
- The committed gateway state needs crash recovery and concurrency rules; the stated deletion rule does not implement `lan_dns: false` rollback.
- Recovery must restore DNS as well as connector routes, retain origin restrictions, and work with the front door stopped. Both pushbacks are narrowed below.

## Context

Every tunnel-published `*.chifor.me` hostname (the about 28 in `kubernetes/apps/apps/edge/cloudflared.yaml`)
is reachable only through Cloudflare, so LAN and in-cluster callers leave the house and come back.
Measured 2026-10-27 from dev-worker-1: `router.chifor.me/health` takes 65-80 ms through Cloudflare's
Bucharest PoP (`cf-ray ...-OTP`) against about 1 ms to a LAN host. Latency is the smallest cost:

- Cloudflare terminates TLS, so it sees every prompt, every line of code Codex sends, and the bearer
  keys.
- Internal LLM access dies with the WAN, even for the local-model routes.
- Edge limits (the 100 s origin timeout, bot checks) apply to internal calls.
- Every LAN caller shares the home's one public IP in the router's per-address limits.

**Out of scope**:
- `registry.chifor.me`: a DNS-only A record to the zot LXC at .36, `registry-dns.tf`.
- The `openbao-lan` NodePort and the other `*-lan` NodePorts.
- `trueswarm-admin`: its own tunnel and Access app.
- `strive.place`: `cloudflared-strive` on another Cloudflare account.

None of them changes.

The owner decided (2026-10-27):

- **Mode A, "direct", is the target default**: the home gateway forwards TCP 443 to a reverse proxy
  in ailab. No Cloudflare in the data path.
- **Mode C, "tunnel", stays available**, per hostname or globally, in git, so the estate can fall
  back to Cloudflare in one commit.
- Admin surfaces become **private**: reachable from the LAN and the VPN only.
- Everything is automated and documented as a standard, so the edge can be installed on new
  infrastructure or rebuilt from the repo alone.
- Free only: no VPS.

### Verified facts this plan relies on (2026-10-27)

- **Gateway**: UniFi UXG (model `UXGA6AA`), WAN1 PPPoE (`Internet UXG Fiber`, `ppp0`). A second WAN
  network `Internet 2` is configured but **not connected** (the device reports no `wan2`), so there
  is no failover path today. Managed by UniFi Network **10.6.109** on a Cloud Key Gen2 Plus at
  **192.168.1.152** (UniFi OS 6.0.11, site `default`).
- **No CGNAT**: the public IPv4 answers from the LAN at hop 1 with TTL 64, so it sits on the UXG.
- **DDNS**: the UXG's built-in Cloudflare DDNS keeps the apex `chifor.me` (and `chifor.net`) A record
  on the WAN IP (interface `wan`). The apex has **no AAAA** record; its A record has TTL 300.
- **LAN**: network `Home` is **192.168.0.0/23** (gateway .1), not the /24 `docs/network-plan.md`
  states. Other UniFi networks: Lab 192.168.2.0/24, Smarthome .20, Automation .25, Appliances .30,
  Cameras .40, Trusted .50, Guest .60, Mgmt .99 (all /24, VLAN-tagged).
- **Already fixed today by API, with backups**:
  - The dead WAN forwards 443 -> 192.168.0.250 (`k8s`) and 22 -> 192.168.0.68 (`git`) are deleted.
    Nothing answered at either, and both addresses were in the DHCP pool.
  - The controller's `Home` DHCP range started at **.50**. That contradicted the network plan's
    ".51", and .50 is the static address of cloudlab VM **cloud-ci-11** (Proxmox MAC prefix, online).
    It now starts at **192.168.0.52**, which frees **192.168.0.51** (no online client, no
    reservation).
  - Remaining forwards: WAN 9443 -> .160:443 and WAN 80 -> .160:80 (Nextcloud, alive).
- **UniFi API**: the key at OpenBao `af/estate/unifi` (field `api_token`, 32 chars, hand-placed
  2026-09-08, no seed: a vault wipe loses it) works on:
  - the official integration API (`/proxy/network/integration/v1/*`);
  - the classic API (`/proxy/network/api/s/default/rest/{portforward,networkconf,dynamicdns}`,
    `stat/*`);
  - v2 static DNS (`/proxy/network/v2/api/site/default/static-dns`, 60 `*.lan` records today).

  Port-forward and static-DNS objects carry no comment or ownership field.
- **LAN resolvers**: ailab VMs and the Talos nodes resolve through 1.1.1.1/9.9.9.9
  (`kubernetes/infra/variables.tf` `nameservers`, cloud-init/netplan), not the gateway.
- **Cluster**:
  - Talos, Cilium 1.19.8 (hand-installed), kube-proxy replacement, vxlan, default SNAT LB mode.
  - Cilium L2 announcements are off. Cilium's docs state that L2 announcements are **incompatible
    with `externalTrafficPolicy: Local`** and that the lease ignores local endpoints.
  - Pod CIDR 10.244.0.0/16.
  - Control-plane nodes .41-.43 are untainted and **carry
    `node.kubernetes.io/exclude-from-external-load-balancers`**; agent nodes .47-.49 carry
    `dedicated=agent`.
  - Talos mirrors docker.io through mirror.gcr.io and pulls quay.io directly.
- **cloudflared**: routes the hosts one by one to Services or LAN IPs.
  - Cloudflare Access gates dw1-4, k8s, hubble, dsh, proxmox, qnap, prometheus, alertmanager,
    openbao and `vault/admin`; the `api` gate is off (`access.tf`).
  - Its pod label is `app.kubernetes.io/name: cloudflared`; `cloudflared-strive`'s is
    `cloudflared-strive`.
- **Header trust that is an authentication boundary today**:
  - llm-router: `ROUTER_AUTH_CLIENT_ADDRESS_HEADER=cf-connecting-ip`, which also feeds the
    loopback-only break-glass check; api-keys and server-registration limits; `cf-ray` turns on the
    90 s image deadline.
  - Relay: `RELAY_CLIENT_ADDRESS_HEADER=cf-connecting-ip`.
  - Vaultwarden: `IP_HEADER=CF-Connecting-IP`; no ingress NetworkPolicy.
  - The dev-worker Caddy gate: `Cf-Access-Jwt-Assertion` selects the JWT path, otherwise Basic +
    cookie. WebSocket Origin must be `https://dwN.chifor.me`, and the site answers the IP, the
    hostname and `dwN.chifor.me`.

  Each one is safe only because its NetworkPolicy admits nothing but the Cloudflare path.
- **NetworkPolicies that admit only cloudflared**: agentforge, dsh (+ searxng-auth), headlamp,
  keycloak, llm-router-edge, relay. Relay, llm-router and searxng-auth egress to public 443 excludes
  RFC1918.
- **Authelia 4.39.28** (forward-auth `/api/authz/forward-auth`), cookie domain `chifor.me`, default
  policy `one_factor`, group `admins` exists. cert-manager has ClusterIssuer
  `letsencrypt-cloudflare` (DNS-01).
- **Tailscale** subnet router (a pod) advertises 192.168.0.0/24 and SNATs: tailnet traffic reaches
  the VIP from a pod IP.
- **Images**: `caddy:2.11.7-alpine@sha256:eb4d35b7...` is already pinned by llm-router-edge, whose
  manifest documents the runtime needs: uid/gid 1000, `NET_BIND_SERVICE` must be added (the binary
  carries that file capability), writable `/data` and `/config`, `grace_period 90s` with a 100 s
  termination grace. PyYAML is on the CI runners and the workstation.

## Approach

### 1. One registry, three modes, authentication independent of mode

**Registry** `kubernetes/edge/hostnames.yaml` is the single source of truth for the in-scope hosts. A
schema is enforced by `scripts/edge/render.py`; anything invalid fails CI. Each entry declares,
with no defaults for the security fields:

- **`exposure`**: `public` or `private`.
- **`edge`** (public only): `default`, `direct` or `tunnel`. The effective mode is the global
  `edge_default` unless pinned.
- **`auth`**: `native` (the app's own login, named in a comment), `authelia` (forward-auth with
  `group`), or `none` (public content only: status, the public assets of id/trueswarm). Required.
- **`access_gated: true`** for today's Access set. Validation **rejects** an access-gated host whose
  effective mode is `direct` unless it is `auth: authelia` or private.
- **`backend`**: `url`, plus optional `tls_insecure_skip_verify`, `tls_ca`, `tls_server_name` and
  `host_header`.
- **`routes`**: ordered path rules for the exceptional hosts: id's Keycloak guard, trueswarm's media
  split, and `private_paths` (vault `/admin`).
- **`lan_dns`**: whether the UXG serves a local record for it. It is set per host once that host
  has passed the LAN smoke test.

Global settings:

- `edge_default`, which starts at **`tunnel`**;
- `force_tunnel`, an emergency override that beats every per-host `direct` pin;
- the VIP 192.168.0.51;
- the trusted source ranges;
- the tunnel id.

Validation also rejects duplicate names, unknown keys, private hosts in tunnel mode, and a VIP inside
any DHCP range declared in `gateway.yaml`.

A **parity test** compares every registry backend (hostname, ordered paths, destination, Host, SNI,
TLS verification) with a frozen copy of today's cloudflared ingress
(`scripts/tests/fixtures/edge/cloudflared-ingress-2026-10-27.yaml`). Intended differences are listed
in the test with a reason; one example is the dw Host/SNI below.

Consumers:

| Consumer | How |
|---|---|
| Caddy config | `render.py` writes `kubernetes/apps/apps/edge/proxy/Caddyfile` (committed; CI `--check` fails on drift) |
| Cloudflare DNS | `kubernetes/infra/cloudflare` reads the YAML with `yamldecode` |
| UniFi local DNS + forward | `scripts/edge/gateway.py` (`just edge-gateway-plan` / `edge-gateway-apply`) |
| cloudflared | one catch-all rule to the proxy (P5) |

**DNS per effective mode** (Cloudflare, one `cloudflare_dns_record.edge` resource keyed by name):

| Host | Record |
|---|---|
| public + direct | CNAME `<name>` -> `chifor.me`, DNS-only, TTL 60. Effective TTL ≤ 300 s through the apex; v4 only, because the apex has no AAAA |
| public + tunnel | CNAME `<name>` -> `<tunnel>.cfargotunnel.com`, proxied (today's record, comment and Access `depends_on` preserved) |
| private | A `<name>` -> 192.168.0.51, DNS-only |

A CNAME<->A change replaces the record, and the two cannot coexist, so there is a brief NXDOMAIN
gap. That transition is rehearsed on a throwaway name, `edge-canary.chifor.me`, with the real
provider before any real host moves.

**LAN paths**:
- **Clients on the gateway resolver**, for hosts with `lan_dns`, go straight to the VIP whatever the
  public mode.
- **Clients on public resolvers** reach direct hosts through the UXG's NAT loopback. Their source
  then becomes the gateway, so attribution collapses to one address. For that reason the ailab VMs
  (dev-workers, reviewers, runners) move to the gateway resolver in P6 through their IaC.
<!-- codex: round-2: Keeping public cluster DNS avoids the documented RFC1918 egress denial, and router.yaml confirms bearer inference does not need OIDC; however Relay's router calls and new llm-router/Relay/searxng-auth OIDC sessions still depend on public DNS, so this is more than one caller's attribution tradeoff. Explicitly limit the offline guarantee to LAN bearer clients and prove it with cold caches and the WAN disconnected, or provide narrowly allowed access through the sanitizing :8443 listener without admitting Relay directly to llm-router-edge. -->
<!-- opus-pushback: Accepted for the VMs (P6 moves dev-workers, reviewers and runners to the gateway resolver). Not for the Talos nodes: pods would then resolve chifor.me names to 192.168.0.51, and the relay/llm-router/searxng-auth egress policies exclude RFC1918, so OIDC and router calls would break; in-cluster callers keep public resolution, loop back through the UXG (one caller, relay, whose attribution as the gateway is acceptable), and local inference never crosses the edge. -->

### 2. The proxy: Caddy, as a DaemonSet on the control-plane nodes

**Caddy rather than Traefik.** The apps above trust one header, `CF-Connecting-IP`, which a proxy
must compute from Cloudflare's header on the tunnel path and from the TCP peer on the direct path.
Even an app migrated to a canonical header needs that conversion at the proxy, which Caddy does
natively with per-listener `trusted_proxies` and `client_ip_headers`. Traefik's `X-Real-Ip` on a
trusted path is the cloudflared pod, so it needs a third-party plugin. The choice is proven by a
runtime fixture in CI (Verification), not by the rendered file. The repo already runs this Caddy
image. Cost: CrowdSec would need a custom build (P8, optional).

**Workload**: `edge-proxy` in namespace `edge`, a DaemonSet on the three control-plane nodes, with
the llm-router-edge runtime settings above:

- read-only root filesystem, `emptyDir` `/data` and `/config`;
- requests 100m/128Mi, limits 1 CPU/512Mi;
- `grace_period 90s`, `terminationGracePeriodSeconds: 100`;
- `maxUnavailable: 1`.

Running on the control-plane nodes is a deliberate exception to their exclude-from-LB label. They
already carry cloudflared and the apps, whereas the agent nodes run untrusted sandboxes and must not
hold the wildcard key.

**Listeners** (`auto_https off`, protocols h1 h2, no HTTP/3, `read_header 10s`, `idle 5m`,
`max_header_size 64KB`, no response timeouts, `stream_close_delay 5m` on every reverse_proxy so a
config or cert reload does not cut WebSockets):
<!-- codex: Caddy's stream_close_delay postpones forced WebSocket closure for five minutes; it does not preserve the connection indefinitely ([documented behavior](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming)). State the accepted reconnect behavior and keep the renewal test running past that delay so it cannot report a false continuity success. -->

- **:8443 "direct"** (TLS, wildcard cert) = VIP :443.
- **:8081** = VIP :80. Sources outside the trusted ranges get 403; trusted sources get a 308 to
  `https://{host}{uri}`. WAN 80 stays Nextcloud's.
- **:8080 "tunnel"** (plain HTTP), reachable only through ClusterIP `edge-proxy-tunnel`.
  - A NetworkPolicy admits namespace `edge` **and** pod label `app.kubernetes.io/name: cloudflared`.
    The test proves that `cloudflared-strive` and an ordinary pod are refused.
  - `trusted_proxies 10.244.0.0/16`, `client_ip_headers CF-Connecting-IP`.
  - A request whose `CF-Connecting-IP` is missing, not exactly one valid IP, or contains a comma
    gets **400**.
<!-- codex: The tunnel may carry a visitor's plain-HTTP request, but the header policy unconditionally tells every backend and Authelia that it was HTTPS; the Cloudflare module does not manage an Always Use HTTPS rule. Verify/enforce that redirect at Cloudflare or validate Cloudflare's overwritten scheme header before sanitization and reject/redirect HTTP before authentication or proxying, with a live http:// hostname test ([header semantics](https://developers.cloudflare.com/fundamentals/reference/http-headers/)). -->
- **:8082** `/healthz` (probes), **:9180** `/metrics` (PodMonitor; the NetworkPolicy admits the
  monitoring namespace on that port only), admin API on localhost:2019.

**Header policy** (one snippet, applied before any routing):

- **Removed on both listeners**: `Forwarded`, `X-Forwarded-*`, `X-Real-IP`, `True-Client-IP`,
  `Cdn-Loop` and `Remote-*`.
- **Also removed on :8443**: every `Cf-*` header, including `Cf-Access-Jwt-Assertion`, `Cf-Ray` and
  `Cf-Visitor`.
- **Kept on :8080**: `Cf-Ray`, `Cf-Visitor`, `Cf-Ipcountry` and the Access JWT/email headers.
- **Set toward every upstream and toward forward-auth**: `CF-Connecting-IP`, `X-Real-IP` and
  `X-Forwarded-For` = `{client_ip}`, `X-Forwarded-Proto https`, `X-Forwarded-Host {host}`.
- **Never trusted**: private authorization never reads a forwarded header; it reads the TCP peer
  (`remote_ip`).

**Trusted ranges** (private hosts, private paths, tunnel-mode hosts on :8443, the :8081 redirect):
192.168.0.0/23, 192.168.50.0/24 (Trusted), 192.168.99.0/24 (Mgmt) and, from P7, the VPN
192.168.70.0/24. **Excluded**:
- the gateway 192.168.0.1, the NAT-loopback source;
- the six node IPs .41-.43 and .47-.49, sources that only host-network software could present.

This narrows, but does not by itself prove, protection against translated sources. That is why every
path (routed VLAN, NAT loopback, pod-to-VIP, WAN) is tested by measurement.

**Routing rules** (each one a fixture test), evaluated in this order:

1. **Unknown host**: 404 on every listener.
2. **Private host**:
   - on :8443, served only to trusted peers, everyone else 403;
   - on :8080, 404.
3. **Private path on a public host** (vault `/admin` and below), before the public route:
   - same treatment as a private host;
   - matched case-insensitively on Caddy's cleaned, decoded path;
   - a raw-URI guard refuses encoded slashes or dots in that prefix (`%2f`, `%2e`, any case, double
     encoding).
<!-- codex: These rules privatize vault/admin as soon as P5 redirects cloudflared, while working WireGuard is deferred to P7 and the Tailscale-to-VIP path is explicitly denied. Make VPN readiness a prerequisite for that path's cutover too, or explicitly accept and document the intervening LAN-only administration window. -->
4. **Public host, effective mode direct**: :8443 serves any source; :8080 serves.
5. **Public host, effective mode tunnel**: :8443 serves trusted peers only; :8080 serves. This keeps
   WAN 443 from serving anything that has not been switched to direct, and it closes any direct
   bypass of Access.
6. **`auth: authelia`**: inside one `route` block (no directive reordering), `forward_auth` to
   `authelia.auth.svc.cluster.local:9091` `/api/authz/forward-auth` runs before `reverse_proxy`.
   - It copies exactly `Remote-User`, `Remote-Groups`, `Remote-Email` and `Remote-Name`.
   - If Authelia is unreachable, the request fails with 502.
   - Authelia gets, per host, an allow rule (`group:admins`, `one_factor`) followed by a deny rule
     for the same domain, so the global `one_factor` default never applies to it.

**Backends**: parity with cloudflared, with these exact points:

- **home and search**: their oauth2-proxy Services (:4180).
- **router**: `llm-router-edge`.
- **dsh**: keeps its public Host (its trust fence).
- **openbao**: upstream `https://openbao-ui.openbao.svc.cluster.local:8200`, verified with the
  `openbao-tls` `ca.crt` already issued into `edge` and server name
  `openbao.openbao.svc.cluster.local`. The UI Service is kept so a sealed vault stays reachable,
  which gets its own acceptance check.
- **proxmox (.2:8006), qnap (.1.225), dw1-4 (.8-.11)**: TLS verification skipped, as today.
  - The origin-CA pinning gap from ADR 0007 stays open and is recorded.
  - dw hosts move from cloudflared's `Host: <IP>` to `Host` and SNI `dwN.chifor.me`. This is the
    one intended parity difference, which the dev-worker site already serves and which its Origin
    check expects.
- **id (Keycloak)**:
  - a raw-URI guard returns 404 for `..`, `;` and their encoded forms in any case, including double
    encoding;
  - the path must match `^/realms/(relay|llm-router)(/|$)` or `^/resources/`;
  - everything else is 404.
- **trueswarm**: `^/(media/[^/]+|art/[^/]+|api/v1/uploads/[^/]+/content)$` -> trueswarm-muse, every
  other path -> trueswarm.

**Address: MetalLB, not Cilium.** MetalLB (L2 mode, Flux HelmRelease, namespace `metallb-system`
with privileged PSA, quay.io images pulled directly) elects the announcing node among nodes with an
active endpoint when `externalTrafficPolicy: Local`. That is the property Cilium lacks.

- `loadBalancerClass: metallb.io/metallb` on both MetalLB and the Service, so Cilium's LB-IPAM
  ignores it.
- `IPAddressPool` `edge` = 192.168.0.51/32 with `autoAssign: false`.
<!-- codex: With autoAssign disabled, the Service must explicitly request this pool or IP, for example metallb.io/address-pool: edge and metallb.io/loadBalancerIPs: 192.168.0.51; the listed Service currently requests neither and can remain Pending ([allocation rules](https://metallb.io/configuration/_advanced_ipaddresspool_configuration/)). Assert its assigned status address before advertising or enabling the gateway forward. -->
- `L2Advertisement` with `nodeSelectors` for control-plane nodes.
- Speakers run only on the control-plane nodes with `ignoreExcludeLB: true`; the label is honoured
  everywhere else.
- Service `edge-proxy` (ports 443->8443, 80->8081), `externalTrafficPolicy: Local`.

**Cilium is not changed**: no Helm upgrade, and `externalIPs` stays off. MetalLB's documented
failover is "within a few seconds" (memberlist). The failure tests below measure it, and the
acceptance target is a recovery of 30 s or less after the Caddy pod on the announcing node dies.

**Certificate**: cert-manager `Certificate` `edge-wildcard` (`*.chifor.me`, issuer
`letsencrypt-cloudflare`), mounted as a directory (no `subPath`), mode 0440, group 1000. A sidecar
from the same image runs a loop:

1. every 5 minutes, check that `tls.crt` and `tls.key` parse and that their public keys match;
2. if their hash changed, run `caddy reload --force --config /etc/caddy/Caddyfile --address
   localhost:2019`;
3. remember the new hash **only after the reload succeeds**.

A failed reload is retried and counted in a metric. Alerts: `EdgeCertReloadFailing`, and
`EdgeCertExpiringSoon` (the served certificate, probed through each node, under 14 days).

**Config rollout**: a `configMapGenerator` (namespace set: the estate's Flux-stall trap), so the
hash suffix rolls the DaemonSet.
<!-- codex: An announcing-node change sends existing direct TCP flows to a different local Caddy endpoint, which has neither their TLS nor connection state; graceful shutdown cannot migrate those streams. Exercise a normal config rollout and speaker failover with active SSE/WebSockets/uploads, and document interruption/reconnect semantics separately from the recovery target for new connections. -->

**Observability**:
- **Logs**: JSON access logs, with query strings replaced and credentials redacted (Caddy's
  default for Authorization/Cookie).
<!-- codex: Caddy's default redaction does not cover Cf-Access-Jwt-Assertion, which this proxy deliberately retains on :8080, so the new central log can expose replayable Access credentials ([default redacted headers](https://caddyserver.com/docs/caddyfile/directives/log)). Explicitly remove/redact Access JWT and service-token secret headers in logs, and assert their absence using sentinel values in the runtime fixture. -->
- **Alerts**: `EdgeProxyDown`, `EdgeUpstreamErrors` (sustained 5xx per host), the two certificate
  alerts.
- **Gatus probes**:
  - the VIP through each mode for one public host;
  - the tunnel path;
  - the WAN path in P6.

### 3. The gateway reconciler

`scripts/edge/gateway.py` (PyYAML, declared, present on the workstation and CI) plus
`kubernetes/edge/gateway.yaml` (the UniFi-specific half of the standard):
- **Connection**: controller URL, site, **the controller certificate's pinned SHA-256 fingerprint**
  (TLS is checked against it, not skipped).
- **Network**: WAN interface, LAN network ID and subnet, the trusted ranges.
- **Owned objects**: listed in `kubernetes/edge/gateway-state.yaml` (committed, no secrets, written
  by `apply`).
<!-- codex: An API create can succeed before its returned ID is durably saved or committed, leaving an orphan that the next run must treat as unowned; a git revert or another checkout can also restore stale IDs. Specify atomic per-operation checkpoints, an intent/recovery journal for ambiguous API outcomes, controller/site/type/key binding for each ID, and explicit recovery/adoption without broad automatic ownership. -->

`plan` prints the diff and a **plan hash**; `apply --plan-hash <h>` recomputes and aborts if
anything differs. Every mutation is checked by a re-read.
<!-- codex: A plan hash does not serialize two applies from different checkouts or bind apply-only options unless those inputs are hashed. Require a single-writer lock and freshness check for the canonical state, include adoption/enable/disable options in the plan, and test concurrent/stale applies before either mutates objects. -->
<!-- codex: The API inventory must be complete and schema-validated, including pagination and success/error envelopes, before computing the plan or interpreting an object as absent. Fail closed on partial or unexpected responses; otherwise collision checks can miss an existing DNS record or WAN forward despite the new ID ownership rule. -->

1. **Port forward** `ailab-edge-https`: WAN TCP 443 -> 192.168.0.51:443.
   - Ownership is the ID in the state file.
   - It aborts on: a second object with that name, any other forward on WAN 443 (a conflict), or an
     unexpected site or interface.
   - Unrelated fields are preserved, and the two Nextcloud forwards are asserted unchanged after
     every apply.
2. **Static DNS**: an A record per registry host with `lan_dns: true` -> 192.168.0.51.
   - Ownership is the ID in the state file.
   - An existing record with the same key that it does not own aborts the run, unless the operator
     passes `--adopt <key>`.
   - Deletes happen only for owned IDs whose host left the registry.
<!-- codex: This predicate never deletes a local record when its host remains registered but lan_dns becomes false, contradicting both Global and Front-door failure rollback. Define the desired DNS set as the hosts with lan_dns: true, delete only owned IDs outside that set, and test true -> false -> true without deleting the hostname. -->
   - An empty or invalid registry aborts before any mutation.
3. **`Home` DHCP range**: preconditions on the network ID, the subnet `192.168.0.1/23` and the full
   object's hash from plan time. Re-read right before the PUT, change only the start/stop fields,
   and write a full backup first. A drift between plan and apply aborts.
<!-- codex: A last-moment re-read still leaves a race before the full-object PUT, so an intervening controller/UI edit can be overwritten. Use a server-enforced conditional update if available; otherwise require an exclusive network-configuration maintenance window and describe this residual race rather than guaranteeing that all drift aborts. -->
4. **DDNS**: read-only assertion that the `chifor.me` and `chifor.net` entries exist on `wan`.
   Credentials are never read or printed. A missing entry is reported with the runbook procedure;
   the token scope for chifor.net is not established, so nothing is created.
5. **Order**: DNS records, then the forward on enable; the forward first on disable.

**WireGuard** is a separate deliverable (P7).

**API key** sources, in order:
1. `UNIFI_API_KEY`;
2. the OpenBao operator ceremony (breakglass inline, never printed);
3. offline: the new `estate/unifi` entry in `estate-seeds.sops.yaml`, decrypted with the age key,
   which works when OpenBao or the cluster is down.

The seed and the completeness matrix in `estate-provision-job.yaml` are updated in P3, per
`docs/runbooks/openbao-estate-credentials.md`.

**Why not the community UniFi OpenTofu provider**: adopting `Home` would make tofu own every
attribute of the LAN that carries the whole house, for one field. The reconciler's blast radius is
the objects listed above.

### 4. App-side changes

- **NetworkPolicies (P2, additive)**: each policy that admits cloudflared also admits the
  `edge-proxy` pods (namespace `edge` + name label), on the same ports. The trust comments in
  router.yaml, relay's network-policy.yaml and vaultwarden.yaml are rewritten for the proxy chain:
  the edge proxy overwrites `CF-Connecting-IP` on every request, so it is a sanctioned source. Today
  they assert "only cloudflared".
- **Vaultwarden (P5)**: a new ingress NetworkPolicy admits only cloudflared and edge-proxy, so no pod
  can hand it its own `CF-Connecting-IP`.
- **Relay is unchanged**: it keeps `https://router.chifor.me/v1`. Once the router is direct it loops
  back through the UXG to the proxy, which overwrites `CF-Connecting-IP`. Calling llm-router-edge
  directly would let it forge the router's trusted address, including loopback for break-glass.
<!-- codex: The P3 loopback probe originates on a VM, while Relay originates behind Cilium and its RFC1918-excluding egress policy; it does not prove this new pod-to-public-IP-to-VIP path works. Before switching router/id/sso to direct, run Relay inference and real OIDC discovery/token exchanges from the affected pods, checking Cilium drops and backend-observed source addresses. -->
- **Authelia (P2, before any LAN exposure)**: admins-only allow + deny rules for prometheus,
  alertmanager, hubble, k8s and dsh.
- **proxmox, qnap, openbao and the dw terminals** keep their own logins.
- **Recovery without the cluster**: the runbook documents LAN/VPN access to the origins directly
  (`https://192.168.0.2:8006`, `https://192.168.1.225`, `https://192.168.0.8-.11`) for when Caddy or
  the cluster is down; OpenBao has no such path.

### 5. Phases (each one a PR, each reversible)

| Phase | Change | Public effect |
|---|---|---|
| P1 | This plan, the standard `docs/standards/edge-ingress.md`, ADR 0040 (replaces ADR 0007's exposure section), runbook `docs/runbooks/edge-ingress.md`, network-plan corrections (/23, DHCP .52 and the .50 collision, VIP .51, forwards) | none |
| P2 | Registry (every host `tunnel`, `lan_dns: false`), renderer, schema + parity + fixture tests, CI, MetalLB, edge-proxy, certificate, additive NetworkPolicies, Authelia admin rules. LAN smoke and failure tests through .51 with `--resolve` | none; nothing points at .51 yet |
| P3 | Gateway reconciler, `just` recipes, estate seed. Apply: local DNS for the smoke-passed hosts, then the WAN 443 forward | 443 open; every host is tunnel-mode, so WAN gets 403/404 |
| P4 | Cloudflare module reads the registry, run from the operator's checkout with the local state: `moved` blocks only (TTLs, proxied flags, comments, imports and Access `depends_on` preserved). Then the `edge-canary` transition rehearsal | none |
| P5 | cloudflared catch-all to `edge-proxy-tunnel:8080` (config-revision bump); Vaultwarden NetworkPolicy. App policies keep admitting cloudflared, so `git revert` of P5 restores the old per-host routing | tunnel traffic flows through Caddy |
| P6 | `status` -> direct, then `router`, then the non-admin public hosts. VMs move to the gateway resolver | public DNS points home |
| P7 | WireGuard discovery spike (API objects, peers, keys, revocation, source preservation), then reconciler support, then admin hosts -> private | admin UIs leave the internet |
| P8 | Optional, separate decision: CrowdSec (custom Caddy build, LAPI, bouncer fail-open/closed policy) | WAF-like filtering |
| P9 | Cleanup: Access apps for private hosts, cloudflared peers in app NetworkPolicies, vestigial strive rules. Removing the cloudflared peers ends the P5 revert path, so this phase states that decision explicitly. Private A records stay | none |

<!-- codex: P2 advertises .51 before P3 can reconcile DHCP, so a rebuild still depends on today's manual pool correction and may collide with an existing lease. Make live DHCP exclusion and lease/reservation checks a prerequisite to creating the pool/advertisement; validating only the declared gateway.yaml range does not establish that prerequisite. -->
<!-- codex: Reverting the entire P5 PR also removes Vaultwarden's new ingress NetworkPolicy and reopens the header-forgery path the review required closing. Put that restriction in the persistent additive-policy phase, or define a routing-only rollback that retains it. -->

**Residual risk accepted** (in the ADR):
- WAN traffic now reaches the control-plane nodes, with no upstream volumetric protection. Bounds:
  per-pod resource limits, header timeouts and sizes.
- Hosts can return to the tunnel at any time.

**Rollback** (runbook):
- **Per host**: `edge: tunnel`, commit, `tofu apply`. Cached direct answers live up to 300 s; for
  that window direct clients of that host get 403 from :8443, which is acceptable for an emergency.
  For a planned move, apply DNS first and merge the Caddy change after the TTL.
- **Global**: `force_tunnel: true` (beats every pin) and `lan_dns` off for the affected hosts
  (`just edge-gateway-apply` removes those local records).
- **Front-door failure** (Caddy or MetalLB): `git revert` of P5, which takes cloudflared back to the
  per-host backends; LAN clients fall back to public DNS once the local records are removed. Both
  are rehearsed in P5 before P6.
<!-- codex: After P6, reverting P5 and removing local DNS still leaves direct public CNAMEs pointing at the failed VIP; recovery also requires force_tunnel and a Cloudflare apply, plus expiry of both public and local DNS caches. Define the combined sequence and local-record TTL, and rehearse it after a canary has actually gone direct with Caddy stopped, rather than only in the all-tunnel P5 state. -->
<!-- codex: The recovery commit must be deliverable while git.chifor.me is behind the failed proxy: Flux reads GitHub, but gotk-sync.yaml documents Gitea as the sole writer and warns its mirror can overwrite emergency GitHub commits. Include and rehearse direct LAN/port-forward access to Gitea or the documented mirror-paused emergency path, using credentials independent of this front door. -->

**WAN failover**: none is connected today. If `Internet 2` is ever connected, the apex DDNS still
follows `wan` only, and the documented response is `force_tunnel`.

## Critical files

- `kubernetes/edge/hostnames.yaml`, `gateway.yaml`, `gateway-state.yaml`.
- `scripts/edge/{render,gateway,smoke}.py`; `scripts/tests/test_edge_{render,gateway}.py`; fixture
  `scripts/tests/fixtures/edge/`; a runtime fixture harness `scripts/tests/edge_runtime.sh` (pinned
  Caddy + echo and a fake auth upstream in docker).
- `kubernetes/apps/infrastructure/sources/metallb.yaml`, `kubernetes/apps/infrastructure/metallb/`
  (HelmRelease, pool, L2Advertisement), wired into the infrastructure kustomization.
<!-- codex: Do not put the new HelmRelease and its MetalLB custom resources into the same Flux apply: on a rebuild, dry-run encounters missing CRDs/webhooks before Helm can install them, as the existing cert-manager-config and edge-connector split documents. Pin the MetalLB chart version and place pool/advertisement resources in a separate Kustomization that waits for that release, with the dependency manifests included here. -->
- `kubernetes/apps/apps/edge/proxy/` (DaemonSet, Services, generator, Certificate, NetworkPolicy,
  PodMonitor, PrometheusRule), wired into `edge/kustomization.yaml`.
- `kubernetes/infra/cloudflare/dns.tf`, `variables.tf`.
- `kubernetes/apps/apps/edge/cloudflared.yaml`.
- NetworkPolicies: agentforge, dsh, searxng-auth, headlamp, keycloak, llm-router edge, relay; a new
  one for vaultwarden; trust comments in router.yaml, relay, vaultwarden.
- `kubernetes/apps/apps/auth/configuration.yml`.
- `kubernetes/apps/infrastructure/security/openbao/` estate seeds + matrix.
- Ansible/tofu resolver settings for dev-workers, reviewers and runners (P6).
- `justfile`: `edge-render`, `edge-gateway-plan`, `edge-gateway-apply`, `edge-smoke`.
- `.gitea/workflows/manifests.yaml`: render `--check`, the unit tests, the runtime fixture.
- Docs: `docs/standards/edge-ingress.md`, `docs/decisions/0040-*.md`, `docs/runbooks/edge-ingress.md`,
  `docs/network-plan.md`, a pointer in `docs/runbooks/internet-exposure.md`, the CLAUDE.md inventory.

## Verification

- **Unit tests**:
  - Schema rejections.
  - Parity with the frozen cloudflared ingress.
  - Every routing rule and its order.
  - The header policy per listener.
  - Allowlist exclusions.
  - id and vault raw-URI guards (encoded, mixed-case, double-encoded, repeated slashes, dot
    segments).
<!-- codex: These cases are listed only as unit tests, while the runtime fixture substitutes echo backends; neither establishes how the actual Caddy-to-Keycloak/Vaultwarden chain normalizes an admitted request. Run the same raw request targets through the pinned proxy and real application versions, preserving bytes with --path-as-is and asserting protected handlers are never reached. -->
  - Deterministic output.
  - Reconciler against recorded fixtures: never touches unowned objects, aborts on collisions and
    conflicts, the plan-hash binding, DHCP read-modify-write preserving every other field.
- **Runtime fixture (CI, docker)**: the pinned Caddy runs the rendered config with backends swapped
  for an echo server and a fake Authelia. Asserted:
  - Upstream headers on both listeners: spoofed, duplicate and missing `CF-Connecting-IP`,
    `Remote-*` injection, `X-Forwarded-*`.
  - Access JWT kept only on :8080.
  - Private hosts and paths from trusted and untrusted peers (two docker networks).
  - Unknown and mismatched SNI/Host.
  - Auth service down -> 502; auth denial.
- **P2 live**:
  - **Certificate**: .51 serves the wildcard cert from each node.
  - **Smoke**: `smoke.py` from dev-worker-1, with a per-host expected outcome in the registry
    (status and, for login hosts, the expected redirect target or `WWW-Authenticate`), as anonymous
    and as an Authelia admin and non-admin test user.
  - **Allowlist from other sources**: the same private checks from an ordinary pod (403), a host on
    another VLAN (Trusted), and the Tailscale path (403, documented).
<!-- codex: A positive test from the Trusted VLAN does not establish that an untrusted routed VLAN remains excluded. Add a real Lab/Guest source denial and record both the socket peer and backend non-reachability, including forged headers, to catch any gateway translation into an allowed range. -->
  - **Header provenance**: the router logs the caller's IP on an API-key request through .51.
  - **Failure tests**:
    - kill the Caddy pod on the announcing node: measure recovery, target ≤ 30 s;
    - roll the DaemonSet with a broken image: the old pods stay serving or the VIP moves;
    - drain one node;
<!-- codex: kubectl drain with --ignore-daemonsets leaves both Caddy and MetalLB speakers running, so it does not test loss of the announcing node. Add actual node shutdown, endpoint readiness loss without deletion, and Cilium failure while the speaker remains alive; measure EndpointSlice/election and UXG ARP convergence separately, since memberlist's timing alone does not bound those failures. -->
    - restart the MetalLB speakers;
    - force-renew the certificate during an open SSE stream, a WebSocket (dw terminal) and a long
      upload.
<!-- codex: Also inject a malformed/mismatched certificate update and stop the reload sidecar: valid renewal alone cannot verify the promised retain-old-certificate, retry, and alert behavior. Check the certificate served by every replica and recovery after the bad update is repaired. -->
- **P3 live**:
  - **NAT loopback**: from a dev-worker,
    `curl --resolve router.chifor.me:443:<public IP> https://router.chifor.me/health` gets Caddy's
    403 (tunnel-mode host, source = gateway).
  - **Outside**: an external HTTP check service against `https://<public IP>/`. Caddy's access log
    shows the probe's own public address, never a LAN one, which proves the UXG keeps the WAN source.
  - **The argument for private hosts**: private authorization reads only that peer address, and the
    runtime fixture proves untrusted peers get 403. Together these cover private hosts from the WAN
    for any Host/SNI.
  - **Owner check**: one manual run from a phone on mobile data is recorded.
<!-- codex: round-2: Composing a measured WAN peer with exhaustive routing fixtures is reasonable, but https://<public IP>/ cannot validate the *.chifor.me certificate and may fail before producing any HTTP access log, so the proposed probe does not yet establish the first fact. Use a DNS-only canary hostname or the existing phone as a hotspot for a laptop running HTTPS --resolve, and record the socket peer; neither requires a paid VPS. -->
<!-- opus-pushback: There is no controlled external client without paying (no VPS; Internet 2 is not connected). The plan composes two measured facts instead: the external probe proves the UXG preserves WAN sources (HTTPS on 443, so it cannot hit Nextcloud's port 80), and the runtime fixture proves every private host, private path and forged-header case is denied to any untrusted peer whatever the SNI/Host. The owner's mobile-data run adds one end-to-end confirmation. -->
- **P4**: `tofu plan` shows only moves. Then the `edge-canary` goes tunnel -> direct -> private -> tunnel,
  including one deliberately interrupted apply, with the recovery steps recorded in the runbook.
- **P5**: every host through the tunnel:
  - unauthenticated denial and an authenticated session;
  - Vaultwarden's native client;
  - OIDC discovery and token exchange on id and sso;
  - Keycloak forbidden paths;
  - Trueswarm media and uploads;
  - the router's break-glass refused through the edge, and `cf-ray` kept;
  - the dw terminal through Access (JWT path) and from the LAN (Basic path), including WebSocket and
    upload;
  - a sealed-OpenBao UI check.

  Then the P5 revert is rehearsed.
- **P6**, per host:
  - DNS answer (no AAAA), cert, and status from outside (external check) and inside;
  - `cf-ray` absent on the direct path;
  - LAN latency back to about 1-2 ms plus TLS;
  - router attribution showing dev-worker IPs once the VMs use the gateway resolver.
- **P7**:
  - A WireGuard client from outside reaches a private host with source 192.168.70.x.
  - From outside without the VPN, `--resolve <host>:443:<public IP>` gets 403.
  - Peer revocation works.
  - Recovery access works with Caddy and Authelia stopped.

<!-- codex-review-status: complete -->