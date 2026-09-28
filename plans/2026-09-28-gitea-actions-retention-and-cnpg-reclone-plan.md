# Gitea Actions run retention + CNPG lost-slot auto-reclone

## Context

ailab#923 (2026-09-28, four critical alerts): `infra-pg`'s replica lost its replication slot
(`wal_status=lost`, `wal_removed`) and could never rejoin; CNPG neither notices nor repairs this, so it
was re-cloned by hand. Two measured causes:

1. **Gitea's database commits ~344/s around the clock** — Actions log streaming through DBFS from
   ~9,000 jobs/day — and Gitea keeps Actions **run history forever** (`LOG_RETENTION_DAYS` purges only
   logs/artifacts): `action_task` 5.4 GB (292 k rows since 2026-07-09), `action_task_step` 3.5 M rows,
   `action_run_job` 1.2 GB; the gitea DB is 12 GB of `infra-pg`'s 20 Gi data+WAL volume (5.3 GB free).
   The `@midnight` `cron.cleanup_actions` purge did 3.5 M deletes in 01:00–02:30Z, which is when the
   replica lost the race.
2. The replica ran on a control plane at 91–95 % requested (replay lag 100–300 s before the purge).

Measured on 2026-09-28 11:20Z (gitea DB, `action_run` with `status IN (success, failure, cancelled,
skipped)` and `stopped` older than 14 d): **eligible backlog 33,045 runs** — platform 25,425 of 36,775,
ailab 4,055 of 7,057, agentforge 2,290 of 3,065, agentforge-platform 1,257 of 1,937, cloudlab 18 of 74;
the four newest repos have nothing older than 14 d. Runs age past the cutoff at **~1,934/day** (the
count in the 14–15 d band). The `_cnpg_infra_pg_5` slot currently reports `safe_wal_size` 1.08 GB,
i.e. the whole `max_slot_wal_keep_size` budget: at the documented 9.7–12.9 MiB/s WAL peaks that is
**80–106 s** of retained WAL, which is the margin every deletion burst has to respect.

Not done, on purpose: raising `max_slot_wal_keep_size` — `kubernetes/apps/databases/infra-pg.yaml`
documents why the proposed 4 GB was rejected (5–7 min at peak on a volume that also holds the data).
Platform-side PR-workflow fan-out is already bounded: 6 of the 8 PR workflows carry
`concurrency`/`cancel-in-progress` (only the two small guards do not) — noted on #923, no PR.

## Approach

### 1. Bound the Actions run history — CronJob `gitea-actions-run-retention` (namespace `gitea`)

Files: `kubernetes/apps/apps/gitea/actions-run-retention.yaml` (CronJob + `configMapGenerator` entry
in `kustomization.yaml` for the script), `scripts/gitea-actions-run-retention.sh` (the script — a
file, not an inline heredoc, so `scripts/tests/gitea-actions-run-retention-mock.py` can run it against
a fake Gitea and Prometheus), `gitea-actions-retention.sops.yaml` (Secret `gitea-actions-retention`,
key `token`) plus `gitea-actions-retention.sops.yaml.example` in the `ci-rerun-watchdog` shape (plaintext
shape + the exact mint commands, never the value).

**What Gitea actually does on DELETE (verified in 1.26 source, `services/actions/cleanup.go:175`
`DeleteRun`):** refuses non-done runs (API 400 "this workflow run is not done"), then in ONE transaction
deletes `action_run`, `action_run_job`, `action_task`, `action_task_step`, `action_task_output`,
`action_artifact` rows (plus `CleanupEphemeralRunners`), then — outside the transaction, best-effort,
errors only logged — removes each task's log (DBFS rows or the storage file) and each artifact's
storage file. So a storage failure during DELETE would orphan files, exactly the trap
`actions-storage.yaml` records. **Resolution: `RETENTION_DAYS=16 > LOG_RETENTION_DAYS=ARTIFACT_RETENTION_DAYS=14`.**
The nightly cleanup has already expired the logs (`log_expired=true`, files gone) and artifacts
(status 4 = expired) of anything this job deletes, so the storage half of `DeleteRun` is a no-op for
them (live: 199 k of 204 k eligible tasks already expired; the 5.5 k stragglers are what the disposable-run
test below covers, and the Gitea log is checked for `remove artifact file` / `Failed to remove log`
after the canary).

