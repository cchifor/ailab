# Relay control-plane monitoring

`kubernetes/components/relay-control-monitoring` is an opt-in component for the
Relay release containing [Relay #90](https://git.chifor.me/cchifor/relay/pulls/90)
and [#91](https://git.chifor.me/cchifor/relay/pulls/91). It is not included in a
live Kustomization. The deployed v0.3.5 service cannot serve this endpoint.

## Credential and network boundaries

Provision two Secrets through the existing OpenBao/SOPS route, for one explicitly
approved Relay tenant:

| Namespace / Secret | Keys | Consumer |
|---|---|---|
| `relay/relay-control-metrics` | `tenant-id`, `token-sha256`, `expires-at` | Relay verifier initializer |
| `monitoring/relay-control-collector` | `token` | Prometheus ServiceMonitor |

Generate an independent random 32-byte base64url token. `token-sha256` is its
lowercase SHA-256 hex digest. `expires-at` is a future UTC RFC3339 timestamp ending
in `Z`; `tenant-id` must be the real approved tenant, which Relay verifies at
startup. Keep raw credentials in the secret-management route, never in command
arguments, environment dumps, PRs or this document. No credential is committed.

Relay receives only the verifier metadata. Its pinned Node initializer reads the
kubelet projection, validates bounded inputs, and writes an atomic regular 0600
configuration file under a private 0700 directory in a 1 MiB memory-backed volume.
It runs as UID/GID 1000 with a read-only root filesystem and no Linux capabilities.
The base Relay pod's `fsGroup: 1000` makes the 0440 projected Secret readable;
the integration test asserts this prerequisite in both base and rendered pods.
The application mounts only that completed volume read-only, not the input Secret
or collector token. Invalid input prevents startup without printing values.

The ServiceMonitor lives in `monitoring`, reads its separate token Secret there,
and selects only the labelled Relay Service in namespace `relay`. It scrapes
`/metrics/agent-control-plane` every 30 seconds with a 10-second timeout and a
100-sample ceiling. Redirects are disabled. A fixed `job="relay-control-plane"`
label connects the scrape and rules. No tenant or agent labels come from Relay.

The additional ingress policy admits only the kube-prometheus-stack Prometheus
pods in the monitoring namespace to Relay's port 8788. Existing ingress and
egress policies remain in place. The bearer credential travels over the private
cluster HTTP service network, as configured here; this is not an Internet scrape
or a claim of transport encryption. Environments requiring encrypted pod traffic
need a reviewed TLS/network design before activation. Network access does not
grant Relay API authority; the collector credential works only at the metrics path.

## Activation and verification

1. Complete Relay's new-release, database/artifact backup, restore and OIDC gates.
   Select a reviewed image containing migrations through 037 and the collector
   endpoint, and explicitly enable its agent-control-plane feature in that rollout.
   This component does not change the image or enable agent launches.
2. Provision both Secrets and validate their relationship without exposing values.
   Check Prometheus discovery/RBAC for ServiceMonitors and Secrets in `monitoring`.
3. In a separate reviewed rollout, include this component in
   `kubernetes/apps/apps/relay/kustomization.yaml`:
   `components: [../../../components/relay-control-monitoring]`.
   The resources have explicit Relay/monitoring namespaces. Do not add a namespace
   transformer to that Kustomization. The notification component can coexist.
   Update the integration test's explicit not-yet-activated assertion in that
   same rollout PR; it deliberately guards the currently disabled deployment.
4. After Flux convergence, confirm initializer success and private output
   ownership. Confirm exactly the intended Relay target with the fixed job label,
   `up=1`, a fresh `relay_control_observed_timestamp_seconds`, and the expected
   collector credential expiration. The metrics must expose no private identifiers
   or text. Anonymous requests, browser cookies, wrong/expired tokens and tenant
   query parameters must fail. Use a dedicated isolated fixture for expiry checks;
   do not disrupt the production credential to prove a unit-tested boundary.
5. Verify alerts are loaded and routed by the existing Alertmanager receiver. Run
   an approved isolated fault/recovery drill and record both firing and resolution,
   with no prompts or tokens in evidence. Missing targets and a successful HTTP
   response without observation metrics must be detected, not silently green.
6. Verify the daily `relay-postgres-dump` job's last-success metric. Its 26-hour
   age threshold plus a 30-minute alert delay detects overdue or absent job success.
   This establishes scheduling visibility only. Verify a named artifact/database
   restore separately; successful jobs do not prove recoverability.

The 12 rules cover collector failure, stale/missing observations, an incomplete
critical metric schema, missing holder
measurements, journal history gaps and quota pressure, unknown delivery, router
revocation/escrow cleanup, queued controls, old unacknowledged messages, collector
expiration and scheduled backup freshness. Thresholds are conservative initial
values and need live qualification. A history-gap alert never authorizes replay;
an unknown-outcome alert never proves an action completed. Message age does not
authorize a wake, permission approval or an access grant. Journal quotas are not
whole-filesystem/native-home usage.

## Rotation, rollback and evidence

An expired verifier does not stop the initializer or application startup. It
loads normally and the metrics endpoint returns 401 until rotation. A missing
Secret or malformed/unreadable configured verifier still blocks startup as an
explicit opt-in deployment error; provision and validate it before activation.
Remove the component through GitOps if that configuration cannot be restored.

Relay reads verifier configuration at startup and checks expiration on every
scrape, including expiry during a query. Updating a projected Secret alone does
not change its in-memory verifier or regenerate the private file. Provision the
new token/digest/deadline, restart Relay via the normal GitOps deployment path and
update the collector's Secret. Expect a bounded failed-scrape interval. Verify
old-token rejection, new-token success and fresh timestamps afterward. Secret
deletion alone does not revoke an already loaded verifier; restart or wait for its
explicit deadline. The metric grants no permission to mutate Relay state.

To remove the feature, remove the component through a reviewed PR and converge
Flux, then remove its Secrets through the normal credential process. Remove the
collector and absence rules together so an intentionally removed endpoint does
not produce a permanent missing-target alert. Preserve existing notification,
backup, network and application resources. Confirm no stale ServiceMonitor or
PrometheusRule remains.

Local validation uses five isolated Node/container and rendered-resource tests
plus Prometheus fault, threshold, missing-series and recovery scenarios. Native
`promtool` evaluates the real rules; repository-wide rule lint discovers this
component even while it is opt-in; its unfiltered push/PR workflow runs separately
from the path-filtered native setup workflow. These tests do not establish deployed scrape
discovery, token provisioning, live alert delivery or restore acceptance. Record
operator results in the Relay handoff file without credential values.
