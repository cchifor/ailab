# Implementation review — dev-worker-cleanup-gc-reflog-and-link-guard — round 1

<!-- codex-impl-review-status: finalized -->

All 12 findings were accepted and fixed in `c3803d53`. Each regression test below fails against
the pre-review implementation (`4b3ff63a`) and passes now. 231 tests pass, both as root and as
uid 1000.

## Findings

### Slow inventory refreshes invalidate the preceding deletion checks
**Location:** ansible/roles/dev_worker/files/cleanup (remove_deps, remove_worktree)
**Severity:** blocker
**Resolution:**
- A removal now calls `Inventory.ready()` BEFORE its safety checks (activity, contents, processes,
  mounts, git state). That call rebuilds the inventory when it is older than 120 s.
- Right before deleting, the removal calls `recheck()` against that same index and never rebuilds
  there.
- An index that aged past 120 s during the checks refuses the removal ("link inventory aged during
  the safety checks"), and the next run retries it.
- Tests: `test_recheck_never_rebuilds_and_refuses_an_index_that_aged_during_the_checks`,
  `test_a_link_made_after_the_scan_is_caught_at_the_removal`,
  `test_a_link_made_after_the_scan_stops_the_worktree_removal`.

### Nested consumers beyond discovery limits are treated as internal links
**Location:** cleanup (owner, link_index)
**Severity:** blocker
**Resolution:**
- Ownership now comes from the inventory's own walk: every dir that holds a `.git`, at any depth.
- `find_worktrees` no longer decides it.
- Test: `test_a_nested_consumer_deeper_than_discovery_is_still_another_tree`, run through `main()`.

### The inode check does not pin the directory that is deleted
**Location:** cleanup (remove_dep_dir)
**Severity:** important
**Resolution:**
- After the rename, the dir now under the trash name is checked against the planned
  `(st_dev, st_ino)`. Anything else stays where it is and is reported; nothing is deleted.
- The trash name has a random suffix.
- Test: `test_a_dir_replaced_between_the_check_and_the_rename_is_not_deleted`.

### Internal-link exemptions can break externally referenced subtrees
**Location:** cleanup (linked_from)
**Severity:** important
**Resolution:**
- A worktree's own link stops being exempt once another tree's link reaches the part of the
  worktree it sits in. The per-worktree "exposed" set is cached on the index.
- This corrects the plan's internal-link assumption.
- Test: `test_a_worktrees_own_link_counts_when_another_tree_reaches_its_part`.

### Unhandled inventory errors bypass the sticky failure state
**Location:** cleanup (link_index, Inventory.get)
**Severity:** important
**Resolution:**
- The child and root `fstat` calls are handled as inventory errors.
- `Inventory.get` turns a raising walk into an incomplete index.
- The first incomplete index latches `Inventory.failed`.
- Test: `test_a_walk_that_raises_is_an_incomplete_inventory`.

### Reporting crashes when the fresh scan needs no inventory
**Location:** cleanup (main.report)
**Severity:** important
**Resolution:**
- The report reads `links_seen`, `seconds` and `walks`, which are kept across `reset()`, and no
  longer the current index.
- Test: `test_a_run_whose_fresh_scan_needs_no_inventory_still_reports`, a two-scan run through
  `main()`.

### Execution-time link refusals disappear from the final report
**Location:** cleanup (Inventory.recheck, main.report)
**Severity:** important
**Resolution:**
- `recheck()` records every refusal in `at_removal`.
- The last line now reads "… at planning, N at removal (e.g. …)".
- The incomplete-inventory message no longer claims nothing was removed. It now reads "every
  --deps/--worktrees removal from then on was refused".

### Inventory failures are forgotten across scans and bypassed by early returns
**Location:** cleanup (Inventory.reset, main)
**Severity:** important
**Resolution:**
- `failed` is the run's: `reset()` keeps it, and every later answer is that refusal.
- The abort and no-terminal exits now pass through `report()`.
- Test: `test_an_incomplete_refresh_refuses_every_later_removal`.

### An aligned tail window drops a complete reflog record
**Location:** cleanup (reflog_time)
**Severity:** important
**Resolution:**
- The read takes one byte before the window. The first record is dropped only when that byte is
  not a newline.
- Test: `test_a_window_that_starts_exactly_on_a_record_keeps_it`.

### Destructive-boundary coverage is incomplete
**Location:** scripts/tests/test_dev_worker_cleanup.py
**Severity:** important
**Resolution:** added tests for:
- the `remove_worktree` removal-time link
  (`test_a_link_made_after_the_scan_stops_the_worktree_removal`);
- the multi-dir deps action skipped whole (`test_one_newly_linked_dir_skips_its_whole_deps_action`);
- an incomplete refresh after an earlier successful removal
  (`test_an_incomplete_refresh_after_a_removal_stops_the_rest`).

### Non-root permission handling differs between walking and resolving
**Location:** cleanup (traverse, link_index)
**Severity:** nit
**Resolution:**
- `traverse` returns the `OSError` itself.
- `link_index` applies the same strict and non-strict rule to resolution errors as to walk errors.
- Test: `test_a_permission_error_resolving_a_link_is_skipped_only_when_not_strict`.

### Worktree inventories are built before full eligibility is established
**Location:** cleanup (plan_worktrees)
**Severity:** nit
**Resolution:**
- The link check runs only after `inspect_worktree` passes.
- Test: `test_a_worktree_that_fails_inspection_never_costs_an_inventory`.

## Diff stat

 ansible/roles/dev_worker/files/cleanup   | 489 ++++++++++++++++++++++++++---
 docs/runbooks/dev-workers.md             |  22 ++
 scripts/tests/test_dev_worker_cleanup.py | 509 +++++++++++++++++++++++++++++++
 3 files changed, 980 insertions(+), 40 deletions(-)
