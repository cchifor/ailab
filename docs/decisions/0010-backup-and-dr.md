# ADR 0010 — Backup & disaster recovery for stateful volumes

**Status:** ACCEPTED (2026-06-15) — Layer A (in-cluster snapshots) live; Layer B (off-NAS DR) deferred.
**Relates to:** ADR 0007 (k8s storage), the QNAP iSCSI CSI work.

## Context
The QNAP is the **single failure domain** for all network storage (`nfs-csi` + `qnap-iscsi`). ZFS
RAID-Z1 protects against a *disk* failure, not NAS controller death / pool corruption / theft / fire.
Before the platform there was **no restore path at all**: Talos ships no snapshot stack, so the cluster
had no external-snapshotter, no VolumeSnapshot API, and no off-site copy. The durable block payload is
small — essentially the ~10 GiB Prometheus TSDB on `qnap-iscsi` (Retain); Loki/Open-WebUI are nfs-csi.

## Decision
Two layers, built in order. **Layer A now; Layer B deferred** (user choice — start with same-NAS
point-in-time recovery, add off-site DR when warranted).

**Layer A — in-cluster snapshots (LIVE):** vendor the upstream **external-snapshotter v8** (3 CRDs +
`snapshot-controller`, `registry.k8s.io/sig-storage` — explicitly NOT the stale **staging** v5.0.0 image
the QNAP plugin vendors) at `kubernetes/apps/infrastructure/storage/snapshot-controller/`, plus a
`VolumeSnapshotClass qnap-iscsi` (driver `csi.trident.qnap.io`, `deletionPolicy: Delete`) in
`qnap-storage/`. The Trident controller **already runs a `csi-snapshotter` sidecar** (verified 6/6
live), so no Trident change was needed. Gives fast "oops" recovery (drop a bad migration, restore a PVC
from a ZFS snapshot) — but the snapshot lives on the same NAS, so it is **not** DR.

**Layer B — off-NAS DR (DEFERRED, documented):** **Velero + CSI data-mover (kopia)** streaming snapshot
data to **Cloudflare R2** (S3-compatible, zero egress, the Cloudflare account already exists). Verified
gotchas to apply when built: R2 rejects streaming checksums → `checksumAlgorithm: ""` +
`s3ForcePathStyle: true` + `region: auto` in the `BackupStorageLocation`; creds via a new SOPS secret.
The `VolumeSnapshotClass` is pre-labelled `velero.io/csi-volumesnapshot-class: "true"` so Velero adopts
it later. Optionally pair with a `talosctl etcd snapshot` → R2 cron for full cluster-DR (Velero backs up
API objects + PV data, not the Talos-managed etcd).

## Rejected
- **Longhorn / Rook-Ceph replicated CSI** — would remove the NAS SPOF for block, but costs ~1 vCPU +
  1–2 GiB RAM/node + 3× NVMe, colliding with the AI LXC budget (ADR 0009). Revisit only if a true
  zero-RTO HA stateful workload (e.g. a primary DB) appears.
- **Backups to a second dataset on the same QNAP** — same failure domain; not DR.
- **Velero restic/fs-backup** instead of CSI snapshot + data-mover — crash-consistent, slower, no true
  point-in-time; the data-mover path is preferred when Layer B is built.

## Consequences
- Recoverable from accidental deletion/corruption *today* via VolumeSnapshots; **still exposed to a QNAP
  loss** until Layer B ships — this is an accepted, documented residual risk for now.
- One more small controller in `kube-system` (snapshot-controller, 2 replicas).
- Snapshot/restore is k8s-native; validated by a round-trip test (snapshot a PVC, restore into a new PVC,
  confirm data).
