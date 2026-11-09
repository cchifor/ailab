# Independent Relay candidate journal on ailab

This optional bundle supplies the retained launch journal required by finite
candidate worker runs. It is separate from Relay, the router, their restore sets,
and the independent inference-budget database. It creates no candidate authority,
run, witness, qualification, worker or provider budget. No live Flux inventory
references `kubernetes/optional/relay-candidate-journal`; both runtime workloads
are off by default, and the initialization Job is suspended and never included
in a continuously reconciled resource list.

The generic Relay installation skill remains infrastructure-neutral. This is an
ailab deployment prerequisite for its first finite qualification run, not a
second worker controller or a replacement for the agent's least-privilege RBAC.
The dev worker prepares and validates source; an authorized operator performs all
cluster changes and records nonsecret evidence.

## Isolation and retained state

The PostgreSQL 17 cluster has its own `relay-candidate-journal-db` namespace,
10 GiB `qnap-iscsi` PVC, fixed candidate-only roles and schema marker version 2.
It never shares a PostgreSQL cluster with the budget authority: the schema's four
`relay_candidate_*` group roles are cluster-global. The storage class must still
be observed as `Retain` before provisioning. Namespace/PVC pruning is disabled;
objects and Pods exclude ordinary Velero backup, and the database volume has an
explicit backup exclusion. The operator must verify the bound PV is also excluded
from ordinary Relay/router backup and restore selections and label it accordingly.
These source settings do not prove live retention or storage durability.

The database and initializer use restricted Pod Security, UID/GID 70, no service
account token, dropped capabilities and a read-only image filesystem. The HTTPS
service has a separate baseline namespace `relay-candidate-journal`: its strict
file reader requires root ownership and owner-only secret modes, so the service
runs as UID/GID 0 with all capabilities dropped, no privilege escalation,
RuntimeDefault seccomp, no host access, no RBAC and no service-account token. This
exception does not apply to database or worker namespaces. Its immutable image
filesystem remains read-only. Both namespaces' workloads explicitly exclude
`ailab.io/relay-worker-pool` through required node affinity; they have no
`dedicated=relay-worker` toleration. They must stay off the VM spike worker pool.

Each namespace starts with default-deny ingress and egress. The service renderer
admits only the signed control namespace's infrastructure-agent selector and the
signed execution namespace's managed-worker selector, combined as namespace AND
Pod matches. Signed journal requests still authenticate every caller; labels are
network reachability, not authority. The journal reaches only its database and
cluster DNS. Database clients are the journal and an explicitly labeled operator
client in the database namespace. Caller-side journal egress must be included in
the separately reviewed agent/candidate policies and the run's policy digest;
this bundle does not silently alter those policies.

## Reviewed inputs and initialization

1. Observe cluster identity, storage class/PV retention, ordinary backup/restore
   exclusions, node placement and DNS/certificate prerequisites. Capture the
   operator evidence. Apply the optional base only after review; its database
   StatefulSet remains at zero replicas. The base requests independent PostgreSQL
   and HTTPS certificates from the existing `ailab-ca` ClusterIssuer.
2. Select an exact reviewed Relay source commit. Obtain its
   `deploy/worker-candidate-journal/001-journal.sql` and
   `002-signed-clients.sql`, verify their hashes against that commit, and retain
   those independently reviewed pins. Both files must be applied before any run.
   The pure renderer records the supplied source commit; it does not attest Git
   contents or fetch code for the operator.
3. Prepare an owner-controlled `initialize.json`:

   ```json
   {
     "version": 1,
     "relaySourceCommit": "<reviewed 40-hex commit>",
     "schema001": {"path": "/operator/schema/001-journal.sql", "sha256": "<64 hex>"},
     "schema002": {"path": "/operator/schema/002-signed-clients.sql", "sha256": "<64 hex>"}
   }
   ```

   Render with its independently recorded SHA256:

   ```sh
   python3 -B kubernetes/optional/relay-candidate-journal/render.py \
     initialize /operator/initialize.json <config-sha256> > /operator/initialize-rendered.json
   ```

   The result is a Kubernetes List containing immutable script/schema ConfigMaps
   and a suspended one-time Job. Review it, create it once, then explicitly
   unsuspend that Job. Do not put it in Flux or retry it automatically.

