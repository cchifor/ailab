# Router worker-budget release: rollout and recovery

This is the rollout record for upgrading the ailab router from source
`92c7c28a744e33963882a2dfea4b6fdc99d58a8c` to reviewer-merged source
`737d447bb5238fbe6ede0383e135641b7cda7cb6` ([llm-router #161](https://git.chifor.me/cchifor/llm-router/pulls/161)).
The published image, initial rollout and final composition are recorded below.
Source preparation is not live acceptance evidence: retain the operator's backup
verification and acceptance results in the rollout and cleanup PR records.
The final checked-in composition omits the temporary backup init; initial live
acceptance and the cleanup publication gates below precede its deployment.
Cleanup live acceptance then verifies the replacement pod; the operator
reconfirms the retained backup.
Only the operator performs cluster inspection or Kubernetes writes;
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

## Accepted initial rollout, 2026-11-10

[Operator evidence](https://git.chifor.me/cchifor/ailab/pulls/1697#issuecomment-110646)
records reviewer merge `354570499214d22efd82d6f6e2cee660b9e4481c` and passing
[acceptance run 126901](https://git.chifor.me/cchifor/ailab/actions/runs/126901).
The new pod `llm-router-7b59474cd8-k84xb` was Ready by 09:20:37Z with zero restarts;
both init and application ran the pinned `61100143…` image and source 737d447b.
The Recreate replacement gap was about two minutes.

The retained generation is `/data/backups/pre-worker-budgets-20261109`.
Its accepted COMPLETE SHA-256 is
`283d5f6bdcc98646615668ed9353fbdfce481af903542d2ce8a6d1483ce237df`, with first
capture `createdAt: 2026-11-10T09:19:31.786Z`. The SQLite copy is 312,598,528 bytes.
Free space after capture was 1,535,778,816 bytes. The operator verified matching
ordinary route sets and no failed/deferred requests across the before/after
windows; worker-budget-authority remained the sole disabled plugin. No worker
was created for acceptance.

Cleanup owner is dev-worker-3; workstation's operator monitors Flux and verifies
the retained backup. Target the current 2026-11-10 maintenance window after review.
Record a delay/window change if needed. Cleanup requires its own acceptance and
confirmation that this exact COMPLETE hash and first-capture time remain intact.

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

## Initial rollout: backup before startup

Use the existing single-replica `Recreate` strategy and retained RWO data PVC.
After the old process exits, a temporary init container runs the publisher's
packaged `/app/deploy/backup-router.mjs` before the new router starts. The init
must use the same verified image digest as the application, inherit the pod's
non-root uid/gid 1000 and RuntimeDefault seccomp, and receive no credentials or
Kubernetes token. The initial rollout in
[ailab #1697](https://git.chifor.me/cchifor/ailab/pulls/1697) uses the verified
image digest in both containers and the following temporary init configuration.
Retain this specification as the initial-rollout and recovery reference; the final
composition removes it only after initial acceptance and the cleanup publication
gates below. The 256 MiB/one CPU limit below is a bounded proposal, not a
measured live backup resource need;
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

For the initial rollout, update the application image, source annotation,
release annotation (`router-0.1.0-20261109-workers-quota`) and provenance comment
together. Render and validate the manifest before merge; the acceptance parser
rejects a placeholder,
unpinned image or mismatched init/application pins. Leave the current environment,
resource limits, storage,
NetworkPolicies, authentication and model configuration intact. In that initial
rollout, the init is the only additional pod behavior. It does not launch
router/provider plugins or access the network.

The init reuses the existing `tmp` volume, an `emptyDir` under
`spec.template.spec.volumes` ([router.yaml at the reviewed deployment head,
lines 336–337](https://git.chifor.me/cchifor/ailab/src/commit/29a5adaf33bb2c6c424d10e9ae49ce8163174e82/kubernetes/apps/apps/llm-router/router.yaml#L336)).
Both the application and init mount it at `/tmp`; no additional volume is needed.

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
free-space requirements after capture. Before merging the initial rollout, check
actual PVC usage and confirm `/data/router.sqlite` and the required
`/data/router.secrets.key` are present and readable by uid 1000, without copying
or logging the key's contents.
This is local recovery material, not evidence of an offsite restore drill.

## Acceptance and cleanup

The main-only **Router rollout acceptance** workflow discovers the exact
image and init names from `router.yaml` at its checked-out commit. For the initial
rollout, require its passing result against the merged initial-rollout composition:
one fresh Ready router on the pinned digest, exactly the expected init
composition with
successful completion, public health/readiness, and unauthenticated management
refusal. The reusable local observer is `python3 scripts/check-router-rollout.py`;
it is read-only. Record the reviewed ailab commit, workflow run, pod identity,
observed image, backup COMPLETE file hash and first-capture `createdAt`, and the
init's bounded verification log. Preserve that initial timestamp to detect any
later regeneration under the same pre-upgrade label. On first capture the log names
the backup label; a retry reports `Previously completed router backup verified`.
For a retry, pair that message with the exact init environment and the operator's
verified COMPLETE manifest for this label.
Before cleanup, and after any unexplained init restart, compare COMPLETE's hash
and `createdAt` with the originally accepted values. The pinned helper can create
a new generation if the old directory is missing or empty; successful init exit
alone cannot prove that the original pre-upgrade generation survived.
Do not substitute generic `/ready` success for backup evidence.

Have the operator confirm ordinary routes, account/plugin state and existing
client traffic against the pre-roll baseline. Verify the worker authority plugin
remains disabled; health alone cannot prove that. Do not create workers or make
paid inference calls as an implicit acceptance step. This rollout does not prove
live budget enforcement, twelve-probe isolation or KubeVirt qualification.

Prepare the cleanup PR during the rollout. Submit it for review only after
ailab #1697 is reviewer-merged, the operator has supplied the original COMPLETE
hash and first-capture `createdAt` with verified backup evidence, and the initial
live acceptance above has passed. Link that evidence in the cleanup PR; source
checks alone do not satisfy these publication gates. Assign its owner and target
the same maintenance window; if review or acceptance delays it, record the next
window and explicit owner instead of leaving the temporary init indefinitely.
Remove only the temporary init through that reviewed PR, preserve the verified
backup and exact application
pin, and require acceptance again at the merged cleanup commit. Its final
composition expects `init=[]` and no canary Job: the checker rejects the old Ready
pod retaining the init even when it has the same image. The removal does not
delete or rewrite `/data/backups/pre-worker-budgets-20261109` or its data PVC.
Have the operator confirm the original COMPLETE hash and `createdAt` still match
after cleanup. Retain the backup until a newer consistent generation and retention
decision exist. Worker authority remains disabled throughout both compositions.

### Backup verification failure after application startup, before cleanup

While the initial rollout's temporary init remains, a later pod restart can fail
when the retained generation's COMPLETE marker,
checksums or SQLite integrity no longer verify. The never-started revert below
does not apply. Keep the current verified application image and current data;
do not downgrade, restore the pre-upgrade database, delete the failed generation,
edit COMPLETE, or remove the init merely because it failed. After cleanup, the
final composition no longer verifies that backup on pod startup; discovering
backup corruption still requires investigation and a reviewed recovery decision.

The operator and recovery reviewer use this incident-specific procedure:

1. Record the failed init log, pod/image and prior-start history, failed generation
   label, original accepted COMPLETE hash/`createdAt`, current observed hashes,
   and PVC health, usage and uid-1000 permissions. Preserve failed-generation
   bytes in protected recovery storage; never attach encryption keys or secret
   files to the PR. Distinguish a damaged backup copy from damaged or rolled-back
   live data. A green SQLite integrity check alone cannot establish authority
   continuity. Uncertain live data, key material, ledger/witness history or
   revocations require the independent recovery ceremony below, not this path.
2. Prepare a reviewed recovery change that keeps only this Deployment quiesced
   at zero replicas during repair/capture and coordinates its GitOps desired
   state. Do not suspend the shared apps Kustomization. Preserve the current PVC,
   application pin and configuration. Repair the diagnosed storage/permission
   cause first, and recheck writable capacity; no application may open SQLite
   while the maintenance backup work owns that volume. Confirm zero router pods
   before creating any maintenance Job. Record this intentional unavailable
   interval: the normal one-Ready-pod acceptance cannot pass while replicas are
   zero, and only passing acceptance for the restore commit closes the incident.
3. If an independently retained, known-good replica of the **same original
   generation** exists, verify it against the originally accepted COMPLETE hash,
   creation time, file checksums and SQLite integrity. After preserving the failed
   copy, the reviewed procedure may repair only that backup directory from the
   verified replica. It must not copy the backup database over the live database.
   Before any helper run and before scaling the Deployment back up, require the
   complete directory and
   every listed file, owned by uid 1000, with directory mode 0700 and file mode
   0600. Run the same packaged helper against that repaired label. Success means
   **only** `Previously completed router backup verified`, with the post-run
   COMPLETE hash and `createdAt` identical to the originally accepted values.
   A first-capture message or changed hash/time is a failed repair: preserve it
   as evidence and never call it pre-upgrade material. This explicitly detects
   the helper's ENOENT fallback, which otherwise can recapture current data under
   an absent or empty original label. An exact verified repair permits the init
   to verify on restart without erasing newer writes or changing the recovery epoch.
4. If no exact original replica is available but current application data and
   authority continuity are independently trusted, take a **fresh post-upgrade
   backup** under a new incident label, for example
   `post-worker-budgets-20261110-incident1`. The reviewed maintenance change must
   include a one-shot Job using the same digest-pinned image/helper, non-root
   security, `/data` PVC and writable `/tmp` as this init, with
   `ROUTER_DATA_DIR=/data` and the new `ROUTER_BACKUP_LABEL`, `restartPolicy: Never`
   and `backoffLimit: 0`. A standalone Job must set its own pod security:
   `runAsUser: 1000`, `runAsGroup: 1000`, `fsGroup: 1000`, `runAsNonRoot: true`,
   RuntimeDefault seccomp and `automountServiceAccountToken: false`. Its container
   keeps the init's read-only root, no privilege escalation and dropped ALL
   capabilities. Put its manifest outside the router's `*-canary.yaml`
   pattern; it is maintenance work, not a rollout canary. The incident label must
   not exist before the first attempt. Run no router or provider plugins. Require
   the first-capture message naming that label, and verify its completed
   generation's hash/creation time, per-file checksums and SQLite integrity.
   A reviewed rerun that reports `Previously completed` must match the first
   attempt's accepted COMPLETE hash and `createdAt`. An OOMKill or interrupted
   attempt can leave `.label-*` temporary directories: record their identities
   and space usage as failed evidence; do not delete them ad hoc. Record the
   original generation's loss explicitly. The fresh backup is not pre-upgrade
   evidence and never resets the live database or independent authority.
5. Attach the diagnosed cause, current-state continuity evidence and the successful
   repair or fresh-capture evidence to the recovery PR. Only after those checks
   may the reviewed recovery change restore one replica on the **same current
   application image/data**. For a repaired original generation, require again
   the `Previously completed` message and exact original COMPLETE hash/`createdAt`
   when the init runs; the prompt cleanup above follows. With a verified fresh
   post-upgrade generation, the recovery PR explicitly removes the obsolete
   dated init and records why that particular generation satisfies the current
   backup requirement. This is a reviewed recovery decision, not automatic
   removal on failure. Remove the finished maintenance Job after preserving its
   evidence, and require normal image/health/management-refusal acceptance again.

If neither branch can prove a trustworthy complete recovery generation and
current authority continuity, keep the workload and managed traffic fenced and
escalate through the independent recovery procedure. Availability does not justify
resuming from unverifiable state.

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

During the initial rollout, if backup init failed and the new application has
**never started**, restore availability through a reviewed revert to the exact
prior image, annotations
and composition, removing the new init as part of that revert. First have the
operator prove from pod/container status and rollout history (including replaced
pods) that no new-release application container ran and no new application state
was written. The helper only captured a read-only source database; this path
restores the previous composition rather than starting the new image without its
backup. Preserve failed backup evidence. If history is missing or any new
application container started, this exception does not apply.

After the new application has run, preserve a completed backup. If its dated init
is still present, remove it in the same reviewed rollback commit as the application
pin, only after capture and the authority gates above are verified. If cleanup has
already removed the init, keep it absent; the same backup and authority gates still
apply. A downgrade still requires acceptance and preserved ordinary
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

For the initial rollout, all 17 acceptance-checker tests passed with the real
image pin, including nested mount names, init placement before/after application
containers, and
commented/quoted image values and keys, unpinned/mismatched init refusal and
canary masking regressions. The previous digest-pinned manifest validation built all
31 paths: 526 resources valid, zero invalid/errors and 68 skipped under the
existing schema policy. A parsed-YAML comparison against ailab main
`86481778c3cd5b3b3064d1ad027c0252f81ea5cf` confirmed only the application image,
source/release annotations and one reviewed backup init changed. All other
resources and existing application configuration match that base. The documented
init equals that initial-rollout manifest; its checker expectation is exactly this
image and `backup-before-worker-budgets` with no canary Job.

Final Opus review `9082a64a-b0df-48ab-bcf9-b61691a91728` aligned on the concrete
manifest, source/digest/receipt/image-file evidence and complete rollout delta.
The subsequent reviewer follow-up changes only the checker, its tests and this
runbook; the deployment manifest is byte-unchanged, so the full manifest suite
was not repeated. Delta review `156bafc0-aed4-4c03-b43b-1220f109ed14` confirmed the
comment parsing and tmp proof, and required the stricter original-generation and
fresh-capture success criteria now specified above.
Closure review `4d50203f-9b49-4e3d-a50b-d92706022f76` aligned with no remaining
blockers. A future rollout canary must use this same pinned router image for every
container; a different helper image requires an explicitly reviewed checker change.
The earlier dry-compose used an explicitly synthetic test digest; none was
written to the deployment manifest. No live upgrade, downgrade, storage
measurement, worker-budget activation or runtime qualification is claimed.

For cleanup, all 17 acceptance-checker tests also pass after joining the actual
reviewer-merged main. A parsed-YAML comparison against the accepted live rollout
commit `354570499214d22efd82d6f6e2cee660b9e4481c` confirms the only manifest change is
removing `backup-before-worker-budgets`; every remaining field and resource,
including the exact application pin, annotations, configuration and data PVC,
is identical. The checker expects that pin with `init=[]` and no canary Job.
A synthetic Ready pod retaining the old init is rejected; the equivalent Ready
pod without init is accepted. These local checks do not replace the operator's
backup continuity evidence or either composition's live acceptance. The router
Kustomization renders with the repository's pinned validation tools; scoped
kubeconform reports 12 valid resources, zero invalid/errors and one Secret skipped
under the existing policy.
