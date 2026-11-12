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

- `jobs.diff_digest TEXT` - the review FINGERPRINT (below) of the head this job reviewed.
- `jobs.carried_from TEXT` - for a carried review, the head the model actually read (the ORIGIN,
  not the immediately preceding carried head, so a chain A -> B -> C names A).

**The fingerprint** (`review_fingerprint(raw)`) is
`sha256("reviewbot-carry/v1" | CARRY_EPOCH | raw)`, stored as `"sha256:<hex>"`, where `raw` is the
body of Gitea's `/pulls/{n}.diff` (merge base vs head) exactly as fetched, and `CARRY_EPOCH` is
computed once at import from the bytes of `reviewbot.py` itself plus the canonical JSON of the
config keys that decide what is read and how a verdict is reached (`persona`, `exclude_globs`,
`doc_globs`, `max_diff_bytes`, `unattended_authors`, `guarded_paths`, `pin_authors`,
`dependency_authors`). So equal fingerprints mean: the same diff bytes, partitioned by the same
code under the same exclusion/cap/guard/rubric policy, hence the same reviewed input, the same
coverage manifest and the same deterministic guard. Every reviewbot.py deploy or policy edit starts
a new epoch: each PR's next head after it is reviewed in full once (cheap, automatic, and nobody
has to remember to bump a version). If the source cannot be read the epoch is unset and carry is off.

**No carry at all when the raw diff has a binary section** (`BIN_MARKER_RE` anywhere in `raw`:
`Binary files ... differ` / `GIT binary patch`). Such a section is identified in the diff only by
abbreviated `index` hashes, which an author can grind a collision for; textified (NUL-only)
sections are additionally rendered with CRLF/NUL normalisation, so their rendered text does not
bind the bytes either. Text sections carry their whole change in the hunks, so identical bytes mean
an identical change. Conservative by design; relay/llm-router/workbricks PRs are code.

The fingerprint is written by `post_review()` in the same `UPDATE ... state='posting'` that already
records `verdict` and `coverage`, so a review whose POST landed always has it, including the
restart and `--requeue` paths, which reach `done` through the marker pre-check without rewriting
the row.

**Decision point.** In `review_job()` (non-chunk path only - `chunk_enabled(repo)` returns earlier
into the adapter and is never carried), after the post-diff race check (`d2 = pr_ok(...)`) and
`plan_coverage()`, BEFORE `review_round()` / `convergence_context()` / `run_llm()`. New
`carry_source(repo, pr, head_sha, fingerprint, raw, coverage)` returns `(origin, None)` or
`(None, reason)`. Carry only when ALL of:

1. `CFG.get("carry_clean_verdicts")` is true (code default False) and `CARRY_EPOCH` is set.
2. No binary section in `raw`, and the current `coverage == "full"`.
3. **A matching clean row exists:** a `done` row for `(repo, pr)` at another head with
   `verdict='clean'`, `coverage='full'` and `diff_digest == fingerprint` (cheap DB check first, so
   a PR's first review costs no extra API call).
4. **Gitea says it is this persona's latest word on the PR.** A COMPLETE enumeration of the PR's
   reviews (paginate until an empty page, dedupe by review id, fail closed past a bound - the chunk
   adapter's `reviews()` discipline; `iter_reviews()` stops at 10 pages and at the first short page,
   so it is not used here) whose highest-id authenticated own marker (`marker_of`) is
   `verdict=clean` at a head S that has a matching row from 3.
5. **No later contrary verdict in the DB:** no row for `(repo, pr)` with `id >` S's row id, at
   another head, whose recorded `verdict` is anything but `clean`, in ANY state. This catches a
   findings review whose POST failed or is still unresolved (quarantined, posting) and is therefore
   absent from Gitea.

Any exception while evaluating 3-5 -> no carry, log, fall through to the ordinary review (carry is
an optimization; it never fails a job).

**Posting.** Through `post_review()`, the path every review takes, so the carried review gets the
same final head re-check, `posting_disabled()` check, marker dedupe, `posting` state,
ambiguous-POST quarantine and `maybe_merge()` call. Event `APPROVED` (as for every clean verdict:
with `dismiss_stale_approvals` the update dismissed the old approval and branch protection needs a
fresh one), no inline comments, `verdict="clean"`, `coverage="full"`, the fingerprint and
`carried_from=origin`. The body is authored by the bot (never model text):

> Carried forward from <origin9>: this reviewer's review of that head was clean, and this pull
> request's diff at <head9> is byte-identical to the one it reviewed (for example, the base branch
> was merged in), so the model was not run again. Not re-read against the updated base; CI runs on
> this head.

