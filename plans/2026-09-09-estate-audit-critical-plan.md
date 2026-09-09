# Estate-audit critical remediation (#592–#601)

## Context

The 2026-09-09 estate audit found the infrastructure healthy and the data-protection layer
not. Confirmed by hand:

- `infra-pg` — the shared Postgres behind Gitea, Authelia, Grafana, Open WebUI, LiteLLM and
  the AgentForge platform — has had **no replica since 2026-09-02**: `pg_stat_replication` = 0
  rows, slot `_cnpg_infra_pg_3` `active=false wal_status=lost`, 228 GB behind, last replay
  `2026-09-02 08:45:25`. CNPG reports `readyInstances=2, Cluster in healthy state`.
- `.spec.backup`, `.status.lastSuccessfulBackup` and `.status.firstRecoverabilityPoint` are all
  empty; `get scheduledbackups` returns none. There is no independent backup — but, per review,
  that is **not** the same as no re-seed source: the surviving primary can supply
  `pg_basebackup`. Restoring redundancy therefore does not depend on first building an archive,
  and nothing built now can recover the WAL already lost since 09-02.
- `InfraPostgresSingleInstance` — the alert written for exactly this — counts **ready pods**,
  evaluates to `2`, and read all-clear for seven days.
- Prometheus sits at 93–96 % of a 2 GiB limit with liveness probes already timing out, and no
  rule watches memory-vs-limit in `monitoring`.
- Open WebUI's default model points at `192.168.0.28:18020`, which is `ConnectionRefused`,
  with zero LiteLLM fallbacks configured.

### What the codex review changed

