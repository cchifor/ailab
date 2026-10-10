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
  "schemaVersion": 41
}
```

Enable this component after `relay-control-monitoring`. It provides a separate
nonsecret policy ConfigMap, opts the existing materializer into the extension,
and mounts a dedicated 1 MiB RWX `nfs-csi` evidence PVC read-only into Relay. It
does not use a file `subPath`, so an atomic receipt replacement is visible on the
next scrape. The policy currently pins schema 41 and the exact reviewed verifier
source SHA256; a verifier/schema upgrade requires a matching GitOps change.
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
