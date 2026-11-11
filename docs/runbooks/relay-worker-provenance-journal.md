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
`005-runtime-presence.sql`. The provisional B6m SQL 005 hash is not an
activation pin. Review and record the exact five SHA256 values and the source
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
  "schema005": {"path": "/absolute/005-runtime-presence.sql", "sha256": "<64 hex>"}
}
```

Render with `python3 -B kubernetes/optional/relay-provenance-journal/render.py
initialize /absolute/initializer.json <initializer-json-sha256>`. JSON output
is a Kubernetes list. The Job verifies all five SQL files before touching
PGDATA, refuses any existing PGDATA, creates a fresh UTF8 `relay_provenance`
database, applies each transaction in order, checks empty authority/history,
cleanly stops PostgreSQL, and only then writes schema marker 5. An interrupted
attempt or absent marker requires attended inspection. Never reset a retained
volume or rerun the Job against it. The runtime `start.sh` refuses missing or
wrong marker/version, recovery signals, or unsafe PGDATA ownership/mode and
does not initialize or repair data.

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

After Relay B6m SQL 005 and the bounded
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
