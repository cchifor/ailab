# Implementation review — edge-ingress P2 — round 1

<!-- codex-impl-review-status: complete -->

## Findings

### Error logs bypass credential and query redaction
**Location:** scripts/edge/render.py:411; scripts/tests/edge_runtime.py:315
**Severity:** blocker
**Resolution (accepted):** 87683835: `log default` carries the same filter; the fixture forces auth and upstream 502s with sentinels and checks the whole log (a mutation without the filter leaks four of five sentinels).

### Homepage bypasses Vaultwarden’s proxy trust boundary
**Location:** kubernetes/apps/apps/vaultwarden/networkpolicy.yaml:34
**Severity:** blocker
**Resolution (accepted):** 0f51d791: the Homepage exception is gone; its tile checks the public URL.

### Certificate fixture files are inaccessible to the Linux CI user
**Location:** scripts/tests/edge_runtime.py:128; scripts/tests/edge_runtime.py:131
**Severity:** blocker
**Resolution (accepted):** c4fe1c8b: openssl and the proxy run with --user uid:gid on POSIX hosts.

### Liveness restarts leave the replacement container permanently drained
**Location:** kubernetes/apps/apps/edge/proxy/daemonset.yaml:90
**Severity:** important
**Resolution (accepted):** a7302b4e: preStop writes /config/shutdown, removed by the container command before Caddy starts; /config/drain is the operator's alone.

### The reloader’s first pass can acknowledge a certificate never loaded
**Location:** kubernetes/apps/apps/edge/proxy/cert-reloader.sh:49
**Severity:** important
**Resolution (accepted):** a7302b4e: the first pass validates and reloads whatever is mounted, retrying every 10 s until it succeeds; a fixture case covers a renewal that lands before the reloader starts.

### Certificate expiry monitoring checks the Secret instead of each served certificate
**Location:** kubernetes/apps/infrastructure/monitoring/edge-proxy-rules.yaml:86; docs/runbooks/edge-ingress.md:129
**Severity:** important
**Resolution (accepted):** a7302b4e: heartbeat and files-not-loaded gauges, with EdgeCertNotLoaded, EdgeCertReloaderStale and EdgeCertReloaderNotScraped; with EdgeCertExpiringSoon they compose the served-certificate claim (stated in the rules file).

### The two scrape endpoints have indistinguishable target labels
**Location:** kubernetes/apps/apps/edge/proxy/podmonitor.yaml:19
**Severity:** important
**Resolution (accepted):** a7302b4e: source=caddy|reloader; EdgeProxyNotScraped reads source=caddy, EdgeCertReloaderNotScraped the reloader.

### HTTPS upstreams do not preserve the required Host
**Location:** scripts/edge/render.py:263; kubernetes/edge/hostnames.yaml:287
**Severity:** important
**Resolution (accepted):** 87683835: `header_up Host {host}` on every HTTPS upstream without host_header; the fixture's TLS echo asserts Host and SNI for dw1, proxmox and openbao.

### Forward-auth omits the canonical CF-Connecting-IP
**Location:** scripts/edge/render.py:434
**Severity:** important
**Resolution (accepted):** 87683835: set in edge_authelia; the auth stand-in echoes what it received through Remote-Name and the fixture asserts it on both listeners.

