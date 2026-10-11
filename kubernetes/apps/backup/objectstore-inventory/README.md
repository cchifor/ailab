# Temporary objectstore backup inventory

This component is prepared but **not included in the backup Kustomization**.
It supports the isolated-restore prerequisite for
[platform #2219](https://git.chifor.me/cchifor/platform/pulls/2219).
It does not restore data, restart storage, or release Document Insights.

Before activation, observe the actual Velero controller and node-agent images
using the temporary grants in [ailab #1252](https://git.chifor.me/cchifor/ailab/pulls/1252).
The implementation below depends on Velero 1.18.1 semantics. Confirm the pinned
backup remains Completed with UID `84590917-87e3-4dc9-b642-a1638e792214`, 6,198
items, zero errors, both snapshot flags false and zero asynchronous operations.
Then include this directory from the parent Kustomization in a reviewed change.
Do not run `kubectl apply` from a worker.

Flux creates the fixed-name `BackupResourceList` DownloadRequest. Its signed URL
is a bearer capability: only the single-purpose Job receives a named GET. The
worker receives no new access. The Job can GET that one Backup, GET that one
DownloadRequest, and PATCH one pre-created result ConfigMap in `platform-edge`.
It cannot list resources, create requests, read Secrets, restore data, or obtain
objectstore credentials. Egress allows only the API-server entity and
`192.168.1.225:7070`; redirects and proxy environment settings are refused.

TLS uses the existing public trust from
[the Talos backup CA](../talos-backup/versitygw-ca.yaml). The test compares its
DER fingerprint and exact bytes. It does not inherit the older BackupStorageLocation
TLS bypass. No signed URL, resource name, payload or transport exception is logged.
Only counts by API-version/Kind, the pinned backup identity, acquisition time and
artifact SHA-256 reach the receipt. Invalid, expired, truncated, duplicate-key,
count-mismatched or unavailable artifacts produce `status: unavailable`, without
a zero DataUpload claim.

Velero's [resource list](https://github.com/vmware-tanzu/velero/blob/v1.18.1/pkg/backup/backed_up_items_map.go)
contains keys such as `v1/Pod` and `velero.io/v2alpha1/DataUpload`. Sum all array
lengths without deduplicating across resource types. A zero DataUpload count is
insufficient by itself: the
[async finalizer](https://github.com/vmware-tanzu/velero/blob/v1.18.1/pkg/controller/backup_finalizer_controller.go)
can append archive objects without replacing the resource list. The Job therefore
also requires false `snapshotVolumes`/`snapshotMoveData` and zero attempted,
completed and failed async-operation counters. Omitted counters mean zero under
Velero's `int,omitempty` schema; malformed or nonzero values are refused.
The initial inventory remains acquisition evidence, not a restore-success receipt.

Observe only `platform-edge/ConfigMap/objectstore-backup-inventory-20261011` via
`platform kubectl`. Require `receipt.json` to be available, recent relative to
this activation, tied to the exact backup UID, count-matched, and to report zero
DataUploads before advancing the separately reviewed restore plan. A missing
receipt is unavailable. A previous receipt is not evidence for a rerun.

After preserving the sanitized receipt, promptly remove the parent reference and
this component through a cleanup PR, including its request, Job, RBAC, script,
trust, network policy and receipt ConfigMap. Complete by **2026-10-18**. The script
refuses acquisition from that date. This is not automatic RBAC expiry. The Job
has no TTL; Flux could recreate a deleted Job. Velero deletes expired requests,
so Flux would also recreate a request still in Git. Removing a request does not
revoke an issued URL before its natural ten-minute expiry. The Job's force
annotation satisfies the repository's immutable-field gate; changing a referenced
ConfigMap can rerun acquisition, so review such changes as another activation.

Validation: `python3 -m unittest scripts.tests.test_objectstore_backup_inventory`
and a strict Kustomize/kubeconform render. Fixtures exercise aggregate-only output,
DataUpload versions, wrong backup identity, async operations, stale requests,
malformed/truncated/compressed input, duplicate JSON keys and names, changed
counts, redirects, TLS failures, the request-creation race, and exact RBAC/network
scope. No backup payload is fetched by these tests.
