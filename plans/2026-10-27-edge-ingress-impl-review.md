# Implementation review — edge-ingress P2 — round 2

<!-- codex-impl-review-status: finalized -->

## Round-1 verification

- Error logs bypass credential and query redaction — resolved: default and access loggers both apply the filter.
- Homepage bypasses Vaultwarden’s proxy trust boundary — resolved: the exception is removed and Homepage checks the public URL.
- Certificate fixture files are inaccessible to the Linux CI user — resolved: certificate generation and Caddy use the invoking UID/GID on POSIX.
- Liveness restarts leave the replacement container permanently drained — resolved: startup removes the shutdown marker while preserving the operator’s drain marker.
- The reloader’s first pass can acknowledge a certificate never loaded — resolved: an empty initial loaded hash requires successful validation and forced reload, with startup retries.
- Certificate expiry monitoring checks the Secret instead of each served certificate — incomplete in round 2; resolved below.
- The two scrape endpoints have indistinguishable target labels — resolved: `source=caddy|reloader` separates them.
- HTTPS upstreams do not preserve the required Host — resolved: explicit Host preservation leaves configured TLS server names intact.
- Forward-auth omits the canonical CF-Connecting-IP — resolved: the forward-auth block explicitly overwrites it.
- The tunnel IP regex accepts invalid addresses and rejects valid ones — incomplete in round 2; resolved below.
- Duplicate YAML keys silently replace security declarations — resolved: the SafeLoader subclass rejects repeated mapping keys.
- A quoted false value disables upstream TLS verification — resolved: the option requires a boolean.
- Parity checks allow additional sensitive routes — resolved: ordered route/target lists must match exactly.
- The tunnel NetworkPolicy boundary is never exercised — incomplete in round 2; resolved below.
- Required private-path and failure cases are absent — resolved: private Vaultwarden paths, explicit auth denial and malformed-certificate recovery are covered.
- Connection refusal aborts the fixture’s startup retry loop — resolved: connection establishment is caught and startup retries.
- Cancellation leaves running Docker fixtures behind — resolved: signal handling, run-scoped cleanup and age-scoped stale cleanup cover the persistent fixture resources.
- Smoke testing can crash on a connection-closing response — resolved: certificate capture happens during connection establishment.
- Unknown hosts do not consistently return 404 — resolved: unknown-host handling precedes tunnel validation and HTTP redirects.

## Findings

All five round-2 findings were accepted and fixed; none is contested.

### The required admin source tier is missing
**Location:** scripts/edge/render.py:339; kubernetes/edge/hostnames.yaml:36
**Severity:** blocker
**Resolution (accepted):** `hostnames.yaml` gains `admin_sources` (exclude the lab statics block
192.168.0.0/27, .32/28, .48/30; include the owner's VM 192.168.0.38/32), and `render.py` derives two
tiers, LAN and admin = LAN minus the exclusions plus the inclusions, validated like the LAN block.
Private hosts, Access-gated tunnel-mode hosts and restricted paths match `@not_admin`; ordinary
tunnel-mode hosts and the redirect listener keep `@not_lan`. Unit tests cover the tier arithmetic,
the matcher each host gets and an include inside an excluded block; the runtime fixture's
`automation` client (inside the LAN, inside the excluded block) reaches router and Vaultwarden's
public API, and gets 403 without reaching a backend from dsh, dw1, proxmox and `vault/admin`.

### Proxy activation races the Authelia policy rollout
**Location:** kubernetes/apps/apps/edge/kustomization.yaml:18; kubernetes/apps/apps/edge/proxy/daemonset.yaml:78
**Severity:** important
**Resolution (accepted):** the proxy leaves the `apps` Kustomization for its own Flux
Kustomization `edge-proxy` (`clusters/ai/edge-proxy.yaml`: `dependsOn: apps`, `wait: true`).
Authelia's configuration is a hash-suffixed generated ConfigMap, so a rule change rolls its pods,
and `apps` has `wait: true`, so it is Ready only once that rollout has completed on every replica;
Flux then applies the proxy. A proxy failure also no longer holds the whole `apps` layer. The
standard's install order is updated to match (follow-up docs PR).

### Client/peer equality does not validate the tunnel header
**Location:** scripts/edge/render.py:534
**Severity:** important
**Resolution (accepted):** the tunnel listener now checks the RAW header's shape first
(`@cf_client_shape`, 400): exactly one dotted-quad or one colon-bearing IPv6 token, so a port, a
zone suffix, brackets or a list are refused before Caddy's parser can strip them. The equality
check stays behind it as the parse-failure guard; a client address equal to the cloudflared peer
remains refused, which is acceptable because Cloudflare never reports a pod-CIDR client. Unit tests
assert the order; the fixture adds port, zone and bracket cases, none reaching a backend.

### Certificate alerts still cannot detect stale served certificates
**Location:** kubernetes/apps/infrastructure/monitoring/edge-proxy-rules.yaml:124; kubernetes/apps/apps/edge/proxy/cert-reloader.sh:63
**Severity:** important
**Resolution (accepted):** a `cert-probe` sidecar (alpine/openssl, pinned by digest) connects to
the pod's own :8443 with SNI `edge-canary.chifor.me` and publishes the SERVED certificate's
notAfter, a success gauge and a heartbeat on `/probe-metrics` (third PodMonitor endpoint,
`source=probe`). New alerts: `EdgeCertServedExpiringSoon` (< 14 days on any pod) and
`EdgeCertProbeFailing` (failure, stale heartbeat or failed scrape). The promtool case pairs a fresh
80-day issued certificate (EdgeCertExpiringSoon quiet) with one pod serving a 13-day certificate
(EdgeCertServedExpiringSoon fires for that pod only); the runtime fixture runs the real probe
script against the fixture proxy. The DaemonSet's grace period is now 120 s.

### NetworkPolicy failures can produce a false pass
**Location:** scripts/edge/netpol-check.sh:31
**Severity:** important
**Resolution (accepted):** each probe is now a pod created from a manifest, waited for to
`Succeeded` and read through its log, and every probe pod also runs a control request to the
direct listener :8443. "Blocked" requires curl exit 28 or 7 AND a successful control from the same
pod; a missing pod, failed admission, image pull, DNS or TLS error, or failed control is an
infrastructure error (exit 2), never a pass; a policy mismatch exits 1. Probe pods carry a
run-scoped label and an exit trap deletes them. The image is pinned by digest. Exercised against a
fake kubectl in seven modes (healthy, open policy, DNS failure, failed control, image pull,
admission rejection, no proxy pod), with the pod's own shell script run each time.

## Deferred to live verification

The summary's live-evidence items (VIP allocation, certificates from every pod, real admin and
non-admin authorization, NetworkPolicy enforcement, source attribution, failover and stream-renewal
drills) are the P2 verification steps of the plan. They run after merge, from the LAN, with
`just edge-smoke`, `scripts/edge/netpol-check.sh` and the plan's failure drills; their results are
recorded on the PR.
