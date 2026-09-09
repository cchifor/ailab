# Estate-audit critical remediation (#592–#601)

## Codex Review

- **Blocking:** the failing USB-backed gateway is not a sound sole recovery destination. Enabling WAL archiving against it can fill the primary's shared volume; #595 is a dependency unless storage changes. Prefer internal-pool dumps copied directly to encrypted Drive while establishing a reliable archive destination.
- **High:** the 14 GiB calculation is not a disk-usage upper bound. PostgreSQL's 4GB slot cap provides about 5.3–7.0 minutes at the stated peak rates, shorter than documented node/storage recovery paths.
- **High:** replication alerts need standby filtering and per-target coverage. The suggested `absent(...) or vector(0)` expression can alert permanently. Talos etcd reachability is unverified and the committed machine configuration does not explicitly expose it.
- **High:** dump capture needs capacity, concurrency, complete-database coverage and restore checks. The SOPS filename does match the existing rule when credentials are under `data`/`stringData`; the live dump PVC's capacity could not be verified.
- **High:** split rollout into explicit recovery gates, correct the Gatus path and LiteLLM rollout trigger, and add an isolated restore before claiming recovery. The file inventory omits several dependencies; avoid turning emergency repair into a new storage/telemetry platform.

<!-- codex: Review evidence: inspected the repository and upstream documentation. Live reads were limited: kubectl could not read C:\Users\chifo\.kube\config (Access is denied), and HTTP probes from this environment to 192.168.0.41/.42/.43:2381 all failed to connect. These failures do not establish in-cluster network behavior. No live PVC capacity, Talos 1.11.2 listener/authentication state, recovery timing, or Prometheus label set is certified by this review. -->

## Context

The 2026-09-09 estate audit found the infrastructure healthy and the data-protection layer
not. Confirmed by hand:

- `infra-pg` — the shared Postgres behind Gitea, Authelia, Grafana, Open WebUI, LiteLLM and
  the AgentForge platform — has had **no replica since 2026-09-02**: `pg_stat_replication` = 0
  rows, slot `_cnpg_infra_pg_3` `active=false wal_status=lost`, 228 GB behind, last replay
  `2026-09-02 08:45:25`. CNPG reports `readyInstances=2, Cluster in healthy state`.
- There is nothing to re-seed from: `.spec.backup`, `.status.lastSuccessfulBackup` and
  `.status.firstRecoverabilityPoint` are all empty; `get scheduledbackups` returns none.
<!-- codex: MEDIUM — Distinguish no independent backup from no re-seed source. The surviving primary can supply pg_basebackup for a new standby. Starting an archive now cannot recreate the WAL already lost since September 2, so it cannot repair the existing 228 GB gap by itself. -->
- `InfraPostgresSingleInstance` — the alert written for exactly this — counts **ready pods**,
  evaluates to `2`, and read all-clear for seven days.
- `kubeEtcd` is disabled: 0 etcd targets, 0 etcd rules, while 24 containers across 10
  namespaces restarted together at 00:10–00:11Z with no alert.
- Prometheus sits at 93–96 % of a 2 GiB limit with liveness probes already timing out, and no
  rule watches memory-vs-limit in `monitoring`.
- Open WebUI's default model points at `192.168.0.28:18020`, which is `ConnectionRefused`,
  with zero LiteLLM fallbacks configured.

This plan implements the code-side fixes. It deliberately does **not** attempt the two items
that are not code:

- **#595** (both on-site backup copies behind one failing USB cable) is hardware. Recorded, not
  fixed here.
<!-- codex: BLOCKER — Step 1a depends on fixing or bypassing this failure domain. plans/2026-09-08-dr-remediation-plan.md records a physically absent USB disk and already specifies internal-storage dumps with direct encrypted Drive copies. A separate bucket on the same failed disk provides namespace isolation, not independent durability. Make destination readiness an explicit prerequisite; hardware work may remain outside this PR, but its dependency cannot. -->
- **#602** (OpenBao breakglass token exposure) is an operator decision between accepting the
  exposure and paying a wipe-and-re-bootstrap that loses KV. Not mine to take.

**#594** (strive-pg PodMonitor missing `release: kube-prometheus-stack`) is confirmed but its
manifest lives in `cchifor/platform`, not this repo. Out of scope here; the alert rule added in
step 2 is written to cover it the moment that label is fixed.

## Approach

### 1. Give `infra-pg` a real recovery path (#592)

