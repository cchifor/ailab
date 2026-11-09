# Router worker-budget release: rollout and recovery

This is the rollout record for upgrading the ailab router from source
`92c7c28a744e33963882a2dfea4b6fdc99d58a8c` to reviewer-merged source
`737d447bb5238fbe6ede0383e135641b7cda7cb6` ([llm-router #161](https://git.chifor.me/cchifor/llm-router/pulls/161)).
The published image and desired deployment change are recorded below. Live
acceptance is pending; no deployment or worker admission is claimed. Only the
operator performs cluster inspection or Kubernetes writes;
the dev-worker's platform credential has no access to this namespace.

## Publication and activation gates

The release archive is `router-0.1.0-20261109-workers-quota.tar.gz`, directory
`router-0.1.0-20261109-workers-quota`, SHA-256
`dfa2ee16e1fb20685f88127a48766bdb68aaf7e4d4d486d3660823c754957b0d`.
[Relay #280](https://git.chifor.me/cchifor/relay/pulls/280) reviewer-merged at
`c7b78ae969950384f67f7a8f8711216f02a39701` and pins that source archive for
publication. The earlier `c5943026` archive is superseded.

[Publication run 126503](https://git.chifor.me/cchifor/relay/actions/runs/126503)
passed at that exact packaging commit. Its
[receipt, asset 522](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-126503.json)
has SHA-256 `d84700ba2969a25184977540cbb85adba30a277cf9492993709637ae1c68dacc`.
The downloaded receipt's source, archive, directory, archive checksum, run and
packaging commit match the reviewed inputs. Retain its bytes and hash independently;
release assets can be deleted.

The router pin is
`registry.chifor.me/llm-router/router@sha256:61100143a90c66219d1e2acb1d7b6cd484dfea3c9622a779c92c8229599b1bb7`.
Independent registry reads verified the index, linux/amd64 manifest
`sha256:f0a64b4219da363e83ebc5d7d3a02c256aa635d7c4c127905b223b4c73112b79`
and config `sha256:604c2cdbc70dfaa027993b848104e280fca16a77c22d081867e2df1f7d2c616d`
against their bytes, including the OCI source label, uid/gid `1000:1000` and
working directory `/app`. The runtime base remains
`docker.io/library/node@sha256:a723b54c35a76e947095a20a67d39585bb09c862e6b1adeb8a9f518f95e34fb0`.
The publisher passed its production smoke and backup-helper checks. A subsequent
exact-digest pull and disposable container with networking disabled, a read-only
root filesystem and uid/gid 1000 verified `/app/SOURCE_COMMIT` equals
`737d447bb5238fbe6ede0383e135641b7cda7cb6`. Its packaged backup-helper SHA-256 is
`63be6a39b3ce7e58a0206aff443ee8bd3a042fb254989730974d127588bfbd58`, identical to
`deploy/ci/backup-router.mjs` at packaging commit `c7b78ae969950384f67f7a8f8711216f02a39701`.
That container and the locally pulled image were removed after verification.
The [durable publication and pulled-image verification record](https://git.chifor.me/cchifor/relay/pulls/280#issuecomment-109782)
ties these checks to the receipt and exact image pin.
The publisher also built Relay; this change pins only the router, leaving the
Relay deployment unchanged. No source test or archive checksum is used as an
image digest.

Deploy this router correction before the Relay readiness composition. It reserves
twelve unexpired diagnostic assertions separately from the 120 worker-management
slots per issuer/tenant, while retaining one durable replay journal. Relay cannot
detect an older router's shared-quota behavior automatically.

Keep `worker-budget-authority` disabled, with no new trust ceiling, DSN, witness,
token policy, route, provider or credential in this image-only rollout. The plugin
is default-disabled and requires explicit configuration plus independent
authority. Lifetime accounting of existing Relay-managed keys is part of the
normal API-key service and can write state without that plugin being enabled.
Production worker activation remains a separate reviewed ceremony in
[the independent authority runbook](relay-worker-budget-authority.md), including
durability, recovery fencing and exact provider/runtime qualifications.

## Persistent-state compatibility

The source diff adds bounded SQLite KV prefix reads; it adds no SQL table or
schema-version migration. It does add durable values to the existing `kv` table:

| Store | New behavior and recovery significance |
| --- | --- |
| `relay-key-meter` | Durable deployment identity, lifetime aggregates and open attempt records. Retention of trajectories cannot erase these counters. A restored identity is not proof that its counters are current. |
| `relay-worker-budgets` | Allocation groups, token/request charges, reservations, active indexes and operation receipts. Unknown attempts retain their conservative charge; old allocations cannot be reissued or refunded from a restore. |
| `api-keys` / `state` | Managed keys may bind `workerBudget` references. The previous image loads these keys but does not enforce the new budget. |
| `relay-key-nonces` | Readiness entries gain a `readiness:true` marker; management entries remain untagged. All consumed assertions retain their replay identity until expiry. |
| `relay-key-operations`, encrypted secrets and audit | Issuance/revocation/acknowledgement remain coordinated with key state. A restore can resurrect key authority or delivery state. |

These values, the SQLite WAL, `/data/router.secrets.key` and `/data/plugins.yml`
must remain consistent. External auth Secrets, OIDC PostgreSQL and any future
trust/authority inputs are not covered by the local SQLite helper. In particular,
the independently retained worker ledger and its epoch/witness must never be
copied into this application's restore set.

The previous image's `KeyService.restore()` casts stored key state and registers
live keys. It does not reject a key merely because `managed.workerBudget` exists.
Disabling the `relay-keys` management plugin or removing its trust-file variable
does not disable inference with already issued bearer keys. An unchanged SQL
schema therefore does not make an active-worker downgrade safe.

## Backup before startup

Use the existing single-replica `Recreate` strategy and retained RWO data PVC.
After the old process exits, a temporary init container runs the publisher's
packaged `/app/deploy/backup-router.mjs` before the new router starts. The init
must use the same verified image digest as the application, inherit the pod's
non-root uid/gid 1000 and RuntimeDefault seccomp, and receive no credentials or
Kubernetes token. The checked-in pod spec uses the verified image digest in both
containers and the following init configuration. The 256 MiB/one
CPU limit below is a bounded proposal, not a measured live backup resource need;
an OOMKill blocks startup and requires the recovery decision below:

```yaml
      initContainers:
        - name: backup-before-worker-budgets
          image: registry.chifor.me/llm-router/router@sha256:61100143a90c66219d1e2acb1d7b6cd484dfea3c9622a779c92c8229599b1bb7
          imagePullPolicy: IfNotPresent
          workingDir: /app
          command: [node, /app/deploy/backup-router.mjs]
          env:
            - name: ROUTER_DATA_DIR
              value: /data
            - name: ROUTER_BACKUP_LABEL
              value: pre-worker-budgets-20261109
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: "1", memory: 256Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - name: data
              mountPath: /data
            - name: tmp
              mountPath: /tmp
```

Update the application image, source annotation, release annotation
(`router-0.1.0-20261109-workers-quota`) and provenance comment together. Render and
validate the manifest before merge; the acceptance parser rejects a placeholder,
unpinned image or mismatched init/application pins. Leave the current environment,
resource limits, storage,
NetworkPolicies, authentication and model configuration intact. The init is the
only additional pod behavior. It does not launch router/provider plugins or
access the network.

The helper uses `VACUUM INTO`, copies the encryption key and optional plugin file,
checks SQLite integrity and per-file SHA-256, then atomically publishes
`/data/backups/pre-worker-budgets-20261109/COMPLETE`. Repeated startup verifies an
existing completed generation; it never overwrites it. A failed backup must
block startup. Do not delete a failed/corrupt generation or hand-edit around the
init to start the new image. Investigate with the operator and retain the original
evidence. A reviewed revert to the prior composition is available if the new
application has never started, as specified below. The helper requires at least
twice the main database size plus 64 MiB
free before capture; the operator must also preserve the application's separate
free-space requirements after capture. Before merging, check actual PVC usage
and confirm `/data/router.sqlite` and the required `/data/router.secrets.key` are
present and readable by uid 1000, without copying or logging the key's contents.
This is local recovery material, not evidence of an offsite restore drill.

## Acceptance and cleanup

The main-only **Router rollout acceptance** workflow already discovers the exact
image and init names from `router.yaml`. Require its passing result: one fresh
Ready router on the pinned digest, exactly the expected init composition with
successful completion, public health/readiness, and unauthenticated management
refusal. The reusable local observer is `python3 scripts/check-router-rollout.py`;
it is read-only. Record the reviewed ailab commit, workflow run, pod identity,
observed image, backup COMPLETE file hash and first-capture `createdAt`, and the
init's bounded verification log. Preserve that initial timestamp to detect any
later regeneration under the same pre-upgrade label. On first capture the log names
the backup label; a retry reports `Previously completed router backup verified`.
For a retry, pair that message with the exact init environment and the operator's
verified COMPLETE manifest for this label.
Do not substitute generic `/ready` success for backup evidence.

Have the operator confirm ordinary routes, account/plugin state and existing
client traffic against the pre-roll baseline. Verify the worker authority plugin
remains disabled; health alone cannot prove that. Do not create workers or make
paid inference calls as an implicit acceptance step. This rollout does not prove
live budget enforcement, twelve-probe isolation or KubeVirt qualification.

After public acceptance, remove only the temporary init through a reviewed
cleanup PR, preserve the verified backup and exact application pin, and require
acceptance again. The checker verifies the init composition is absent, preventing
the old Ready pod on the same image from satisfying cleanup prematurely. Retain
the backup until a newer consistent generation and retention decision exist.

## Rollback and restore decision

Prefer a forward fix. Before any downgrade, obtain current operator evidence of
managed-key use, budget-bearing keys, independent allocations and client reliance
on lifetime accounting. Missing evidence keeps managed traffic fenced. A count
from the pre-upgrade backup cannot describe authority issued afterward.

If no new worker authority or budget-bound key has ever been issued and no client
relies on the new lifetime accounting, a reviewed image-only rollback may retain
the current database **with all managed-key issuance and inference fenced for
the entire downgraded interval**. Preserve the new KV records. The old image does
not implement that fence. If an external fence cannot be independently verified,
durably revoke all managed keys through the supporting current service, verify
the receipts before downgrade, and prevent replacement issuance. The old image does
not meter managed-key traffic. If any such traffic nevertheless occurred, those
keys' preserved aggregates are unverifiable on re-upgrade: they silently omit
that interval. Record the gap independently, durably revoke the affected keys,
and issue replacements only on a supporting release through a reviewed recovery
procedure. Never use those prior aggregates as complete lifetime proof.
Disable readiness callers before downgrading; the previous shared 120-slot nonce
limit can temporarily refuse management until already consumed assertions expire.
Do not delete nonce state to restore availability. Change the application image
and both annotations together to the prior source/release/image:

- Source: `92c7c28a744e33963882a2dfea4b6fdc99d58a8c`.
- Release: `router-0.1.0-20261107-availability`.
- Image: `registry.chifor.me/llm-router/router@sha256:55ee00f0521046baffa0b0e8809e2b42526710abd2f05b4b0e1a23777cd7dc5c`.

If backup init failed and the new application has **never started**, restore
availability through a reviewed revert to the exact prior image, annotations
and composition, removing the new init as part of that revert. First have the
operator prove from pod/container status and rollout history (including replaced
pods) that no new-release application container ran and no new application state
was written. The helper only captured a read-only source database; this path
restores the previous composition rather than starting the new image without its
backup. Preserve failed backup evidence. If history is missing or any new
application container started, this exception does not apply.

After the new application has run, preserve a completed backup and remove its
dated init in the same reviewed rollback commit as the application pin, only after capture and the authority gates
above are verified. A downgrade still requires acceptance and preserved ordinary
route behavior. This source audit is not a live downgrade rehearsal.

Once managed worker authority exists, **do not restore the old snapshot or serve
managed traffic with the old image**. The old binary ignores budget bindings;
restored state can lower counters and resurrect revoked keys. Removing a plugin,
waiting for an authority refresh or restarting with the same witness is not a
fence. Before exceptional recovery:

1. Stop worker consumers and new issuance/renewals; prevent automatic restart and
   fence managed ingress independently of either application database. Drain
   accepted work and record unresolved attempts conservatively.
2. Freeze the independent ledger and rotate its epoch and external witness using
   its reviewed recovery procedure. Retain every previous prepaid allocation as
   spent; do not transfer or refund its remainder. Ledger loss or uncertain
   history cannot be repaired by reseeding an empty database.
3. Reconcile and durably revoke outstanding managed credentials through the
   supported current service while fenced; preserve external revocation and
   allocation receipts. Capture the latest consistent application state before
   any restore. Keep independent authority outside the restored generation.
4. Restore only under the [authority recovery procedure](relay-worker-budget-authority.md)
   and Relay's application recovery fence. Reapply externally recorded
   revocations before allowing traffic. Requalify the exact runtime/policies and
   new witness on a supporting image; old allocations never migrate or reissue.
5. Keep worker issuance and managed inference disabled until reviewed independent
   evidence establishes the new state. If ordinary traffic must run on an older
   image, all managed credentials must remain demonstrably unusable and no
   management path may mint replacements. An empty catalog or `/ready` response
   does not establish those conditions.

For a database restore without worker budgets, the existing managed-key recovery
rules still apply: reapply revocations before ingress, treat decreases in lifetime
counters as unverifiable and never reconstruct accounting from retained request
history. Every restore loses writes after its snapshot; recovery authorization
must address those writes explicitly.

## Preparation validation

This preparation compares the two exact source commits above, including the
previous key service, new meter/budget stores, nonce handling, default-disabled
authority and SQLite adapter diff. Opus review
`0d9ecd0c-4a67-4a8d-8472-982861579e4a` identified the acceptance parser's nested-name
bug and missing downgrade/revert details; those were corrected. Closure review
`f809613d-9047-46e3-a55d-f05d946dc20d` aligned with no remaining blockers.

All 14 acceptance-checker tests passed again with the real image pin, including
nested mount names, init placement before/after application containers, and
refusal of an unpinned init image. Digest-pinned manifest validation built all
31 paths: 526 resources valid, zero invalid/errors and 68 skipped under the
existing schema policy. A parsed-YAML comparison against ailab main
`86481778c3cd5b3b3064d1ad027c0252f81ea5cf` confirmed only the application image,
source/release annotations and one reviewed backup init changed. All other
resources and existing application configuration match that base. The documented
init equals the actual manifest; the checker expects exactly this image and
`backup-before-worker-budgets` with no canary Job.

Final Opus review `9082a64a-b0df-48ab-bcf9-b61691a91728` aligned on the concrete
manifest, source/digest/receipt/image-file evidence and complete rollout delta.
The earlier dry-compose used an explicitly synthetic test digest; none was
written to the deployment manifest. No live upgrade, downgrade, storage
measurement, worker-budget activation or runtime qualification is claimed.
