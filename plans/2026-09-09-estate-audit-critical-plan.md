# Estate-audit critical remediation (#592–#601)

## Context

The 2026-09-09 estate audit found the infrastructure healthy and the data-protection layer
not. Confirmed by hand:

- `infra-pg` — the shared Postgres behind Gitea, Authelia, Grafana, Open WebUI, LiteLLM and
  the AgentForge platform — has had **no replica since 2026-09-02**: `pg_stat_replication` = 0
  rows, slot `_cnpg_infra_pg_3` `active=false wal_status=lost`, 228 GB behind, last replay
  `2026-09-02 08:45:25`. CNPG reports `readyInstances=2, Cluster in healthy state`.
- There is nothing to re-seed from: `.spec.backup`, `.status.lastSuccessfulBackup` and
  `.status.firstRecoverabilityPoint` are all empty; `get scheduledbackups` returns none.
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

1a. Add `spec.backup.barmanObjectStore` to `infra-pg`, pointing at a **new, separate**
    versitygw bucket `infra-pg-backups` — not the `velero` bucket, which rejects unexpected
    top-level directories (that broke a copy for 5 days on 2026-08-10). Reuse the existing
    versitygw credential pattern; the endpoint is `https://192.168.1.225:7070` with
    `s3ForcePathStyle` and the self-signed cert already trusted by the Velero path.
    `wal.compression: gzip`, `data.jobs: 1` (one small cluster, no need to parallelise).

1b. Add a `ScheduledBackup` at 02:30 daily (offset from Velero's 02:00 so they do not contend
    for the same NAS spindle), `backupOwnerReference: self`, retention via
    `spec.backup.retentionPolicy: "14d"`.

1c. Raise `max_slot_wal_keep_size` 1GB → 4GB **only after 1a is live**. At the measured
    9.7–12.9 MiB/s peak WAL rate, 1 GB is ~80 seconds; 4 GB is ~5 minutes, which covers a
    replica restart. Worst case on the 20 GiB volume becomes DB ~6 GiB + `max_wal_size` 4 GB +
    4 GB slot retention ≈ 14 GiB of ~19.5 GiB usable — still headroom, and now a blown cap is
    recoverable from the archive rather than terminal.

1d. **Correct the false rationale** in `infra-pg.yaml`. The comment claims "the lagging
    replica's slot is invalidated and CNPG re-clones it". CNPG 1.24.1 does not; seven days
    later there was no re-clone. That sentence is why this went unexamined.

1e. Codify the nightly logical dump that currently exists only as a hand-applied one-shot:
    a `CronJob` writing per-database `pg_dump -Fc` to the existing `postgres-dumps` PVC, with
    `pg_restore --list` verification and atomic publish. **The PVC must be mounted by a running
    pod for Velero fs-backup to capture it** — an unmounted PVC is skipped by design, which is
    why the current dump has zero PodVolumeBackups. The CronJob alone does not fix that, so it
    also prunes to the last 7 days and the plan adds the PVC to a long-lived mount (see 1f).

1f. Mount `postgres-dumps` read-only into a minimal always-running pod so Velero's fs-backup
    sees it. Without this the dumps are invisible to every backup, which is the current state.

### 2. Make the replication failure detectable (#593, and the class in #600)

Add to `ha-rules.yaml`:

- `PostgresReplicaNotStreaming` — `cnpg_pg_replication_is_wal_receiver_up == 0`, `for: 10m`,
  critical. Covers both CNPG clusters.
- `PostgresReplicationLagHigh` — `cnpg_pg_replication_lag > 900`, `for: 15m`, warning.
- Both must carry an `absent()`/`or vector()` guard: the audit measured the `infra-pg` metrics
  target **flapping down** (`Get ".../metrics": EOF`), so a naive rule goes silent rather than
  firing — the exact failure mode being fixed.
- Rename `InfraPostgresSingleInstance` → `InfraPostgresPodCountLow` and reword its annotation
  so it no longer reads as replication coverage. It is a valid pod-loss alert; it was only ever
  mislabelled.