The keystone is WAL archiving, not the slot cap. With an archive, a lagging replica catches up
from the archive instead of the slot, which is what makes a *small* `max_slot_wal_keep_size`
safe rather than fatal. Raising the cap alone would trade a lost replica for a full volume —
data and WAL share one 20 GiB volume here, and the manifest already documents that
`walStorage` cannot be added to an existing cluster.

Order matters: archive first, then re-clone, because re-cloning without an archive just
reproduces the failure the next time a replica falls behind.

<!-- codex: HIGH — Establish a verified dump outside the USB path before changing the surviving primary. A scheduled or hours-long initial backup leaves the cluster without a usable standby throughout that window; neither a slot-cap increase nor an archive creates failover capacity. Start an on-demand backup, watch primary free space and archive progress, define a deadline/abort procedure, and defer unrelated node restarts. If the destination cannot be made reliable promptly, explicitly sequence replica repair after the independent dump instead of leaving redundancy blocked indefinitely on #595. -->

1a. Add `spec.backup.barmanObjectStore` to `infra-pg`, pointing at a **new, separate**
    versitygw bucket `infra-pg-backups` — not the `velero` bucket, which rejects unexpected
    top-level directories (that broke a copy for 5 days on 2026-08-10). Reuse the existing
    versitygw credential pattern; the endpoint is `https://192.168.1.225:7070` with
    `s3ForcePathStyle` and the self-signed cert already trusted by the Velero path.
    `wal.compression: gzip`, `data.jobs: 1` (one small cluster, no need to parallelise).

