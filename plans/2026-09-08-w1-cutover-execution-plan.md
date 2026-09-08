# Execution plan — W1 Actions cutover + W5/W1 acceptance

## Codex Review

- **Do not execute unchanged.** The added execution report says cutover already occurred; steps 1–7 must now be treated as historical. Approval-triggered auto-merge defeated the intended suspension barrier.
- HelmRelease suspension is conditional: the checked-in release does not enable drift correction. If retained, resume Helm only after the intended backend configuration is staged.
- Pin the verified post-#568 migration manifest; `gitea/main~1` is unreliable. Require source-revision, writer-quiescence and database consistency gates, plus recovery that works with Gitea down.
- The watchdog pause command is malformed and lacks recovery that survives session loss. A1 does not reproduce TCP-accepted-but-silent behavior; the reported catch-up counts do not alone prove recovery.
- Steps 8–9 remain disruptive. For the original full procedure, provisionally allow **90–120 minutes**; with cutover already reported complete, allow **45–60 minutes** for remaining acceptance/cleanup and recovery, subject to timed checks. The 11-second copy is not the maintenance-window duration.

## Context

W2, W3, W4, W5 and W6 of `plans/2026-09-08-usb-failure-domain-plan.md` are shipped and live.
What remains is the one step that needs an outage — moving Gitea's Actions storage off the USB
disk — plus the two acceptance tests the plan requires but which deliberately break things.

State at time of writing:

| | |
|---|---|
| Flux source | `https://github.com/cchifor/ailab.git`, unauthenticated, Ready (W5 merged) |
| Backup layer | own `backup` Kustomization; nothing dependsOn it (W6 merged) |
| Actions PVC | `gitea-actions-storage` 40Gi `qnap-iscsi`, Bound |
| Pre-copy | verified — 8,982 artifacts + 48,783 logs, 0 differences |
| Gitea | still serving Actions from versitygw S3 — cutover NOT applied |
| Open PRs | #568 (`--size-only`), #567 (the cutover; needs rebase after #568) |

No `flux` CLI on this workstation: suspend/resume is `kubectl patch ... {"spec":{"suspend":true}}`,
reconcile is the `reconcile.fluxcd.io/requestedAt` annotation.

## Constraints that shape the order

1. **Gitea hosts the git being merged.** Any PR merge must happen while Gitea is UP. So the cutover
   must be merged *before* Gitea stops, and prevented from applying until we are ready — hence
   suspend-then-merge, not merge-then-suspend.
2. **The final copy must run with every writer stopped.** Draining runners is not enough: Gitea's
   own offload and cleanup paths write to Actions storage. Anything written after the final copy but
   before the backend switches exists only in S3 and becomes invisible.
3. **The migration Job holds the same RWO volume Gitea will mount.** RWO restricts a volume to one
   NODE, not one pod, so co-scheduling is possible and must be prevented explicitly.
4. **`apps` is suspended during the window**, so Flux will not recreate the Job — the final copy has
   to be applied by hand from the git manifest.
5. **Killing versitygw is not enough to keep it dead.** The W3 watchdog restarts it within 3 minutes
   whenever the disk is healthy. Its cron must be paused first, and restored after.

## Step 0 — pre-flight

```bash
export KUBECONFIG=kubernetes/infra/_out/kubeconfig
K="kubectl --context admin@ai"
$K get kustomization -A --no-headers | awk '$4!="True"'      # expect: empty
$K -n gitea get pvc gitea-actions-storage                     # expect: Bound
$K -n gitea get job gitea-actions-migrate                     # note its state
$K -n databases get cluster infra-pg                          # expect: healthy
```

Abort if anything is not Ready: a cutover on top of an already-degraded estate makes any failure
impossible to attribute.

## Step 1 — land #568, rebase #567

`#568` changes `actions-migrate-job.yaml`; `#567` deletes it. Git reports that as a
modify/delete conflict, resolved by keeping the deletion.

```bash
# merge #568 first
git fetch gitea && git checkout feat/gitea-actions-cutover
git rebase gitea/main            # resolve: git rm kubernetes/apps/apps/gitea/actions-migrate-job.yaml
git push --force-with-lease gitea feat/gitea-actions-cutover
```

