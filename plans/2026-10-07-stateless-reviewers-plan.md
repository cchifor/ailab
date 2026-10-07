# Stateless reviewers: several instances per kind, coordinated only through the forge (Gitea or GitHub)

## Context

reviewbot runs one instance per kind (`claude` on reviewer-1, `codex` on reviewer-2). Each keeps its
queue, failure memory and round history in a local SQLite file, so a second instance of the same
kind would duplicate model runs and miscount convergence rounds. Measured over 7 days (2026-10-07):
claude p90 enqueue-to-posted 6.8 min, codex 0.8 min; merges wait for both.

**Requirements.**

1. N instances of one kind (claude / codex) on different machines.
2. **Posting exclusivity is absolute:** at most one review per kind per PR head, ever.
   **Execution exclusivity is best effort:** one model run per kind per PR, except an overlap of about
   60 s after a push while the stale run aborts.
3. Instances are **stateless**: nothing on a machine is needed to decide anything. A local SQLite
   file may keep telemetry counters only.
4. **The forge is the only synchronization point** (PR reviews, plus tags and one file in a
   coordination repository on the same forge). No shared database.
5. Works on **Gitea and GitHub** (github.com and GitHub Enterprise Server).

**Facts measured on Gitea 1.26.1** (`cchifor/primes-lab`, `workstation-bot`; scripts in
`scripts/spikes/`):

