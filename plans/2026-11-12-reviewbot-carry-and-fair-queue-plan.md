# reviewbot: carry a clean verdict across base-only updates, and a fair queue

## Context

Measured 2026-11-12 over the last 24 h on both reviewer hosts:

- Each persona got 575 jobs for 180 PRs (3.2 heads per PR). `cchifor/relay` is 84 % of them: one
  agent (dev-worker-3) holds ~105 open stacked PRs.
- reviewer-2 (codex) keeps up, p90 enqueue->done 1 min. reviewer-1 (claude) is serial and
  quota-bound: p90 257 min. platform#2748, another agent's priority PR, waited behind 30 relay jobs.
- relay, llm-router and workbricks `main` have `block_on_outdated_branch=true` and
  `dismiss_stale_approvals=true`. With ~45 relay merges/day sibling PRs go behind base (the bots
  logged `405 The head branch is behind the base branch` 723 times on 14 PRs), get updated by
  merging main in (a new head, the PR's own changes untouched), and then BOTH personas re-review the
  new head with an LLM, from scratch, to reach the same verdict.

Two independent changes, each behind its own flag (off in code, on in the role defaults):

- **A. Carry**: a new head whose PR diff is byte-identical to the diff of the head this persona
  last reviewed clean gets this persona's clean review again WITHOUT an LLM run.
- **B. Fair queue**: the single worker picks the least-recently-served repository among those with
  an eligible job, then the oldest eligible job in it, instead of the globally oldest job.

## Approach

All code is in `ansible/roles/pr_reviewer/files/reviewbot.py`.

### A. Carry a clean verdict

**Schema** (`db()`, same guarded `ALTER TABLE ... ADD COLUMN` + `duplicate column` tolerance as the
existing four migrations; no backfill - old rows have NULL and are never a carry source):

- `jobs.diff_digest TEXT` - `"sha256:<hex>"` of the diff bytes this job partitioned for review.
- `jobs.carried_from TEXT` - for a carried review, the head the model actually read (the ORIGIN,
  not the immediately preceding carried head, so a chain A -> B -> C names A).

**What is digested.** The bytes `review_job()` already holds after `textify_nul_sections()`: the
Gitea `/pulls/{n}.diff` body (merge base vs head) with each NUL-only binary section rebuilt as a
text diff. For every PR without such a section this IS the raw `.diff` body byte for byte. For one
that has them, the raw body carries only `index <abbrev>..<abbrev>` + `Binary files ... differ` for
that file - an abbreviated blob hash a PR author can grind a collision for in seconds - while the
model reviewed the rendered TEXT; digesting the rendered bytes binds the content the verdict vouched
for. The digest is written by `post_review()` in the same `UPDATE ... state='posting'` that already
records `verdict` and `coverage`, so a review whose POST landed always has it (including the
restart/`--requeue` paths, which reach `done` through the marker pre-check without rewriting it).

**Decision point.** In `review_job()` (non-chunk path only - `chunk_enabled(repo)` returns earlier
into the adapter and is never carried), after the existing post-diff race check (`d2 = pr_ok(...)`)
and after `plan_coverage()`, BEFORE `review_round()` / `convergence_context()` / `run_llm()`. A new
function `carry_source(repo, pr, head_sha, digest, coverage, author, paths)` returns
`(origin_head, None)` or `(None, reason)`. Carry only when ALL of:

1. `CFG.get("carry_clean_verdicts")` is true (code default False).
2. The current head's `coverage == "full"` (computed now, under the current config).
3. `guard_findings(author, paths)` is empty under the CURRENT config (the deterministic unattended-
   author guard is re-applied, so a guard added since the source review is never bypassed).
4. **The persona's LATEST completed review of this PR is the clean one.** The most recent `done` row
   for `(repo, pr)` at any other head (`ORDER BY updated DESC, id DESC LIMIT 1`, all verdicts
   including NULL) must have `verdict='clean'`, `coverage='full'` and `diff_digest == digest`.
   "Any earlier clean head" is NOT enough: A clean -> B findings -> C reverts to A's diff must be
   re-reviewed, because B's findings may be about code that was already in A.
5. **Gitea agrees.** This persona's last authenticated marker on the PR (`iter_reviews` +
   `marker_of`, oldest-first so the last one wins - the same reading `convergence_context()` uses)
   is at that source head with `verdict=clean`. Guards against the state DB and the forge diverging
   (a lost or restored DB, a review posted by another instance of the persona).