### The tunnel IP regex accepts invalid addresses and rejects valid ones
**Location:** scripts/edge/render.py:57
**Severity:** important
**Resolution (accepted):** 87683835: refused when {client_ip} == {remote_host} (Caddy's own parser fell back to the peer) or the joined header has a comma; cases for 999.999.999.999, 1:2:3 and ::ffff:192.0.2.1.

### Duplicate YAML keys silently replace security declarations
**Location:** scripts/edge/render.py:232
**Severity:** important
**Resolution (accepted):** 87683835: a SafeLoader subclass raises on any repeated key; tests load files with a duplicated host and a duplicated field.

### A quoted false value disables upstream TLS verification
**Location:** scripts/edge/render.py:162; scripts/edge/render.py:268
**Severity:** important
**Resolution (accepted):** 87683835: tls_insecure_skip_verify must be a bool; tls_ca, tls_server_name and host_header plain tokens; respond restricted; smoke statuses integers.

### Parity checks allow additional sensitive routes
**Location:** scripts/tests/test_edge_render.py:171
**Severity:** important
**Resolution (accepted):** 87683835: the ordered (path, target) lists must be equal, and Host/SNI overrides absent unless cloudflared had them or the difference is listed.

### The tunnel NetworkPolicy boundary is never exercised
**Location:** scripts/tests/edge_runtime.py:305; kubernetes/apps/apps/edge/proxy/networkpolicy.yaml:26
**Severity:** important
**Resolution (accepted):** c4fe1c8b: scripts/edge/netpol-check.sh runs the four peers against the Service and a pod IP in the cluster, after merge (P2 verification). Docker cannot apply NetworkPolicy, as the fixture's docstring now states; its tun-forged case now expects 400 (client equals peer).

### Required private-path and failure cases are absent
**Location:** scripts/tests/edge_runtime.py:327; scripts/tests/edge_runtime/servers.py:40; scripts/tests/edge_runtime.py:405
**Severity:** important
**Resolution (accepted):** c4fe1c8b: vault /admin private on both listeners, an explicit auth denial that never reaches the backend, and a malformed certificate followed by recovery.

### Connection refusal aborts the fixture’s startup retry loop
**Location:** scripts/tests/edge_runtime/client.py:16; scripts/tests/edge_runtime.py:154
**Severity:** important
**Resolution (accepted):** c4fe1c8b: create_connection is inside the handler; the harness retries to a 30 s deadline.

### Cancellation leaves running Docker fixtures behind
**Location:** scripts/tests/edge_runtime.py:52; scripts/tests/edge_runtime.py:459; .gitea/workflows/manifests.yaml:42
**Severity:** important
**Resolution (accepted):** c4fe1c8b: random run id, labels with a start time, SIGTERM/SIGHUP raise SystemExit so `finally` runs, label-and-age-scoped stale cleanup.

### Smoke testing can crash on a connection-closing response
**Location:** scripts/edge/smoke.py:53
**Severity:** nit
**Resolution (accepted):** c4fe1c8b: the certificate is read in connect().

### Unknown hosts do not consistently return 404
**Location:** scripts/edge/render.py:474; scripts/edge/render.py:495
**Severity:** nit
**Resolution (accepted):** 87683835: `respond @unknown_host 404` first on :8080 and :8081; cases cover a bad header and an http visitor for an unknown host.

## Diff stat

```text
 .gitea/workflows/manifests.yaml                    |  15 +
 justfile                                           |  16 +
 kubernetes/apps/apps/agentforge/networkpolicy.yaml |   7 +
 kubernetes/apps/apps/auth/configuration.yml        |  20 +
 kubernetes/apps/apps/dsh/networkpolicy.yaml        |   3 +
 kubernetes/apps/apps/dsh/searxng-auth.yaml         |   3 +
 kubernetes/apps/apps/edge/kustomization.yaml       |   3 +
 kubernetes/apps/apps/edge/proxy/Caddyfile          | 823 +++++++++++++++++++++
 kubernetes/apps/apps/edge/proxy/cert-reloader.sh   |  68 ++
 kubernetes/apps/apps/edge/proxy/certificate.yaml   |  24 +
 kubernetes/apps/apps/edge/proxy/daemonset.yaml     | 147 ++++
 kubernetes/apps/apps/edge/proxy/kustomization.yaml |  18 +
 kubernetes/apps/apps/edge/proxy/networkpolicy.yaml |  40 +
 kubernetes/apps/apps/edge/proxy/podmonitor.yaml    |  25 +
 kubernetes/apps/apps/edge/proxy/services.yaml      |  45 ++
 kubernetes/apps/apps/headlamp/networkpolicy.yaml   |   7 +
 kubernetes/apps/apps/keycloak/network-policy.yaml  |   7 +
 kubernetes/apps/apps/llm-router/edge.yaml          |  10 +-
 kubernetes/apps/apps/llm-router/router.yaml        |   5 +-
 kubernetes/apps/apps/relay/network-policy.yaml     |  14 +-
 .../apps/apps/vaultwarden/kustomization.yaml       |   1 +
 .../apps/apps/vaultwarden/networkpolicy.yaml       |  38 +
 kubernetes/apps/apps/vaultwarden/vaultwarden.yaml  |   6 +-
 kubernetes/apps/clusters/ai/metallb.yaml           |  39 +
 .../cert-manager-config/clusterissuers.yaml        |   4 +-
 .../metallb-config/kustomization.yaml              |   4 +
 .../apps/infrastructure/metallb-config/pool.yaml   |  28 +
 .../apps/infrastructure/metallb/helmrelease.yaml   |  53 ++
 .../apps/infrastructure/metallb/kustomization.yaml |   5 +
 .../apps/infrastructure/metallb/namespace.yaml     |  10 +
 .../monitoring/edge-proxy-rules.test.yaml          | 166 +++++
 .../monitoring/edge-proxy-rules.yaml               |  95 +++
 .../infrastructure/monitoring/kustomization.yaml   |   1 +
 .../apps/infrastructure/sources/kustomization.yaml |   1 +
 .../apps/infrastructure/sources/metallb.yaml       |   9 +
 kubernetes/edge/hostnames.yaml                     | 322 ++++++++
 scripts/edge/render.py                             | 559 ++++++++++++++
 scripts/edge/smoke.py                              | 109 +++
 scripts/tests/edge_runtime.py                      | 469 ++++++++++++
 scripts/tests/edge_runtime/client.py               |  63 ++
 scripts/tests/edge_runtime/servers.py              |  70 ++
 .../edge/cloudflared-ingress-2026-10-27.yaml       | 100 +++
 scripts/tests/test_edge_render.py                  | 276 +++++++
 43 files changed, 3718 insertions(+), 10 deletions(-)
```