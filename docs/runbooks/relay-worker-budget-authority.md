# Independent Relay worker budget authority (opt-in)

This installs the independently retained PostgreSQL ledger required by Relay
worker allocation and llm-router's `worker-budget-authority` plugin. It is separate
from Relay PostgreSQL, router SQLite, `infra-pg`, OpenBao and their restore sets.
Its history must survive a simultaneous Relay/router restore. Lost or uncertain
ledger history denies inference; cleanup remains available.

`kubernetes/optional/relay-budget-authority/` is **not referenced by live Flux**.
The StatefulSet starts at zero replicas. Its separate initialization Job is
suspended and is not part of the default build. Merging this change installs
nothing, creates no credentials or policy, and enables no worker.

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
- Runtime pods receive separate allocation/reader DSNs only. They never receive
  the schema owner role, administrator credentials, Kubernetes API token, or
  database PVC. No SQL, app credentials, policy or recovery witness is embedded
  in these manifests.

The schema source is Relay's `deploy/worker-budget-ledger/001-authority.sql`
([initial review commit](https://git.chifor.me/cchifor/relay/src/commit/22dac084442cc18f180aba7b1fc08eafd7181afa/deploy/worker-budget-ledger/001-authority.sql)).
Use the final reviewer-merged commit, record its SHA256 in the operator evidence,
and apply it once. Do not copy it into ailab or run it as a Relay app migration.
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
   all Flux and backup selections. In a separate activation PR, exclude
   `relay-budget-authority` from **both** Velero daily/weekly schedules before
   installing it. This directory applies `velero.io/exclude-from-backup=true` to
   every resource/pod and to cert-manager's generated TLS Secret, and excludes the
   database volume, but these are additional guards. The operator must also label
   the dynamically provisioned backing PV with that exclusion and confirm no
   custom/cluster-wide backup selects it. Never add the ledger to the Relay
   logical dump, router restore, `infra-pg` dump, or generic app recovery job.
2. Render and inspect the optional base; apply it with StatefulSet replicas **0**.
   Wait for its retained PVC and Certificate. Record PVC UID, PV name/UID,
   QNAP LUN identity, Retain policy, image digest and issued certificate SANs.
   The backing PV is not created by this repo, so its exclusion is an operator
   gate. Do not delete or replace an existing claim to make initialization pass.
3. Obtain `001-authority.sql` from the final approved Relay commit into a private
   operator workspace; compare its recorded SHA256. Create an **immutable**
   ConfigMap `relay-budget-reviewed-schema-once` in the ledger namespace with
   exactly the key `001-authority.sql`, labelled
   `velero.io/exclude-from-backup=true`. This contains schema only, no credentials,
   authority row or witness. Render/apply `.../initialize/` separately; confirm
   `suspend=true`, `backoffLimit=0`, and no running database or initializer holds
   the claim. Explicitly unsuspend the Job once. It creates the empty database,
   applies the reviewed schema and creates the two logins below without passwords.
   Any failed/interrupted initialization requires owner investigation; do not
   erase markers/data or automatically rerun it.
4. After Job success and its Pod termination, delete the completed Job and its
   temporary schema/script ConfigMaps. Scale the StatefulSet to **one** replica
   in the operator's reviewed activation overlay. Never use more than one. No
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
   Keep the administrator socket inaccessible to app pods. The role memberships
   created by initialization are:

   | LOGIN | Granted NOLOGIN role | Purpose |
   | --- | --- | --- |
   | `relay_budget_allocator` | `relay_budget_client` | Relay prepare/inspect/ack/current_epoch |
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
   witness file. Generate the witness digest from fresh independent random bytes;
   it is an identity fence, not a password and not the SHA of a self-referential
   JSON file. Keep evidence of its deliberate issuance independently.
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
8. Prove TLS hostname verification, TLS-only login, different role permissions,
   current primary status, synchronous settings and checksum mode. Verify the
   current external witness, preserved allocation/outbox history, and independent
   durability/recovery evidence. Failure of any gate keeps `ready=false`.
   After independent review, set `worker_budget.authority.ready=true` explicitly.
   Authority readiness is an allocation gate; Kubernetes readiness only proves
   PostgreSQL is reachable and must never be treated as qualification.

Use a separate reviewed ESO/OpenBao delivery change for the two DSNs and public
CA, with no secret material in git. DSN shape is
`postgresql://<login>:<percent-encoded-password>@relay-budget-postgres.relay-budget-authority.svc.cluster.local:5432/relay_budget`
without query options. Application TLS verification cannot be disabled. Relay's
files are `RELAY_WORKER_BUDGET_DSN_FILE`, `_CA_FILE`, `_WITNESS_FILE`; router's
corresponding prefix is `ROUTER_WORKER_BUDGET`, plus
`ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST`. Witness/credential mounts must satisfy
the readers' ownership/mode checks: regular files, no hardlinks, symlinks or
group/world writes. Use read-only `subPath` files; changing the external witness
requires a deliberate consumer rollout. Do not add the witness to app ConfigMaps,
Secrets or other resources selected by app/ledger backup.

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
epochs. Run Relay's offline `recovery:fence`, check exact release/qualification
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
Kubeconform and default/CRD schema locations. Neither build is added to live Flux.
The focused script exercises the actual pinned PostgreSQL image using disposable
local volumes and the approved schema supplied explicitly:

```sh
bash scripts/tests/test-relay-budget-postgres.sh /path/to/reviewed/001-authority.sql
```

It refuses empty/wrong-major/recovery startup, initializes only once, starts with
a root-owned 0440 TLS key as projected by kubelet, checks enforced durability,
TLS/plaintext boundaries and role isolation, and removes all test containers,
volumes and synthetic certificates. It makes no Kubernetes calls.
