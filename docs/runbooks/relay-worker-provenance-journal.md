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
`011-runtime-presence-current-readback.sql`. SQL 006 supplies the signed presence ledger;
SQL 007 repairs the open-execution versus closed-cleanup boundary. SQL 008 repairs
lookup expiry after lock waits and exact backing replay for already installed
databases; updated SQL 001 also contains those fixes for fresh installations.
SQL 009 adds the prepared Pod spec commitment for new original Pod effects and
does not rewrite existing immutable effects. SQL 010 requires fresh READ COMMITTED
snapshots for both runtime-presence activation and admission latching. SQL 011
adds a restricted, locked current-read envelope with explicit policy/admission
time bounds. These
migrations do not install a Node signer, independent witness or execution fence.
Review and record all eleven
SHA256 values and the source commit. SQL 010 and 011 are fixed reviewed
artifacts: only the digests shown below are accepted, and the renderer refuses
any other schema010 or schema011 pin before producing the Job. The initializer renderer input is a single
root or operator-owned, single-link, non-group-writable regular JSON file:

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
  "schema008": {"path": "/absolute/008-authority-completion.sql", "sha256": "<64 hex>"},
  "schema009": {"path": "/absolute/009-original-pod-spec-commitment.sql", "sha256": "<64 hex>"},
  "schema010": {"path": "/absolute/010-runtime-presence-latch-isolation.sql", "sha256": "86fd1612f0d9aac62902825d7e4fb290ac6c2c030f89d3e4a764a23230a125e1"},
  "schema011": {"path": "/absolute/011-runtime-presence-current-readback.sql", "sha256": "094e6639b07d497401f86ab6af5ccd14813238d2beb1d538bcd1ac7f105caebd"}
}
```

Render with `python3 -B kubernetes/optional/relay-provenance-journal/render.py
initialize /absolute/initializer.json <initializer-json-sha256>`. JSON output
is a Kubernetes list. The Job verifies all eleven SQL files before touching
PGDATA, refuses any existing PGDATA, creates a fresh UTF8 `relay_provenance`
database, applies each transaction in order, checks empty authority/history,
cleanly stops PostgreSQL, and only then writes schema marker 11. An interrupted
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
reapply 007 or require empty presence history because 008 preserves all data and grants.
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
the new schema-11 startup guard installed while replicas remain zero, so an
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
checks 007 prerequisites without clearing history. Every upgrade checks the
exact SHA256, owner, security-definer flag and return type of both authority
functions; 008 repeats this check under exclusive locks. Known incompatible
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
Pod on the target before activation. The corrected-source schema 5→8, 6→8,
7→8 and 008 replay scenarios also passed against candidate Relay source
`ba496c1a2e3601949c853382c102cabc19e52c3e`; each reported repaired digests
`transition=96f5fbc1440e80e791c628f08812790c3a1cb5255e7bd6efac8e0fba88472eb7`
and `lookup=1e1314f11045c5f53603dc1da1a0a66f224d215d0e365c811bcecf71a6d2eef9`.
These candidate-source runs are not merged-source or deployment qualification.

### Authority function digest generation

The `previous_digest` and `repaired_digest` values in `upgrade.sh` are SHA-256
hashes of PostgreSQL's stored `pg_proc.prosrc` text, encoded as UTF-8. Generate
them against the exact Relay source pin that will be rendered, after that source
has merged. Until the source merge is complete, `ba496c1a` is only the candidate
pin used for local validation; do not render a live upgrade from it. The pin
must include both `deploy/worker-provenance-journal/001-provenance.sql` and
`008-authority-completion.sql`.

In a disposable PostgreSQL 17 database, apply the selected 001 SQL and run the
query below. For `previous_digest`, select 001 from the reviewed historical
Relay source `2aae6c149a8ee092a10dacc9e9191bb21c8a3196`. For
`repaired_digest`, select 001 from the final merged Relay source commit. Then
apply 008 to a disposable schema-7 database and run the query again to confirm
the installed post-migration bodies. The upgrade's `source_ok` preflight and
SQL 008 both require these exact values, owner, security-definer flag and
return type.

```sh
git -C "$RELAY_REPO" show "$RELAY_SOURCE_COMMIT:deploy/worker-provenance-journal/001-provenance.sql" > /tmp/001-provenance.sql
psql -X -v ON_ERROR_STOP=1 -d relay_provenance -f /tmp/001-provenance.sql
psql -X -At -v ON_ERROR_STOP=1 -d relay_provenance <<'SQL'
SELECT p.proname,
       encode(sha256(convert_to(p.prosrc, 'UTF8')), 'hex') AS prosrc_sha256
