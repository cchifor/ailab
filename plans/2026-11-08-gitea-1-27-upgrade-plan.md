# Upgrade Gitea 1.26.1 → 1.27.3 (chart 12.6.0 → 12.7.0)

Status: finalized · Created: 2026-11-08 · Owner approval: "implement the Still open items" (2026-11-08)

## Context

GHSA-w5pg-649r-p6gg (stale "official" approvals survive PR retargeting) affects Gitea ≤ 1.26.4 and is fixed in 1.27.0.
Live: chart `gitea` 12.6.0 → image `docker.gitea.com/gitea:1.26.1-rootless`, database `gitea` on CNPG `infra-pg`
(6.5 GB, migration version 331), PVCs `gitea-shared-storage` (data) and `gitea-actions-storage` on qnap-iscsi,
Recreate strategy, HR interval 1h, `upgrade.remediation.retries: 3` (strategy defaults to rollback).

Findings (sources in the research notes):

- **Chart.** 12.7.0 is the only chart that ships 1.27.x (appVersion 1.27.0). A render with our values differs only
  in labels, the image and the config checksum.
- **Patch level.** 1.27.0 has GHSA-rcr6-4jqh-j84m (critical, fixed in 1.27.1), so the image is pinned to `1.27.3`.
  The release does not set `image.tag` today; the chart appends `-rootless`.
- **Breaking changes in 1.27.** Reusable workflows are expanded server-side, and cross-instance URL calls are
  dropped; none were found in the local checkouts (cchifor/platform is not checked). Inline scripts need a CSP nonce;
  we have no custom templates. `[webhook] ALLOWED_HOST_LIST` is deprecated but still honoured; moving it is deferred
  to the 28.x upgrade.
- **Migrations.** 331 → 343, forward-only. A 1.26.1 binary refuses to start on a migrated database.
- **API.** No API paths or methods are removed (our scripts are unaffected).
- **Runners.** v0.6.1 runners stay compatible.
- **No automatic rollback.** helm-controller v1.5.5 offers only `rollback`/`uninstall`. With `retries: 3` a failed
  upgrade is rolled back, which after migrations takes the forge down. The upgrade must not remediate.

## Approach (revised after codex round 1)

The procedure is `docs/runbooks/gitea-upgrade.md` (new). Its main points, and how each round-1 finding is handled:

1. **Remediation (codex 6).** Use `upgrade.strategy.name: RetryOnFailure` (`retryInterval: 10m`), checked against the
   live CRD with a server-side dry run, instead of `remediation.retries`. It never rolls back and does not wedge.
2. **Rehearsal (codex 4, 5, 6), done.** Today's dump restored into `postgres:17` with role `gitea` and grants
   (`pg_restore --exit-on-error -j 4`) in 132 s. `gitea:1.27.3-rootless` `gitea migrate` took version 331 → **343** in
   3 s, so the acceptance value is 343 and the 15-minute Helm timeout is not at risk.
3. **One consistent state (codex 1).**
   - Annotate the HR `reconcile=disabled` and set `spec.suspend: true`, then merge and confirm the GitHub mirror.
   - Scale Gitea to 0 (write freeze), then take the DB dump and a Velero fs-backup of both PVCs through a holder pod.
     Both PodVolumeBackups must be Completed.
   - Then remove the annotation and unsuspend.
4. **Rollback (codex 2, 3).** Freeze both controllers and wait for no pending Helm action. Restore the DB as owner
   `gitea` with grants. Reinstall from staged `helm get values` plus an explicit `image.tag=1.26.1`, rendered and
   inspected first; never `--reuse-values`.
5. **Verification (codex 7).**
   - Version 1.27.3 and DB version 343; the HR applied revision matches the merge SHA.
   - API, git clone and push, a CI run on a scratch branch, GitHub mirroring of a merge.
   - The owner's real OIDC login, a reviewer-bot review, the credential-inventory metrics.
   - The platform workflow audit: only local composite actions (`./.github/actions/...`), no cross-instance
     reusable workflows.
6. **Target (codex 8).** 1.27.3 now; the owner asked for ≥ 1.27.0 and only chart 12.7.0 supports 1.27. On the
   advisories:
   - GHSA-8pwg-hcgm-54cp excludes PostgreSQL installs, so it does not apply here.
   - GHSA-pvjf-7jrm-6w9g needs an untrusted fork PR; estate accounts are SSO-gated.
   - 28.x is the dated follow-up (by 2026-12-08), rehearsed the same way, with the `[webhook] ALLOWED_HOST_LIST` →
     `[security]` move and a check of the RUN_RETENTION_DAYS interplay.


## Review

Two codex rounds. Round 1: 4 blockers and 4 important findings, all folded in. Round 2: six of those resolved; it
accepted the pushback on a full isolated restore of both volumes (deferred, with a concrete restore procedure
required), and raised sequencing fixes to the runbook (pending Helm action, Flux field manager for suspend, async
scale-down and DB sessions, replicaCount=0 during a restore, the volume-restore commands, unsuspend only after the
revert is live, pg_restore from a file, the revision check on GitRepository). All of them are applied in
`docs/runbooks/gitea-upgrade.md`.

<!-- codex-review-status: finalized -->