- **Identity (not `gitea_admin`):** a dedicated non-admin user `actions-retention` (`gitea admin user
  create --random-password --must-change-password=false`), member of a new org team
  `actions-retention` in `cchifor` whose only unit is **Actions = write**, `includes_all_repositories`
  (created once through the admin API with the break-glass `gitea-admin` basic-auth Secret read from the
  cluster — no admin PAT minted). The PAT `actions-run-retention` is minted for THAT user with scopes
  `read:organization,write:repository`: `write:repository` on a user who only has the Actions unit is
  bounded by the team, and `/orgs/cchifor/repos` needs `read:organization`. Setup commands live in the
  `.example` file; the token is pasted into the plaintext example copy and `sops --encrypt --in-place`d.
- **Credential handling:** the token reaches the container as env `GITEA_TOKEN`; the script writes a
  mode-600 curl config (`header = "Authorization: token …"`) into the tmpfs `/tmp`, `unset`s the
  variable, and every curl call uses `--config`; there is no `set -x` anywhere. Error paths print HTTP
  codes and the first 160 bytes of the response body, never a request. **Transport assumption,
  stated:** plain HTTP to `gitea-http.gitea.svc:3000` inside the cluster, the same path
  `ci-rerun-watchdog` and the reviewbot use; SOPS protects the committed Secret, Cilium's pod-network
  boundary is what protects the bearer in flight. No NetworkPolicy exists in the `gitea` namespace
  today (verified), so none is added for a job that only needs Gitea and Prometheus.
- **Repository discovery:** paginate `GET /orgs/cchifor/repos?limit=50&page=N` until an empty page,
  add `EXTRA_REPOS`, dedupe (`sort -u`). Log the repo list once per execution.
- **Selection — no early stop, bounded scan, revalidate before DELETE:** listing is `id DESC`
  (verified: tail page of platform is ids 7, 6, 5 from 2026-07-09), and `id` is creation order, so every
  eligible run (completed_at < cutoff ⇒ created < cutoff) has a lower id than every run created after
  the cutoff: eligible runs cluster at the TAIL, ineligible low-id runs exist only as reruns (an old run
  rerun recently keeps its id; `completed_at` moves). The walk therefore starts at page
  ceil(total/50) and moves towards page 1 for at most `MAX_PAGES_PER_REPO` (20 = 1,000 runs) pages,
  collecting candidates where `status == "completed"`, `completed_at != null`, `completed_at < cutoff`
  (one UTC epoch computed once per execution), deduped by id; ineligible runs on a tail page are
  skipped, not a stop signal. A page is re-read after deletions from it (a deletion shifts the pages)
  and offset pagination is NOT a snapshot: a run that shifts onto an already-visited page is missed
  **this** execution and picked up by the next — **eventual coverage across hourly executions is the
  accepted contract**, and the mock test has a scenario where completions arrive between page reads.
  Immediately before each DELETE the run is re-read (`GET /repos/{o}/{r}/actions/runs/{id}` — exists in
  1.26, verified live) and skipped unless still `completed` and older than the cutoff; the server's own
  `IsDone` check is the second guard.
- **Budget is GLOBAL, not per repo:** `MAX_DELETES_PER_RUN` counts across all repos; repos are
  processed in rotating order (start index = hour-of-day mod repo count) so a big backlog in one repo
  cannot starve the others forever; repos not reached are logged as deferred.
- **Pacing by measurement, not by run count alone:** `DELETE_PAUSE_SECONDS` between deletions, and a
  **replication gate** re-evaluated before the first deletion and after every `GATE_EVERY` (25)
  deletions: `GET http://kube-prometheus-stack-prometheus.monitoring.svc:9090/api/v1/query?query=
  count(ALERTS{alertstate="firing",alertname=~"CNPG.*|PostgresReplica.*|PostgresReplicationLagHigh"})`.
  Any firing alert, a non-200, or unreachable Prometheus ⇒ **stop deleting (fail closed), exit 2** —
  the KubeJobFailed rule (present in the kps `kubernetes-apps` group, verified) is the escalation. This
  is also the cross-job exclusion: while a re-clone is running the replica alerts are firing, so
  retention pauses. Schedule `17 3-23 * * *` — **never during the 01:00–02:30Z cleanup burst**.
  Initial numbers: canary `MAX_DELETES_PER_RUN=25`, `DELETE_PAUSE_SECONDS=5`; the canary records
  `pg_current_wal_lsn()` deltas and slot `safe_wal_size` before/after, and the steady numbers (target
  300 per execution, 3 s pause ⇒ ~15 min of work spread through each hour, 21 executions/day = 6,300/day,
  net ~4,400/day against ~1,934/day ageing-in ⇒ backlog gone in ~8 days) are set in a follow-up commit
  **from the measured per-deletion WAL**, not assumed.