Wait for #567 CI green. Do NOT merge yet.

**Checkpoint:** `apps` reconciles #568 and replaces the Job with the `--size-only` version. Confirm
the live Job's args contain `--size-only` before continuing — this is what keeps step 6 short.

<!-- codex: HIGH — Mere presence of the size-only flag is insufficient: the pre-#568 manifest already uses it for rclone check, while rclone copy still uses checksum comparison. Inspect the copy command itself and require the replacement Job to complete. Time a repeat against the populated PVC; #568 records a projected two-hour run with the old copy command. -->

## Step 2 — suspend, so merging does not apply early

```bash
$K -n flux-system patch kustomization apps --type=merge -p '{"spec":{"suspend":true}}'
$K -n gitea patch helmrelease gitea --type=merge -p '{"spec":{"suspend":true}}'
```

Both: the Kustomization stops re-applying the HelmRelease, and suspending the HelmRelease stops
helm-controller reverting a manual `scale` as drift. Suspending only one leaves a path to a surprise
roll mid-window.

<!-- codex: HIGH — This necessity claim is too broad. kubernetes/apps/apps/gitea/gitea.yaml has no spec.driftDetection; an unchanged healthy release does not automatically correct replica drift unless correction is enabled. Check the live release and pending Helm actions. Suspending apps is the merge barrier; also suspending Helm is a precaution against upgrades/remediation, not an unconditional requirement for scaling. The checked-in HelmRelease CRD states that suspension does not cancel already-started reconciliations, so establish controller quiescence before merging. See [Flux drift detection](https://fluxcd.io/flux/components/helm/helmreleases/#drift-detection). -->

**Verify** both report `suspend: true` before proceeding.

## Step 3 — merge #567 (Gitea still up, change not yet applied)

```bash
python <scratchpad>/merge.py 567
```

**Verify:** `apps` `lastAppliedRevision` is UNCHANGED — proving the suspend held. If it moved, the
switch has been applied while Gitea was still writing to S3: go to Rollback R1.

<!-- codex: CRITICAL — An unchanged lastAppliedRevision does not exclude a partial/in-progress apply or independent Helm action. Record the baseline before suspension and verify live HelmRelease values, the Deployment template and effective backend remain on S3. Before shutdown, require the exact merged cutover SHA on GitHub and in flux-system GitRepository's Ready artifact: Gitea's mirror cannot deliver a missing commit while Gitea is stopped. Annotating apps alone does not fetch a newer source artifact. The execution report also shows the hold must precede any approval that can auto-merge. -->

## Step 4 — database backup

Gitea's metadata (which artifacts exist, which runs they belong to) lives in `infra-pg`, not on the
PVC. A restored PVC without a matching database is not a restore.

```bash
PRIMARY=$($K -n databases get cluster infra-pg -o jsonpath='{.status.currentPrimary}')
$K -n databases exec "$PRIMARY" -c postgres -- pg_dump -U postgres -d gitea -Fc \
  > _out/gitea-db-precutover-$(date +%Y%m%d%H%M).dump
```

Confirm the dump is non-trivial in size and readable (`pg_restore --list | head`). `_out/` is
gitignored. Discover the exact DB/role names in step 0 rather than assuming.

<!-- codex: CRITICAL — This dump precedes writer shutdown. Although pg_dump is internally consistent, subsequent Gitea metadata changes, deletions and offloads make it an unmatched cutover recovery point. Take and validate a final dump after Gitea has stopped and before the copy/backend change. Preserve corresponding Gitea data/repository volume state if database rollback is intended. Check dump exit status and the complete archive listing, rather than relying on size or a pipeline ending in head. -->

## Step 5 — stop the writers  ← OUTAGE BEGINS

<!-- codex: HIGH — Drain or explicitly cancel Actions and pause new submissions before scaling down; failed runner requests do not prove external jobs have stopped or cannot retry when Gitea returns. Maintain that admission hold through acceptance, allowing only controlled test runs. Pre-stage migration, cutover and rollback payloads outside Gitea before this point. -->

```bash
$K -n gitea scale deployment gitea --replicas=0
$K -n gitea wait --for=delete pod -l app.kubernetes.io/name=gitea --timeout=180s
```

Runners will fail their jobs; that is expected and is why this is a window.

## Step 6 — final delta copy, with nothing writing

<!-- codex: HIGH — Step 5 stops Gitea only. The independent backup Kustomization leaves Velero and talos-backup active. Their separate buckets mean they need not stop merely to freeze Actions data, but gateway-wide writer quiescence is unproved. If required, pause Velero schedules and backup/prune CronJobs, prevent controllers from re-enabling them, and verify no active Backups, PodVolumeBackups, maintenance Jobs or talos-backup/prune pods remain. Coordinate the probe and off-site sync too. Suspending reconciliation or schedules does not stop existing work; save prior states for restoration. See [CronJob suspension](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/#schedule-suspension). -->

`apps` is suspended, so Flux will not recreate the Job. Apply it from the merge-base of #567:

<!-- codex: CRITICAL — gitea/main is a local remote-tracking ref, and step 3 does not refresh it. Its first parent may therefore be the pre-#568 checksum version; after fetching, the answer still depends on merge strategy and intervening commits. A first parent is not generally the PR's merge-base. After #568 lands and before #567 merges, pin verified main as PRE_CUTOVER_SHA and save its migration manifest locally. Validate its copy command and apply that saved file with explicit error handling. See [Git revision syntax](https://git-scm.com/docs/gitrevisions). -->

```bash
$K -n gitea delete job gitea-actions-migrate --ignore-not-found
git show gitea/main~1:kubernetes/apps/apps/gitea/actions-migrate-job.yaml | $K -n gitea apply -f -
$K -n gitea wait --for=condition=complete job/gitea-actions-migrate --timeout=1800s
$K -n gitea logs job/gitea-actions-migrate --tail=20
```

The Job fails loudly (`do NOT cut over`) if the destination does not contain the source. If it
fails, go to Rollback R2 — do not continue.

Then release the volume:

<!-- codex: HIGH — Job deletion can precede dependent Pod termination. Require bounded waits for migration pods to disappear before recreation and before Gitea mounts the PVC. A kubectl wait timeout does not cancel the Job; explicitly stop it and verify exit before rollback/resume. The current Job has retries and no activeDeadlineSeconds, so the 1800-second client wait does not bound its lifetime. Every failed gate must prevent later commands. -->

```bash
$K -n gitea delete job gitea-actions-migrate
$K -n gitea get pods -l job-name=gitea-actions-migrate      # expect: none
```

## Step 7 — resume; Gitea comes up on local storage

<!-- codex: CRITICAL — Resuming Helm first permits a pending action or enabled drift correction to restart Gitea on S3 after the final copy. Keep Helm suspended while apps applies the verified cutover artifact; confirm the live HelmRelease has the local paths/mount and no migration pods remain, then resume and request Helm reconciliation. Do not wait for apps' overall Ready before releasing Helm, because apps has wait:true. Require Helm Ready for the new generation and the intended apps revision before test writes. -->

```bash
$K -n gitea patch helmrelease gitea --type=merge -p '{"spec":{"suspend":false}}'
$K -n flux-system patch kustomization apps --type=merge -p '{"spec":{"suspend":false}}'
$K -n flux-system annotate kustomization apps reconcile.fluxcd.io/requestedAt="$(date -u +%FT%TZ)" --overwrite
```

**Verify:**
- pod Running 1/1 with `/data-actions` mounted
- rendered `app.ini` has `STORAGE_TYPE=local` for both sections and **no** `[storage.actions_s3]`
- `GITEA__metrics__TOKEN` and `GITEA__database__PASSWD` still present
- web UI serves; an existing run's artifact and log are downloadable (history survived)
- a NEW CI run produces an artifact, and it lands on the PVC not in S3

**OUTAGE ENDS.**

<!-- codex: HIGH — This ends the outage too early: A1/A2 delete the sole Gitea pod and B scales it to zero again. Keep acceptance inside the announced window; restore normal admissions afterward. The parent plan requires B's independent-repair proof before the storage cutover. For the original procedure, 3 minutes shutdown + 30 copy wait + 15 Helm rollout + roughly 20 acceptance + 20 rollback already total 88 minutes, excluding dump time. Reserve 90–120 minutes provisionally. Given the reported completed cutover, budget the remaining tests separately: approximately 25–40 minutes testing/cleanup plus 20 recovery gives a provisional 45–60-minute window. Neither estimate is a measured bound; establish per-phase deadlines and a latest rollback-start time. -->

## Step 8 — acceptance A: Gitea does not need the USB disk

The plan's headline requirement: *Gitea starts and serves with `:7070` unreachable*, from a COLD
start, in both modes.

Structural check first — after the cutover Gitea has no `:7070` configuration at all:

```bash
$K -n gitea exec deploy/gitea -c gitea -- grep -c 7070 /data/gitea/conf/app.ini   # expect 0
```

**A1 — silent hang** (the actual 2026-09-08 failure: TCP accepted, never answered). Blackhole the
endpoint from the gitea namespace, then cold-start:

<!-- codex: HIGH — Dropping egress typically prevents TCP establishment; it does not reproduce TCP accepted followed by silence. Pre-stage a reversible fault that proves an established connection followed by stalled TLS/HTTP, and verify it from the Gitea pod network. The placeholder policy is not executable; standard NetworkPolicies are additive allow rules, so an existing allow can defeat the exclusion. Scope the fault to port 7070, preserve required DNS/database/NAS access, and provide cleanup that survives session loss. See [NetworkPolicy behavior](https://kubernetes.io/docs/concepts/services-networking/network-policies/). -->

```bash
$K apply -f <a NetworkPolicy in ns gitea denying egress to 192.168.1.225/32>
$K -n gitea delete pod -l app.kubernetes.io/name=gitea      # cold start
```
Expect: pod Ready, UI serves, artifacts still listed. Then remove the policy.

**A2 — connection refused.** versitygw genuinely stopped. The W3 watchdog would restart it within
3 minutes, so pause its cron first:

<!-- codex: CRITICAL — The sed expression below is malformed: the # before PAUSED terminates the replacement, leaving invalid substitution flags. Reproduced locally: unknown option to s. Appending a trailing comment would not disable the schedule either; comment out the exact cron entry at its beginning and verify durable and installed crontabs. Pre-stage tested stop/start/restore commands instead of the placeholder. -->

<!-- codex: CRITICAL — Restoration must survive SSH/session loss; a local EXIT trap is insufficient. Before pausing, arm and verify an independent, time-bounded recovery task on the NAS internal pool that restores the original watchdog entry and gateway service. Preserve unrelated cron entries, drain already-running watchdog invocations, then stop the gateway and prove port 7070 refuses connections. Disarm recovery only after durable/live cron state, a watchdog execution and an authenticated S3 round-trip pass. -->

```bash
python scripts/qnap-ssh.py --sudo "sed -i 's#^\*/3 .*versitygw-supervisor.*#\0 # PAUSED#' /etc/config/crontab && crontab /etc/config/crontab"
# stop versitygw, cold-start gitea, verify, then restart versitygw and RESTORE the cron
```

**Blast radius:** while versitygw is down, Velero, talos-backup and the W4 probe fail. That is
expected and is itself a check that the alerting works — `VersitygwProbeFailed` should fire and then
clear. Keep A2 short. **Restoring the watchdog cron is mandatory** and must be verified, not assumed.

<!-- codex: HIGH — A short A2 may miss the probe: it runs every 10 minutes, has a 120-second deadline, and the alert has for:2m. Scheduled failure detection can take about 14 minutes plus scrape/evaluation delay, followed by another probe to clear it. Use non-overlapping manual probe Jobs named versitygw-probe-.*, establish baseline success, observe the alert firing, then restore and require a newer success. Include this in the recovery deadline. Paused backup jobs cannot demonstrate failure behavior; that requires a separately controlled test if intended. -->

## Step 9 — acceptance B: repair works with the forge down (W5)

The plan: *demonstrate a fresh GitHub fetch and application with Gitea stopped; reconciling from a
cached artifact does not count.*

<!-- codex: HIGH — Scaling to zero is asynchronous and does not prove Gitea stays down. Apply necessary reconciliation holds, wait for all Gitea pods to terminate and the endpoint to become unavailable, and maintain that condition through fetch/apply. The checked-in source-controller uses emptyDir, supporting cache loss; verify the live mount and new pod UID, and record a post-restart GitHub fetch/artifact revision. Stale Ready alone is insufficient. Replace the comment-only reconcile step with a concrete request for a named safe Kustomization and evidence that it handled the request and applied the expected resource. Restore saved replicas/holds and wait for Gitea Ready. -->

```bash
$K -n gitea scale deployment gitea --replicas=0             # forge down
$K -n flux-system delete pod -l app=source-controller       # drop ALL cached artifacts
# confirm the GitRepository re-fetches from GitHub and goes Ready
# then force-reconcile a Kustomization and confirm it applies
$K -n gitea scale deployment gitea --replicas=1
```

Restarting source-controller is what makes this a *fresh* fetch rather than a cached one — its
artifact cache does not survive the pod.

**Honest limitation:** this proves fetch + apply with the forge down. It does not prove applying a
revision *authored* while the forge is down, which would need a direct push to GitHub — no GitHub
remote or credential is configured in this checkout, and the emergency-write policy requires pausing
the mirror first. Recorded as a residual gap rather than silently skipped.

## Rollback

<!-- codex: CRITICAL — R2/R3 require reverting on Gitea while it is stopped or unhealthy; the stated lack of GitHub write credentials leaves that path unavailable. Pre-stage a tested direct Kubernetes/Helm restoration from saved S3 manifests, retaining reconciliation holds until Gitea is restored and the revert is merged, mirrored and fetched. Preserve the Actions PVC and prevent recreation of the migration Job. Specify how to resume the intended Helm action without apps reapplying the failed cutover. -->

- **R1 — cutover applied too early.** Revert #567 on `main`; Gitea returns to S3 (the
  `gitea-actions-s3` secret was deliberately kept). The PVC copy is untouched.
- **R2 — final copy fails.** Do not resume. Revert #567, resume `apps` and the HelmRelease; Gitea
  restarts on S3 exactly as before. Nothing has been deleted from S3 at any point — the migration
  only ever copies.
- **R3 — Gitea unhealthy on local storage.** Revert #567 and reconcile. If the pod cannot start at
  all, `kubectl scale` it to 0, revert, then resume.
- **R4 — database.** Restore the step-4 dump into `infra-pg`.

<!-- codex: CRITICAL — R1/R3 can occur after local writes, and step 7 explicitly creates one. Reverting configuration then exposes stale S3 data. Stop writers again, retain both stores, and reconcile/verify post-cutover changes before switching back, or use an explicitly accepted recovery point with matching database/filesystem state. R4 must target only the gitea database in shared infra-pg, use the quiesced dump and include a verified restore procedure. The parent plan already warns rollback is asymmetric after local writes; retaining the old bucket does not make every rollback cheap. -->

The S3 data is never deleted by any step here, which is what makes every rollback cheap.

## Acceptance criteria

- [ ] Gitea serves, and Actions history from before the cutover is readable
- [ ] A new CI run's artifact and log land on the PVC, not in S3
- [ ] `app.ini` contains no `:7070` reference
- [ ] Gitea cold-starts and serves with the endpoint blackholed (A1)
- [ ] Gitea cold-starts and serves with versitygw stopped (A2), and the watchdog cron is restored
- [ ] `VersitygwProbeFailed` fires during A2 and clears afterwards
- [ ] A fresh GitHub fetch + apply succeeds with Gitea stopped (B)
- [ ] All Kustomizations Ready at the end; `backup` layer healthy

<!-- codex: HIGH — Ready alone does not prove reconciliation/schedules were restored: suspended resources can retain earlier Ready conditions. Compare suspension flags, replicas, schedules and cron entries with saved states, remove fault rules/recovery tasks, and verify current source/Helm generations, successful probe recovery and resumed backup operation. -->

## WHAT ACTUALLY HAPPENED — this plan was overtaken by events

Recorded before the remaining steps, because it changes them.

**The cutover applied itself before the window.** #567 was approved by both review bots and the
estate **auto-merges on approval**, so it merged at 14:49:09Z and Flux applied it immediately. The
suspend-then-merge ordering in steps 2-3 below never ran. This is a process finding, not a one-off:
a PR that must not take effect on merge cannot be protected by intending to merge it later — it has
to be held as a DRAFT, or its Kustomization suspended *before* the PR is opened.

<!-- codex: CRITICAL — This report supersedes steps 1–7; do not rerun their old-state assumptions against the live local backend. Before remaining work, capture actual backend/revisions and validate auto-merge behavior for the chosen hold: a draft must demonstrably block the estate's automation. Any future cutover needs the hold in place before requesting reviews, plus the source-artifact and controller-quiescence gates above. -->

**Consequence: the write window this plan existed to prevent actually opened.** Measured immediately
after:

| | S3 (source of truth) | PVC (what Gitea could see) |
|---|---|---|
| artifacts | 8,982 | 8,982 — complete |
| logs | 48,831 | 48,788 — **43 missing** |

**Nothing was lost**, because the migration only ever `copy`s and never deletes — S3 still held
everything. Recovered with a one-off catch-up Job pinned to Gitea's node (RWO is per-NODE, so a pod
elsewhere could not have mounted the volume) using `--size-only`. It completed in **11 seconds**,
which is the clearest possible evidence for the `--size-only` change: the same work with
`--checksum` had been running for 20+ minutes and was on track for ~2 hours.

After recovery: artifacts 8,982 = 8,982, logs 48,836 >= 48,831. The destination now holds *more*
than the source, which is correct — those are new logs Gitea has written locally since the cutover,
and they are the proof that local writes work.

<!-- codex: HIGH — Aggregate count equality/inequality does not prove no objects were lost, correct prefix mapping, readable contents or database consistency; extra local objects can hide missing source keys. Record per-key containment/size verification for both prefixes, preserve the immutable-key assumption behind size-only copy, and verify old/new artifacts and logs through Gitea. The catch-up ran alongside local writes, so retain both stores and account for concurrent cleanup. Eleven seconds measures this catch-up only, not cold starts, alerting, cleanup or rollback. -->

**Imperative work disclosed:** the catch-up Job was applied with `kubectl`, not through git, because
the cutover had already (correctly) pruned the migration manifest. It is deleted again; the estate
matches git.

### Two defects found while verifying

1. **Removing a config section from the HelmRelease does NOT remove it from the on-PVC `app.ini`.**
   The chart's `environment-to-ini` MERGES into the existing file, so `[storage.actions_s3]` — with
   its credentials — is still present and still says `STORAGE_TYPE = minio`, even though the values
   no longer define it. It is inert (nothing references that named section any more; the two
   sections that matter say `STORAGE_TYPE = local`), but it is misleading and it defeats the
   "no `:7070` in app.ini" acceptance criterion as originally written.
2. **A credential was printed to the session transcript** while grepping that config. The versitygw
   `gitea` S3 key must be rotated. It is LAN-scoped and now unused except for rollback, but a leaked
   credential is a leaked credential.

<!-- codex: HIGH — Credential rotation and residual app.ini cleanup are now outstanding closure tasks. Rotate the exposed key and update the retained rollback secret through a path that does not print values; verify the old key is rejected. Remove the obsolete named section while preserving database/metrics settings and then prove the effective configuration and cold-start behavior. An unreferenced section is not, by itself, evidence that the required failure-mode tests pass. -->

<!-- codex-review-status: complete -->


## RESULTS — everything executed, 2026-09-08

Run after the codex review, against the live estate. Where the review changed the method, the
method changed.

### W1 — verified THROUGH GITEA, not by file counts

The review's objection to counting files was right: extra local objects can mask missing source
keys. So both directions were proved by fetching through the forge:

| | |
|---|---|
| migrated history | job 189125 (run #1507, **2026-09-07**, i.e. pre-cutover) — 14,558 bytes, 172 lines |
| new writes | a post-cutover run's log (15:34Z) — 14,127 bytes |

A wrong prefix mapping would 404 rather than return bytes, so this also proves the mapping.

### A1 — Gitea cold-starts with the gateway unreachable

My first attempt broke cluster DNS and crashlooped Gitea for ~5 minutes: an `ipBlock` allow-except
NetworkPolicy does not permit in-cluster traffic under Cilium, which matches cluster destinations by
IDENTITY, not CIDR. Redone with the review's shape — a `CiliumNetworkPolicy` with `egressDeny` and
`enableDefaultDeny.egress: false`, scoped to TCP/7070 — and validated on a disposable canary first:

```
DNS PASS · postgres:5432 PASS · NAS:22 PASS (control) · NAS:7070 BLOCKED
```

NAS:22 is the control that proves only the port is denied, not the host. Then, on Gitea:
**Ready in 22s**, healthz `pass`, external 200, and the migrated log served byte-identically
(14,558 bytes) while blocked.

### A2 — Gitea cold-starts with versitygw genuinely STOPPED

The review reproduced the malformed `sed` from this plan locally (`unknown option to s`) and pointed
out that editing cron leaves the gateway unwatched if the session dies. Replaced with an **expiring
maintenance lease** in the watchdog (`feat/watchdog-maintenance-lease`): while valid the watchdog
reports `maintenance` and remediates nothing; it expires on its own, so a lost session self-heals,
and the cron entry is never touched.

versitygw stopped — refused from both the NAS and the cluster. **Gitea Ready in 47s**, healthz
`pass`, external 200, migrated history readable.

Restored by the watchdog itself: lease cleared, it detected down + disk-ok and restarted the
gateway (`restarted`, http=403) — which incidentally proved its restart path in production.

**Blast radius was exactly as documented**: 9 Velero Kopia maintenance jobs failed with
`connection refused` to `:7070`, and the BackupStorageLocation returned to `Available` on its own
afterwards. The backup layer degraded; the forge did not. As the review predicted, a short A2 does
NOT exercise `VersitygwProbeFailed` — the probe is 10-minutely with `for: 2m`, so ~14 minutes are
needed. Not claimed as proven.

### B — repair works with the forge down

Rather than deleting every production source artifact, the review's narrower proof: a **brand new**
GitRepository, created while Gitea was confirmed absent (0 pods, endpoints empty, external 502).
Nothing cached can serve a name that never existed.

```
fetched in 20s, Ready=True, revision ca2c843905ab… == GitHub HEAD exactly
```

And application, not just fetch: a Kustomization reconcile requested while the forge was down was
handled (`lastHandledReconcileAt` advanced, `Ready=True`) at the GitHub revision.

### The stale config section — the fix was not where I first looked

`[storage.actions_s3]`, with credentials, survived the cutover in the on-PVC `app.ini`. Editing the
file was not enough: it came back on the next start. The cause is that the chart stores **each config
section as a KEY** in the `gitea-inline-config` Secret, and Helm did not prune the key when the value
was removed from the HelmRelease — the live HelmRelease no longer declares it, yet the Secret still
did. Removing the key, then re-cleaning `app.ini` with Gitea stopped, made it durable:

```
after restart:        7070 refs 0 · actions_s3 sections 0 · MINIO_SECRET 0
after Helm reconcile: 0 stale keys
```

The excision is section-aware and atomic, with a backup kept — `app.ini` also holds generated
application secrets and must never be regenerated wholesale.

### Credential rotation

The versitygw `gitea` secret was exposed in a session transcript and has been rotated: new value
generated locally, sent over the SSH channel's STDIN (never argv, which is visible in the NAS
process table), verified by authenticating and listing 48,831 objects. The access key is unchanged —
it is an identifier and it owns the bucket under versitygw's ownership-based authorisation.

### Final state

`0` Kustomizations not Ready · `0` unhealthy pods · BSL `Available` · watchdog `healthy` · no
maintenance lease outstanding · no `Versitygw*` alert firing · forge serving internally and
externally.

### Still open, stated rather than skipped

- **The true "TCP accepted but silent" mode is not reproduced.** A1 denies packets (timeout) and A2
  refuses. Reproducing the 2026-09-08 shape needs a proxy that accepts, completes TLS, then withholds
  the response. The structural argument is now stronger than any single fault though: `app.ini`
  contains **zero** references to `:7070`, so Gitea cannot reach it in any mode.
- **`VersitygwProbeFailed` firing and clearing** was not observed (A2 was shorter than the ~14 minute
  detection path).
- **Applying a revision authored while the forge is down** still requires a direct GitHub push; no
  GitHub remote or credential is configured here, and the emergency-write policy requires pausing the
  mirror first.