| Fact | Evidence |
| --- | --- |
| `POST /repos/{o}/{r}/tags` is create-if-absent under concurrency: one git tag object results | 30-way × 5, 20-way × 25 and 20-way × 15 staggered rounds |
| **The `201` does not identify the writer**: in 25/25 staggered rounds the git tag's message belonged to a caller that got `500` | Spike 2 |
| Reading the tag object back by nonce yields exactly one owner | Spike 3: 15/15 |
| Sequential duplicate → `409 tag already exists` | Spike 2 |
| Tags written by a `500` caller have no release row: `DELETE /tags/{name}` → 404; `git push :refs/tags/<name>` removes them | Spike 2 |
| `GET /git/refs/tags/<prefix>` → `{ref, url, object{sha}}`, complete at 300 refs; `GET /git/tags/{sha}` → message + tagger `Gitea <gitea@fake.local>`, date = server time (0.0 s from `Date`) | Spikes 2, 3 |
| Messages round-trip exactly (quotes, backslashes, Unicode, newlines) | Spike 3 |
| **A create whose client timed out still lands** (5/5, 0.2 s later) | Spike 3 |
| **No immutable PR diff over the API** (compare returns no files; web `.diff` needs a browser session) | Spike 3 |
| **A review POST with an invalid inline position returns 500 and leaves an empty PENDING review that the next POST by the same account absorbs** | Spike 3 (primes-lab#18, closed) |

Review trail: `…-review-r1-fable.md`, `…-review-r1-codex.md`, `…-review-r2.md`, `…-review-r3.md`,
`…-review-r4.md`, `…-review-a1.md`, `…-review-a2.md`, `…-review-a3.md` (alignment rounds). This
version (v3.2) applies alignment round 3: rights enumerated by exact-ref walk, a late-landed right is
always finished (never used), and the operator's cleanup holds a right like any other POST.

## Design principle

**Safety comes from a sequence of irrevocable publication rights per (kind, PR), each proven by
reading back a nonce from the forge and used for at most one POST. Everything else is efficiency.**
Work leases, attempt counters, clocks and retention only decide who spends model time; if any of them is wrong, the cost is a duplicated model
run or a delay, never a second review.

## 1. Forge adapter

The core (coverage planning, prompts, verdicts, rounds, seats, publication and claim logic) never
calls a forge API; one adapter per forge does (`GiteaForge`, `GitHubForge`). Repositories are
addressed as `<forge>:<owner>/<name>` (a bare name means Gitea). Each forge has its own coordination
repository; no state crosses forges.

| Operation | Gitea | GitHub |
| --- | --- | --- |
| Auth | PAT (`token`) | **GitHub App per kind** (installation token) or machine-user PAT (`Bearer`) |
| PR read | `GET /pulls/{n}` | same; `mergeable: null` = not yet |
| List open PRs | `?state=open&limit=50&page=` until an empty page | `?per_page=100` + `Link`; `If-None-Match` (304 exempt from the primary limit) |
| List reviews | paginate to the empty page (no cap); a failed page fails the read | same with `Link` |
| Review diff | **local git** (section 6) | **local git** (section 6) |
| Post review | `{commit_id, event: APPROVED\|COMMENT, body, comments[{path, new_position\|old_position}]}` | `{commit_id, event: APPROVE\|COMMENT, body, comments[{path, line, side: RIGHT\|LEFT}]}` |
| Definitive POST failure | 4xx (an invalid position answers **500**: not definitive) | 4xx incl. 422 (nothing is created) |
| CI green | combined status `success`, non-empty | ≥ 1 check run or status; check runs paginated, all `completed` with `success`/`neutral`/`skipped`; statuses `success`, or none only if check runs exist; required names from `GET /rules/branches/{b}` present and green |
| Merge | `POST /merge {Do: merge, head_commit_id}` | `PUT /merge {sha, merge_method}`; merge-queue repositories: review and approve only |
| Webhook | `X-Gitea-Signature` (hex HMAC-SHA256) | `X-Hub-Signature-256: sha256=<hex>`; actions `opened, reopened, synchronize, ready_for_review, edited, labeled, review_requested, converted_to_draft, closed` |
| Create-if-absent tag (claims, rights) | `POST /tags {tag_name, target, message}`; status ignored | `POST /git/tags` with a payload **frozen per claim** (fixed `tagger.date`, same SHA on every retry), then `POST /git/refs`; 422 "Reference already exists" is the normal race outcome |
| Ownership readback | `GET /git/refs/tags/<name>` → `GET /git/tags/{sha}` | `GET /git/ref/tags/<name>` → `GET /git/tags/{sha}` |
| Tags without a message (outcomes, operator) | API tag create | lightweight ref to the root commit (one call) |
| List by prefix | `GET /git/refs/tags/<prefix>` | `GET /git/matching-refs/tags/<prefix>` (truncation threshold measured in Phase 0b; fail closed at it) |
| Server clock | `Date` header; claim lease start = `tagger.date` (server-set) | `Date` header; `tagger.date` client-written from `Date`, future values beyond 300 s rejected (leases are efficiency only) |
| Coordination file | contents API | contents API |
| Bot identity | login + user id | `<app>[bot]` login + user id (`user.type == Bot`) |

Adapter rules that hold on both forges: tags are always read **by exact ref** (`pub1` vs `pub10`,
`p11` vs `p112`), never by prefix match; a review body is shortened to the forge's limit (GitHub
65,536 characters) by trimming the summary and coverage table, **never the marker**; the computed
diff is bounded by `max_raw_bytes` before it is parsed.

GitHub specifics:

- **Identity:** one App per kind; permissions pull requests write, contents read on reviewed
  repositories and write wherever the bot merges and on the coordination repository, checks and
  statuses read, metadata read. Whether an App APPROVE counts toward required approvals is checked
  in Phase 0b; where it does not, humans approve and the bot does not merge.
- **Intake:** GitHub cannot reach the LAN reviewers, so instances **poll** (conditional requests,
  `poll_s` 60 s, jittered); webhooks are optional through a public ingress.
- **Rate limits** are shared per identity: slow discovery below 20 % remaining; a secondary-limit
  403/429 parks this instance's forge use for `Retry-After`, or 60 s doubling to 15 min. About 9
  content-creating calls per review; the secondary limit (500 per hour) is per identity, so each of
  N instances defers claims above `400 / N` per hour.

