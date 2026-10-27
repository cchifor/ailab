# Implementation review — edge-ingress P2 — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Changes are required before merge: error logs can leak credentials, the Vaultwarden policy admits an unsanitized ingress path, and the runtime fixture has a Linux file-permission failure.
- Container restarts and sidecar restarts can leave unavailable endpoints or stale certificates; the expiry alert does not observe served certificates.
- Validation and parity checks miss security-relevant inputs, and the runtime fixture omits several required boundary cases.
- Reviewed `43022401..ad844d2a` against the plan and P1 documents on `feat/edge-ingress-spec`; accepted the author’s reported test and rendering results without repeating them.

## Findings

### Error logs bypass credential and query redaction
**Location:** scripts/edge/render.py:411; scripts/tests/edge_runtime.py:315
**Severity:** blocker
<!-- codex: The site-level log filter covers access logs, but Caddy's separate http.log.error logger records the original request on upstream/forward-auth failures, exposing Cf-Access-Jwt-Assertion, Cf-Access-Client-Secret, X-Api-Key and query credentials even when request_header later deletes them; Caddy 2.11.7 clones the request before handlers (https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddyhttp/server.go#L590). Apply equivalent redaction to the error/default logging path and send credential sentinels through forced backend and authentication failures, checking both stdout and stderr after those cases. -->

### Homepage bypasses Vaultwarden’s proxy trust boundary
**Location:** kubernetes/apps/apps/vaultwarden/networkpolicy.yaml:34
**Severity:** blocker
<!-- codex: This namespace-only exception admits every Homepage pod to every Vaultwarden handler on port 8080, including /admin, and lets those callers choose CF-Connecting-IP; a NetworkPolicy cannot restrict this allowance to the documented /alive probe. Remove the exception and route monitoring through a sanitizing proxy or a separately constrained health endpoint, preserving the plan's cloudflared/edge-proxy-only ingress boundary. -->

### Certificate fixture files are inaccessible to the Linux CI user
**Location:** scripts/tests/edge_runtime.py:128; scripts/tests/edge_runtime.py:131
**Severity:** blocker
<!-- codex: OpenSSL runs as container root against the bind mount, creating a root-owned private key that the host-side shutil.copy cannot read when constructing the mismatched pair, and subsequent copies also attempt to overwrite root-owned certificate files; the configured host-mode runner runs as runner (ansible/roles/gitea_runner/defaults/main.yml:40). Generate these files under the invoking UID/GID or explicitly normalize ownership, then verify the fixture under the unprivileged Linux runner identity. -->

### Liveness restarts leave the replacement container permanently drained
**Location:** kubernetes/apps/apps/edge/proxy/daemonset.yaml:90
**Severity:** important
<!-- codex: Kubernetes also executes preStop for a liveness-triggered container restart, so this hook creates /config/drain in an emptyDir that survives that restart, while the startup command never removes it. The recovered Caddy then passes TCP liveness but fails readiness indefinitely; distinguish operator drains from shutdown state or clear the shutdown marker on startup, and test a container restart within the same Pod. -->

### The reloader’s first pass can acknowledge a certificate never loaded
**Location:** kubernetes/apps/apps/edge/proxy/cert-reloader.sh:49
**Severity:** important
<!-- codex: A sidecar restart after a Secret update, or an update between Caddy startup and the sidecar's first pass, records the new hash without validating or loading it, leaving Caddy serving the previous certificate until another certificate change occurs. Treat the first observation as requiring validation and a successful reload, retrying while the admin API is unavailable, and cover independent sidecar restart in the fixture. -->

### Certificate expiry monitoring checks the Secret instead of each served certificate
**Location:** kubernetes/apps/infrastructure/monitoring/edge-proxy-rules.yaml:86; docs/runbooks/edge-ingress.md:129
**Severity:** important
<!-- codex: certmanager_certificate_expiration_timestamp_seconds observes the issued certificate, so successful renewal clears this alert even when one or more Caddy pods continue serving an expiring certificate, contrary to the plan and the P1 runbook's description. Add TLS expiry/fingerprint probes against each proxy pod or node with valid SNI, retaining the cert-manager metric as a separate issuance check. -->

### The two scrape endpoints have indistinguishable target labels
**Location:** kubernetes/apps/apps/edge/proxy/podmonitor.yaml:19
**Severity:** important
<!-- codex: Both endpoints use the same port, address and rewritten job label, so their up and scrape_* samples share the same series labels because the request path is not automatically a metric label; EdgeProxyNotScraped also remains silent when only the reloader endpoint fails. Assign distinct endpoint/source labels and alert separately on missing or failed reloader scrapes. -->

