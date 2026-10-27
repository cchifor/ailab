# Implementation review — edge-ingress P2 — round 2

<!-- codex-impl-review-status: pending -->

## Summary

- Sixteen round-1 findings are resolved; three remain incomplete. Two additional deployment issues are raised below.
- Accepted the author’s 91 runtime checks, mutation result, 34 unit tests, promtool results and kustomize builds without rerunning them.
- The supplied spec branch now requires a separate admin source tier (`b0f16048`, newer than the reviewed implementation); that boundary is missing.
- Deployment must ensure every Authelia replica enforces the new rules before the proxy accepts traffic.
- P2 completion still requires recorded live evidence: VIP allocation and address availability, certificates from every proxy pod, real admin/non-admin authorization, NetworkPolicy enforcement including Vaultwarden’s existing tunnel path, source attribution, and the planned failover and stream-renewal drills.

## Round-1 verification

- Error logs bypass credential and query redaction — resolved: default and access loggers both apply the filter.
- Homepage bypasses Vaultwarden’s proxy trust boundary — resolved: the exception is removed and Homepage checks the public URL.
- Certificate fixture files are inaccessible to the Linux CI user — resolved: certificate generation and Caddy use the invoking UID/GID on POSIX.
- Liveness restarts leave the replacement container permanently drained — resolved: startup removes the shutdown marker while preserving the operator’s drain marker.
- The reloader’s first pass can acknowledge a certificate never loaded — resolved: an empty initial loaded hash requires successful validation and forced reload, with startup retries.
- Certificate expiry monitoring checks the Secret instead of each served certificate — incomplete: fresh issuance plus a healthy reloader cannot detect an old Secret projection.
- The two scrape endpoints have indistinguishable target labels — resolved: `source=caddy|reloader` separates them.
- HTTPS upstreams do not preserve the required Host — resolved: explicit Host preservation leaves configured TLS server names intact.
- Forward-auth omits the canonical CF-Connecting-IP — resolved: the forward-auth block explicitly overwrites it.
- The tunnel IP regex accepts invalid addresses and rejects valid ones — incomplete: address equality is not strict header validation; peer-equal addresses fail and port/zone forms pass.
- Duplicate YAML keys silently replace security declarations — resolved: the SafeLoader subclass rejects repeated mapping keys.
- A quoted false value disables upstream TLS verification — resolved: the option requires a boolean.
- Parity checks allow additional sensitive routes — resolved: ordered route/target lists must match exactly.
- The tunnel NetworkPolicy boundary is never exercised — incomplete: the new checker can report probe failures as successful policy denials.
- Required private-path and failure cases are absent — resolved: private Vaultwarden paths, explicit auth denial and malformed-certificate recovery are covered.
- Connection refusal aborts the fixture’s startup retry loop — resolved: connection establishment is caught and startup retries.
- Cancellation leaves running Docker fixtures behind — resolved: signal handling, run-scoped cleanup and age-scoped stale cleanup cover the persistent fixture resources.
- Smoke testing can crash on a connection-closing response — resolved: certificate capture happens during connection establishment.
- Unknown hosts do not consistently return 404 — resolved: unknown-host handling precedes tunnel validation and HTTP redirects.

## Findings

### The required admin source tier is missing
**Location:** scripts/edge/render.py:339; kubernetes/edge/hostnames.yaml:36
**Severity:** blocker
<!-- codex: round-2: The supplied spec branch’s adopted standard §5.3, updated by b0f16048 after this implementation, excludes the lab statics block from admin access, but this renderer applies the broad LAN allowlist to private hosts, Access-gated tunnel-mode hosts and restricted paths, admitting dev-workers and CI runners to those surfaces. Implement and validate the separate admin_sources tier, preserve the named owner exception, and add fixture/smoke coverage proving a lab source can reach ordinary LAN services but receives 403 for admin surfaces. -->

### Proxy activation races the Authelia policy rollout
**Location:** kubernetes/apps/apps/edge/kustomization.yaml:18; kubernetes/apps/apps/edge/proxy/daemonset.yaml:78
**Severity:** important
<!-- codex: round-2: Authelia and the new proxy deploy in the same apps Flux reconciliation, while proxy readiness checks only Caddy’s health endpoint; an old Authelia replica still applying default_policy: one_factor can therefore authorize a non-admin for dsh or the monitoring UIs if its rollout lags or stalls. Stage and verify the complete Authelia policy rollout before enabling proxy traffic, using an enforced deployment dependency or a documented staged activation with real admin/non-admin checks. -->

### Client/peer equality does not validate the tunnel header
**Location:** scripts/edge/render.py:534
**Severity:** important
<!-- codex: round-2: A valid CF-Connecting-IP equal to the cloudflared peer is rejected, while malformed values such as 203.0.113.9:443 or 203.0.113.9%junk pass because Caddy’s parser strips ports and zone suffixes before returning client_ip; see the pinned version’s trustedRealClientIP implementation (https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddyhttp/server.go). Validate exactly one raw IP independently of peer trust and parser fallback, retaining IPv6/mapped-address coverage and adding peer-equal, port-bearing and zone-bearing cases. -->

### Certificate alerts still cannot detect stale served certificates
**Location:** kubernetes/apps/infrastructure/monitoring/edge-proxy-rules.yaml:124; kubernetes/apps/apps/edge/proxy/cert-reloader.sh:63
**Severity:** important
<!-- codex: round-2: If a node’s Secret projection remains old after cert-manager renews, Caddy and the reloader agree on the old files, the heartbeat and scrapes remain healthy, and the expiry rule sees only the fresh certificate, so every added certificate alert can remain silent while that pod’s served certificate expires. Implement the planned per-pod TLS certificate expiry probe, including failed/missing-probe alerts, and cover a fresh issued certificate alongside an old served certificate. -->

### NetworkPolicy failures can produce a false pass
**Location:** scripts/edge/netpol-check.sh:31
**Severity:** important
<!-- codex: round-2: Discarding kubectl’s exit status and stderr and classifying every non-HTTP result as refused makes admission, scheduling, image-pull, DNS and attach failures indistinguishable from policy enforcement; the final direct-listener check uses another pod and does not validate the denied sources. Run a successful direct-listener control from each same probe pod, verify pod execution and curl outcomes explicitly, fail on infrastructure errors, and clean up the probes with an exit trap before treating a tunnel timeout as evidence of denial. -->