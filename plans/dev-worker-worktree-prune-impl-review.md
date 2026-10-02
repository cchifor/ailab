# Implementation review — dev-worker-worktree-prune — round 1

<!-- codex-impl-review-status: complete -->

## Findings

### Restore the finalized disposable allowlist
**Location:** ansible/roles/dev_worker/files/cleanup:861
**Severity:** important
<!-- codex: The implementation adds deletion allowances absent from the finalized plan, including `out`, generated declaration filenames, and the broad `dist-` and `test-results-` prefixes, so an ignored `dist-backup/archive.db` can now be approved for removal. Restore the finalized allowlist and add negative tests for these additional names and prefixes. -->
<!-- opus-pushback: Partly accepted: the broad `dist-` prefix is gone (exact `dist-portable` only) and negative tests cover `dist-backup/`, `archive.log/`, `.env`, `local.db`, `secrets/`, bare `venv/`. The other additions are a deliberate, measured deviation, not drift: the plan's list kept 41 of dev-worker-1's 50 merged worktrees on `.coverage`, `.hypothesis/`, `out/`, `.failure-context/`, `test-results-*/`, `auto-imports.d.ts` (dry run 2026-10-02) — all test/build output those repos regenerate. `out` is a build dir name (the secret-holding dir in this estate is `_out`, which stays protected). -->

### File suffixes also authorize deleting entire directories
**Location:** ansible/roles/dev_worker/files/cleanup:1007
**Severity:** important
Fixed: file suffixes apply to a file entry only; `.egg-info` is the only directory suffix. Tested.

### Compare the full walk against the rule’s idle threshold
**Location:** ansible/roles/dev_worker/files/cleanup:1140
**Severity:** important
Fixed: `rule_cutoff()` at both call sites; the 40/35-day case is tested (and fails against the old code).

### Keep late planning operations inside the exception handler
**Location:** ansible/roles/dev_worker/files/cleanup:1144
**Severity:** important
Fixed: both inside the guarded block; vanish-after-inspect and symbolic-ref timeout are tested.

### Honor `--all` for worktree rows
**Location:** ansible/roles/dev_worker/files/cleanup:1216
**Severity:** important
<!-- codex: Worktree rows are always sliced to IMAGE_ROWS, so eleven eligible worktrees still produce only ten detailed rows with `--all`, despite the displayed instruction promising that flag lists the remainder. Respect show_all when selecting rows and computing the remainder, with a reporting test containing more than ten candidates. -->
<!-- opus-pushback: Not a bug: `display_rows` returns every plan entry, ungrouped, at its first line when `show_all` is set (`if show_all: return [...] for a in plan`), so the slicing is never reached with `--all`. Pinned by `test_all_lists_every_worktree_row` (12 actions → 12 rows with --all, 10 + "N more" without). -->

### Limit the custom-driver veto to the merged rule
**Location:** ansible/roles/dev_worker/files/cleanup:1122
**Severity:** nit
Fixed: the veto applies to the merged proof only; a pushed stale worktree with a driver configured is tested.

## Diff stat

 ansible/roles/dev_worker/defaults/main.yml |  11 +-
 ansible/roles/dev_worker/files/cleanup     | 483 ++++++++++++++++++++++++++---
 ansible/roles/dev_worker/tasks/cleanup.yml |  19 +-
 ansible/roles/dev_worker/tasks/docker.yml  |   9 +-
 docs/runbooks/dev-workers.md               |  35 ++-
 scripts/tests/test_dev_worker_cleanup.py   | 355 +++++++++++++++++++++
 6 files changed, 865 insertions(+), 47 deletions(-)