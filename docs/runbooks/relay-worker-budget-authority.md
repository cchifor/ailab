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
`003-scoped-requests.sql`, `004-preflight-errors.sql` and
`005-provenance-purpose.sql`, applied in that order. 005 adds the protected
provenance purpose and **replaces** `worker_budget.prepare()` and
`prepare_token_only()`, which Relay's ordinary path calls, so a ledger serving
that Relay needs all five. The initializer requires all five and writes marker
`5`; the server wrapper refuses every other marker. The earlier 001–004-only
initializer (marker `4`) is **retired**: no ailab ledger was initialized with it,
the wrapper refuses its marker, and there is no 4→5 upgrade. A version-1 database
needs the offline upgrade below. Do not vendor the SQL into ailab or run it as a
Relay app migration.

The source commit is one reviewed pin:
`RELAY_BUDGET_SOURCE_COMMIT` in
`kubernetes/optional/relay-budget-authority/initialize/relay-source.env`. It is
empty until the operator sets the full SHA of the reviewer-merged release commit
in a reviewed PR. The bundle renderer reads it, and it ships in the script
ConfigMap, so both standalone Jobs refuse a bundle whose `source-commit` differs
or a pin that is unset.

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
   name/UID, QNAP LUN identity, Retain policy, image digest and issued
   certificate SANs.
   The backing PV is not created by this repo, so its exclusion is an operator
   gate. Do not delete or replace an existing claim to make initialization pass.
3. Set `RELAY_BUDGET_SOURCE_COMMIT` in `initialize/relay-source.env` to the
   **reviewer-merged full Relay commit SHA** in a reviewed PR, and wait for it to
   merge. Then, from that merged ailab commit, export the five files using the
   local-only bundle renderer. It reads the pin and committed objects, not
   working-tree files, and refuses an unset pin or an existing output directory:

   ```sh
   python3 scripts/relay-budget-schema-bundle.py /path/to/relay \
     /private/operator/relay-budget-schema
   ```

   The receipt must say `"pinned": true` and `"schemaVersion": 5`.
   `--validation-commit` renders an unpinned bundle for local validation only;
   the Jobs refuse it. Review the source commit and every SQL change. Record the
   returned `manifestSha256` independently in the operator evidence and in
   `RELAY_BUDGET_SCHEMA_MANIFEST_SHA256` of a local copy of `initialize/job.yaml`.
   Do not derive this pin from an unreviewed live ConfigMap. The bundle includes
   source identity, ordered `SHA256SUMS`, five SQL files, and an **immutable**
   `configmap.json` named `relay-budget-reviewed-schema-once`, excluded from
   backup. It contains no credentials, authority row or witness. Apply that
   ConfigMap and render/apply the script-only `initialize/` Kustomization from the
   same merged ailab commit (it carries the pin).
   Create the reviewed standalone Job explicitly, verify the mounted bundle and
   pin, then unsuspend it once. The Job is outside all Flux inventories, has no
   automatic retry, and uses only a local Unix socket. Missing/changed files or
   a mismatched manifest or source pin refuse before creating the
   initialization-attempt marker.
   A failed attempt after that point requires owner investigation; do not delete
   the marker or data and retry. Successful initialization leaves both application
   passwords NULL and no authority/witness/qualification rows. Record version `5`,
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
   (`af/relay-budget/witness`, field `witness`), the owner-accepted witness store
   (External witness). Write the offline issuance record entry first, then the
   witness, by hand. Generate the witness digest from fresh independent random
   bytes; it is an identity fence, not a password and not the SHA of a
   self-referential JSON file.
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
   durability/recovery evidence (for the first `ready=true`: "Durability evidence
   for the first `ready=true`" below). Run the four witness drills (External witness §
   Drills) and archive their evidence. Failure of any gate keeps `ready=false`.
   After independent review, set `worker_budget.authority.ready=true` explicitly.
   Authority readiness is an allocation gate; Kubernetes readiness only proves
   PostgreSQL is reachable and must never be treated as qualification.
