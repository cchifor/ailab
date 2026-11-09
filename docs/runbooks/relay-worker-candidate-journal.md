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
The 600-second deadline is for an empty database ceremony, not a retained-state
upgrade. No retained-state upgrade or stale-PID repair is automated here.

After successful initialization, the operator may explicitly start one database
replica. `start.sh` bypasses the image's normal auto-initializing entrypoint and
refuses missing/wrong PG_VERSION, missing/wrong schema marker or recovery/standby
signals. A missing volume must remain unavailable, never become fresh history.
Normal runtime startup never applies SQL. The operator provisions passwords through
a protected channel and retains the operator login outside the journal service.
Only `relay_candidate_agent_service` and `relay_candidate_holder_service` DSNs go
to the service; their group roles cannot install runs or modify journal tables.
The operator login is allowed through TLS only from the isolated operator-client
path. Local PostgreSQL administration remains peer-authenticated as OS postgres.

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
No private key, DSN, password or bootstrap token is a renderer input. It refuses
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
The renderer outputs an immutable ConfigMap containing only these public files,
a ClusterIP Service, network policy and a Deployment with zero replicas.

```sh
python3 -B kubernetes/optional/relay-candidate-journal/render.py \
  service /operator/service-render.json <config-sha256> > /operator/service-rendered.json
```

The operator separately provisions `relay-candidate-journal-runtime` with exactly
`reply-key.pem`, `agent-dsn`, and `holder-dsn`. The TLS key comes only from the
independent `relay-candidate-journal-tls` Secret. No operator DB credential is
mounted. Secret files use root-only 0400 mode; public files use 0444. Every file
mount uses its exact `subPath`, without fsGroup or a directory projection symlink,
to satisfy Relay's root-owned/canonical-file reader. No secret values enter git,
rendered output or logs.

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
TLS/CA, scoped-login and external-authority evidence is reviewed. A TCP readiness
probe proves only that the HTTPS listener exists; it does not prove database
readiness, run installation, valid witness, workload qualification or provider
admission. Install the exact signed run using Relay's operator verifier only after
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
paths were refused. The tests used synthetic public data and no credentials.
They did not execute a published candidate hub image or establish Kubernetes
subPath/fsGroup behavior, live TLS reachability, storage durability, restore
safety, provider execution or worker qualification. Those remain operator evidence
requirements before any activation.
