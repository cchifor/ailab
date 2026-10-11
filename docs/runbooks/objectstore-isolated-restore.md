# Objectstore isolated restore qualification

This is a staged plan for the recovery evidence required by
[platform #2219](https://git.chifor.me/cchifor/platform/pulls/2219), the private
HTTPS endpoint for Forge storage. It is not required for Document Insights.
Only the temporary read-only observer is implemented in this change. No Restore,
scratch workload, production storage restart or maintenance operation is enabled.

## Known backup and unresolved evidence

Observed on 2026-10-11: Backup `velero-weekly-20261011030034` and
PodVolumeBackup `velero-weekly-20261011030034-t2nnh` are Completed. The PVB
contains 58,388,168 bytes for pod `strive-ailab/objectstore-7fd787c788-4h44c`,
volume `data`, PVC UID `73726d9c-d190-4de9-8057-f17bc8996a3b`, Kopia snapshot
`4135d8921030b5297964a06193e26cbb`. The backup expires on 2026-11-10.
Recheck these identities and expiry before activation. No retained successful
Restore/PodVolumeRestore receipt has been found.

The committed chart is 12.0.3 / Velero 1.18.1; the actual deployed controller and
node-agent images remain to be observed. Installed OpenAPI v3 exposes
`Restore.spec.resourceModifier`. Do not infer unsupported fields from a failing
`kubectl explain` alone.

## Reviewable stages

| Stage | Change | Evidence before advancing |
| --- | --- | --- |
| Observe | Temporary named GET grants described in [platform access](dev-worker-platform-access.md#temporary-objectstore-restore-planning-observer-remove-by-2026-10-18) | Actual Velero images; production PV identity; storage-class behavior |
| Prepare | Separate namespace, default-deny networking, tokenless service account, new dynamically provisioned PVC, admission restrictions | Scratch PVC/PV UID and CSI handle differ from production; admission and network protections reconciled |
| Restore inertly | Exact backup selection, namespace mapping and tested resource modifiers | Restore and exactly intended PVR Complete; expected snapshot and destination volume identities |
| Qualify application | Separate verifier using pinned RustFS against the restored copy | Authenticated reads and expected content hashes match |
| Clean up | Reviewed removal of scratch resources and temporary access | Scratch data deleted; production identities, readiness and objects unchanged |

Do not combine destination preparation with Restore activation. Verify isolation
first. Future observation of scratch pods, PVCs, Jobs, events and protections
needs a separate scoped grant. Observe the eventual scratch PV by its exact
name, or use an operator's sanitized backing-volume receipt.

## Velero 1.18.1 constraints to prove before activation

The [restore implementation](https://github.com/vmware-tanzu/velero/blob/v1.18.1/pkg/restore/restore.go)
requires both PV and PVC resource types in the selection before it schedules
filesystem PVRs. Use `includedResources: [pods, persistentvolumeclaims,
persistentvolumes]` with `includeClusterResources: false`: the latter prevents
restoring cluster-scoped PV objects while retaining the filesystem path.
Verify this with a disposable synthetic fixture using the actual deployed
version before applying it to this backup.

Resource modifiers execute after the
[restore-wait injection](https://github.com/vmware-tanzu/velero/blob/v1.18.1/pkg/restore/actions/pod_volume_restore_action.go).
Replace application containers and sensitive mounts individually; never replace
the full Pod spec. Preserve `restore-wait`, the source pod name and volume name
for [PVB matching](https://github.com/vmware-tanzu/velero/blob/v1.18.1/pkg/podvolume/util.go).
The [restore helper](https://github.com/vmware-tanzu/velero/blob/v1.18.1/cmd/velero-restore-helper/velero-restore-helper.go)
waits for local files and does not require network access.

Modifier errors accumulate but can allow object creation to continue. Therefore
modifiers alone do not establish isolation. Reconciled namespace-scoped admission
must reject any restored pod that differs from the exact reviewed inert shape,
including Secret references, service-account tokens, host mounts/networking,
unexpected containers, owner references or volumes other than the scratch claim
and approved temporary volumes. Test failed and omitted modifiers against that
admission policy before activation.

`datauploads.velero.io` and `volumesnapshotcontents.snapshot.storage.k8s.io`
are mandatory resource types that can bypass ordinary selection. Cluster-resource
exclusion blocks VolumeSnapshotContents. Obtain a **sanitized resource inventory**
of this backup and confirm DataUploads are absent before activation. Setting
`snapshotVolumes: false` or `snapshotMoveData: false` alone is insufficient.
Do not download a backup archive to work around denied Secret access.

The temporary inventory from [ailab #1255](https://git.chifor.me/cchifor/ailab/pulls/1255)
returned an available receipt at **2026-10-11 14:50:51 UTC**, for the backup and UID
above: all **6,198** expected resource-list entries were counted, with **zero
DataUploads**. The acquired artifact SHA-256 is
`90724fd44d7621c636e3477f78057f58ab9315f01cc0bbca742dec7e62b03f96`.
The sanitized receipt was preserved before removing the temporary component,
including its Job, DownloadRequest, permissions and ConfigMaps. This establishes
the inventory prerequisite for this exact backup only; it is not restore-success
evidence or permission to restart production storage. A different backup needs
its own inventory qualification. The isolated restore stages above remain pending.

## Inert destination

Pre-create `objectstore-data` in the isolated namespace. Prove its fresh backing
volume identity before creating the Restore. Map only `strive-ailab` to this
namespace, select the objectstore labels and admit only the exact source pod
name. Keep `existingResourcePolicy: none`; never update existing resources.

Replace RustFS with a pinned inert container and remove its environment, Secret
references, projected tokens and owner references while preserving `restore-wait`
and `data`. Deny all ingress and egress; create no Service or route. The existing
Velero/node-agent supplies backup access through its host-mounted volume
mechanism; backup credentials must never enter the restored pod. Inspect result
metadata only, without logging stored IAM data, object payloads or credentials.

## Application and content qualification

A nonempty directory is insufficient proof. Use the exact RustFS digest against
the copy, listening only on loopback, with a same-pod verifier and newly generated
ephemeral credentials. First prove startup on a disposable synthetic restore:
the copied IAM configuration may depend on encryption or external keys, and
changing root credentials is not yet proven to open this backup.

Digest records contain `storage_key`, `size_bytes` and a SHA-256 `content_hash`
derived from the original bytes. Select records predating the backup as a
reviewed expected-content manifest; compare retrieved bytes and report aggregate
counts and matches only. Account for deletions and additions since backup time.
Current database state is not automatically a complete backup-time inventory.

The production HTTPS rollout stays held until this recovery evidence, certificate
bootstrap, live monitoring/notification receipt, tailnet/CA/IAM/CORS/network
qualification and a maintenance window are established in platform #2219.
