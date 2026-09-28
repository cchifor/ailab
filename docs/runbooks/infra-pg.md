# infra-pg — the shared CNPG Postgres (gitea, authelia, litellm, grafana, open-webui, agentforge)

Manifests: `kubernetes/apps/databases/` (Flux Kustomization `databases`). The Cluster is
`infra-pg` (2 instances, 20 Gi shared data+WAL on `qnap-iscsi`, `max_slot_wal_keep_size: 1GB`).
Context: `kubectl --context admin@ai -n databases`.

## The 30-second health check (after ANY drain, failover, or replica alert)

Readiness is a pod probe; a standby with no walreceiver is 1/1 Ready and "healthy" forever. Ask
Postgres, on the PRIMARY (`kubectl get cluster infra-pg -o jsonpath='{.status.currentPrimary}'`):

```sql
select slot_name, active, wal_status, safe_wal_size from pg_replication_slots;  -- want active=t, reserved
select application_name, state, replay_lag from pg_stat_replication;             -- one streaming row per replica
```

`wal_status=lost` (or an empty `pg_stat_replication`) = the replica is dead, whatever k8s says.
There is no WAL archive (#595), so a lost slot is permanent: the instance must be re-cloned.

## Re-clone a dead standby (by hand)

Delete the instance's PVC(s) and pod — NOT `kubectl cnpg destroy --keep-pvc` (that keeps the stale
data directory and re-creates the same dead standby). CNPG then joins a NEW instance number:

```sh
kubectl -n databases delete pvc infra-pg-N infra-pg-N-wal --ignore-not-found --wait=false
kubectl -n databases delete pod infra-pg-N --wait=false
kubectl -n databases get cluster infra-pg -w        # "Creating a new replica" -> healthy, ~10 min for 12 GB
```

`qnap-iscsi` reclaims with `Retain`: the old PV stays `Released` and its zvol/LUN stays on the NAS.
Remove both through Trident (a bare `kubectl delete pv` would orphan the LUN — the #880 pile):

```sh
kubectl -n databases get pvc                                   # confirm the claim is gone
kubectl get pv | grep Released                                  # the old instance's PV name(s)
kubectl -n trident exec deploy/trident-controller -c trident-main -- tridentctl -n trident delete volume <pv-name>
kubectl delete pv <pv-name>                                     # only if Trident left it behind
```

## Automatic re-clone — CronJob `cnpg-lost-slot-reclone` (2026-09-28, ailab#923)

`cnpg-lost-slot-reclone.yaml` + `cnpg-lost-slot-reclone.sh` (tests:
`python scripts/tests/cnpg-lost-slot-reclone-mock.py`). Every 5 min an init container probes the
primary through `infra-pg-rw` as the LOGIN-only managed role `slotwatch`; the main container acts only
on a physical, inactive, `lost` slot that maps to a replica of an allowlisted cluster (`CLUSTERS`),
never on the primary, never during a transition, at most once per `MIN_INTERVAL_SECONDS` (6 h).

Protocol on the Cluster's annotations:

| annotation | meaning |
|---|---|
| `ailab.io/reclone-in-progress=<utc>/<inst>` | written BEFORE the delete; while present the job only observes |
| `ailab.io/last-reclone=<utc>/<inst>` | stamped when a later execution verified the replacement (instances complete, old instance gone, no lost slot, all physical slots active); the marker is removed at the same time |

A marker unverified after `STUCK_AFTER_SECONDS` (45 min) makes every execution exit 1 — `KubeJobFailed`
is the page. Look at the join job / new pod, fix, then clear the marker yourself:
`kubectl -n databases annotate cluster infra-pg ailab.io/reclone-in-progress-`.

- Dry run (the merged default): `DRY_RUN=true` in the CronJob logs the decision only. Flip it in git
  only after run-retention has run safely — it must not hide a bad deletion rate.
- Drill (done 2026-09-28, repeatable): `scripts/tests/fixtures/cnpg-reclone-drill.yaml` (disposable
  2-instance cluster on local-path, 64 MB slot budget) + `cnpg-reclone-drill-isolate.yaml` (the break)
  + `build-reclone-drill-job.py` (a throwaway Job wired to the drill cluster's own credentials, CA and a
  Role scoped to it — the real CronJob is never modified). The full order is in the fixture header.
- Stop: `kubectl -n databases patch cronjob cnpg-lost-slot-reclone -p '{"spec":{"suspend":true}}'`.
- Manual execution against infra-pg carries its own env: `kubectl -n databases create job
  --from=cronjob/cnpg-lost-slot-reclone reclone-manual-1 --dry-run=client -o yaml`, edit `DRY_RUN`,
  apply. Editing the CronJob never changes an existing Job. Concurrent executions cannot both act: the
  marker is taken with a resourceVersion precondition.

## Why the replica dies: Gitea's Actions history — CronJob `gitea-actions-run-retention`

The gitea database (12 GB on 2026-09-28) commits ~344/s from Actions log streaming and keeps run history
forever; the nightly `cron.cleanup_actions` purge is what pushed the replica past the 1 GB slot budget.
`kubernetes/apps/apps/gitea/actions-run-retention.yaml` + `gitea-actions-run-retention.sh` (tests:
`python scripts/tests/gitea-actions-run-retention-mock.py`) deletes COMPLETED runs older than
`RETENTION_DAYS` (16, deliberately > `LOG/ARTIFACT_RETENTION_DAYS` 14 so Gitea's best-effort file
removal on DELETE has nothing left to orphan), hourly outside 00–02Z, with a global per-execution budget,
a pause between deletions, and a replication gate: any firing `CNPG*`/`PostgresReplica*` alert — or an
unreachable Prometheus — pauses it (exit 2). Identity: the non-admin Gitea user `actions-retention`
(org team with the Actions unit only; `gitea-actions-retention.sops.yaml.example` has the recipe).

- Progress: the last line of each Job's log,
  `retention: deleted=N already_gone=N skipped_revalidation=N candidates_seen=N repos_deferred=… gate=…`.
- Backlog (run on the primary, database `gitea`):
  ```sql
  select count(*) from action_run where status in (1,2,3,4) and stopped < extract(epoch from now())-16*86400;
  ```
  Acceptance = this stays near zero (nothing older than cutoff + 48 h), not a target `total_count`.
- Stop a running execution: `kubectl -n gitea delete job <job>`; stop the schedule:
  `kubectl -n gitea patch cronjob gitea-actions-run-retention -p '{"spec":{"suspend":true}}'`.
- Deleted rows come back as dead tuples that autovacuum reuses in-file; the on-disk 12 GB only shrinks
  with a `VACUUM FULL`/`pg_repack` window (separate decision).
- Rate changes (`MAX_DELETES_PER_RUN`, `DELETE_PAUSE_SECONDS`) go through git, from measured WAL per
  deletion (`pg_current_wal_lsn()` before/after a canary; slot `safe_wal_size` must not dip during it).