The wrapper validates both SHA256s before touching PGDATA or the attempt marker.
It refuses an existing data directory or prior initialization attempt. It starts
PostgreSQL on a local Unix socket only, applies 001 and 002, creates three narrow
login roles with NULL passwords, verifies that authority/history tables are empty,
then cleanly stops PostgreSQL before writing schema marker 2 and syncing. The
reviewed 001 and 002 each contain explicit BEGIN/COMMIT. They are separate
transactions, not one transaction spanning both files; `psql -f` and
ON_ERROR_STOP do not make arbitrary SQL atomic. Every source update requires
transaction-boundary review. An interruption during either phase leaves no
success marker and requires operator inspection, not deletion or rerunning init.
A PGDATA directory plus `.initialization-started` but no
`relay-candidate-journal-schema-version` is the expected interrupted signature,
including interruption between initdb and server-start trap installation; it is
not by itself evidence of data corruption.
An earlier interruption can leave `.initialization-started` with no PGDATA at
all; that is also a refused attempt requiring inspection, not permission to retry.
If the operator independently proves this volume was never activated and held no
authority, history or workloads, replacing that failed empty initialization
volume is an owner-approved recovery option. Otherwise preserve it and design
recovery; do not discard an unknown or previously active volume.
The 600-second deadline is for an empty database ceremony, not a retained-state
upgrade. No retained-state upgrade or stale-PID repair is automated here.

After successful initialization, require the Job to be terminal and its Pod to
be gone before explicitly starting one database replica. ReadWriteOnce does not
fence two Pods using the same volume on one node. Do not overlap initialization
and runtime, and do not assume ReadWriteOncePod support without validating the
qnap CSI driver. `start.sh` bypasses the image's normal auto-initializing entrypoint and
refuses missing/wrong PG_VERSION, missing/wrong schema marker or recovery/standby
signals. A missing volume must remain unavailable, never become fresh history.
Normal runtime startup never applies SQL. It also checks that PGDATA is owned by
UID 70 with permission bits 0700 or 0750; an inherited setgid bit (2700/2750)
is accepted, matching PostgreSQL. Do not chown/chmod the retained volume root during
inspection: a root ownership mismatch can trigger kubelet fsGroup recursion and
make PGDATA group-writable, which PostgreSQL rejects. The wrapper refuses this
state with a bounded permission diagnostic and performs no automatic repair.
Record the existing ownership/mode and PV identity before an operator-reviewed
recovery; the tmpfs smoke does not establish actual kubelet fsGroup behavior. The operator provisions passwords through
a protected channel and retains the operator login outside the journal service.
Only `relay_candidate_agent_service` and `relay_candidate_holder_service` DSNs go
to the service; their group roles cannot install runs or modify journal tables.
The operator login is allowed through TLS only from the isolated operator-client
path. Local PostgreSQL administration remains peer-authenticated as OS postgres.

## Independent authority ceremony

Schema initialization intentionally leaves `worker_candidate.authority` empty.
The group `relay_candidate_operator` may install/withdraw signed runs, but cannot
insert or enable this root authority row. A database administrator must separately
review the retained database identity, independent gate ID, external epoch and
witness SHA256, establish the matching witness outside app restore inventories,
and use local peer authentication as OS postgres (UID 70) in the dedicated
PostgreSQL Pod. Only the authorized cluster operator has that exec/administration
path; the dev-worker platform credential and journal service do not.

In a reviewed operator-attended `psql -X -v ON_ERROR_STOP=1` session on
`relay_candidate`, first verify there are no unexpected authority/history rows.
If this is the independently approved first authority creation, supply the four
reviewed nonsecret pins as psql variables and insert exactly one disabled row:

```sql
INSERT INTO worker_candidate.authority
  (singleton, database_identity, gate_id, external_epoch, witness_sha256, ready)
VALUES (true, :'database_identity'::uuid, :'gate_id'::uuid,
        :'external_epoch'::uuid, :'witness_sha256', false);
```

Only after comparing those exact pins to the independently retained witness and
reviewing database readiness may the administrator explicitly set that same row
ready. Require the exact four pins and `ready=false` in the update predicate,
check that exactly one row changes, and record nonsecret evidence. Never use an
upsert, create a replacement epoch to recover consumed slots, or seed authority
from a restored application backup. Then the narrow operator login can use
Relay's signed-run verifier/install command. The manifests and runtime contain
no implementation of this authority-enabling ceremony.

The database's cert-manager Secret uses a normal directory mount so a renewed
matching certificate/key pair becomes visible. PostgreSQL still retains the
previous TLS pair in memory until reload or restart. Before its leaf expires,
the authorized operator verifies the renewed pair and CA chain, uses local
`pg_ctl reload` (or an attended clean restart), then checks a new TLS connection
and served certificate expiry. `OnDelete` does not automate reload. Any CA change
also requires new pinned service inputs and coordinated restart. Never disable
TLS verification to cover a missed renewal.

## Candidate-capable service release and public configuration

