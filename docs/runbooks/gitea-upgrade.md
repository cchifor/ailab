# Runbook: upgrading Gitea (forward-only migrations)

Every Gitea version bump runs database migrations that cannot be undone: an older Gitea refuses to start on a migrated
database. So an upgrade is a one-way door with a restore as the only way back. This runbook is the procedure. It was
written for 1.26.1 → 1.27.3 (chart 12.6.0 → 12.7.0, 2026-11-08); the plan and its review are in
`plans/2026-11-08-gitea-1-27-upgrade-plan.md`. 1.27.3 → 28.4.0 (chart unchanged at 12.7.0) follows it too:
`plans/2026-11-16-gitea-28-upgrade-plan.md`. Gitea dropped the `1.` prefix at 28.0, so 28.x is the release after 1.27.x.

## What is in place

- **No rollback remediation.** `kubernetes/apps/apps/gitea/gitea.yaml` sets `upgrade.strategy.name: RetryOnFailure`:
  a failed or slow upgrade is retried and never rolled back.
- **Pinned image.** The release pins `image.tag`, so the chart's default appVersion never slips in unreviewed.
- **No native database backup.** infra-pg has no WAL archive or Barman, and Velero skips its data volumes. The upgrade
  backup is therefore a `pg_dump` taken during a write freeze, kept off-cluster.
- **`app.ini` is merged, never rewritten.** The chart's `init-app-ini` container runs
  `gitea config edit-ini --apply-env` onto the existing `/data/gitea/conf/app.ini` on the data volume. It adds and
  changes keys but never deletes one, so a key removed from the HelmRelease values stays in the file and keeps taking
  effect. Removing one is a file edit while Gitea is down (step 5).
- **A dropped section also stays in the chart's Secret.** The values reach `init-app-ini` through the
  `gitea-inline-config` Secret, one key per `app.ini` section, which the chart renders with `stringData`. When the values
  drop a whole section, Helm's upgrade patch deletes it from `stringData`, which the live object never holds, so the
  section stays in the Secret's `data`. `init-app-ini` then writes it back into `app.ini` at every start (2026-11-16:
  `[webhook]`). A key dropped from a section that stays is not affected: that section's whole value is replaced.
- **Views on Gitea's tables.** The gitea database holds the `credential_inventory` views
  (`kubernetes/apps/databases/gitea-credential-inventory.sql`), which read `"user"`, `access_token` and `oauth2_grant`.
  A migration that drops or rebuilds one of those tables fails while the views exist. The rehearsal catches this,
  because the dump includes the views.

## Procedure

Tools: `kubectl --context admin@ai`, `helm` 4.x, Docker. Owner-only API steps use `scripts/gitea-owner-op.sh`.

1. **Rehearse.**
   - Restore the latest dump into a throwaway `postgres:<major>` container (create role `gitea`;
     `pg_restore --exit-on-error`).
   - Run the target image's `gitea migrate` against it. Record the duration and the resulting
     `select version from version`; that is the acceptance value for step 6.
   - Query the `credential_inventory` views as `pg_monitor` on the migrated copy, and diff `team` and `team_unit`
     against an untouched copy (`create database <x> template gitea` before migrating).
   - Smoke-boot the target image on a copy of the migrated database with the new config, on a
     `docker network create --internal` network: the copy's pending webhooks and mirrors must not reach anything.
     Read its startup log for config problems (`[E]`/`[W]`, `Deprecation`), and its `/metrics` families.
