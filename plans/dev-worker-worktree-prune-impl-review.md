# Implementation review — dev-worker-worktree-prune — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Owner privilege drop, execution rechecks, and report-first scheduling largely follow the plan.
- Two disposable-content classification issues permit deletion of content the finalized plan protects.
- The full-walk cutoff incorrectly rejects idle candidates, and `--all` still truncates worktree reports.
- Planning has an unhandled disappearance/timeout window; the supplied tests miss these cases.

## Findings

### Restore the finalized disposable allowlist
**Location:** ansible/roles/dev_worker/files/cleanup:861
**Severity:** important
<!-- codex: The implementation adds deletion allowances absent from the finalized plan, including `out`, generated declaration filenames, and the broad `dist-` and `test-results-` prefixes, so an ignored `dist-backup/archive.db` can now be approved for removal. Restore the finalized allowlist and add negative tests for these additional names and prefixes. -->

### File suffixes also authorize deleting entire directories
**Location:** ansible/roles/dev_worker/files/cleanup:1007
**Severity:** important
<!-- codex: Applying `.pyc`, `.tsbuildinfo`, and `.log` suffixes to every path component makes an ignored `archive.log/` containing `notes.db` disposable, although the plan permits those suffixes only for files. Separate file-suffix checks from directory/component allowances and test that such a directory keeps the worktree. -->

### Compare the full walk against the rule’s idle threshold
**Location:** ansible/roles/dev_worker/files/cleanup:1140
**Severity:** important
<!-- codex: Both planning and removal pass `w.last_activity` as the full-walk cutoff instead of the selected rule’s threshold: if the shallow scan reports activity 40 days ago and a node_modules file was modified in place 35 days ago, the worktree is incorrectly kept despite satisfying the 30-day stale rule. Pass `now - timedelta(days=merged_days if rule == "merged" else days)` at both call sites and add this old-but-newer dependency-file case. -->

### Keep late planning operations inside the exception handler
**Location:** ansible/roles/dev_worker/files/cleanup:1144
**Severity:** important
<!-- codex: The fingerprint lstat and branch lookup occur outside the per-worktree try block, so a concurrent removal after inspection raises FileNotFoundError, or a branch-lookup timeout raises TimeoutExpired, and aborts the entire scan instead of handling that worktree; main catches neither exception. Include these operations in the guarded block and test disappearance and timeout after inspection. -->

### Honor `--all` for worktree rows
**Location:** ansible/roles/dev_worker/files/cleanup:1216
**Severity:** important
<!-- codex: Worktree rows are always sliced to IMAGE_ROWS, so eleven eligible worktrees still produce only ten detailed rows with `--all`, despite the displayed instruction promising that flag lists the remainder. Respect show_all when selecting rows and computing the remainder, with a reporting test containing more than ten candidates. -->

### Limit the custom-driver veto to the merged rule
**Location:** ansible/roles/dev_worker/files/cleanup:1122
**Severity:** nit
<!-- codex: A clean 40-day-old worktree whose HEAD is on origin/feature is rejected solely because a custom merge driver exists, although the finalized stale rule uses containment and does not invoke that driver. Keep the driver veto on the merged proof, allow the independent stale check, and test a pushed stale worktree with a configured driver. -->

## Diff stat

 ansible/roles/dev_worker/defaults/main.yml |  11 +-
 ansible/roles/dev_worker/files/cleanup     | 483 ++++++++++++++++++++++++++---
 ansible/roles/dev_worker/tasks/cleanup.yml |  19 +-
 ansible/roles/dev_worker/tasks/docker.yml  |   9 +-
 docs/runbooks/dev-workers.md               |  35 ++-
 scripts/tests/test_dev_worker_cleanup.py   | 355 +++++++++++++++++++++
 6 files changed, 865 insertions(+), 47 deletions(-)