## 2. Coordination repository

`<owner>/reviewbot-coord` on each forge: private, initialised with one commit (the tag target), no
mirror, no webhooks, no Actions, no protected-tag rules or tag rulesets; writable by the kind bots and
the owner only. Repository ACL is the only authentication (the tagger proves nothing).

It holds **one file**, `policy.json`, which is **authoritative** (loaded, never compared with local
configuration):

```json
{"v": 1, "epoch": "<uuid4>",
 "merge_personas": ["claude", "codex"], "merge_authors": ["cchifor", "chifor", "renovate-bot"],
 "merge_authors_by_repo": {"gitea:cchifor/ailab": ["dsh"]}, "unattended_authors": ["dsh"],
 "guarded_paths": [".gitea/workflows/", ".github/workflows/"],
 "caps": {"charged": 5, "timeouts": 2, "window": 40}}
```

Instances read it (ETag / blob SHA) at start, each sweep, and **immediately before every POST and
merge**. Changing policy is one commit to this file by the owner. Local configuration keeps only
instance matters (seats, keys, model ladder, timeouts). `epoch` is rewritten by the restore procedure
(section 10): an instance refuses to post or merge when the epoch differs from the one it started
with.

## 3. Tags

Flat names (`check-ref-format` valid, ≤ 120 characters; the forge's numeric repository id). Work
claims are per head; **publication rights are per PR**:

```
rb1.<kind>.r<repo_id>.p<pr>.<head_sha40>.<work suffix>
rb1.<kind>.r<repo_id>.p<pr>.<pub suffix>
```

| Suffix | Writer | Meaning |
| --- | --- | --- |
| `a<N>` | create-if-absent, message | Work claim N for this head (lease) |
| `a<N>.<outcome>.<t>` | claim owner | `ok` (published), `fail`, `failt` (charged), `rel.<why>` (uncharged: `ratelimit`, `shutdown`, `moved`, `closed`, `budget`, `restart`, `pub`); names read `a<N>.rel.<why>.<t>` |
| `cut<K>` | `--requeue` | Attempts below K no longer count; numbering continues |
| `pub<G>` | create-if-absent, message (`head`, `purpose`: `review`, `approval` or `operator`) | **Publication right** G for this PR |
| `pubsent<G>.<t>` | right owner, mandatory | About to send the POST, at forge time `t` |
| `pubfin<G>` | right owner | Final: the POST got a 2xx or 4xx answer, or was never sent. This owner will not send again |
| `pubvoid<G>` | `--requeue --force` | Right G released by the operator |

A right G is **resolved** when it has `pubfin<G>` or `pubvoid<G>`. A new right G+1 may be created only
when every existing right of the PR is resolved, so at most one POST of a kind is ever in flight on a
PR — reviews, approvals and operator cleanup alike [a2: Codex 5, Fable 2]. Rights have no gaps (every
creator targets one past the highest), so they are **enumerated by exact-ref walk** — `pub<k>`,
`pubfin<k>`, `pubvoid<k>` for k = 1, 2, … until `pub<k>` is absent — and never by a prefix listing,
which keeps the safety argument independent of listing size [a3: Fable 2].

Messages (claims and rights only):

```json
{"v": 1, "kind": "codex", "instance": "reviewer-2b", "nonce": "<boot_id>-<pid>-<uuid4>",
 "repo": "gitea:cchifor/ailab", "pr": 1112, "head": "<sha40>", "purpose": "review",
 "lease_s": 1200, "issued": "<forge time, GitHub only>"}
```

## 4. Ownership: read back, never trust the status

`create_owned(name)`: create; **whatever the answer** (201, 409/422, 500, timeout) read the exact ref
and its object: owned **iff** the object's message nonce is this claim's. The nonce (and on GitHub
the whole tag object) is fixed per (kind, head, name) for the life of the process. Not found → poll
for up to `post_margin_s`, then "not owned for now". A tag that lands later carrying my nonce is mine
(spike 3 shows timed-out creates landing): a work claim becomes my lease; a **publication right is
finished at once** (consumed + `pubfin`, never sent) so the PR's sequence moves on [a3]. Persistent non-race errors
(401/403, a 422 that is not "already exists", 5 consecutive not-found) are counted and alerted.

## 5. Publication (the safety mechanism)

Every POST of a kind on a PR — a review, an approval upgrade, or the operator's cleanup — goes
through one publication right. In `post_review`, after the existing checks (head unchanged,
`posting-disabled` absent):

1. Walk the PR's rights. An unresolved right that is **mine** (a late landing) → finish it first (it
   was never sent). An unresolved right that is **not mine** → wait for it to resolve within the
   remaining lease budget (polling the exact refs), then repeat; budget exhausted or head moved →
   `a<N>.rel.pub.<t>` (uncharged) [a3: Codex 1, Fable 5].
