# Inactive provenance source journal

`kubernetes/optional/relay-provenance-journal/` is an opt-in source bundle. No
Flux inventory selects it. Its independent PostgreSQL StatefulSet and cleanup
listener both have zero replicas. Rendering creates no credentials, authority,
epoch, policy, client, witness or workload. The one-time initializer is
suspended. Do not use the candidate journal PVC, namespace or backup for this
source. The 10 GiB PVC has prune disabled and the QNAP storage class retains
volumes; verify its actual PV reclaim policy before creating it.

## Source and initialization

Use a merged, reviewed Relay source commit that contains
`deploy/worker-provenance-journal/001-provenance.sql` through
`008-authority-completion.sql`. SQL 006 supplies the signed presence ledger;
SQL 007 repairs the open-execution versus closed-cleanup boundary. SQL 008 repairs
lookup expiry after lock waits and exact backing replay for already installed
databases; updated SQL 001 also contains those fixes for fresh installations.
These migrations do not install a Node signer, independent witness or execution
fence. Review and record all eight SHA256 values and the source
commit. The initializer renderer input is a single root or operator-owned,
single-link, non-group-writable regular JSON file:

```json
{
  "version": 1,
  "relaySourceCommit": "<reviewed 40-hex commit>",
  "schema001": {"path": "/absolute/001-provenance.sql", "sha256": "<64 hex>"},
  "schema002": {"path": "/absolute/002-router-runtime.sql", "sha256": "<64 hex>"},
  "schema003": {"path": "/absolute/003-cleanup-authority.sql", "sha256": "<64 hex>"},
  "schema004": {"path": "/absolute/004-cleanup-transitions.sql", "sha256": "<64 hex>"},
  "schema005": {"path": "/absolute/005-runtime-presence.sql", "sha256": "<64 hex>"},
  "schema006": {"path": "/absolute/006-runtime-presence-ledger.sql", "sha256": "<64 hex>"},
  "schema007": {"path": "/absolute/007-runtime-presence-open-lifecycle.sql", "sha256": "<64 hex>"},
  "schema008": {"path": "/absolute/008-authority-completion.sql", "sha256": "<64 hex>"}
}
```

Render with `python3 -B kubernetes/optional/relay-provenance-journal/render.py
initialize /absolute/initializer.json <initializer-json-sha256>`. JSON output
is a Kubernetes list. The Job verifies all eight SQL files before touching
PGDATA, refuses any existing PGDATA, creates a fresh UTF8 `relay_provenance`
database, applies each transaction in order, checks empty authority/history,
cleanly stops PostgreSQL, and only then writes schema marker 8. An interrupted
attempt or absent marker requires attended inspection. Never reset a retained
volume or rerun the Job against it. The runtime `start.sh` refuses missing or
wrong marker/version, recovery signals, or unsafe PGDATA ownership/mode and
does not initialize or repair data.

## Upgrade a retained schema 5, 6 or 7 database

Use the separate `upgrade` renderer; never use initialization against retained
data. It accepts `version: 1`, the exact reviewed `relaySourceCommit`,
`fromVersion: 5`, `6` or `7`, and only the missing pinned SQL files. Schema 5
requires `schema006`, `schema007` and `schema008`; schema 6 requires `schema007`
and `schema008`; schema 7 requires only `schema008`. Each has the same
absolute-path/raw-SHA256 shape as initialization. The target is always schema 8.
SQL 007 is an **inactive-source migration**: original presence, ledger and ledger
head tables must all be empty. It refuses existing evidence rather than
reinterpreting it under the repaired lifecycle. Inventory this condition through
the approved database client before scheduling maintenance. Existing presence
history requires a separately reviewed migration; do not delete evidence to pass
this guard. Other source history is preserved. A schema 7→8 upgrade does not
reapply 007 or require empty presence history:008 preserves all data and grants.
It accepts only the exact reviewed prior or repaired authority function bodies.
An unrelated function rewrite requires investigation, not overwriting its hash.

```sh
python3 -B kubernetes/optional/relay-provenance-journal/render.py \
  upgrade /absolute/upgrade-render.json <upgrade-render-json-sha256>
```

The output contains immutable SQL/script ConfigMaps and a suspended Job with
no automatic retry, service-account token or network listener. It mounts the
existing database PVC. An authorized operator must first pause issuance, stop
the source listeners and database StatefulSet, and verify that **all** other
PVC consumers are gone. ReadWriteOnce does not provide this exclusion. Keep
the new schema-8 startup guard installed while replicas remain zero, so an
older startup script cannot reopen the database during maintenance. Preserve
the independently reviewed recovery procedure and source history; this job
does not create a backup or qualify a restore. Uncertain workers stay reserved.

