# Execution plan — W1 Actions cutover + W5/W1 acceptance

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

## Step 2 — suspend, so merging does not apply early

```bash
$K -n flux-system patch kustomization apps --type=merge -p '{"spec":{"suspend":true}}'
$K -n gitea patch helmrelease gitea --type=merge -p '{"spec":{"suspend":true}}'
```

Both: the Kustomization stops re-applying the HelmRelease, and suspending the HelmRelease stops
helm-controller reverting a manual `scale` as drift. Suspending only one leaves a path to a surprise
roll mid-window.

**Verify** both report `suspend: true` before proceeding.

## Step 3 — merge #567 (Gitea still up, change not yet applied)

```bash
python <scratchpad>/merge.py 567
```

**Verify:** `apps` `lastAppliedRevision` is UNCHANGED — proving the suspend held. If it moved, the
switch has been applied while Gitea was still writing to S3: go to Rollback R1.

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

## Step 5 — stop the writers  ← OUTAGE BEGINS

```bash
$K -n gitea scale deployment gitea --replicas=0
$K -n gitea wait --for=delete pod -l app.kubernetes.io/name=gitea --timeout=180s
```

Runners will fail their jobs; that is expected and is why this is a window.

## Step 6 — final delta copy, with nothing writing

`apps` is suspended, so Flux will not recreate the Job. Apply it from the merge-base of #567:

```bash
$K -n gitea delete job gitea-actions-migrate --ignore-not-found
git show gitea/main~1:kubernetes/apps/apps/gitea/actions-migrate-job.yaml | $K -n gitea apply -f -
$K -n gitea wait --for=condition=complete job/gitea-actions-migrate --timeout=1800s
$K -n gitea logs job/gitea-actions-migrate --tail=20
```

The Job fails loudly (`do NOT cut over`) if the destination does not contain the source. If it
fails, go to Rollback R2 — do not continue.

Then release the volume:

```bash
$K -n gitea delete job gitea-actions-migrate
$K -n gitea get pods -l job-name=gitea-actions-migrate      # expect: none
```

## Step 7 — resume; Gitea comes up on local storage

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

## Step 8 — acceptance A: Gitea does not need the USB disk

The plan's headline requirement: *Gitea starts and serves with `:7070` unreachable*, from a COLD
start, in both modes.

Structural check first — after the cutover Gitea has no `:7070` configuration at all:

```bash
$K -n gitea exec deploy/gitea -c gitea -- grep -c 7070 /data/gitea/conf/app.ini   # expect 0
```

**A1 — silent hang** (the actual 2026-09-08 failure: TCP accepted, never answered). Blackhole the
endpoint from the gitea namespace, then cold-start:

```bash
$K apply -f <a NetworkPolicy in ns gitea denying egress to 192.168.1.225/32>
$K -n gitea delete pod -l app.kubernetes.io/name=gitea      # cold start
```
Expect: pod Ready, UI serves, artifacts still listed. Then remove the policy.

**A2 — connection refused.** versitygw genuinely stopped. The W3 watchdog would restart it within
3 minutes, so pause its cron first:

```bash
python scripts/qnap-ssh.py --sudo "sed -i 's#^\*/3 .*versitygw-supervisor.*#\0 # PAUSED#' /etc/config/crontab && crontab /etc/config/crontab"
# stop versitygw, cold-start gitea, verify, then restart versitygw and RESTORE the cron
```

**Blast radius:** while versitygw is down, Velero, talos-backup and the W4 probe fail. That is
expected and is itself a check that the alerting works — `VersitygwProbeFailed` should fire and then
clear. Keep A2 short. **Restoring the watchdog cron is mandatory** and must be verified, not assumed.

## Step 9 — acceptance B: repair works with the forge down (W5)

The plan: *demonstrate a fresh GitHub fetch and application with Gitea stopped; reconciling from a
cached artifact does not count.*

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

- **R1 — cutover applied too early.** Revert #567 on `main`; Gitea returns to S3 (the
  `gitea-actions-s3` secret was deliberately kept). The PVC copy is untouched.
- **R2 — final copy fails.** Do not resume. Revert #567, resume `apps` and the HelmRelease; Gitea
  restarts on S3 exactly as before. Nothing has been deleted from S3 at any point — the migration
  only ever copies.
- **R3 — Gitea unhealthy on local storage.** Revert #567 and reconcile. If the pod cannot start at
  all, `kubectl scale` it to 0, revert, then resume.
- **R4 — database.** Restore the step-4 dump into `infra-pg`.

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

## WHAT ACTUALLY HAPPENED — this plan was overtaken by events

Recorded before the remaining steps, because it changes them.

**The cutover applied itself before the window.** #567 was approved by both review bots and the
estate **auto-merges on approval**, so it merged at 14:49:09Z and Flux applied it immediately. The
suspend-then-merge ordering in steps 2-3 below never ran. This is a process finding, not a one-off:
a PR that must not take effect on merge cannot be protected by intending to merge it later — it has
to be held as a DRAFT, or its Kustomization suspended *before* the PR is opened.

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

<!-- codex-review-status: pending -->
