# Objectstore served-certificate monitoring

The `objectstore-tls` Probe verifies a real TLS handshake to the internal
`objectstore-forge-tls.strive-ailab.svc.cluster.local:443` Service, using
`forge-objects.taild43998.ts.net` as SNI and the verification hostname. It trusts
the committed **public** ailab root CA, whose bytes are checked against the
existing OpenBao-agent trust root. It sends no S3 request or credential and
requires no Kubernetes API permissions. It does not test tailnet reachability,
signed transfers, bucket isolation or certificate serial equality.
Both the module configuration and public CA use content-hashed ConfigMaps, so
changes to either update the Deployment's volume references and replace the
exporter pod. They never replace the shared objectstore pod.

The exporter runs without a service-account token or Secrets, as UID 65534 on a
read-only filesystem. Its NetworkPolicy accepts Prometheus in `monitoring` and
permits only DNS plus port 8443 on the exact objectstore pod. The matching ingress
peer is prepared in platform #2219. The ClusterIP exporter is not publicly exposed;
do not grant general access to its parameterized `/probe` endpoint.

## Ordered activation

1. Before merging this monitor or making platform #2219 ready, re-check its
   **current reviewed head**, not only the historical draft. The contract checked
   at `96f4baba6` is: Deployment `objectstore` has pod label
   `strive.io/service: objectstore`; Service `objectstore-forge-tls` selects that
   label and maps TCP 443 to container port 8443; its ingress peer admits only
   namespace `monitoring` with `app.kubernetes.io/name=objectstore-tls-probe`.
   Compare `deploy/components/objectstore/deployment.yaml`, `forge-services.yaml`
   and `forge-netpol.yaml` with this probe's target and egress policy. Record the
   checked full commit on both PRs. If the selector or target port changes,
   update and requalify both sides before either rollout; a literal local guard
   cannot establish another repository's current contract.
2. Merge and observe this monitor through the reviewer before shared-storage TLS
   activation. Verify the Deployment, Probe selection, target scrape and
   cert-manager metrics. A failed handshake is expected before the TLS Service
   exists. These five custom alerts are gated on the exact Service's
   `kube_service_info` series and remain quiet before activation or after deliberate
   Service removal. Kubernetes inventory availability is a monitoring dependency.
3. Complete platform #2221 certificate bootstrap independently; observe its
   current-generation Certificate and installed Flux dependency. Keep platform
   #2219 draft until its separate restore, access and maintenance prerequisites
   are satisfied. This monitoring change itself does not restart storage.
4. On #2219 activation, verify `probe_success{job="objectstore-tls"} == 1`, an
   observed `probe_ssl_earliest_cert_expiry`, and
   `certmanager_certificate_expiration_timestamp_seconds` for
   `strive-ailab/objectstore-forge-tls`. A successful certificate reload request
   alone is insufficient. Compare served-chain and issued-leaf expiry after renewal.
5. Confirm the existing Alertmanager `ntfy` receiver and `ailab-alerts` subscription
   reach the responsible infrastructure operator. Source routing is established;
   actual receipt and recipient availability must be confirmed before rollout.
   Never publish test notifications or read notification credentials merely to
   validate source configuration.

## Alerts and response

The threshold is specific to platform #2221's reviewed Certificate at
`639e449ec937904b59773dd03688ab6ceb8a9eff`: `duration: 2160h` (90 days) and
`renewBefore: 720h` (30 days). Normal renewal starts around day 60, leaving a
16-day margin before the 14-day served-expiry warning. This is not a general
threshold for shorter certificates. Re-check these two fields in
`deploy/gitops/flux/clusters/ailab/objectstore-certificate/certificate.yaml`
before merging this monitor and observe actual `notAfter`/`renewalTime` after
bootstrap; an issuer can choose a different effective lifetime. A changed or
shortened lifetime requires adjusting the threshold and its fixtures before
activation. The 90-day steady-state fixture covers renewal at day 60 through
the original expiry date without a permanent warning.

| Alert | Condition | Response |
| --- | --- | --- |
| `ObjectstoreTLSProbeFailed` | Verification fails for 5 minutes | Inspect Service endpoints, network admission, hostname, trust and expiry. Keep verification enabled. |
| `ObjectstoreTLSProbeMissing` | No probe observations for 10 minutes after Service activation | Check Probe selection, exporter readiness and Prometheus-to-exporter access. |
| `ObjectstoreTLSCertificateTelemetryMissing` | Issued expiry is absent, or a successful probe has no expiry, for 10 minutes | Restore scraping/metric availability. Missing metrics do not certify renewal. |
| `ObjectstoreTLSCertificateExpiring` | Earliest served-chain expiry is within 14 days for 15 minutes | Inspect cert-manager issuance, mounted-file propagation and nginx supervisor failures. |
| `ObjectstoreTLSIssuedCertificateAhead` | Issued-leaf expiry exceeds served-chain expiry by over 5 minutes for 15 minutes | Verify whether nginx still serves the old leaf. An earlier-expiring CA/intermediate can also cause this signal; inspect the chain. |

All five use the existing infrastructure `ntfy` receiver. The 15-minute renewal
allowance covers normal Secret projection and the 30-second reload supervisor;
it is not a claim that reload was observed. Expiry comparison cannot distinguish
certificates with identical expiry timestamps or prove key/serial equality.
After a CA change, update the public trust root and verify both paths through the
reviewed trust-distribution process. Never copy a CA private key into this probe.

## Verification and rollback

`python3 -m unittest discover -s scripts/tests -p test_objectstore_tls_monitor.py -v`
checks identity, trust and network boundaries. `bash scripts/rules-lint.sh`
evaluates the alert expressions, the one-minute threshold/hold fixtures and the
hourly 90-day renewal companion fixture. Companion names follow
`<rule-file-basename>.<scenario>.test.yaml` so the same CI gate discovers them.
Runtime qualification must additionally demonstrate valid and invalid real TLS handshakes
with the pinned exporter; Kubernetes policy enforcement remains a live check.

Removing this monitor does not change the provider, certificates, buckets or PVC.
While TLS is serving, replace its observations and alerts before removing them.
Rollback of platform #2219 remains a separately scheduled shared-provider change.