The selected Relay image must contain compiled
`/app/dist/scripts/candidate-journal-service.js` and its production dependencies.
The current old `v0.9.1` deployment predates these source changes and must not be
used as a default. There is deliberately no service image default in this bundle.
Require the reviewed publication receipt's digest-only image and source commit;
inspect the actual compiled image before activation. The Relay Dockerfile compiles
scripts into `dist` but does not ship these deployment SQL files, which is why the
initializer takes separately reviewed source pins.

The pure `service` renderer requires:

```json
{
  "version": 1,
  "relaySourceCommit": "<reviewed 40-hex image source commit>",
  "relayImage": "registry.example/relay@sha256:<64 hex>",
  "config": {"path": "/operator/journal-config.json", "sha256": "<64 hex>"},
  "run": {"path": "/operator/run.json", "sha256": "<64 hex>"},
  "witness": {"path": "/operator/witness.json", "sha256": "<64 hex>"},
  "databaseCa": {"path": "/operator/db-ca.pem", "sha256": "<64 hex>"},
  "tlsCertificate": {"path": "/operator/tls.crt", "sha256": "<64 hex>"}
}
```

All input files must be canonical absolute regular files, single-linked, root or
operator owned, not group/other writable, at most 64 KiB and exactly SHA256 pinned.
No private key, DSN, password or bootstrap token is a renderer input. Refusals
include bounded reason codes for bad hashes, file ownership/modes, duplicate JSON,
missing fields and other input categories; arbitrary input or exception strings
are never echoed. It refuses
missing release pins, mismatched fixed paths, wrong public run/service bindings,
unsafe file sources and extra configuration keys. Its JSON output is valid
Kubernetes input; the renderer never calls kubectl. It validates public wiring,
not the run's cryptographic authority. The compiled service independently verifies
the exact run, issuer and journal reply key before listening.

The actual service config follows Relay's
`deploy/worker-candidate-journal/README.md` and must use these fixed paths:

| Config field | Value |
| --- | --- |
| `run.path` | `/run/relay-journal/run.json` |
| `witness.path` | `/run/relay-journal/witness.json` |
| `replyPrivateKeyFile` | `/run/relay-journal/reply-key.pem` |
| `tls.certificate.path` | `/run/relay-journal/tls.crt` |
| `tls.privateKeyFile` | `/run/relay-journal/tls-key.pem` |
| `database.ca.path` | `/run/relay-journal/db-ca.pem` |
| `database.agentConnectionStringFile` | `/run/relay-journal/agent-dsn` |
| `database.holderConnectionStringFile` | `/run/relay-journal/holder-dsn` |
| `listen` | `{"host":"0.0.0.0","port":8443}` |
| `journal.origin` and signed ledger origin | `https://relay-candidate-journal.relay-candidate-journal.svc:8443` |

Every public file pin must match the corresponding operator input; issuer keys,
journal CA/reply key, run ledger and witness must be independently established.
The signed run's hub image must equal the supplied candidate-capable Relay image.
This equality is deliberate deployment policy: every hub image change requires
a new signed run and reviewed re-render of the journal inputs.
The renderer outputs an immutable ConfigMap containing only these public files,
a ClusterIP Service, network policy and a Deployment with zero replicas.

```sh
python3 -B kubernetes/optional/relay-candidate-journal/render.py \
  service /operator/service-render.json <config-sha256> > /operator/service-rendered.json
```

### Optional private metrics collector

Metrics are off by default: omitting `metrics` from both renderer input and
service config preserves the original four objects and protocol port 8443.
The matching Relay release must contain the optional authenticated metrics
listener in `scripts/candidate-journal-service.ts`. Enabling the collector does
not change the signed run, admit a worker or make the journal authoritative.

Add this public `metrics` object to the renderer input:

```json
{
  "ca": {"path": "/operator/journal-ca.pem", "sha256": "<reviewed ledger caSha256>"},
  "serverName": "relay-candidate-journal.relay-candidate-journal.svc",
  "collectorSecret": {"name": "relay-candidate-journal-metrics-v1", "key": "token"}
}
```

Also add `metrics` to the separately pinned service config, then update its
renderer input SHA256:

```json
{
  "listen": {"host": "0.0.0.0", "port": 9444},
  "tokenSha256": "<SHA256 of the independently provisioned collector token>",
  "expiresAt": "<reviewed future UTC ISO timestamp ending in Z>"
}
```

These are input shapes, not live values. One-sided opt-in is refused. Port 9444
is the renderer default; optional renderer `metrics.port` may select another
nonprivileged port, except 8443, only when the service config matches exactly.
Loopback-only listeners, missing/invalid verifiers and expired/invalid timestamps
are refused. The current time is checked during preparation; the service enforces
expiry on every request after deployment.