- **Failure policy (explicit):** `backoffLimit: 0`, `restartPolicy: Never`, `concurrencyPolicy: Forbid`,
  `activeDeadlineSeconds: 1500`, `startingDeadlineSeconds: 900`, `--max-time 60` per request. 401/403 on
  any call ⇒ exit 1 immediately (never "0 deleted, success"); 429/5xx/timeout on a list ⇒ that repo is
  skipped and reported; 429/5xx/timeout on a DELETE ⇒ counted against the budget (outcome uncertain) and
  the execution stops with exit 1; 404 on DELETE ⇒ "already gone", not counted; malformed JSON ⇒ exit 1.
  Every execution ends with one summary line `retention: deleted=N already_gone=N skipped_revalidation=N
  candidates_seen=N repos_deferred=… gate=ok|paused` — the log is the record. A manual Job is created
  with the intended DRY_RUN/limits already in ITS template (`kubectl create job --from=cronjob/… -o yaml
  --dry-run=client | edit env | apply`), because editing the CronJob never changes a Job that exists;
  stopping an active Job = `kubectl delete job`, stopping the schedule = `kubectl patch cronjob …
  -p '{"spec":{"suspend":true}}'` (both in the runbook).
- Restricted PSA: `runAsNonRoot` 65534, `seccompProfile RuntimeDefault`, `allowPrivilegeEscalation:
  false`, `capabilities drop ALL`, `readOnlyRootFilesystem`, `automountServiceAccountToken: false`
  (no Kubernetes API use at all), no init containers. Verified not by "apply had no warnings" but by
  running a manual dry-run Job and confirming the pod was admitted and the script ran as uid 65534.
- Housekeeping note in the manifest: deleted rows come back as dead tuples that autovacuum reuses
  in-file; the 12 GB does not shrink on disk without a `VACUUM FULL`/`pg_repack` window — growth stops,
  the free-space cliff moves out; a later decision. Acceptance is **"no eligible run older than
  cutoff + 48 h"** (measured with the same SQL as the backlog), not a target `total_count`.

### 2. Self-healing for a lost slot — CronJob `cnpg-lost-slot-reclone` (namespace `databases`)

Files: `kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml` (SA, Role, RoleBinding, CronJob,
`configMapGenerator` for the script), `scripts/cnpg-lost-slot-reclone.sh`,
`scripts/tests/cnpg-lost-slot-reclone-mock.py` (fake `kubectl` on PATH + fixture slot files, ~12
scenarios), `kubernetes/apps/databases/infra-pg-slotwatch.sops.yaml` (basic-auth Secret for the new
managed role), `infra-pg.yaml` gains the role and a corrected comment, and
`scripts/tests/fixtures/cnpg-reclone-drill.yaml` (the disposable drill cluster, applied by hand for
verification, never listed in a kustomization).

- **Scope: allowlist, not namespace-wide.** `CLUSTERS="infra-pg"` (env). Only infra-pg's topology (2
  instances, no WAL archive, shared data+WAL volume, Retain storage) has been examined; another cluster
  is added to the list only after its own recovery conditions are checked. The Role's `clusters` rule
  carries `resourceNames: ["infra-pg"]`.