2. **Audit.** Check workflows for the release's breaking changes (for 1.27: cross-instance reusable workflows, by URL;
   for 28.x: the plan's list) in every repository with Actions, and custom templates under `/data/gitea/templates`.
   Render the chart with the new values (`helm template`) and diff it against the old render.
   - Run `scripts/gitea-workflow-check.sh <target>`. It runs the target version's own workflow parser over every
     workflow on every repository's default branch (28.0 and later; older trees lack `modules/actions/jobparser`).
     Every `INVALID` line is a workflow that stops running at the upgrade: Gitea drops it with only a log warning.
     Get each one fixed in its repository before the window. Reading release notes is not enough:
     2026-11-16 missed that 28.x rejects `matrix` in a job-level `if:`, and platform's `ci.yml` stopped
     (platform #2778).
3. **Stage recovery files on the workstation, before anything stops:**
   - `helm -n gitea get values gitea -a -o yaml > gitea-values-before.yaml`;
   - `helm -n gitea get manifest gitea > gitea-manifest-before.yaml`;
   - the current chart, from `helm pull gitea --repo https://dl.gitea.com/charts --version <old>`.
4. **Freeze Flux for the release, then merge the change.**
   - Annotate the HelmRelease so kustomize-controller stops re-applying it, then suspend it with Flux's own field
     manager. kustomize-controller strips changes made under kubectl's managers, but not under this one:

     ```bash
     kubectl -n gitea annotate hr gitea kustomize.toolkit.fluxcd.io/reconcile=disabled --overwrite
     kubectl -n gitea patch hr gitea --type merge --field-manager=flux-client-side-apply -p '{"spec":{"suspend":true}}'
     ```

   - Suspension does not cancel a Helm action already running. Wait until `helm -n gitea status gitea` reports
     `deployed` (not `pending-*`) and the HelmRelease is not `Progressing`.
   - Merge the PR. Check the merge SHA on the GitHub mirror and on `GitRepository` `flux-system`.
   - Check that the live HelmRelease still names the OLD chart and image, and that `spec.suspend` is still `true`.
5. **Write freeze and backups.**
   - **Running CI jobs fail.** Gitea's `stop_zombie_tasks` check runs at startup and every 5 minutes. It fails every
     running task with no update from its runner for `ZOMBIE_TASK_TIMEOUT` (10 minutes), so how many jobs the stop costs
     depends on the outage:
     - longer than 10 minutes: every job that was running fails as Gitea starts (2026-11-08: 14 minutes, 18 jobs,
       10 s after the start);
     - shorter: a job that is still running reports again and finishes normally. A job that ends on its runner while
       Gitea is down cannot report its result, and Gitea fails it 10 minutes after its last update (2026-11-16:
       4 min 42 s; of 23 running jobs, 8 reported again and 13 short ones failed; newer pushes cancelled the other 2).

     Keeping the outage under 10 minutes therefore saves the long jobs. Note the running jobs first, on the primary,
     database `gitea` (job status 6 is running, 5 waiting):

     ```sql
     select p.owner_name || '/' || p.name as repo, r.ref, r.index, r.id as run_id
     from action_run_job j join action_run r on r.id = j.run_id join repository p on p.id = r.repo_id
     where j.status = 6 order by repo, r.ref, r.index;
     ```

     After step 7, once the failed jobs show up, re-run the newest run of each ref:
     `scripts/gitea-api.sh POST /repos/<repo>/actions/runs/<run_id>/rerun` (1.27). On 28.x, use
     `.../runs/<run_id>/rerun-failed-jobs` once the run has finished, so the jobs that passed are not run again. A run
     that a newer run of the same ref superseded needs no re-run. `ci-rerun-watchdog` does not re-run them: it skips
     jobs that did not run on a cloud runner (`job_not_cloud`).
   - Run `kubectl -n gitea scale deploy/gitea --replicas=0`, then wait for `kubectl -n gitea get pods -l
     app.kubernetes.io/name=gitea` to list nothing. Gitea is the only writer to its database and volumes.
   - **Database.** Dump to the workstation and require both exit statuses to be 0:
     `kubectl -n databases exec <primary> -c postgres -- pg_dump -U postgres -Fc -d gitea >
     gitea-pre-<version>-<utc>.dump`, then `pg_restore -f /dev/null <file>` in a `postgres:<major>` container.
   - **Volumes.** Start a holder pod in `gitea` that mounts `gitea-shared-storage` and `gitea-actions-storage`
     (RWO, so only after the Gitea pod is gone). Create a Velero `Backup` named `gitea-pre-<version>` from the
     `velero-daily` template, restricted to `includedNamespaces: [gitea]`, with `defaultVolumesToFsBackup: true` and
     `ttl: 720h`. Wait for `Completed`, and for both `PodVolumeBackup`s (`-l velero.io/backup-name=gitea-pre-<version>`)
     to be `Completed`. Then remove any keys the change deletes from the values (next bullet), and delete the holder
     pod and wait until it is gone.
   - **Keys removed from the values** (see "`app.ini` is merged"). If the change drops a whole section, delete it from
     the Secret first, or `init-app-ini` writes it back at step 6. The step-6 upgrade does not restore it:

     ```bash
     kubectl -n gitea patch secret gitea-inline-config --type=json -p '[{"op":"remove","path":"/data/<section>"}]'
     ```

     For 28.x the section is `webhook`. Then, in the holder pod, after the backup, with `<data>` the holder's mount of
     `gitea-shared-storage`, edit `app.ini` in three steps:
     1. Back the file up and check the copy.
     2. Build a candidate without the keys, and check it removes exactly `$n` lines.
     3. Only then copy the candidate over the file through a redirection. That keeps the file's inode, owner
        `1000:1000` and mode 660. `sed -i` would replace it with a file owned by the holder's user, which Gitea's
        init container (uid 1000) cannot rewrite.

     The first line sets what goes: one section, the keys to drop in it (whole key names, `|` between them), and how
     many lines that is. For 28.x, `[webhook] ALLOWED_HOST_LIST` moved to `[security]`. Rollback step 3 runs the same
     block with other settings.

     ```sh
     sec='[webhook]'; keys='ALLOWED_HOST_LIST'; n=1; b_suffix=pre-<version>
     f=<data>/gitea/conf/app.ini; b="$f.$b_suffix"; c=/tmp/app.ini.candidate
     (
       stop() { echo "STOP: $*; app.ini was not changed"; exit 1; }
       # An existing $b is the original from an earlier run: never overwrite it, never read it as the current file.
       [ ! -e "$b" ] || stop "$b already exists"
       cp -p "$f" "$b" && cmp "$f" "$b" || stop "backup copy failed"
       # Matching ignores CR and leading blanks, and every section header (indented or not) ends the previous
       # section; the other lines are copied byte for byte.
       awk -v sec="$sec" -v keys="$keys" '{ l=$0; sub(/\r$/,"",l) }
            l ~ /^[ \t]*\[/ { s=l; gsub(/^[ \t]+|[ \t]+$/,"",s) }
            !(s==sec && l ~ ("^[ \t]*(" keys ")[ \t]*="))' "$b" > "$c" || stop "awk failed"
       diff "$b" "$c"                             # read it: only those keys in that section go
       # awk can only drop lines, so "exactly $n lines shorter" means exactly the matched lines (any diff format)
       [ -s "$c" ] && [ "$(wc -l < "$b")" -eq "$(( $(wc -l < "$c") + n ))" ] || stop "not exactly $n line(s) removed"
       cat "$c" > "$f" && cmp "$c" "$f" || { echo "FAILED writing app.ini: restore it with cat \"$b\" > \"$f\""; exit 2; }
       echo REWRITTEN
     )
     stat -c '%u:%g %a' "$f"                      # 1000:1000 660
     ```

     Each check ends the subshell before the write, so every `STOP` leaves `app.ini` as it was; an empty `diff` means
     the keys are already gone. If it stops because `$b` already exists, run `cmp "$f" "$b"`: identical means no
     earlier run rewrote the file, so delete `$b` and run again; different means the rewrite already happened and `$b`
     is the original. If the copy over `$f` itself fails (`FAILED`), put the original back with `cat "$b" > "$f"` (the
     Velero backup holds it too). Skipping this step does not break 28.x: the old `[webhook]` key still applies (with
     `external` dropped as invalid), and it logs two startup errors. Delete `$b` and `$c` after step 7; they hold the
     same secrets as `app.ini`.
   - Name the kinds in full: `kubectl -n velero get backups.velero.io gitea-pre-<version>`. The short name
     `backup` resolves to CNPG's `Backup` kind first, so a wait on it answers NotFound until it times out
     (2026-11-08: it cost 9 of the 14 minutes of outage).
6. **Upgrade.**
   - Remove the annotation (`kubectl -n gitea annotate hr gitea kustomize.toolkit.fluxcd.io/reconcile-`) and
     reconcile the `apps` Kustomization. Check that the live HelmRelease now names the NEW chart and image, and that
     `spec.suspend` is still `true`.
   - Unsuspend with the same field manager:
     `kubectl -n gitea patch hr gitea --type merge --field-manager=flux-client-side-apply -p '{"spec":{"suspend":false}}'`.
   - Watch the `configure-gitea` init container (it runs `gitea migrate`), the pod and the HelmRelease. Never roll back
     mid-migration.
7. **Verify.** Do not stop at the login page:
   - the version (`scripts/gitea-api.sh GET /version`: this forge answers `/api/v1/version` only to a signed-in
     caller), and `select version from version` equal to the rehearsal value;
   - the merge SHA is applied: `GitRepository` `flux-system` and Kustomization `apps` report it, and the HelmRelease is
     Ready for its current generation (HelmRelease revisions name charts, not Git commits);
   - `scripts/gitea-api.sh GET /user`;
   - clone and push a scratch branch, and a CI run on it goes green (runner pick-up, logs, artifacts);
   - a merged PR mirrors to GitHub;
   - a real OIDC login through Authelia (the owner);
   - the reviewer bots post a review;
   - the credential-inventory metrics are present.
   - **28.x additions:**
     - the startup log carries no config problem: `kubectl -n gitea logs deploy/gitea -c gitea | grep -E
       'Deprecation|ALLOWED_HOST_LIST|EGRESS|invalid entry'` prints nothing;
     - no workflow is dropped: `kubectl -n gitea logs deploy/gitea -c gitea | grep 'ignore invalid workflow'`
       prints nothing. Gitea parses a repository's workflows only when an event reaches it, so an empty grep right
       after the start proves little; `scripts/gitea-workflow-check.sh <version>` covers every repository;
     - the org webhooks to the reviewer VMs deliver (the reviewer-bot review above), and their latest deliveries are
       green in the org's webhook settings;
     - a clone over SSH (`gitea-ssh`; 28.0 replaced the built-in SSH server's library);
     - the UI's WebSocket upgrades through the edge: `/-/ws` (28.0 replaced server-sent events) answers
       `101 Switching Protocols` to a signed-in browser (DevTools, Network, WS);
     - the next `gitea-actions-run-retention` execution completes (`retention: deleted=...`), and `ci-rerun-watchdog`
       and `cloud-power` log no Gitea API errors.

## Rollback (data written after step 6 is lost)

1. **Freeze.**
   - Annotate and suspend as in step 4, and wait until no Helm action is pending.
   - Scale `deploy/gitea` to 0 and wait until no Gitea pod exists.
   - On the primary, end the remaining sessions:
     `select pg_terminate_backend(pid) from pg_stat_activity where datname = 'gitea' and pid <> pg_backend_pid();`.
2. **Restore the database** as its owner, with grants (roles `gitea` and `pg_monitor` exist in infra-pg):
   - copy the dump into the primary: `kubectl -n databases cp <file> <primary>:/var/lib/postgresql/data/restore.dump
     -c postgres`;
   - `drop database gitea; create database gitea owner gitea;`;
   - `pg_restore -U postgres -d gitea --exit-on-error /var/lib/postgresql/data/restore.dump`, which must exit 0;
   - check that `select version from version` equals the pre-upgrade value, then delete `restore.dump`.
3. **Install the old version with Gitea held down.** Use the staged values and an explicit old image. NEVER use
   `--reuse-values`: it keeps the new tag and re-migrates the restored database.
   - Render first: `helm template gitea <old chart .tgz> -n gitea -f gitea-values-before.yaml --set
     image.tag=<old> --set replicaCount=0`, and check the image of every container and init container.
   - Then run `helm upgrade` with the same arguments.
   - `app.ini` keeps the keys the new values added unless step 4 restores the volume. For 28.4.0 → 1.27.3 those are
     `[security] EGRESS_MODE`, which 1.27 ignores, and `[security] ALLOWED_HOST_LIST` without `external`. On 1.27 that
     list makes Gitea refuse public OAuth2 avatar fetches (cosmetic; webhooks get their own `[webhook]` list back from
     the old values). Remove both keys, or restore the volumes (step 4). To remove them, keep Gitea at 0 replicas,
     start the step-5 holder pod, and run the step-5 block with its first line set to
     `sec='[security]'; keys='EGRESS_MODE|ALLOWED_HOST_LIST'; n=2; b_suffix=pre-rollback`. The whole-key match leaves
     the chart's own `[security]` keys, such as `INSTALL_LOCK`, in place. Then delete the holder pod.
4. **Restore the volumes, only if their data must match the restored database** (for example, after pushes made
   during the failed window):
   - With Gitea still at 0 replicas, delete the PVCs `gitea-shared-storage` and `gitea-actions-storage` and wait until
     they are gone.
   - Create a Velero `Restore` from `gitea-pre-<version>` with `includedNamespaces: [gitea]` and
     `includedResources: [persistentvolumeclaims, pods]`. This restores the holder pod with both PVCs, and the data
     through `PodVolumeRestore`.
   - Wait for the `Restore` and both `PodVolumeRestore`s to be `Completed`, then delete the restored holder pod.
5. **Start and verify.**
   - `kubectl -n gitea scale deploy/gitea --replicas=1`, then verify as in step 7.
   - Revert the PR (Gitea is back as the writer). Wait until the revert SHA is on the GitHub mirror and in `GitRepository`
     `flux-system`.
   - Only then remove the annotation, and check that the live HelmRelease names the OLD chart and image with
     `replicaCount: 1`.
   - Unsuspend with `--field-manager=flux-client-side-apply`.

## Record

- **2026-11-08: 1.26.1 → 1.27.3** (chart 12.6.0 → 12.7.0; PR #1679, merge `b81ba53b`; plan
  `plans/2026-11-08-gitea-1-27-upgrade-plan.md`).
  - **Outage:** 19:36:26 → 19:50:33 UTC, 14 minutes. Without the `backup` short-name wait it would have been
    about 5.
  - **Dump:** 1.05 GB in 2 min 18 s; `pg_restore -f /dev/null` exited 0. It is kept on the workstation as
    `gitea-pre-1.27.3-20261108T193632Z.dump`, sha256
    `0a519a4b382e8b0c250eb7a5db27b253e8ced2114e5e3c49c030c56835ee4895`.
  - **Volumes:** Velero `gitea-pre-1273`, 143/143 items, completed in 62 s (incremental fs-backup: `data`
    12.5 GB, `actions-storage` 10.8 GB).
  - **Migrations:** 331 → 343 in about 2 s, in the `configure-gitea` init container; 343 matches the rehearsal.
    The HelmRelease is Ready at its generation 25 (Helm revision 20).
  - **Verified:**
    - `gitea-api.sh GET /user` and `GET /version` (1.27.3);
    - a branch push with its CI, and re-run jobs going green;
    - a reviewer-bot review on PR #1678;
    - the ailab push mirror syncing to GitHub;
    - the `cnpg_gitea_*` credential metrics, with no Gitea alert firing;
    - the owner's OIDC login through Authelia (the owner, 2026-11-09).
  - **Cost:** the restart failed 18 running jobs (relay, ailab, workbricks). The newest run of each branch or
    pull request was re-run through the API (17 runs).
- **2026-11-16: 1.27.3 → 28.4.0** (chart 12.7.0 unchanged; PR #1794, merge `b8772752`; plan
  `plans/2026-11-16-gitea-28-upgrade-plan.md`).
  - **Before the stop:** the 28.4.0 image was pulled on the three control-plane nodes. The HelmRelease was frozen at
    about 13:13 UTC, and the `WIP:` prefix came off at about 13:14. The reviewer bots' findings, all on the step-5 snippet, were
    fixed in two pushes, and the reviewer system merged at 13:30.
  - **Outage:** 13:31:17 → 13:35:59 UTC, 4 min 42 s.
  - **Dump:** 941 MB in 1 min 50 s; `pg_restore -f /dev/null` exited 0. It is kept on the workstation as
    `gitea-pre-28.4.0-20261116T133129Z.dump`, sha256
    `15cfa0f486415f31d1682bd650b7249102a8b014ac9800b9e35fc6132ceeb2a5`.
  - **Volumes:** Velero `gitea-pre-2840`, 154/154 items, completed in 27 s (`data` 14.2 GB, `actions-storage`
    6.0 GB).
  - **Migrations:** 343 → 358 in about 1 s, in the `configure-gitea` init container; 358 matches the rehearsal. The
    HelmRelease is Ready at its generation 28 (Helm revision 21).
  - **Key removal, done twice.** The step-5 rewrite removed `[webhook] ALLOWED_HOST_LIST` in the holder pod, but the
    first start logged both deprecation errors again. The `gitea-inline-config` Secret still held a `webhook` key with
    the old value (see "A dropped section also stays in the chart's Secret"), and `init-app-ini` had written it back.
    At 13:39 the key was deleted from the Secret, and the step-5 block was run again on the running pod's `app.ini`
    (`b_suffix=pre-28.4.0-secret`). Gitea reads `app.ini` only at startup, so that start keeps both lists. They allow
    the same two private ranges, so webhook behaviour is unchanged; the two errors go away at the next start.
  - **Verified:**
    - `gitea-api.sh GET /version` (28.4.0) and `GET /user`; the HelmRelease Ready; `GitRepository` `flux-system` and
      Kustomization `apps` at `b8772752`;
    - the org webhooks: the 6 deliveries to the reviewer VMs after the start all succeeded, and none is pending;
    - SSH: the five Flux `GitRepository`s that fetch over `gitea-ssh` fetched again at 13:40;
    - `/-/ws` answers `101 Switching Protocols` through the edge and in-cluster;
    - the `cnpg_gitea_*` credential metrics and Gitea's own scrape (`up` 1), with no Gitea alert firing;
    - a branch push mirrored to GitHub within 6 s;
    - `ci-rerun-watchdog` scans with `errors=[]`, and `cloud-power` logged no error.
  - **Regression: two workflows stopped.** 28.x rejects a job-level `if:` that names a context other than `github`,
    `gitea`, `needs`, `vars` or `inputs`, and drops the whole workflow with a warning. 1.27 accepted it.
    - platform `ci.yml` (job `python`, `needs.changes.outputs[matrix.name]`): no `ci.yml` run, and so no required
      CI status, on any platform pull request after the start. dev-worker-4 reported it (platform #2778). The
      warning was in this window's startup log and was not acted on.
    - ci `matrix-admission-canary.yml` (manual only): the canary that qualified platform's gate.

    `scripts/gitea-workflow-check.sh 28.4.0`, written after the report, found exactly these two among 111 workflows
    in 21 repositories. Step 2 now runs it before the window.
  - **Cost:** 23 jobs were running at the stop. 8 reported again after the start and ran to their end, and newer
    pushes cancelled 2. The other 13, short jobs that ended during the outage, were failed as zombies at about 13:45.
    Their runs got `rerun-failed-jobs` once each had finished (ailab `main`: `rules-lint`, `script-tests`; platform
    `main`: `e2e-preflight`, `ci`, `contract`). `ci-rerun-watchdog` skipped them (`job_not_cloud`).