Add the corresponding `backup-rules.yaml` entry `VeleroBackupFailing` on
`velero_backup_failure_total` — PartiallyFailed is alerted today, Failed is not.

Add `CSIVolumeDeleteFailing` on the `VolumeFailedDelete` event rate (#601). 57,818 events and
climbing, 240 failed QNAP API calls/hour against the API that serves every qnap-iscsi PVC.

### 3. Stop the alerting plane from dying silently (#597)

- Prometheus `limits.memory` 2Gi → 3Gi. Requests stay 1Gi.
- Exempt Prometheus from the `monitoring` LimitRange CPU default. The LimitRange injects
  `cpu: 500m` the HelmRelease never asked for, throttling 7.6–19.5 % continuously against
  0.109 cores of real use. Set an explicit generous `cpu` limit in `prometheusSpec.resources`
  so the LimitRange default no longer applies.
- Add `MonitoringContainerNearMemoryLimit` — `working_set / limit > 0.9`, `for: 15m`, warning,
  scoped to `namespace="monitoring"`. Generic, so it covers Loki and Alertmanager too.

### 4. Give etcd telemetry and catch restart waves (#598)

- `kubeEtcd: { enabled: true }` with endpoints `192.168.0.41/.42/.43`, port 2381 (Talos serves
  etcd metrics unauthenticated on 2381, so no client certs are needed). This loads the chart's
  four etcd rules, `etcdHighFsyncDurations` being the one that matters given measured
  `slow fdatasync` up to 8.5 s.
- Add `ControlPlaneRestartWave` — `sum(increase(kube_pod_container_status_restarts_total{...}[10m])) > 8`
  over control-plane namespaces. The per-pod `KubePodCrashLooping` cannot express "many things
  restarted at once": each container restarted once and recovered, so nothing entered
  CrashLoopBackOff.

### 5. Restore the default chat model (#599)

- Add `fallbacks` to the LiteLLM router mapping `qwen3.8-27b-vllm-cloud` and
  `qwen3.8-27b-vllm-fast-cloud` to a reachable route. `192.168.0.26:8080` answers from the
  litellm pod (verified). Because that is a *different* engine, the fallback entry must not
  inherit `:18020`'s `chat_template_kwargs` / `enable_thinking` sampling — declare the fallback
  target as its own `model_name` with its own params.
- Add a Gatus endpoint for `192.168.0.28:18020` so this is not next discovered by a user typing
  into the chat box.

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

## Verification

Static, before merge:

1. `scripts/rules-lint.sh` — `promtool check rules` over every PrometheusRule.
2. `promtool test rules` against the `.test.yaml` files. **Every new alert gets a unit test
   that fails when the rule is inverted** — the audit's central finding is rules that cannot
   fire, so "it loads" is not evidence.
3. `scripts/manifest-lint.sh` — `kustomize build` + `kubeconform` for every touched overlay.
4. `sops --decrypt` round-trip on the new credential, and a grep asserting every secret leaf is
   `ENC[` before commit. The file must be named to match the `encrypted_regex` for its path or
   SOPS silently writes plaintext.

Live, after Flux reconciles:

5. `kubectl -n databases get cluster infra-pg -o jsonpath='{.status.firstRecoverabilityPoint}'`
   becomes non-empty — this is the single acceptance test for #592.
6. A `Backup` object reaches `phase: completed`, and the `infra-pg-backups` bucket is non-empty.
7. **Then** re-clone: `kubectl cnpg destroy infra-pg 3 --keep-pvc`, and confirm
   `pg_stat_replication` returns 1 row with `state=streaming` and the slot returns to
   `active=t, wal_status=reserved`.
8. Each new alert rule returns a non-empty series from the Prometheus API — a rule matching zero
   series is the defect being fixed, so this check is mandatory, not optional.
9. `up{job=~".*etcd.*"}` returns 3 series.
10. A chat completion against the default model succeeds through the fallback.

Explicitly out of scope, and stated so rather than implied: no restore drill is performed by
this plan (#596 remains open), the USB hardware is untouched (#595), and no OpenBao rotation is
attempted (#602).

<!-- codex-review-status: pending -->