The first draft proposed WAL archiving to a new versitygw bucket. **That is dropped.** The
review's blocking objection is correct and decisive: if the archive destination stalls,
unarchived WAL accumulates in `pg_wal` regardless of `max_slot_wal_keep_size`, and the
destination is the USB disk that has failed twice in two days (#595). Measured live:
`/var/lib/postgresql/data` is 20 G with **12 G free**; at the observed 9.7–12.9 MiB/s peak WAL
rate a stalled archive consumes that in ~16 minutes and takes the **primary** down — strictly
worse than today's degraded-but-serving state. Trading "no replica" for "no primary" is not a
remediation.

Also dropped after live verification (both would have shipped as rules that cannot fire, the
exact defect this work exists to remove):

- **kubeEtcd.** Probed from inside the cluster: `192.168.0.41/.42/.43:2381` all return
  `http=000`. Talos does not expose etcd metrics without `cluster.etcd.extraArgs.listen-metrics-urls`.
  Enabling the chart flag alone yields a ServiceMonitor with no target.
- **`CSIVolumeDeleteFailing`.** `VolumeFailedDelete` is a Kubernetes Event reason, not a
  Prometheus series. No event-to-metric exporter exists in this estate, so the rule would match
  nothing.

Corrected after live verification:

- The cnpg metrics job label is **`databases/infra-pg-metrics`**, not `kube-prometheus-stack-cnpg`.
  The draft's selector would have been permanently empty.
- `postgres-dumps` is **30Gi, ReadWriteOnce, qnap-iscsi, Bound** — so a writer and a holder pod
  must be co-scheduled on one node; RWO cannot attach across two nodes.
- Gatus lives at `kubernetes/apps/apps/gatus/configmap.yaml`.
- `192.168.0.26` shares the nightly cloudlab power-off with `.28`, so it is not a 24/7 fallback.
  `llm-node2.ai.svc.cluster.local:8080` (node2, `.45`) is open and always-on, and is the correct
  terminal fallback.

Out of scope, stated rather than implied: **#595** is hardware; **#602** is an operator
decision; **#594**'s manifest lives in `cchifor/platform`; **#596**'s restore drill is a separate
gated exercise. Their dependencies are named below rather than hidden.

## Approach

Per review, this ships as **two independent changes** so a Helm or config failure in the
low-risk half cannot delay restoring database redundancy.

---

### Change A — monitoring and availability (low risk, ships first)

**A1. Make the replication failure detectable (#593).** In `ha-rules.yaml`, using the verified
job label:

- `PostgresReplicaNotStreaming` — standby-scoped, joined to recovery state so a healthy primary
  (which has no WAL receiver by definition) cannot page:
  `cnpg_pg_replication_is_wal_receiver_up == 0 and on(namespace,pod) cnpg_pg_replication_in_recovery == 1`,
  `for: 10m`, critical.
- `PostgresReplicationLagHigh` — `cnpg_pg_replication_lag > 300`, `for: 10m`, warning. 300 s not
  900 s: the review is right that `>900` plus `for: 15m` is ~30 minutes of blindness, which is
  longer than the whole slot-retention window.
- `PostgresMetricsTargetDown` — `up{job="databases/infra-pg-metrics"} == 0`, `for: 10m`. This is
  the correct fix for the flapping target, **not** `absent(...) or vector(0)`. The review's
  PromQL analysis is right: `vector(0)` returns an unlabelled series whose presence makes the
  alert fire permanently and never resolve. Per-target `up == 0` also survives the case where
  one target fails while another still exports the metric, which a single `absent()` misses.

Rename `InfraPostgresSingleInstance` → `InfraPostgresPodCountLow` and reword its annotation so
it no longer reads as replication coverage.

**A2. `VeleroBackupFailing`** in `backup-rules.yaml` — on `increase(velero_backup_failure_total[6h]) > 0`
with schedule scope, not the lifetime counter being non-zero. Failed is currently unalerted
while PartiallyFailed is.

**A3. Prometheus headroom (#597).** `limits.memory` 2Gi → 3Gi, and `requests.memory` 1Gi → 2Gi
— per review, a 3Gi limit against a 1Gi request schedules only 1Gi of real capacity. Set an
explicit `cpu` limit so the `monitoring` LimitRange stops injecting its 500m default (measured
7.6–19.5 % continuous throttling against 0.109 cores of use). Add
`MonitoringContainerNearMemoryLimit` on working-set / limit > 0.9, excluding containers with no
limit so an unlimited container cannot divide by zero.

**A4. `ControlPlaneRestartWave`** — `count(increase(kube_pod_container_status_restarts_total{namespace=~"..."}[10m]) > 0) > 8`.
`count(... > 0)` not `sum(...)`: per review, a sum is satisfied by one container restarting nine
times, which is a crashloop, not a wave. The 00:10–00:11Z incident was 24 *distinct* containers
each restarting once.

**A5. Restore the default chat model (#599).** Add a `fallbacks` chain to the LiteLLM router:
`qwen3.8-27b-vllm-cloud` → `qwen3.5-122b-cloud` (`.26`, daytime) → a new route on
`llm-node2.ai.svc.cluster.local:8080` (always-on). Same for the `-fast` alias. The fallback
target is declared as its own `model_name` with its own sampling — it is a different engine and
must not inherit `:18020`'s `chat_template_kwargs`. **Add a config checksum annotation to the
Deployment**: per review the ConfigMap is read only at startup and there is no Reloader, so
without it Flux applies the change without activating it.

**A6. Gatus probe** for `:18020` at `kubernetes/apps/apps/gatus/configmap.yaml`, as an HTTP
completion check rather than a TCP connect, with the nightly cloudlab power-off window
accounted for so it does not page every night.

### Change B — database redundancy and independent backup (#592, gated)

Ordered so that redundancy is restored **first and independently**, because it needs only the
surviving primary.

**B1. Correct the false rationale** in `infra-pg.yaml`. The comment claims "the lagging
replica's slot is invalidated and CNPG re-clones it". CNPG 1.24.1 does not; seven days later
there was no re-clone. Replace it with what is true, and explicitly **not** with an archive
self-healing promise.

**B2. `max_slot_wal_keep_size` stays at 1GB.** The draft proposed 4GB; dropped. Review
arithmetic: 4 GB buys 5.3–7.0 minutes at peak, against documented ~13-minute node recovery
paths — so it does not actually cover the case it was justified by, while consuming headroom on
a shared data+WAL volume. Without an archive it changes nothing about recoverability. Left
alone, and the reasoning recorded.

**B3. Nightly logical dump, codified (`infra-pg-dump-cronjob.yaml`).** This is the independent
backup, and it touches neither `pg_wal` nor the USB path:

- Covers **all eight databases plus `pg_dumpall --globals-only`**, using the `backup_dumper`
  role with `pg_read_all_data` **and `BYPASSRLS`** — per review and confirmed by the 09-08
  results, per-owner dumps silently missed RLS-protected rows in `agentforge_platform`. The role
  and its credential are codified here rather than left as the hand-made artefact they are today.
- Connects to `infra-pg-rw`, never the stale replica.
- `concurrencyPolicy: Forbid`, `startingDeadlineSeconds`, `backoffLimit`, explicit resources.
- Writes to a staging directory and publishes the **whole generation** atomically with a
  completion marker; prunes only after the replacement generation verifies, so a failed run can
  never leave the PVC holding nothing.
- Verifies with `pg_restore --list` **and** records per-file sizes/checksums. The review is
  right that `--list` checks the TOC, not restorability — the real proof is B6.
- Retention sized against measured reality: ~2.14 GiB per generation on a 30Gi RWO PVC → 7
  generations ≈ 15 GiB, leaving room for one in-flight generation plus growth.

**B4. Off-site leg for the dumps.** `rclone-cronjob.yaml` currently syncs exactly two legs
(`garage-velero:velero`, `garage-talos:talos-etcd-backups`). Add a third that copies the dump
generations **directly** to `gdrive-crypt`, bypassing versitygw and therefore the USB disk
entirely. Without this the only independent backup is on-site on failing hardware.

**B5. Make the dumps visible to Velero.** `postgres-dumps` is **RWO**, so a separate holder pod
can only attach if it lands on the same node as the writer. Rather than fight that, the holder
is a 1-replica Deployment with `podAffinity` to co-locate, mounting read-only, with no database
or cloud credentials and `automountServiceAccountToken: false`. Acceptance is a **completed
PodVolumeBackup naming this volume** — not merely "a pod exists".

**B6. Re-clone the standby — manually gated, after B3 has produced one verified generation.**
`kubectl --context admin@ai -n databases cnpg destroy infra-pg <n> --keep-pvc`, with the
instance re-identified immediately before execution rather than assuming `3`. Note per review
that `--keep-pvc` detaches and preserves the old PVC and provisions a **new** one — budget a
replacement 20Gi claim, and retain the detached PVC until validation passes.

## Critical files

**Change A**

| Path | Role |
|---|---|
| `kubernetes/apps/infrastructure/monitoring/ha-rules.yaml` | 3 replication rules; rename the pod-count alert |
| `kubernetes/apps/infrastructure/monitoring/ha-rules.test.yaml` | promtool fixtures for the above |
| `kubernetes/apps/infrastructure/monitoring/backup-rules.yaml` + `.test.yaml` | `VeleroBackupFailing` |
| `kubernetes/apps/infrastructure/monitoring/kube-prometheus-stack.yaml` | Prometheus requests/limits + explicit cpu limit |
| `kubernetes/apps/infrastructure/monitoring/k8s-rules.yaml` (or nearest existing group) + test | `MonitoringContainerNearMemoryLimit`, `ControlPlaneRestartWave` |
| `kubernetes/apps/apps/ai/litellm.yaml` | fallback chain, new always-on route, **checksum annotation** |
| `kubernetes/apps/apps/gatus/configmap.yaml` | `:18020` HTTP check |

**Change B**

| Path | Role |
|---|---|
| `kubernetes/apps/databases/infra-pg.yaml` | correct the false comment only |
| `kubernetes/apps/databases/infra-pg-dump-cronjob.yaml` | **new** — dump CronJob + holder Deployment |
| `kubernetes/apps/databases/postgres-dumps-pvc.yaml` | **new** — adopt the existing 30Gi PVC declaratively, without replacing its data |
| `kubernetes/apps/databases/infra-pg-dumper.sops.yaml` | **new** — `backup_dumper` credential (`kind: Secret`, `stringData`, so the `^(data\|stringData)$` rule encrypts it) |
| `kubernetes/apps/databases/kustomization.yaml` | wire the three new files in |
| `kubernetes/apps/backup/backup-offsite/rclone-cronjob.yaml` | third sync leg for the dumps |

## Verification

Static, before merge:

1. `scripts/rules-lint.sh` — it already extracts `spec.groups` and runs every `*-rules.test.yaml`,
   so reuse that gate rather than adding a parallel one.
2. `promtool test rules` covering, for each new alert: healthy primary, healthy standby, lagging
   standby, one target down, all targets down, counter reset, and the exact `for:` boundary.
   Assert **both** that it fires when it should **and resolves when it should** — per review,
   threshold inversion alone would not have caught the `vector(0)` never-resolving defect.
3. `scripts/manifest-lint.sh`, plus `kustomize build` of the databases and monitoring overlays
   and a `helm template` render of kube-prometheus-stack 86.2.3 to confirm the resource names the
   rules and Deployment patch actually target.
4. SOPS: assert every payload leaf under `data`/`stringData` is `ENC[` **before** committing,
   without printing decrypted values. A successful round-trip alone does not prove encryption.

Live, after Flux reconciles — Change A:

5. Each new rule is **loaded and evaluating without error**, and healthy alerts are **inactive**.
   Per review, requiring a non-empty result would be backwards and would reward exactly the
   `vector(0)` defect: a comparison-filtered alert *should* return nothing when healthy. What
   must be non-empty is the **input** series — `cnpg_pg_replication_lag`,
   `up{job="databases/infra-pg-metrics"}`, `container_memory_working_set_bytes` — at the expected
   cardinality.
6. `PostgresReplicaNotStreaming` fires within 10 minutes against the *current* broken state.
   This is the one alert whose firing path can be verified against production without inducing a
   fault, because the fault is already there — and it is the single strongest proof that #593 is
   closed.
7. LiteLLM: a completion against the default model succeeds, and the routing log shows the
   **fallback executed** rather than a direct call to the fallback model.

Live — Change B:

8. One dump generation completes, `pg_restore --list` passes on all eight files, globals are
   present, and the completion marker is written.
9. A **completed PodVolumeBackup naming the `postgres-dumps` volume** appears in a Velero backup.
10. The off-site leg reports the dump prefix present on `gdrive-crypt` with a matching object count.
11. **Restore proof, before #592 is closed**: restore the newest generation into a scratch
    database and check row counts on a known table in `gitea` and `authelia`. Per review, a
    status field is not an acceptance test; this is the minimum that justifies the claim. The
    broader estate DR drill stays in #596.
12. Only then B6, confirming sustained `state=streaming`, small replay lag, and a slot that is
    `active=t` — accepting `reserved` **or** `extended`, since both are valid.

## Known dependencies, not hidden

- PITR remains **absent** until #595 is resolved; this plan buys daily logical recovery, not
  point-in-time. Recorded on #592 rather than implied to be fixed.
- etcd telemetry (#598) needs a Talos machine-config change and a one-CP-at-a-time roll with
  quorum verification between each. Deliberately not bundled with a database change, and not
  attempted while the database has no verified backup.
- `CSIVolumeDeleteFailing` (#601) needs an event-to-metric producer that does not exist yet.

<!-- codex-review-status: complete -->
