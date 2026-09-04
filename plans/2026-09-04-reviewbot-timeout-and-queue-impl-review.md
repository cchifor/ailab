# Implementation review — reviewbot-timeout-and-queue — round 1

<!-- codex-impl-review-status: complete -->

## Findings

### Near-deadline failures bypass the timeout-attempt cap
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:393
**Severity:** blocker
**Resolved** in 65dae2cc. Confirmed real: a nonzero exit after ~880 s raised a plain
`RuntimeError`, which `next_failure_state` billed to the fast-failure budget — 5 further
attempts of nearly a full budget each. Failures consuming >= `llm_timeout_s / 3` now raise
`ExpensiveFailure`, and `is_budget_failure()` counts it against the deadline cap. Pinned by
`test_a_costly_nonzero_exit_is_classified_as_a_budget_failure` and, through the database, by
`test_an_expensive_nonzero_exit_counts_against_the_deadline_budget`.

### The wall-clock deadline does not cover the whole operation
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:344
**Severity:** important
**Partly resolved** in 65dae2cc: the clock starts at function entry (before the isolated-user
`mktemp`), `remaining()` raises `TimeoutExpired` on a non-positive remainder instead of
launching a run with `max(1.0, ...)`, and the auxiliary `sudo cat`/`rm` calls are bounded at
60 s.
<!-- codex: ... and add an outer attempt deadline if the limit is intended to cover the complete worker attempt. -->
<!-- opus-pushback: `llm_timeout_s` is documented as the LLM budget, not a whole-attempt SLA, and the rest of review_job is already bounded — every api() call carries timeout=60 and the diff fetch is size-capped. An outer watchdog would have to kill an in-flight job from another thread, which is a large amount of new failure surface for a case ReviewbotWorkerStuck already alerts on at 40 min. -->

### Best-effort telemetry can change or mask job outcomes
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:413; :818
**Severity:** important
**Resolved** in 65dae2cc. All three paths were real: a truthy non-mapping `usage` raised
`AttributeError` past the `(TypeError, ValueError)` guard, `record_gauge` in `finally` would
replace an in-flight `TimeoutExpired`, and a `bump_meta` failure would escape the worker's
exception handler and kill the only worker thread. Both helpers now swallow and log their own
failures; `usage` is type-checked. Pinned by
`test_telemetry_failure_cannot_mask_the_real_exception` (which breaks the database rather than
stubbing the helper — the swallowing is inside it) and `test_telemetry_never_propagates`.

### Quarantine retirement races with an in-flight old head
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:131; :708
**Severity:** important
**Resolved** in 65dae2cc. The race is real: head B enqueued while A is still `running` leaves
A out of enqueue's retire, and A's later give-up strands. Retirement now happens on the dedupe
path, when a later head completes, and via a reconciler sweep for PRs that are no longer open.
The sweep checks each quarantined row INDIVIDUALLY against the API rather than diffing against
the reconciler's open-PR listing — that listing is paginated at `limit=50`, so "absent from the
page" is not proof a PR is closed and would silently retire live quarantines. A transient API
error leaves the row alone: staying noisy is the safe failure.

### Requeue can double-post an ambiguous review
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:668; :861
**Severity:** important
**Resolved** in 65dae2cc — the finding is correct and my claim was too strong. The marker
pre-check makes a retry cheap, not idempotent: Gitea can commit the original POST after the
requeued worker's marker check and before its own. `--requeue` now refuses the `ambiguous POST`
class unless `--force` is passed, and the alert annotation and runbook text that asserted it was
"safe for both quarantine classes" are corrected.

### Schema migration runs on every connection
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:50
**Severity:** nit
**Partly resolved** in 65dae2cc: the `ALTER TABLE` tolerates a duplicate column, which is the
correctness half — `--requeue` is a second process and does not share `db_lock`. Left the
per-connection `PRAGMA table_info` in place: `db()` is already a per-call connection factory
following two `CREATE TABLE IF NOT EXISTS` statements, the file is local SQLite, and hoisting
initialisation out would restructure every existing call site for a cost that does not show up
against a worker that does one job per minute at most.

### Tests omit the worker and real concurrency paths
**Location:** scripts/tests/test_reviewbot.py:155
**Severity:** important
**Resolved** in 65dae2cc, and the criticism of the existing tests was correct: the fake
subprocess ignored the timeout it was handed (making the budget assertions vacuous) and
`assertRaises(Exception)` would have accepted an unrelated telemetry failure. `worker_once()`
is now factored out of the loop so one claim/run/persist cycle is testable end to end, and the
suite grew 24 -> 48: worker-level persistence, counters surviving a later success, the
stale-quarantine race, a ticker/worker concurrency test, the quarantine sweep, and the CLI exit
codes. The fake runner now raises `TimeoutExpired` when it exceeds its own timeout.

### The promtool test wrapper is not fully fail-closed
**Location:** scripts/rules-lint.sh:113
**Severity:** important
**Resolved** in 65dae2cc: every `rule_files:` entry a fixture names must now exist among the
extracted specs, or the gate exits 1. Verified both ways — a typo'd reference is rejected and
the real fixture is accepted. One correction to the finding's premise: the vacuous-pass hole is
latent rather than active here, because this fixture carries positive assertions, so promtool
alone already exits 1 on an unmatched reference (measured). The check protects future
negative-only fixtures.

### Requeue argument failures have inconsistent exit behavior
**Location:** ansible/roles/pr_reviewer/files/reviewbot.py:886
**Severity:** nit
**Resolved** in 65dae2cc. The unknown-option case was the serious half — it fell through and
STARTED THE DAEMON. Explicit parsing now returns 2 for an unknown option, wrong arity, or a
non-integer PR, and `CommandLineTest` covers all of them plus the `--force` behaviour.

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