FROM pg_proc AS p
WHERE p.oid IN (
  to_regprocedure('worker_provenance.transition(text,text,text)'),
  to_regprocedure('worker_provenance.lookup(text,text,text)')
)
ORDER BY p.proname;
SQL
```

Run that block once with `RELAY_SOURCE_COMMIT` set to the historical source
commit and once with it set to the final merged source commit. For the
post-008 confirmation, apply the pinned 008 file to a complete schema-7 scratch
database before running the query; do not apply only 008 to a database that
lacks migrations 002–007.

Record the exact full Relay source commit and both query results in the review
evidence. Check the final merged commit again before rendering the upgrade
bundle or creating a live maintenance Job; if 001 or 008 changed, regenerate
and update both the previous and repaired digest columns first.

## Upgrade a retained schema 8 database to schema 9

This is a separate one-shot stage. Keep the existing schema 5/6/7 to schema 8
upgrade unchanged; do not offer a direct 5/6/7 to 9 path. After that upgrade
finishes, keep every consumer stopped and do not start the schema 8 runtime
between stages. Complete both the separate 9→10 and 10→11 stages before
restarting; the current runtime startup guard accepts only marker 11, so the
database cannot start at marker 8, 9, or 10.

Before rendering, use the final merged Relay source pin that contains SQL 009
and recheck both installed-function hashes against that exact source. The
current Relay #352 candidate is `264b1265502900558628bbd1e39379971faa8e31`;
it is not a merged or deployable pin. Recompute the SQL 009 file SHA-256 and
the `transition`/`lookup` `prosrc` hashes from the final merged source before
producing the renderer input or a maintenance Job.

Pause new original Pod creation, stop the cleanup listeners and database, and
verify every other consumer of the PVC is gone. Resolve all possibly-issued
Pod-create requests under the existing uncertainty rules. Drain every
`pod-create` effect in `reserved` or `armed` state and every admission with a
non-null execution and `closed = false`. The renderer produces a suspended
Job with `backoffLimit: 0`; its read-only preflight refuses these conditions
before creating the persistent latch, and SQL 009 checks them again in its
transaction. It also requires the exact post-008 authority function bodies
and the pre-009 reserve schema.

Use a renderer input with only the schema 009 pin and `fromVersion: 8`:

```json
{
  "version": 1,
  "relaySourceCommit": "<final reviewed 40-hex Relay commit>",
  "fromVersion": 8,
  "schema009": {"path": "/absolute/009-original-pod-spec-commitment.sql", "sha256": "<64 hex>"}
}
```

```sh
python3 -B kubernetes/optional/relay-provenance-journal/render.py \
  upgrade9 /absolute/upgrade9-render.json <upgrade9-render-json-sha256>
