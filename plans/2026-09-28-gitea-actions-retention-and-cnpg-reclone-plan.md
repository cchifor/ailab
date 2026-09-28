# Gitea Actions run retention + CNPG lost-slot auto-reclone

## Context

ailab#923 (2026-09-28, four critical alerts): `infra-pg`'s replica lost its replication slot
(`wal_status=lost`, `wal_removed`) and could never rejoin; CNPG neither notices nor repairs this, so it
was re-cloned by hand. Two measured causes:

1. **Gitea's database commits ~344/s around the clock** — Actions log streaming through DBFS from
   ~9,000 jobs/day — and Gitea keeps Actions **run history forever** (`LOG_RETENTION_DAYS` purges only
   logs/artifacts): `action_task` 5.4 GB (292 k rows since 2026-07-09), `action_task_step` 3.5 M rows,
   `action_run_job` 1.2 GB; the gitea DB is 12 GB of `infra-pg`'s 20 Gi data+WAL volume (5.3 GB free).
   `cchifor/platform` alone has **36,735** runs (36,706 completed); the `@midnight`
   `cron.cleanup_actions` purge did 3.5 M deletes in 01:00–02:30Z, which is when the replica lost the race.
2. The replica ran on a control plane at 91–95 % requested (replay lag 100–300 s before the purge).

Not done, on purpose: raising `max_slot_wal_keep_size` — `kubernetes/apps/databases/infra-pg.yaml`
documents why 4 GB buys ~6 min at the measured WAL peaks on a volume that also holds the data.
Platform-side PR-workflow fan-out is already bounded: 6 of the 8 PR workflows carry
`concurrency`/`cancel-in-progress` (only the two small guards do not) — noted on #923, no PR.

## Approach

### 1. Bound the Actions run history — CronJob `gitea-actions-run-retention` (namespace `gitea`)

`kubernetes/apps/apps/gitea/actions-run-retention.yaml` (+ `kustomization.yaml` entry) and a SOPS
Secret `gitea-actions-retention.sops.yaml` (`token`), encrypted by the repo's default `*.sops.yaml`
rule (`data|stringData`, recipient `age1nfa6…`).

- **API (Gitea 1.26.1, verified live):** `GET /orgs/cchifor/repos` (needs `read:organization`),
  `GET /repos/{o}/{r}/actions/runs?status=completed&limit=50&page=N` (newest first; response
  `{total_count, workflow_runs[]}`; a run has `id, status, started_at, completed_at, repository.full_name`
  — there is **no `created_at`**), `DELETE /repos/{o}/{r}/actions/runs/{id}` (cascades run → jobs →
  tasks → steps → logs). `/admin/actions/runs` is site-admin-only and not used.
- **Token:** a PAT for `gitea_admin` named `actions-run-retention`, scopes exactly
  `read:organization,write:repository`, minted once with
  `gitea admin user generate-access-token --raw` inside the Gitea pod into a mode-600 file and
  encrypted straight into the SOPS Secret — never printed, never in argv (the runner uses
  `Authorization: token …` from the env of the job container only). Base URL is the in-cluster
  service `http://gitea-http.gitea.svc.cluster.local:3000` (no Cloudflare, no UA rule).
- **Logic (bash + jq + curl, image `docker.io/alpine/k8s:1.32.13@sha256:…` as the other ailab
  CronJobs):** for each org repo (plus `EXTRA_REPOS`), walk the completed runs **from the last page
  backwards** (oldest first: page = ceil(total/50), decreasing); a run is eligible iff
  `status == "completed"` and `completed_at` is non-null and older than `RETENTION_DAYS` (14 — the
  same number as `ARTIFACT/LOG_RETENTION_DAYS`); stop at the first page that holds no eligible run
  (they are ordered). Never touch `queued`/`running`/`waiting`. **Pacing is the point:** at most
  `MAX_DELETES_PER_RUN` (200) per execution, `sleep 0.5` between deletes, hourly schedule
  (`17 * * * *`) → ≤4,800/day, i.e. the ~35 k backlog drains over ~a week in small WAL slices
  instead of one purge, and steady state (~1,600 runs/day) is a few minutes per hour. `DRY_RUN=true`
  prints what it would delete; the first merged version ships with `DRY_RUN=false` only after one
  manual dry run is pasted on #923.
- Idempotent, safe to overlap-forbid (`concurrencyPolicy: Forbid`, `activeDeadlineSeconds: 1500`),
  restricted-PSA compliant (runAsNonRoot 65534, seccomp RuntimeDefault, drop ALL), resources
  50m/64Mi. No RBAC — it only talks to the Gitea API.