2. `create_owned(pub<G+1>)` (G = highest) with this head and purpose. Not owned → step 1.
3. **Pre-send checks**, all re-read now: PR open, not draft, head equal to the right's head; for a
   review, no authenticated marker of this kind at the head; for an approval, a clean marker of this
   kind at the head **and no APPROVED review by the bot at the head** [a3: Codex 4]; `policy.json`
   loaded, epoch unchanged; guard findings and verdict recomputed against that policy; **no PENDING
   review by the bot on the PR** (a failed POST leaves one and the next POST would absorb it —
   spike 3). A read error is retried within the remaining budget. Any definite failure → finish
   without sending.
4. Create `pubsent<G>.<t>` and confirm it **by readback**. Absent → finish without sending.
5. **Self-fence:** more than `post_margin_s` (90 s, monotonic) since step 3 → finish without sending.
6. POST once. 2xx or 4xx → finish; `a<N>.ok.<t>`. 5xx, timeout or crash → nothing: the right stays
   unresolved until the operator resolves it.

**Finishing** = add the right to the in-process consumed set, then write `pubfin<G>`, **retrying the
write with backoff for the life of the process** (always safe: the right is consumed) [a3: Fable 3].
Every exit after step 2 that does not POST finishes the right. Once a right is owned the head watcher
stops; step 3 re-checks the head.

**State of a PR's publication** (pure function over its rights and its reviews, for this kind):

| Observed | State |
| --- | --- |
| Authenticated marker of this kind at the current head | head **done** |
| A **PENDING review by the bot** on the PR | **ambiguous** (operator) [a3: Fable 1] |
| All rights resolved (or none) | **open** |
| Unresolved right younger than `pub_grace_s` (600) | **publishing** (skip) |
| Unresolved right older than `pub_grace_s` | **ambiguous** (operator) |

**Exclusivity.** Two POSTs of a kind on one PR at once need two unresolved rights; a right is created
only when all others are resolved, is owned by one nonce, is sent at most once, and is voided only
under section 9. **Stated residual:** a crash inside the post window (right owned, no `pubfin`) or a
failed POST blocks reviews and approvals of that PR for this kind until the operator acts; the alert
names the PR and the owner instance [a3: Fable 3].

Approval upgrades carry **no marker** (they are not verdicts); their landing evidence is an APPROVED
review by the bot at the head.

## 6. Work (efficiency)

**Clock:** `server_now = Date(last response) + monotonic elapsed`; a response without `Date` fails
the cycle closed. Lease start = claim `tagger.date` (Gitea, server-set) or `issued` (GitHub).

**State of a head's work** (pure, over the tag list):

- `hw` = highest attempt ever; counting starts at the latest `cut<K>`.
- `charged` = `fail` + `failt` + abandoned (no outcome, lease expired); `timeouts` = `failt`.
- **Exhausted** if `charged ≥ 5`, `timeouts ≥ 2`, or `hw − K + 1 ≥ 40` (every claim in the window,
  whatever the reason). Alerted; `--requeue` writes `cut<hw+1>`.
