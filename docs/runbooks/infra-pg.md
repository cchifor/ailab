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

## strive-pg (the PLATFORM's cluster, not this one): the harness role and database

`strive-pg` in ns `strive-ailab` is the Strive platform's CNPG cluster. Its Cluster CR belongs to the
platform repo (`deploy/components/cnpg-cluster/`, Flux Kustomization `platform-cnpg`); ailab never
patches it. The one database object ailab does keep there is the harness service's login role and
database (owner runbook step 12 of the platform's 2026-10 modularization program):

| Object | Where it comes from |
|---|---|
| OpenBao `af/strive/pg-harness`, field `password` | created ONCE (48 random hex characters) by Job `openbao-strive-pg-harness-provision` (`security/openbao/strive-pg-harness-provision-job.yaml`), which also owns policy + k8s-auth role `af-app-strive-pg-harness`. Operator-owned after creation. |
| Secret `strive-pg-harness` (key `password`) in `strive-ailab` | ExternalSecret `strive-pg-harness` via SecretStore `strive-pg-harness-store` (`infrastructure/strive-pg-harness/eso.yaml`). |
| Role `harness` (LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION) and database `harness` OWNER harness | Job `strive-pg-harness-bootstrap` (`infrastructure/strive-pg-harness/bootstrap.py`), as the CNPG superuser. Idempotent; reaped after 15 min and re-applied by Flux, so it re-converges about every 25 min. |

Why a Job and not the Cluster's `managed.roles` or a CNPG `Database`: the Cluster is the platform
repo's object (a second Flux owner would fight it), `managed.roles` leaves `app` in
`pending-reconciliation` on this cluster today, and the `Database` CRD needs CNPG >= 1.25 (the
operator is 1.24.1). `kustomization.yaml` in that directory has the long form.

Check it:

```sh
kubectl -n strive-ailab get externalsecret strive-pg-harness          # SecretSynced / Ready=True
kubectl -n strive-ailab logs job/strive-pg-harness-bootstrap         # "role harness ..." / "database harness present, owner harness"
platform psql -d postgres -c "select rolname, rolsuper, rolbypassrls, rolcanlogin from pg_roles where rolname='harness'"
platform psql -d postgres -c "select datname, pg_get_userbyid(datdba) from pg_database where datname='harness'"
```

Before the provision Job has run, the ExternalSecret is not Ready and the bootstrap Job logs
"not rendered yet" and changes nothing. Both are harmless: the `strive-pg-harness` Flux
Kustomization is `wait: false`.

**Wiring the harness service to it (the owner's H9.1 database step).** The harness's DSN is the
Secret `strive-pg-harness-dsn` (key `database-url` ONLY), rendered by ailab's ExternalSecret of the
same name (`strive-pg-harness/eso.yaml`) from the same OpenBao value as the role password:
`postgres://harness:<password>@strive-pg-rw.strive-ailab.svc.cluster.local:5432/harness`. The
platform's harness chart consumes it as `HARNESS_DATABASE_URL`; the SOPS `harness-secrets` is no
longer the source. Check it: `kubectl --context admin@ai -n strive-ailab get externalsecret
strive-pg-harness strive-pg-harness-dsn` (both `SecretSynced`). The owner may read the Secret;
dev workers cannot. Identity activation (S2S, projected tokens) is in
[`s2s-identity.md`](s2s-identity.md).

**Rotating the password. The order matters** (the bootstrap Job reads the password from its startup
env, `bootstrap.py:66`, so a stale env re-applies the OLD value):

1. `bao kv patch -mount=af strive/pg-harness password=-` (value on stdin). Keep the invariant:
   exactly 48 hex characters (no URL-escaping needed in the DSN).
2. Force-sync BOTH ExternalSecrets and wait for a FRESH successful reconcile of each (compare
   `status.refreshTime` before and after, and `Ready=True`):
   ```sh
   for es in strive-pg-harness strive-pg-harness-dsn; do
     kubectl --context admin@ai -n strive-ailab annotate externalsecret $es force-sync=$(date +%s) --overwrite
   done
   kubectl --context admin@ai -n strive-ailab get externalsecret strive-pg-harness strive-pg-harness-dsn \
     -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.refreshTime}{"\n"}{end}'
   ```
3. Re-run the bootstrap Job (it picks up the new password at startup) and confirm it applied
   ("role harness ..." in its log, and no "not rendered yet"):
   `kubectl --context admin@ai -n strive-ailab delete job strive-pg-harness-bootstrap`. The Job is
   recreated on the next Flux reconcile (up to 10 min), with the new Secret in its env; or trigger it
   now by annotating the `strive-pg-harness` Kustomization with `reconcile.fluxcd.io/requestedAt`
   (a fresh timestamp value):
   ```sh
   kubectl --context admin@ai -n flux-system annotate kustomization strive-pg-harness \
     reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
   kubectl --context admin@ai -n strive-ailab get job strive-pg-harness-bootstrap \
     -o jsonpath='{.metadata.creationTimestamp}{"\n"}'
   ```
   Check that the new Job's `creationTimestamp` is after the sync (the `refreshTime` values from step
   2), not the old Job's, then
   `kubectl --context admin@ai -n strive-ailab logs job/strive-pg-harness-bootstrap`.
4. Restart the harness and wait for `/ready`.

Until step 3 completes the database still holds the old password, and until step 4 the harness
holds an old connection; do not skip either.

**After an OpenBao wipe** the provision Job GENERATES A NEW value and the bootstrap Job writes it into
Postgres, which breaks the running harness's DSN. Put the old value back first. Two places still hold
it, and only one of them is safe:

- the running harness pod's `HARNESS_DATABASE_URL`, which predates the wipe for as long as the pod has
  not restarted (PREFERRED);
- the Secret `strive-pg-harness-dsn` (`database-url`, owner-readable), which is only the old value
  until the next ExternalSecret refresh. Once the provision Job has re-run and the ExternalSecret has
  refreshed, the Secret holds the NEW random password, and "recovering" it would write the wrong value
  back.

The script reads both without echoing, refuses when they differ, and refuses to trust the Secret alone
unless you confirm it predates the wipe:

```sh
set -o pipefail
parse='
import sys, urllib.parse as u
pw = u.urlsplit(sys.stdin.read().strip()).password
if not pw:
    sys.exit("no password in the DSN")
sys.stdout.write(u.unquote(pw))'
pod_pw=$(kubectl --context admin@ai -n strive-ailab exec deploy/harness -- printenv HARNESS_DATABASE_URL 2>/dev/null | python3 -c "$parse") || pod_pw=
sec_pw=$(kubectl --context admin@ai -n strive-ailab get secret strive-pg-harness-dsn -o jsonpath='{.data.database-url}' | base64 -d | python3 -c "$parse") || sec_pw=
pw=
if [ -n "$pod_pw" ] && [ -n "$sec_pw" ] && [ "$pod_pw" != "$sec_pw" ]; then
  echo 'pod and Secret DSN passwords DIFFER: the Secret was already regenerated. Use the pod value (do not restart the pod) or restore from backup.' >&2
elif [ -n "$pod_pw" ]; then
  pw=$pod_pw
elif [ -n "$sec_pw" ] && [ "${I_CHECKED_SECRET_PREDATES_WIPE:-}" = 1 ]; then
  pw=$sec_pw
elif [ -n "$sec_pw" ]; then
  echo 'only the Secret is readable: set I_CHECKED_SECRET_PREDATES_WIPE=1 once you have confirmed it predates the wipe' >&2
else
  echo 'no password recoverable from the pod or the Secret' >&2
fi
if [ -n "$pw" ]; then printf '%s' "$pw" | bao kv put -mount=af strive/pg-harness password=-; fi
unset pw pod_pw sec_pw parse
```

(`python3` on Linux hosts; use `python` in Git Bash. The deployment name `harness` is the platform
chart's; adjust if it differs.) The password lives only in shell variables and is never echoed.

Run it before the provision Job runs (or use `bao kv patch` after); then follow the rotation order above (steps 2-4) so both
ExternalSecrets, the role and the harness converge. `openbao-recovery.md` lists this path.
