# Relay hub upgrade for the first researcher worker (plan, inactive)

Status: **plan and staged components only. Nothing here is applied, and merging changes no cluster
object.** It expands step 2 and 3 of the gated order in
[relay-worker-capacity.md](relay-worker-capacity.md#next-stage-first-researcher-runtime) and step 8
("Researcher release deployed") of [relay-router-connection.md](relay-router-connection.md), into the
exact settings, their order, the readbacks and what rollback means. The installation that follows is
[relay-researcher-installation.md](relay-researcher-installation.md).

Live today (operator readback, recheck before acting): Relay v0.9.1, source `a53a3ee8`, image
`registry.chifor.me/relay/control-plane@sha256:b2c269e7…`, schema 56, no installation, no recovery
generation, no Router connection, no worker identity. Every write below is an operator action with the
owner's direct confirmation for the exact reviewed rollout.

## Staged here

| Component (none included by `kubernetes/apps/apps/relay`) | Sets | Step |
| --- | --- | --- |
| `relay-restore-fencing` (existing) | `RELAY_RECOVERY_GENERATION_FILE` from the operator's `relay-recovery-generation` ConfigMap | H3 |
| `relay-infrastructure-installations` (new) | `RELAY_INFRASTRUCTURE_INSTALLATIONS=1` | H3 |
| `relay-worker-budget-client` (existing) | `RELAY_WORKER_BUDGET_{DSN,CA,WITNESS}_FILE` | H4 |
| `relay-worker-profiles` (new) | `RELAY_WORKER_PROFILES_FILE` and `_SHA256`, the registry ConfigMap | H4 |
| `relay-router-connection` (existing) | `RELAY_ROUTER_CONNECTIONS_FILE`, `NODE_EXTRA_CA_CERTS`, OpenBao egress | H6 |
| `relay-agent-control-plane` (new) | `RELAY_AGENT_CONTROL_PLANE=1` | H6 |
| `relay-control-monitoring`, `relay-worker-monitoring` (existing) | control metrics and worker alerts | H6 |

`scripts/tests/test_relay_v060_activation_gate.py` refuses all of them in the live overlay; each step's
PR removes exactly its own entries from that list. `test_relay_researcher_installation.py` refuses
installations without the recovery generation (and the reverse), the profile registry without the
installations and budget client or with a placeholder or wrong SHA, and the control plane without the
Router connection. `test-relay-researcher-render.sh` proves each component changes only the relay
container's env, mounts and volumes, and that the whole H3-H6 set combines without collision.

## Operator-filled values

| Value | Where it goes | From |
| --- | --- | --- |
| Hub image digest | both `registry.chifor.me/relay/control-plane@sha256:…` lines of `kubernetes/apps/apps/relay/relay.yaml` (`migrate` and `relay`) | the release's `deployment-images-<run>.json` receipt |
| Source commit, release name | the `chifor.me/source-commit` and `chifor.me/release` annotations and the two image comments | the same receipt; equals the installation's `sourceCommit` |
| N, the target schema | readback only | `CURRENT_SCHEMA_VERSION` in `server/schema.ts` and the highest `migrations/NNN_*.sql` at that commit (both 79 on Relay main `2ce7cd7ab`) |
| Recovery generation UUID | the operator-created ConfigMap `relay/relay-recovery-generation` (never in git, never in a backup) | `uuid.uuid4()` at step H3 |
| Profile registry, its SHA-256 | `kubernetes/components/relay-worker-profiles/profiles.json` and that component's `RELAY_WORKER_PROFILES_SHA256` | the release's researcher profile package |

None is guessed: until the release is pinned, the image, commit, N and registry stay unknown.

## Two decisions before H3

**G1. The first recovery generation on a live database is a full restore fence.** Relay creates a
`relay_recovery_fences` row only through `npm run recovery:fence`
(`server/agent-sessions/restore-fence.ts`, `fenceRestoredDatabase`); there is no "initialize" path
on Relay main `2ce7cd7ab` or any branch. Run against the live database it does what it does to a
restored one: every connector is revoked and blocked, every browser session deleted, every agent key
revoked and its session closed, pending key requests expired, messaging principals revoked and **all
channel memberships disabled** (the researcher channel `ae9b92bb-0b59-4987-b41f-376bd269db62`
included), launch profiles and access resources disabled, pending commands marked unknown, Router
credentials sent to revocation, environments fenced. Live connectors, relay-bus agent keys and the
owner's sessions all have to be re-approved afterwards. Before deciding, count what it would retire
(as `postgres` in `relay-postgres-0`):

```sql
SELECT (SELECT count(*) FROM connectors WHERE NOT revoked) AS connectors,
       (SELECT count(*) FROM agent_keys WHERE revoked_at IS NULL) AS agent_keys,
       (SELECT count(*) FROM agent_channel_members WHERE enabled) AS memberships,
       (SELECT count(*) FROM sessions) AS browser_sessions,
       (SELECT count(*) FROM router_credentials) AS router_credentials;
```

Two ways forward, the owner's choice: **(a)** a Relay change that adds an initialization command
which writes the first generation row and its receipt only when `relay_recovery_fences` is empty and
retires nothing (the database is not restored, so its authority is still current), shipped in the
pinned release; or **(b)** accept a maintenance window with the full fence, then re-approve every
connector and key and re-enable the memberships, including the researcher channel's. This plan writes
H3 for (b) with (a) as a drop-in replacement of its fence command.

**G2. The researcher needs `RELAY_AGENT_CONTROL_PLANE=1`.** A worker's holder installs its Router
credential through the holder launch transport, and Relay hands that transport its credential
manager only with the control plane on (`server/main.ts`, `launchTransportRoutes`;
`server/agent-sessions/holder-router.ts` refuses a routed launch with "Routed holder installation is
unavailable" otherwise). The flag also turns on control health and metrics, session control,
elicitations, the router-credential administration routes and the Router access manager for
`https://router.chifor.me`. The owner approves turning it on at H6; re-check the pinned release.

## Steps

`kubectl --context admin@ai` throughout. A failed readback stops the sequence.

### H0. Preconditions

- The researcher release is reviewer-merged and published: hub receipt, agent and runtime publish
  receipts, all from one `sourceCommit`. The Router side is live (llm-router #189/#190 release,
  route `relay-researcher-luna`, ailab #1763 trust), per Relay's
  `researcher-budgeted-codex-route.md`.
- G1 and G2 decided. The budget ledger is through its runbook's step 6 (logins, authority row
  `ready=false`, witness) so H4's Secret exists.
- Capacity: `kubectl top node` for the agent node running Relay; the Recreate rollout is an outage
  of Relay for the backup, migration and start (minutes; the backup has a 30-minute bound).

### H1. Verified backup before anything changes

1. Record the live state: image digest and annotations of `deploy/relay`, and

   ```sql
   SELECT count(*), max(version) FROM schema_migrations;   -- 56, 56
   SELECT to_regclass('infrastructure_installations');      -- NULL
   ```

2. Take a fresh coordinated backup and verify that it restores:

   ```sh
   kubectl --context admin@ai -n relay create job --from=cronjob/relay-postgres-dump relay-dump-pre-researcher-$(date -u +%Y%m%dT%H%M%SZ)
   kubectl --context admin@ai -n relay wait --for=condition=complete job/<that job> --timeout=35m
   ```

   Then the restore check of [relay.md](relay.md) ("To test a fresh backup"): `SHA256SUMS` OK,
   `pg_restore` into a **disposable** database as `relay_migrator` with `--no-owner --no-acl
   --exit-on-error`, table and row counts equal, `relay_migrator` owns every table, forced RLS on
   every protected table, the generation's `plugins/` load; drop the disposable database. Record the
   generation name and its `relay.dump` SHA-256. This is the rollback point if the rollout's own
   pre-migration generation is unusable.
3. Confirm `velero` produced its last daily backup of namespace `relay` (`velero backup get`).

### H2. Deploy the release, installation still disabled

One reviewed PR edits `kubernetes/apps/apps/relay/relay.yaml` only: the hub digest in both
`migrate` and `relay`, the release and source annotations, the two comments. No env change, no
component. Merging is a Recreate rollout through the existing gate: `backup-database` dumps,
`backup-artifacts` publishes and writes `/tmp/relay-pre-migration-backup.json`, `migrate` applies
migrations **057 through N** in order, each in its own transaction (on main `2ce7cd7ab`: 057
`infrastructure_installations` … 079 `worker_provenance_connectors`, 23 files), re-grants `relay_app`,
runs bootstrap, then `relay` starts.

Readback:

- the new Pod's image ID is the receipt digest; all three init containers succeeded; record the
  generation named in the **first** attempt's `backup-database` log;
- `SELECT count(*), max(version) FROM schema_migrations;` returns N, N (contiguous: the later fence
  refuses any gap);
- `SELECT to_regclass(t) FROM unnest(ARRAY['infrastructure_installations','worker_tenant_controls',
  'worker_inference_restore_fences','worker_router_rotations']) t;` all present;
- `/ready` 200, OIDC sign-in works, connectors reconnect, relay-bus keys still answer;
- `GET /api/infrastructure/installations` is **404** (installations are off) and the Pod env has no
  `RELAY_INFRASTRUCTURE_INSTALLATIONS` or `RELAY_RECOVERY_GENERATION_FILE`.

### H3. First recovery generation, then installations on

1. **Generation file.** On the operator host:

   ```sh
   python3 -c 'import json,uuid; print(json.dumps({"version":1,"generation":str(uuid.uuid4())}))' > generation.json
   kubectl --context admin@ai -n relay create configmap relay-recovery-generation --from-file=generation.json
   kubectl --context admin@ai -n relay get configmap relay-recovery-generation -o json \
     | jq --slurpfile expected generation.json -e '(.data["generation.json"] | fromjson) == $expected[0]'
   ```

   It is created by hand on purpose: it stays out of Flux, git and every backup selection
   ([relay-recovery-verification.md](relay-recovery-verification.md)). Record its UUID with the
   owner's evidence, outside the cluster.
2. **Stop every database client.** `flux suspend kustomization apps -n flux-system`; `kubectl -n
   relay scale deploy/relay --replicas=0`; make sure no `relay-postgres-dump` Job is running and
   suspend the CronJob for the window (`kubectl -n relay patch cronjob relay-postgres-dump -p
   '{"spec":{"suspend":true}}'`, reverted by Flux on resume). As `postgres`:
   `GRANT pg_read_all_stats TO relay_migrator;` (the fence counts unclassifiable backends as clients).
3. **Fence (G1 (b)) or initialize (G1 (a)).** A one-off Pod with the release image, the schema owner
   from `relay-migrations` and the generation file, in namespace `relay`, labelled `app: relay-backup`
   so the existing `relay-backup` NetworkPolicy gives it DNS and PostgreSQL only:

   ```yaml
   apiVersion: v1
   kind: Pod
   metadata: {name: relay-recovery-fence, namespace: relay, labels: {app: relay-backup}}
   spec:
     restartPolicy: Never
     automountServiceAccountToken: false
     tolerations: [{key: dedicated, operator: Equal, value: agent, effect: NoSchedule}]
     securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
     containers:
     - name: fence
       image: registry.chifor.me/relay/control-plane@sha256:<the H2 digest>
       workingDir: /app
       command: [node, dist/scripts/fence-recovery.js, relay]
       envFrom: [{secretRef: {name: relay-migrations}}]
       env: [{name: RELAY_RECOVERY_GENERATION_FILE, value: /run/relay-restore/generation.json}]
       securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
       volumeMounts: [{name: generation, mountPath: /run/relay-restore/generation.json, subPath: generation.json, readOnly: true}]
     volumes:
     - name: generation
       configMap: {name: relay-recovery-generation, defaultMode: 292, items: [{key: generation.json, path: generation.json}]}
   ```

   `kubectl create -f`, wait for it to finish, and save its single stdout line: the receipt
   `{version, generation, schemaVersion: N, tenants, connectors, sessions, credentials, environments}`.
   That receipt, kept with the generation UUID outside the cluster, is the independently recorded
   receipt. Exit non-zero means nothing changed (one transaction); read its stderr, fix, retry the
   same generation. Then delete the Pod and `REVOKE pg_read_all_stats FROM relay_migrator;`.
4. **Enable together.** One PR includes `../../../components/relay-restore-fencing` and
   `../../../components/relay-infrastructure-installations` in `kubernetes/apps/apps/relay` and removes
   their four entries from the activation gate's forbidden list. Merge, then
   `flux resume kustomization apps -n flux-system` (Relay starts with a fresh pre-migration backup;
   migrate finds nothing to do).

Readback: the Pod has both variables; it started (Relay refuses to start unless the active fence
generation equals the file); `SELECT generation, active, receipt FROM relay_recovery_fences;` is one
active row with the recorded UUID and receipt; `GET /api/infrastructure/installations` is 200 `[]`;
the UI reports workers enabled. Under G1 (b): re-approve connectors and keys and re-enable the
memberships the owner names, and check the researcher channel's.

### H4. Budget client and profile registry

Preconditions: the `relay/relay-worker-budget` ExternalSecret is Ready (budget runbook steps 5-6);
the profile registry for the pinned release is reviewed (`relay-worker-profiles/kustomization.yaml`
lists its content: tenant, sealed researcher template digest, runtime digest, `modelPolicy`, the
complete profile template with `launcher-proxy-v1`, UID 1000, `/workspace`, channel
`ae9b92bb-0b59-4987-b41f-376bd269db62`, inference `{connectionId
babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8, protocol codex-responses, selection {kind: model, model:
relay-researcher-luna}}` with `adaptedResponses {catalogModel gpt-6-luna, maxOutputTokens 8192,
reasoningEffort none}`, the release approval and the qualified `tokenPolicyDigests`).

One PR: replace `profiles.json`, set `RELAY_WORKER_PROFILES_SHA256` to its `sha256sum`, include
`relay-worker-budget-client` and `relay-worker-profiles`, remove `RELAY_WORKER_PROFILES_FILE` and
`relay-worker-profiles` from the forbidden list. Readback: the mounted file's SHA-256 inside the Pod
equals the pin; the budget files' SHA-256 equal the Secret's; the log has no
`worker_budget_authority_unavailable` with reason `profiles-unavailable`. Until H6 adds the Router
connection the authority itself cannot start; that warning (`ledger-config-invalid`) is expected
here and must disappear at H6.

### H5. Escrow token

[relay-router-connection.md](relay-router-connection.md) step 9, unchanged: mint the
`relay-router-escrow` token, read it back, `relay-router-files` becomes Ready. Its 720 h clock starts
now, so H6 follows promptly.

### H6. Router connection, control plane, monitoring

Preconditions: budget authority `ready=true` and the Router plugin enabled (budget runbook steps
8-9, Router connection runbook step 7); G2 approved; the `relay-control-metrics` collector Secret
provisioned ([relay-control-monitoring.md](relay-control-monitoring.md)).

One PR includes `relay-router-connection`, `relay-agent-control-plane`, `relay-control-monitoring` and
`relay-worker-monitoring`, and amends the activation gate (`RELAY_ROUTER_CONNECTIONS_FILE`,
`relay-router-connection`, `RELAY_AGENT_CONTROL_PLANE`, `relay-agent-control-plane`) and
`InclusionGate.test_relay_component_is_not_included` of `test_relay_router_connection.py`. Readback
as Router connection runbook step 10, plus: control health 200; the metrics scrape includes the
worker and budget series with budget startup `started`; `GET /api/router-credentials/connections`
lists `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`; no `worker_budget_authority_unavailable` in the log.

After H6 the hub can approve the installation: continue with
[relay-researcher-installation.md](relay-researcher-installation.md) step I3. Its steps I1-I2 can run
any time after H3.

### H7. Later hub settings, each its own reviewed component and PR

| Setting | When | Note |
| --- | --- | --- |
| `RELAY_WORKER_CANDIDATE_FILE`, `_SHA256`, `_SESSION_KEY_FILE` | the nineteen scenarios (delivery plan step 9) | needs the independent candidate journal and a signed run |
| `RELAY_WORKER_ORDINARY_CANARY_FILE`, `_SHA256`, `_POLICY_FILE` | the finite canary (step 10) | a configured permit that cannot load stops Relay from starting |
| `RELAY_WORKER_QUALIFICATION_FILE`, `_SHA256` | general activation (step 11) | the registry of accepted evidence |
| `RELAY_WORKER_PROVENANCE_FILE`, `_SHA256` | not for the repository-free researcher | stays unset |

## Enablement order at a glance

| Step | Change | Must already hold | Readback that gates the next step |
| --- | --- | --- | --- |
| H1 | none (backup) | – | verified restore of a fresh generation |
| H2 | hub image → release | H1 | schema N contiguous, installations 404 |
| H3 | fence/initialize, then `RELAY_RECOVERY_GENERATION_FILE` + `RELAY_INFRASTRUCTURE_INSTALLATIONS` | G1, H2 | one active fence row; installations API 200 |
| H4 | `RELAY_WORKER_BUDGET_*` + `RELAY_WORKER_PROFILES_FILE`/`_SHA256` | ledger Secret Ready, registry reviewed | file pins equal; no `profiles-unavailable` |
| H5 | escrow token | H4 | `relay-router-files` Ready |
| H6 | `RELAY_ROUTER_CONNECTIONS_FILE` + `RELAY_AGENT_CONTROL_PLANE` + monitoring | ledger `ready=true`, plugin on, G2 | budget startup `started`, connection listed |
| I1-I10 | the researcher installation | H3 (approval needs H6 for workers) | installation `ready`, fence UID pinned |
| H7 | candidate, canary, qualification files | the evidence for each | per Relay's documents |

## Rollback: migrations are forward-only

Relay's migrations only move forward, and no Relay binary is supported against a schema newer than
its own: never run v0.9.1 against schema N. "Rollback" therefore means one of these, by step:

- **H2 fails or the release misbehaves:** restore the database to the **earliest** pre-migration
  generation of this rollout (the first `backup-database` log; later retries may have dumped a
  partially migrated database; fall back to the H1 generation and accept its loss window), following
  [relay.md](relay.md)'s "Rollback to 0.3.5" mechanics: freeze Flux and scale to zero, rename the
  failed database aside, recreate `relay` owned by `relay_migrator` with the bootstrap's per-database
  statements, `pg_restore`, verify counts/ownership/RLS, restore `plugins/` if needed, then a reviewed
  PR pins v0.9.1 (`b2c269e7…`, `a53a3ee8`) again and Flux resumes. Writes after the backup are lost.
- **H3 after the fence:** the fence cannot be undone. Removing the generation file makes Relay refuse
  to start against a fenced database, so the components stay. To abandon workers, revert only
  `relay-infrastructure-installations` (Relay keeps the generation check and runs without the worker
  APIs). Going back to v0.9.1 is the H2 restore, to a pre-fence generation.
- **H4-H6:** revert the inclusion PR; nothing in the database changes. Before reverting H6 after any
  issuance, revoke worker credentials through Relay and let escrow cleanup finish (Router connection
  runbook, "Rollback").
- **After installation enrollment:** retire through the installation runbook first; removing hub
  components never revokes issued keys or reconciles workers.

## What still blocks a web-UI-created researcher

1. **G1:** no non-destructive way to create the first recovery generation on the live database.
2. **G2:** the routed holder needs `RELAY_AGENT_CONTROL_PLANE=1`, still refused by the activation gate.
3. **Nothing is pinned or published:** no reviewed researcher release, hub receipt, agent or runtime
   image receipt, sealed researcher template, `PodRuntimeBinding` or profile registry. Relay main has
   no researcher template with resources; the ceilings here (2 CPU, 4 GiB, 10 GiB scratch, 512
   processes, 24 h lifetime, 300 s lease) are this review's proposal and enter the binding's
   `policyDigest`.
4. **The release's binding must place the researcher with `kubernetes.io/hostname: relay-worker-1`**,
   or Relay's preparation refuses the control placement.
5. **Model and Router:** paid qualification of `gpt-6-luna` on `openai-2` through
   `relay-researcher-luna` (owner), llm-router #1764 and ailab #1763 merged and live, the budget
   authority `ready=true` with the `clients` row's `max_tokens` = 8 × (I + 8192) once I is proven.
6. **Qualification 0/19, no canary:** the slot keeps zero Pods and the deny-all gate until the nineteen
   rows are accepted, the canary passes and the activation compiler's outputs (guards, gate
   replacement, one-Pod quota, `ordinaryNamespacePins`, `bootstrapSecrets: true`) are reviewed and
   committed. Only then does the Workers page offer a researcher that can start.
7. **Egress measurement limits** (installation runbook): the hub and the Router share two Cloudflare
   addresses, so a hub-only partition cannot be measured, and the allowance reaches other zones on
   those addresses.