The CA is a public pinned PEM certificate bundle whose SHA256 must match the
signed run's journal `caSha256`. It is rendered as an immutable, content-addressed
ConfigMap in `monitoring`. The renderer checks PEM parsing and binding, not that
the service's leaf certificate is signed by this CA or currently served. Both
listeners use the same reviewed immutable TLS pair. `serverName` must be one of
the two DNS SAN names declared by `journal-certificate.yaml` (the name above or
its `.cluster.local` form); the operator still verifies the actual certificate
chain, hostname, validity and served fingerprint before activation.

The operator provisions a **separate** collector Secret in `monitoring`, with
key `token` and name `relay-candidate-journal-metrics` or that name plus a bounded
DNS-label suffix. This namespace-local reference cannot select the journal's
runtime credentials. Use an independent random base64url token of 43–128
characters, with no newline or whitespace; hash those exact token bytes for the
service verifier. Never reuse the
agent/holder, reply-key or database credentials. The renderer accepts only the
Secret reference and verifier; it does not create/read the Secret or prove its
contents match. The token is mounted by Prometheus, never by the journal or a
worker. Verify matching-token success and wrong/expired-token rejection through
the authorized collector path; do not copy a token into a command argument, log
or PR. Rotate the Secret, service config/verifier/expiry and its digest pin with
the coordinated service restart, then verify old-token rejection.

Opt-in adds a separate metrics container port, ClusterIP Service
`relay-candidate-journal-metrics` and ingress-only NetworkPolicy in
`relay-candidate-journal`. The existing protocol Service, client ingress and
database/DNS egress remain unchanged. The new policy admits only pods with both
`app.kubernetes.io/name=prometheus` and
`app.kubernetes.io/instance=kube-prometheus-stack-prometheus` in namespace
`monitoring`, to the selected metrics port. Namespace and pod selectors share
one peer and are an AND, not alternative allowances. A candidate control or
execution namespace named `monitoring` is refused when metrics are selected.
Neither candidate namespace gains metrics access. Keep the default-deny baseline and inspect additive policies
in the destination; this source cannot negate an unrelated broad allow rule.

The generated ServiceMonitor lives in `monitoring`, carries
`release=kube-prometheus-stack`, and selects only the metrics-labelled Service in
`relay-candidate-journal`. It uses HTTPS, the explicit CA/server name and collector
Secret, `/metrics/candidate-journal`, 30s interval, 10s timeout, sampleLimit 32,
redirects disabled, `honorLabels: false`, and fixed job
`relay-candidate-journal`. Cross-namespace CA/credential lookup is not used: the
CA ConfigMap and collector Secret are in the ServiceMonitor's own namespace.
Deployment replicas remain zero; no live overlay selects these objects.
The unchanged service stops if its metrics listener cannot bind or later fails;
the existing readiness probe checks only the protocol listener. Include listener
startup/error behavior in the operator check, and inspect the separate scrape
for metrics availability rather than treating protocol readiness as proof.

Include the generated resources in the reviewed rollout that selects
`kubernetes/components/relay-candidate-monitoring`, after the
[monitoring runbook](relay-worker-monitoring.md) checks pass. Confirm the protocol
listener rejects `/metrics/candidate-journal`, the worker network cannot reach
the metrics listener, Prometheus sees all sixteen samples, and the actual alert
receiver observes firing/recovery. Remove the ServiceMonitor and absence rules
together on deactivation. Re-render without both metrics inputs to remove the
listener and dedicated network/Service/CA resources; retire its separate Secret
through the normal owner. Do not alter journal history or issue synthetic work
to refresh observations.

### Runtime Secret and TLS delivery

The operator separately provisions `relay-candidate-journal-runtime` with exactly
`reply-key.pem`, `agent-dsn`, and `holder-dsn`. The TLS key comes from the versioned immutable Secret
`relay-candidate-journal-tls-<first 32 hex characters of certificate SHA256>`,
never directly from the mutable cert-manager issuance Secret. No operator DB credential is
mounted. Secret files use root-only 0400 mode; public files use 0444. Every file
mount uses its exact `subPath`, without fsGroup or a directory projection symlink,
to satisfy Relay's root-owned/canonical-file reader. No secret values enter git,
public rendered output or logs.

Before activation, snapshot the reviewed matching TLS pair from cert-manager's
issuance Secret. The following command is an operator-only secret-to-secret pipe;
never enable shell tracing, use tee, redirect it to a file, or run the snapshot
tool with an unconnected terminal as stdout. The tool refuses terminal or regular-file
stdout and requires a consumer pipe:

