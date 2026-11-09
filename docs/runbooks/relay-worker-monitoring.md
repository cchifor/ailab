# Relay worker and independent authority monitoring

These components are opt-in; this PR selects none in a live Kustomization.
Use the matching reviewed Relay implementation described in its
[worker operations monitoring](https://git.chifor.me/cchifor/relay/src/branch/main/docs/worker-operations-monitoring.md).
Complete its release/migration, installation, authority and runtime acceptance
gates separately. Metrics never grant authority or qualify a runtime.

## Hub worker alerts

Enable `kubernetes/components/relay-worker-monitoring` only alongside the existing
`relay-control-monitoring` component and its separately provisioned collector
Secrets. The application collector limit is 200; Relay's complete render currently
has 126 samples (including 40 histogram samples), with a tested ceiling of 160.
Both components stay off until the operator's reviewed rollout. Remove their
collector and absence/schema rules together during intentional deactivation.

The worker rules cover delayed enrollment/fence receipt, queued lifecycle work,
unknown outcomes, cleanup, configured budget startup failure, unsuccessful or
unobserved budget checks, and missing required metric series. Thresholds are
initial operational values requiring a live fault/recovery drill. Installation
ready state is stored metadata, not a heartbeat. Counts include previous linked
environments. Retained storage alone is not a leak, and active bootstrap Secrets
are excluded from cleanup alerts. Expired source/rotation records are diagnostic
inventory without an automatic incident rule because retained history can be
intentional.

The budget observer is isolated from authorization pools and callbacks but the
hub exposes its result only when the application snapshot succeeds. When the
application DB is unavailable, the whole scrape fails and the existing collector
alert owns that condition; absence of budget series is not evidence of a healthy
ledger. `started` is a startup result. A failed check can be witness mismatch,
authentication refusal, recovery-lock contention or connectivity failure. It
cannot diagnose a database outage. An entirely unconfigured authority does not
fire the check-failed rule; a partially configured/failed startup does.

The operator must budget two additional app-role connections (collector/admin)
and one independent budget-role connection. Check role and connection-proxy
limits. All use existing scoped credentials; no new login or SQL grant is needed.
The independent ledger check uses the primary because its existing `current_epoch`
function takes a bounded FOR SHARE lock. It must not be redirected to a read-only
standby or treated as a pure lock-free health query.

## Candidate journal collector and alerts

`kubernetes/components/relay-candidate-monitoring` contains **rules only**. No
candidate journal placement, metrics TLS server name or CA has yet been selected
in this repository. Do not add these absence/schema rules until a reviewed
collector has the following concrete operator-verified configuration:

- The signed run's actual journal service has Relay's optional metrics listener
  configured on a distinct private port, for example 9444. The worker-reachable
  protocol port must reject `/metrics/candidate-journal`.
- A private Service selects only that journal workload and exposes only the
  dedicated metrics port. It has no public ingress.
- A ServiceMonitor in `monitoring` selects the exact Service/namespace. It uses
  `/metrics/candidate-journal`, HTTPS, a pinned trusted CA, the certificate's
  verified `serverName`, redirects disabled, `honorLabels: false`, 30s interval,
  10s timeout, `sampleLimit: 32`, and fixed job `relay-candidate-journal`.
  Do not use `insecureSkipVerify` or copy an unrelated deployment's CA/name.
- Provision an independent random collector token through OpenBao/SOPS to the
  ServiceMonitor's own Secret. The service's digest-pinned configuration holds
  only its SHA256 verifier and expiry. No agent/holder request key or journal
  reply key is a collector credential. Rotate config, digest pin, token and
  service restart together; validate old-token rejection afterward.
- Default-deny plus a reviewed ingress rule permits only the existing
  kube-prometheus-stack Prometheus pods in namespace `monitoring` to that port.
  Confirm that other additive policies do not expose it to workers. Adding a
  narrow allow policy does not override an existing broad allow policy.
- Confirm both fixed roles and all sixteen samples. Initially each role is
  unobserved with zero timestamp/outcomes. Exported labels contain only role
  and outcome, never run/worker/deployment identifiers.

This is an explicit collector handoff, not an executable deployment claim.
Render and validate those concrete resources in the same rollout PR that selects
the candidate rules. The private listener shares the journal service's verified
TLS certificate but has independent bearer authentication. Worker collection and
candidate collection remain separate jobs/credentials/ports.

The candidate alerts cover collector reachability, incomplete metric schema,
and an informational retained driver-interruption observation. This event stays
visible until a real operation or restart; route it as information, not an outage
page, and do not manufacture activity to clear it. A last interruption can be an
idle pool disconnect and does not assert authority failure. A `not_ok` outcome
includes legitimate denials, so it is available for diagnosis without a generic
outage alert. Idle unobserved/stale roles do not alert. There is no idle SQL
probe: `inspect` locks journal state and requires launch authority. Never create,
consume or inspect a synthetic launch just to make metrics look fresh.

## Response, rollback and independent recovery

1. For missing collection, inspect target discovery, listener/process health,
   dedicated credential expiry, verified TLS/network path and matching release.
   Verify firing and recovery through the existing Alertmanager receiver.
2. For unknown operations or delayed cleanup, inspect worker/environment history
   through Relay's administrator API and obtain UID-bound Kubernetes/credential
   evidence through the authorized operator path. Preserve original identities,
   reservations and tombstones. A timeout never proves absence or completion.
3. For budget or journal observations, correlate already-authorized operation
   evidence, external witness pins, scoped DB login, primary routing and service
   logs. Do not seed/reset history, rotate epochs, add grants or replay prompts
   to repair a monitor. Restrict credential material to the existing secret path.
4. Validate application database/artifact restore, independent budget-ledger
   recovery and independent candidate-journal recovery separately. Application
   `relay_control_restore_*` evidence excludes both independent authorities.
   Preserve digest-pinned runtime images and external witnesses required by the
   reviewed recovery ceremony. An application-only restore cannot resume worker
   authority by itself.
5. To roll back monitoring, remove the relevant opt-in collector and its absence
   rules together, then remove only its dedicated credential through the normal
   secret-management process. Preserve databases, history, runtime gates and
   other operational controls.

Local checks cover rule syntax, threshold/delay/firing/resolution, missing series,
unconfigured authority, idle journal roles, resource rendering and sample budget.
Actual installation, collection/rotation, alert routing, independent recovery and
runtime qualification remain named operator acceptance evidence. Record results
and nonsecret hashes in the rollout PR; this source change performs no live write.
