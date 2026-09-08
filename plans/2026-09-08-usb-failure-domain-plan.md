# Remove the USB backup disk from the critical path — 2026-09-08

## Context

On 2026-09-08 a USB-attached disk on the QNAP wedged and took the git forge down for ~4.5 hours.
The chain, measured live:

1. `/dev/sdb2` sits on a JMicron USB-SATA bridge. Its USB bulk transport stalled. Kernel:
   `usb_stor_bulk_transfer_sglist`, then `INFO: task jbd2/sdb2-8 blocked for more than 246 seconds.
   state:D`, then `ext4_read_bh_lock`. Every read on that filesystem hung. `df` still answered
   (cached statfs); reads did not.
2. **versitygw** — a single-node S3 gateway serving a POSIX backend on that disk — kept **accepting
   TCP but never answering**. `netstat` showed `LISTEN` with `Recv-Q 1256`: a backed-up accept queue.
3. Its `watchdog.sh` checks that the process *exists*, so it never restarted it. 25 watchdog copies
   piled up in D-state.
4. **Gitea** blocked at startup on `Creating Minio storage at 192.168.1.225:7070`, never bound
   `:3000`, was liveness-killed (exit 137), and entered CrashLoopBackOff.
5. `gitea-http` is a **headless** Service. No ready endpoints means DNS returns NXDOMAIN, which is
   why Flux reported `no such host` rather than a connection error.
6. **Flux reconciles from that in-cluster Gitea**, so GitOps could not self-heal.

Buckets on the USB disk: `velero` 239 G, `talos-etcd-backups` 12 G, `gitea-actions` 11 G
(`actions_artifacts` 9.1 G + `actions_log` 1.5 G).

Nothing alerted on versitygw itself. The only probe available, `storage-fabric-probe`, is a
blackbox **`tcp_connect`** module — which stayed green throughout, because the port was accepting.
The first alert was `GiteaForgeDown`, 1.5 h after the stall.

### Requirement

> If the USB is not working, the backup system should report an error, but not block important
> systems like Gitea. We will not have backups until the issue is fixed, but the system will work.

## The five defects

| # | Defect | Fix |
|---|---|---|
| 1 | Gitea's **operational** data lives on the **backup** medium | W1 — move Actions storage to a PVC |
| 2 | Failures **hang** instead of erroring within a bounded time | W2 — deadlines on both backup consumers |
| 3 | The watchdog tests **liveness**, not the ability to answer | W3 — real request, bounded attempts, alert |
| 4 | **Nothing monitors versitygw**, and the available probe is `tcp_connect` | W4 — authenticated round-trip probe |
| 5 | **Recovery depends on the forge that failed** | W5 — source the bootstrap layer from the mirror |

Defects 3 and 4 are the same blind spot twice: both test "is it alive?" when the failure mode was
"alive but unable to answer."

## Design decisions, and what was rejected

**Actions storage → a separate 40 Gi `qnap-iscsi` PVC.**
Rejected `nfs-csi` despite its space-reclamation advantage. The original argument for it was that the
QuTS thin LUN does not honour SCSI UNMAP — but ext4 **reuses freed blocks**, so the zvol plateaus at
its high-water mark rather than growing without bound. Measured pool headroom is 5.5 T free on
ZFS2_DATA and 4.7 T on ZFS18_DATA, so a 40 Gi LUN plateauing near ~15–20 G is rounding error.
Decisive argument: Gitea's existing 20 Gi PVC is **already** `qnap-iscsi`, so this adds **no new
failure domain**, whereas NFS would add a second protocol and client stack — and a stalled NFS mount
is a classic D-state source, which is the exact failure class being fixed. `nfs-csi` is also unproven
here (zero PVCs use it) and its PVC size is not enforced without a NAS-side quota.
*Caveat retained:* iSCSI is not immune to D-state either, and `recovery_tmo=2s` is not a two-second
bound on all storage hangs — W6 tests that separately.

**Separate PVC, not an enlargement of the existing 20 Gi volume.** Blast-radius isolation: Actions
churn must not be able to fill the volume holding repos and config.