```sh
kubectl --context=admin@ai -n relay-candidate-journal get secret relay-candidate-journal-tls -o json | \
  python3 -B kubernetes/optional/relay-candidate-journal/snapshot-tls.py <reviewed-certificate-sha256> | \
  kubectl --context=admin@ai create -f -
```

The helper checks the exact certificate hash and uses OpenSSL to prove its public
key matches the private key, emits only the versioned immutable Secret to the
consumer, and never echoes raw input or subprocess errors. Creation must fail if
that version already exists; verify the existing immutable version rather than
overwriting it. The rendered Deployment references this fingerprint-specific
version. Automatic cert-manager renewal may replace the issuance Secret, but
recreating a Pod still uses the old reviewed matching pair. Before that pair
expires, review the renewed certificate, create its new immutable snapshot,
re-render public pins and coordinate the service restart. No renewal activation
is automatic. Snapshot Secrets have no automatic garbage collector: after
re-pinning and proving no Pod or retained run still references an old version,
the operator may explicitly remove that old Secret. Never delete the active
version or infer disuse from age alone.

The witness ConfigMap is outside ordinary app restore inventories, not a live
freshness feed. Immutable subPath projections deliberately do not update in place.
Withdrawal/re-epoch is an explicit independent database gate/run operation; stop
the service before changing epoch/witness/certificate pins, publish a new reviewed
config and restart with it. Never infer that a frozen projection reflects a newer
external witness. Keep the retained DB and witness outside ordinary app recovery;
recovery of either authority needs a separate operator ceremony preserving consumed
history. Do not reset them from an app backup or copy a newly initialized witness
into a failed installation.

Activate one service replica only after storage, source/image, file-mode, schema,
TLS/CA, scoped-login and external-authority evidence is reviewed. Readiness/liveness use
a bounded loopback connection inside the container, so they require no kubelet
host-ingress exception in the default-deny policy; actual CNI behavior still
requires operator validation. An in-container loopback TCP readiness
probe proves only that the HTTPS listener exists; it does not prove database
readiness, run installation, valid witness, workload qualification or provider
admission. Each exec probe starts an additional Node process inside the service's
256 MiB memory limit; measure probe/startup headroom and record it in the live
prerequisite evidence before activation. Local tests do not establish that margin.
Install the exact signed run using Relay's operator verifier only after
those independent authority prerequisites are ready. Retain run tombstones and
reconcile original UID-bound workload cleanup before withdrawing run credentials.

## Local validation and remaining live evidence

Run renderer contract tests without Kubernetes:

```sh
python3 -B -m unittest discover -s scripts/tests -p test_relay_candidate_journal.py
kubectl kustomize kubernetes/optional/relay-candidate-journal
```

The explicit isolated PostgreSQL smoke uses an already-local pinned PG17 image,
no network, tmpfs-only disposable storage and `docker run --rm`:

```sh
python3 -B scripts/tests/relay-candidate-journal-postgres-smoke.py /absolute/reviewed/relay
```

On 2026-11-09 it verified bad pins before PGDATA, both actual schema files, clean
shutdown before marker 2, empty authority/history, NULL-password narrow login
roles, denial of runtime table writes/operator membership, and no automatic reseed.
A separate local Node22 Docker check executed Relay's compiled candidate-file
reader: root-owned regular files were accepted; wrong owner/mode/hash and symlink
paths were refused. TLS lifecycle tests generate local synthetic certificate/key
pairs, reject mismatches and prove Pod rendering retains the fingerprint-specific
Secret reference after the mutable issuance Secret rotates. The tests used synthetic public data and no credentials.
They did not execute a published candidate hub image or establish Kubernetes
subPath/fsGroup behavior, live TLS reachability, storage durability, restore
safety, provider execution or worker qualification. Those remain operator evidence
requirements before any activation.

The requested Opus route reviewed the 2026-11-09 source snapshot and returned
ALIGNED without blockers (request `83cbe844-2136-47fc-970c-ba15cff69aad`). Its review
snapshot SHA256 is `3fa1e785a811238004fa05190aa1e32d23ccae66a7e530b855d9e651f39b9c7a`.
The review
covered deployment/rendering, TLS snapshots, local validation and the included
Relay service/file/signature contracts. It did not include the full supporting
`candidate-journal-protocol.ts`, `candidate-journal.ts` and `primitives.ts` modules,
and did not validate a published image or live cluster. The follow-up strengthens
the smoke's positive startup assertion and records the RWO fencing, image-update,
interruption and probe-headroom advisories above; it claims no new runtime proof.