followed by `<!-- review-bot-carried: from=<origin> -->` on its own line directly before
`review_marker(head_sha, "clean")`. An HTML comment cannot come from model text (`parse_review`
rewrites `<!--`), and it is recognised only at that fixed position in an authenticated own review.

**Interactions, decided:**

- `review_round()`: adds `AND carried_from IS NULL`. A carried head is not a new reading, so it must
  not advance the convergence ladder (three carries would otherwise buy the next real review the
  round-3 blockers-only ladder).
- `convergence_context()`: when choosing "your previous review", skip own reviews ending in the
  carried comment, so the next real round still sees the substantive review (a clean review can
  carry nits, and from round 3 advisories) rather than the carry notice.
- `existing_marker()`: unchanged; the carried marker dedupes its head like any review.
- `maybe_merge()`: unchanged. It reads markers at the CURRENT head and merges at that head. A
  carried marker exists only for a head whose reviewed input the persona already judged clean as
  its latest word; the merge still needs every persona clean at that head + CI green + author
  allowlisted + labels/owner gates. Each persona carries only ITS OWN verdict (each host has its own
  state DB), so carry never manufactures the peer's approval. The carry posts APPROVED itself, so
  the 405 approval-upgrade branch is not reached for it.
- Quarantine/retry: inherited from `post_review()`; a retry re-evaluates deterministically.
- `commit_sweep()` merge-blocked age: a carried `done` row dates "our review landed" like any other.
- Metrics: `bump_meta("reviews_carried_total")` only when the carried POST actually landed;
  exported as `reviewbot_reviews_carried_total`. A carry does NOT bump `reviews_full_total`.
- Journal: the existing `reviewed <repo>#<pr> @ <sha>: <note>` line with note
  `carried clean from <origin9> (diff unchanged)`; plus, with the flag on and a prior own review,
  `carry <repo>#<pr> @ <sha9>: no (<reason>)`. No line for a PR's first review.
- Dismissal: the carry does not read Gitea's `dismissed` flag (see disposition 6).
- No new endpoint is called: `/pulls/{n}`, `/pulls/{n}.diff`, `/pulls/{n}/reviews` only - all
  `write:repository`-readable.
- Latency: a carry runs in the worker, so while every seat is parked (`RATE_LIMITED_UNTIL`) carries
  wait too. A carry-only pass during parks is a possible follow-up, not this change.

**Residual risk (stated in the PR):** the persona does not re-read the PR against the new base. A
change on main can make an unchanged PR diff wrong (semantic conflict: a renamed function the PR
still calls, a config key main removed). CI re-runs on the new head and is a merge precondition;
the reviewer's own verdict is the earlier reading. A rebase update is covered the same way as a
merge update: only the bytes of the diff matter.

### B. Fair queue

`worker_once()` claim, inside the same single `with db_lock:` block and connection:

- `CFG.get("queue_policy", "fifo")`. `"fifo"`: the existing query, byte-for-byte. `"fair"`
  (`claim_fair()`):
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
- Meta values are TEXT: the sequence and stamps are parsed with `int()` (unparsable = 0), never
  compared as strings.
- Unchanged guarantees: the `RATE_LIMITED_UNTIL` early return (before the lock), `next_at`, the
  coalescing/supersede done by `enqueue()`, quarantine stickiness (only `queued`/`retry` are
  claimable), `--requeue` (sets `queued`, `next_at=0`), the startup recovery, and the
  one-lock/one-connection/one-commit claim. (Fairness does not assume one queued head per PR: an
  older RUNNING head that fails returns to `retry` beside a newer queued one, and is superseded by
  `pr_ok()` when claimed, exactly as under FIFO.)
- Chunked reviews (`progress` requeues with `next_at=0`) now interleave with other repos instead of
  running back to back; correctness is unaffected (each part is durably recorded), only latency -
  and a 128-part train holding the only worker for hours is exactly the starvation this removes.
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

- `ansible/roles/pr_reviewer/files/reviewbot.py` - `db()`, `review_job()`, new `CARRY_EPOCH` /
  `review_fingerprint()` / `all_reviews()` / `carry_source()` / `carried_review()`, `post_review()`
  (2 kwargs), `review_round()`, `convergence_context()`, `worker_once()` claim + `claim_fair()`,
  `write_metrics()` (2 counters), `main()` startup log.
- `ansible/roles/pr_reviewer/defaults/main.yml`, `templates/config.json.j2` - the two flags.
- `scripts/tests/test_reviewbot.py` - new test classes.
- `docs/runbooks/dev-workers.md` § reviewers - a short operator note on both.

## Verification

