# Implementation review — reviewbot-timeout-and-queue — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The implementation broadly follows the plan, and the current `bump_meta` / `record_gauge` call sites do not recursively acquire the plain `db_lock`.
- The central queue bound is not guaranteed: expensive nonzero exits can receive five full attempts, and several paths remain outside the wall-clock deadline.
- Quarantine retirement has a race, unresolved quarantines eventually become invisible, and requeuing an ambiguous POST can still duplicate a review.
- Important worker/concurrency behavior is not tested, and the promtool wrapper is not fully fail-closed.
- No clear new remote-authentication, SQL-injection, or secret-handling vulnerability was found.

## Findings

### Near-deadline failures bypass the timeout-attempt cap
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:393
**Severity:** blocker
<!-- codex: When the primary exits nonzero after consuming all but `llm_fallback_min_s`, fallback is skipped and a `RuntimeError` leaves `timeout_attempts` unchanged, permitting up to five nearly 900-second runs instead of the claimed two-budget maximum. Classify budget-consuming failures as deadline failures, or apply the lower cap based on elapsed duration as well as `TimeoutExpired`. -->

### The wall-clock deadline does not cover the whole operation
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:344
**Severity:** important
<!-- codex: The deadline starts after isolated-directory setup, while codex output/auth reads and cleanup are untimed; `max(1.0, remainder)` also launches the primary after an already-expired budget, and `review_job()` adds separately timed API work. Start the clock at entry, reject non-positive remainders, bound every child process, and add an outer attempt deadline if the limit is intended to cover the complete worker attempt. -->

### Best-effort telemetry can change or mask job outcomes
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:413; ansible/roles/pr_reviewer/files/reviewbot.py:818
**Severity:** important
<!-- codex: A truthy non-mapping `usage` raises an uncaught `AttributeError`, and a `record_gauge()` failure in `finally` replaces an active `TimeoutExpired`; a subsequent `bump_meta()` failure then escapes the worker exception handler and kills the sole worker thread. Type-check the envelope and make all telemetry writes catch/log failures without replacing the original job result or exception. -->

### Quarantine retirement races with an in-flight old head
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:131; ansible/roles/pr_reviewer/files/reviewbot.py:708
**Severity:** important
<!-- codex: If head B is queued while head A is running and A subsequently times out, A becomes quarantined after B's enqueue already attempted retirement; later B deduplication returns before retiring A, leaving a stale quarantine indefinitely. Retire quarantines for other heads even on the dedupe path, and explicitly retire closed PRs so unresolved current-head quarantines can remain alerted instead of silently aging out after 24 hours. -->

### Requeue can double-post an ambiguous review
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:668; ansible/roles/pr_reviewer/files/reviewbot.py:861
**Severity:** important
<!-- codex: After a client-side POST timeout, Gitea may still commit the original request after the requeued worker's marker check but before its second POST, so the preflight check does not make ambiguous-POST recovery idempotent. Keep this quarantine class manual until the request has conclusively settled, or use a server-side idempotency mechanism before allowing it to re-enter the queue. -->

### Schema migration runs on every connection
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:50
**Severity:** nit
<!-- codex: `db_lock` serializes the four service threads, but every connection still performs two schema statements plus `PRAGMA table_info`, and the standalone requeue process does not share that lock; two processes can both observe the missing column and the second `ALTER TABLE` then fails. Run initialization and migration once before starting threads, using a transaction or duplicate-column-safe retry, and keep `db()` as a connection factory. -->

### Tests omit the worker and real concurrency paths
**Location:** scripts/tests/test_reviewbot.py:155
**Severity:** important
<!-- codex: The finalized plan promised concurrent ticker/worker access and worker-level persistence, but the suite never runs a worker step, ticker, concurrent migration, or the CLI entry point; consequently it does not prove timeout counters survive a later success or expose the quarantine retirement race. The fake subprocess also ignores its supplied timeout and `assertRaises(Exception)` accepts unrelated telemetry failures, so add an enforceable fake runner, exact exception assertions, and integration tests around a factored single worker iteration. -->

### The promtool test wrapper is not fully fail-closed
**Location:** scripts/rules-lint.sh:113
**Severity:** important
<!-- codex: The unmatched fixture glob is handled, but Prometheus v3.5 merely warns when a fixture's `rule_files` pattern matches nothing, allowing a negative-only fixture to pass without loading any extracted rule. Validate every referenced basename against the extracted files, or make the warning and a zero-group load fatal. -->

### Requeue argument failures have inconsistent exit behavior
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:886
**Severity:** nit
<!-- codex: A non-integer PR raises a traceback with exit 1, while an unknown mode falls through and starts the daemon; the tests call `requeue()` directly and therefore miss both behaviors. Use explicit argument parsing and test successful requeue, no match, malformed PR, wrong arity, and unknown-command exit codes. -->

## Diff stat

 ansible/roles/pr_reviewer/defaults/main.yml        |  19 +-
 ansible/roles/pr_reviewer/files/reviewbot.py       | 261 ++++++++++++--
 ansible/roles/pr_reviewer/templates/config.json.j2 |   2 +
 docs/runbooks/dev-workers.md                       |  42 ++-
 .../infrastructure/monitoring/kustomization.yaml   |   1 +
 .../monitoring/reviewbot-rules.test.yaml           | 252 +++++++++++++
 .../infrastructure/monitoring/reviewbot-rules.yaml | 155 ++++++++
 scripts/rules-lint.sh                              |  25 ++
 scripts/tests/test_reviewbot.py                    | 388 +++++++++++++++++++++
 9 files changed, 1111 insertions(+), 34 deletions(-)