- **Leased** if the newest outcome-less claim's lease has not expired. A live claim with my instance
  name but another nonce is my dead predecessor's → `rel.restart`.
- Otherwise **claimable** at `not_before` = last charged outcome + `min(3600, 60·2^charged)`.

**Claim cycle** for a due candidate `(repo, pr)`:

1. Skip when this instance cannot serve (all seats parked, `inhibit`, `posting-disabled`, forge
   identity parked).
2. Read the PR: closed or draft → drop; else head H and base ref.
3. Publication state of the PR: head done or PR ambiguous → drop (ambiguous counted, alerted per PR);
   publishing → later.
4. Work state of H: exhausted → drop; leased → recheck at lease end; not claimable → at `not_before`.
5. `create_owned(a<hw+1>)`; not owned → recheck in 60 s.
6. Owned: budget = lease start + `lease_s` − `post_margin_s` − `server_now`; below `min_llm_s` (120)
   → `rel.budget`. Else run `review_job` with that deadline.

**Head watcher** during a run, until a publication right is owned: read the PR every 60 s; on a head
move, close or draft, abort the run (router: stop reading the stream; CLI: terminate the child) and
write `rel.moved`/`rel.closed`. This is what bounds the per-PR overlap to about a minute.

**Outcomes:** deadline class (including running out of lease before posting) → `failt`; other
failures → `fail`; `RateLimited` → `rel.ratelimit`; SIGTERM during a run → `rel.shutdown`
(`TimeoutStopSec=120`); after a right was won, finish the POST first. A failed outcome write just lets
the lease expire.

**Immutable diff (both forges).** Each instance keeps a disposable bare partial clone per repository
(`/var/cache/reviewbot/git/…`, `--filter=blob:none`). Fetch the base ref and `refs/pull/<n>/head`,
verify the head SHA is present, `merge_base = git merge-base <base_tip> <head>`,
`git diff <merge_base> <head>`. A moved head is detected by SHA and supersedes the job; an A→B→A move
cannot substitute content. Wiping the cache only costs a re-fetch. Git authenticates via
`GIT_ASKPASS` reading the token file.

## 7. Discovery

Every instance lists open PRs of every allowlisted repository every `reconcile_s` (Gitea, ±20 %
jitter) or `poll_s` (GitHub, conditional), plus at start; webhooks add candidates where reachable.
No leader. Errors are isolated **per PR**. Candidates are in memory with a `next_check`; a restart
rebuilds them from the first listing. `maybe_merge` runs on every instance (idempotent).

## 8. Markers, rounds and the merge gate

- **Coverage comment** before the canonical marker (old readers ignore it):
  `<!-- review-bot:v1.cov persona=<p> head=<sha40> base=<base ref> coverage=<c> -->`.
- **Rounds** = distinct heads with this kind's authenticated marker (verdict clean or findings) and a
  `v1.cov` saying `full`, + 1. Markers without `v1.cov` (before cut-over) count as not full.
- **Strictest marker wins** per persona per head.
- **Markers authenticate** by the bot's user id (and login).
- **Merge gate** (per instance, before each merge): `policy.json` re-read; every merge persona has a
  marker at the current head, `clean`, whose `v1.cov` base equals the PR's current base ref (legacy
  markers accepted); the **current** guard rule passes (an unattended author touching a guarded path
  is never merged, whatever the posted verdict) [a2: Codex 6]; CI green; author allowed; no
  `no-automerge`; epoch unchanged. Merge with the head
  binding. **After** the merge call — success or error — re-read the PR: merged with a different base
  than validated → `ReviewbotMergedUnreviewedBase` (critical).