```

The Job verifies the SQL pin, schema marker 8, PostgreSQL 17, UID 70, retained
directory mode, and absence of a postmaster, recovery signal, version temp file
or existing latch. It runs only SQL 009 and publishes marker 9 after the SQL
transaction commits and PostgreSQL stops cleanly. SQL 009 is not replay-safe:
never retry it automatically after an uncertain commit. Preserve the latch and
old marker and require attended inspection and recovery. Existing closed and
observed documents keep their exact bytes and remain inspectable; without the
new commitment, they cannot establish live original Pod presence.

Disposable PostgreSQL 17 checks passed for fresh 001–009 initialization, schema
8→9 with closed legacy history preserved, and refusal of an open execution,
reserved and armed Pod effects, SQL 009 already applied under marker 8, and a
pending upgrade latch. These local runs used Relay #348 candidate
`04e412f241c12c0d4a99a3aca7d2bb18f668274e` for SQL 001–008 and Relay #352
candidate `264b1265502900558628bbd1e39379971faa8e31` for SQL 009. They do not
qualify a merged source pin or a live maintenance Job.

Any interruption leaves the pending latch or old marker in place. The runtime
and upgrade Job both refuse that state; do not clear the latch, rerun migrations,
or change the version marker to manufacture success. Have the operator inspect
which transactions committed and follow the reviewed recovery procedure. SQL 007
also refuses inconsistent phase data instead of reopening a closed admission.
After a successful Job is terminal and its Pod is gone, verify the exact schema
and continue through 9→10 and 10→11 before an attended database/listener
restart with the compatible source image. Do not restart at the intermediate
marker 8, 9, or 10; the runtime startup guard accepts only marker 11.
All committed manifests remain optional and inactive.

## Upgrade a retained schema 9 database to schema 10

SQL 010 repairs both sides of runtime-presence activation serialization. A
REPEATABLE READ or SERIALIZABLE transaction could retain a stale snapshot while
waiting for the policy row lock; both activation and admission latching now
require READ COMMITTED. This migration replaces only two function bodies. It
preserves admissions, effects, presence evidence, source authority and grants.

Keep all consumers stopped through the existing 5/6/7→8, 8→9 and this 9→10
sequence. Use a final merged, reviewed Relay commit containing
`010-runtime-presence-latch-isolation.sql` and record its raw SHA256. The script
binds its expected function bodies to the exact SQL file digest
`86fd1612f0d9aac62902825d7e4fb290ac6c2c030f89d3e4a764a23230a125e1` and refuses a
different artifact before starting PostgreSQL. If the final merged SQL differs,
update these pins through a reviewed repository change; do not edit the script
while rendering a live Job. Do not use an unmerged candidate as a live release
pin. The renderer enforces this same digest for the `upgrade10` and
`initialize` inputs and refuses any other schema010 pin, before producing a Job; fresh initialization enforces the same SQL 010
artifact pin at runtime.

```json
{
  "version": 1,
  "relaySourceCommit": "<final reviewed 40-hex Relay commit>",
  "fromVersion": 9,
  "schema010": {"path": "/absolute/010-runtime-presence-latch-isolation.sql", "sha256": "86fd1612f0d9aac62902825d7e4fb290ac6c2c030f89d3e4a764a23230a125e1"}
}
```

```sh
python3 -B kubernetes/optional/relay-provenance-journal/render.py \
  upgrade10 /absolute/upgrade10-render.json <upgrade10-render-json-sha256>
```

This renders an inactive suspended Job with no retry, token or network listener.
As for the earlier stages, an authorized operator stops every source/database
consumer and excludes concurrent PVC users before starting maintenance. The Job
requires marker 9, safe retained PGDATA, PostgreSQL 17 and no pending latch. It
checks the two installed function signatures, owner, return types, search paths,
security-definer flags and exact bodies, and verifies the enabled admission
latch trigger before creating the persistent latch. Only the
reviewed pre-010 or already-repaired bodies are accepted. Function drift leaves
the old marker and no new latch, after a clean local PostgreSQL shutdown.

After pin verification and preflight, the Job persists `from=9 to=10`, applies
SQL 010, and checks the corrected function bodies and unchanged function
configuration, return types and ACLs before stopping PostgreSQL.
Only then does it atomically publish marker 10 and remove the latch. SQL 010 is
idempotent and a known already-applied body under marker 9 is supported when
there is no pending latch. This does not authorize automatic retries: SQL
failure, a failed final function check, interruption, or an existing pending
latch requires attended investigation. Keep the marker and latch as evidence;
never clear them to manufacture success. All manifests remain outside Flux
inventory and all runtime replicas remain zero. Do not restart the database at
marker 10 either: the runtime startup guard accepts only marker 11, so the
separate 10→11 stage must complete before any attended database or listener
restart.

Generate and compare the presence-function digests with this read-only query
against a disposable database initialized from the exact reviewed 001–009
source, then against the same database after SQL 010. The installed marker-9
preflight must match one of those reviewed bodies. SQL 005 is the last definition
of these functions before SQL 010; SQL 006–009 do not replace them.

```sql
SELECT p.oid::regprocedure AS signature,
       encode(sha256(convert_to(p.prosrc,'UTF8')),'hex') AS prosrc_sha256
FROM pg_proc p
WHERE p.oid IN (
  'worker_provenance.latch_runtime_presence_requirement()'::regprocedure,
  'worker_provenance.activate_runtime_presence_policy(text,text)'::regprocedure)