Unit tests (stdlib unittest), run in Docker with `--network none` on an image that adds PyYAML and
jinja2 (the CI runner's system packages) to `python:3.13`: the three reviewbot modules, then the
whole `scripts/tests` discover as CI runs it, compared with a baseline run on `main`. Each new test
fails without the change:

- Carry: flag off -> LLM runs; clean at A + identical diff at B -> no LLM, APPROVED, marker
  `head=B verdict=clean`, carried comment, row `carried_from=A`, counter +1; latest Gitea review is
  findings -> LLM; A clean, B findings POST ambiguous (quarantined, absent from Gitea), C == A's
  diff -> LLM; diff differs -> LLM; a binary section -> LLM; partial coverage -> LLM; Gitea's
  latest own marker beyond 10 pages / with short pages is still found; enumeration past the bound ->
  no carry; an exclusion-policy change between A and B (same bytes) -> LLM; chain A -> B -> C names
  A; `review_round` does not count carried rows; `convergence_context` skips a carried own review;
  migration adds both columns.
- Fair: fifo default keeps global oldest-first; fair interleaves A1,B1,A2,A3; `next_at` in the
  future excludes a repo; a restart (fresh module, same DB) continues the rotation; the sequence
  crosses 9 (numeric, not lexical); `RATE_LIMITED_UNTIL` still blocks; quarantined/superseded never
  claimed; a `--requeue`d job is claimed under the rotation; an older head returned to `retry`
  after a newer one was enqueued is still claimable; unknown policy -> fifo; override counter + log.
- Role: defaults + rendered template carry both keys.

Post-deploy (operator, not in this PR): `reviewbot_reviews_carried_total` rising on both hosts;
journal `carry ... no (...)` reasons; per-repo enqueue->done p90 from `state.sqlite` (`mode=ro`) for
platform vs relay.

## Codex plan review, round 1 - dispositions

1. Failed findings POST lets an older clean carry - ACCEPTED: rule 5 (a later contrary verdict in
   any state vetoes).
2. Equal digest + full coverage does not mean equal reviewed input across a policy change -
   ACCEPTED: the fingerprint includes `CARRY_EPOCH` (code bytes + review-policy config).
3. The mutable `.diff` endpoint can be raced (C -> A -> C during the fetch) - REJECTED for this
   change: the same flip already makes the ORDINARY path review A's bytes and post the verdict at
   C (the `d2` check compares head equality only), so carry does not widen it; exploiting it needs
   the author to win the race on both hosts independently, and CI still runs on C's real content
   and gates the merge. Binding diffs to immutable refs is a change to both paths, not this one.
4. Abbreviated hashes / textify normalisation - ACCEPTED in the conservative form: no carry when
   the raw diff has any binary section; the fingerprint is over the raw bytes.
5. `iter_reviews()` truncates - ACCEPTED: carry uses a complete, deduped, fail-closed enumeration.
6. Carry can re-approve after a human dismissal - REJECTED: in the target repos EVERY source
   approval is auto-dismissed by the very update being carried across, and telling that from a
   human dismissal needs the issue timeline (`read:issue`, which the bot tokens lack). Dismissal is
   not a veto today either: `persona_verdicts()` ignores it and a re-review of the identical diff
   re-approves. The supported vetoes (`no-automerge`, owner-approval label/paths) are enforced by
   `maybe_merge()` at merge time, independent of carry.
7. Marker-only completions have NULL provenance - PARTLY ACCEPTED: provenance (verdict, coverage,
   fingerprint, carried_from) is written before the POST, so restart and `--requeue` keep it; a
   carried review rediscovered with a LOST database counts as a round exactly as every review does
   today (pre-existing legacy-NULL rule, out of scope). Ordering no longer uses `updated` (Gitea
   review id + DB row id instead).
8. Restart re-queues an ordinary `posting` row - REJECTED: pre-existing, documented decision in
   `main()`, identical for carried and ordinary reviews; a duplicate clean APPROVED at one head is
   harmless.
9. The 405 approval-upgrade posts outside `post_review()` - wording ACCEPTED (the plan no longer
   calls it the single mutation path); carry never reaches that branch (it posts APPROVED itself).
10. Carry notice replaces the substantive review in convergence context - ACCEPTED: authenticated
    carried comment; `convergence_context()` skips it.
11. Chunk policy binding hashes reviewbot.py; interleaving delays parts - the binding is
    pre-existing for every reviewbot.py change (deploy both hosts together, as always);
    interleaving is accepted and documented above.
12. Carries wait while the account is parked - ACCEPTED as a documented limitation (follow-up).
13. "At most one queued head per PR" was false; numeric parsing; transition tests - ACCEPTED.
14. Offline test environment needs PyYAML/jinja2 - ACCEPTED (image above).

<!-- codex-review-status: complete -->
