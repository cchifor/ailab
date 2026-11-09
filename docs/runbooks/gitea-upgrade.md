# Runbook: upgrading Gitea (forward-only migrations)

Every Gitea version bump runs database migrations that cannot be undone: an older Gitea refuses to start on a migrated
database. So an upgrade is a one-way door with a restore as the only way back. This runbook is the procedure. It was
written for 1.26.1 → 1.27.3 (chart 12.6.0 → 12.7.0, 2026-11-08); the plan and its review are in
`plans/2026-11-08-gitea-1-27-upgrade-plan.md`.

## What is in place

- **No rollback remediation.** `kubernetes/apps/apps/gitea/gitea.yaml` sets `upgrade.strategy.name: RetryOnFailure`:
  a failed or slow upgrade is retried and never rolled back.
- **Pinned image.** The release pins `image.tag`, so the chart's default appVersion never slips in unreviewed.
- **No native database backup.** infra-pg has no WAL archive or Barman, and Velero skips its data volumes. The upgrade
  backup is therefore a `pg_dump` taken during a write freeze, kept off-cluster.

## Procedure

Tools: `kubectl --context admin@ai`, `helm` 4.x, Docker. Owner-only API steps use `scripts/gitea-owner-op.sh`.

1. **Rehearse.**
   - Restore the latest dump into a throwaway `postgres:<major>` container (create role `gitea`;
     `pg_restore --exit-on-error`).
   - Run the target image's `gitea migrate` against it. Record the duration and the resulting
     `select version from version`; that is the acceptance value for step 6.
2. **Audit.** Check workflows for the release's breaking changes (for 1.27: cross-instance reusable workflows, by URL)
   in every repository with Actions, and custom templates under `/data/gitea/templates`.
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
   - **Running CI jobs fail.** Stopping Gitea fails every Actions job that is running at the time. The runners
     keep going, but Gitea marks those jobs failed as it comes back (2026-11-08: 18 jobs, 10 s after the start).
     Note them first, on the primary, database `gitea` (job status 6 is running, 5 waiting):

     ```sql
     select p.owner_name || '/' || p.name as repo, r.ref, r.index, r.id as run_id
     from action_run_job j join action_run r on r.id = j.run_id join repository p on p.id = r.repo_id
     where j.status = 6 order by repo, r.ref, r.index;
     ```

     After step 7, re-run the newest run of each ref:
     `scripts/gitea-api.sh POST /repos/<repo>/actions/runs/<run_id>/rerun` (1.27). A run that a newer run of the
     same ref superseded needs no re-run.
   - Run `kubectl -n gitea scale deploy/gitea --replicas=0`, then wait for `kubectl -n gitea get pods -l
     app.kubernetes.io/name=gitea` to list nothing. Gitea is the only writer to its database and volumes.
   - **Database.** Dump to the workstation and require both exit statuses to be 0:
     `kubectl -n databases exec <primary> -c postgres -- pg_dump -U postgres -Fc -d gitea >
     gitea-pre-<version>-<utc>.dump`, then `pg_restore -f /dev/null <file>` in a `postgres:<major>` container.
   - **Volumes.** Start a holder pod in `gitea` that mounts `gitea-shared-storage` and `gitea-actions-storage`
     (RWO, so only after the Gitea pod is gone). Create a Velero `Backup` named `gitea-pre-<version>` from the
     `velero-daily` template, restricted to `includedNamespaces: [gitea]`, with `defaultVolumesToFsBackup: true` and
     `ttl: 720h`. Wait for `Completed`, and for both `PodVolumeBackup`s (`-l velero.io/backup-name=gitea-pre-<version>`)
     to be `Completed`. Then delete the holder pod and wait until it is gone.
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
