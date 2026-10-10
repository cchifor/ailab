# Relay isolated recovery verification

The opt-in `kubernetes/components/relay-recovery-monitoring` component extends the
existing Relay control collector with separate restore-evidence alerts. It is
not included in the live cluster overlay. Its matching Relay release provides
`npm run recovery:verify -- /absolute/config.json` and four fixed metrics:
availability, completed verification time, backup generation time and restored
schema version. No backup digest, path, tenant or row content becomes a label.

The verifier consumes the coordinated generation produced by `backup.yaml`.
It copies and hashes the exact inventory, restores the archive into a fresh
networkless unprivileged PostgreSQL container, checks current and previous plugin
artifacts against the restored rows, and checks control-plane journal readability
and forced RLS. It executes no plugin code and contacts no production database,
router, OpenBao or guest. Successful cleanup precedes evidence publication.
The fixed PostgreSQL image matches this deployment's pinned 17.11 image.

Run it on an approved isolated Docker worker with workspace-backed private
scratch/output directories and a read-only copy of an approved backup generation.
The verifier command needs local Docker access; do not mount a Docker socket into
the Relay application or cluster monitoring pod. Limits are 2 GiB compressed
archive, 1 GiB restored database storage, 2 GiB container memory without swap and
128 artifacts. Larger restores need a separately reviewed capacity change.

The Relay repository's `verified-recovery.md` specifies the command and exact
receipt format. After a successful verification, the operator publishes the
completed private 0600 receipt through a reviewed file mount owned by the
Relay execution UID (1000 in this deployment). The application must mount it read-only. Add this nonsecret
configuration to the existing private collector verifier JSON:

```json
"recoveryEvidence": {
  "path": "/run/relay-recovery/private/evidence.json",
  "verifierSha256": "<SHA256 of the reviewed deployed verifier>",
  "schemaVersion": 43
}
```

Enable this component after `relay-control-monitoring`. It provides a separate
nonsecret policy ConfigMap, opts the existing materializer into the extension,
and mounts a dedicated 1 MiB RWX `nfs-csi` evidence PVC read-only into Relay. It
does not use a file `subPath`, so an atomic receipt replacement is visible on the
next scrape. The policy currently pins schema 43 and the exact reviewed verifier
source SHA256; a verifier/schema upgrade requires a matching GitOps change.
Enabling this schema-43 policy requires a successful isolated restore of schema
42; any existing schema-42 `evidence.json` becomes unavailable on the next scrape.
The unchanged verifier validates a strictly increasing migration inventory, actual table
readability and forced RLS, and hashes the restored artifact inventory. It does
not impose column-specific ownership or recovery-claim semantics; those are
qualified by Relay's native recovery tests, separately from restore evidence.
Relay [#105](https://git.chifor.me/cchifor/relay/pulls/105) exercised this exact
verifier against an actual schema-43 database/artifact restore. The matching
collector rejects evidence whose final migration does not match its policy.
The policy's generated name also changes the pod template on policy updates.

The operator must approve storage placement and a publisher's narrow mount of
this PVC. Precreate a canonical UID-1000-owned 0700 `private` directory, then
publish only the verifier's completed `private/evidence.json`, preserving UID
1000 ownership and 0600 mode, with atomic rename; do not copy raw backups,
SQL output, credentials or plugin sources to this volume. No publisher has been
enabled and no receipt is fabricated by this component. An empty volume is an
explicit unavailable observation and alerts after five minutes. The operator
handoff remains request 8. Check the actual receipt mode after a pod mount or
restart: storage-driver/fsGroup changes that add group permissions make the
receipt unavailable until the trusted publisher restores its private mode.

The rules require all four metrics after every successful scrape. Missing or
invalid evidence warns after five minutes. Evidence is stale after seven days
if either its verification time or its backup generation time is old. Separate
fixed reason labels preserve simultaneous faults; newly verifying an old archive
cannot clear its age fault. Scheduled backup success remains independently
monitored by the existing component.

The evidence proves an isolated database/artifact restore of the named bytes.
It does not prove live outage recovery, release/worker convergence, restored
revocation authority, approval/message replay safety, native home compatibility,
Incus reboot or device notification receipt. Those remain explicit acceptance
gates. Preserve the concise evidence artifact with the applicable release record.

Qualification exercises each missing series, unavailable and recovered evidence,
independent/simultaneous age faults, recovery with a new backup, and suppression
of age alerts while evidence is unavailable. All rules are included automatically
in the repository-wide Prometheus rule gate.


## Restore authority before reconnecting services

The separate opt-in `kubernetes/components/relay-restore-fencing` component mounts
an externally provisioned `relay-recovery-generation` ConfigMap key
`generation.json`, read-only, at `/run/relay-restore/generation.json`. It sets
`RELAY_RECOVERY_GENERATION_FILE` only for the Relay application. The referenced
ConfigMap is intentionally not created by this component: the operator owns its
fresh generation for each restore and must keep it outside the backup inventory.
It is nonsecret JSON with `version: 1` and a new `generation` UUID. There is no
placeholder generation that could accidentally authorize an old database.

A single-file `subPath` gives the verifier a canonical regular file, avoiding
kubelet's projected-volume symlinks. Generation changes require a deliberate pod
replacement after fencing; they do not update a running process. Stop Relay and
all provisioning/bootstrap workers first and prevent automatic restart. Perform
and verify the isolated restore, migrate it, then run the matching Relay
`npm run recovery:fence -- <expected-restored-database-name>` with the same
generation file and the deployment's schema-owner connection, without RLS bypass.
Provisioning workers need that same file/path configuration before restarting.
Do not change database role privileges or enable RLS bypass for this operation.

Relay refuses startup when the generation does not match a completed fence, and
refuses removal of the file once a fence exists. The fence retires old logins,
approvals, agent identities and queued work; known router keys enter independent
revocation and escrow cleanup. It does not reenable pools/profiles or release
capacity. Inventory router/OpenBao/Incus/native resources created after the backup
before admitting new work: those resources are absent from the restored rows.
The detailed behavioral contract and tested command are in Relay's
`restored-authority-fence.md`. No component is enabled in the live overlay.
