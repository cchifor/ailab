# Implementation review — dev-worker-cleanup-gc-reflog-and-link-guard — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The implementation broadly follows the finalized plan and stays within its intended scope. Leaving disk-guard unchanged is justified by its existing failure classification and test.
- Approval is blocked by incomplete nested-worktree ownership information and slow inventory refreshes invalidating the safety checks immediately preceding deletion.
- Inventory failure handling and final reporting have several gaps, including a reproducible crash and missing execution-time refusal details.
- Tests cover many planned cases but miss important destructive-boundary scenarios. Local verification passed 38 tests and skipped 95 Linux-only tests; Linux, real-git integration, CI and canary results were not independently verified here.

## Findings

### Slow inventory refreshes invalidate the preceding deletion checks
**Location:** ansible/roles/dev_worker/files/cleanup:953, ansible/roles/dev_worker/files/cleanup:1597
**Severity:** blocker
<!-- codex: Both removal functions finish their activity, contents, process and mount checks before recheck(), which can spend minutes rebuilding the inventory; a checkout added inside node_modules or a valuable ignored file added to a worktree during that interval can consequently be deleted, and git worktree remove does not protect ignored files. Refresh the inventory before the final safety checks, then perform a cached link check immediately before deletion, restarting validation if another refresh becomes necessary; add mutation-during-refresh regressions for both removal paths. -->

### Nested consumers beyond discovery limits are treated as internal links
**Location:** ansible/roles/dev_worker/files/cleanup:1962, ansible/roles/dev_worker/files/cleanup:1179
**Severity:** blocker
<!-- codex: Inventory.worktrees comes from find_worktrees(), whose depth-six limit and skipped directory names omit legitimate nested worktrees even though the link inventory walks their links; owner() then returns the enclosing candidate worktree and exempts those consumers, allowing deletion of their shared dependencies. Discover ownership boundaries during the full inventory walk, or conservatively withhold the internal-link exemption when ownership is unproven, and test a nested consumer beyond DISCOVERY_DEPTH through main() rather than manually supplying its owner. -->

### The inode check does not pin the directory that is deleted
**Location:** ansible/roles/dev_worker/files/cleanup:923
**Severity:** important
<!-- codex: This pre-existing root deletion race remains outside the plan's safeguards: another process can replace name with a different real directory between os.stat() and os.rename(), after which rmtree deletes the replacement without comparing it to the planned inode; descriptor-relative access and O_NOFOLLOW do not prevent directory replacement. Move into protected staging and verify the moved inode before recursion, avoiding an attacker-writable trash pathname, and test replacement specifically between the identity check and rename. -->

### Internal-link exemptions can break externally referenced subtrees
**Location:** ansible/roles/dev_worker/files/cleanup:1179
**Severity:** important
<!-- codex: The planned predicate misses an indirect consumer such as C/pkg -> W/packages/p together with W/packages/p/node_modules -> W/node_modules: the external link does not reach the candidate directly, the internal link is exempted, and deleting W/node_modules breaks C/pkg/node_modules despite both links being inventoried. Extend protection through internal links reachable beneath externally referenced subtrees, or conservatively retain the affected dependencies, and add this regression as a correction to the plan's internal-link assumption. -->

### Unhandled inventory errors bypass the sticky failure state
**Location:** ansible/roles/dev_worker/files/cleanup:1123, ansible/roles/dev_worker/files/cleanup:1217
**Severity:** important
<!-- codex: The root and child fstat calls can raise outside the inventory error handlers, leaving Inventory.get() with its previous complete index and no poisoned state; a read-only injected-error probe confirmed that a later successful recheck permits removal after such a failure. Convert these errors into an incomplete inventory and latch that failure before returning to callers, with tests covering root and child fstat failures during planning and refresh. -->

### Reporting crashes when the fresh scan needs no inventory
**Location:** ansible/roles/dev_worker/files/cleanup:1982
**Severity:** important
<!-- codex: reset() clears inventory.index but preserves inventory.walks, so an initial eligible candidate followed by a fresh scan with no eligible candidates reaches len(index.links) with index=None; this was reproduced through main(), turning a legitimate skip into an AttributeError and losing the final summary. Preserve separate reporting metadata for the last completed walk, or handle an absent current index explicitly, and add a two-scan CLI regression. -->

### Execution-time link refusals disappear from the final report
**Location:** ansible/roles/dev_worker/files/cleanup:1986
**Severity:** important
<!-- codex: The report counts only refused and kept from planning, while remove_deps/remove_worktree discard recheck reasons, so newly discovered links can produce a final count of zero and an incomplete refresh loses its error path and cause; the accompanying “removed nothing” assertion is also false when earlier actions succeeded. Accumulate execution-time refusals and inventory diagnostics, report actual partial progress, and emit the linked summary last across the output retained by disk-guard. -->

### Inventory failures are forgotten across scans and bypassed by early returns
**Location:** ansible/roles/dev_worker/files/cleanup:1208, ansible/roles/dev_worker/files/cleanup:2024
**Severity:** important
<!-- codex: reset() clears an earlier incomplete inventory, allowing a mixed Docker/walk run to encounter an inventory failure, complete a healthy second scan and exit zero, while abort and other early-return paths bypass report() entirely. Keep a run-wide failure latch separate from per-scan state and route post-scan exits through a common finalizer so an observed incomplete inventory consistently produces exit 1 and the required reporting. -->

### An aligned tail window drops a complete reflog record
**Location:** ansible/roles/dev_worker/files/cleanup:622
**Severity:** important
<!-- codex: Every nonzero starting offset discards the first record, including when the 64 KiB window begins exactly at a record boundary; a read-only probe confirmed that a newer complete first record followed by older entries is incorrectly ignored, which is outside the documented limitation concerning entries before the window. Check whether the starting offset actually cuts a record before discarding it, and add an exact-boundary regression with nonmonotonic timestamps. -->

### Destructive-boundary coverage is incomplete
**Location:** scripts/tests/test_dev_worker_cleanup.py:1575
**Severity:** important
<!-- codex: The new execution tests exercise a single dependency directory and Inventory helpers, but do not exercise removal-time link protection through remove_worktree or the explicitly planned still_planned case where one newly linked directory skips an entire multi-directory dependency action. Add both integration cases, including an incomplete refresh after an earlier successful removal and assertions covering filesystem preservation, exit status and final diagnostics. -->

### Non-root permission handling differs between walking and resolving
**Location:** ansible/roles/dev_worker/files/cleanup:1143
**Severity:** nit
<!-- codex: strict=False suppresses PermissionError during the directory walk, but traverse() converts resolution errors to strings and link_index() unconditionally records them, so an accessible symlink into an unreadable home can invalidate the entire non-root dry run. Preserve error types or errno through resolution and apply the same permission exception in both phases, with a non-root regression. -->

### Worktree inventories are built before full eligibility is established
**Location:** ansible/roles/dev_worker/files/cleanup:1558
**Severity:** nit
<!-- codex: plan_worktrees checks links before inspect_worktree, so a tree containing recently modified dependencies, another checkout or otherwise disallowed contents can trigger an expensive inventory and be reported as link-protected despite never being removable. Move the link check after successful inspection to match the plan's lazy, otherwise-eligible-only contract and test that inspection failures do not build an inventory. -->

## Diff stat

 ansible/roles/dev_worker/files/cleanup   | 489 ++++++++++++++++++++++++++---
 docs/runbooks/dev-workers.md             |  22 ++
 scripts/tests/test_dev_worker_cleanup.py | 509 +++++++++++++++++++++++++++++++
 3 files changed, 980 insertions(+), 40 deletions(-)