- **Residuals, stated:** a retarget in the second between the last check and the merge call is not
  prevented (no forge offers an expected-base merge). Detection by the post-merge re-read is **best
  effort**: a crash after the merge call (successful or not) and before the re-read completes loses
  it, and a failed re-read is retried only while the process lives [a2: Codex 7]. Operator edits to
  bot reviews are out of scope.

## 9. Operator actions

- `--coord-state <repo> <pr>`: publication and work state of the PR and every head.
- `--requeue <repo> <pr>`: exhausted current head → `cut<hw+1>`; refuses when leased.
- `--requeue <repo> <pr> --force`: resolves an ambiguous PR.
  1. **Resolve the unresolved right G, if any:**
     - definitive (`pubfin<G>` exists): already resolved;
     - never sent (no `pubsent<G>`): require `--owner-stopped <instance>` (printed from the right's
       message; for a migrated right the legacy service is stopped), then write `pubvoid<G>`;
     - possibly sent (`pubsent<G>`, no `pubfin<G>`): require `--owner-stopped`, then **wait
       `pub_void_min_age_s` (3600) on the tool's monotonic clock** and re-read the evidence of landing
       — a marker of this kind at the right's head for a review, an APPROVED review by the bot for an
       approval [a3: Codex 4]. Landed → `pubfin<G>`; else `pubvoid<G>`. Run it in the background
       (`systemd-run`); an interruption restarts the wait. A request the forge commits more than an
       hour after receipt is the remaining exposure.
  2. **Clean up under a right of its own:** `create_owned(pub<G+1>)` with `purpose: operator`, so no
     instance can POST meanwhile; delete the bot's PENDING reviews on the PR; finish the right
     (`pubfin<G+1>`) [a3: Codex 2, Fable 1]. All rights are then resolved and the PR is open.

## 10. Cut-over, rollback and restore

- **Cut-over (per kind):** stop the instance (drain: wait for an in-flight model run to end, or stop
  it); `--export-coord` turns the old jobs table into tags: per PR, the newest `posting` row (the POST
  may be in flight) or `ambiguous POST` quarantine → **one** migrated right `pub<G+1>` (one past any
  existing right) with instance `migrated` and `pubsent<G+1>.<export time>` — or `pubfin<G+1>` when a
  marker of this kind already exists at that head; other quarantines → `fail`/`failt` tags
  [a3: Fable 6].
  Deploy the new code (there is no local mode); start. Migrated rights then follow section 9 step 3
  [a2: Codex 3, Codex 4, Fable 1].
- **Rollback:** roll forward if at all possible. Emergency return to the old code: stop all instances
  of the kind, run `--import-local` (every `pub` without a marker and every exhausted head → old-style
  quarantined rows), deploy the old code, start one instance.
- **Restore of the forge:** stop every instance; restore; check open PRs with `--coord-state`; write a
  fresh random `epoch` into `policy.json`; start. A survivor of the restore cannot post or merge (epoch
  check).

## 11. Failure handling

Forge down: nothing to review. Coordination repository or `policy.json` unreadable: **fail closed**
(no claim, no post, no merge) and alert.

## 12. Telemetry and alerts

SQLite keeps `meta` counters only. Series gain `instance`. New: `reviewbot_candidates`,
`reviewbot_coord_claims_total{layer, result}`, `reviewbot_coord_prs{state=ambiguous}` and
`reviewbot_coord_heads{state=exhausted}`,
`reviewbot_coord_errors_total{op}`, `reviewbot_forge_parked`. Kind-wide alerts aggregate with
`max by (persona)`. New alerts: `ReviewbotCoordinationFailing`, `ReviewbotAmbiguousPR` (labels: repo,
PR, owner instance),
`ReviewbotExhaustedHead`, `ReviewbotMergedUnreviewedBase`, `ReviewbotForgeRateLimited`.

## 13. Rollout

0. **Gitea spike** — done (facts above). Remaining before code: repeat the ownership race with a
   reviewer bot's token; `git fetch` of `refs/pull/<n>/head` with that token.