**Shared-QNAP-kernel coupling: accepted residual risk, documented.** A separate backup host would
isolate it further, but during the full 4.5 h stall every `qnap-iscsi` PV kept serving, the cluster
stayed healthy, and QNAP mgmt answered in 11 ms — only Gitea broke, and only via the S3 dependency.
Bound the gateway's resources and concurrent requests, and keep monitoring off the USB filesystem.

## Workstreams

### W1 — Gitea Actions off the backup medium

Replace the `storage.actions_s3` minio section with a local backend on a new 40 Gi `qnap-iscsi` PVC,
mounted separately from the 20 Gi data PVC. Keep `actions_log` and `actions_artifacts` on distinct
paths beneath that mount so they cannot collide.

**Remove ONLY the two `MINIO_*` entries from `additionalConfigFromEnvs`.** That same block also
carries `GITEA__metrics__TOKEN` and the `[database] PASSWD` for the infra-pg `gitea` role — removing
it wholesale would break Gitea's database authentication. The `gitea-actions-s3` secret can go once
nothing references it.

Verify the *rendered* pod and effective config, plus rootless write permissions on the new mount.
Add free-space and inode alerts for the new PVC; retention (`ARTIFACT_RETENTION_DAYS: 14`,
`LOG_RETENTION_DAYS: 14`) bounds steady state at ~11 G, but alerts must exist because a separate PVC
protects repository capacity while itself becoming a required Gitea mount.

Note: the file's `gitea.db` / SQLite comments are stale — `DB_TYPE: postgres` since 2026-07-22.
DBFS backlog now pressures **Postgres**, not a SQLite file. Moving the destination does not by itself
prove the previously-failing large-log transfers will now succeed; validate one explicitly.

### W2 — Bounded failure for the backup consumers

- **Velero:** set `fsBackupTimeout`. `ItemOperationTimeout` is the wrong mechanism — this estate uses
  filesystem backup (Kopia) since 2026-08-30, so item-operation timeouts do not bound those transfers.
- **talos-backup:** add `activeDeadlineSeconds` at `jobTemplate.spec`; keep `concurrencyPolicy: Forbid`.
- Choose bounds from observed healthy durations; verify cancellation actually releases workers.
- Resolve the Sunday 02:00/03:00 daily-vs-weekly overlap, and bound off-site replication separately.

### W3 — Watchdog that tests the answer

Rewrite `watchdog.sh` to issue a real, timed request. Keep the supervisor, its working directory,
lock and logs **off the USB disk**. Prevent overlapping runs (25 copies accumulated). Bound restart
attempts; start a replacement only after the previous process exits; on persistent failure **stop
retrying and alert for storage intervention**.

A restart cannot repair uninterruptible disk I/O — a timeout detects failure, it cannot guarantee the
process dies. Never auto-reboot the shared NAS: that would violate the availability objective.

### W4 — Monitoring that proves it can serve

Authenticated **PUT → GET (verify content) → DELETE** against a **dedicated health bucket**, with an
overall deadline, plus an alert within minutes on failure or missing probe results. Do not write probe
objects into Velero's bucket.

A plain HTTP check is insufficient: an auth rejection (403) returns without touching the backend, so
it would likely have been **green throughout this incident**. Keep the existing backup-freshness
alerts; they answer a different question.

### W5 — Break the bootstrap loop

Permanently repoint the existing `flux-system` GitRepository at the GitHub push-mirror **in committed
history**, preserving its name and paths. A live URL patch alone gets reverted by reconciliation.
Application repositories stay on Gitea.

Hazards to respect:
- **`prune: true` means an older mirror can revert or delete newer resources.** Confirm the mirror's
  HEAD matches Gitea's before switching.
- **Emergency write policy:** Gitea stays the sole writer; monitor replication lag. During recovery,
  pause or revoke mirror writes *before* committing directly to GitHub, then bring Gitea onto the
  repaired history before resuming mirroring — otherwise recovery commits get overwritten.
