# Implementation review — reviewbot-timeout-and-queue — rounds 1 and 2

<!-- codex-impl-review-status: finalized -->

Two rounds, 15 findings, all resolved. Codex withdrew nothing from round 1; it accepted the
single pushback in round 2 ("The pushback is fair that API work outside `run_llm()` does not
require an outer worker watchdog") and raised six further findings against the round-1 fixes,
including a second blocker. Fixes landed in 65dae2cc, 1c5495e9 and d4b91f69.

## Round 1

### Near-deadline failures bypass the timeout-attempt cap — blocker
A nonzero exit after ~880 s raised a plain `RuntimeError`, billed to the fast-failure budget:
5 further attempts of nearly a full budget each. Failures consuming >= `llm_timeout_s / 3` now
raise `ExpensiveFailure` and count against the deadline cap.

### The wall-clock deadline does not cover the whole operation — important
Clock moved to function entry (before the isolated-user `mktemp`); `remaining()` raises
`TimeoutExpired` on a non-positive remainder rather than launching a run with `max(1.0, ...)`;
the auxiliary `sudo cat`/`rm` calls are bounded. Codex accepted the pushback on an outer
whole-attempt watchdog: `llm_timeout_s` is the LLM budget, the rest of `review_job` is already
bounded (`api()` carries `timeout=60`, the diff is size-capped), and `ReviewbotWorkerStuck`
alerts at 40 min on a wedge.

### Best-effort telemetry can change or mask job outcomes — important
All three paths were real. `bump_meta`/`record_gauge` now swallow and log their own failures
(one runs in a `finally`, the other inside the worker's exception handler, where a raise would
have killed the only worker thread); a non-mapping `usage` is type-checked.

### Quarantine retirement races with an in-flight old head — important
Retirement added on the dedupe path, on completion of a later head, and via a reconciler sweep
for PRs no longer open — the sweep checking each row individually against the API, because the
reconciler's open-PR listing is paginated and "absent from the page" is not proof of closure.

### Requeue can double-post an ambiguous review — important
Correct, and my "safe for both classes" claim was wrong. The marker pre-check makes a retry
cheap, not idempotent. `--requeue` now refuses the `ambiguous POST` class without `--force`,
and the alert annotation and runbook are corrected.

### Schema migration runs on every connection — nit
`ALTER TABLE` tolerates a duplicate column (`--requeue` is a second process and does not share
`db_lock`). The per-connection `PRAGMA` is kept: `db()` is already a per-call factory after two
`CREATE TABLE IF NOT EXISTS` statements, against local SQLite, for a worker doing at most one
job a minute.

### Tests omit the worker and real concurrency paths — important
Correct, including that the fake subprocess ignored its own timeout (making the budget
assertions vacuous) and that `assertRaises(Exception)` would accept an unrelated failure.
`worker_once()` factored out; suite 24 -> 48.

### The promtool test wrapper is not fully fail-closed — important
Reference validation added. One correction to the premise: the vacuous-pass hole was latent
here, since this fixture carries positive assertions and promtool already exits 1 on an
unmatched reference (measured). The check protects future negative-only fixtures.

### Requeue argument failures have inconsistent exit behavior — nit
The serious half was that an unknown option fell through and STARTED THE DAEMON. Explicit
parsing, exit 2 for unknown option / wrong arity / non-integer PR, all covered by tests.

## Round 2

### Costly failures still bypass the timeout budget — blocker
Correct and important: cost was classified only at the nonzero-exit site, but `run_llm` has
five other raise paths (isolated tmpdir, codex-no-output, credential scan, "no JSON object",
"missing summary/findings"). A run that burned the budget and then failed on malformed output
was billed cheap and got five more full-length retries. `run_llm` is now a wrapper that
re-raises ANY failure over the cost threshold as `ExpensiveFailure`.

On the suggested cumulative per-job LLM-time budget: not taken. The bound is enforced by the
two attempt counters, and the honest figure — independently enumerated through the real
`next_failure_state` — is **45 min** per head (`b,f,f,f,b` = 3 x 300 s + 2 x 900 s), not the
30 min the plan first claimed. That error was corrected in 1c5495e9 rather than papered over.
A cumulative budget would tighten 45 -> 30 and is the right next step if that matters; it needs
another column and the elapsed time plumbed back to the worker, which was not worth doing
unreviewed at the round cap.

### Auxiliary timeouts can replace a real LLM outcome — important
The `finally` cleanup is now strictly non-propagating: `except OSError` did not cover the
`TimeoutExpired` the 60 s `rm -rf` can raise, and an exception escaping a `finally` replaces a
successful review or the true failure — and would then be misreported as an exhausted deadline.
The isolated `mktemp` cleans up its workdir instead of leaking one per failed attempt.

### Stale observations can retire a live quarantine — important
`enqueue()` retires OLDER heads only, by `created`: it also runs for a DELAYED webhook carrying
a stale head, which must not clear the current head's live quarantine. The sweep demands an
exact `state == "closed"` rather than `!= "open"`. And an `ambiguous POST` quarantine is never
auto-retired on any path.

### Requeue's force check and update are not atomic — important
Inspect and update now happen in one transaction, and the UPDATE names the exact ids shown to
the operator; rows that changed state concurrently are reported rather than claimed as
requeued.

### The concurrency test can pass after every metrics write fails — important
Correct: `write_metrics()` swallows its own exceptions, so "nothing escaped" proved nothing,
and the test drove real network calls. It now stubs `review_job`, forces overlap with a
barrier, and asserts the emitted metrics file, the counters, the job count and
`PRAGMA integrity_check`.

### Rule-file validation still fails open when it parses no references — important
Correct: the regex required a following top-level key, so `rule_files:` at EOF parsed zero
references and passed. The parser moved to `scripts/promtest-refs.py` (stdlib, 11 unit tests),
matches to EOF, and exits non-zero unless it parses a nonempty list. Writing it as a real
script also removed the heredoc escaping that had put a **raw CR byte** into `rules-lint.sh` —
which `.gitattributes` (`*.sh eol=lf`) would strip on checkout, silently turning the guard into
a no-op.

## Diff stat

 ansible/roles/pr_reviewer/defaults/main.yml        |  19 +-
 ansible/roles/pr_reviewer/files/reviewbot.py       | 439 +++++++++++--
 ansible/roles/pr_reviewer/templates/config.json.j2 |   2 +
 docs/runbooks/dev-workers.md                       |  48 +-
 .../infrastructure/monitoring/kustomization.yaml   |   1 +
 .../monitoring/reviewbot-rules.test.yaml           | 252 ++++++++
 .../infrastructure/monitoring/reviewbot-rules.yaml | 155 +++++
 scripts/promtest-refs.py                           |  48 ++
 scripts/rules-lint.sh                              |  52 ++
 scripts/tests/test_promtest_refs.py                |  85 +++
 scripts/tests/test_reviewbot.py                    | 702 +++++++++++++++++++++