0b. **GitHub spike** on a private scratch repository with a test App: concurrent tag object + ref
   creates (one ref, nonce readback, frozen payload gives the same SHA), `matching-refs` truncation,
   review POST with `line`/`side` and failure leftovers, pending-review rule, check runs + statuses on
   a repository with Actions and which commit PR checks attach to, `PUT /merge` with `sha` (409 on a
   moved head), App APPROVE vs required approvals, ETag 304, rate-limit headers with and without
   `Retry-After`, `git fetch refs/pull/<n>/head` with an installation token.
1. **Adapter refactor** (no behaviour change): every Gitea call behind `GiteaForge`; existing suites
   unchanged. Then `GitHubForge` and a forge contract test suite.
2. **Coordination** code; cut over each kind with its single instance; one week of metrics.
3. **Second claude instance:** pre-test with a second process on reviewer-1 against `primes-lab`;
   then a VM (blocked on a free LAN address in `docs/network-plan.md`).
4. **Follow-up:** retention (delete tags of PRs merged more than 7 days ago, via the adapter, absence
   confirmed by listing). Deferred: listings are per-PR prefixes, so growth never affects behaviour.

## Corner cases

| # | Situation | Handling |
| --- | --- | --- |
| 1 | Every instance receives the same event | Race on `a<N>`; readback picks one |
| 2 | Create answers 201/409/422/500/timeout | Readback decides |
| 3 | Create timed out and landed later | My nonce on readback → a claim is my lease; a right is finished at once |
| 4 | Owner crashes before `pub` | Lease expires → abandoned (charged); another instance claims |
| 5 | Owner crashes between `pubsent` and the answer | Ambiguous; operator: owner stopped, then a 1 h monotonic wait |
| 6 | Owner never sent (pre-send check failed) | Consumed + `pubfin`; void at once |
| 7 | Owner paused before sending | Self-fence (monotonic) → finish, no send |
| 8 | Review deleted after a 2xx | `pubfin` exists → `--force` voids at once |
| 9 | Failed POST left a PENDING review | PR ambiguous for this kind; operator cleanup under its own right deletes it |
| 9a | `pubfin` write fails after finishing | Consumed in-process, never sent; the write is retried for the process's life |
| 9e | A peer holds a right when I want to publish | Wait for it within my budget, then publish; else `rel.pub` |
| 9f | Stuck right (crash in the post window) | Blocks the PR for this kind until the operator; alert names PR and owner (stated residual) |
| 9g | Approval upgrade landed but `pubfin` missing | Landing evidence = APPROVED review by the bot |
| 9b | Two heads of a PR want to publish | Per-PR rights serialise them |
| 9c | Policy changed after a verdict was computed | Guard and verdict recomputed before sending; merge gate enforces the current guard |
| 9d | Legacy POST in flight at cut-over (`posting` row) | Exported as possibly sent (`pubsent`) |
| 10 | Push during a review | Head watcher aborts within ~60 s |
| 11 | Push race (claim on a stale head) | Watcher / pre-send head check stops it |
| 12 | Force-push back to a reviewed / ambiguous / exhausted head | Marker → done; tags persist → same state |
| 13 | PR closed, draft, reopened | Drop / `rel.closed`; reopened → states persist |
| 14 | Both kinds on one PR | Separate namespaces |
| 15 | Duplicate approval upgrades | Serialised by the per-PR right; no marker |
| 16 | Two instances merge at once | Head binding; benign 405/409 |
| 17 | An instance's seats or forge identity parked | It does not claim; others do |
| 18 | Rate limited mid-run | `rel.ratelimit`; the 40-claim window bounds churn |
| 19 | Poison PR crashing the process | Abandoned counts as charged → exhausted |
| 20 | Coordination repo or policy unreadable | Fail closed + alert |
| 21 | Duplicate instance names / restarts | Nonce decides; my-name foreign-nonce claim → `rel.restart` |
| 22 | Requeue while leased | Refused |
| 23 | Late outcome tags from an old owner | Counters only |
| 24 | Lease too short after acquiring | `rel.budget` |
| 25 | Clock wrong | Duplicate run or delay only; GitHub future `issued` rejected |
| 26 | Many reviews on a PR | Paginated to the end; a failed page fails closed |
| 27 | Policy changed | Loaded before every POST and merge; no stale copy exists |
| 28 | Base retargeted, same head | Merge gate requires `v1.cov` base = current base; post-merge re-read |
| 29 | Retarget between check and merge | Residual; post-merge re-read alert |
| 30 | Forge restore while instances run | Procedure stops them; epoch refuses survivors |
| 31 | Repo renamed or transferred | Repository id in names |
| 32 | Bot token revoked on one instance | Its creates fail → alert; others continue |
| 33 | Head A→B→A during diff | Local git diff by SHA |
| 34 | Git cache wiped or corrupt | Re-fetched |
| 35 | GitHub `mergeable: null` | Retry next sweep |
| 36 | GitHub zero CI / unpaginated checks | Not green; paginated |
| 37 | GitHub App approval does not count | Humans approve; bot does not merge |
| 38 | GitHub tag-object retry | Frozen payload → same SHA |
| 39 | Webhooks unreachable (GitHub, LAN) | Polling with ETags |
| 40 | Merge-queue repository | Review and approve only |
| 41 | Same repo name on both forges | Forge-qualified names |
| 42 | Legacy markers without coverage | Count as not full |
| 43 | Gitea invalid inline position (500) | Not definitive → ambiguous; positions pre-validated by `parse_hunks` |