Any exception while evaluating 4-5 -> no carry, log, fall through to the ordinary review (carry is
an optimization; it never fails a job).

**Posting.** Through `post_review()` - the single mutation path - so the carried review gets the
same final head re-check, `posting_disabled()` check, marker dedupe, `posting` state, ambiguous-POST
quarantine and the `maybe_merge()` call. Event `APPROVED` (as for every clean verdict: with
`dismiss_stale_approvals` the update dismissed the old approval and branch protection needs a fresh
one), no inline comments, `verdict="clean"`, `coverage="full"`, `digest=digest`,
`carried_from=origin`. Body, authored by the bot (never model text), then the canonical
`review_marker(head_sha, "clean")`:

> Carried forward from <origin9>: this reviewer's review of that head was clean, and this pull
> request's diff at <head9> is byte-identical to the one it reviewed (for example, the base branch
> was merged in), so the model was not run again. Not re-read against the updated base; CI runs on
> this head.

**Interactions, decided:**

- `review_round()`: adds `AND carried_from IS NULL`. A carried head is not a new reading, so it must
  not advance the convergence ladder (the same inflation the `verdict`/`coverage` columns removed for
  skips: three carries would otherwise buy the next real review the round-3 blockers-only ladder).
- `existing_marker()`: unchanged; the carried marker dedupes its head like any review.
- `maybe_merge()`: unchanged. It reads markers at the CURRENT head and merges at that head. A
  carried marker exists only for a head whose diff the persona effectively approved (identical bytes,
  latest own verdict clean); the merge still needs every persona clean at that head + CI green +
  author allowlisted + labels/owner gates. Each persona carries only ITS OWN verdict (each host has
  its own state DB), so carry never manufactures the peer's approval.
- `convergence_context()`: unchanged; after a carry, "your previous review" is the carried body (the
  previous verdict was clean, so there are no findings to verify).
- Quarantine/retry: inherited from `post_review()`. A carry re-evaluates deterministically on retry.
- `commit_sweep()` merge-blocked age: a carried `done` row dates "our review landed" like any other.
- Metrics: `bump_meta("reviews_carried_total")` only when the carried POST actually landed;
  exported as `reviewbot_reviews_carried_total`. A carry does NOT bump `reviews_full_total`.
- Journal: the existing `reviewed <repo>#<pr> @ <sha>: <note>` line with note
  `carried clean from <origin9> (diff unchanged)`; plus, with the flag on and a prior review present,
  `carry <repo>#<pr> @ <sha9>: no (<reason>)` (reasons: `coverage <x>`, `guarded path`,
  `last review <verdict>`, `diff changed`, `gitea marker <...>`, `check failed: <err>`). No line
  for a PR's first review (the common case).
- Dismissal: like `persona_verdicts()`, the carry ignores Gitea's `dismissed` flag - the update
  itself auto-dismisses the source approval, and telling that from a human dismissal needs the
  issue timeline, which the bots' `write:repository` tokens cannot read (`read:issue`). No new
  endpoint is called: only `/pulls/{n}`, `/pulls/{n}.diff`, `/pulls/{n}/reviews`, `/raw/...`.

**Residual risk (stated in the PR):** the persona does not re-read the PR against the new base. A
change on main can make an unchanged PR diff wrong (semantic conflict: a renamed function the PR
still calls, a config key main removed). CI re-runs on the new head and is a merge precondition;
the reviewer's own verdict is the earlier reading. A rebase update is covered the same way as a
merge update: only the bytes of the diff matter.

### B. Fair queue

`worker_once()` claim, inside the same single `with db_lock:` block and connection:

- `CFG.get("queue_policy", "fifo")`. `"fifo"`: the existing query, byte-for-byte. `"fair"`:
  1. `now = time.time()` once; candidates =
     `SELECT repo, MIN(created) FROM jobs WHERE state IN ('queued','retry') AND next_at<=? GROUP BY repo`.
  2. For each candidate repo read `meta['queue_served:<repo>']` (exact key, no LIKE; missing = 0).
  3. Pick `min(candidates, key=(served, oldest_created, repo))` - never-served first, ties to the
     repo whose oldest eligible job is oldest (FIFO among equals), then name for determinism.
  4. Claim `SELECT ... WHERE repo=? AND state IN ('queued','retry') AND next_at<=? ORDER BY created,
     id LIMIT 1`, then the existing `UPDATE jobs SET state='running'`.
  5. `seq = meta['queue_served_seq'] + 1`; write it and `meta['queue_served:<repo>'] = seq`, same
     transaction as the claim. A monotonic sequence rather than a timestamp, so a clock step cannot
     re-order repos; persisted, so a restart keeps the rotation.