9. **Bootstrap gates (owner decision 2026-11-17).** Enable the router plugin only
   after these gates, each read back in the same window:
   - **Step 8 complete:** its proofs; drills A–D reviewed (External witness §
     Drills); the durability evidence accepted ("Durability evidence for the first
     `ready=true`" below); `worker_budget.authority.ready=true` read back.
   - **Router-side readbacks:** the Relay trust included (Router connection step 6:
     `relay-keys` active, the mounted trust equal to the rendered one);
     `ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST` equal to the `readers` row's
     `deployment_digest` (today
     `547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad`, Consumer
     wiring); and the `clients` and `readers` rows of step 7 (File A) unchanged.

   **File B, the `qualifications` rows, is not one of these gates.** The ledger
   refuses every allocation that lacks a `qualifications` row, covering the
   original deadline, for each of its policy digests: `prepare` raises `42501`
   `Worker budget qualification unavailable` and writes nothing (Relay
   `deploy/worker-budget-ledger/001-authority.sql:102-107`, replaced by the same
   check in `005-provenance-purpose.sql:110-115`'s `prepare_token_only`, which every
   allocating entry point reaches). An enabled plugin without File B therefore
   cannot allocate, and an `available` Router proves availability only (the gate,
   the witness and the reader mapping), never qualification. File B then becomes
   mandatory: committed, read back and qualified by the Router (H6's preflight,
   ailab #1753 comment 124071 §5) **before any candidate run is signed**
   (`relay-researcher-hub-upgrade.md` H7).

   Then enable it through the authenticated admin API:
   `POST /admin/v1/plugins/worker-budget-authority` with `{"enabled":true}`. Read
   back `GET /admin/v1/plugins` (state active) and the router log line
   `Worker budget authority available`. No manifest enables it.

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
comes from OpenBao. OpenBao is the **owner-accepted witness store**, a named
exception to "outside every backup selection". See "External witness" below for
the failure model, the offline issuance record, write ownership, the change
procedure and the drills.

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

Two components hold the wiring. Each
mounts its namespace's ESO Secret as three read-only `subPath` files under `/run/`
(never `/var/run`, a symlink), mode 0440 root:1000, and sets the variables above.
`scripts/tests/test-relay-budget-consumers-render.sh` proves each changes nothing
else in its Deployment.

- `kubernetes/components/llm-router-worker-budget-reader`: included by
  `kubernetes/apps/apps/llm-router/kustomization.yaml` since the first part of
  step 8 (ailab #1807, rolled 2026-11-16 17:16Z), after the router
  ExternalSecret was Ready (steps 5-6). The same PR replaced the placeholder
  `ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST` with the 64-hex digest of the reviewed
  Relay router deployment record: SHA-256 of canonical JSON of `{id, origin,
  issuer, baoOrigin, baoMount, baoPrefix}` (Relay `deploymentDigest`), the value of
  `readers.deployment_digest`. For the record in
  `kubernetes/components/relay-router-connection/connections.json` that is
  `547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad`
  (`docs/runbooks/relay-router-connection.md`, "Deployment digest"; recompute it
  before use). The activation test refuses the inclusion while the
  placeholder remains. Inclusion is a `Recreate` rollout of the router. The
  plugin stays disabled until step 9: the reader files are mounted, and nothing
  reads them until the plugin is enabled.
- `kubernetes/components/relay-worker-budget-client`: included alone by hub upgrade
  step H4a (`docs/runbooks/relay-researcher-hub-upgrade.md`; ailab #1805, rolled
  2026-11-16 17:33Z). The live Relay
  (v0.10.4) reads these variables, but starts the authority only with
  `RELAY_INFRASTRUCTURE_INSTALLATIONS`, a router connection
  (`RELAY_ROUTER_CONNECTIONS_FILE`, from the reviewed
  `kubernetes/components/relay-router-connection` that hub step H6 includes) and a
  profile source: the candidate or ordinary-canary file, or the pinned
  worker-profile registry, which needs the genuine release approval and so comes
  last (H7 and H4b). Until H7's candidate file, budget startup stays
  `start_failed`.

Including either before its Secret exists leaves the new Pod unable to mount the
volume. Both use `Recreate`, so that is an outage: check `kubectl get
externalsecret` Ready first.

## External witness: the owner-accepted OpenBao exception

The owner accepted OpenBao as the witness store on 2026-11-14, with conditions
([relay#277 comment 120994](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-120994),
decision in [comment 121330](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-121330)).
`af/relay-budget/witness` (field `witness`) is therefore a named, deliberate
exception to "outside all backup selections". OpenBao's raft snapshots and the
Velero copy of namespace `openbao` contain it. The ESO-rendered Secrets do not:
they stay labelled `velero.io/exclude-from-backup=true`.

**Failure model.** OpenBao may roll back; ledger history must not.
- **OpenBao rolls back alone.** The consumers get an older witness, which matches
  no `ready=true` authority row (`worker_budget.gate()` requires an exact match).
  The result is denial, an outage, never a grant.
- **Both roll back to the same era.** A grant can only be forged if the ledger
  and OpenBao go back together, which is why the ledger must never be rolled
  back. That covers QNAP/LUN snapshots, LUN clones, replication of the ledger's
  LUN, and any ledger backup or restore. Before step 6, the operator confirms on
  the QNAP that no snapshot schedule, clone or replication job covers the ledger
  LUN (its internal name is in the step-2 evidence), and records that check.
- **Changing that rule.** This exception must be reviewed again before any ledger
  backup, restore or failover design is introduced.

### Offline issuance record

Every witness ever issued (step 6, and every rotation) gets one entry in an
offline issuance record, written **before** the witness reaches OpenBao:
- the witness JSON (`version`, `instanceId`, `recoveryEpoch`, `witnessDigest`);
- the UTC issue date;
- the reason (initial issuance, rotation after `<restore or drill>`, …);
- the operator;
- the ailab commit in use.

The record holds no secret; the witness is public identity evidence. Keep it
**outside this repository and outside every snapshot and backup selection**: not
OpenBao, not the cluster, not a QNAP share or LUN that is snapshotted or
replicated. Recovery re-enters the witness **from this record only**, never from
the ledger's authority row (that is the value being checked) and never from an
OpenBao or Velero snapshot (that is what may have rolled back).

### Who writes it

Only the operator, by hand, during the ceremony, a drill or a recovery:
`bao kv put -mount=af relay-budget/witness witness=-` with the JSON on stdin.
- Relay and Router only read it. Policies `af-app-relay-budget` and
  `af-app-router-budget` grant `read` and nothing else, and the consumers mount
  the file read-only.
- No Job, CronJob, seed or provision script writes it.
  `relay-budget-provision-job.yaml` creates no KV document.
- There is no ESO `PushSecret` for it.
- `scripts/tests/test_relay_budget_witness_store.py` fails if any of this changes.

### Changing the witness

ESO refreshes the rendered Secrets hourly (`refreshInterval: 1h`), and each
consumer mounts `witness.json` as a `subPath` file, which never updates in a
running Pod. A new witness therefore reaches a consumer only through this
sequence:

1. Withdraw authority (`UPDATE worker_budget.authority SET ready=false`), and stop
   the consumers:
   - Router: `POST /admin/v1/plugins/worker-budget-authority {"enabled":false}`;
   - Relay: stop worker allocation.
2. Wait **at least 20 seconds**, so every cached snapshot (15 s) expires. Verify
   that streams have drained.
3. Add the offline record entry, then write the witness to OpenBao.
4. Force both ExternalSecrets to sync instead of waiting up to an hour:
   `kubectl annotate externalsecret <name> force-sync=$(date +%s) --overwrite`.
   - Do it in `relay` and in `llm-router`.
   - Verify the SHA-256 of each Secret's `witness.json` against the record.
5. Roll both consumers, which use `Recreate`.
6. Verify **both mounted** witnesses against the record:
   `sha256sum /run/*-worker-budget/witness.json` in each Pod.
7. Only then review reopening: `ready=true`, then the plugin.

The authority row's `recovery_epoch` and `witness_digest` change only as part of
a reviewed rotation. Never rewrite them to match a witness.

### After an OpenBao restore

After **any** raft snapshot restore or KV version rollback, follow
`docs/runbooks/openbao-recovery.md` § "After any restore: the Relay budget
witness" before anything reopens. In short:
- withdraw authority and stop consumers;
- compare the OpenBao witness, the live authority row and the latest offline
  record entry;
- rewrite and roll deliberately, using "Changing the witness".

### Drills before production `ready=true`

Run these after step 7's rows and step 8's proofs, in a drill window **before**
the production opening. That means no Relay router connection or credential
binding exists yet, so no allocation is possible whatever the gate says.
- **Why rotation is cheap here.** Rotating the epoch at this point fences no
  allocation, because none exists.
- **Relay.** The Relay consumer cannot be wired yet (see Consumer wiring). Its
  half of drills A–C runs when it is included, before Relay's first allocation.
- **Drills are not restores.** They inject witness states on purpose; § After
  an OpenBao restore does not apply inside a drill.

**Drill protocol.** Installing a witness ("Changing the witness") withdraws the
gate, so a refusal seen at that point proves nothing about the witness. Every
negative drill (A–C) therefore re-opens the gate for the drill alone and proves
it is open, at the moment of the observation:

1. **Install** the drill's witness state using "Changing the witness" steps
   1–2 and 4–6, **skipping step 3's record entry**. It ends withdrawn, with both
   consumers rolled onto the drill's file.
   - **Drill injections are never issuance entries.** That covers drill A's
     restored A, B's empty witness, C's malformed one and D's rewrite of B. Log
     each one in the drill captures only, never in the offline issuance record.
   - **What does go in the record.** Only a witness the authority row is rotated
     to (the initial A, drill A's rotation to B) is an issuance and gets a
     record entry.
   - **Why it matters.** The record's latest entry stays the real current
     witness, which step 5 below and the restore procedure re-enter from.
2. **Re-open for the drill only.** The owner sets `ready=true` and the operator
   enables the Router plugin. Allocation stays impossible: no Relay connection
   or binding exists.
3. **Prove the gate is open, immediately before observing.** Capture:
   - `SELECT ready, instance_id, recovery_epoch, witness_digest FROM worker_budget.authority`
     shows `ready = t`;
   - `GET /admin/v1/plugins` shows `worker-budget-authority` enabled;
   - a positive control: `worker_budget.current_epoch(<the record's CURRENT ids>)`
     as `relay_budget_router`, from the operator client Pod, returns the epoch.
4. **Observe the refusal** (the Pass column). It must name the drill's witness:
   `current_epoch(<the ids in the MOUNTED file>)` returns SQLSTATE 42501 while
   step 3's control still succeeds.
5. **Withdraw** (`ready=false`, plugin disabled). Restore the current witness
   from the record before the next drill.

| Drill | Do | Pass, with the gate open (protocol step 3) |
| --- | --- | --- |
| A, stale | Issue A (step 6). Rotate to B: owner updates the row's `recovery_epoch` and `witness_digest`, operator records and writes B ("Changing the witness"). Then restore A in OpenBao (`bao kv rollback -mount=af -version=<A> relay-budget/witness`), force-sync and roll | Router log `Worker budget authority unavailable: …`, still unavailable after waiting ≥20 s. `worker_budget.current_epoch(<A ids>)` as `relay_budget_router` from an operator client Pod returns SQLSTATE 42501. Relay, once wired: `worker_budget_ledger_unavailable`, allocation refused |
| B, missing | Write an empty `witness` (the only "missing" a running consumer can receive), force-sync and roll. Separately, soft-delete the document (`bao kv delete`) | Empty: Router unavailable, as in A; the positive control still succeeds. Soft-delete: the ExternalSecret goes not Ready while the Secret is retained (ESO Retain), and nothing changes until a deliberate roll |
| C, malformed | Write JSON that fails the schema (for example `{"version":1}`, or an uppercase digest), force-sync and roll | Router unavailable, as in A |
| D, older snapshot, unchanged witness | Write B again (a new KV version, same bytes; not a record entry). Roll back to the previous version, force-sync and roll; re-open as in protocol step 2 | What the window can show: the Router is `available`; the authority row is identical before and after; both mounted witness digests equal B and the record's latest entry. No allocation exists in this window, so the history comparison (allocations, `token_limit`, `original_deadline`, `recovery_epoch`, outbox sequences, receipts) runs when D is repeated with the Relay-wired drills, and after any real OpenBao restore |

- **Restoring B.** After A–C, restore B from the record and verify `available`
  before the next drill.
- **Not drilled.** Production OpenBao is not raft-restored for a drill: its blast
  radius is the whole estate. The KV version rollback exercises exactly the path
  the consumers read; a real raft restore follows the recovery step above.

**Evidence.**
- **Where.** Post each drill's non-secret readbacks as a comment on the ceremony
  thread (ailab #1753), titled "Witness drill A/B/C/D". Keep raw captures with
  the offline issuance record.
- **What a readback holds:**
  - the protocol step-3 captures, timestamped just before the observation: the
    authority row with `ready`, the plugin state and the positive control;
  - Router log lines;
  - the mounted witness SHA-256 in both Pods;
  - the SQL outputs and the before/after `SELECT` diffs.

  A refusal without step-3 captures from the same window does not count.
- **Never include** DSNs, passwords or admin tokens.

The production `ready=true` (step 8) waits for all four drills to be reviewed.

## Offline upgrade of a retained version-1 database

This is an operator-owned schema upgrade, not initialization or history recovery.
It applies only `002`–`005` and retains authority identity, debits, original
lifetimes, outbox requests and receipts. It never changes `ready=false` to true,
seeds request scopes, refunds allocations, rotates witnesses, or alters login
passwords. The server wrapper now requires marker `5`: coordinate the config
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
   It checks withdrawn authority and the absence of already applied
   `002`/`003`/`005` before creating the durable attempt marker. 005 itself
   refuses if a retained allocation ID already has its reserved `72776131-`
   prefix; that refusal comes after 002–004 committed, so it is the
   partial-migration case below. A refusal in those SQL checks
   cleanly stops PostgreSQL and leaves no migration-attempt marker; correct the
   prerequisite and review a fresh standalone Job. The preflight changes no ledger
   rows or schema, but normal PostgreSQL startup/shutdown still writes WAL and
   checkpoints. Once checks pass, it records
   the attempt, runs migrations in order, and writes marker `5` only after all
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
   recording version 5; the operator must investigate before retry or recovery.
4. Confirm the same allocation IDs, token ceilings, original deadlines, outbox
   operations, receipts, witness and withdrawn authority. Ordinary old allocations
   gain NULL request-scope and `purpose` columns; they must not become new
   allocations. Confirm
   client execution on `prepare_token_only` is revoked and the new mediating
   `prepare` entry point is available. Ordinary token-only allocations remain
   intentionally supported through that wrapper; signed candidate requests must
   supply both scope and request allowance. `clients.max_requests` remains NULL
   until the independent operator ceremony enables request-limited allocations.
   Do not manually set marker `5` as a repair.
5. Start the pinned server at version `5`, check TLS/durability and narrow roles,
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
below verify startup/privilege/TLS behavior; they prove no QNAP durability. For the
first production `ready=true`, "Durability evidence for the first `ready=true`"
below is the procedure.

Independently retained backup/WAL evidence must cover every acknowledged debit;
nightly dumps are **not zero-loss recovery**. This initial design has no automatic
backup, failover or restore Job and archive mode is off. A separate reviewed ledger
backup policy can add continuous archival and tested restore, with its own
retention/inventory and exclusion from app restore. Until then, data loss or an
uncertain history keeps authority disabled; a nightly snapshot cannot re-enable
it. Backups may contain the expected witness identity in the authority row. The
current external witness lives in OpenBao, whose snapshots do contain it; that is
the owner-accepted exception (External witness). It holds only because the
**ledger's history is never rolled back**: no QNAP/LUN snapshot, clone or
replication of the ledger's LUN, and no ledger restore. Re-review the exception
before any ledger backup or failover is introduced.

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
epochs. Issue the new witness through the offline record and "Changing the
witness" (OpenBao, forced ESO sync, deliberate consumer roll, both mounted
witnesses verified). Run Relay's offline `recovery:fence`, check exact
release/qualification bindings, and review before reopening. Old prepaid worker
identities remain fenced. A Relay-only restore still runs `recovery:fence`; an intact independent
ledger rejects conflicting restored identities and grant regressions.

If the ledger is lost, rolled back or uncertain, keep every consumer disabled.
Do not reinitialize, promote an unqualified replica, reset budgets or restore the
old witness from a backup. Recover complete independently retained history or
perform a separately reviewed ceremony permanently fencing the old identity
domain. A PG_VERSION/schema marker only prevents empty reseeding; it cannot prove
that a nonempty restored volume is current. That is the external witness and
operator recovery boundary. Simultaneous rollback of ledger **and** independent
witness is outside automatic recovery.

### Durability evidence for the first `ready=true` (owner decision 2026-11-17)

The gate above asks for a test allocation acknowledged before controlled
interruptions. On the production ledger none can exist before hub step H6: there is
no Relay router connection, so nothing allocates. The owner decided on 2026-11-17
that the first production `ready=true` rests on three pieces of evidence instead,
each posted on the ceremony thread (ailab #1753) and accepted there by the owner,
and that one confirmation on production follows R1:

| Part | What | Where |
| --- | --- | --- |
| (i) | the durability settings | production, read-only |
| (ii) | a measured probe: synthetic acknowledged allocations through controlled interruptions | a disposable second ledger |
| (iii) | a committed-row comparison across one planned restart | production, by the maintenance procedure above |
| after R1 | the same comparison over R1's allocation history | production, by the same procedure |

Checklist step 7 (`relay-researcher-remaining-live-steps.md`) does not run until
(i)-(iii) are accepted. Every command below is NEW (not in this runbook before the
decision) unless it names its source. They run in the session of the checklist's
step 0 (its `cap`, `lsql` and `lwrite`) with step 5's `plugin` and `rlog`, always
with `--context admin@ai`.

**Limit.** Nothing here cuts power to the QNAP. (ii) interrupts PostgreSQL, the
Kubernetes node and the iSCSI path, and relies on the NAS keeping every write it has
acknowledged. The NAS side is the "storage-cache/power-loss guarantees" item above:
the operator records what QuTS states for the ledger's LUN and pool
(`<operator fills: where QuTS shows the LUN's write-cache setting and the pool's
synchronous-write policy>`), and the owner accepts it, or not, with (i)-(iii).

**The SQL files.** Write them once into `"$ev"`. The two `ledger-*` files only read
and also serve production. The four `probe-*` files write, and run **only on the
disposable ledger**: `probe-allocate.sql` and `probe-write.sql` act as the allocator
through `SET SESSION AUTHORIZATION`, which the ledger accepts because it maps
`session_user` to the `clients` row. The probe therefore needs no password.

```sh
# One "name|value" line per field (records of a multi-row result are separated by a blank line).
cat > "$ev/ledger-identity.sql" <<'SQL'
\set ON_ERROR_STOP on
\set QUIET on
\pset format unaligned
\x on
SELECT system_identifier FROM pg_control_system();
SELECT pg_is_in_recovery() AS in_recovery;
SELECT pg_postmaster_start_time() AS postmaster_started;
SHOW fsync;
SHOW full_page_writes;
SHOW synchronous_commit;
SHOW data_checksums;
SELECT name, setting, source FROM pg_settings
 WHERE name IN ('fsync', 'full_page_writes', 'synchronous_commit', 'data_checksums', 'wal_sync_method', 'data_sync_retry')
 ORDER BY name;
SELECT md5(string_agg(p.oid::regprocedure::text || E'\n' || pg_get_functiondef(p.oid), E'\n' ORDER BY p.oid::regprocedure::text)) AS functions_md5,
       count(*) AS functions
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'worker_budget';
SELECT r.rolname, m.rolname AS member_of FROM pg_auth_members am
  JOIN pg_roles r ON r.oid = am.member JOIN pg_roles m ON m.oid = am.roleid
 WHERE r.rolname LIKE 'relay_budget_%' ORDER BY 1, 2;
SQL
# Every row of every worker_budget table, one tab-separated line each: table, key, the row as JSON.
cat > "$ev/ledger-rows.sql" <<'SQL'
\set ON_ERROR_STOP on
\set QUIET on
\pset tuples_only on
\pset format unaligned
\pset fieldsep '\t'
SELECT 'authority', 'singleton', to_jsonb(x)::text FROM worker_budget.authority x
UNION ALL SELECT 'clients', x.login::text, to_jsonb(x)::text FROM worker_budget.clients x
UNION ALL SELECT 'readers', x.login::text, to_jsonb(x)::text FROM worker_budget.readers x
UNION ALL SELECT 'qualifications', x.deployment_digest || '/' || x.digest, to_jsonb(x)::text FROM worker_budget.qualifications x
UNION ALL SELECT 'allocations', x.allocation_id::text, to_jsonb(x)::text FROM worker_budget.allocations x
UNION ALL SELECT 'outbox', x.allocation_id || '/' || x.sequence, to_jsonb(x)::text FROM worker_budget.outbox x
UNION ALL SELECT 'receipts', x.operation_id::text, to_jsonb(x)::text FROM worker_budget.receipts x
UNION ALL SELECT 'scope_runs', x.run_digest, to_jsonb(x)::text FROM worker_budget.scope_runs x
UNION ALL SELECT 'allocation_scopes', x.scope_digest, to_jsonb(x)::text FROM worker_budget.allocation_scopes x
ORDER BY 1, 2;
SQL
# Probe only. Throwaway identities, generated here and used nowhere else; ready=true from the start.
cat > "$ev/probe-seed.sql" <<'SQL'
\set ON_ERROR_STOP on
SELECT gen_random_uuid() AS instance, gen_random_uuid() AS epoch, gen_random_uuid() AS connection,
       gen_random_uuid() AS tenant, gen_random_uuid() AS scoped_worker, gen_random_uuid() AS scoped_grant,
       encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex') AS witness,
       encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex') AS deployment,
       encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex') AS policy,
       encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex') AS run,
       encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex') AS scope,
       floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint + 7 * 86400000 AS until_ms \gset
BEGIN;
SET LOCAL ROLE relay_budget_owner;
INSERT INTO worker_budget.authority (instance_id, recovery_epoch, witness_digest, ready)
  VALUES (:'instance', :'epoch', :'witness', true);
INSERT INTO worker_budget.clients
  (login, issuer, connection_id, deployment_digest, tenant_ids, max_tokens, max_authority_ms, max_requests)
  VALUES ('relay_budget_allocator', 'durability-probe.invalid', :'connection', :'deployment',
          ARRAY[:'tenant']::uuid[], 1000000, 300000, 8);
INSERT INTO worker_budget.readers (login, deployment_digest) VALUES ('relay_budget_router', :'deployment');
INSERT INTO worker_budget.qualifications (deployment_digest, digest, policy)
  VALUES (:'deployment', :'policy',
          jsonb_build_object('synthetic', 'durability probe, not a ReviewedTokenPolicy', 'qualificationExpiresAt', :until_ms));
INSERT INTO worker_budget.scope_runs
  (run_digest, run_document, client_login, tenant_id, worker_ids, token_cap, request_cap, expires_at, recovery_epoch)
  VALUES (:'run', '{"synthetic": "durability probe"}', 'relay_budget_allocator', :'tenant',
          ARRAY[:'scoped_worker']::uuid[], 1000, 8, :until_ms, :'epoch');
INSERT INTO worker_budget.allocation_scopes (scope_digest, run_digest, worker_id, grant_id, token_limit, request_limit)
  VALUES (:'scope', :'run', :'scoped_worker', :'scoped_grant', 1000, 8);
COMMIT;
SQL
# Probe only. The allocator's call sequence (prepare, then acknowledge with the returned operationId):
# an ordinary allocation, a scoped one (R1's form), and one prepared but never acknowledged.
cat > "$ev/probe-allocate.sql" <<'SQL'
\set ON_ERROR_STOP on
SELECT instance_id AS i, recovery_epoch AS e, witness_digest AS w FROM worker_budget.authority \gset
SELECT connection_id AS c, deployment_digest AS d, tenant_ids[1] AS t FROM worker_budget.clients \gset
SELECT digest AS q FROM worker_budget.qualifications \gset
SELECT s.worker_id AS sw, s.grant_id AS sg, s.scope_digest AS sd, s.request_limit AS sr, r.expires_at AS sx
  FROM worker_budget.allocation_scopes s JOIN worker_budget.scope_runs r USING (run_digest) \gset
SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms \gset
SET SESSION AUTHORIZATION relay_budget_allocator;
SELECT worker_budget.prepare(:'i', :'e', :'w', jsonb_build_object(
  'tenantId', :'t', 'workerId', gen_random_uuid(), 'connectionId', :'c', 'deploymentDigest', :'d',
  'limit', 1000, 'originalDeadline', :now_ms + 3600000, 'authorityExpiresAt', :now_ms + 300000,
  'grantId', gen_random_uuid(), 'grantRevision', 1,
  'grantDigest', encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex'),
  'tokenPolicyDigests', jsonb_build_array(:'q'))) AS r1 \gset
SELECT worker_budget.acknowledge(:'i', :'e', :'w', (:'r1'::jsonb ->> 'operationId')::uuid);
SELECT worker_budget.prepare(:'i', :'e', :'w', jsonb_build_object(
  'tenantId', :'t', 'workerId', :'sw', 'connectionId', :'c', 'deploymentDigest', :'d',
  'limit', 1000, 'requestLimit', :sr, 'allocationScopeDigest', :'sd',
  'originalDeadline', least(:now_ms + 3600000, :sx), 'authorityExpiresAt', :now_ms + 300000,
  'grantId', :'sg', 'grantRevision', 1,
  'grantDigest', encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex'),
  'tokenPolicyDigests', jsonb_build_array(:'q'))) AS r2 \gset
SELECT worker_budget.acknowledge(:'i', :'e', :'w', (:'r2'::jsonb ->> 'operationId')::uuid);
SELECT worker_budget.prepare(:'i', :'e', :'w', jsonb_build_object(
  'tenantId', :'t', 'workerId', gen_random_uuid(), 'connectionId', :'c', 'deploymentDigest', :'d',
  'limit', 1000, 'originalDeadline', :now_ms + 3600000, 'authorityExpiresAt', :now_ms + 300000,
  'grantId', gen_random_uuid(), 'grantRevision', 1,
  'grantDigest', encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex'),
  'tokenPolicyDigests', jsonb_build_array(:'q'))) AS r3 \gset
RESET SESSION AUTHORIZATION;
SELECT count(*) AS allocations, (SELECT count(*) FROM worker_budget.outbox) AS outbox,
       (SELECT count(*) FROM worker_budget.receipts) AS receipts,
       count(*) FILTER (WHERE request_limit IS NOT NULL) AS scoped
  FROM worker_budget.allocations;
SQL
# Probe only. One fresh ordinary allocation; each line is printed only after its commit returned,
# with the server's UTC time of that line.
cat > "$ev/probe-write.sql" <<'SQL'
\set ON_ERROR_STOP on
\set QUIET on
\pset tuples_only on
\pset format unaligned
SELECT instance_id AS i, recovery_epoch AS e, witness_digest AS w FROM worker_budget.authority \gset
SELECT connection_id AS c, deployment_digest AS d, tenant_ids[1] AS t FROM worker_budget.clients \gset
SELECT digest AS q FROM worker_budget.qualifications \gset
SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms \gset
SET SESSION AUTHORIZATION relay_budget_allocator;
SELECT worker_budget.prepare(:'i', :'e', :'w', jsonb_build_object(
  'tenantId', :'t', 'workerId', gen_random_uuid(), 'connectionId', :'c', 'deploymentDigest', :'d',
  'limit', 1000, 'originalDeadline', :now_ms + 3600000, 'authorityExpiresAt', :now_ms + 300000,
  'grantId', gen_random_uuid(), 'grantRevision', 1,
  'grantDigest', encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex'),
  'tokenPolicyDigests', jsonb_build_array(:'q'))) AS r \gset
SELECT 'PREPARED ' || (:'r'::jsonb #>> '{authorization,allocationId}') || ' ' || (:'r'::jsonb ->> 'operationId')
       || ' ' || to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
SELECT worker_budget.acknowledge(:'i', :'e', :'w', (:'r'::jsonb ->> 'operationId')::uuid);
SELECT 'ACKED ' || (:'r'::jsonb ->> 'operationId') || ' ' || to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
SQL
# Probe only. Needs the contrib amcheck extension, which the probe adds and production never gets.
cat > "$ev/probe-integrity.sql" <<'SQL'
\set ON_ERROR_STOP on
SELECT pg_is_in_recovery() AS in_recovery;
SELECT (SELECT count(*) FROM worker_budget.allocations a WHERE NOT EXISTS (
          SELECT 1 FROM worker_budget.outbox o WHERE o.allocation_id = a.allocation_id AND o.sequence = 1)) AS allocations_without_outbox,
       (SELECT count(*) FROM worker_budget.outbox o
         WHERE o.request ->> 'operationId' IS DISTINCT FROM o.operation_id::text
            OR o.request #>> '{authorization,allocationId}' IS DISTINCT FROM o.allocation_id::text) AS outbox_inconsistent,
       (SELECT count(*) FROM worker_budget.receipts r WHERE NOT EXISTS (
          SELECT 1 FROM worker_budget.outbox o WHERE o.operation_id = r.operation_id)) AS orphan_receipts;
SELECT c.relname AS index_checked, bt_index_check(c.oid, true) FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'worker_budget' ORDER BY 1;
SQL
```

These files were run against the pinned image and schema (Relay `d1c77f87`, 001-005,
initialized by `initialize.sh` and served by `start.sh` in local Docker, with a
writer loop and a `kill -9` of the checkpointer). `ledger-identity.sql` gave
`functions_md5` `e2780ee57aabbc6bee166ec76e154fda` over 15 functions; `source` is
`command line` for `fsync`, `full_page_writes` and `synchronous_commit`.

#### (i) The settings

The gate's `SHOW` and `pg_is_in_recovery()` reads, with the identity that (iii)
compares, over the local socket:

```sh
cap durability-settings lsql < "$ev/ledger-identity.sql"
```

Expected: `fsync`, `full_page_writes`, `synchronous_commit` and `data_checksums` all
`on`; `in_recovery` `f`; `source` `command line` for the first three (`start.sh`
sets them) and `data_checksums` on from `initdb --data-checksums`. Checklist step 4
captured the same values. Post the output on #1753 as "Durability evidence (i):
settings".

#### (ii) The measured probe on a disposable ledger

A second ledger built from the same reviewed base: the same image, `start.sh`,
`pg_hba.conf`, PostgreSQL flags and resources, the same pinned schema bundle (001-005,
marker `5`) through the same `initialize.sh`, on a new `qnap-iscsi` LUN.

**Its lifecycle.**
- **Namespace** `relay-budget-durability-probe`, created and deleted by the operator,
  outside Flux and git. Every object carries `velero.io/exclude-from-backup=true` and
  the Pod keeps the base's volume exclusion; its PV is labelled too. No backup of any
  kind, and no QNAP snapshot, clone or replication (the QNAP check made before step 6
  for the ledger's LUN, repeated for the probe's). Keep its life clear of the Velero start times (`0 2 * * *` daily,
  `0 3 * * 0` weekly, `apps/backup/velero/helmrelease.yaml`).
- **PVC** from `qnap-iscsi`, whose class reclaim is `Retain`. Right after it binds,
  its PV is patched to `Delete`, as `qnap-storage-setup.md` §9 does for a test PVC,
  so deleting the namespace removes the LUN.
- **Never a consumer's.** No ExternalSecret, no OpenBao document, no DSN, and no
  password: both logins stay NULL-password. Of the base's NetworkPolicies only
  `default-deny` stays, so nothing reaches the Service. The probe is driven through
  the local socket only.
- **Its witness** is the throwaway authority row that `probe-seed.sql` generates.
  No witness JSON is written anywhere: not to OpenBao, not to the offline issuance
  record (it is not an issuance), not to a consumer.
- **Its node** is chosen for tier (b) below and pinned for the probe's life. It is
  never the production ledger's node.
- **Deleted afterwards**, with its PV and LUN, the same day.

**Choosing the node (read-only).** Tier (b) resets the probe's node, so the probe is
pinned to the node with the least collateral from the start. Only the three agent
nodes qualify: `talos-agent-node-1..3`, vmid 4301-4303 on `ai-node1..3`
(`kubernetes/infra/agent-nodes/variables.tf`; the VM name is `talos-<key>`). Never a
control-plane node: it is an etcd member and carries Prometheus, Gitea, OpenBao and
the CNPG clusters. Never `relay-worker-1`: its binding forbids NAS storage classes
(`relay-worker-capacity.md`). The agent nodes carry, depending on scheduling, the
singletons `llm-router` (all estate inference, a `qnap-iscsi` volume), `relay` and
`relay-postgres` (`qnap-iscsi`), the production ledger, `llm-router-edge` (2
replicas), the researcher agent and the DaemonSets. Read where they are now:

```sh
kubectl --context admin@ai get nodes -L ailab.io/agent-pool -o wide
for n in talos-agent-node-1 talos-agent-node-2 talos-agent-node-3; do
  echo "== $n"
  kubectl --context admin@ai get pods -A -o wide --field-selector spec.nodeName="$n",status.phase=Running
  kubectl --context admin@ai get volumeattachments -o json \
    | jq -r --arg n "$n" '.items[] | select(.spec.nodeName == $n) | .spec.source.persistentVolumeName' \
    | while read -r v; do kubectl --context admin@ai get pv "$v" -o json \
        | jq -r '[.metadata.name, .spec.storageClassName, (.spec.claimRef // {} | "\(.namespace)/\(.name)")] | @tsv'; done
  kubectl --context admin@ai describe node "$n" | grep -A8 'Allocated resources'
done
```

Choose the agent node that runs none of `relay-budget-postgres-0`, `llm-router`,
`relay` and `relay-postgres`, with the fewest `qnap-iscsi` attachments and room for
the probe's requests (250m CPU, 1 Gi memory). If every agent node runs one of them,
tier (b) costs that workload's outage, and the owner decides whether to accept it or
skip (b). After a reset, the node's Pods restart on it once it is back.

**Build.** From the reviewer-merged ailab commit in use, in a private directory:

```sh
PNS=relay-budget-durability-probe
node='<operator fills: the agent node chosen above>'
vmid='<operator fills: its vmid, kubernetes/infra/agent-nodes/variables.tf>'
host='<operator fills: its Proxmox host address, the same file>'
nodeip='<operator fills: its InternalIP, kubectl get node "$node" -o wide>'
python scripts/node-ssh.py "$host" "qm config $vmid | grep -E '^name:'"   # name: $node, or stop
commit='<operator fills: the reviewer-merged ailab commit in use, full SHA>'
pd="$(mktemp -d)"
git -C '<operator fills: the ailab checkout>' archive "$commit" \
  kubernetes/optional/relay-budget-authority kubernetes/apps/relay-budget-authority | tar -x -C "$pd"
# Budget step 3's command, run in a checkout at "$commit". The receipt must say "pinned": true,
# "schemaVersion": 5 and the same manifestSha256 as the production initialization evidence.
python3 scripts/relay-budget-schema-bundle.py /path/to/relay "$pd/bundle"
manifest='<operator fills: the receipt manifestSha256>'
mkdir "$pd/probe"
cp "$pd/kubernetes/optional/relay-budget-authority/initialize/job.yaml" "$pd/probe/job.yaml"
cp "$pd/bundle/configmap.json" "$pd/probe/schema-configmap.json"
cat > "$pd/probe/issuer.yaml" <<'YAML'
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: durability-probe-selfsigned
spec:
  selfSigned: {}
YAML
cat > "$pd/probe/kustomization.yaml" <<YAML
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $PNS
resources:
- ../kubernetes/optional/relay-budget-authority
- ../kubernetes/optional/relay-budget-authority/initialize
- schema-configmap.json
- job.yaml
- issuer.yaml
labels:
- pairs:
    relay.chifor.me/durability-probe: "true"
    velero.io/exclude-from-backup: "true"
  includeSelectors: false
  includeTemplates: true
patches:
- target: {kind: NetworkPolicy, name: "postgres-clients|operator-client|relay-independent-budget|router-independent-budget"}
  patch: |-
    \$patch: delete
    apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata: {name: any}
- target: {kind: Certificate, name: relay-budget-postgres-tls}
  patch: |-
    - {op: replace, path: /spec/issuerRef, value: {name: durability-probe-selfsigned, kind: Issuer}}
    - {op: replace, path: /spec/dnsNames, value: [relay-budget-postgres.$PNS.svc, relay-budget-postgres.$PNS.svc.cluster.local]}
- target: {kind: StatefulSet, name: relay-budget-postgres}
  patch: |-
    - {op: add, path: /spec/template/spec/nodeSelector, value: {kubernetes.io/hostname: $node}}
- target: {kind: Job, name: relay-budget-initialize-once}
  patch: |-
    - {op: add, path: /spec/template/spec/nodeSelector, value: {kubernetes.io/hostname: $node}}
    - {op: test, path: /spec/template/spec/containers/0/env/1/name, value: RELAY_BUDGET_SCHEMA_MANIFEST_SHA256}
    - {op: replace, path: /spec/template/spec/containers/0/env/1/value, value: $manifest}
YAML
kubectl kustomize "$pd/probe" > "$pd/probe.yaml"
kubectl kustomize "$pd/kubernetes/apps/relay-budget-authority/ledger" > "$pd/prod.yaml"
sts() { yq 'select(.kind == "StatefulSet") | del(.metadata.namespace) | del(.metadata.labels["relay.chifor.me/durability-probe"])
  | del(.spec.template.metadata.labels["relay.chifor.me/durability-probe"]) | del(.spec.replicas) | del(.spec.template.spec.nodeSelector)' "$1"; }
cap probe-sts-diff diff <(sts "$pd/prod.yaml") <(sts "$pd/probe.yaml")   # no output: identical but for namespace, label, replicas, node
grep -E '^kind: |^  name: ' "$pd/probe.yaml"   # Namespace, 3 ConfigMaps, Service, PVC, StatefulSet, Job, Certificate, Issuer, NetworkPolicy default-deny
```

The render was checked with ailab's pinned kustomize (v5.4.3) and with `kubectl
kustomize` (v5.7.1): identical, and the StatefulSet differs from production's only in
those four places (its ConfigMap name, a content hash, is the same). `yq` is
mikefarah's v4.

**Create, initialize, start.** Budget steps 2-4, in the probe namespace:

```sh
kubectl --context admin@ai create -f "$pd/probe.yaml"
kubectl --context admin@ai -n "$PNS" wait --for=jsonpath='{.status.phase}'=Bound pvc/relay-budget-postgres-data --timeout=15m
kubectl --context admin@ai -n "$PNS" wait --for=condition=Ready certificate/relay-budget-postgres-tls --timeout=5m
pv=$(kubectl --context admin@ai -n "$PNS" get pvc relay-budget-postgres-data -o jsonpath='{.spec.volumeName}')
kubectl --context admin@ai patch pv "$pv" -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'
kubectl --context admin@ai label pv "$pv" velero.io/exclude-from-backup=true
pvinfo() { kubectl --context admin@ai get pv "$pv" -o json | jq -c '{name: .metadata.name,
  reclaim: .spec.persistentVolumeReclaimPolicy, class: .spec.storageClassName, handle: .spec.csi.volumeHandle}'; }
cap probe-pv pvinfo                                                 # reclaim Delete, class qnap-iscsi
kubectl --context admin@ai -n "$PNS" get job relay-budget-initialize-once -o json | jq -c '{suspend: .spec.suspend,
  image: .spec.template.spec.containers[0].image, node: .spec.template.spec.nodeSelector,
  pin: (.spec.template.spec.containers[0].env[] | select(.name == "RELAY_BUDGET_SCHEMA_MANIFEST_SHA256") | .value)}'
kubectl --context admin@ai -n "$PNS" patch job relay-budget-initialize-once --type=merge -p '{"spec":{"suspend":false}}'
kubectl --context admin@ai -n "$PNS" wait --for=condition=Complete job/relay-budget-initialize-once --timeout=10m
cap probe-init kubectl --context admin@ai -n "$PNS" logs job/relay-budget-initialize-once
kubectl --context admin@ai -n "$PNS" delete job relay-budget-initialize-once
kubectl --context admin@ai -n "$PNS" delete configmap relay-budget-reviewed-schema-once relay-budget-initialize
kubectl --context admin@ai -n "$PNS" scale statefulset relay-budget-postgres --replicas=1   # no Flux owns the probe
until kubectl --context admin@ai -n "$PNS" get pod relay-budget-postgres-0 >/dev/null 2>&1; do sleep 2; done
kubectl --context admin@ai -n "$PNS" wait --for=condition=Ready pod/relay-budget-postgres-0 --timeout=15m
```

Stop if the Job fails: delete the namespace (the probe is disposable) and investigate
before building another.

**Helpers, seed and the acknowledged allocations.**

```sh
mkdir -m 0700 "$ev/probe"
# lsql's form, in the probe namespace.
psql_probe() {
  kubectl --context admin@ai -n "$PNS" exec -i relay-budget-postgres-0 -- \
    env PSQL_HISTORY=/dev/null psql -X -v ON_ERROR_STOP=1 -h /var/run/postgresql -U postgres -d relay_budget -f -
}
# probe_rows <label>: every row, sorted bytewise for comm.
probe_rows() { local -; set -o pipefail; psql_probe < "$ev/ledger-rows.sql" | LC_ALL=C sort > "$ev/probe/rows-$1.tsv"; }
# probe_writer <tier> <seconds>: fresh allocations one after another, each prepared then acknowledged.
# Each call is bounded (timeout 120), so a call stuck on a reset node cannot hold the loop, and a
# short pause keeps a failing call (Pod down) from flooding the API server.
probe_writer() {
  local end=$(( $(date +%s) + $2 ))
  touch "$ev/probe/writer-$1.log" "$ev/probe/writer-$1.err"
  while [ "$(date +%s)" -lt "$end" ]; do
    timeout 120 kubectl --context admin@ai -n "$PNS" exec -i relay-budget-postgres-0 -- \
      env PSQL_HISTORY=/dev/null psql -X -v ON_ERROR_STOP=1 -h /var/run/postgresql -U postgres -d relay_budget -f - \
      < "$ev/probe-write.sql" 2>> "$ev/probe/writer-$1.err" | grep -E '^(PREPARED|ACKED) ' >> "$ev/probe/writer-$1.log"
    sleep 0.2
  done
}
# has <table> <key> <rows file>
has() { awk -F'\t' -v t="$1" -v k="$2" '$1 == t && $2 == k { f = 1 } END { exit !f }' "$3"; }
# probe_compare <tier>: every row from before survives unchanged; every write psql reported is present;
# writes were acknowledged both before and after the interruption ($ti, set just before it); and the
# writer met no SQL error (connection loss and recovery refusals are FATAL or psql errors, never ERROR:).
probe_compare() {
  local b="$ev/probe/rows-$1-before.tsv" a="$ev/probe/rows-$1-after.tsv" w="$ev/probe/writer-$1.log" e="$ev/probe/writer-$1.err"
  local miss=0 x y lost other before after sqlerr
  lost=$(LC_ALL=C comm -23 "$b" "$a" | wc -l)
  other=$(LC_ALL=C comm -13 "$b" "$a" | cut -f1 | grep -cvxE 'allocations|outbox|receipts')
  before=$(awk -v t="$ti" '$1 == "ACKED" && $3 < t' "$w" | wc -l)
  after=$(awk -v t="$ti" '$1 == "ACKED" && $3 > t' "$w" | wc -l)
  sqlerr=$(grep -c 'ERROR:' "$e")
  echo "lost or changed: $lost"
  echo "new rows by table (other tables: $other):"; LC_ALL=C comm -13 "$b" "$a" | cut -f1 | sort | uniq -c
  while read -r _ x y _; do
    { has allocations "$x" "$a" && has outbox "$x/1" "$a" && grep -qF "\"operation_id\": \"$y\"" "$a"; } \
      || { echo "MISSING prepared $x $y"; miss=$((miss + 1)); }
  done < <(grep '^PREPARED ' "$w")
  while read -r _ y _; do has receipts "$y" "$a" || { echo "MISSING acknowledged $y"; miss=$((miss + 1)); }; done < <(grep '^ACKED ' "$w")
  echo "prepared $(grep -c '^PREPARED ' "$w"), acknowledged $(grep -c '^ACKED ' "$w") ($before before $ti, $after after it), missing $miss"
  echo "writer errors, SQL ERROR lines: $sqlerr"; sort "$e" | uniq -c | sort -rn | head -5
  [ "$lost" -eq 0 ] && [ "$other" -eq 0 ] && [ "$miss" -eq 0 ] && [ "$before" -gt 0 ] && [ "$after" -gt 0 ] && [ "$sqlerr" -eq 0 ]
}
cap probe-amcheck psql_probe <<<'CREATE EXTENSION amcheck;'
cap probe-seed psql_probe < "$ev/probe-seed.sql"                    # ends COMMIT
cap probe-allocate psql_probe < "$ev/probe-allocate.sql"            # 3 | 3 | 2 | 1
cap probe-identity psql_probe < "$ev/ledger-identity.sql"           # as (i); functions_md5 equal to production's
cap probe-not-production psql_probe <<<'SELECT instance_id, recovery_epoch, witness_digest FROM worker_budget.authority;'
cap probe-integrity-0 psql_probe < "$ev/probe-integrity.sql"
```

`probe-not-production` must differ in all three IDs from production's authority row
(the checklist's step-0 readback).

**Each tier** follows one pattern: rows before, a writer running across the
interruption, the interruption, recovery, rows after, comparison, integrity. Each
tier is one `&&` chain: a failed capture stops it before the interruption, or
before the next capture. If a chain stops after `probe_tier`, run `wait "$wpid"` and
stop.

```sh
probe_tier() {   # probe_tier <tier> <writer seconds>: the baseline, then the writer in the background
  t0=$(date -u +%FT%TZ)
  probe_rows "$1-before" || { echo "STOP: no baseline for tier $1"; return 1; }
  probe_writer "$1" "$2" & wpid=$!
}
probe_after() {  # probe_after <tier>: fails at the first failed capture or comparison
  wait "$wpid"
  kubectl --context admin@ai -n "$PNS" wait --for=condition=Ready pod/relay-budget-postgres-0 --timeout=15m &&
  cap "probe-$1-log" kubectl --context admin@ai -n "$PNS" logs relay-budget-postgres-0 --since-time="$t0" &&
  cap "probe-$1-identity" psql_probe < "$ev/ledger-identity.sql" &&
  probe_rows "$1-after" &&
  cap "probe-$1-compare" probe_compare "$1" &&
  cap "probe-$1-integrity" psql_probe < "$ev/probe-integrity.sql"
}
```

**Expected after every tier.** `lost or changed: 0`. New rows only in
`allocations`, `outbox` and `receipts`. `missing 0`. Acknowledged writes both before
and after the interruption, and no SQL `ERROR` in the writer's errors. A write in flight at the
interruption may exist or not, never in part: `allocations_without_outbox`,
`outbox_inconsistent` and `orphan_receipts` are 0, and every `worker_budget` index
passes `bt_index_check`. `in_recovery` `f`. **Stop** on any other result, on
`PANIC`, `could not fsync`, `invalid page` or `checksum` in the log, or the probe not Ready within 15 minutes (and, for tiers
(a) and (b), no `database system is ready to accept connections` in the tier's
log): no further tier runs, and the result goes to the owner.

**Tier (a): `kill -9` of PostgreSQL (owner-approved).** The postmaster is PID 1 of
its container and ignores a SIGKILL sent from inside its own PID namespace (`kill -9
1` returns 0 and changes nothing). A SIGKILL of a server child such as the
checkpointer makes the postmaster end every process and run crash recovery from the
WAL, which is the path a killed postmaster takes on its next start:

```sh
probe_tier a 120 && sleep 20 && ti=$(date -u +%FT%TZ) &&
cap probe-a-kill kubectl --context admin@ai -n "$PNS" exec relay-budget-postgres-0 -- \
  sh -c 'pgrep -f "^postgres: checkpointer"; kill -9 "$(pgrep -f "^postgres: checkpointer")"' &&
probe_after a
```

The log shows, in order: `checkpointer process (PID n) was terminated by signal 9:
Killed`, `terminating any other active server processes`, `all server processes
terminated; reinitializing`, `database system was interrupted`, `database system was
not properly shut down; automatic recovery in progress`, `redo starts at`, `redo
done at`, `database system is ready to accept connections`. A local run of these
blocks (Docker, the pinned image and bundle, `kubectl` replaced by `docker`) gave 297
writes prepared and acknowledged across the kill (67 before it, 227 after),
`lost or changed: 0` and `missing 0`. An earlier run also had one write prepared and
never acknowledged, the one in flight: present, with its outbox row.

**Tier (b): hard reset of the probe's node. Runs only with the owner's explicit OK
on #1753 naming the node, its vmid and the window.** It is a `qm reset` of the
node's VM: no guest shutdown and no flush, so the guest loses whatever it had not
sent to the NAS, and every write the NAS acknowledged must survive. A reset of the
Proxmox host `ai-nodeN` is **not** proposed: the iSCSI initiator runs in the guest
and the host only routes its packets, so a host reset shows nothing more about the
ledger's data path, while it would also stop every other guest of that host: its
control-plane VM and, depending on the host, LLM or registry LXCs, CI runners, dev
workers and reviewer VMs. The VM reset is disruptive for everything on the node:
each Pod restarts after the reboot, each `qnap-iscsi` volume there is re-staged and
crash-recovered by its own workload, and a singleton on the node is down until then.

```sh
cap probe-b-collateral kubectl --context admin@ai get pods -A -o wide --field-selector spec.nodeName="$node" &&
probe_tier b 600 && sleep 20 && ti=$(date -u +%FT%TZ) &&
cap probe-b-reset python scripts/node-ssh.py "$host" "qm reset $vmid && date -u +%FT%TZ" &&
probe_after b
cap probe-b-node kubectl --context admin@ai get node "$node"
cap probe-b-collateral-after kubectl --context admin@ai get pods -A -o wide --field-selector spec.nodeName="$node"
```

Also expected: the probe's log has `database system was interrupted` and `automatic
recovery in progress`; every Pod of the `collateral` capture is Running again (a Job
Pod may be Completed).
**Stop** also when the node or the probe is not Ready within 15 minutes after the
writer ends: escalate to the owner, and do not force-delete Pods.

**Tier (c): an iSCSI path cut. Runs only with the owner's explicit OK on #1753
naming the form below, the node and the window.**

*Blast radius.* `qnap-iscsi` is the QNAP Trident CSI driver (`csi.trident.qnap.io`,
backend `qts`, driver `qnap-nas`, `kubernetes/apps/qnap-storage/`). Each PVC is its
own thin LUN, `trident-pvc-<uuid>`, mapped into an iSCSI target. A target can hold
more than one LUN: the retired test pool's golden clones shared one (ADR 0037). All
targets share one portal, `10.55.0.254:3260` (ADR 0011). The node logs in per target
when it stages a volume (`iscsiadm -m node -T <iqn> -p <portal>`,
`apps/infrastructure/storage/iscsi-recovery-tmo.yaml`), so it holds one session per
target it uses (six on `talos-cp1` on 2026-08-30), all over one path: the node's /32
route to `10.55.0.254` through its Proxmox host. Nothing else on a Talos node
uses iSCSI; the other classes are `nfs-csi` (the same address, TCP 2049) and
`local-path`. A session waits `recovery_tmo` before failing its device: 120 s once
the reconciler has corrected it, 2 s for a session staged within its last 30 s poll
(the driver bug that file describes). After that, ext4 stays mounted over a dead
device, every read is EIO, and recovery needs a manual Pod delete and an iSCSI
logout.
- **Node-wide** is any cut on the network path (host firewall, route, link): it
  stalls every `qnap-iscsi` volume attached to that node, and kills any whose
  session timeout is shorter than the cut. Limiting it to TCP 3260 spares the node's
  NFS mounts. The node's storage-fabric probe fails meanwhile;
  `StorageFabricUnreachable` fires only after 2 minutes.
- **Per-target** is possible only on the NAS: acting on the probe's target alone
  confines the I/O effect to the LUNs mapped into that target, which is the probe's
  LUN alone only if the NAS's mapping shows no other LUN there. No ailab runbook
  documents that action or Trident's reaction to it, and NAS-side mapping changes
  have gone wrong here: the 2026-08-30 incident left LUNs unmapped from their targets
  (`apps/backup/velero/helmrelease.yaml`), and one stale Trident transaction stops
  every `qnap-iscsi` provision and attach estate-wide until removed
  (`qnap-storage-setup.md` §9). Its blast radius is that target's LUNs for I/O, and
  Trident's control plane if it goes wrong.

Read the node's sessions first (read-only; the reconciler Pod mounts the node's
`/sys`):

```sh
tmo=$(kubectl --context admin@ai -n trident get pod -l app.kubernetes.io/name=iscsi-recovery-tmo \
  --field-selector spec.nodeName="$node" -o jsonpath='{.items[0].metadata.name}')
sessions() { kubectl --context admin@ai -n trident exec "$tmo" -- sh -c '
  for s in /host/sys/class/iscsi_session/session*; do
    echo "$(basename "$s") $(cat "$s/targetname") $(cat "$s/state") recovery_tmo=$(cat "$s/recovery_tmo")"; done
  cat /host/sys/class/scsi_device/*/device/state | sort | uniq -c'; }
# sessions_ok: prints them; fails unless every session is LOGGED_IN at recovery_tmo=120 and every device running.
sessions_ok() {
  local out; out=$(sessions) || return 1; printf '%s\n' "$out"
  printf '%s\n' "$out" | grep -q '^session' &&
  ! printf '%s\n' "$out" | grep '^session' | grep -qv ' LOGGED_IN recovery_tmo=120$' &&
  ! printf '%s\n' "$out" | grep -v '^session' | grep -qvE '^ *[0-9]+ running$'
}
# sessions_cut [<target IQN>]: prints them; fails while that target's session (default: any) is LOGGED_IN.
sessions_cut() {
  local out; out=$(sessions) || return 1; printf '%s\n' "$out"
  printf '%s\n' "$out" | grep -q "^session[0-9]* ${1:-}" &&
  ! printf '%s\n' "$out" | grep "^session[0-9]* ${1:-}" | grep -q ' LOGGED_IN '
}
```

Both forms start only when `sessions_ok` passes, and are judged with
`sessions_cut` during the cut (with the probe's target IQN for the per-target form).
The NAS's target and LUN lists map each session's `targetname` to its
LUNs (`<operator fills: the read-only qcli target and LUN listing, as
qnap-storage-setup.md §9 reads the LUN list>`); keep that mapping in the evidence.

The per-target form: `<operator fills: the QuTS action that disables only the
probe's target, and its reversal, named in the owner's OK>`. Go only if the mapping
shows the probe's LUN, `trident-<the probe's PV name>`, as the **only** LUN of that
target.

The node-wide form, TCP 3260 only, on the host that routes the node, 60 s (half the
corrected timeout). The whole cut runs in **one detached host process**, marked with
this cut's own token: it inserts the rule, waits 60 s, then retries the deletion
until `iptables -C` reports the rule absent (exit 1). No insertion can happen without
the removal that follows it in the same process, whatever happens to the session or
to the timing. The foreground only starts that process and reads whether the rule
is in place; if it reads `NOT-CUT`, the process still removes any rule it inserts.

```sh
rule="FORWARD -s $nodeip/32 -d 10.55.0.254/32 -p tcp --dport 3260 -j DROP"
cut="iptables -w 5 -I $rule || exit 1; sleep 60; while :; do iptables -w 5 -D $rule 2>/dev/null; iptables -w 5 -C $rule 2>/dev/null; [ \$? -eq 1 ] && break; sleep 1; done"
check="iptables -w 5 -C $rule 2>/dev/null; rc=\$?; case \$rc in 0) echo STILL-PRESENT; exit 2;; 1) echo removed;; *) echo CHECK-FAILED rc=\$rc; exit 3;; esac"
tok="probe-cut-$(date -u +%Y%m%dT%H%M%SZ)"
cap probe-c-sessions-before sessions_ok &&
probe_tier c 300 && sleep 20 && ti=$(date -u +%FT%TZ) &&
cap probe-c-cut python scripts/node-ssh.py "$host" "nohup sh -c ': $tok; $cut' </dev/null >/dev/null 2>&1 &
  sleep 2; iptables -w 5 -C $rule 2>/dev/null || { echo NOT-CUT; exit 1; }
  pgrep -f '^sh -c : $tok' >/dev/null || { echo NO-REMOVER; exit 1; }
  echo cut-in-place; date -u +%FT%TZ" &&
{ sleep 30; cap probe-c-sessions-during sessions_cut; held=$?             # the rule check runs either way
  sleep 60; cap probe-c-rule python scripts/node-ssh.py "$host" "$check" && [ "$held" -eq 0 ]; } &&
probe_after c &&
cap probe-c-sessions-after sessions_ok
```

Also expected: during the cut the node's sessions are not `LOGGED_IN` (the cut
holds; `sessions_cut` fails the chain otherwise, since the tier then proves nothing);
the writer stalls and resumes; PostgreSQL does not restart (`postmaster_started` equal to the previous
tier's after-identity, and no recovery lines); afterwards every
session is `LOGGED_IN` and every device `running`. **Stop** also on `NOT-CUT` (the
rule was not in place 2 s after the start; the tier proves nothing, and the process
removes any rule it still inserts: read `$check` after 70 s), and on `NO-REMOVER`,
`STILL-PRESENT` or `CHECK-FAILED` (remove the rule at once with `iptables -w 5 -D
$rule` on the host and read `$check` again until it says `removed`), or on any
session or device in another state: escalate to the owner with the list of volumes
on the node.

**Teardown, the same day.**

```sh
kubectl --context admin@ai delete namespace "$PNS" --timeout=30m
kubectl --context admin@ai get pv "$pv"        # NotFound once Trident has deleted the LUN (minutes per sweep)
```

Then confirm on the NAS that LUN `trident-<pv>` is gone, as `qnap-storage-setup.md`
§9 reads the LUN list (`<operator fills: the read-only qcli LUN listing used
there>`). If the PV or the LUN is still there after 30 minutes, stop and hand it to
the storage owner (Trident's logs, the LUN list). That section's NAS-first cleanup
is for PVs set to `Retain` with no VolumeAttachment left; it applies only after the
storage owner has established both, never while Trident may still delete the LUN.

**Evidence on #1753**, one comment titled "Durability evidence (ii): disposable
ledger probe", no secret (the probe has none):
- the ailab commit, the bundle receipt, the `probe-sts-diff` result and the render's
  object list;
- the node, its collateral readback, the owner's OK for (b) and (c) where run;
- `probe-pv`, `probe-identity` and `probe-not-production`;
- per tier: the interruption's command and UTC time, the PostgreSQL log lines,
  `probe_compare`'s output, the integrity output and, for (b) and (c), the
  after-captures;
- the teardown: namespace, PV and LUN gone.

#### (iii) One planned restart of the production ledger

The maintenance procedure above, once, with the committed rows compared across it.
Run it after the drills (checklist step 5), in checklist step 6a before 6b's review,
in the state the drills leave: `ready=f` with B's IDs, the plugin disabled, no Relay
router connection.

```sh
markers() { kubectl --context admin@ai -n relay-budget-authority exec relay-budget-postgres-0 -- sh -c \
  'cd "$PGDATA" && cat PG_VERSION relay-budget-schema-version relay-budget-schema-source-commit &&
   sha256sum relay-budget-schema-SHA256SUMS relay-budget-schema-source-commit && ls -d ../.initialization-started'; }
ledger_capture() {   # ledger_capture <label>
  local -; set -o pipefail
  lsql < "$ev/ledger-identity.sql" > "$ev/prod-identity-$1.txt" &&
  lsql < "$ev/ledger-rows.sql" | LC_ALL=C sort > "$ev/prod-rows-$1.tsv" &&
  markers > "$ev/prod-markers-$1.txt" &&
  kubectl --context admin@ai -n relay-budget-authority get pod relay-budget-postgres-0 -o json \
    | jq -c '{uid: .metadata.uid, node: .spec.nodeName}' > "$ev/prod-pod-$1.json"
}
# The maintenance procedure's preconditions, asserted: one authority row, withdrawn; the plugin disabled.
withdrawn() { lsql <<<"SELECT 'WITHDRAWN' FROM worker_budget.authority HAVING count(*) = 1 AND bool_and(NOT ready);" | grep -q WITHDRAWN; }
plugin_off() { plugins | jq -e 'select(.id == "worker-budget-authority") | .state == "disabled"' >/dev/null; }
prod_compare() {    # prod_compare <before> <after>: fails unless everything but the start time is identical
  cmp "$ev/prod-rows-$1.tsv" "$ev/prod-rows-$2.tsv" && echo "rows identical" &&
  cmp "$ev/prod-markers-$1.txt" "$ev/prod-markers-$2.txt" && echo "markers identical" &&
  diff <(grep -v '^postmaster_started|' "$ev/prod-identity-$1.txt") \
       <(grep -v '^postmaster_started|' "$ev/prod-identity-$2.txt") && echo "identity identical" &&
  grep -h '^postmaster_started|' "$ev/prod-identity-$1.txt" "$ev/prod-identity-$2.txt" &&
  cat "$ev/prod-pod-$1.json" "$ev/prod-pod-$2.json"
}
withdrawn && plugin_off && sleep 20 &&
cap prod-restart-rlog rlog &&                                # no budget activity since the withdrawal
ledger_capture before &&
cap prod-restart-delete kubectl --context admin@ai -n relay-budget-authority delete pod relay-budget-postgres-0 --timeout=5m &&
{ until kubectl --context admin@ai -n relay-budget-authority get pod relay-budget-postgres-0 >/dev/null 2>&1; do sleep 2; done; } &&
kubectl --context admin@ai -n relay-budget-authority wait --for=condition=Ready pod/relay-budget-postgres-0 --timeout=15m &&
ledger_capture after &&
cap prod-restart-log kubectl --context admin@ai -n relay-budget-authority logs relay-budget-postgres-0 &&
cap prod-restart-compare prod_compare before after
```

The `OnDelete` StatefulSet recreates its only Pod; the `preStop` hook stops
PostgreSQL cleanly first. Expected:
- the rows (authority, `clients`, `readers` and every other table) and the markers
  (`17`, `5`, the source commit, the SHA-256 of the copied `SHA256SUMS` equal to the
  manifest pin of budget step 3) are byte-identical;
- `identity identical` apart from `postmaster_started`, which is later after the
  restart: `system_identifier`, the settings, `functions_md5` and the role memberships
  are unchanged, `in_recovery` `f`;
- a new Pod UID (the node may differ; a move re-attaches the LUN, minutes per
  Trident sweep);
- the log has `database system was shut down at` (a clean shutdown) and `database
  system is ready to accept connections`, and no `interrupted`.

**Stop** on any other difference: `ready` stays false and the owner investigates.
Reopening stays the separately reviewed checklist step 7. Post the captures on #1753
as "Durability evidence (iii): production restart".

#### After R1 settles: the confirmation on production

R1 makes the first real allocation. After R1 has settled (campaign §3.10: worker
removed, journal slot settled, run withdrawn) and before R2 is signed, repeat (iii)
by the same maintenance procedure: `lwrite withdraw`, `plugin false`, Relay
allocating nothing (no active worker, no next run signed); then the chain of (iii)
with the labels `r1-before` and `r1-after`: `withdrawn && plugin_off && sleep 20`,
`rlog`, `ledger_capture r1-before`, the same Pod restart, `ledger_capture r1-after`
and `prod_compare r1-before r1-after`. The rows now hold R1's `allocations`, `outbox`
sequences, `receipts`, `scope_runs` and `allocation_scopes`, and must be
byte-identical across the restart, with the same markers, identity and log
expectations as (iii). Reopening is separately reviewed. Post it on #1753 as
"Post-R1 ledger confirmation".

**If it fails, the ledger is uncertain** ("If the ledger is lost, rolled back or
uncertain" above). Every consumer stays disabled: authority withdrawn, plugin
disabled, Relay allocating nothing. The cost, stated in advance:
- the campaign stops after R1, with nothing reopened on this ledger;
- there is no restore: the ledger has no backup and archive mode is off, and no
  snapshot may be used;
- the only way forward is the separately reviewed ceremony that permanently fences
  the old identity domain. No runbook writes it yet, so it needs its own design and
  review first;
- R1's allocation, and every other allocation of that domain, stays fenced forever;
- (i)-(iii) no longer hold for this ledger: what replaces them before any later
  `ready=true` is the owner's decision, with that ceremony.

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

Before the pin is set, render that bundle with `--validation-commit`. The harness
pins the supplied bundle's own commit over `relay-source.env` inside its
containers, and separately proves that the committed pin (while unset) and a
different pin both refuse before any storage change.
It refuses empty/wrong-major/recovery/legacy/retired-marker-4 startup and
altered/missing schema files, initializes 001–005 only once, and starts with a
root-owned 0440 TLS key. It checks enforced durability, TLS/plaintext boundaries,
role isolation, the 005 entry points and grants, retained version-1 upgrade to 5,
authority refusal and partial-migration replay refusal, then removes all
test containers, volumes and synthetic certificates. It makes no Kubernetes calls.
Docker does not exercise kubelet fsGroup/CSI behavior: the operator must inspect
actual PVC ownership and PGDATA mode (0700 or 0750) after mounting. A recursive
fsGroup rewrite can make PGDATA group-writable and cause PostgreSQL refusal;
resolve mount ownership under the storage owner before running either Job.
