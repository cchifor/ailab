# Relay hub upgrade for the first researcher worker (in progress)

Status: **in progress. H1-H3 and H4a are applied; H5, H6, H7 and H4b are not.** Each step's PR changes
the live hub when it merges (Flux), so each is held until the owner approves its rollout. This runbook
expands step 2 and 3 of the gated order in
[relay-worker-capacity.md](relay-worker-capacity.md#next-stage-first-researcher-runtime) and step 8
("Researcher release deployed") of [relay-router-connection.md](relay-router-connection.md), into the
exact settings, their order, the readbacks and what rollback means. The installation that follows is
[relay-researcher-installation.md](relay-researcher-installation.md), staged with its manifests in
ailab #1766 (a separate PR; either can merge first).

State (hub operator readbacks through 2026-11-21 01:00Z: H4a on 2026-11-16, H6 on 2026-11-20; agent
pin updated 2026-11-18; recheck before acting):
- Relay v0.10.4, source `e77bd09a`, image `registry.chifor.me/relay/control-plane@sha256:d1482bc1…57a7`
  (ailab #1789), schema 79, is live until H8's roll; H8 (ailab #1915) pins v0.10.9, source `04a897f2`,
  image `…@sha256:1b0f2f72…7a39`, and this line changes to it with H8's readback;
- the first recovery generation is initialized, and installations are enabled (H3);
- installation `b380ff7f-0716-478d-8b01-3d39f30e750b` is `ready`, epoch 2, with its fence UID pinned
  (installation runbook I1-I10), and the slot is on its qualification egress stage (Q1-Q5);
- the budget client files are mounted (H4a, ailab #1805);
- the infrastructure agent is now v0.10.6 (`1599d06e`), an agent-only update pinned by
  `docs/relay-workers/researcher-binding-inputs.json` `agentRelease`. Its `release` still pins the
  runtime and binding to v0.10.3 (`c0fe50f0`); the hub is v0.10.4 until H8. A later agent update is not a
  replacement pin until its own reviewed update lands;
- H6 is applied (ailab #1811, 2026-11-20): the Router connection file, the agent control plane and control
  monitoring are in the running Pod. No worker pool is approved and `router_connections` is empty: in
  v0.10.4 the first inference pool cannot be approved (H8). No candidate files and no profile registry
  yet.

Before this work the hub was Relay v0.9.1 (`a53a3ee8`, schema 56, no installation and no recovery
generation). Every write below is an operator action with the owner's direct confirmation for the exact
reviewed rollout.

The per-run ceremony for H7's signed candidate runs is [relay-researcher-qualification-campaign.md](relay-researcher-qualification-campaign.md).

## Staged here

| Component (`kubernetes/apps/apps/relay` includes each from its step: H3, H4a and H6 so far) | Sets | Step |
| --- | --- | --- |
| `relay-restore-fencing` (existing) | `RELAY_RECOVERY_GENERATION_FILE` from the operator's `relay-recovery-generation` ConfigMap | H3 |
| `relay-infrastructure-installations` (new) | `RELAY_INFRASTRUCTURE_INSTALLATIONS=1` | H3 |
| `relay-worker-budget-client` (existing) | `RELAY_WORKER_BUDGET_{DSN,CA,WITNESS}_FILE` | H4a |
| `relay-worker-profiles` (new) | `RELAY_WORKER_PROFILES_FILE` and `_SHA256`, the registry ConfigMap | H4b (after the release approval) |
| `relay-router-connection` (existing) | `RELAY_ROUTER_CONNECTIONS_FILE`, `NODE_EXTRA_CA_CERTS`, OpenBao egress | H6 |
| `relay-agent-control-plane` (new) | `RELAY_AGENT_CONTROL_PLANE=1` | H6 |
| `relay-control-monitoring`, `relay-worker-monitoring` (existing) | control metrics and worker alerts | H6 |
| `relay-worker-candidate` (new) | `RELAY_WORKER_CANDIDATE_FILE`, `_SHA256`, `_SESSION_KEY_FILE`; the run's document ConfigMap and the key-copy init container; `run/` is generated per signed run ([relay-candidate-run-generator.md](relay-candidate-run-generator.md)) | H7 |

`scripts/tests/test_relay_v060_activation_gate.py` refuses H7's candidate files and H4b's profile registry in
the live overlay; each step's PR removes exactly its own entries from that list (H3's installation pair and
H6's Router connection and agent control plane are gone; H6's monitoring components were never on it).
`relay-restore-fencing` stays includable on its own, because a restore of the hub needs it with or without
installations. `test_relay_researcher_hub_upgrade.py`
pins each patch, and refuses installations without the recovery generation (one way only), the profile
registry without the installations and budget client or with a placeholder or wrong SHA, the control
plane without the Router connection or next to `RELAY_ENVIRONMENTS`, `relay-gitea-authority` or
`relay-notifications`, the control-plane collector without the control plane (Relay refuses to start)
and the worker alerts without the collector, the candidate files without H6 or next to the profile registry, and an H3 that
runs the fence script; `test_relay_candidate_run.py` refuses a placeholder `run/` in the overlay and any
committed `run/` or agent candidate value that is not `scripts/relay-candidate-run.py`'s output for the
committed run. It does not require the registry for the
control plane, because H6 comes before H4b. `test-relay-researcher-hub-render.sh` proves each step's
components change only the relay container's env, mounts and volumes (H7 also adds its one init
container, the only one that mounts the session-key Secret), and that the whole set (H3, H4a, H6 and
H4b) and the H7 set (H3, H4a, H6 and the candidate files) combine without collision. It layers every case on the overlay without the hub
components, and proves that the live overlay renders as exactly that plus the ones it already
includes (H3 and H4a on).

## Operator-filled values

| Value | Where it goes | From |
| --- | --- | --- |
| Hub image digest | both `registry.chifor.me/relay/control-plane@sha256:…` lines of `kubernetes/apps/apps/relay/relay.yaml` (`migrate` and `relay`) | the release's `deployment-images-<run>.json` receipt |
| Source commit, release name | the `chifor.me/source-commit` and `chifor.me/release` annotations and the two image comments | the hub receipt: `04a897f22678cd59d2af3e01680a793bf63eb85f`, `relay-0.10.9-release-143446` (H8; before it `e77bd09a…`, `relay-0.10.4-release-134512`); distinct from the installation's runtime `sourceCommit` |
| N, the target schema | readback only | `CURRENT_SCHEMA_VERSION` in `server/schema.ts` and the highest `migrations/NNN_*.sql` at the pinned hub commit (`04a897f2` after H8, `e77bd09a` before; both 79) |
| Recovery generation UUID | the operator-created ConfigMap `relay/relay-recovery-generation` (never in git, never in a backup) | `uuid.uuid4()` at step H3 |
| Profile registry, its SHA-256 | `kubernetes/components/relay-worker-profiles/profiles.json` and that component's `RELAY_WORKER_PROFILES_SHA256` | the release's researcher profile package, with the genuine release approval and the qualified `tokenPolicyDigests` (step H4b) |

The hub image, commit and N are pinned in `kubernetes/apps/apps/relay/relay.yaml` and the cited Relay
source. The registry stays unknown until the release approval exists (H4b).

## Owner decisions (relay#277 comment 122379, 2026-11-14)

**G1. The first recovery generation comes from a one-shot initializer, never from the fence.** Until
now the only way to create a `relay_recovery_fences` row was `npm run recovery:fence`
(`server/agent-sessions/restore-fence.ts`), which on the live hub would revoke every connector, agent
key, browser session and channel membership. The pinned hub release (v0.10.4, `e77bd09a`) ships the
`recovery:initialize` command (`scripts/initialize-recovery.ts`, `package.json`; exact invocation
in H3). As decided, it:

- refuses if **any** `relay_recovery_fences` row exists, active or not, or any generation-bound
  authority exists (infrastructure installations, worker grants, management credentials, creation
  policies, non-legacy accounting pools, environments bound to a recovery generation);
- requires the operator's explicit statement that this is the current, unrestored database, and uses
  the fence's guards (schema owner without RLS bypass, the recovery lock, the exact schema check);
- writes the generation row with an independent receipt of `kind: "initial"` and one audit event per
  tenant, and retires nothing: connectors, agent keys, sessions, memberships and Router credentials
  are untouched;
- never runs at migration time or startup.

Every restore, including a restore of a backup taken before initialization, still goes through
`recovery:fence` with a fresh generation.

**G2. `RELAY_AGENT_CONTROL_PLANE=1` on the live hub at H6, full scope accepted.** The researcher needs
it: a worker's holder installs its Router credential through the holder launch transport, and Relay
hands that transport its credential manager only with the control plane on (`server/main.ts`,
`launchTransportRoutes`; `server/agent-sessions/holder-router.ts` refuses a routed launch with
"Routed holder installation is unavailable" otherwise). Every feature it turns on keeps its existing
authorization checks; the owner accepted all of them:

- launch profiles (approval and launch admission);
- session control, permission decisions and elicitations;
- the Router access manager, implicitly: `RELAY_LLM_URL` is `https://router.chifor.me/v1`, so its
  origin becomes `routerAccessOrigin` and the access-request flow and its worker loop start;
- delegation (with the installations of H3);
- managed Assistant inference and the router-credential administration routes (with the connection
  file of `relay-router-connection`);
- the connector control loops (the control-plane variants of the session, holder-event, messaging and
  conversation routes) and the control health and metrics routes.

Excluded at H6 and after: `RELAY_ENVIRONMENTS` stays unset, and the `relay-gitea-authority` and
`relay-notifications` components stay out of the overlay (the tests refuse either next to the control
plane). A narrower routing-only flag is a Relay follow-up.

**G3 and G4** concern the installation: the Cloudflare egress rule is for the enrollment stage only;
qualification moves the slot to two in-cluster TLS terminators (ailab #1768, `relay-internal-tls`) and
a DNS-less egress stage (the installation runbook's steps Q1-Q6, ailab #1769), and the template is Relay's
researcher baseline inside the ceilings 2000m / 4096 MiB / 10240 MiB / 512 processes, lease 900 s,
lifetime 8 h. See [relay-researcher-installation.md](relay-researcher-installation.md).

## Steps

`kubectl --context admin@ai` throughout. A failed readback stops the sequence.

### H0. Preconditions

- The researcher releases are reviewer-merged and published: the hub receipt matches `relay.yaml`,
  the runtime receipt matches `researcher-binding-inputs.json` `release`, and the agent receipt matches
  its `agentRelease`. These are separate source commits after the hub-only and agent-only updates.
  The pinned hub release (v0.10.4, `e77bd09a`) contains the `recovery:initialize` command (G1),
  run from H2's hub image in H3 step 4. The pinned runtime release (v0.10.3, `c0fe50f0`) contains the
  Relay part of G3: `PodRuntimeBinding.hostAliases` rendered and measured, the Router and
  communication CA pins, the candidate journal alias and the hub-partition driver with separate hub
  and Router rules. The runtime is built with `hubCa` = the ailab root CA
  (`docs/relay-workers/researcher-runtime-ca.crt`, sha256 `3d109e83…54ad4c3`), every worker template's
  `holder.router` and `holder.communication` carry `caFile: /etc/relay/hub-ca.pem` with that
  `caSha256`, and the binding carries `hostAliases {hub: 10.96.0.40, router: 10.96.0.41,
  candidateJournal: 10.96.0.42}` (installation runbook, step Q1). The hub itself is unchanged by G3:
  workers still call `https://relay.chifor.me`, which must stay equal to `RELAY_URL`'s origin. The
  Router side is rolled and read back (ailab #1764 for llm-router #189/#190,
  the route `relay-researcher-luna` and ailab #1763's trust, both merged), per Relay's
  `researcher-budgeted-codex-route.md`.
- The budget ledger is through its runbook's step 6 (logins, authority row `ready=false`, witness) so
  H4a's Secret exists.
- Capacity: `kubectl top node` for the agent node running Relay; the Recreate rollout is an outage
  of Relay for the backup, migration and start (minutes; the backup has a 30-minute bound).

### H1. Verified backup before anything changes

H1-H3 and H4a below record the completed rollout from v0.9.1. Do not repeat the migration or initial
generation ceremony on today's initialized database; resume at H5 after the checklist readbacks.

1. Record the live state: image digest and annotations of `deploy/relay`, and

   ```sql
   SELECT count(*), max(version) FROM schema_migrations;   -- 56, 56
   SELECT to_regclass('infrastructure_installations');      -- NULL
   ```

2. Take a fresh coordinated backup and verify that it restores:

   ```sh
   kubectl --context admin@ai -n relay create job --from=cronjob/relay-postgres-dump relay-dump-pre-researcher-$(date -u +%Y%m%d%H%M%S)
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
migrations **057 through N** in order, each in its own transaction (on the hub commit `e77bd09a`: 057
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

### H3. First recovery generation (initializer), then installations on

1. **Baseline counts.** As `postgres` in `relay-postgres-0`, the authority the initializer must NOT
   touch. Run it once after step 3's freeze (no Relay process can change these then) and again right
   after step 4, before anything restarts:

   ```sql
   SELECT (SELECT count(*) FROM connectors WHERE NOT revoked) AS connectors,
          (SELECT count(*) FROM agent_keys WHERE revoked_at IS NULL) AS agent_keys,
          (SELECT count(*) FROM agent_channel_members WHERE enabled) AS memberships,
          (SELECT count(*) FROM sessions) AS browser_sessions,
          (SELECT count(*) FROM router_credentials) AS router_credentials,
          (SELECT count(*) FROM relay_recovery_fences) AS fences;          -- fences must be 0
   ```

2. **Generation file.** On the operator host:

   ```sh
   python3 -c 'import json,uuid; print(json.dumps({"version":1,"generation":str(uuid.uuid4())}))' > generation.json
   kubectl --context admin@ai -n relay create configmap relay-recovery-generation --from-file=generation.json \
     --dry-run=client -o yaml \
     | kubectl --context admin@ai label --local -f - velero.io/exclude-from-backup=true -o yaml \
     | kubectl --context admin@ai create -f -
   kubectl --context admin@ai -n relay get configmap relay-recovery-generation -o json \
     | jq --slurpfile expected generation.json -e '((.data["generation.json"] | fromjson) == $expected[0])
         and .metadata.labels["velero.io/exclude-from-backup"] == "true"'
   ```

   It is created by hand on purpose: it stays out of Flux and git, and the
   `velero.io/exclude-from-backup=true` label keeps it out of both Velero schedules, which otherwise
   back up every resource of namespace `relay` (H1 step 3). The coordinated logical dump contains only
   the database. Restoring a copy of the generation next to the database would defeat the restore
   detection ([relay-recovery-verification.md](relay-recovery-verification.md)). Record its UUID with
   the owner's evidence, outside the cluster.
3. **Freeze Relay for the window.** Once a generation row exists, a Relay process started without
   the generation file refuses to start, so no Relay may restart between step 4 and step 5:
   `flux --context admin@ai suspend kustomization apps -n flux-system`, then
   `kubectl --context admin@ai -n relay scale deploy/relay --replicas=0` and wait until no `relay` Pod
   remains. (Installations are off until step 5, so a running hub could not have created
   generation-bound authority; the freeze is about the restart, not about racing the initializer.)
   The shipped initializer imports and calls `initializeRecoveryGeneration` from `restore-fence.ts`
   (`e77bd09a:scripts/initialize-recovery.ts:6-10,87-94`). That helper locks the generation-bound
   authority tables and allows other clients to stay connected
   (`e77bd09a:server/agent-sessions/restore-fence.ts:605-609`). Its initialization path does not
   inspect `pg_stat_activity`, so no `pg_read_all_stats` grant is needed (`restore-fence.ts:546-664`);
   the other-client inspection belongs to `recovery:fence` (`restore-fence.ts:227-244`, same commit).
4. **Initialize.** A one-off Pod with the H2 release image, the schema owner from `relay-migrations`
   and the generation file, in namespace `relay`, labelled `app: relay-backup` so the existing
   `relay-backup` NetworkPolicy gives it DNS and PostgreSQL only. The command line is the pinned
   release's `recovery:initialize`: the attestation repeats the database name, and `--receipt-file`
   must name a new absolute writable path (`e77bd09a:scripts/initialize-recovery.ts:12-45,77-96`).
   The small `emptyDir` below supplies that path under the read-only root filesystem; stdout is
   retained outside the Pod before deletion:

   ```yaml
   apiVersion: v1
   kind: Pod
   metadata: {name: relay-recovery-initialize, namespace: relay, labels: {app: relay-backup}}
   spec:
     restartPolicy: Never
     automountServiceAccountToken: false
     tolerations: [{key: dedicated, operator: Equal, value: agent, effect: NoSchedule}]
     securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
     containers:
     - name: initialize
       image: registry.chifor.me/relay/control-plane@sha256:<the H2 digest>
       workingDir: /app
       # recovery:initialize (package.json), compiled in the pinned hub image
       command: [node, dist/scripts/initialize-recovery.js, relay, --current-unrestored-database, relay, --receipt-file, /run/relay-initialize/receipt.json]
       envFrom: [{secretRef: {name: relay-migrations}}]
       env: [{name: RELAY_RECOVERY_GENERATION_FILE, value: /run/relay-restore/generation.json}]
       securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
       volumeMounts:
       - {name: generation, mountPath: /run/relay-restore/generation.json, subPath: generation.json, readOnly: true}
       - {name: receipt, mountPath: /run/relay-initialize}
     volumes:
     - name: generation
       configMap: {name: relay-recovery-generation, defaultMode: 292, items: [{key: generation.json, path: generation.json}]}
     - name: receipt
       emptyDir: {sizeLimit: 1Mi}
   ```

   `kubectl --context admin@ai create -f <reviewed Pod file>`, wait for it to finish, and save its stdout: the receipt with
   `kind: "initial"`, the generation and `schemaVersion: N`. Kept with the generation UUID outside the
   cluster, it is the independently recorded receipt. A non-zero exit requires inspection: a refusal
   before commit changes no authority, but a receipt-file write failure occurs **after** the database
   commit and prints the receipt to stdout (`scripts/initialize-recovery.ts:100-127`). Preserve it and
   read back the row before deciding anything; do not retry initialization. **Never
   fall back to `recovery:fence`**: on this database that would retire every connector, key, session
   and membership, and the fence is only for a restore. Delete the Pod after retaining the receipt.
5. **Enable together.** One PR includes `../../../components/relay-restore-fencing` and
   `../../../components/relay-infrastructure-installations` in `kubernetes/apps/apps/relay` and removes
   `RELAY_INFRASTRUCTURE_INSTALLATIONS` and `relay-infrastructure-installations` from the activation
   gate's forbidden list (the recovery generation itself is never refused there: a restore needs it). Merge, then
   `flux --context admin@ai resume kustomization apps -n flux-system` (Relay starts with a fresh pre-migration backup;
   migrate finds nothing to do).

Readback:

- the Pod has both variables and started (Relay refuses to start unless the active generation row
  equals the file);
- `SELECT generation, active, receipt->>'kind' FROM relay_recovery_fences;` is exactly one row: the
  recorded UUID, `active = true`, `initial`; the release's initialization audit event exists once per
  tenant;
- step 1's two runs are identical except `fences` 0 → 1: no connector, agent key, session,
  membership or Router credential was retired, so nothing is re-approved;
- `GET /api/settings` (signed in) reports `workers: true` and still `agentControlPlane: false`;
  `GET /api/infrastructure/installations` is 200 `[]`;
- dev-worker connectors are online again and a relay-bus key still answers its inbox.

### H4a. Budget client only

Preconditions: the `relay/relay-worker-budget` ExternalSecret is Ready (budget runbook steps 5-6).

One PR includes `relay-worker-budget-client` and nothing else. The profile registry is **not** part
of this step: every registry entry carries a genuine release approval, which exists only after
qualification, so the registry is step H4b, after H7
([H4b](#h4b-profile-registry-after-the-release-approval) has the evidence). The budget client is not
on the activation gate's forbidden list, so the gate does not change here.

Readback: the Pod started; the budget files' SHA-256 inside the Pod equal the Secret's; the log has
no `worker_budget_authority_unavailable`. The budget authority does **not** start here, and is not
expected to until H7: it needs a profile source (H7's candidate or canary file, later H4b's
registry) as well as H6's Router connection. With no profile configuration at all Relay skips the
startup without any warning. The warning comes only from a failing profile load (`profiles-unavailable`)
or a failing `startWorkerBudgetAuthority`, which validates the ledger settings (`ledger-config-invalid`).
With no profile file and no candidate or canary authority the load returns nothing without failing
(Relay `server/workers/runtime-session/profiles.ts:430-433`), and the ledger start runs only
`if (workerProfiles)` (`server/main.ts:337`), so neither happens
(`deploy/worker-budget-ledger/README.md:118-121`: "An entirely absent worker-profile configuration
disables budget startup without that warning, even when ledger settings are present"). Its state
becomes visible with H6's metrics.

### H5. Escrow token

[relay-router-connection.md](relay-router-connection.md) step 9, unchanged: mint the
`relay-router-escrow` token, read it back, `relay-router-files` becomes Ready. Its 720 h clock starts
now, so H6 follows promptly.

[relay-researcher-remaining-live-steps.md](relay-researcher-remaining-live-steps.md) is the checklist
of the owner and operator live steps before H6, H5 and the H6 preconditions included. Its steps 1-2
(signer/trust creation and Router trust inclusion) are DONE; step 9 is this H5 escrow token. It also
records the decisions from ailab #1820 and the readbacks still required.

### H6. Router connection, control plane, monitoring

Preconditions: budget authority `ready=true` and the Router plugin enabled (budget runbook steps
8-9, Router connection runbook step 7); the `relay-control-metrics` collector Secret provisioned
([relay-control-monitoring.md](relay-control-monitoring.md)). G2 is decided: the control plane comes
with its full scope (section above).

One PR includes `relay-router-connection`, `relay-agent-control-plane`, `relay-control-monitoring` and
`relay-worker-monitoring`, and amends the activation gate (`RELAY_ROUTER_CONNECTIONS_FILE`,
`relay-router-connection`, `RELAY_AGENT_CONTROL_PLANE`, `relay-agent-control-plane`) and
`InclusionGate.test_relay_component_is_not_included` of `test_relay_router_connection.py`. It does
**not** add `RELAY_ENVIRONMENTS`, `relay-gitea-authority` or `relay-notifications`; the tests refuse
them next to the control plane.

Readback, as Router connection runbook step 10, plus:

- `GET /api/settings` (signed in): `agentControlPlane: true`, `environments: false`,
  `routerAccessRequests: true` (the implicit Router access manager), `workers: true`,
  `agentCommunication: true`;
- the Pod env has neither `RELAY_ENVIRONMENTS` nor `RELAY_GITEA_ISSUERS_FILE` nor
  `RELAY_NOTIFICATION_DESTINATIONS_FILE`;
- dev-worker connectors stay online across the rollout, and an existing relay-bus agent key still
  reads its inbox and sends (the control plane changes no key or connector authority);
- control health 200; the metrics scrape includes the worker and budget series; no
  `worker_budget_authority_unavailable` in the log;
- `GET /api/router-credentials/connections` lists `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`.

The budget authority is expected **not** to start at H6. Until H7 it has no profile source: no
candidate or canary file before H7 and no registry before H4b, so `loadWorkerRuntimeProfiles` returns
nothing and Relay never starts the authority (Relay `server/main.ts:337`). The ledger files of H4a are
configured, so the startup metric reads `start_failed`, not `started` (`server/main.ts:420`). That is
expected here and stays until H7. `RelayWorkerBudgetStartupFailed` (`relay-worker-monitoring`, `for: 2m`)
does not fire for it: since 2026-11-22 the rule fires only for a hub Pod that carries a candidate (its
`candidate-session-key` init container, read from kube-state-metrics). The metric must read `started` at
H7, where the budget-started readback is.

The installation itself can be approved any time after H3 (installation runbook I1-I10); a worker
session on it needs H6 and H7's candidate files. The first sessions are the qualification scenarios,
and those also need the qualification stage read back (installation runbook Q1-Q5: the internal TLS
terminators, then the slot's DNS-less egress stage); the enrollment-stage slot admits no Pod.

### H7. Candidate, canary and qualification files, each its own reviewed component and PR

Before the candidate files: Q5's readback, the candidate journal at `10.96.0.42`
([relay-worker-candidate-journal.md](relay-worker-candidate-journal.md)), and **budget ledger File
B**, step 7's reviewed `worker_budget.qualifications` row (the Router `ReviewedTokenPolicy` and its
canonical `tokenPolicyDigest`; plan in ailab #1753 comment 124071), committed and read back. The signed
candidate runs pin that digest (the run's `router.tokenPolicyDigests`), so File B comes before any run
is signed.

| Setting | When | Note |
| --- | --- | --- |
| `RELAY_WORKER_CANDIDATE_FILE`, `_SHA256`, `_SESSION_KEY_FILE` | the nineteen scenarios (delivery plan step 9) | needs the independent candidate journal, File B and a signed run |
| `RELAY_WORKER_ORDINARY_CANARY_FILE`, `_SHA256`, `_POLICY_FILE` | the finite canary (step 10), after the genuine release approval | a configured permit that cannot load stops Relay from starting |
| `RELAY_WORKER_QUALIFICATION_FILE`, `_SHA256` | general activation (step 11) | the registry of accepted evidence |
| `RELAY_WORKER_PROVENANCE_FILE`, `_SHA256` | not for the repository-free researcher | stays unset |

**The candidate component, `kubernetes/components/relay-worker-candidate` (staged, not included).**
Reviewed once here; its `run/` directory is generated per signed run by `scripts/relay-candidate-run.py`
from the run and the document Relay's `scripts/compose-candidate-hub-document.ts` composed for it
(owner decision O6, S-A; [relay-candidate-run-generator.md](relay-candidate-run-generator.md)):

- `RELAY_WORKER_CANDIDATE_FILE` = `/run/relay-worker-candidate/document.json`, a subPath of the run's
  immutable ConfigMap `relay-worker-candidate-<runDigest[:16]>` (`defaultMode: 0444`): a regular,
  single-link, `root:1000 0444` file, as Relay's `readCandidateFile(…, {trustedUid: 0})` requires;
- `RELAY_WORKER_CANDIDATE_SHA256` = sha256 of that document (canonical compact JSON, about 57 KB of the
  reader's 65,536 bytes);
- `RELAY_WORKER_CANDIDATE_SESSION_KEY_FILE` = `/run/relay-worker-candidate-key/session-key.pem`. The
  reader's `secret` rule (no group or other bit, owner root or UID 1000) cannot be met by any Secret
  volume under the Pod's `fsGroup`, so the init container `candidate-session-key` (UID 1000, the pinned
  node image already in the Pod) copies the key from the operator-created Secret
  `relay-worker-candidate-key-<runDigest[:16]>` with `install -m 0400` into a Memory `emptyDir`, which
  the relay container mounts read-only. Only that init container mounts the Secret.

**The first inclusion PR is drill run R0's per-run PR**: the generator's files for R0, plus
`- ../../../components/relay-worker-candidate` in `kubernetes/apps/apps/relay/kustomization.yaml` and
the H7 group removed from the activation gate. R0 suspends the agent and never grants or creates;
its reset removes the inclusion, so R1 includes H7 again
([relay-researcher-r0-relay-drills.md](relay-researcher-r0-relay-drills.md), §§6, 10).
After R1, each later run's PR changes only the generated files,
and CI (`test_relay_candidate_run.py`) fails unless they equal the generator's output for the committed
run; the merge gate is `scripts/relay-candidate-run.py check --base origin/main --run-digest <digest>`
(the generator on the base revision's files, and nothing else changed). Create the
run's session-key Secret before the merge: without it the Pod waits in `ContainerCreating`.

Readback of the candidate files, the budget-started readback that H6 cannot give: the init container
`candidate-session-key` exited 0 and the Pod's `RELAY_WORKER_CANDIDATE_SHA256` is the run's; the
metrics scrape shows budget startup `started`; `RelayWorkerBudgetStartupFailed` has cleared; the log has neither
`worker_budget_authority_unavailable` nor `worker_candidate_unavailable`. A document that does not
load only logs that warning and Relay starts without candidate authority (`server/main.ts`), so the
log line is the gate: never grant or create while it is there, and abort the run before the grant if
this readback is not done by `t_s + 60 min` (owner decision O6). The candidate authority is
the first profile source: `loadWorkerRuntimeProfiles` builds the runtime profiles from it alone, on an
empty registry (Relay `server/workers/runtime-session/profiles.ts:430-433`, the candidate or canary branch), and Relay then starts the
budget authority with H6's Router connection.

Then, in this order:

1. the nineteen scenarios, on signed candidate runs;
2. a **genuine `ReleaseApproval`**, which the operator issues only after the nineteen reports and the
   other release gates are independently accepted;
3. the finite canary, whose permit carries that approval. Relay's design removes the candidate
   authority before the canary (`docs/proposals/worker-candidate-qualification.md`); the permit is
   then the profile source, so budget startup stays `started`;
4. H4b, the profile registry, before the bounded general lifecycle trial.

### H4b. Profile registry, after the release approval

This step runs here, after the genuine release approval and before the bounded general lifecycle
trial. The canary does not need it. Why it cannot come earlier (Relay v0.10.3, `c0fe50f0`):

- **Every entry needs an approval that comes later.** Each registry entry carries a `ReleaseApproval`
  (`server/workers/runtime-session/profiles.ts:32`) whose `workerTemplates` subject asserts nine
  passed gates: `workerIsolation` and the eight shared ones (`packages/contracts/release-approval.ts`).
  Relay's design issues that approval only after the nineteen qualification reports and the other
  release gates are independently accepted (`docs/proposals/worker-candidate-qualification.md`, lines
  28 and 310 on Relay main). An empty registry is refused (`.min(1)`, `profiles.ts:40`).
- **The candidates and the canary do not read it.** A candidate session takes its profile and
  token-policy digests from the signed run (`profiles.ts:149`), the canary from its permit
  (`server/workers/ordinary-canary/authority.ts:283`).
- **The budget authority needs a profile source, but not this one.** With no registry and no candidate
  or canary file, `loadWorkerRuntimeProfiles` returns nothing (`profiles.ts:430-433`) and Relay never
  starts the budget authority (`server/main.ts:337`), so the startup metric reads `start_failed`
  (`server/main.ts:420`, `deploy/worker-budget-ledger/README.md`). H7's candidate file starts it on an
  empty registry.

Preconditions: the genuine `ReleaseApproval`; the qualified `tokenPolicyDigests`; the registry for the
pinned release reviewed (`relay-worker-profiles/kustomization.yaml` lists its content: tenant, sealed
researcher template digest, runtime digest, `modelPolicy`, the complete profile template with
`launcher-proxy-v1`, UID 1000, `/workspace`, channel `ae9b92bb-0b59-4987-b41f-376bd269db62`,
inference `{connectionId babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8, protocol codex-responses, selection
{kind: model, model: relay-researcher-luna}}` with `adaptedResponses {catalogModel gpt-6-luna,
maxOutputTokens 8192, reasoningEffort none}`, the release approval and the qualified
`tokenPolicyDigests`).

One PR: replace `profiles.json`, set `RELAY_WORKER_PROFILES_SHA256` to its `sha256sum`, include
`relay-worker-profiles`, remove `RELAY_WORKER_PROFILES_FILE` and `relay-worker-profiles` from the
activation gate's forbidden list. Readback: the mounted file's SHA-256 inside the Pod equals the pin;
the log has no `worker_budget_authority_unavailable` with reason `profiles-unavailable` (the registry
failed to load or validate); budget startup is still `started`.

### H8. v0.10.9: owner pool approval materializes the Router connection

Why: in v0.10.4 pool approval (`POST /api/worker-pools`) refuses any pool with an inference policy
unless an enabled `router_connections` row exists, and only Router credential issuance wrote that row;
issuance needs an approved pool. The first inference pool could never be approved (ailab #1753
comment 134379; live: `router_connections` and `agent_environment_pools` empty). Relay #569 (main
`69b1ae62`), released as v0.10.9 (#570, `04a897f2`, run 143446): in the approval transaction, under the
router-credentials lock taken after the environment lock, Relay inserts the row issuance would write
from the mounted connection record whose `tenants` include the approving tenant (`ON CONFLICT DO
NOTHING`), re-reads it and refuses on a disabled row or origin/digest drift. It never updates or
re-enables a row; a failed approval leaves none. No Router call, key, binding or budget change. Hub
only, no migrations (schema 79), so re-pinning v0.10.4 (`d1482bc1…57a7`) is a valid rollback; a rollback
does not remove a row or pool approved meanwhile.

Change: this PR pins the v0.10.9 digest in `migrate` and `relay`, with the annotations. One Recreate
roll by the merge itself (Flux), with the pre-migration backup; outside the QNAP windows.

Readback after the roll (operator):

- one Running Pod; source `04a897f22678cd59d2af3e01680a793bf63eb85f`, release
  `relay-0.10.9-release-143446`; the `relay` container's image ID ends in `sha256:1b0f2f729f78d4b166d9f6e6d36402df924ae2fe1481dbb5c486e9bb0ab37a39`; the migrate init
  container exited 0 and `schema_migrations` reads `79|79`; the env is unchanged from H6 (the
  generation file, installations, budget files, `RELAY_ROUTER_CONNECTIONS_FILE`,
  `RELAY_AGENT_CONTROL_PLANE`);
- the admin `GET /api/router-credentials/connections` lists `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`
  with origin `https://router.chifor.me`;
- `SELECT count(*) FROM router_connections` and `SELECT count(*) FROM agent_environment_pools` are
  still 0 (tenant-scoped; nothing is created by the roll).

After the owner approves the pool in their own session (owner rule, ailab #1799), tenant-scoped:

- exactly one `router_connections` row: tenant `00000000-0000-4000-8000-000000000001`, id
  `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`, name `ailab llm-router`, origin `https://router.chifor.me`,
  `enabled` true, `deployment_digest`
  `547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad` (Relay's `deploymentDigest` of
  the mounted record);
- the pool at revision 1 with `approved_by` the owner's `users.id` and its approval audit entry;
- credential, binding and issuance-operation counts unchanged by the approval. Pool approval does not
  establish worker readiness.

### H9. v0.10.16: agent state, Claude Code's state, Activity table, Needs you, Fleet, Terminals

Why: the console derived an agent's state from defaults (a pane became Idle on connect), not from
evidence.
- Relay #573, released as v0.10.10 (#574, `716c2ed8`, run 145785): a v0.10.10 connector sends each pane's
  terminal sample to a hub that advertises `activity`, and the hub shows Output observed, Quiet, No signal
  with its reason, or Offline, with source and age.
- Relay #575 with #576 and #580, released as v0.10.11 (#579, `12ea8d52`, run 145909): a v0.10.11
  connector adds Claude Code's own state, read from its session file, and the hub shows Working, Idle, or
  Waiting for a decision or an answer.
- Relay #581 with #583, released as v0.10.12 (#582, `c4601358`, run 146045), hub only: the Activity page
  on the shared data table, paged by scrolling, with the view in its address.
- Relay #577, released as v0.10.13 (#585, `5cd8c324`, run 146105), hub only: Needs you on the Status page, every
  pending request Relay can decide (hosts, agent keys, bus access, Router access), decided inline, with the header
  tray, personal snooze and claims.
- Relay #584 with #587, released as v0.10.14 (#586, `82ed6ff9`, run 146182), hub only: Needs you also lists
  terminal agents waiting on a person, Claude Code's open questions as Blocking.
- Relay #588 with #590, released as v0.10.15 (#589, `78f9a175`, run 146315), hub only: Fleet replaces the Agents
  page (the navigation entry "Agents" is "Fleet"; `#/agents` opens it): one group per host, every host listed,
  each agent with its observed state and Needs you, and a detail beside the list for an agent or a host.
- Relay #591 with #593 and #594, released as v0.10.16 (#592, `77920aff`, run 146394), hub only: Fleet's Terminals
  view (`#/terminals`, under Fleet) shows the fleet's terminals in a grid that fits the window, filtered like the
  list and those that need a person first; the order holds while the reader is in a terminal.

The samples live in the hub's memory; nothing is stored. Hub only in this step, no migrations (schema
79), same runtime, skill, tools and prerequisites as v0.10.9 (the deployment receipts agree), so
re-pinning v0.10.9 (`1b0f2f72…7a39`) is a valid rollback.

Before the merge (operator): the connector pins of ailab #1947 (0.10.10) and #1949 (0.10.11,
`docs/runbooks/relay-connectors.md`) are merged and v0.10.11 is converged on dev-worker-1, -2, -3 and -4,
one at a time, and on each `relay-connector status --json` reports 0.10.11, fresh and connected. Mixed
versions are safe both ways: a worker still on 0.10.10 shows terminal evidence without Claude Code's
state, and one older than 0.10.10 shows "No signal: update the connector".

v0.10.12 to v0.10.16 rebuild the connector (its code is v0.10.11's, only the version string differs), so
the dev-workers stay on the v0.10.11 pin; the hub serves both.

Change: this PR pins the v0.10.16 digest in `migrate` and `relay`, with the annotations. One Recreate
roll by the merge itself (Flux), with the pre-migration backup; outside the QNAP windows. H9 gates no
other step: H7 and the steps after it need only H8.

Readback after the roll (operator):

- one Running Pod; source `77920aff114e7d802edb190feeada64ecb8fcf73`, release
  `relay-0.10.16-release-146394`; the `relay` container's image ID ends in
  `sha256:bc60742499a6aa7cbd34f10f32940062b29f9de550dce131542642990d2436c2`; the migrate init container
  exited 0 and `schema_migrations` reads `79|79`; the env is unchanged from H8;
- the console's Fleet (its Agents page's address opens it) shows each dev-worker agent, grouped by host, with an
  observed state and its age (Output
  observed or Quiet), not "No signal: update the connector", and each Claude Code agent as Working, Idle
  or waiting, with "Claude Code <version>";
- Activity loads past its first page by scrolling, and a filtered address opens the same view in another
  browser;
- Status shows Needs you with its coverage line; a pending host or agent key request appears there and is decided
  from its Review; a permission question in a Claude Code terminal appears as "Waiting for a decision" and clears
  when it is answered there;
- Fleet lists every host as a group (a pending request by its installation, its name in its Review), and a host's
  detail shows its connection, scope and agents;
- Fleet's Terminals view shows the dev-workers' terminals, those that need a person first, and opens and controls
  one from its tile.

### H10. v0.10.25: the Assistant's action registry, every Assistant tool on it, its cards, its brief and its routes; Fleet as designed

Why: the console's changes and the Assistant's tools ran separate code, so the Assistant could not be held to the
console's checks, and in Auto it switched plugins without a click.
- Relay #595 with #597, released as v0.10.17 (#596, `a5fde4a5`, run 146573): host decisions, the plugin switch and
  Needs you's decisions (keys, bus access, Router access, snooze, claim) are one registered action each, which the
  console's routes and the Assistant's tools both dispatch. Each change holds the administrator's authority until
  it commits; Auto no longer applies authority actions (`/enable` and `/disable` show a card); the Assistant's
  model never reads text a requester wrote (pending hosts are named by id); the Apply bar shows the host in full.
- Relay #598 with #600, released as v0.10.18 (#599, `031f62a6`, run 146627): every remaining Assistant tool is a
  registered action too, and History's and the default model's routes dispatch the same actions. Terminal input
  never takes over another tab's lease, its card shows the exact input, and an unconfirmed delivery reads as
  uncertain. In Ask a rename shows a card; in Auto the default model asks, and images apply up to 4 per turn.
  Repairs: links that went nowhere, Router wording, agents' display names.
- Relay #601 with #603, released as v0.10.19 (#602, `dd5bd297`, run 146686): the Assistant draws cards from a closed
  catalog. Its first card is Needs you: "/attention", or "what needs me" in the guide, shows Status's own rows. The
  latest card acts while its list is current; an older one is a read-only snapshot with Refresh. The model never
  receives a card, only a counts-only sentence; clicks from a card are recorded with `surface: assistant-card`.
- Relay #604, released as v0.10.20 (#605, `a3ffa52b`, run 146743): the table card. "/agents", "/hosts",
  "/plugins" and recent activity show as Fleet's and Activity's own compact table, a row opening its page.
- Relay #607 with #611, released as v0.10.21 (#609, `dd3374c7`, run 146831): the lanes and session cards: the bus's
  lanes (the Lanes page's diagram) and a managed session's timeline, names and states only, never a message's or
  a prompt's text.
- Relay #608 with #611 and #613, released as v0.10.22 (#612, `4ea243c0`, run 146865): the terminal card: a selected
  terminal's name and state; its text only on "Show terminal text" (Operators and above, recorded), discarded
  when the terminal is deselected. Also a deterministic worker source journal test (the v0.10.20 release flake).
- Relay #610, released as v0.10.23 (#614, `36fb4510`, run 146949): the Assistant's brief is the attention queue
  (D4): its Needs you lists Status's own items with the same count as the badge, a failing source keeps its notice
  beside it, and an agent that goes offline again after it came back is a new notice.
- Relay #616 with #618, released as v0.10.24 (#620, `8f7f615b`, run 147126): route selection on every connection
  type (D3): in Router mode, Settings → Assistant lists the Router's routes and a conversation picks one. The mode
  has its own flag, `RELAY_ASSISTANT_ROUTER`; unset, it follows the control plane, so on this hub (no control plane)
  it stays off and nothing about the Assistant's model changes. #618: an inference connection that a newer Relay
  stored reads as disabled and is said so in Settings and at a turn's start, so a later rollback to this release
  is safe.
- Relay #624 with #626, released as v0.10.25 (#625, `175f9166`, run 147163): Fleet follows its design board (the
  list, its toolbar and View panel, one-line host headers, the agent row, the footer, the Needs you filter, a held
  order while the reader is in the list, the phone layout). Web only.

Hub only, no migrations (schema 79), no new settings. v0.10.25's deployment receipt equals v0.10.16's to
v0.10.24's in runtime, skill, tools and prerequisites (only the image, its source and run, the connector's hashes
and the manifest differ), so re-pinning v0.10.24 (`89f69cae…e037`), v0.10.22 (`9ea637c9…afb2`), v0.10.19 (`db277a54…b44d`), v0.10.18
(`ff85b992…fe06`), v0.10.17 (`e20b6def…ca0c`) or v0.10.16 (`bc607424…36c2`, H9) is a valid rollback. The
connector is rebuilt with the same code; the dev-workers stay on the v0.10.11 pin. H10 gates no other step.

Change: this PR pins the v0.10.25 digest in `migrate` and `relay`, with the annotations. One Recreate roll by the
merge itself (Flux), with the pre-migration backup; outside the QNAP windows.

Readback after the roll (operator):

- one Running Pod; source `175f916602526504a4542afa02847a22f69748ba`, release `relay-0.10.25-release-147163`;
  the `relay` container's image ID ends in
  `sha256:d98c6ce7c5578f40057ac6873f07276dbcb4a7c4ca4d9b604d0a407337a2e22e`; the migrate init container exited
  0 and `schema_migrations` reads `79|79`; the env is unchanged from H9;
- the features' production checks (the owner, in the console):
  - D1a: decide one host request from Fleet or Needs you and another from the Assistant (ask it to approve the
    host, then Apply). In Activity, both `connector` events carry `action: host.authorize`, one with
    `via: console`, the other with `via: assistant`;
  - D1b: ask the Assistant to rename the conversation, then Apply its card. In Activity, the `assistant` event
    carries `action: conversation.rename` with `via: assistant`;
  - D2a: type "what needs me" in the Workspace guide (or `/attention`): the Needs you card lists the same items as
    Status. Approve a host request from the card: in Activity, its `connector` event carries
    `action: host.authorize`, `via: console` and `surface: assistant-card`;
  - D2b: ask the Assistant for the agents (or `/agents`), the lanes, a managed session and a selected terminal.
    Each shows as its card: the agents' table lists Fleet's rows; the lanes card the Lanes page's lanes; the
    session card the session's steps without prompt text; the terminal card the terminal's name and state, and its
    text only after "Show terminal text" (that read is recorded in Activity);
  - D4: start a new conversation: the brief's Needs you lists the same items, with the same count, as Status's
    Needs you and the sidebar's badge;
  - D3: Settings → Assistant shows the same connection as before and no Router routes (Router mode is off here);
  - C1: Fleet shows one list of agents grouped by host, as `design/Fleet.html` draws it (four columns, one Open
    action, View with grouping, density and columns, the Needs you filter); on a phone a host's Review is in its ⋯.

### H11. v0.11.1: the v0.11 line, schema 88

Why: release/v0.10 stops at schema 79, and every console feature with a migration ships from main. v0.11.1 is main
(159c8051) as the owner chose it over v0.11.0 as cut, released through Relay #638, #640 and #643 (merge parts) and
#637 (bump), `3678b8ed`, run 147466:
- migrations 080-088: the managed runtime's records (080-087) and 088, whose owner-rights guards refuse a blind
  lookup under another tenant's context (with #635's review items);
- the managed runtime work, D3 and D3b-1, release Phase A;
- the console work already live in v0.10.25 (D4, Fleet as designed), plus Terminals as designed (C2) and the new shell
  behind `?console=new` (F0a, with the top bar's tiers, #641). The kill switch's engager is looked up only while it is
  engaged (#642).

Rehearsal (operator, on the published digest `d0bbbc58…fbbc` against a fresh production restore), all PASS:
- migrate 79→88 in 1.2 s;
- 088's checks: 13 guarded writes refused without the tenant context, with identical outcomes when it is set;
- the fence on seeded VM rows in 2 tenants (environments 2, no 23514);
- `recovery:verify` before and after;
- inert for 10 minutes with production's flags;
- the console against v0.10.25;
- a rollback drill to v0.10.25.

v0.11.0's rehearsal also showed two things. With `lock_timeout=10s`, a blocked migration fails 55P03 in 11 s; that
migration rolls back, and the ones before it stay committed. The fleet's v0.10.11 connectors work with a v0.11 hub,
and the reverse pairing works too. The dev-workers stay on the v0.10.11 pin.

Change: this PR pins the v0.11.1 digest in `migrate` and `relay`, with the annotations. It also gives `migrate`
`PGOPTIONS=-c lock_timeout=10s`, so a migration behind a lock fails (55P03) instead of waiting. Each migration commits
in its own transaction, so the schema is then at the last migration that committed (read `schema_migrations`): in
v0.11.0's rehearsal, 087 blocked and left 080-086 committed (schema 86). The kubelet retries the failed `migrate` with
backoff, and each retry resumes from the next uncommitted migration: clearing the lock is enough, so do NOT force a
second rollout. If the Pod is recreated instead, `backup-database` runs again and publishes a generation of the partly
migrated schema; that is why a rollback restores this roll's EARLIEST generation. v0.10.25 does not support a partial
schema either, so a rollback is the restore below, never a re-pin. The fence comment says what rollback really is. The
merge itself rolls the hub once (Recreate, through Flux), with the pre-migration backup, outside the QNAP windows. No
new settings.

Rollback: forward-only from here. An older image is not refused: v0.10.25 starts against schema 88, so re-pinning it
alone would run it on a schema it does not support. Rollback to v0.10.25 (`d98c6ce7…e22e`) takes four steps, in order:
1. Restore this roll's **earliest** pre-migration generation (the first `backup-database` log of the v0.11.1
   rollout).
2. Run `recovery:fence` with a **fresh** generation.
3. Put that generation in `relay-restore-fencing`.
4. A reviewed PR re-pins v0.10.25.

The cost, measured in the rehearsal's drill:
- every connector and agent key is revoked (4 and 8), so each host and agent enrolls again;
- every session ends (49);
- the undelivered outbox is deleted;
- a new generation ConfigMap is written;
- writes after the backup are lost.

Readback after the roll (operator):

- one Running Pod; source `3678b8ed882efc48c8ce8d75d043896d7d285eaa`, release `relay-0.11.1-release-147466`;
  the `relay` container's image ID ends in
  `sha256:d0bbbc58392ac75db0c1d08ed4cfb90aade846ae415898bdf209e40f31c8fbbc`;
- the backup init container exited 0 (record its generation: it is the rollback's restore point), the migrate init
  container exited 0 with `PGOPTIONS` set, and `schema_migrations` reads `88|88`;
- the `relay` container's env is unchanged from H10;
- the features' production checks (the owner, in the console):
  - the classic console still works, with H10's checks;
  - Status, Fleet and Terminals load; Terminals follows `design/Terminals.html` (one tile per terminal, Focus for
    one);
  - `?console=new` opens the new shell (sidebar, top bar), and every page in it works;
  - Activity and the connectors are unaffected: the dev-workers' hosts reconnect on v0.10.11 and their agents show
    observed states.

### H12. v0.11.4: health tiles with history, schema 89

Why: Status had no record of the platform's health over time; the owner asked for health as discrete bars with
history. Relay #649 (B4) with its review fixes (#653), released as v0.11.4 (#652, `225b7747`, run 147982), on top of
v0.11.3 (live since 2026-11-24, ailab #1979: Activity Events and the native Pi inference work):
- a sampler in the hub writes one sample a minute per source (Relay platform, Hosts, Bus, Agents, Control plane,
  Inference) per tenant, only from an observation made in that minute, with its own small pools (app max 2, owner
  max 1), statement and query timeouts and a 50 s tick deadline;
- `GET /api/health/history?window=4h|24h|7d` for every member (check details for administrators);
- Status shows the six tiles with 48 bars each, after Needs you.

Migration 089 (schema 88 → 89):
- the `health_samples` table, with forced row security, kept seven days by the sampler;
- `CREATE INDEX agent_messages_expiry ON agent_messages(tenant_id, expires_at)`, under `lock_timeout 10s`. The build
  holds SHARE on `agent_messages` for its duration; production's table is small, so expect well under a second;
- `relay_app` gets SELECT, INSERT and DELETE on `health_samples`.

Check before the merge (operator), recorded on the PR (ailab #1984, the operator's check comment), PASS when all hold:
- the published digest `4f30750e…97c9` (deployment-images-147982.json and the image's revision label) migrates the
  latest production generation (here 20261124T162049Z-bfa30aad, schema 88) to schema 1..89, `migrate` exit 0, with
  its time recorded (1.1 s);
- `health_samples` has forced row security and its policy; grants: owner SIUD, app SID, backup S;
- the `agent_messages_expiry` index built, with the table's row count recorded (1,665 rows);
- the hub on that copy writes samples each minute (9 rows in 3 minutes).

Change: this PR pins the v0.11.4 digest in `migrate` and `relay`, with the annotations, and moves the fence comment to
schema 89. `migrate` keeps `PGOPTIONS=-c lock_timeout=10s` (H11). The merge itself rolls the hub once (Recreate,
through Flux), with the pre-migration backup, outside the QNAP windows. No new settings; `RELAY_HEALTH_SAMPLING=0`
would turn the sampler off and is not set.

Rollback: forward-only, as H11. v0.11.3 starts against schema 89 and recovery's checks demand exactly 1..89, so a bare
re-pin is never a rollback. Rollback to v0.11.3 (`2c350aee…8a2b`) = restore this roll's **earliest** pre-migration
generation (the first `backup-database` log of the v0.11.4 rollout), `recovery:fence` with a **fresh** generation kept
in `relay-restore-fencing`, then a reviewed PR re-pins v0.11.3; H11's cost applies (every connector, agent key and
session; the undelivered outbox; a new generation ConfigMap; writes after the backup lost).

Readback after the roll (operator):

- one Running Pod; source `225b7747f8875da1ccc4e9badccae46c252e0abe`, release `relay-0.11.4-release-147982`;
  the `relay` container's image ID ends in
  `sha256:4f30750efe6715f86f7cb1028d20833cfd74788baafbe466e05f86dd78bb97c9`;
- the backup init container exited 0 (record its generation: the rollback's restore point), the migrate init container
  exited 0, and `schema_migrations` reads `89|89`; the `relay` container's env is unchanged from v0.11.3;
- after two minutes, `health_samples` has a row per source in use for the minute. Forced row security reads empty
  without the tenant, so read it as the postgres superuser, or in one transaction:
  `BEGIN; SET LOCAL relay.tenant_id = '00000000-0000-4000-8000-000000000001'; SELECT source, max(sampled_at) FROM
  health_samples GROUP BY source; COMMIT;`;
- the owner, in the console: Status shows Health after Needs you, with the six tiles, their bars filling from the
  right, and a tile's Details opening under the grid.

### H13. v0.11.5: the Status page as the board draws it, no migration

Why: the console redesign's B5. Status becomes the board's page: Needs you, then Health, then the fleet as a tile map
grouped by host, with its keyboard and pointer hold. Relay #654 (B5) with its review fixes (#656), and the health
fixes of #653, released as v0.11.5 (#655, `34aeb51e`, run 148170), on top of v0.11.4 (live since 2026-11-24,
ailab #1984, H12).

No migration: schema stays 89, and the relay env is unchanged. No new settings.

Change: this PR pins the v0.11.5 digest `f8e2acc8…30ba` in `migrate` and `relay`, with the annotations. `migrate` runs
and finds nothing to apply. The merge rolls the hub once (Recreate, through Flux), with the pre-migration backup as
every roll, outside the QNAP windows.

Rollback: the schema is unchanged, so a reviewed PR re-pins v0.11.4 (`4f30750e…97c9`); no restore or fence. Below
v0.11.4 the H12 chain applies.

Readback after the roll (operator):

- one Running Pod; source `34aeb51ebe037162145f3251b1a23178a42895df`, release `relay-0.11.5-release-148170`; the
  `relay` container's image ID ends in `sha256:f8e2acc8ed4c4eaa6f38e907564a3435e6319aff44526c6de3131c9cb8da30ba`;
- the backup and migrate init containers exited 0, and `schema_migrations` still reads `89|89`; the relay env is
  identical to v0.11.4's;
- health samples keep arriving each minute (H12's query);
- the owner, in the console: Status shows Needs you, Health, then the fleet map grouped by host; a tile opens its agent.

### H14. v0.11.6: the single-agent page, no migration

Why: the console redesign's C1b. Each agent gets its own page at `#/fleet/agents/<id>`: Terminal first (the live tile,
focus on the page's own address, the agent's Needs you items), Overview (the reported task, today's events and
exceptions as Activity counts them) and Details (the shared facts, edit and delete). Status, Activity, Fleet, the
tile's menu and the Assistant link to it. Relay #657, released as v0.11.6 (#659, `eae6709c`, run 148372), on top of
v0.11.5 (live since 2026-11-24, ailab #1989, H13).

No migration: schema stays 89, and the relay env is unchanged. No new settings.

Change: this PR pins the v0.11.6 digest `8adca17a…0b8e` in `migrate` and `relay`, with the annotations. The merge
rolls the hub once (Recreate, through Flux), with the pre-migration backup as every roll, outside the QNAP windows.

Rollback: a reviewed PR re-pins v0.11.5 (`f8e2acc8…30ba`); no restore or fence. Below v0.11.4 the H12 chain applies.

Readback after the roll (operator):

- one Running Pod; source `eae6709c9d4bd537b68601f9baa4cd79f1a03812`, release `relay-0.11.6-release-148372`; the
  `relay` container's image ID ends in `sha256:8adca17a954913f3bd920dfc5fbba1343c1570a724a3f2171715fe4956270b8e`;
- the backup and migrate init containers exited 0, and `schema_migrations` still reads `89|89`; the relay env is
  identical to v0.11.5's;
- health samples keep arriving each minute (H12's query);
- the owner, in the console: an agent's name on Status's map opens `#/fleet/agents/<id>` with Terminal first; Focus
  opens the terminal full-size and Exit focus returns to the tab.

### H15. v0.12.0: the console preference and Needs you message problems, schema 91

Why: the console redesign's F0b and B3b. F0b: Settings → Appearance → Console (Classic | New), saved per person and
followed across devices; the `?console=` address switch stays as an override. B3b: withheld, undeliverable,
expired-unread and backlog message problems in Needs you, each with Open thread, in a new Failing tier. Relay #658
(F0b), #661 (B3b) and #660 (the agent page's release-review notes), released as v0.12.0 (#662, `11ac8f72`, run
148517). This is the first release from `release/v0.12`, cut from main at `738711a6`, on top of v0.11.6 (live since
2026-11-24, ailab #1990, H14).

Migrations 090 and 091 (schema 89 → 91):
- 090: the `user_preferences` table: forced row security, policy `tenant_isolation`, a composite reference to
  `memberships(tenant_id, user_id)` with `ON DELETE CASCADE`; `relay_app` gets SELECT, INSERT and
  UPDATE(console, updated_at) only;
- 091: two partial indexes on `agent_message_recipients`, under `lock_timeout 10s`. The builds hold SHARE on that table
  (writes wait, reads don't); production's table is small, so expect milliseconds.

Content parity: no v0.11.x fix regressed. The operator checked every release/v0.11 change against `11ac8f72`: 8
reverse-apply cleanly, 8 are present as their main merge commits, and the rest are release merges
(https://git.chifor.me/cchifor/ailab/pulls/1993#issuecomment-141543).

Check before the merge (operator), recorded on the operator's message of 2026-11-24 21:09Z and on this PR, PASS (13/13):
- the published digest `e63d533f…c364` (deployment-images-148517.json and the image's revision label) migrates the
  latest production generation (20261124T194107Z-5a59ca38, schema 89) to schema 1..91 contiguous, `migrate` and
  bootstrap as `relay_migrator` with `lock_timeout 10s`, rc 0 in 1.07 s including the container start; 090 and 091
  applied 12 ms apart;
- `agent_message_recipients` has 1,844 rows (memberships 2), so the index builds take milliseconds;
- `user_preferences`: owned by `relay_migrator`, row security forced, policy `tenant_isolation`, the foreign key to
  memberships with cascade; grants: owner SIUD, app S + I + UPDATE(console, updated_at) (no table UPDATE, no
  `tenant_id` update, no DELETE), backup S;
- both partial indexes present; 137 tables, all owned by the migrator;
- the v0.12.0 hub on that copy answered `/ready` 200 in about 4 s, and the sampler wrote all six sources.

Change: this PR pins the v0.12.0 digest in `migrate` and `relay`, with the annotations, and moves the fence comment to
schema 91. `migrate` keeps `PGOPTIONS=-c lock_timeout=10s` (H11). The merge rolls the hub once (Recreate, through
Flux), with the pre-migration backup, outside the QNAP windows. No new settings.

Rollback: forward-only, as H12. v0.11.6 starts against schema 91 and recovery's checks demand exactly 1..91, so a bare
re-pin is never a rollback. Rollback to v0.11.6 (`8adca17a…0b8e`) = restore this roll's **earliest** pre-migration
generation (the first `backup-database` log of the v0.12.0 rollout), `recovery:fence` with a **fresh** generation
kept in `relay-restore-fencing`, then a reviewed PR re-pins v0.11.6; H11's cost applies.

Readback after the roll (operator):

- one Running Pod; source `11ac8f72efc6edb0f820e02db67eeaf6351e33da`, release `relay-0.12.0-release-148517`; the
  `relay` container's image ID ends in
  `sha256:e63d533fae058a5674d705ad86a0c57cc5ca421d677dc710ad7023ebc10dc364`;
- the backup init container exited 0 (record its generation: the rollback's restore point), the migrate init container
  exited 0, and `schema_migrations` reads `91|91`; the `relay` container's env is unchanged from v0.11.6;
- health samples keep arriving each minute (H12's query);
- the owner, in the console: Settings → Appearance shows Console with Classic and New; choosing New on one device opens
  the new shell on another after a reload (F0b's done-when). Needs you lists a withheld message within 25 s plus query
  time, made with a short-lived test key that is then revoked, and opens its thread (B3b's done-when).

### H16. v0.12.1: Buses, schema 92

Why: the console redesign's C3a. Fleet gets a Buses tab: the list, the detail, Create bus (general buses), and member
management (add, pause, resume, remove, change default bus, replace key, revoke). Pausing a parent on a bus, or
turning a bus off, pauses the parent's open sub-agents on that bus with it, so the 60 s sweep no longer retires them.
The 13 bus actions are registered for the Assistant with their risk tiers. Relay #663, with the release-review fixes
#665, #667, #668 and #669, released as v0.12.1 (#664 and #666, `dfc739a4`, run 148784) on top of v0.12.0 (live since
2026-11-24, ailab #1993, H15).

Migration 092 (schema 91 → 92):
- `agent_channels`: `type` (NOT NULL, default `general`) and `description` (NOT NULL, default empty);
- `agent_channel_members`: `changed_at`, `changed_by` and `paused_with`, nullable. No constraint ties `paused_with` to
  `enabled` (deliberate: the previous image's Resume must not fail on a rollback);
- no rows written; row security and grants unchanged.

Check before the merge (operator), recorded on the operator's message of 2026-11-25 03:28Z, PASS:
- the published digest `177c1af2…a350f` (deployment-images-148784.json and the image's revision label) migrates the
  nightly generation 20261125T001502Z-a93067e5 (schema 91) to schema 1..92 contiguous; `migrate` and bootstrap rc 0 in
  1.14 s including the container start;
- `agent_channels.type` and `description` present, with both existing rows reading `general` and empty;
  `agent_channel_members.changed_at`, `changed_by` and `paused_with` present and NULL on all 8 rows;
- the CHECKs as written; row security forced on both tables, unchanged; 137 tables, all owned by the migrator;
- the v0.12.1 hub on that copy answered `/ready` 200 in about 4 s, and the sampler wrote.

Change: this PR pins the v0.12.1 digest in `migrate` and `relay`, with the annotations, and moves the fence comment to
schema 92. `migrate` keeps `PGOPTIONS=-c lock_timeout=10s` (H11). The merge rolls the hub once (Recreate, through
Flux), with the pre-migration backup, outside the QNAP windows. No new settings.

Rollback: forward-only, as H15. Restore this roll's **earliest** pre-migration generation (the first
`backup-database` log of the v0.12.1 rollout), `recovery:fence` with a **fresh** generation kept in
`relay-restore-fencing`, then a reviewed PR re-pins v0.12.0 (`e63d533f…c364`); H11's cost applies. If the previous
image is ever run against schema 92 before that restore, make **no bus or membership changes** (pause, resume, turn
off) until the roll-forward: it has no sub-agent cascade, so a parent's pause would retire its sub-agents again.

Readback after the roll (operator):

- one Running Pod; source `dfc739a492217ea5a8dfaf2471aead43b2ea646c`, release `relay-0.12.1-release-148784`; the
  `relay` container's image ID ends in
  `sha256:177c1af20e5708af2ceb01e4e6360bd0b08a4859f50edad3784257ef74ea350f`;
- the backup init container exited 0 (record its generation: the rollback's restore point), the migrate init container
  exited 0, and `schema_migrations` reads `92|92`; the `relay` container's env is unchanged from v0.12.0;
- health samples keep arriving each minute (H12's query);
- the owner, in the console (C3a's done-when): Fleet → Buses lists `workspace`; create a bus "Operations", add a keyed
  agent, pause it and resume it; the detail shows the change with its time and who made it.

## Enablement order at a glance

> **Campaign rows are historical.** The owner retired the researcher qualification campaign and its
> candidate journal on 2026-11-22 ([ailab #1753 comment 136889](https://git.chifor.me/cchifor/ailab/issues/1753#issuecomment-136889)).
> The `journal`, H7, `scenarios`, `approval` and `canary` rows, and H4b's dependence on the genuine approval,
> are kept as the record and are not actionable; the rows before them describe the live hub, ledger and installation.

| Step | Change | Must already hold | Readback that gates the next step |
| --- | --- | --- | --- |
| H1 | none (backup) | – | verified restore of a fresh generation |
| H2 | hub image → release | H1 | schema N contiguous, installations 404 |
| H3 | `recovery:initialize` (receipt `kind: "initial"`), then `RELAY_RECOVERY_GENERATION_FILE` + `RELAY_INFRASTRUCTURE_INSTALLATIONS` | H2, the initializer in the release | one active `initial` row, baseline counts unchanged, `workers: true` |
| H4a | `RELAY_WORKER_BUDGET_*` only (budget client) | ledger Secret Ready | budget files' SHA-256 equal the Secret's; no `worker_budget_authority_unavailable` |
| H5 | escrow token | H4a | `relay-router-files` Ready |
| H6 | `RELAY_ROUTER_CONNECTIONS_FILE` + `RELAY_AGENT_CONTROL_PLANE` + monitoring; no environments, Gitea or notifications | ledger `ready=true`, plugin on | `agentControlPlane: true`, `environments: false`, connectors and bus keys working, connection listed. Budget startup `start_failed` is expected: no profile source until H7 |
| I1-I10 | the researcher installation (enrollment-stage egress) | H3; worker sessions need H6 | installation `ready`, fence UID pinned |
| Q1-Q6 | internal TLS terminators (`relay-internal-tls`), then the slot's qualification egress stage (Q6 is the drill configuration) | I10; the release's G3 binding and CA pins | Q5: both Certificates Ready and chaining to the pinned root, Services `10.96.0.40`/`.41`, live policy digest `f6d3187c…282f`. The third alias, the candidate journal's Service at `10.96.0.42`, is not created here: it comes with the journal itself ([relay-worker-candidate-journal.md](relay-worker-candidate-journal.md)) before H7's candidate files |
| journal | RETIRED 2026-11-22 (ailab #1753 comment 136889): the independent candidate journal ([relay-worker-candidate-journal.md](relay-worker-candidate-journal.md)) is no longer under Flux | none | none: no Service on `10.96.0.42`, which stays reserved |
| File B | budget ledger step 7's `qualifications` row (ailab #1753 comment 124071) | the final Router rollout read back | the stored policy re-verified, its `tokenPolicyDigest` recorded for the signed runs |
| H8 | hub image → v0.10.9 (pool approval materializes the Router connection) | the roll: H4a's state only (no new settings); its connection readback and the owner's pool approval: H6 (applied) | v0.10.9 digest running, schema still 79, connection listed, `router_connections` and pools still empty; then the owner approves the pool and exactly one connection row appears |
| H7 | HISTORICAL, campaign retired 2026-11-22 (ailab #1753 comment 136889): candidate files: `relay-worker-candidate`, first included for R0 (never granted), reset, then included again for R1; each run's values generated (the canary file follows the approval; the qualification file is general activation, step 11) | H6, H8 with the owner-approved pool (one connection row), Q5's readback, the journal at `10.96.0.42`, File B, the run's session-key Secret | init container exit 0, the run's pin in the Pod, budget startup `started`, `RelayWorkerBudgetStartupFailed` cleared, no `worker_budget_authority_unavailable` or `worker_candidate_unavailable` |
| scenarios | HISTORICAL, campaign retired 2026-11-22: the nineteen qualification scenarios | H7's candidate files | the nineteen reports, independently accepted |
| approval | HISTORICAL, campaign retired 2026-11-22: a genuine `ReleaseApproval` | the nineteen reports and the other release gates accepted | its `workerTemplates` subject asserts the nine passed gates |
| canary | HISTORICAL, campaign retired 2026-11-22: H7's canary file | the approval (its permit carries it) | the canary's reviewed report |
| H4b | HISTORICAL while the campaign is retired (needs its genuine approval): `RELAY_WORKER_PROFILES_FILE`/`_SHA256` (profile registry) | the genuine approval, the qualified `tokenPolicyDigests`, registry reviewed | file pin equal; no `profiles-unavailable`; budget still `started`. Before the bounded general lifecycle trial |
| H9 | hub image → v0.10.16 (agent state, Claude Code's state, Activity table, Needs you, Fleet, Terminals); gates no step above | every dev-worker connector on v0.10.11 (ailab #1947, #1949, read back per worker); H8's state only (no new settings) | v0.10.16 digest running, schema still 79, agents show observed states and Claude Code's, Activity pages by scrolling, Needs you lists pending requests |
| H10 | hub image → v0.10.25 (the Assistant's action registry, every Assistant tool on it, its cards, its brief and its routes; Fleet as designed); gates no step above | H9 live | v0.10.25 digest running, schema still 79, a host decided from the console and one from the Assistant both read `action: host.authorize`, an Assistant rename reads `action: conversation.rename`, and a host approved from the Needs you card reads `surface: assistant-card`, in Activity, and the agents, lanes, session and terminal cards show (the terminal text only on request), and the brief's Needs you matches Status |
| H11 | hub image → v0.11.1 (schema 88: migrations 080-088; Terminals as designed; the new shell behind `?console=new`); `PGOPTIONS` lock timeout on `migrate`; gates no step above | H10 live; the operator's rehearsal of the published digest PASS | v0.11.1 digest running, `schema_migrations` `88\|88`, the backup generation recorded, the console's checks pass, the dev-workers reconnected |
| H12 | hub image → v0.11.4 (schema 89: health samples; health tiles on Status); gates no step above | v0.11.3 live (ailab #1979); the operator's 88 → 89 check PASS | v0.11.4 digest running, `schema_migrations` `89\|89`, the backup generation recorded, health samples written each minute, the tiles on Status |
| H13 | hub image → v0.11.5 (Status as the board draws it, the fleet map; no migration); gates no step above | H12 live | v0.11.5 digest running, `schema_migrations` still `89\|89`, Status shows Needs you, Health and the fleet map |
| H14 | hub image → v0.11.6 (the single-agent page; no migration); gates no step above | H13 live | v0.11.6 digest running, `schema_migrations` still `89\|89`, an agent's page opens from Status with Terminal first |
| H15 | hub image → v0.12.0 (schema 91: migrations 090-091; the console preference per person; Needs you message problems); gates no step above | H14 live; the operator's 89 → 91 check PASS | v0.12.0 digest running, `schema_migrations` `91\|91`, the backup generation recorded, the console preference follows the owner across devices, a withheld message shows in Needs you |
| H16 | hub image → v0.12.1 (schema 92: migration 092; Buses in Fleet); gates no step above | H15 live; the operator's 91 → 92 check PASS | v0.12.1 digest running, `schema_migrations` `92\|92`, the backup generation recorded, a bus created and a member paused and resumed from Fleet → Buses |

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
- **H3 after initialization:** the generation row cannot be removed, and removing the generation
  file makes Relay refuse to start against it, so `relay-restore-fencing` stays. To abandon workers,
  revert only `relay-infrastructure-installations` (Relay keeps the generation check and runs without
  the worker APIs). Going back to v0.9.1 is a restore, and every restore, including one of the
  pre-initialization H2 generation, goes through `recovery:fence` with a **fresh** generation (v0.9.1
  has its own `recovery:fence` for its schema 56) and keeps `relay-restore-fencing` with that new
  generation. That fence retires the restored database's connectors, keys, sessions and memberships:
  it is the cost of restoring, not of initializing.
- **H4a, H6 and H4b:** revert that step's inclusion PR; nothing in the database changes.
- **H11 (schema 88):** a bare re-pin is never a rollback: v0.10.25 starts against schema 88 and would run on a schema
  it does not support. Restore the v0.11.1 rollout's **earliest** pre-migration generation (the first
  `backup-database` log) with the H2 mechanics above, run `recovery:fence` with a **fresh** generation and keep it in
  `relay-restore-fencing`, then a reviewed PR re-pins v0.10.25 (`d98c6ce7…e22e`). Cost, rehearsed: every connector
  and agent key revoked (4 and 8), every session ended (49), the undelivered outbox deleted, a new generation
  ConfigMap; writes after the backup are lost.
- **H16 (schema 92):** as H15: never a bare re-pin. Restore the v0.12.1 rollout's **earliest** pre-migration
  generation, `recovery:fence` with a **fresh** generation kept in `relay-restore-fencing`, then a reviewed PR re-pins
  v0.12.0 (`e63d533f…c364`); H11's cost applies. No bus or membership changes while an older image runs on schema 92.
- **H15 (schema 91):** as H12: never a bare re-pin. Restore the v0.12.0 rollout's **earliest** pre-migration
  generation, `recovery:fence` with a **fresh** generation kept in `relay-restore-fencing`, then a reviewed PR re-pins
  v0.11.6 (`8adca17a…0b8e`); H11's cost applies.
- **H14 (schema 89, no migration):** a reviewed PR re-pins v0.11.5 (`f8e2acc8…30ba`); no restore or fence.
- **H13 (schema 89, no migration):** a reviewed PR re-pins v0.11.4 (`4f30750e…97c9`); no restore or fence.
- **H12 (schema 89):** as H11: never a bare re-pin. Restore the v0.11.4 rollout's **earliest** pre-migration
  generation, `recovery:fence` with a **fresh** generation kept in `relay-restore-fencing`, then a reviewed PR re-pins
  v0.11.3 (`2c350aee…8a2b`); H11's cost applies.
- **H8:** a reviewed PR re-pins v0.10.4 (`d1482bc1…57a7`); schema 79 is unchanged. A connection row or
  pool approved under v0.10.9 stays, and v0.10.4 accepts it (its check only reads the row).
- **H7's candidate files:** the next run's PR replaces a run's values; before the canary one PR drops
  the component and resets `run/` and the agent's candidate values (`scripts/relay-candidate-run.py reset`). Removing the file stops
  new candidate admission; Relay keeps the candidate rows it already wrote. Before
  reverting H6 after any issuance, revoke worker credentials through Relay and let escrow cleanup
  finish (Router connection runbook, "Rollback"). Reverting the last profile source (H4b once H7's
  candidate and canary files are gone) returns budget startup to `start_failed`, and Relay then has
  no worker inference authority; H7's files are rolled back as Relay's own documents say.
- **After installation enrollment:** retire through the installation runbook first; removing hub
  components never revokes issued keys or reconciles workers.

## What still blocks a web-UI-created researcher

1. **Release and binding prerequisites are complete.** The published hub, agent and runtime pins
   are listed above; the sealed template and `PodRuntimeBinding` are in
   `docs/relay-workers/researcher-i4/`. The binding places the researcher with
   `kubernetes.io/hostname: relay-worker-1`. These are not remaining blockers.
2. **H5-H7 and the later profile registry remain.** The registry needs the genuine release approval
   and comes after qualification (H4b).
3. **Model and Router:** paid qualification of `gpt-6-luna` on `openai-2` through
   `relay-researcher-luna` (owner), the rolled Router read back, and the budget authority `ready=true`
   with the `clients` row's `max_tokens` = 7441536 (I = 922,000 already accepted, relay#277 comment
   122095; Router connection runbook, "Router trust mapping").
4. **Qualification egress (G3):** the internal TLS terminators and qualification egress are applied
   (Q1-Q5, starting state above); the pinned binding carries the hostAliases and CA pins. Recheck Q5
   before a scenario. The candidate journal was retired on 2026-11-22 (ailab #1753 comment 136889): no
   Service holds `10.96.0.42`, which stays reserved in `docs/network-plan.md`.
5. **Qualification 0/19, no canary:** the slot keeps zero Pods and the deny-all gate until the nineteen
   rows are accepted, the genuine release approval is issued, the canary passes, H4b's registry is in
   and the activation compiler's outputs (guards, gate replacement, one-Pod quota,
   `ordinaryNamespacePins`, `bootstrapSecrets: true`) are reviewed and committed. Only then does the
   Workers page offer a researcher that can start.