The Job validates every SQL pin before changing data, checks the exact old
marker, PostgreSQL 17, UID 70, owner-only or owner/group-readable directory mode
(700/750, optionally setgid), and absence of a running
postmaster or recovery signal. It starts a Unix-socket-only PostgreSQL process
with TLS disabled for that local maintenance listener. A read-only preflight
requires the retained `postgres` role to be a superuser so row-level security
cannot hide history. For schemas 5/6 it checks presence tables, admission phases
and absence of 006/007 targets before any migration commits. For schema 7 it
checks007 prerequisites without clearing history. Every upgrade checks the
exact SHA256, owner, security-definer flag and return type of both authority
functions;008 repeats this check under exclusive locks. Known incompatible
history or function drift exits after a clean stop, leaving the old marker and
no upgrade latch. Retained PostgreSQL configuration and local authentication remain
in force; review the existing peer/HBA settings and any custom includes before
maintenance. A stale 5/6 marker with already applied 006/007 DDL is refused before
the latch; inspect that drift rather than changing the marker to bypass it.
All supported starting versions accept exact already-repaired function bodies,
including databases initialized from the corrected 001. SQL 008 changes no
records and its replay is idempotent; this never bypasses a pending latch or
permits preapplied 006/007 under stale 5/6 markers.
A stale postmaster PID file or unclean shutdown needs attended
recovery review first; do not delete the PID file to pass the guard.

Once preflight passes, the Job persists `relay-provenance-journal-upgrade-pending`,
applies the missing transactions in order, and stops PostgreSQL cleanly. Only then does it atomically
replace the version marker with 8 and remove the pending latch. It does not
initialize, chmod/chown or discard retained data.
During migration execution, the shell waits on a background psql child so an
INT/TERM signal promptly invokes fast PostgreSQL shutdown and aborts any active
transaction. SIGKILL, host loss or a shutdown exceeding the Pod grace period can
still leave an unclean database; the persistent latch continues to refuse use.
The upgrade Pod also omits `fsGroup` so kubelet cannot recursively repair the
retained directory's permissions before the script checks them.
The socket and temporary directories are Pod-private `emptyDir` volumes:
[Kubernetes 1.35's emptyDir implementation](https://github.com/kubernetes/kubernetes/blob/v1.35.0/pkg/volume/emptydir/empty_dir.go)
uses mode 0777 for disk directories and kernel tmpfs defaults for memory volumes.
They remain writable by UID 70 without `fsGroup`; no root init container is needed
or admitted by this namespace's Restricted Pod Security policy. Local validation with pinned PostgreSQL 17 and root-owned scratch mounts (1777
for the socket directory and 0777 for `/tmp`), UID 70 and all capabilities dropped
passed fresh initialization, 5→8, 6→8, 7→8, populated schema 7 history preservation,
source-drift refusal, and interruption/retry checks. The old schemas used exact
Relay source `2aae6c14`; schema 008 used `faa1148b`. Fresh initialization and
7→8 also passed against the added output-hash checks at `ba496c1a`. This establishes local
process/mount behavior, not live Kubernetes qualification; verify the rendered
Pod on the target before activation.

Any interruption leaves the pending latch or old marker in place. The runtime
and upgrade Job both refuse that state; do not clear the latch, rerun migrations,
or change the version marker to manufacture success. Have the operator inspect
which transactions committed and follow the reviewed recovery procedure. SQL 007
also refuses inconsistent phase data instead of reopening a closed admission.
After a successful Job is terminal and its Pod is gone, verify the exact schema
and compatible source image before an attended database/listener restart.
All committed manifests remain optional and inactive.

## Database credentials

Only after the suspended Job is explicitly run, terminal, and its Pod is gone
may an operator set the database StatefulSet to one replica. ReadWriteOnce
does not fence two Pods on one node. SQL creates NOLOGIN purpose groups only.
An authorized operator separately creates distinct LOGIN roles with passwords,
grants only required purpose groups, and installs the independently reviewed
authority, epoch, policy, client keys and witness. The cleanup listener gets
source read and transition DSNs only; it never gets the administrator login.
Use TLS with verified server CA/name. PostgreSQL cert-manager rotation requires
an attended TLS reload and new connection check before the old certificate
expires.

## Cleanup listener

Use a reviewed, published Relay image whose receipt binds a digest to the same
source commit. It must contain compiled
`/app/dist/scripts/cleanup-journal-service.js`. The current live Relay image
predates this feature. The service renderer input has exact fields
`version`, `relaySourceCommit`, `relayImage`, `config`, `policy`,
`authorization`, `witness`, `databaseCa`, `tlsCertificate`, and `clients`.
Each public file reference has `{ "path": "/absolute/file", "sha256": "<64
hex>" }`. `relayImage` is digest-only. `clients` is one to eight explicit
`{"namespace":"name","podLabels":{"key":"value"}}` peers. Review actual
client Pod labels and existing additive NetworkPolicies in the destination;
this bundle alone cannot negate an unrelated broad allow rule.