ORDER BY 1;
```

Before activation, inspect database/role `default_transaction_isolation` settings
and the source client's transaction setup through the operator's existing client.
Admission insertion and `activate_runtime_presence_policy` must run in READ
COMMITTED transactions. Explicit REPEATABLE READ or SERIALIZABLE callers remain
refused; a global server default alone cannot override role or client settings.
Use an explicit `BEGIN ISOLATION LEVEL READ COMMITTED` for attended activation.
Verify this with the exact selected source image before enabling issuance.

Local validation passed 13 offline rendering/startup tests and nine disposable
PostgreSQL 17 Alpine scenarios: fresh initialization, retained-history upgrade,
known preapplied SQL 010, pending-latch refusal, function-drift refusal,
artifact-drift refusal, migration failure with the latch retained, disabled
trigger refusal, and NULL function-configuration refusal before the latch.
The failure case injects a local test event trigger into the
disposable database; no target database was changed. The retained-history rows
are explicitly synthetic fixtures. These runs used Relay source candidate
`8178be756622af49f88c6b85a15fa0f120150a02` and local PostgreSQL image
`sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0`.
They do not qualify a merged source, the deployment image or a live maintenance
Job. Explicit Opus source reviews through LLM Router `pool-claude`
(`aafc078a-2186-4944-a5db-78dd3deac593`, then
`088a4d20-4401-4aee-aebf-f350921283a3`) ended ALIGNED for this inactive stage.
The subsequent fresh-initializer artifact check and repeated final trigger check
address its two optional consistency suggestions; the eight PostgreSQL scenarios
were rerun afterward; the NULL-configuration refusal and successful upgrade were
also checked after the reviewer-requested NULL handling correction. Raw snapshots and logs are in
`/workspace/archive/relay-provenance-schema10-20261112` on dev-worker-3, subject to
archive retention. No live Kubernetes writes were performed.

## Upgrade a retained schema 10 database to schema 11

Relay PR #399 adds `011-runtime-presence-current-readback.sql` for the inactive
runtime-presence ledger. Its current readback needs this function and refuses
without it. SQL011 adds a new function only; it does not rewrite existing
migration bytes, history or grants on older functions. This is separate from
the runtime-presence observer endpoint and does not create a Node signer,
sequence witness, physical fence or live lease authority.

Use the separate `upgrade11` renderer with `fromVersion: 10`, a merged reviewed
Relay source commit containing the complete 001–011 chain, and only `schema011`:

```json
{
  "version": 1,
  "relaySourceCommit": "<reviewed 40-hex commit>",
  "fromVersion": 10,
  "schema011": {"path": "/absolute/011-runtime-presence-current-readback.sql", "sha256": "094e6639b07d497401f86ab6af5ccd14813238d2beb1d538bcd1ac7f105caebd"}
}
```

```sh
python3 -B kubernetes/optional/relay-provenance-journal/render.py \
  upgrade11 /absolute/upgrade11-render.json <upgrade11-render-json-sha256>
