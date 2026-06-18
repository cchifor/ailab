# ADR 0010 — Backup & disaster recovery for stateful volumes

**Status:** ACCEPTED (2026-06-15); Layer A live. **Layer B REVISED 2026-06-18** — the deferred
"Velero → Cloudflare R2" plan is replaced by "Velero → versitygw-on-QNAP-NVMe (local) + rclone-crypt →
Google Drive (off-site)" after the user ruled out paid R2 (has 5 TB Google One). Design + engine choice
were workflow/adversarially verified; manifests are pre-staged on `feat/backup-velero` and deploy once
the user provisions versitygw + the Drive OAuth token + an age key. The single load-bearing DR fix (the
Talos secrets bundle off-site) is already merged (PR #28).
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

**Layer B — off-NAS DR (REVISED 2026-06-18, free 3-2-1, pre-staged on `feat/backup-velero`):** a real
3-2-1 with no paid object storage. **Copy 2 (local, authoritative)** = single-node **versitygw**
(Versity S3 Gateway, **Apache-2.0** — chosen over AGPL Garage/MinIO for a clean license posture on the
commercial-Strive cluster; MinIO also rejected for CVE-2025-62506 + archived repo) in QNAP Container
Station on the USB-NVMe (POSIX backend, `--sidecar` for xattr-less mounts). **Copy 3 (off-site,
best-effort, encrypted)** = nightly one-directional `rclone sync` of the versitygw buckets through an
`rclone crypt` overlay to the user's 5 TB Google One/Drive (consumer; rate-limited + shared quota — the
weakest link, never the only copy). Producers, all Flux: (1) **Velero v1.18 (chart 12.0.3) + CSI snapshot
DATA MOVEMENT (Kopia)** → versitygw `velero` bucket — captures k8s API objects AND PV bytes; (2)
**talos-backup CronJob** (age-encrypted etcd snapshot via `kubernetesTalosAPIAccess` + a scoped
`talos.dev` ServiceAccount) → `talos-etcd-backups` bucket. **Verified config corrections:** the AWS SDK
only sends its trailing checksum over **HTTPS**, so versitygw is served over **HTTPS** + the BSL sets
`checksumAlgorithm: ""` (NOT `http://` + default CRC32, which fails client-side) + `s3ForcePathStyle:
true` + dummy `region`; the talos-backup leg sets `AWS_REQUEST/RESPONSE_CHECKSUM_CALCULATION=when_required`.
The `VolumeSnapshotClass` is already labelled `velero.io/csi-volumesnapshot-class: "true"`. **Total-loss
fix (DONE, PR #28):** the Talos secrets bundle (cluster/etcd/k8s CA + SA keys) lived ONLY in the
gitignored `terraform.tfstate`; it is now committed SOPS+age-encrypted at
`kubernetes/infra/talos-secrets-bundle.sops.yaml` (DR-only, never applied) so `talosctl gen config
--with-secrets` can reproduce the PKI — without it `bootstrap --recover-from` regenerates a new bundle
→ CA/PKI mismatch → control plane never converges. **DR order:** secrets bundle → machine config →
`talosctl bootstrap --recover-from` → Flux/Trident reconcile → Velero restore (drill it). **Acceptance
gate before trusting:** prove a real Velero backup→delete→restore AND a talos-backup snapshot write+read
against versitygw over HTTPS (no published Velero-v1.18+versitygw golden report). Offline-escrow the
rclone crypt password/salt + the age private key + the SOPS age key, or the off-site copy is
permanently undecryptable in a real site loss.

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