- **Read the slot state WITHOUT `pods/exec` and WITHOUT superuser:** an init container `probe`
  (image `ghcr.io/cloudnative-pg/postgresql:17.11`, the cluster's own image, so `psql` is present and
  already on every node) connects to `infra-pg-rw.databases.svc:5432` — the service CNPG points at the
  CURRENT primary — as a new managed role `slotwatch` (`login: true`, nothing else: `pg_replication_slots`
  is readable by every role; no `pg_monitor`, which would expose other sessions' SQL) with
  `PGSSLMODE=verify-ca` against the mounted `infra-pg-ca`. It writes `/work/slots.tsv`:
  `select pg_is_in_recovery(); select slot_name, slot_type, active, wal_status, safe_wal_size,
  invalidation_reason from pg_replication_slots` — every field the decision uses. psql failure ⇒ the
  init container fails ⇒ the Job fails (KubeJobFailed), which is distinct from "no lost slots".
  `pg_is_in_recovery() = t` (the rw service pointed at a non-primary mid-switchover) ⇒ exit 3, no action.
  The RBAC rule for `pods/exec` is gone; the SA keeps `clusters` get/list/patch (resourceName-bound —
  `patch` is needed for annotations and does allow spec changes; accepted and documented),
  `pods` get/list/delete, `persistentvolumeclaims` get/list/delete, all namespace-scoped.
- **Slot ↔ instance mapping from the cluster's own instance list, not by parsing:** for the cluster
  read `.status.instanceNames`, `.status.currentPrimary`, `.status.phase`, `.spec.instances`,
  `.spec.replicationSlots.highAvailability.slotPrefix` (live: `_cnpg_`, enabled) and compute the expected
  slot name per instance (`prefix + name with '-'→'_'` — CNPG 1.24's `replicationSlots` naming; Cluster
  names cannot contain `_`, so the map is exact). A slot from the probe is actionable only if it maps to
  exactly one instance AND `slot_type=physical` AND `active=f` AND `wal_status=lost`. Unmapped or
  non-physical slots are logged and ignored.
- **Guards, re-checked immediately before mutation (check-to-delete race narrowed to one API
  round-trip):** phase == `Cluster in healthy state`; instance is not `currentPrimary`; pod exists with
  label `cnpg.io/instanceRole=replica`; no `ailab.io/reclone-in-progress` annotation; `ailab.io/last-reclone`
  older than `MIN_INTERVAL_SECONDS` (21600 — at most 4 re-clones/day, a finite budget); then re-read
  `currentPrimary`, phase and the pod's role one more time and abort on any change. **Residual race,
  documented not denied:** CNPG could start a switchover in the window between the last re-read and the
  delete; the operator offers no maintenance lock a job can take (`kubectl cnpg destroy` is this same
  pod+PVC deletion, not a locked procedure). With 2 instances the lost-slot replica is also the only
  failover candidate and is weeks stale — promoting it would be data loss, so its removal costs no
  usable HA.
- **Mutation and completion protocol:** record the instance's PVC names, UIDs and PV names
  (`<inst>`, plus `<inst>-wal` if present) BEFORE acting; annotate
  `ailab.io/reclone-in-progress=<utc>/<inst>` (in-progress marker); `kubectl delete pvc … --wait=false`
  then `kubectl delete pod <inst> --wait=false` (PVC first, pod second — waiting for the PVC before the
  pod would deadlock on pvc-protection); then wait, bounded (150 s), until the pod is gone and every
  recorded PVC UID is gone; log the Released PV names for the tridentctl recipe. Timeout ⇒ exit 1 with
  the marker still set. Verification is NOT the same execution's job: later executions see the marker
  and only observe — when phase is healthy, `.status.instanceNames` again equals `spec.instances`, the
  old instance is absent, the probe shows no lost slot and every physical slot `active=t`, they clear
  the marker and set `ailab.io/last-reclone=<utc>/<inst>`. A marker older than `STUCK_AFTER_SECONDS`
  (2700) that is still unverified ⇒ exit 1 every 5 min until an operator clears it (KubeJobFailed is the
  page). A crash between delete and marker cannot double-act: the marker is written before the delete.
  The new instance is a new number (CNPG's monotonic counter, seen 07-21 and today: 4 → 5).
- **Conflict reconciled:** `infra-pg.yaml:70` says "`kubectl cnpg destroy <cluster> <n> --keep-pvc`".
  `--keep-pvc` keeps the STALE data directory and re-creates the same instance on it; a standby whose
  slot is `lost` cannot recover from that directory, so the flag is wrong for this failure — the 09-08 DR
  plan's wording came from a review that wanted the plugin, not from a lost-WAL recovery. What healed
  it on 07-21 and 09-28 is deletion of pod AND PVCs (= `kubectl cnpg destroy` without `--keep-pvc`).
  The comment in `infra-pg.yaml` is corrected in this change; the dated plan is history and stays.
- `DRY_RUN` env, default `true` on merge for BOTH jobs.

### 3. Rollout order (both jobs dry-run first; retention canary while auto-reclone stays off)

1. Merge with `DRY_RUN=true` on both. Confirm each CronJob's first pod is admitted under restricted
   PSA and the scripts run (uid 65534, tmpfs writable).
2. **Re-clone positive path in isolation, before it can ever act on infra-pg:** apply the drill fixture
   `reclone-drill` (CNPG Cluster, 2 instances, 1 Gi `local-path`, `max_slot_wal_keep_size: 64MB`, its
   own `slotwatch` Secret) in `databases`; break the standby with a CiliumNetworkPolicy that denies
   its egress to 5432 (Cilium is the CNI; `kubectl delete pod` would just come back); churn WAL on the
   primary past 64 MB (`generate_series` inserts + `pg_switch_wal()` + `checkpoint`) until the slot
   reports `lost`; remove the policy (the standby reconnects and is refused — the real symptom); run a
   manual Job from the CronJob template with `CLUSTERS=reclone-drill DRY_RUN=false PGHOST=reclone-drill-rw`
   and verify: exactly the lost instance's PVC+pod deleted, marker set, join job, new instance number,
   physical slot `active=t wal_status=reserved`, marker cleared by the next execution; then re-run the
   scenarios "active slot", "primary's own slot", "marker present", "phase not healthy", "last-reclone
   too recent" and prove no deletion. Delete the drill cluster and its PVCs.
3. Retention canary: manual Job `MAX_DELETES_PER_RUN=25 DELETE_PAUSE_SECONDS=5 DRY_RUN=false` while
   watching, on the primary, `pg_current_wal_lsn()` before/after (bytes per deletion), the slot's
   `safe_wal_size`, `pg_stat_replication.replay_lag`, and Prometheus `cnpg_pg_replication_lag`; also the
   Gitea log for storage-removal errors. Before it, pick one disposable RECENT run (a `renovate` run
   with logs still present), delete it by hand through the same API and confirm its task log files /
   DBFS rows and artifact rows are gone — the cascade proof codex asked for, on a run whose files still
   exist. Record exact candidate ids from the canary log and confirm those ids 404 while the newest
   runs remain.
4. Enable retention (`DRY_RUN=false`, measured `MAX`/pause) and watch two full nights across the
   01:00–02:30Z cleanup. If retention ever costs a lost slot: suspend the CronJob and investigate — do
   NOT lean on auto-reclone to hide an unsafe rate.
5. Only then enable auto-reclone (`DRY_RUN=false`) — the last flip, because an automatic destructive
   action must not paper over a bad deletion rate.

### 4. Verification (byte-based, not "streaming and lag < 60 s")

- Mock tests pass (`python scripts/tests/gitea-actions-run-retention-mock.py`,
  `python scripts/tests/cnpg-lost-slot-reclone-mock.py`): retention — pagination across page shifts,
  completions arriving between page reads, ineligible partial tail page followed by eligible pages,
  null `completed_at`, exact boundary, rerun (revalidation skips), 401 ⇒ exit 1, DELETE 5xx ⇒ stop,
  global budget across repos, gate paused ⇒ exit 2, token only ever in the Authorization header;
  reclone — every guard above plus custom slot prefix, missing pod/PVC, separate WAL PVC, timeout
  waiting for deletion, marker present, stuck marker ⇒ exit 1, probe says in-recovery ⇒ exit 3.
- `kustomize build` of both directories renders; Flux applies; `scripts/tests/test_manifest_paths.py`
  unchanged (no new Kustomization path).
- Live: the numbers from step 3 pasted on #923 — WAL bytes per deleted run, peak `safe_wal_size` drop,
  replay lag, free space on the 20 Gi volume before/after, and after 48 h the eligible-backlog SQL
  trending to zero. Job success alone is not evidence; the summary line and the SQL are.

## Critical files

- `kubernetes/apps/apps/gitea/actions-run-retention.yaml`, `gitea-actions-retention.sops.yaml` (+ `.example`), `kustomization.yaml`
- `scripts/gitea-actions-run-retention.sh`, `scripts/tests/gitea-actions-run-retention-mock.py`
- `kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml`, `infra-pg-slotwatch.sops.yaml`, `infra-pg.yaml` (managed role + comment), `kustomization.yaml`
- `scripts/cnpg-lost-slot-reclone.sh`, `scripts/tests/cnpg-lost-slot-reclone-mock.py`, `scripts/tests/fixtures/cnpg-reclone-drill.yaml`
- `docs/runbooks/infra-pg.md` (new: the 30-second check, both jobs, how to stop/suspend, the tridentctl PV cleanup pointer)

<!-- codex-review-status: complete -->