## Critical files

- `ansible/roles/pr_reviewer/files/`: stdlib-only package — `reviewbot.py` (core: publication, work,
  head watcher, discovery, merge gate, CLI `--coord-state`, `--requeue [--force --owner-stopped]`,
  `--export-coord`, `--import-local`), `forge_gitea.py`, `forge_github.py` (App JWT via the `openssl`
  CLI). The jobs-table queue is removed; SQLite keeps `meta`.
- Role: `templates/config.json.j2` + `defaults/main.yml` (instance, forge endpoints, `coord_repo`,
  `lease_s` overhead, `post_margin_s` 90, `pub_grace_s` 600, `pub_void_min_age_s` 3600, `min_llm_s`
  120, `poll_s`); `reviewbot.service.j2` (`TimeoutStopSec=120`); `tasks/main.yml` (git, cache
  directory, `GIT_ASKPASS` helper, coordination-repo assertions, unique instance names);
  `group_vars/reviewers_<kind>.yml`; per-instance tokens in `reviewbot.sops.yaml`.
- `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` + tests.
- `scripts/tests/test_reviewbot_forge.py` (contract suite over fake Gitea and fake GitHub reproducing
  the measured quirks: 201 to a non-writer, 500 losers, late-landing creates, 422 races, pending
  review leftovers), `scripts/tests/test_reviewbot_coord.py`.
- `docs/runbooks/dev-workers.md`; docs spec `specifications/pr-reviewers/` (follow-up).

## Verification

1. Pure functions: publication and work state tables, tag names, marker + `v1.cov` parsing, merge gate.
2. Deterministic interleavings for corner cases 2–9, 11, 21–24, 30 with step-controlled instances.
3. Randomized simulation: 3 instances, fake LLM, both fake forges, injected pauses, kill -9, SIGTERM,
   lost responses, late-landing creates, pushes, closes. Invariants: never two review markers per
   (kind, head); every open head ends done, ambiguous or exhausted; no merge without every persona
   clean at the current head.
4. Live: cut-over metrics for a week; two processes of one kind on 10 simultaneous `primes-lab` PRs,
   then kill one mid-review.

<!-- codex-review-status: complete -->