```

The Job remains suspended, has no retry or service-account token, and mounts
the retained PVC without fsGroup ownership repair. The existing maintenance
exclusion rules apply: pause issuance, stop all listeners and the database,
and verify every PVC consumer is gone. The current runtime startup guard
requires marker11; earlier retained schemas must complete their separate
reviewed upgrades through schema10 first. Do not change a marker to skip a
stage or install an older guard to bypass maintenance.

The script requires exact schema10, PostgreSQL17, reviewed ownership/mode,
no recovery/postmaster/pending markers, and the fixed SQL011 SHA256 above.
It starts PostgreSQL over a Unix socket only. Before writing a maintenance
latch, it checks the exact owner, return type, SECURITY DEFINER flag, search
path and body digests of the policy/admission lock, current read, admission
latch and activation functions. The owner and ledger groups must remain
NOLOGIN, non-superuser and without BYPASSRLS; the new function must be absent.
Any event trigger or member of the owner role is refused. Existing independently
provisioned ledger logins may remain; their membership is preserved exactly.
Any drift exits after stopping PostgreSQL and preserves marker10. Retained
PostgreSQL configuration and local authentication remain trusted inputs that
the operator must review before maintenance.

Only after those checks does it write `from=10 to=11`, apply the pinned SQL
transaction, then verify the new function body, configuration and exact EXECUTE
ACL for its owner and the ledger purpose group. PUBLIC, observer and cleanup
verifier gain no grant. A before/after catalog snapshot also requires the four
prerequisite functions, purpose-role attributes, memberships and event-trigger
count to remain unchanged. The process stops PostgreSQL cleanly before atomically
publishing marker11 and removing the latch. A SQL or post-verification failure
leaves marker10 and the pending latch. A preapplied function with a stale marker
is refused; CREATE FUNCTION is forward-only, and an uncertain result needs
attended inspection rather than replay. No database credentials are created.

The ledger holds policy/admission locks across external witness reads and
checks deadline validity after COMMIT. Production composition therefore still
needs bounded connection/query timeouts and concurrency; sustained readers can
delay policy revocation. A readback is a serialized snapshot, and later
consumers must revalidate subsequent revocations. Do not configure the ledger
until this stage and the independent witness/custody prerequisites are complete.

Local validation uses SQL001–011 from source composition `021d2da4` and the
exact renderer image
`postgres@sha256:e5d2edd28b1f8d40a0a6c0c9cdc545b19b56a0d5d88f94bc11a97d970d4700d0`
(PostgreSQL 17.11 Alpine), running as UID70 with read-only root, no network,
no capabilities and temporary data. Fifteen scenarios passed: fresh
initialization; 10→11 with exact retained row hashes preserved; successful
upgrade with a pre-provisioned ledger login and unchanged membership; refusal of a
pending latch, preapplied011, NULL prerequisite configuration, LOGIN ledger
role, altered prerequisite body, event trigger, owner-role membership and
unrelated SQL artifact; preserved maintenance latch on injected SQL failure,
extra function ACL and prerequisite ACL drift; and a successful current read
as the ledger role followed by refusal after policy expiry. Failure injection
uses a test-owned executable psql wrapper; production accepts no event triggers.
Each result records the exact maintenance-script and harness SHA256 values.
The current-read fixtures are synthetic SQL projections, not signed or physical
evidence; the expiry case exercises the existing read refusal, not every new
time-bound branch in SQL011. Sixteen offline rendering/startup tests, shell syntax, kustomize
rendering and diff checks also passed.
Opus review through LLM Router `pool-claude` ended ALIGNED for inactive merge
(request `5e830b4d-19ca-4c2c-8549-bb21d41c004e`). Its retained-ledger-membership
and script-digest evidence suggestions were then added to the final smoke run.
That source composition is evidence only; substitute the final merged source
pin before a live change. Raw scenario scripts and logs are in
`/workspace/archive/relay-provenance-schema11-20261112` on dev-worker-3 and
follow archive retention. No live Kubernetes or database write was performed.

## Stage the separate retained VM source chain

The runtime-presence `011-runtime-presence-current-readback.sql` remains the
schema-11 stage above. The retained VM source uses a **different**
`011-kubevirt-original-registration.sql`, followed by
`012-kubevirt-connector.sql`, `013-kubevirt-lease-renewal.sql`, and
`014-vm-backing-effect-lookups.sql`. The attended `upgradeVm` renderer adds
those four transactions after marker 11 and publishes marker 15. It first
replays the reviewed 008 compatibility repair. That replay accepts the prior
and corrected schema-8 transition bodies, changes no retained rows, and brings
both paths to the NULL-safe body required by the current 009 and VM source.
The 009 transaction is never replayed. The current startup script accepts
markers 11 and 15; it still refuses every intermediate marker and a pending
maintenance latch.

The ailab renderer and upgrade script pin these exact current Relay source
artifacts by raw SHA-256:

| Input | SHA-256 |
| --- | --- |
| 008 compatibility repair | `9e7b48a1f624c83e421bd187354318f589f788229908bc283d4e4e0abf07f95c` |
| VM 011 original registration | `02a3d6459458304f69ded3c3601d2b3df53ea84ca5cd59b3c4df99f94d66993d` |
| VM 012 connector | `cabffdf93c9759110d31480481d90b4cf9996514d1fb217080e2350ed8155c3a` |
| VM 013 lease renewal | `4d0397e70d144104d0a991b2f4475b9fa8d5d52ed03f1a63de8eb6023d679fcd` |
| VM 014 backing reads | `17721e0021c88d76bf68157fbe6f28bb1ac44119fd92ecb291e001644d6eddf0` |

The deployment preparer derives all five file pins from one checked-out,
reviewed Relay commit and verifies that each worktree file equals that
commit's Git object. This is build/publication work; the operator does not
hand-enter SQL hashes. After the Relay source and image release receipts are
final, run from the ailab checkout with a clean checkout of that exact Relay
source:

```sh
python3 -B scripts/relay-provenance-vm-upgrade-input.py \
  /absolute/relay-checkout FULL_RELAY_COMMIT \
  /operator/run/vm-upgrade-render.json