### HTTPS upstreams do not preserve the required Host
**Location:** scripts/edge/render.py:263; kubernetes/edge/hostnames.yaml:287
**Severity:** important
<!-- codex: Caddy 2.11 automatically rewrites Host to the HTTPS upstream host:port unless explicitly overridden, so dw1 receives 192.168.0.8:443 rather than the required dw1.chifor.me despite its configured SNI, with analogous unrecorded Host changes for the other HTTPS origins (https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#https). Render the intended Host explicitly and test HTTPS echo origins that inspect both Host and SNI, since the current HTTP-only backend substitution removes this behavior from the fixture. -->

### Forward-auth omits the canonical CF-Connecting-IP
**Location:** scripts/edge/render.py:434
**Severity:** important
<!-- codex: edge_authelia overwrites X-Forwarded-For and X-Real-IP but never CF-Connecting-IP, so direct requests reach Authelia without that header after Cf-* stripping, while tunnel requests retain the incoming value instead of the computed canonical address. Set CF-Connecting-IP to {client_ip} here too and assert the headers received by the auth server itself, as required by the plan. -->

### The tunnel IP regex accepts invalid addresses and rejects valid ones
**Location:** scripts/edge/render.py:57
**Severity:** important
<!-- codex: SINGLE_IP accepts values such as 999.999.999.999 and 1:2:3 while rejecting valid IPv4-embedded IPv6 such as ::ffff:192.0.2.1, so it does not enforce exactly one valid IP and malformed inputs can proceed with Caddy falling back to the socket peer. Use an actual address-validity check combined with an explicit single-header/single-value check, and add invalid numeric IPv4, invalid IPv6 compression and embedded-IPv4 IPv6 cases. -->

### Duplicate YAML keys silently replace security declarations
**Location:** scripts/edge/render.py:232
**Severity:** important
<!-- codex: yaml.safe_load silently keeps the last duplicate key before validate sees the mapping, so duplicate hostnames or duplicate exposure/edge fields are accepted despite the explicit rejection requirement; an in-memory duplicate router declaration passed validation and changed its effective mode to direct. Use a duplicate-rejecting YAML loader at every mapping level and test duplicate hostnames and security fields through load rather than through already-created dictionaries. -->

### A quoted false value disables upstream TLS verification
**Location:** scripts/edge/render.py:162; scripts/edge/render.py:268
**Severity:** important
<!-- codex: Backend option types are unchecked, and tls_insecure_skip_verify: "false" passes validation but is truthy when rendered, enabling tls_insecure_skip_verify instead of preserving certificate verification. Require a real boolean for this field and validate the string-valued TLS/Host options before interpolation, with rejection tests for incorrectly typed security options. -->

### Parity checks allow additional sensitive routes
**Location:** scripts/tests/test_edge_render.py:171
**Severity:** important
<!-- codex: The parity test checks that each frozen route exists somewhere but does not reject additional routes or compare their order, and adding an id route for ^/admin/ passed test_backends_match in an in-memory mutation. Compare complete ordered route lists and Host/SNI/TLS settings against the frozen configuration plus narrowly enumerated exceptions, and add a mutation proving an extra Keycloak admin route fails. -->

### The tunnel NetworkPolicy boundary is never exercised
**Location:** scripts/tests/edge_runtime.py:305; kubernetes/apps/apps/edge/proxy/networkpolicy.yaml:26
**Severity:** important
<!-- codex: The non-cloudflared case explicitly accepts a 200 response from the tunnel listener, and Docker networks never apply the Kubernetes policy, so these tests provide no evidence for the plan's required refusal of ordinary pods and cloudflared-strive. Add a Kubernetes/Cilium acceptance test for the allowed connector, cloudflared-strive, an ordinary edge pod and a matching-label pod in another namespace, asserting both ClusterIP and Pod-IP access where applicable. -->

### Required private-path and failure cases are absent
**Location:** scripts/tests/edge_runtime.py:327; scripts/tests/edge_runtime/servers.py:40; scripts/tests/edge_runtime.py:405
**Severity:** important
<!-- codex: Neither registry variant changes a restricted path to exposure: private, the fake auth service supplies only success or anonymous redirect rather than an explicit denial, and the certificate test never supplies malformed PEM, leaving required plan branches untested. Add private restricted-path cases on both listeners, an authenticated non-admin/403 response that must never reach the backend, and malformed-certificate rejection followed by recovery. -->

### Connection refusal aborts the fixture’s startup retry loop
**Location:** scripts/tests/edge_runtime/client.py:16; scripts/tests/edge_runtime.py:154
**Severity:** important
<!-- codex: socket.create_connection executes outside the exception handler, so a refused connection during Caddy startup exits the client container nonzero and raises from docker(), aborting the nominal 50-attempt readiness loop on its first attempt. Catch connection-establishment errors and return a case result so the caller can retry until a bounded deadline, which matters on loaded shared runners. -->

### Cancellation leaves running Docker fixtures behind
**Location:** scripts/tests/edge_runtime.py:52; scripts/tests/edge_runtime.py:459; .gitea/workflows/manifests.yaml:42
**Severity:** important
<!-- codex: Cleanup relies on finally, which normal SIGTERM cancellation does not execute, leaving detached proxy/backend containers and their networks behind when cancel-in-progress supersedes a run; PID-only names also collide across PID namespaces sharing a Docker daemon. Use a random run identifier and ownership labels, handle cancellation with bounded cleanup, and provide ownership-scoped stale-run cleanup instead of unconditional deletion based only on a PID prefix. -->

### Smoke testing can crash on a connection-closing response
**Location:** scripts/edge/smoke.py:53
**Severity:** nit
<!-- codex: HTTPConnection.getresponse clears conn.sock when the response requires connection closure, so getpeercert here can raise an uncaught AttributeError and abort the entire smoke run, including during graceful shutdown checks. Capture the peer certificate immediately after establishing TLS and report per-host failures without terminating the remaining checks. -->

### Unknown hosts do not consistently return 404
**Location:** scripts/edge/render.py:474; scripts/edge/render.py:495
**Severity:** nit
<!-- codex: The redirect listener returns 308 for any trusted caller's Host, and the tunnel listener performs header validation and HTTP redirection before checking Host, producing 400 or 308 for unknown names instead of the plan's first-rule 404. Apply a known-host check before these listener-specific actions and add unknown-host cases for :8081 and tunnel HTTP/missing-header requests. -->

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