The pinned config follows Relay's
`deploy/worker-provenance-journal/cleanup-service.example.json`. Set listener
to `{"host":"0.0.0.0","port":8443}` and origin to
`https://relay-provenance-journal.relay-provenance-journal.svc:8443`.
Use these exact paths:

| Field | Path |
| --- | --- |
| `policy.path` | `/run/relay-provenance/policy.json` |
| `authority.path` | `/run/relay-provenance/authorization.json` |
| `witness.path` | `/run/relay-provenance/witness.json` |
| `databaseCa.path` | `/run/relay-provenance/db-ca.pem` |
| `tls.certificate.path` | `/run/relay-provenance/tls.crt` |
| `tls.privateKeyFile` | `/run/relay-provenance/tls-key.pem` |
| `replyPrivateKeyFile` | `/run/relay-provenance/reply-key.pem` |

There must be 2–8 unique sources, at least one with a transition connection.
For source index `i` (zero based), set `readConnectionStringFile` to
`/run/relay-provenance/source-i-read-dsn` and, where needed,
`transitionConnectionStringFile` to
`/run/relay-provenance/source-i-transition-dsn`. Set an explicitly reviewed
`maxClockSkewMs` in 0–5000; the independent clock checks remain in Relay.
The renderer verifies public path/hash bindings and fixed listener/origin; it
does not verify signatures or grant cleanup authority. The compiled listener
must do that before binding its socket.

Render with `python3 -B kubernetes/optional/relay-provenance-journal/render.py
service /absolute/service-render.json <service-render-json-sha256>`. The output
contains an immutable public ConfigMap, Service, zero replica Deployment and
scoped NetworkPolicy. Public config, policy, authorization, witness, CA and
certificate are regular read-only `subPath` mounts, not symlink projections.
The operator separately provisions the root-only 0400
`relay-provenance-journal-runtime` Secret containing `reply-key.pem` and the
exact DSN keys derived from the source list. The matching TLS private key is
in the immutable `relay-provenance-journal-tls-<first 32 certificate-hash hex>`
Secret. The candidate journal snapshot helper was adapted here as
`snapshot-tls.py`; pipe cert-manager's issuance Secret directly through it to
`kubectl create -f -` without writing secret bytes to files or logs. It checks
certificate hash and key match and refuses terminal or regular-file output.
The rendered Deployment never references the mutable issuance Secret.

## Runtime presence source listener

After Relay SQL 001–008 and the compatible bounded
`dist/scripts/serve-runtime-presence-source.js` are reviewed and published,
render the separate `presence` mode. Its input has exact fields `version`,
`relaySourceCommit`, `relayImage`, `tlsCertificate`, `clientCa`,
`operatorRootHex`, `clientCertSha256`, and `clients`. The image and source
commit have the same receipt requirement as the cleanup listener. The two
certificate/CA fields are pinned public file references as above; the two hex
fields are public 64-character pins. `clients` selects only the qualified
supervisor Pods that hold the pinned client certificate. Both source listener
and cleanup listener start at zero replicas and have separate Services and
NetworkPolicies.

```sh
python3 -B kubernetes/optional/relay-provenance-journal/render.py \
  presence /absolute/presence-render.json <presence-render-json-sha256>
```

The presence renderer produces an applyable Kubernetes List with an immutable
public ConfigMap, Service, zero-replica Deployment and scoped policy. The
source uses the exact endpoint `/provenance/v1/runtime-presence`, TLS 1.3
client authentication, a pinned client leaf fingerprint and a restricted
observer login. Its runtime Secret is `relay-runtime-presence-source-runtime`
with only `observer-dsn`. Its immutable TLS Secret is
`relay-runtime-presence-source-tls-<first 32 certificate-hash hex>`, containing
the matching `tls.crt` and `tls.key` pair; the Deployment mounts only the key
from that immutable Secret and the independently pinned public certificate
from the ConfigMap. Prepare that snapshot from the separate
`relay-runtime-presence-source-tls` issuance Secret using
`snapshot-runtime-presence-tls.py` in the same secret-to-secret pipe as the
cleanup listener helper. The public leaf and client CA are single-file
ConfigMap `subPath` mounts; private files are root-only 0400 Secret `subPath`
mounts. The CLI receives only the fixed paths and public pins via environment
variables. Before activation, verify the observer login has only
`relay_provenance_observer` membership, client CA/leaf validity, exact client
certificate fingerprint, and that wrong or missing clients are refused.

Before activating one listener replica, verify signed authorization continuity,
current witness, source login grants, TLS chain/hostname, actual image contents,
database persistence, source topology and client identity. Recheck the served
certificate and wrong-client rejection. An unknown cleanup outcome remains
held; no manifest retries or resets it. Private key, DSN and certificate
rotation requires a new reviewed public pin and a coordinated restart. Keep
the authority database and witness outside ordinary application restore.

Local validation: `kubectl kustomize
kubernetes/optional/relay-provenance-journal` and `python3 -B -m unittest
discover -s scripts/tests -p test_relay_provenance_journal.py`.
