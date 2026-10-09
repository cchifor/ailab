# CNPG: in-tree Barman Cloud -> plugin-barman-cloud

How the three trueswarm Postgres clusters (trueswarm-admin-pg, trueswarm-platform-pg, trueswarm-pg) moved
from in-tree `spec.backup.barmanObjectStore` to the CNPG-I Barman Cloud plugin, starting 2026-10-09.
**CNPG 1.31.0 removes in-tree Barman**, so no cluster may still use it at that hop (the pre-1.31 gate below).
Plan and review trail: `plans/2026-10-09-k8s-1.35-and-barman-plugin-plan.md` (Track A).
Monitoring: `kubernetes/apps/infrastructure/monitoring/cnpg-backup-rules.yaml` plus
`trueswarm-pg-podmonitors.yaml`.

All `kubectl` below means `kubectl --context admin@ai`. The default context is a different cluster.

## Preconditions (do not skip)

- **CNPG >= 1.30.1** (or 1.29.3 / 1.28.4).
  - Earlier versions deadlock the primary's switchover when a WAL-archiver plugin is enabled on an
    existing cluster (cnpg#11032/#11059).
  - They also lack the operator's empty-archive decision (plugin #1009), so archiving can wedge on
    "Expected empty archive".
- **plugin-barman-cloud is installed in `cnpg-system`** (the operator's namespace): platform
  `deploy/components/cnpg-operator/plugin-barman-cloud.yaml`, chart 0.8.1 / v0.15.1, controller and
  sidecar images digest-pinned. Check that:
  - the `objectstores.barmancloud.cnpg.io` CRD exists;
  - the plugin Certificates are Ready;
  - the `barman-cloud` Service carries the CNPG-I annotations.
- **ObjectStores exist first, in an inert PR**: trueswarm-admin `deploy/foundation/objectstore.yaml` and
  `deploy/platform/objectstores.yaml`.
  - `spec.configuration` = the cluster's live `barmanObjectStore`, verbatim, **without `serverName`**.
    The v0.15.1 schema forbids it, and the default (the cluster name) keeps the **same catalog**.
  - `retentionPolicy: 30d` moves here. It is a recovery window, not an object TTL, and it also covers
    the old in-tree backups.
- **The backup alerts are fed and quiet**, and every cluster has a daily ScheduledBackup.
- **Timing:** never between 01:30 and 03:30Z (the ScheduledBackup times), and never during a node
  operation or another CNPG roll.

## Per cluster (one PR each: admin -> platform-pg -> trueswarm-pg)

1. **Read drill** (before the first cutover). It proves the plugin can read the existing in-tree
   catalog. Create namespace `pg-restore-drill`, PSA `restricted`, containing:
   - copies of the source's backup-storage Secret, `backup-ca` and `registry-pull`;
   - network policy: ingress denied except `cnpg-system` -> :8000; egress (a CiliumNetworkPolicy in the
     style of `trueswarm-database-egress`) only to kube-system DNS :53, `kube-apiserver` 443/6443 and
     versitygw `192.168.1.225:7070`;
   - an `ObjectStore drill-store` with the same configuration and **no** `retentionPolicy`;
   - a 1-instance Cluster:

   ```yaml
   apiVersion: postgresql.cnpg.io/v1
   kind: Cluster
   metadata: {name: drill-<cluster>, namespace: pg-restore-drill}
   spec:
     instances: 1
     imageName: <same imageName as the source>
     imagePullSecrets: [{name: registry-pull}]
     storage: {size: <source size>, storageClass: qnap-iscsi}
     bootstrap:
       recovery:
         source: origin
         # continuity drill only: recoveryTarget: {backupID: <baseline in-tree backupID>, targetName: <restore point>}
     externalClusters:
     - name: origin
       plugin:
         name: barman-cloud.cloudnative-pg.io
         parameters: {barmanObjectName: drill-store, serverName: <source cluster>}
   ```

   There is **no `spec.plugins`, no `spec.backup` and no `retentionPolicy`**, so nothing in the drill
   writes to the bucket. Wait for `Ready`, then count rows in the 3 largest tables.

   **Cleanup** (the namespace holds production data). `qnap-iscsi` is `reclaimPolicy: Retain`:
   1. Patch each drill PV to `persistentVolumeReclaimPolicy: Delete`. Only PVs whose claimRef is in
      `pg-restore-drill`.
   2. Delete the Cluster.
   3. Wait until each PV **and** its `trident/<pv-name>` TridentVolume are gone (that is the QNAP
      backend volume).
   4. Delete the namespace.
2. **Baseline** (immediately before the cutover). On the **current primary**:
   - `pg_stat_archiver`: `last_archived_time` is non-NULL and recent; `last_failed_time` is NULL or
     older. Record the primary pod and `stats_reset`.
   - `ContinuousArchiving=True`.
   - No `$PGDATA/.check-empty-wal-archive` on any instance.
   - Instances are on different nodes.
   - Record `.status.firstRecoverabilityPoint` and the live `barmanObjectStore`.
   - Take a fresh **in-tree** on-demand Backup and keep its `backupID`.
3. **Schedule boundary.** A delayed reconcile can fire a missed run mid-cutover, because CNPG computes
   due runs from `lastCheckTime`. So:
   1. Suspend the schedule:
      `kubectl -n <ns> patch scheduledbackup <schedule> --type merge -p '{"spec":{"suspend":true}}' --field-manager=cutover-window`.
      Git does not declare `suspend`, so Flux leaves it alone.
   2. Confirm every Backup of the cluster is terminal.
4. **Merge the cutover PR** (trueswarm-admin, owner-merged). It changes three things:
   - `spec.backup` becomes `{target: prefer-standby}`;
   - it adds `spec.plugins: [{name: barman-cloud.cloudnative-pg.io, isWALArchiver: true, parameters: {barmanObjectName: <cluster>-store}}]`;
   - it sets the cluster's ScheduledBackup to `method: plugin`, `pluginConfiguration: {name: barman-cloud.cloudnative-pg.io}`.

   Because the schedule moves in the same PR, a revert restores both.
   - **Only Flux applies it.** Flux applies it as `kustomize-controller`, the sole owner of
     `spec.backup.barmanObjectStore`, so the field is removed. Under any other field manager,
     server-side apply keeps `barmanObjectStore`, and the CNPG webhook rejects the Cluster: "Cannot
     enable a WAL archiver plugin when barmanObjectStore is configured".
   - **The webhook does not check that the plugin or ObjectStore exist.** Keep the order: plugin ->
     ObjectStores -> cutover.

   CNPG then rolls the replica first, then the primary. Expect a bounded write interruption on that
   cluster:
   - switchover clusters: seconds;
   - `primaryUpdateMethod: restart` (trueswarm-admin-pg): ~3 min of smart shutdown, then ~30 s down.
5. **Verify**:
   - every instance pod has the **native sidecar**: initContainer `plugin-barman-cloud`,
     `restartPolicy: Always`, ready, at the pinned digest;
   - `ContinuousArchiving=True`;
   - `SELECT pg_switch_wal()` on the primary is archived within 2 min, with no new failure on the same
     primary and the same `stats_reset`;
   - an on-demand Backup (`method: plugin`, `pluginConfiguration.name: barman-cloud.cloudnative-pg.io`)
     reaches `completed`;
   - `kubectl -n <ns> get objectstore <cluster>-store -o jsonpath='{.status.serverRecoveryWindow}'`:
     `firstRecoverabilityPoint` for the cluster equals the baseline value. That is the catalog
     continuity check; allow up to 30 min for the first retention pass;
   - every replica is streaming and every slot active;
   - `barman_cloud_cloudnative_pg_io_last_available_backup_timestamp` is fresh in Prometheus;
   - no `CNPG*` alert fires.
6. **Continuity drill.** It proves that WAL from both archivers replays.
   1. On the source primary, **commit a drill marker** in the `postgres` maintenance database, not the
      application database:
      `CREATE TABLE IF NOT EXISTS ailab_drill_marker (name text PRIMARY KEY, at timestamptz DEFAULT now()); INSERT INTO ailab_drill_marker (name) VALUES ('post-plugin-<cluster>');`
      Then, **after** that commit, `SELECT pg_create_restore_point('post-plugin-<cluster>')` and
      `pg_switch_wal()`. The marker's commit record precedes the restore point in WAL, so a recovery that
      reaches the point must contain it, whatever the application writes meanwhile.
   2. Run the drill with `recoveryTarget: {backupID: <baseline in-tree backupID>, targetName: post-plugin-<cluster>}`.
      It passes when both hold:
      - the drill Cluster is Ready: PostgreSQL fails recovery if the named target is never reached;
      - the marker row is present in the drill's `postgres` database.

      Together these prove that the in-tree base backup plus WAL from **both** archivers replays.
   3. Row counts of the 3 largest tables, taken in one `REPEATABLE READ` transaction just before the
      marker, are a **sanity check only**. Concurrent commits between that snapshot and the restore point
      can legitimately change them; expect an exact match only if application writes were quiesced.
   4. Clean up as above.
   5. Do this for all three clusters, each against its own `serverName`. trueswarm-pg and
      trueswarm-platform-pg share the bucket prefix.
7. **Un-suspend** the schedule with the same `--field-manager=cutover-window`.
8. **After an hour**, re-check that `failed_count` stayed flat on the same primary.

## Expected noise

Every primary change (switchover or failover) logs exactly **one** failed archive of the new timeline's
`.history` file, then archiving continues. trueswarm-pg's ~47 lifetime failures roughly equal its
switchovers. `CNPGWalArchivingFailing` (last failure newer than last success for 15 min) does not fire on it.

## Escape hatches (diagnose first, never preemptive)

- **`ContinuousArchiving=False` with "Expected empty archive":** confirm that the catalog's latest
  timeline belongs to this cluster, then
  `kubectl -n <ns> annotate cluster <cluster> cnpg.io/skipEmptyWalArchiveCheck=enabled`.
- **No progress for 10 min:** this is an escalation trigger, not a licence to delete.
  - **Diagnose:**
    - `currentPrimary` / `targetPrimary`;
    - `pg_is_in_recovery()` on each instance;
    - replay LSNs;
    - sidecar readiness;
    - events;
    - storage and Trident health.
  - **If writes or replication are failing,** stop and notify the owner.
  - **Otherwise,** use only a diagnosed graceful `kubectl cnpg restart <cluster> <instance>`.
  - **Never force-delete:** a reused PVC does not guarantee the same primary election.

## Rollback (valid only while CNPG <= 1.30)

1. Suspend the cluster's schedule (`--field-manager=cutover-window`). Wait until no plugin Backup of the
   cluster is non-terminal.
2. Revert the cutover PR. Its schedule change reverts with it.
3. Keep the ObjectStores and the plugin until every cluster is back. Never prune them in the same change.
4. Wait for the roll, which drops the sidecar.
5. A forced `pg_switch_wal()` must archive in-tree, and an in-tree Backup must complete. Then un-suspend.

Retention deletions made by the plugin in the meantime are not reversible.

## Pre-CNPG-1.31 gate

All of these must hold before any CNPG >= 1.31 hop (`ta_gate_131.sh` in the program tooling does the
same):

```sh
kubectl get clusters.postgresql.cnpg.io -A -o json | jq '[.items[] | select(.spec.backup.barmanObjectStore
  or ([.spec.externalClusters[]? | select(.barmanObjectStore)] | length > 0))] | length'          # 0
kubectl get scheduledbackups.postgresql.cnpg.io -A -o json | jq '[.items[] | select(.spec.method == null
  or .spec.method == "barmanObjectStore")] | length'                                            # 0
kubectl get backups.postgresql.cnpg.io -A -o json | jq '[.items[] | select((.spec.method == null
  or .spec.method == "barmanObjectStore") and .status.phase != "completed")] | length'           # 0
```

Repeat the selection over every Flux-built directory of trueswarm-admin. `kubectl kustomize` emits a
multi-document YAML stream, so wrap it into an `items` list with Python and PyYAML. Don't use `yq`: the Go
(mikefarah) yq treats `-s` as split-to-files, which would feed jq nothing. The jq filter **errors** when
nothing was rendered, so a parse failure can't pass as `[]`:

```sh
set -o pipefail
for d in deploy/platform deploy/foundation; do
  kubectl kustomize "$d"     | python3 -c 'import sys, json, yaml; print(json.dumps({"items": [o for o in yaml.safe_load_all(sys.stdin) if o]}))'     | jq -e 'if (.items | length) == 0 then error("nothing rendered for this dir") else [.items[]
        | select((.kind == "Cluster" and (.spec.backup.barmanObjectStore or ([.spec.externalClusters[]? | select(.barmanObjectStore)] | length > 0)))
              or (.kind == "ScheduledBackup" and (.spec.method == null or .spec.method == "barmanObjectStore")))
        | .kind + "/" + .metadata.name] end' || echo "GATE ERROR in $d"                        # expect []
done
```

- **Historical Backup CRs.** About 30 completed in-tree Backup CRs remain as audit records, and are
  allowed. If 1.31's CRD rejects them, export them (`kubectl get backup -o yaml`), then remove them from
  Git; deleting a Backup CR does not touch object-store data.
- **Unbuilt directories.** `deploy/base` and `deploy/transition` in trueswarm-admin still contain
  `barmanObjectStore`, but no Flux Kustomization builds them. Fix or delete them before they are ever
  reused.