<!-- codex: BLOCKER — A USB outage during archiving retains unarchived WAL in pg_wal regardless of max_slot_wal_keep_size; max_wal_size is also a soft target. This can turn another backup-disk failure into loss of the primary and its applications. Even the claimed 5.5 GiB spare would last only about 7.3–9.7 minutes at the stated peak rate if archiving stops. Require measured headroom, archive-backlog/free-space alerts and an outage procedure before enabling it; timeouts alone do not release unarchived WAL. Never discard required WAL or fake archive success to clear space. See [PostgreSQL WAL configuration](https://www.postgresql.org/docs/17/wal-configuration.html). -->

<!-- codex: HIGH — Existing alternatives: qnap-iscsi uses internal QNAP pools; the September 8 USB plan records 5.5 T/4.7 T free on ZFS2_DATA/ZFS18_DATA (recheck current capacity). Put logical dumps there and copy directly to the existing gdrive-crypt remote with dated immutable generations, bypassing versitygw as the earlier DR plan requires. Internal QNAP storage still shares NAS failure with production, so keep the off-site leg. Moving the gateway's backing store to an internal pool is another option, but requires a coordinated migration of existing consumers. No other S3 endpoint exists: Drive is not S3, and NFS is not a drop-in barmanObjectStore target. A new independently hosted object store is additional deployment work; paid R2 was explicitly rejected in ADR 0010. Do not silently add either to this emergency change. -->

<!-- codex: HIGH — The Velero HelmRelease actually sets insecureSkipTLSVerify: "true"; it does not supply reusable CA trust. CNPG 1.24 needs endpointURL and an endpointCA SecretKeySelector in databases, with a certificate valid for the endpoint IP. Existing versitygw-ca resources are ConfigMaps in other namespaces. s3ForcePathStyle is a Velero option, not a CNPG barmanObjectStore field: validate the installed CRD and the bundled Barman/botocore addressing behavior against this IP endpoint. Provision bucket ownership and dedicated, bucket-scoped credentials outside Flux before use; existing Velero/Talos keys are scoped to their buckets. Verify PUT/GET/LIST/DELETE and both archive/restore access without disabling TLS verification. See [CNPG object-store configuration](https://cloudnative-pg.io/documentation/1.24/appendixes/object_stores/). -->

<!-- codex: HIGH — A failed/partial base backup is not a recovery source, and CNPG/Barman do not repair lost or corrupted objects on the USB disk. After a failed run, take a fresh complete backup; recover from an earlier verified complete backup only with the uninterrupted WAL needed for the chosen target. An irretrievable WAL gap blocks replay across it. Test the pinned Barman version's failed-upload cleanup/retry behavior and preserve the previous valid generation. Also specify at-rest protection: SOPS encrypts credentials in git, and gzip is compression; neither encrypts database backups containing application secrets. See [CNPG recovery](https://cloudnative-pg.io/documentation/1.24/recovery/). -->

<!-- codex: MEDIUM — data.jobs controls base-backup work, not WAL throughput. Measure sustained archive/compression/upload/replay rates under concurrent NAS load; tune wal.maxParallel only if measurements require it. A small database can still generate substantial WAL. The cited peak would be about 0.80–1.06 TiB/day uncompressed if sustained, so measure average churn before promising 14 days of archive capacity. See [CNPG WAL archiving](https://cloudnative-pg.io/documentation/1.24/wal_archiving/). -->

1b. Add a `ScheduledBackup` at 02:30 daily (offset from Velero's 02:00 so they do not contend
    for the same NAS spindle), `backupOwnerReference: self`, retention via
    `spec.backup.retentionPolicy: "14d"`.

<!-- codex: HIGH — Use CNPG's six-field schedule, e.g. "0 30 2 * * *", with an explicit timezone convention, and an immediate/on-demand first backup. Set target: primary for the initial backup: CNPG defaults to prefer-standby, while this cluster reports the stale standby as ready. A 30-minute offset does not prevent overlap: Velero permits 90-minute filesystem backups and also runs Sundays at 03:00. Measure durations and coordinate dump publication/pruning, Velero and off-site copying. See [CNPG backup scheduling and targets](https://cloudnative-pg.io/documentation/1.24/backup/). -->

<!-- codex: HIGH — The 14d recovery window is not exactly fourteen daily files: retention may preserve an older base backup plus the WAL required to span the window, and initial deployment cannot establish fourteen days of history. Also, backup-offsite/rclone-cronjob.yaml explicitly copies only velero and talos-etcd-backups; the new bucket will have no off-site copy automatically. Specify the independent dump-copy leg or a validated archive-copy extension, destination permissions, freshness monitoring and restore procedure. See [CNPG retention policies](https://cloudnative-pg.io/documentation/1.24/backup_barmanobjectstore/). -->

1c. Raise `max_slot_wal_keep_size` 1GB → 4GB **only after 1a is live**. At the measured
    9.7–12.9 MiB/s peak WAL rate, 1 GB is ~80 seconds; 4 GB is ~5 minutes, which covers a
    replica restart. Worst case on the 20 GiB volume becomes DB ~6 GiB + `max_wal_size` 4 GB +
    4 GB slot retention ≈ 14 GiB of ~19.5 GiB usable — still headroom, and now a blown cap is
    recoverable from the archive rather than terminal.

<!-- codex: HIGH — Unit arithmetic: PostgreSQL GB is binary, so 6 GiB + 4GB + 4GB numerically is 14 GiB. However, the checkpoint and slot requirements retain overlapping WAL, not separate fixed allocations, and checkpoint overshoot/unarchived WAL invalidate this as a worst-case bound. Measure PGDATA excluding pg_wal to avoid double-counting; include indexes, temporary files, growth, filesystem journal/metadata and reserved blocks. The claimed 19.5 GiB usable needs df evidence for the Postgres UID. No guest LVM layer is established by these manifests; do not invent a fixed LVM deduction. Verify NAS thin-pool capacity separately. See [PostgreSQL parameter units](https://www.postgresql.org/docs/17/config-setting.html). -->

<!-- codex: HIGH — 4GB / 9.7–12.9 MiB/s = 422–318 seconds (7.0–5.3 minutes); 1GB gives 106–79 seconds. This is a nominal retention horizon starting from no backlog, not a guaranteed restart budget. docs/runbooks/node-maintenance.md documents 300-second eviction tolerance and roughly six-minute iSCSI detach delays, with about thirteen-minute recovery paths; fast promotion of an existing healthy replica is a different operation. Ten minutes of peak WAL alone needs 5.7–7.6 GiB before margin. Measure detection, scheduling, attach, restart and catch-up times; neither assert 4GB covers them nor blindly enlarge the cap on this shared 20Gi volume. -->

1d. **Correct the false rationale** in `infra-pg.yaml`. The comment claims "the lagging
    replica's slot is invalidated and CNPG re-clones it". CNPG 1.24.1 does not; seven days
    later there was no re-clone. That sentence is why this went unexamined.

<!-- codex: MEDIUM — Correct this comment, but do not replace it with an unconditional archive self-healing promise. Prove on CNPG 1.24.1 that the standby can fetch the necessary archived WAL with its actual restore_command, credentials and CA, and that an invalidated slot is repaired/replaced as required. Archive continuity and replay throughput are prerequisites; a newly configured archive cannot fill historical gaps. -->

1e. Codify the nightly logical dump that currently exists only as a hand-applied one-shot:
    a `CronJob` writing per-database `pg_dump -Fc` to the existing `postgres-dumps` PVC, with
    `pg_restore --list` verification and atomic publish. **The PVC must be mounted by a running
    pod for Velero fs-backup to capture it** — an unmounted PVC is skipped by design, which is
    why the current dump has zero PodVolumeBackups. The CronJob alone does not fix that, so it
    also prunes to the last 7 days and the plan adds the PVC to a long-lived mount (see 1f).

<!-- codex: HIGH — PVC capacity is UNVERIFIED: postgres-dumps has no committed PVC manifest anywhere in this checkout, and the live read was denied. Record its namespace, requested/actual capacity, access mode, storage class, binding and filesystem free space, then adopt it declaratively without replacing its data. The September 8 results record approximately 2.14 GiB per complete dump generation: seven are about 15.0 GiB, and seven retained plus one being written need about 17.1 GiB before growth, globals, metadata, stale partials and reserve. Thus seven days is plausible but tight on a hypothetical 20Gi claim, not guaranteed; compression and current DB size must be measured. Fail before exhausting space and prune only after a complete replacement is verified, preserving the last good backup. -->

<!-- codex: HIGH — Preserve the already-established coverage: eight databases including agentforge_broker and postgres, plus globals, ownership/ACLs and extension/role settings. The September 8 results used backup_dumper with pg_read_all_data + BYPASSRLS after per-owner dumps missed RLS-protected data; its role/credential/bootstrap path is currently not codified here. Use compatible PG17 tools and infra-pg-rw, not the stale replica. Specify grants for future databases, a securely mounted least-privilege credential, globals handling and SOPS/OpenBao password recovery. pg_restore --list checks the TOC, not all data or restorability. Per-database dumps are not a single cross-database snapshot. -->

<!-- codex: MEDIUM — Specify concurrencyPolicy: Forbid, finite job/retry deadlines, restrictive file permissions and explicit resources (databases defaults otherwise impose 512Mi). Publish the entire verified generation atomically from a staging directory on the same filesystem; add checksums and a completion marker. Exclude staging data from the chosen backup/copy path and prevent pruning while Velero reads it: atomic rename alone is not a filesystem snapshot. Monitor last successful generation, off-site freshness, failures and PVC free space. A failed/skipped CronJob must not look like a current backup. -->

1f. Mount `postgres-dumps` read-only into a minimal always-running pod so Velero's fs-backup
    sees it. Without this the dumps are invisible to every backup, which is the current state.

<!-- codex: HIGH — The running mount is required for the configured Velero filesystem path, not for every possible backup mechanism. A holder and writer can share ReadWriteOnce on the SAME node; mounting read-only does not permit a second node to attach an RWO iSCSI volume. ReadWriteOncePod would prohibit the two-pod design; RWX permits different nodes. Verify the actual mode and add same-node scheduling if RWO, or combine scheduling/mount ownership in a simple controller-managed design. Use a one-replica controller rather than an unmanaged bare pod, mount read-only, omit DB/cloud credentials from the holder and disable unnecessary service-account access. Verify node-agent coverage and completed PodVolumeBackups for this volume, then restore a generation. Sources: [Kubernetes access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes), [Velero filesystem backup](https://velero.io/docs/v1.18/file-system-backup/). -->

### 2. Make the replication failure detectable (#593, and the class in #600)

Add to `ha-rules.yaml`:

- `PostgresReplicaNotStreaming` — `cnpg_pg_replication_is_wal_receiver_up == 0`, `for: 10m`,
  critical. Covers both CNPG clusters.
- `PostgresReplicationLagHigh` — `cnpg_pg_replication_lag > 900`, `for: 15m`, warning.
<!-- codex: HIGH — A healthy primary has no WAL receiver, so the first expression will page on primaries unless joined to cnpg_pg_replication_in_recovery == 1 using verified pod/namespace labels. Add primary-side streaming-replica-count coverage against the expected count; receiver-process presence alone does not prove replay is advancing. Verify the installed monitoring query's lag semantics and test an idle healthy standby, because replay-timestamp age is not always transport lag. The warning's >900 seconds plus for: 15m can take about thirty minutes after lag begins, far beyond this slot window. See [CNPG 1.24 monitoring queries](https://cloudnative-pg.io/documentation/1.24/monitoring/). -->
- Both must carry an `absent()`/`or vector()` guard: the audit measured the `infra-pg` metrics
  target **flapping down** (`Get ".../metrics": EOF`), so a naive rule goes silent rather than
  firing — the exact failure mode being fixed.
<!-- codex: HIGH — The concrete guard absent(cnpg_pg_replication_is_wal_receiver_up{job="kube-prometheus-stack-cnpg"}) or vector(0) is wrong as an alert expression: missing metrics yield {job="kube-prometheus-stack-cnpg"}=1 AND {}=0; present metrics leave {}=0. Prometheus activates alerts for returned series even when their value is zero, so the unlabeled alert never resolves. absent(...) alone, or a final > 0 comparison without bool, fixes that particular defect. A single absent() still misses one failed target while another exports the metric. Use separate per-target up == 0 and expected-inventory/missing-metric coverage with stable labels; test flapping across the for interval so changing label sets cannot repeatedly reset the timer. Sources: [absent semantics](https://prometheus.io/docs/prometheus/latest/querying/functions/#absent), [alert evaluation](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/). -->

<!-- codex: HIGH — The local infra-pg-metrics PodMonitor does not set jobLabel or relabel job to kube-prometheus-stack-cnpg. That job selector is unverified and may be permanently empty. Inspect actual /targets and metric labels for both clusters before choosing selectors. Until #594 supplies scrape coverage, strive-pg remains unmonitored unless expected-cluster coverage explicitly detects its missing monitor. A healthy infra-pg target must not mask it. -->
- Rename `InfraPostgresSingleInstance` → `InfraPostgresPodCountLow` and reword its annotation
  so it no longer reads as replication coverage. It is a valid pod-loss alert; it was only ever
  mislabelled.

Add the corresponding `backup-rules.yaml` entry `VeleroBackupFailing` on
`velero_backup_failure_total` — PartiallyFailed is alerted today, Failed is not.

<!-- codex: MEDIUM — Alert on a recent increase with explicit schedule/ad-hoc scope, not the lifetime counter being nonzero. Test counter resets, repeated failures, resolution after the window, and missing exporter data. Add native CNPG backup/archiver and dump freshness coverage too: a new physical backup chain that silently fails would repeat the existing defect. -->

Add `CSIVolumeDeleteFailing` on the `VolumeFailedDelete` event rate (#601). 57,818 events and
climbing, 240 failed QNAP API calls/hour against the API that serves every qnap-iscsi PVC.

<!-- codex: HIGH — VolumeFailedDelete is a Kubernetes Event reason, not an established Prometheus counter. No event exporter or event-to-metric pipeline was found in this repo; kube-state-metrics does not automatically expose Event reasons as counters. Name and verify the actual metric, labels, repeated-event count semantics and scrape path before writing this rule. If producing the signal requires a new collector, make that an explicit scoped dependency or a follow-up rather than hiding it in a rules-only change. -->

### 3. Stop the alerting plane from dying silently (#597)

- Prometheus `limits.memory` 2Gi → 3Gi. Requests stay 1Gi.
<!-- codex: MEDIUM — A 3Gi limit with a 1Gi request still schedules only 1Gi of capacity. Verify node headroom for the actual working set, TSDB WAL replay/startup peaks and the extra etcd series; choose a request based on those measurements. Pin down the proposed CPU limit numerically and verify CFS throttling, query/rule latency and liveness after rollout rather than inferring safety from average CPU use. -->
- Exempt Prometheus from the `monitoring` LimitRange CPU default. The LimitRange injects
  `cpu: 500m` the HelmRelease never asked for, throttling 7.6–19.5 % continuously against
  0.109 cores of real use. Set an explicit generous `cpu` limit in `prometheusSpec.resources`
  so the LimitRange default no longer applies.
- Add `MonitoringContainerNearMemoryLimit` — `working_set / limit > 0.9`, `for: 15m`, warning,
  scoped to `namespace="monitoring"`. Generic, so it covers Loki and Alertmanager too.

<!-- codex: MEDIUM — Specify the memory denominator and label join: deduplicate cAdvisor and kube-state-metrics by namespace/pod/container, exclude empty/POD containers, select memory limits in bytes and omit zero/missing limits. Test multi-container pods and an unlimited container. Working-set ratio is a warning heuristic; independently verify OOM/restarts, scrape gaps and rule-evaluation failures after changing resources. -->

<!-- codex: HIGH — Retention remains size-bound: kube-prometheus-stack.yaml sets retentionSize: 36GB on a 48Gi PVC despite retention: 15d. Its August measurement estimated about 12.2 days, not fourteen, and new scrape targets can shorten that. Measure current block-write rate, WAL/head overhead and oldest retained samples; do not use a nominal time setting to claim a 14-day observation window. This is separate from Barman's 14d recovery policy. -->

### 4. Give etcd telemetry and catch restart waves (#598)

- `kubeEtcd: { enabled: true }` with endpoints `192.168.0.41/.42/.43`, port 2381 (Talos serves
  etcd metrics unauthenticated on 2381, so no client certs are needed). This loads the chart's
  four etcd rules, `etcdHighFsyncDurations` being the one that matters given measured
  `slow fdatasync` up to 8.5 s.
<!-- codex: HIGH — Do not accept the Talos 1.11.2 reachability claim. kubernetes/infra/machine-config/controlplane.yaml.tftpl exposes scheduler/controller-manager only and explicitly defers etcd metrics; kube-prometheus-stack.yaml agrees. Current [Sidero instructions](https://docs.siderolabs.com/kubernetes-guides/monitoring-and-observability/etcd-metrics) require explicit cluster.etcd.extraArgs.listen-metrics-urls exposure; these docs do not prove the live 1.11.2 configuration. All three workstation probes failed and in-cluster access was unavailable here. Require successful HTTP /metrics requests without client credentials from Prometheus's network context to each IP, plus inspection of the effective listener. If a Talos patch is needed, add it to the file inventory and apply one CP at a time with quorum checks using the pinned 1.11.2 talosctl from the node-maintenance runbook. Restrict unauthenticated metrics to the monitoring network; do not change etcd client/peer TLS. -->

<!-- codex: MEDIUM — Use the existing HelmRelease's spec.values.kubeEtcd block with endpoints and explicit HTTP service/monitor settings. The pinned [chart 86.2.3 values](https://raw.githubusercontent.com/prometheus-community/helm-charts/kube-prometheus-stack-86.2.3/charts/kube-prometheus-stack/values.yaml) already default to port/targetPort 2381 and scheme http, but this says nothing about host listeners. Render that exact chart to verify service selectors, rule enablement, alert names and required histogram series; do not assume an exact count of four rules. -->
- Add `ControlPlaneRestartWave` — `sum(increase(kube_pod_container_status_restarts_total{...}[10m])) > 8`
  over control-plane namespaces. The per-pod `KubePodCrashLooping` cannot express "many things
  restarted at once": each container restarted once and recovered, so nothing entered
  CrashLoopBackOff.

<!-- codex: MEDIUM — Define the scope explicitly: namespace selection is not control-plane-node selection, since these nodes also run application workloads. If the intent is distinct containers across several namespaces, sum(increase(...)) > 8 can instead be satisfied by one container restarting nine times. Test the documented incident, a single crash loop, normal rollouts and counter/pod replacement. Talos etcd is a host service and is not covered by kube_pod_container_status_restarts_total; this alert complements rather than replaces host/etcd health. -->

### 5. Restore the default chat model (#599)

- Add `fallbacks` to the LiteLLM router mapping `qwen3.8-27b-vllm-cloud` and
  `qwen3.8-27b-vllm-fast-cloud` to a reachable route. `192.168.0.26:8080` answers from the
  litellm pod (verified). Because that is a *different* engine, the fallback entry must not
  inherit `:18020`'s `chat_template_kwargs` / `enable_thinking` sampling — declare the fallback
  target as its own `model_name` with its own params.
<!-- codex: HIGH — Reuse a suitable existing independent model entry if possible: litellm.yaml already registers several routes on .26:8080. It explicitly documents that cloud1/2/3 power off nightly, so .26 shares scheduled unavailability with .28 and cannot provide 24/7 fallback alone. Confirm the exact served model, context/tool/streaming compatibility, cold-start latency, retry deadline and whether caller-supplied extra_body overrides survive a fallback. Recompute the Deployment checksum/config annotation: the ConfigMap is read only at startup and no Reloader exists, so otherwise Flux applies this change without activating it. -->
- Add a Gatus endpoint for `192.168.0.28:18020` so this is not next discovered by a user typing
  into the chat box.

<!-- codex: MEDIUM — Gatus lives at kubernetes/apps/apps/gatus/configmap.yaml, not apps/edge/gatus*.yaml. Add an HTTP readiness/model check with timeout and the existing native ntfy alert path; a TCP listener alone does not prove inference readiness. Account explicitly for intentional cloud power-off windows, and validate fallback behavior while the primary route is unavailable without disrupting a healthy production backend. -->

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/databases/infra-pg.yaml` | Cluster: add `spec.backup`, raise slot cap, correct the false comment |
| `kubernetes/apps/databases/infra-pg-backup-credentials.sops.yaml` | **new** — S3 credential for the barman store |
| `kubernetes/apps/databases/infra-pg-scheduled-backup.yaml` | **new** — daily ScheduledBackup |
| `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml` | **new** — nightly logical dump + the always-mounted holder pod |
| `kubernetes/apps/databases/kustomization.yaml` | wire the new files in |
| `kubernetes/apps/infrastructure/monitoring/ha-rules.yaml` | replication rules; rename the pod-count alert |
| `kubernetes/apps/infrastructure/monitoring/backup-rules.yaml` | `VeleroBackupFailing`, `CSIVolumeDeleteFailing` |
| `kubernetes/apps/infrastructure/monitoring/kube-prometheus-stack.yaml` | kubeEtcd on; Prometheus memory + explicit cpu limit |
| `kubernetes/apps/infrastructure/monitoring/*-rules.test.yaml` | promtool unit tests for every new rule |
| `kubernetes/apps/apps/ai/litellm.yaml` | router fallbacks |
| `kubernetes/apps/apps/edge/gatus*.yaml` | probe for :18020 |

<!-- codex: HIGH — Reconcile this inventory with the design: the table names THREE new manifest files (the holder shares the CronJob file), at least seven concrete existing-file changes, and additional test files, rather than four new/four modified. It omits the endpoint-CA Secret, dump PVC adoption, dump-role/credential/bootstrap path, direct off-site-copy changes, possible Talos patch and any event-metric producer. Correct the Gatus path above. Keep changes to existing rules and their fixtures within their already-wired monitoring kustomization; do not add promtool test documents as Kubernetes resources. -->

<!-- codex: HIGH — databases/kustomization.yaml currently lists none of the proposed backup files; no wiring change exists yet to validate. Add every required resource and inspect the rendered object inventory. Resource list order is not a readiness barrier. clusters/ai/databases.yaml has wait: true, depends only on infrastructure and checks Cluster Ready, which already reported healthy during this replication failure; it does not gate on archive success or Backup completion. Stage backup prerequisites/configuration, verified initial backup, then slot-cap changes in separate reconciliations, and keep re-cloning a manually gated operation. A single commit changing spec.backup and the slot cap applies them together. New permanently Pending holders can also block databases readiness and downstream consumers. See [Flux dependency and health checks](https://fluxcd.io/flux/components/kustomize/kustomizations/). -->

## Verification

Static, before merge:

1. `scripts/rules-lint.sh` — `promtool check rules` over every PrometheusRule.
2. `promtool test rules` against the `.test.yaml` files. **Every new alert gets a unit test
   that fails when the rule is inverted** — the audit's central finding is rules that cannot
   fire, so "it loads" is not evidence.
<!-- codex: MEDIUM — scripts/rules-lint.sh already extracts spec.groups and runs every *-rules.test.yaml, so reuse that gate. Include healthy primary/standby, lag, missing one/all targets, stale markers, flapping, target recovery, counter reset, exact for boundaries and missing limits. Assert alert labels and resolution as well as firing; threshold inversion alone misses the permanent-zero guard defect. -->
3. `scripts/manifest-lint.sh` — `kustomize build` + `kubeconform` for every touched overlay.
<!-- codex: HIGH — This script skips Secret and permits missing CRD schemas; its catalog is not pinned to the installed CNPG 1.24.1 schema. It also validates HelmRelease objects without rendering their charts. Add validation against the installed Cluster/Backup/ScheduledBackup CRDs, supported Barman options and rendered kube-prometheus-stack 86.2.3 manifests. Verify the dump scripts and embedded LiteLLM/Gatus configs, required images/roles/Secrets, checksum rollout trigger and final rendered resource names. A green generic manifest lint cannot certify these integrations. -->
4. `sops --decrypt` round-trip on the new credential, and a grep asserting every secret leaf is
   `ENC[` before commit. The file must be named to match the `encrypted_regex` for its path or
   SOPS silently writes plaintext.

<!-- codex: VERIFIED WITH CONDITION — .sops.yaml uses first-match path rules. kubernetes/apps/databases/infra-pg-backup-credentials.sops.yaml matches the generic .*\.sops\.ya?ml$ rule; its encrypted_regex is ^(data|stringData)$. Any credential key nested beneath either matched ancestor is encrypted regardless of its leaf name. Top-level access keys outside those ancestors would remain plaintext. The filename selects path_regex, not encrypted_regex. Use the intended checkout's explicit SOPS config, assert all payload leaves are ENC[...] without printing decrypted values, validate Secret selectors/namespace and verify Flux decryption. A successful decrypt round-trip alone does not prove payload encryption. -->

Live, after Flux reconciles:

5. `kubectl -n databases get cluster infra-pg -o jsonpath='{.status.firstRecoverabilityPoint}'`
   becomes non-empty — this is the single acceptance test for #592.
<!-- codex: BLOCKER — A status timestamp cannot be the sole acceptance test for recovery, especially on known-failing media. Require a fresh completed base backup from the intended source, advancing archiver success, a readable continuous WAL chain, a complete independent dump generation and measured RPO/RTO. Perform a minimal isolated restore and application-data checks before declaring #592 resolved. The broader estate DR drill may remain in #596; omitting all restore verification cannot support the promised recovery guarantee. -->
6. A `Backup` object reaches `phase: completed`, and the `infra-pg-backups` bucket is non-empty.
<!-- codex: HIGH — Check backup identity/source/time, completion metadata and readable object contents; an old backup or a bucket containing only partial uploads/WAL passes these weak checks. Restore an isolated cluster using the documented serverName/path, credentials and CA without writing to the production archive. Verify a recovery point after the base backup to exercise WAL replay, not merely base-backup extraction. Separately check a completed PodVolumeBackup for the holder's exact volume, restore its newest complete dump and verify the direct off-site copy. -->
7. **Then** re-clone: `kubectl cnpg destroy infra-pg 3 --keep-pvc`, and confirm
   `pg_stat_replication` returns 1 row with `state=streaming` and the slot returns to
   `active=t, wal_status=reserved`.
<!-- codex: HIGH — The destructive command must explicitly select --context admin@ai and -n databases; the runbook warns the default context is a DIFFERENT cluster. Re-identify the current primary and stale standby immediately before execution; never assume instance 3 is still the safe target. CNPG destroy --keep-pvc detaches/preserves the old PVC and removes the instance; it does not reuse that volume for an in-place repair. Budget a replacement 20Gi claim and verify scheduling/anti-affinity, NAS provisioning and the new instance/slot identity, which may differ from 3. Confirm sustained streaming, small receive/replay byte lag and archive catch-up; wal_status=extended can also be valid, so reserved alone is unnecessarily strict. Retain the detached PVC until validation and define cleanup/abort handling. See [CNPG destroy semantics](https://cloudnative-pg.io/documentation/1.24/kubectl-plugin/#destroy). -->
8. Each new alert rule returns a non-empty series from the Prometheus API — a rule matching zero
   series is the defect being fixed, so this check is mandatory, not optional.
<!-- codex: HIGH — This acceptance condition is backwards for healthy alert expressions: comparison-filtered alerts SHOULD return no series when healthy. Verify that input/recording metrics exist with expected target cardinality, rules are loaded without evaluation errors, healthy alerts are inactive, failures fire and recovery resolves. Use fixtures or a safe isolated canary for failure paths and verify notification delivery. Requiring every alert expression to be non-empty would reward exactly the permanent vector(0) defect. -->
9. `up{job=~".*etcd.*"}` returns 3 series.
<!-- codex: HIGH — Three series may all be zero. Require three unique intended endpoints with up == 1 over several scrape intervals, successful HTTP metric retrieval, the fsync histogram/count series and loaded/evaluating etcd rules. Also test missing-one and missing-all endpoint detection. -->
10. A chat completion against the default model succeeds through the fallback.
<!-- codex: MEDIUM — Exercise both regular and fast aliases, including streaming and fast background-task behavior. Verify from routing logs/returned model that fallback actually executed, bound refusal/timeout behavior, and verify recovery to the preferred route. A successful direct call to the fallback model does not test the router mapping. -->

Explicitly out of scope, and stated so rather than implied: no restore drill is performed by
this plan (#596 remains open), the USB hardware is untouched (#595), and no OpenBao rotation is
attempted (#602).

<!-- codex: HIGH — Keep OpenBao rotation and a full estate restore exercise out of scope if desired, but document how existing role passwords and backup/decryption keys are recoverable without the failed cluster. The minimal restore and reliable destination required above are dependencies of #592, not optional scope expansion. Separate independent Prometheus/etcd/LiteLLM changes from the risky database rollout so an unrelated Helm/configuration failure cannot delay restoring redundancy. -->

<!-- codex-review-status: complete -->