sha256sum /operator/run/vm-upgrade-render.json
python3 -B kubernetes/optional/relay-provenance-journal/render.py \
  upgradeVm /operator/run/vm-upgrade-render.json RENDER_JSON_SHA256 \
  > /operator/run/vm-upgrade-list.json
```

The rendered Job is suspended, has no retry or service-account token, mounts
the retained PVC without `fsGroup`, and uses the pinned PostgreSQL 17 image.
It requires the stopped marker-11 database, UID 70 and reviewed mode, no
postmaster/recovery/pending files, the exact prior lookup and transition
bodies, the runtime-presence current-readback body, and no preapplied VM
family or v3 policy. It checks these before writing the persistent latch.
After applying the five pinned transactions, it verifies both final function
bodies, VM family owners, unchanged runtime-presence readback and retained
history row counts. It stops PostgreSQL before writing marker 15. Any unknown
commit, failed verification, interruption, or uncertain outcome leaves the
latch and old marker for attended recovery; do not replay or edit the marker.
All existing consumer-exclusion and backup review steps above apply. This
stage grants no source login, policy, signing key or provider authority.

## Stage the independent VM backing service

Relay's dedicated `deploy/vm-original-backing/Dockerfile` produces a Node 22
image that runs as UID/GID 65532. The build/release owner supplies its reviewed
image digest and source receipt. The `backing` renderer takes that digest, a
pinned public runtime config under `publicDirectory`, the existing private
Secret name, a retained receipt-archive PVC name, the agent client selector,
and approved Kubernetes/provider/guest egress CIDRs. It derives the ten other
public file pins from the runtime config, verifies every file, the v3 backing
source ID, fixed paths, owner UIDs and HTTPS endpoints, then emits a
zero-replica Deployment, Service, read-only Kubernetes RBAC and scoped
NetworkPolicies. The bundle creates no Secret, private key, PVC or source
login and is absent from every Flux inventory.

The renderer input has exactly `version: 1`, `relaySourceCommit`,
`backingImage` (digest form), `publicDirectory`, `configSha256`,
`privateSecret`, `archiveClaim`, `clients` (the same namespace/podLabels shape
as the cleanup listener), and nonempty `kubernetesCidrs`, `providerCidrs`,
`guestCidrs` arrays. Each CIDR must be canonical and narrower than a default
route. The renderer takes HTTPS ports from the pinned config and refuses a
source origin other than the local provenance listener. Pin this input JSON
as for every other renderer mode:

```sh
python3 -B kubernetes/optional/relay-provenance-journal/render.py \
  backing /operator/run/vm-backing-render.json RENDER_JSON_SHA256 \
  > /operator/run/vm-backing-list.json
```

The public directory uses these names: `config.json`, `policy.json`,
`reader-pins.json`, `storage-class.json`, `source-ca.pem`,
`kubernetes-ca.pem`, `provider-ca.pem`, `provider-client.crt`,
`guest-ca.pem`, `tls.crt`, and `client-ca.pem`. Its runtime paths must be
`/run/relay-vm-backing/public/<name>`. The existing private Secret must have
`source-key.pem`, `issuer-key.pem`, `provider-client-key.pem`, and
`tls-key.pem`. An init container in the same digest-pinned backing image
reads that projection as root with only `CHOWN`, copies each key into a memory
`emptyDir` as a regular UID/GID-65532 file with mode 0600, and gives the main
nonroot container read-only access. The archive PVC must already have a
writable UID-65532 directory; the Pod has no `fsGroup` repair. The rotating
Kubernetes service-account token alone remains projected. These details meet
the dedicated runtime's strict regular-file checks without putting key bytes
in Git. The operator must verify the target namespace permits the one bounded
root init container before selecting this optional bundle.

The operator supplies the real cluster/namespace identity, qualified storage
class and capacity evidence, reachable provider and guest readback endpoints,
read-only Kubernetes access, approved key custody and client certificate
fingerprint, a pre-owned archive PVC, and the actual network CIDRs. The Relay
release owner supplies the source and image digests, reader and policy
artifacts, and prepared public configuration. The agent's separate opt-in
`vmProvenance` chart values mount only its own source and backing mTLS client
keys; the backing issuer, source and provider keys stay in this service.
The source and backing workloads remain at zero replicas until the operator
checks the current release, provider/guest evidence and full VM lifecycle
contract. No deployment or live qualification is implied by rendering.

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

After Relay SQL 001–011 and the compatible bounded
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