- Give Flux **read-only** GitHub credentials. Any `ImageUpdateAutomation` on the switched source needs
  separate write routing or suspension.

### W6 — Narrow the dependency that can gate forge repair

`apps` declares `dependsOn: infrastructure` with `wait: true`, and `infrastructure` includes
`storage`, which contains Velero and talos-backup. **Gitea lives in `apps`.** So a backup-layer
failure can, in principle, block the reconciliation that would repair the forge — GitHub sourcing
alone would not break that loop.

This did not bite during the incident (all Kustomizations stayed `True` because Velero's Deployment
does not touch S3 at startup), but the coupling is real. Separate backup reconciliation from the
storage/database prerequisites `apps` genuinely needs, or narrow the dependency.

Also verify `recovery_tmo=2s` behaviour with a deliberate iSCSI recovery test.

## Rollout order

The config change must reach the cluster **through the very Gitea + Flux path that breaks**, so
recovery capability is established *before* anything is touched.

1. **Pre-stage recovery, off Gitea and off the USB disk.** A current repository checkout; reviewed
   source-switch, cutover and rollback manifests; admin cluster credentials; working GitHub
   credentials and SSH host trust; escrowed `sops-age` material. **Verify the GitHub secret actually
   works** — a comment mentioning it is not proof.
2. **While Gitea is healthy, establish GitHub sourcing (W5).** Commit the persistent source change,
   confirm that exact revision exists on GitHub, then reconcile. Resolve W6's dependency first.
3. **Prove independent repair before touching storage.** With Gitea stopped, demonstrate a **fresh
   GitHub fetch and application** of a harmless bootstrap change. Reconciling from a cached artifact
   does not count. Keep a direct `kubectl apply` path available as a fallback.
4. **Ship W2, W3, W4. Provision the Actions PVC but do not mount it yet.** Verify permissions,
   pre-copy the 11 G, and pre-stage cutover and rollback payloads on the independent recovery path.
5. **Controlled maintenance window.** Drain Actions; suspend the relevant reconciliation/Helm
   operations; **stop Gitea** so its internal offload/cleanup writers stop too — draining runners
   alone is not enough. Take a database backup. Final verified copy, mapping each S3 prefix into its
   corresponding directory **without duplicating the prefix**. Release the migration helper's RWO
   attachment before starting Gitea on local storage.
6. **Run acceptance, then resume writers and reconciliation.** Retain the source data.

## Acceptance

The requirement is only met when all of these hold:

- **Gitea starts and serves with `:7070` unreachable** — tested in *both* modes: connection refused,
  **and TCP-accepted-but-silent** (the actual failure), from a **cold** start.
- Git operations succeed, and **Flux reconciles**, with the S3 endpoint down.
- Old and newly-produced Actions logs and artifacts are both readable.
- A previously-failing large CI log completes transfer, is viewable, and is later cleaned up.
- Backups **fail within their configured deadline** rather than hanging, and emit alerts.
- The versitygw probe **fails** when the backend is unreadable — verified by inducing the condition,
  not merely by observing a healthy green.
- With Gitea stopped, a fresh GitHub fetch applies a change.

## Rollback

Before cutover, rollback is: revert the config commit, Gitea returns to S3.

**After local writes begin, rollback is no longer symmetric** — restoring the old S3 configuration
would expose stale data. Rollback then requires reconciling the new local writes back. Retain the
source data until acceptance passes.

## Out of scope, flagged

**The Velero manifest claims a live PostgreSQL filesystem copy is a valid crash-consistent restore
source. That is incorrect** — copying changing files is not an atomic crash image, and WAL replay
does not generally repair it. Both CNPG clusters have no native backup (`.spec.backup` empty), so
this would be their only copy. Needs database-native backup plus a proven restore. Independent of
this work, but higher severity than anything in it.

Also: Zot's GC window is shorter than Renovate's PR lifecycle (two pin PRs 404'd within hours), and
branch protection requires only **1** approval with `enable_status_check=False`, so the "both bots
approve, CI green" convention is not enforced server-side.

<!-- codex-review-status: round-2 adjudicated -->
