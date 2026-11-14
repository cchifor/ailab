# Independent Relay worker budget authority (opt-in)

This installs the independently retained PostgreSQL ledger required by Relay
worker allocation and llm-router's `worker-budget-authority` plugin. It is separate
from Relay PostgreSQL, router SQLite, `infra-pg`, OpenBao and their restore sets.
Its history must survive a simultaneous Relay/router restore. Lost or uncertain
ledger history denies inference; cleanup remains available.

`kubernetes/optional/relay-budget-authority/` is the reviewed base. Live Flux
references it only through the activation overlay
`kubernetes/apps/relay-budget-authority/ledger/` (Flux Kustomization
`relay-budget-authority`, owner approval
[relay#277](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-120817)).
The overlay adds nothing but the replica count, which stays **0** until step 4.
The separate initialization Job is suspended, operator-created and outside every
Flux inventory. Merging the activation installs the empty, stopped ledger
(namespace, retained claim, certificate, policies), the OpenBao policies/roles and
ESO objects for credential delivery, and nothing else: no database, no login
password, authority row, witness, qualification, consumer or enabled plugin.

| Source | Flux Kustomization | Merge effect |
| --- | --- | --- |
| `apps/backup/velero/helmrelease.yaml` | `backup` | Both schedules exclude `relay-budget-authority` |
| `apps/relay-budget-authority/ledger/` | `relay-budget-authority` (wait, after `qnap-storage`, `cert-manager-config`, `apps`, `backup`) | Base applied, StatefulSet at 0 |
| `apps/infrastructure/security/openbao/relay-budget-provision-job.yaml` | `openbao` | Policies + k8s-auth roles `af-app-relay-budget`, `af-app-router-budget`; no KV document |
| `apps/relay-budget-authority/credentials/` | `relay-budget-credentials` (no wait) | SecretStore + ExternalSecret in `relay` and `llm-router`, not Ready until step 6 |
| `components/relay-worker-budget-client`, `components/llm-router-worker-budget-reader` | none (not included) | Nothing until a later operator PR (see Consumer wiring) |

`scripts/tests/test_relay_budget_authority_activation.py` pins these gates.

## Reviewed inputs and boundaries

- PostgreSQL 17.11 uses the same digest as `apps/apps/relay/postgres.yaml`.
  One primary, no replicas, no automatic promotion, explicit 1 GiB memory and
  10 GiB retained RWO `qnap-iscsi` claim. The existing class uses `Retain`.
- The server bypasses the image entrypoint. Missing data, wrong major version,
  missing schema marker or recovery/standby signal causes refusal. It cannot
  initialize a replacement database. A failed initialization leaves a persistent
  attempt marker and requires owner investigation.
- Command-line settings enforce `fsync`, `full_page_writes` and
  `synchronous_commit`; client SQL must not weaken them. The schema additionally
  forces synchronous commit inside debit/ack functions. A successful write still
  requires **measured QNAP/CSI power-loss durability** before qualification.
- `ailab-ca` issues the server certificate. Clients use the full Service hostname,
  verify its certificate and trust the public CA. The server admits only the two
  named application logins over TLS/SCRAM. Remote administrator, plaintext and
  replication connections are refused. Administrator access is the local Unix
  socket as OS `postgres`, available only to the operator.
- NetworkPolicies admit `relay/app=relay`, `llm-router/app=llm-router`, and pods
  explicitly labelled `relay.chifor.me/budget-operator-client=true` **in the ledger
  namespace**. They add only TCP 5432 egress to the actual existing application
  labels. The database has no outgoing network permission. The optional operator
  client gets only database and kube-dns access. Namespace/pod creation privileges
  remain an operator boundary; the label itself grants no database privilege.
  The two application egress policies are additive only when the application pods
  already have their reviewed egress policies. Selecting previously unisolated
  pods would instead restrict their egress to the ledger. Installation on a
  different policy baseline must stop until that baseline is reviewed.
- Runtime pods receive separate allocation/reader DSNs only. They never receive
  the schema owner role, administrator credentials, Kubernetes API token, or
  database PVC. No SQL, app credentials, policy or recovery witness is embedded
  in these manifests.

The schema source is Relay's `deploy/worker-budget-ledger/` at a single final
reviewer-merged commit: `001-authority.sql`, `002-preflight.sql`,
`003-scoped-requests.sql`, and `004-preflight-errors.sql`, applied in that order.
The initializer requires all four; a version-1 database needs the offline upgrade
below. Do not vendor the SQL into ailab or run it as a Relay app migration.
The authoritative role/recovery design is the adjacent Relay README;
[llm-router #158](https://git.chifor.me/cchifor/llm-router/pulls/158) provides the
default-disabled runtime plugin and qualification-policy schema.

## Operator activation ceremony

The commands in this section are **operator-only Kubernetes writes**. A dev-worker
must prepare/review the PR and evidence; its read-only `platform` credential cannot
perform this ceremony. Use `--context admin@ai`, never the default home-lab context.
Do not enable application consumers until every gate below is satisfied.

1. Confirm reviewer-merged Relay/router releases, the exact ledger schema, resource
   capacity, `qnap-iscsi` retention, CA readiness, and actual app pod labels. Inspect
   the live `relay/relay` and `llm-router/llm-router` NetworkPolicies and prove they
   already select the production `app=relay` and `app=llm-router` pods with
   `policyTypes: Egress` and the required DNS, own-database, upstream and other
   application rules. Compare against `kubernetes/apps/apps/relay/network-policy.yaml`
   and `kubernetes/apps/apps/llm-router/router.yaml` at the reviewed ailab commit.
   Capture the current selectors/rules and successful application connectivity
   before applying this optional directory; repeat after application. A missing,
   changed or nonselecting baseline is a **stop**, not permission to install an
   isolated ledger-only policy. The activation overlay must first preserve or
   explicitly supply each application's full reviewed egress baseline. It preserves
   them: its Flux Kustomization depends on `apps`, which owns both baselines, and
   the activation test fails if either baseline stops selecting its pods with
   Egress, DNS and HTTPS rules. The live comparison is still this step.
   Inspect all Flux and backup selections. In a separate activation PR, exclude
   `relay-budget-authority` from **both** Velero daily/weekly schedules before
   installing it. The activation PR does this; the ledger Kustomization depends on
   `backup` at the same revision, so the schedules change first. This directory
   applies `velero.io/exclude-from-backup=true` to every resource/pod and to
   cert-manager's generated TLS Secret, and excludes the database volume, but
   these are additional guards. The operator must also label
   the dynamically provisioned backing PV with that exclusion and confirm no
   custom/cluster-wide backup selects it. Never add the ledger to the Relay
   logical dump, router restore, `infra-pg` dump, or generic app recovery job.
2. Render and inspect the optional base; apply it with StatefulSet replicas **0**.
   With the activation overlay, rendering and inspection happen before the merge
   (`kubectl kustomize kubernetes/apps/relay-budget-authority/ledger`) and the merge
   is the apply. Flux's health check waits for the PVC to bind and the Certificate
   to be issued. Wait for its retained PVC and Certificate. Record PVC UID, PV
   name/UID, QNAP LUN identity, Retain policy, image digest and issued certificate SANs.
   The backing PV is not created by this repo, so its exclusion is an operator
   gate. Do not delete or replace an existing claim to make initialization pass.
3. Export the four files from one **reviewer-merged full Relay commit SHA** using
   the local-only bundle renderer. It reads committed objects, not working-tree
   files, and refuses an existing output directory:

   ```sh
   python3 scripts/relay-budget-schema-bundle.py /path/to/relay \
     "$REVIEWED_RELAY_COMMIT" /private/operator/relay-budget-schema
   ```

   Review the source commit and every SQL change. Record the returned
   `manifestSha256` independently in the operator evidence and in
   `RELAY_BUDGET_SCHEMA_MANIFEST_SHA256` of a local copy of `initialize/job.yaml`.
   Do not derive this pin from an unreviewed live ConfigMap. The bundle includes
   source identity, ordered `SHA256SUMS`, four SQL files, and an **immutable**
   `configmap.json` named `relay-budget-reviewed-schema-once`, excluded from
   backup. It contains no credentials, authority row or witness. Apply that
   ConfigMap and render/apply the script-only `initialize/` Kustomization.
   Create the reviewed standalone Job explicitly, verify the mounted bundle and
   pin, then unsuspend it once. The Job is outside all Flux inventories, has no
   automatic retry, and uses only a local Unix socket. Missing/changed files or
   a mismatched pin refuse before creating the initialization-attempt marker.
   A failed attempt after that point requires owner investigation; do not delete
   the marker or data and retry. Successful initialization leaves both application
   passwords NULL and no authority/witness/qualification rows. Record version `4`,
   the copied source/manifest receipt in PGDATA, and the completed Job identity.
   Preserve the retained database and initialization marker.
4. After Job success and its Pod termination, delete the completed Job and its
   temporary schema/script ConfigMaps. Scale the StatefulSet to **one** replica
   in the operator's reviewed activation overlay: `replicas: 1` in
   `kubernetes/apps/relay-budget-authority/ledger/replicas.yaml`, as its own PR.
   `kubectl scale` would be reverted by Flux. Never use more than one. No
   production authority is ready yet: the authority table still has no row and
   both runtime passwords are NULL.
5. Open an interactive, protected local administration session (disable shell
   tracing and psql history):

   ```sh
   kubectl --context admin@ai -n relay-budget-authority exec -it \
     relay-budget-postgres-0 -- env PSQL_HISTORY=/dev/null \
     psql -X -h /var/run/postgresql -U postgres -d relay_budget
   ```

   Use `\password relay_budget_allocator` and `\password relay_budget_router`
   with independently generated passwords through the approved secret workflow.
   PostgreSQL's password prompt avoids cleartext passwords in SQL/history/logs.
   Generate each as 64 lowercase hex characters (`openssl rand -hex 32`), so the
   ESO-built DSN needs no escaping, and write the same value from stdin to OpenBao
   (`bao kv put -mount=af relay-budget/allocator password=-`, and
   `relay-budget/router`). See Credential delivery below.
   Keep the administrator socket inaccessible to app pods. The role memberships
   created by initialization are:

   | LOGIN | Granted NOLOGIN role | Purpose |
   | --- | --- | --- |
   | `relay_budget_allocator` | `relay_budget_client` | Relay prepare/inspect/ack/current_epoch/preflight/scope |
   | `relay_budget_router` | `relay_budget_reader` | Router runtime_snapshot/current_epoch |

   Neither login has table privileges, owner membership, superuser, role creation,
   database creation, replication or RLS bypass. `session_user` fixes each mapping;
   `SET ROLE` cannot select a different deployment or tenant list. Do not grant
   `relay_budget_owner` to either login.
6. As owner, establish a **new** instance UUID, recovery epoch UUID and witness
   digest through the independent recovery process. Insert exactly one authority
   row with `ready=false`. Store the matching JSON
   `{version:1,instanceId,recoveryEpoch,witnessDigest}` in the independently owned
   witness store, outside this repository and **all** ledger/app backup selections.
   The database holds the expected IDs/digest for comparison, not the external
   witness file. The consumers read the witness from OpenBao
   (`af/relay-budget/witness`, field `witness`); whether OpenBao qualifies as the
   independently owned witness store is an owner decision recorded before this
   step (see Credential delivery). Generate the witness digest from fresh
   independent random bytes; it is an identity fence, not a password and not the
   SHA of a self-referential JSON file. Keep evidence of its deliberate issuance
   independently.
7. Insert a reviewed `clients` row for `relay_budget_allocator` with the actual
   Relay issuer, connection UUID, router deployment digest, allowed tenant UUIDs,
   maximum tokens and maximum authority duration (at most 300000 ms). Insert the
   `readers` mapping for `relay_budget_router` with that exact deployment digest.
   Never change issuer/deployment mappings to reissue an already prepaid worker.
   Insert only operator-reviewed `qualifications` rows validated against the
   shipped router `ReviewedTokenPolicy` schema and canonical `tokenPolicyDigest`.
   Each pins actual compiled release/adapter, provider generation/account,
   endpoint/model, input/output bounds including reasoning/cache tokens,
   retained evidence, and expiry covering the **whole original worker lifetime**.
   There is intentionally no example qualifying policy or automatic seed.
   The deployment digest comes from the Relay router deployment record (see
   Consumer wiring), so that record must be fixed before the `readers` row. It is
   fixed: `docs/runbooks/relay-router-connection.md` records connection
   `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8` (issuer `relay.chifor.me`) and digest
   `547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad`, and orders
   these rows within the connection's steps. The UUID is in git before Relay is
   configured with it, so the `clients` row waits only for its `max_tokens`
   (8 × (I + 8192), relay#277 comment 120818 D); allocation stays impossible until
   that row exists.
8. Include the router reader component (Consumer wiring) so the router's files
   can be verified; the plugin stays disabled.
   Prove TLS hostname verification, TLS-only login, different role permissions,
   current primary status, synchronous settings and checksum mode. Verify the
   current external witness, preserved allocation/outbox history, and independent
   durability/recovery evidence. Failure of any gate keeps `ready=false`.
   After independent review, set `worker_budget.authority.ready=true` explicitly.
   Authority readiness is an allocation gate; Kubernetes readiness only proves
   PostgreSQL is reachable and must never be treated as qualification.
9. Only after step 8 and the qualification gates, enable the router plugin through
   the authenticated admin API: `POST /admin/v1/plugins/worker-budget-authority`
   with `{"enabled":true}`. Read back `GET /admin/v1/plugins` (state active) and the
   router log line `Worker budget authority available`. No manifest enables it.

### Credential delivery

The DSNs, public CA and external witness reach the consumers through OpenBao and
ESO, with no secret material in git. Every value is written by the operator during
the ceremony; `relay-budget-provision-job.yaml` owns only the policies and roles
and creates no document, so an ExternalSecret stays not Ready until its documents
exist.

| OpenBao (`af` KV v2) | Field | Written at | Read by |
| --- | --- | --- | --- |
| `relay-budget/allocator` | `password` | step 5 | `af-app-relay-budget` (SA `relay/relay-budget-eso`) |
| `relay-budget/router` | `password` | step 5 | `af-app-router-budget` (SA `llm-router/router-budget-eso`) |
| `relay-budget/ca` | `certificate` | after step 2 | both |
| `relay-budget/witness` | `witness` | step 6 | both |

`certificate` is the public PEM of the CA that issued the ledger's server
certificate. Compare it with the `ca.crt` of Secret `relay-budget-postgres-tls`
and with the committed `ansible/roles/openbao_agent/files/ailab-root-ca.crt`
before writing it. `witness` is the exact JSON of step 6.

ESO renders Secret `relay/relay-worker-budget` and
`llm-router/llm-router-worker-budget`, each with exactly `dsn`, `ca.crt` and
`witness.json`, labelled `velero.io/exclude-from-backup=true`. The templates fix
the login, the full Service hostname, the port and the database; only the password
comes from OpenBao. **Open question for the owner:** the witness must be outside
every ledger/app backup selection. The rendered Secrets are excluded, but OpenBao
itself is captured by its raft snapshots and by Velero (namespace `openbao`). An
OpenBao restore could therefore bring back an older witness. Against the retained,
backup-excluded ledger that fails closed (mismatch, authority denied); it is not a
false match. Record whether OpenBao is accepted as the independently owned witness
store, or name another store, before step 6.

The DSN shape is
`postgresql://<login>:<percent-encoded-password>@relay-budget-postgres.relay-budget-authority.svc.cluster.local:5432/relay_budget`
without query options. Application TLS verification cannot be disabled. Relay's
files are `RELAY_WORKER_BUDGET_DSN_FILE`, `_CA_FILE`, `_WITNESS_FILE`; router's
corresponding prefix is `ROUTER_WORKER_BUDGET`, plus
`ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST`. Witness/credential mounts must satisfy
the readers' ownership/mode checks: regular files, no hardlinks, symlinks or
group/world writes. Use read-only `subPath` files; changing the external witness
requires a deliberate consumer rollout. Do not add the witness to app ConfigMaps,
Secrets or other resources selected by app/ledger backup.

### Consumer wiring

Two components hold the wiring; neither is included by the live overlays yet. Each
mounts its namespace's ESO Secret as three read-only `subPath` files under `/run/`
(never `/var/run`, a symlink), mode 0440 root:1000, and sets the variables above.
`scripts/tests/test-relay-budget-consumers-render.sh` proves each changes nothing
else in its Deployment.

- `kubernetes/components/llm-router-worker-budget-reader`: include it from
  `kubernetes/apps/apps/llm-router/kustomization.yaml` after the router
  ExternalSecret is Ready (steps 5-6). The same PR replaces the placeholder
  `ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST` with the 64-hex digest of the reviewed
  Relay router deployment record: SHA-256 of canonical JSON of `{id, origin,
  issuer, baoOrigin, baoMount, baoPrefix}` (Relay `deploymentDigest`), the value of
  `readers.deployment_digest`. For the record in
  `kubernetes/components/relay-router-connection/connections.json` that is
  `547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad`
  (`docs/runbooks/relay-router-connection.md`, "Deployment digest"; recompute it
  before use). The activation test refuses the inclusion while the
  placeholder remains. Inclusion is a `Recreate` rollout of the router. The
  plugin stays disabled until step 9.
- `kubernetes/components/relay-worker-budget-client`: inert on the live Relay
  (v0.9.1, `a53a3ee8`), which predates these variables (first in Relay `0badd1e3`,
  not yet released). A Relay that has them starts the authority only with
  `RELAY_INFRASTRUCTURE_INSTALLATIONS`, a router connection
  (`RELAY_ROUTER_CONNECTIONS_FILE`, still refused by
  `test_relay_v060_activation_gate.py`; the reviewed record is the un-included
  `kubernetes/components/relay-router-connection`, included last) and a pinned
  worker-profile registry. Include it with that reviewed Relay release, not before.

Including either before its Secret exists leaves the new Pod unable to mount the
volume. Both use `Recreate`, so that is an outage: check `kubectl get
externalsecret` Ready first.

## Offline upgrade of a retained version-1 database

This is an operator-owned schema upgrade, not initialization or history recovery.
It applies only `002`–`004` and retains authority identity, debits, original
lifetimes, outbox requests and receipts. It never changes `ready=false` to true,
seeds request scopes, refunds allocations, rotates witnesses, or alters login
passwords. The server wrapper now requires marker `4`: coordinate the config
rollout with this maintenance window rather than restarting a version-1 server
under the new wrapper.

1. Verify independently retained ledger recovery evidence under its dedicated
   policy (never the application backup set). Record the database/PVC/PV identity,
   schema version, external witness and existing debit/outbox/receipt evidence.
   Compare a schema-only catalog dump against the original reviewed version-1
   definition, accounting for the documented login roles; unexpected DDL/function
   drift requires a separate review. The legacy SQL digest pins source evidence,
   not the live catalog. No live version-1 ledger is established by this repository
   or by the local fixture tests.
   Disable new worker admissions and stop both ledger consumers. Withdraw ledger
   authority with the operator role (`UPDATE worker_budget.authority SET ready=false`)
   and verify it is withdrawn before cleanly scaling the StatefulSet to zero.
   Confirm no Pod still mounts the claim; `ReadWriteOnce` alone does not prove this.
2. Render/review a fresh pinned bundle as above. Compare its `001-authority.sql`
   checksum to the **original initialization evidence**, then set both
   `RELAY_BUDGET_SCHEMA_MANIFEST_SHA256` and `RELAY_BUDGET_LEGACY_SCHEMA_SHA256`
   in a local copy of `initialize/upgrade-job.yaml`. The original initializer
   retained no source receipt. Unknown original SQL, or a changed `001`, is a
   stop requiring a separately reviewed upgrade; a checksum computed only from
   the new bundle cannot substitute for the original evidence.
3. Verify PostgreSQL major `17`, marker `1`, no recovery/standby signal, no live
   postmaster/PID file and no previous `relay-budget-upgrade-started` directory.
   A stale PID after a crash is a recovery case: keep consumers disabled, verify
   no live process owns the data, and use the existing reviewed recovery procedure
   and the retained version-1 wrapper to establish a clean shutdown before returning
   here. The original wrapper is `kubernetes/optional/relay-budget-authority/start.sh`
   at ailab commit `03b207f342ea542d8ec4eacbc250f71db3f5670f` (verify this full
   commit against the original deployment evidence) and uses the same pinned
   PostgreSQL 17.11 image in this directory. Extract it from that committed object
   into the reviewed recovery configuration; do not alter its version check.
   Keep all consumers/authority disabled even if earlier migrations committed.
   Do not delete the PID file or replace the schema marker to force this upgrade.
   Apply the immutable schema ConfigMap and script-only Kustomization, create the
   suspended standalone upgrade Job, review its image/pins and unsuspend once.
   It checks withdrawn authority and the absence of already applied `002`/`003`
   before creating the durable attempt marker. A refusal in those SQL checks
   cleanly stops PostgreSQL and leaves no migration-attempt marker; correct the
   prerequisite and review a fresh standalone Job. The preflight changes no ledger
   rows or schema, but normal PostgreSQL startup/shutdown still writes WAL and
   checkpoints. Once checks pass, it records
   the attempt, runs migrations in order, and writes marker `4` only after all
   commits, clean PostgreSQL shutdown and source receipts complete. The current
   reviewed Relay SQL files contain their own `BEGIN`/`COMMIT`; the script trusts
   those pinned bytes and does not impose transaction boundaries. Verify those
   boundaries for every source update. If a future reviewed file uses autocommit
   or commits mid-file, an error can leave even that file partly applied. Always
   inspect the actual catalog and retained history, not only a file number.
   An error can leave earlier migrations committed: the persistent attempt directory
   prevents automatic replay. Keep consumers and normal startup disabled; inspect
   committed schema and history with the operator before preparing any repair.
   The upgrade Job is operator-attended and has no hard active deadline, avoiding
   an automatic SIGKILL during a large retained-table ALTER. Monitor progress until
   clean shutdown. Operator cancellation, node failure or the 120-second Pod
   termination grace can still interrupt it and requires the same inspection.
   Both Jobs explicitly allow up to 3600 seconds for `pg_ctl` fast shutdown
   instead of its implicit 60-second wait. Exceeding that wait still fails without
   recording version 4; the operator must investigate before retry or recovery.
4. Confirm the same allocation IDs, token ceilings, original deadlines, outbox
   operations, receipts, witness and withdrawn authority. Ordinary old allocations
   gain NULL request-scope columns; they must not become new allocations. Confirm
   client execution on `prepare_token_only` is revoked and the new mediating
   `prepare` entry point is available. Ordinary token-only allocations remain
   intentionally supported through that wrapper; signed candidate requests must
   supply both scope and request allowance. `clients.max_requests` remains NULL
   until the independent operator ceremony enables request-limited allocations. Do not manually set marker `4` as a repair.
5. Start the pinned server at version `4`, check TLS/durability and narrow roles,
   and complete the documented qualification/recovery checks before restoring
   authority or consumers. Archive the public source/manifest, Job UID/result and
   before/after retained-history comparison. Remove only the completed standalone
   Job and temporary ConfigMaps. Keep the attempt directory as history. Rollback
   never restores an older ledger or replays `001`; application rollback must
   preserve retained history and requires its own compatibility review.

Neither standalone Job appears in a Flux inventory. The blank digest fields in
source intentionally refuse execution. Bundle publication and these local tests
are not proof of live ledger storage durability or worker qualification.

## Durability, maintenance and recovery gates

Capture nonsecret evidence of `SHOW fsync`, `SHOW full_page_writes`,
`SHOW synchronous_commit`, `SHOW data_checksums`, `SELECT pg_is_in_recovery()`,
storage-cache/power-loss guarantees, and a test allocation acknowledged before
controlled PostgreSQL/node/storage interruptions. Compare all immutable allocation
IDs, limits, deadlines, epochs and outbox sequences afterward. Local Docker tests
below verify startup/privilege/TLS behavior; they prove no QNAP durability.

Independently retained backup/WAL evidence must cover every acknowledged debit;
nightly dumps are **not zero-loss recovery**. This initial design has no automatic
backup, failover or restore Job and archive mode is off. A separate reviewed ledger
backup policy can add continuous archival and tested restore, with its own
retention/inventory and exclusion from app restore. Until then, data loss or an
uncertain history keeps authority disabled; a nightly snapshot cannot re-enable
it. Backups may contain the expected witness identity in the authority row, but
must never contain or recreate the independently current external witness file.

For scheduled image/config changes, withdraw authority (`ready=false`), stop
allocators/router consumers, wait at least 20 seconds for cached authority to
expire, and verify streams are drained. The StatefulSet uses `OnDelete`; the
operator reviews/restarts its only Pod, checks recovery and synchronous settings,
then separately reviews reopening the gate. A crash restarts the same primary
and retained data; there is no asynchronous promotion. TLS renewals update the
projected files; after verifying new SANs/chain/modes, the operator runs
`SELECT pg_reload_conf()` through the local socket and verifies a new TLS handshake.

For router-only or simultaneous Relay/router restore, close the independent gate
and consumers first. Preserve allocations/outbox/tombstones forever; rotate the
recovery epoch and external witness deliberately without rewriting old allocation
epochs. The new witness goes to `af/relay-budget/witness`; consumers read it only
after a deliberate rollout (subPath). Run Relay's offline `recovery:fence`, check exact release/qualification
bindings, and review before reopening. Old prepaid worker identities remain
fenced. A Relay-only restore still runs `recovery:fence`; an intact independent
ledger rejects conflicting restored identities and grant regressions.

If the ledger is lost, rolled back or uncertain, keep every consumer disabled.
Do not reinitialize, promote an unqualified replica, reset budgets or restore the
old witness from a backup. Recover complete independently retained history or
perform a separately reviewed ceremony permanently fencing the old identity
domain. A PG_VERSION/schema marker only prevents empty reseeding; it cannot prove
that a nonempty restored volume is current. That is the external witness and
operator recovery boundary. Simultaneous rollback of ledger **and** independent
witness is outside automatic recovery.

## Local validation

Build both optional directories using the exact digest-pinned Kustomize image in
`scripts/manifest-lint.sh`, then validate them with that script's pinned
Kubeconform and default/CRD schema locations. The base reaches live Flux only
through the activation overlay, which `scripts/manifest-lint.sh` now builds with
every other Flux path; the `initialize/` build is never added to live Flux.
The focused script exercises the actual pinned PostgreSQL image using disposable
local volumes and the approved schema supplied explicitly:

```sh
bash scripts/tests/test-relay-budget-postgres.sh /private/operator/relay-budget-schema
```

It refuses empty/wrong-major/recovery/legacy startup and altered/missing schema
files, initializes only once, and starts with a root-owned 0440 TLS key. It checks
enforced durability, TLS/plaintext boundaries, role isolation, retained version-1
upgrade, authority refusal and partial-migration replay refusal, then removes all
test containers, volumes and synthetic certificates. It makes no Kubernetes calls.
Docker does not exercise kubelet fsGroup/CSI behavior: the operator must inspect
actual PVC ownership and PGDATA mode (0700 or 0750) after mounting. A recursive
fsGroup rewrite can make PGDATA group-writable and cause PostgreSQL refusal;
resolve mount ownership under the storage owner before running either Job.