- Unchanged guarantees: the `RATE_LIMITED_UNTIL` early return (before the lock), `next_at`, the
  coalescing/supersede done by `enqueue()` (at most one queued head per PR), quarantine stickiness
  (only `queued`/`retry` are claimable), `--requeue` (sets `queued`, `next_at=0`), the startup
  recovery, and the one-lock/one-connection/one-commit claim.
- Chunked reviews (`progress` requeues with `next_at=0`) now interleave with other repos instead of
  running back to back; correctness is unaffected (each part is durably recorded), only latency.
- An unknown `queue_policy` value logs once at startup and runs `fifo`.
- Observability: when the fair pick differs from what FIFO would have claimed, after the lock is
  released: `bump_meta("queue_fair_overrides_total")` (exported
  `reviewbot_queue_fair_overrides_total`) and
  `queue: fair pick <repo>#<pr> ahead of fifo <repo2>#<pr2> (<n> repos eligible)`.
  Startup logs `queue: policy=<x>, carry_clean_verdicts=<bool>`.

**Author dimension: not now.** Repo-level fairness is what stops platform#2748 waiting behind 30
relay jobs. A per-author level would need an `author` column filled on every enqueue path, and
`head-moved` / `--requeue` / the reconciler's resurrection have no payload (an extra API read per
enqueue). Within relay the load is one author anyway. Revisit if the per-repo wait shows starvation
inside a repo.

### Configuration

| config.json key | code default | role default (`defaults/main.yml`) |
|---|---|---|
| `carry_clean_verdicts` | `false` | `pr_reviewer_carry_clean_verdicts: true` |
| `queue_policy` | `"fifo"` | `pr_reviewer_queue_policy: fair` |

Both rendered by `templates/config.json.j2`. Both personas use the role, so one deploy enables both.

## Critical files

- `ansible/roles/pr_reviewer/files/reviewbot.py` - `db()`, `review_job()`, new `diff_digest()` /
  `carry_source()`, `post_review()` (2 kwargs), `review_round()`, `worker_once()` claim,
  `write_metrics()` (2 counters), `main()` startup log.
- `ansible/roles/pr_reviewer/defaults/main.yml`, `templates/config.json.j2` - the two flags.
- `scripts/tests/test_reviewbot.py` - new test classes.
- `docs/runbooks/dev-workers.md` § reviewers - a short operator note on both.

## Verification

Unit tests (stdlib unittest, run in Docker `python:3.13 --network none`, the whole
`scripts/tests` discover as CI does), each failing without the change:

- Carry: flag off -> LLM runs; clean at A + identical diff at B -> no LLM, APPROVED, marker
  `head=B verdict=clean`, body names A, row `carried_from=A`, counter +1; latest review `findings`
  -> LLM runs; A clean / B findings / C == A's diff -> LLM runs; diff differs -> LLM runs; partial
  coverage now -> LLM runs; Gitea's last own marker not clean at A -> LLM runs; a guard added after A
  -> LLM runs; NUL-text file whose raw section is identical but rendered content differs -> LLM runs;
  chain A -> B -> C names A; `review_round` does not count carried rows; migration adds both columns.
- Fair: fifo default keeps global oldest-first; fair interleaves A1,B1,A2,A3; `next_at` in the future
  excludes a repo; a restart (fresh module, same DB) continues the rotation; `RATE_LIMITED_UNTIL`
  still blocks; quarantined/superseded never claimed; unknown policy -> fifo; override counter.
- Role: defaults + rendered template carry both keys.

Post-deploy (operator, not in this PR): `reviewbot_reviews_carried_total` rising on both hosts;
journal `carry ... no (diff changed)` ratio; per-repo enqueue->done p90 from `state.sqlite`
(`mode=ro`) for platform vs relay.

<!-- codex-review-status: pending -->