- Housekeeping note in the manifest: deleting rows returns space as dead tuples that autovacuum
  reuses in-file; the 12 GB does not shrink on disk without a `VACUUM FULL`/`pg_repack` window —
  growth stops, the free-space cliff moves out; a later decision.

### 2. Self-healing for a lost slot — CronJob `cnpg-lost-slot-reclone` (namespace `databases`)

`kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml` (+ `kustomization.yaml` entry): SA, Role,
RoleBinding, CronJob every 5 min (`*/5 * * * *`, `Forbid`).

- For every `clusters.postgresql.cnpg.io` in the namespace: read `.status.currentPrimary` and
  `.status.phase`; skip unless phase is `Cluster in healthy state` (never act during a switchover,
  a join, or an upgrade). Run **on the primary pod** (`kubectl exec -c postgres -- psql -U postgres`
  — local trust, no superuser Secret needed):
  `select slot_name from pg_replication_slots where wal_status='lost'`.
- Slot → instance: CNPG names slots `_cnpg_<cluster with - as _>_<n>` → pod `<cluster>-<n>`. Guards:
  the instance pod must exist, must not be `currentPrimary`, must carry
  `cnpg.io/instanceRole=replica`; the slot's `active` must be false. Then exactly what was done by
  hand on #923: `kubectl delete pvc <inst> <inst>-wal --ignore-not-found --wait=false` and
  `kubectl delete pod <inst> --wait=false`; CNPG bootstraps a NEW instance number via a join job.
  **At most one re-clone per execution**, and a `kubectl annotate cluster … ailab.io/last-reclone=<ts>`
  so the next execution sees phase ≠ healthy anyway. An Event is emitted on the Cluster
  (`kubectl create event` is not portable across kubectl versions — use the annotation + job log).
- RBAC (Role in `databases` only): `clusters.postgresql.cnpg.io` get/list/patch(annotate);
  `pods` get/list/delete; `pods/exec` create; `persistentvolumeclaims` get/list/delete. Nothing
  cluster-scoped.
- Why safe: a `lost` slot means the standby cannot ever stream again (no WAL archive here); its data
  is disposable by construction; the primary is never touched; the guards keep it from acting on a
  primary, a healthy replica, or during any transition. The qnap-iscsi `Retain` policy leaves a
  Released PV each time — the job logs the PV name; cleanup stays with the existing
  `tridentctl delete volume` + `kubectl delete pv` recipe (a follow-up could add it here once
  Trident's own delete path is trusted after ailab#880).
- `DRY_RUN` env, default `true` for the first merge; flipped to `false` in a follow-up commit after
  one execution's log shows the correct detection with no action (the current cluster is healthy, so
  the first proof is the negative case; the positive path was exercised by hand today).

### 3. Verification

1. `kustomize build kubernetes/apps/apps/gitea` and `kubernetes/apps/databases` render; Flux applies both.
2. Retention: `kubectl -n gitea create job --from=cronjob/gitea-actions-run-retention dry-1` with
   `DRY_RUN=true` → log lists the oldest eligible runs per repo and the count it would delete
   (expect ~200 platform runs from July); then `DRY_RUN=false` → the runs are gone from the API
   (`GET /repos/cchifor/platform/actions/runs?status=completed&page=<last>` shifts), infra-pg
   `pg_stat_replication` keeps a streaming replica throughout (lag < 60 s), `pg_replication_slots`
   stays `reserved`, the gitea DB's `action_task`/`action_task_step` row counts fall by the deleted
   runs' share. After a week: platform total_count ≈ 14 days' worth (~22 k at 1.6 k/day).
3. Re-clone job: with the cluster healthy the log prints "no lost slots" each run; annotate nothing.
   Positive path (already exercised by hand): documented on #923 with the exact commands the job runs.
4. Both CronJobs pass the `restricted` PSA (no warnings on apply) and appear in `kubectl get cronjobs`.

## Critical files

- `kubernetes/apps/apps/gitea/actions-run-retention.yaml`, `gitea-actions-retention.sops.yaml`, `kustomization.yaml`
- `kubernetes/apps/databases/cnpg-lost-slot-reclone.yaml`, `kustomization.yaml`
- `docs/runbooks/qnap-storage-setup.md` §9 gains one line pointing at the auto-reclone; `docs/runbooks/gitea-*` (if one exists) gains the retention note.

<!-- codex-review-status: pending -->
