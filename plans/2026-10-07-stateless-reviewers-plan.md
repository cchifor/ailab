# Stateless reviewers: several instances per kind, coordinated only through the forge (Gitea or GitHub)

## Context

reviewbot runs one instance per kind (`claude` on reviewer-1, `codex` on reviewer-2). Each keeps its
queue, failure memory and round history in a local SQLite file, so a second instance of the same
kind would duplicate model runs and miscount convergence rounds. Measured over 7 days (2026-10-07):
claude p90 enqueue-to-posted 6.8 min, codex 0.8 min; merges wait for both.

**Goal.** N instances of one kind on different machines; at most one instance of a kind reviews a
pull request at a time; instances stateless; **the forge is the only synchronization point** (PR
reviews and tags in a coordination repository on the same forge). Both **Gitea** and **GitHub**
(github.com and GitHub Enterprise Server) must be supported (section F). No shared database. A local SQLite file MAY stay for
telemetry; losing it must never change review behaviour.

Precisely: **posting exclusivity is absolute** (at most one review per kind per PR head, ever);
**execution exclusivity is best effort** (one model run per kind per PR, except a bounded overlap of
about 60 s after a push while the stale run aborts) [X6].

**Gitea behaviour, verified on 2026-10-07** (`cchifor/primes-lab`, scripts `scratchpad/tag_race*.py`):

| Fact | Evidence |
| --- | --- |
| `POST /repos/{o}/{r}/tags` is create-if-absent under concurrency: one git tag object results | 30-way × 5 rounds and 20-way × 25 staggered rounds |
| **The `201` does not identify the owner.** In 25/25 staggered rounds the git tag's message belonged to a caller that received `500` | Spike 2 |
| Sequential duplicate → `409 tag already exists` | Spike 2 |
| Tags written by a `500` caller have no release row: `DELETE /tags/{name}` → 404; only `git push :refs/tags/<name>` removes them | Spike 2 |
| `GET /git/refs/tags/<prefix>` → `{ref, url, object{type: tag, sha}}` only, no message | Spike 2 |
| `GET /git/tags/{sha}` → message + tagger `Gitea <gitea@fake.local>` + `date` (server time). The tagger is **not** the API user | Spike 2 |
| `Date` header on 2xx and 404; `cf-cache-status: DYNAMIC` | Spike 2 |
| Readback by nonce yields exactly one owner per name | Spike 3: 15/15 staggered 20-way rounds |
| Tag messages round-trip exactly (quotes, backslashes, Unicode, newlines, trailing newline) | Spike 3 |
| `tagger.date` equals the response `Date` (0.0 s) | Spike 3 |
| **A create whose client timed out still lands** (5/5, visible 0.2 s later) | Spike 3 |
| Prefix listing complete at 300 refs; narrower prefixes exact | Spike 3 |
| Delete racing create: consistent end states; a `201` create can be deleted by a concurrent delete | Spike 3 |
| **No immutable PR diff over the API**: `GET /compare/{a}...{b}` returns no files; the web `.diff` needs a browser session (HTML login page); `GET /git/commits/{sha}.diff` covers one commit only | Spike 3 |
| **A review POST with an invalid inline position returns 500 and leaves an empty PENDING review; the next POST by the same account absorbs it** | Spike 3 (temporary PR primes-lab#18, closed) |

Spikes ran on Gitea **1.26.1** (`GET /version`) with the `workstation-bot` account; Phase 0 repeats the
ownership races with a reviewer bot's token. Spike 3: `scripts/spikes/reviewbot-coord-gitea.py`.

Reviews of this plan: round 1 by Fable (`…-review-r1-fable.md`) and Codex
(`…-review-r1-codex.md`), round 2 by both (`…-review-r2.md`), the reviewer bots on ailab#1127 and a
forge-portability round (`…-review-r3.md`) and its review by Fable and Codex (`…-review-r4.md`);
dispositions are referenced as `[F<n>]`, `[C<n>]`, `[R<n>]`, `[X<n>]`, `[B<n>]` and `[P<n>]` below.

Throughout sections 1–15, "Gitea" names the reference forge; every operation goes through the forge
adapter of section F, which states the GitHub equivalent.

## Design principle

**Safety comes from one irrevocable publication right per (kind, PR head). Everything else is
efficiency.** Leases, attempt counters, clocks and cleanup only decide *who spends model time*;
if any of them is wrong, the cost is a duplicated model run or a delay, never a second review.
This removes every clock- and pause-dependent safety argument from round 1 [C1, C2, C8, C9, F2, F3].

## F. Forge portability (Gitea and GitHub)

### F.1 Principle

The review core (coverage planning, prompts, verdicts, rounds, seats, publication logic, claim
state machine) is forge-agnostic. Everything that touches a forge goes through one adapter
interface; there is one adapter per forge type, and every repository is addressed as
`<forge>:<owner>/<name>` (`gitea:cchifor/ailab`, `github:cchifor/x`). The coordination repository
lives **on the same forge as the PRs it coordinates** (one per forge), so each forge is its own
synchronization point and no state crosses forges. One instance may serve repositories on several
forges.

### F.2 Adapter operations

| Operation | Gitea | GitHub |
| --- | --- | --- |
| Auth | PAT, `Authorization: token` | **GitHub App per kind** (installation token, auto-refreshed) or machine-user PAT; `Authorization: Bearer` |
| Read PR (state, draft, head, base ref, author, labels, mergeable) | `GET /repos/{o}/{r}/pulls/{n}` | same path; `mergeable` may be `null` (computing) → treat as "not yet", retry |
| List open PRs | `?state=open&limit=50&page=` | `?state=open&per_page=100` + `Link` pagination; conditional `If-None-Match` (a 304 does not count against the rate limit) |
| List reviews (markers) | `/reviews?limit=50&page=`, fail closed at the page cap | `/reviews?per_page=100` + `Link`; no cap; ETag |
| Immutable review diff | **local git**: fetch base ref + `refs/pull/<n>/head`, verify the head SHA, `git diff <merge_base> <head_sha>` (section 7) | same local-git path; `GET /compare/{base_sha}...{head_sha}` with the diff media type is an optional fast path |
| Diff too large | size cap on the computed diff | same (local git has no server-side limit); the compare fast path's 406 / 5xx simply falls back to local git. Never `/pulls/{n}/files`, which follows the mutable head [P4] |
| Post review | `POST …/reviews {commit_id, event: APPROVED\|COMMENT, body, comments[{path, new_position\|old_position}]}` | `POST …/reviews {commit_id, event: APPROVE\|COMMENT, body, comments[{path, line, side: RIGHT\|LEFT}]}`; one invalid comment fails the whole request with 422 (definite, not ambiguous) |
| Pending reviews (X2) | list `state=PENDING` by the bot | GitHub allows one pending review per user per PR. reviewbot always posts with an `event`, so it never creates a pending review itself; whether a failed POST can leave one is a Phase 0b item. On both forges: delete only a pending review whose id this attempt received; any other pending review by the bot → do not post, ambiguous (section 4 step 3) [P6] |
| CI green | combined status `success` (non-empty) | **At least one** check run or status must exist [P5]. Check runs of the head, paginated (`per_page=100`, `filter=latest`), every one `completed` with `success`/`neutral`/`skipped`; statuses: combined `success`, or zero statuses **only if** check runs exist. Required check names from `GET /repos/{o}/{r}/rules/branches/{branch}` (and branch protection when readable) must each be present and green. Which commit GitHub attaches PR checks to (head vs test merge commit) is verified in Phase 0b [P5] |
| Merge | `POST …/merge {Do: merge, head_commit_id}` | `PUT …/merge {sha: head, merge_method}` (per-repo `merge_method`); 405 not mergeable, 409 head changed. Repositories using a **merge queue** are out of scope: the bot reviews and approves, a human or the queue merges |
| Webhook verification | `X-Gitea-Signature` = hex HMAC-SHA256 | `X-Hub-Signature-256` = `sha256=` + hex HMAC-SHA256 |
| PR events | `pull_request`, `_sync`, `_label`, `_review_request` | `pull_request` with `action` in `opened`, `reopened`, `synchronize`, `ready_for_review`, `edited` (base change), `labeled`, `review_requested`, `converted_to_draft`, `closed` [P11] |
| Policy / fence / epoch files | contents API | contents API |
| **Create-if-absent ref with ownership proof** | `POST /tags {tag_name, target, message}` → status ignored | `POST /git/tags {tag, message, object, type: commit, tagger}` **once per claim** with a frozen payload (fixed `tagger.date`), its SHA kept for every retry; then `POST /git/refs {ref: refs/tags/<name>, sha}` → 201, or 422 "Reference already exists" (the normal race outcome, not an error). If a retry finds the object gone (422 "Object does not exist") it is re-created from the frozen payload, giving the same SHA [P1] |
| Ownership proof (both forges) | read the exact ref (`GET /git/refs/tags/<name>`), then the object it points at (`GET /git/tags/{sha}`): **owned iff that object's message nonce is mine**. On GitHub, target SHA == my object SHA is a fast path to the same answer | `GET /git/ref/tags/<name>` (singular, exact; `matching-refs` is a string prefix match) then `GET /git/tags/{sha}` [P1, P10] |
| Single-writer tags without a message (outcomes, operator) | API tag create | lightweight ref to the coordination repo's root commit: one call [P9] |
| List refs by prefix | `GET /git/refs/tags/<prefix>` | `GET /git/matching-refs/tags/<prefix>`; no documented pagination, so Phase 0b records the truncation threshold and section 7's fail-closed rule uses it [P10] |
| Delete a ref (janitor) | `git push --delete` (API delete 404s for tags without a release row) | `DELETE /git/refs/tags/<name>`; confirm absence by listing |
| Server clock | `Date` header; lease start = tag object `tagger.date` (server-set) | `Date` header; `tagger.date` is **client-supplied** → the claimant writes its `server_now`; others apply the 300 s future-sanity bound. Acceptable for **work leases** (efficiency only). Publication gates never rely on it (section 11 uses operator-observed time) [P2] |
| Bot identity for markers | login `reviewer-<kind>` (+ user id) | App: `<app-slug>[bot]`, `user.type == "Bot"`; machine user: its login. Per-kind config `bot_login` **and** `bot_user_id`; `marker_of` compares the id (renames do not orphan markers) [P12] |

### F.3 GitHub-specific decisions

- **Identity: one GitHub App per kind** (`reviewbot-claude`, `reviewbot-codex`): permissions
  pull requests write; **contents read on every reviewed repository and write wherever the bot
  merges** (merging, and creating tags/refs in the coordination repository, need `contents: write`;
  one installation grants one permission set to all its repositories); checks and statuses read;
  metadata read; administration read only if classic branch protection must be read (rulesets need
  only metadata) [P3]. Its own rate-limit budget per installation, no human seat, and `[bot]` logins
  that no user can impersonate. All instances of a kind share the App's private key
  (SOPS) and mint installation tokens independently. Machine users remain a fallback.
  **To verify in Phase 0:** whether an App's APPROVE counts toward required approvals under the
  target repositories' branch protection or rulesets; if not, approvals are left to humans (the bots
  still merge where no human approval is required).
- **Intake without inbound reachability.** GitHub cannot reach the LAN reviewer VMs. Default:
  **polling** — every instance lists open PRs with conditional requests every `poll_s` (60 s,
  jittered); unchanged lists answer 304 and cost no rate limit. Optional: webhooks through a public
  ingress (Cloudflare tunnel to the receiver) with `X-Hub-Signature-256`; polling stays as the
  safety net, exactly like the reconciler today.
- **Rate limits are shared by all instances of a kind** (one App installation, or one user's tokens).
  The adapter tracks `X-RateLimit-Remaining`/`Reset` and slows discovery when below 20 %. A
  secondary-limit 403/429 parks **this instance's use of the forge identity** (each instance only
  sees its own refusals): wait `Retry-After` when present, otherwise exponential backoff from 60 s
  to 15 min [P8]. A 304 is exempt from the **primary** limit only. Content-creating calls per review
  on GitHub: work claim 2 (object + ref), publication 3 (object + ref + `pubsent`), review 1,
  outcomes 2–3 → about 9; at N = 3 instances and 30 reviews per hour per kind that is ≈ 270 per hour,
  under the secondary ceilings (80 per minute, 500 per hour) but not by a wide margin; the adapter
  counts its own content calls and defers claims above 400 per hour [P8].
- **Base retarget** arrives as `pull_request` `edited` with `changes.base`; it invalidates the base
  ref check (section 8) exactly as on Gitea.
- **Fork PRs**: head commits live in the fork but are reachable in the base repository's network;
  compare `base_sha...head_sha` works. Reviews and merges are unchanged.
- **Draft PRs, labels (`no-automerge`), HTML comment markers** behave as on Gitea (markers are
  preserved, unrendered).
- **Body limit**: 65,536 characters per review body; the adapter truncates the summary and coverage
  table before the marker, never the marker.

### F.4 Portability invariants

1. The core MUST NOT call a forge API directly; only the adapter does.
2. Safety MUST rest only on the create-if-absent ref primitive with ownership proven by readback,
   which both forges provide (with different proofs: message nonce on Gitea, object SHA on GitHub).
3. Every status-code interpretation that affects safety MUST be adapter-local and covered by the
   adapter's fake-forge tests.

## Approach

### 1. Coordination repository

`cchifor/reviewbot-coord`: private, `auto_init` (one commit = the tag target), **no push mirror,
no webhooks, no Actions, no protected-tag rules (on GitHub: no tag rulesets)**, write access for `reviewer-claude`,
`reviewer-codex` and the owner only. Created once by hand (runbook); the Ansible role asserts
private, not a mirror, empty push-mirror list (owner token). Repo ACL is the only authentication of
tags: the tagger field is always `Gitea`, so it proves nothing [F16, C29].

### 2. Tag names and messages

Flat names (the delete and read endpoints mishandle `/`), validated by `check-ref-format` rules
before use, at most 120 characters:

```
rb1.<kind>.r<repo_id>.p<pr>.<head_sha40>.<suffix>
```

`<repo_id>` is the forge's numeric repository id (stable across renames). Prefix listings always end in `.` so
`p11.` never matches `p112.` [F17].

| Suffix | Kind of tag | Created by | Meaning |
| --- | --- | --- | --- |
| `pub<G>` | **publication right**, create-if-absent | the instance about to POST | Only the owner of `pub<G>` may post this kind's review for this head. G starts at 1 |
| `pubok<G>` | outcome, single writer | the `pub<G>` owner | Optional speed-up: the review POST returned success |
| `pubsent<G>.<t>` | outcome, single writer, **mandatory** | the `pub<G>` owner | Written immediately before sending the POST, with the owner's server time `t`. If this write fails, the owner does not send [P0] |
| `pubx<G>` | outcome, single writer | the `pub<G>` owner | This owner will never POST under `pub<G>` again (failure, abandonment) [B4] |
| `pubseen<G>.<t>` | operator, single writer | `--coord-state` / `--requeue` | The operator tool's first observation of an ambiguous `pub<G>`, with **its own** clock [P0, P2] |
| `mergeint.<base>.<t>` | outcome, single writer | the merging instance | Merge intent: validated base ref (escaped) at time `t`; audited after the merge [P7] |
| `pubvoid<G>` | operator, single writer | `--requeue --force` | The operator confirmed no review from `pub<G>` landed; publication continues at `pub<G+1>` |
| `a<N>` | **work claim**, create-if-absent | an instance starting attempt N | Lease for model work (efficiency only) |
| `a<N>.fail.<t>` / `a<N>.failt.<t>` | outcome, single writer | claim owner | Charged ordinary / deadline-class failure at server time `t` |
| `a<N>.rel.<t>.<why>` | outcome, single writer | claim owner | Released without charge (`ratelimit`, `shutdown`, `moved`, `closed`, `budget`, `restart`) |
| `a<N>.done.<t>` | outcome, single writer | claim owner | The attempt published (or skipped with a marker); frees the work lease at once [R11] |
| `cut<N>` | operator, single writer | `--requeue` | Attempts numbered below N are ignored for caps; numbering continues above the high-water mark [C4] |

One global tag and one file (not per head): `rb1.<kind>.mode.forge.<t>` (mode fence, section 12) and
the `epoch` file (a random id, rewritten by the restore procedure, section 14) [R6, R10, B8, P14].

Outcome and operator tags put their timestamp and reason in the **name**, so `head_state` needs no
message reads for them [F7]. Only `pub<G>` and `a<N>` carry a message:

```json
{"v": 1, "kind": "codex", "instance": "reviewer-2b", "nonce": "<boot_id>-<pid>-<uuid4>",
 "health": "http://192.168.0.25:8477/healthz", "repo": "gitea:cchifor/ailab", "pr": 1112,
 "head": "<sha40>", "lease_s": 1200, "cfg": "<policy hash>", "issued": "<server time, GitHub only>"}
```

`health` is absent on migrated rights, which have no live owner [P13].

### 3. Ownership: read back, never trust the status code

`create_owned(name, msg)`: POST the tag; **whatever the status** (201, 409, 500, timeout), list the
exact ref and read its object with `GET /git/tags/{sha}`. Owned **iff** the message's `nonce` equals
this claim's nonce. The nonce is fixed per (kind, head, tag name) for the life of the process and is
unique across processes (boot id + pid + uuid), so duplicate instance names cannot share ownership
[F1, F6, C3]. Not found after a 5xx/timeout → poll the readback for up to `post_margin_s`; still not
found → not owned for now. A tag that lands later with **my** nonce is mine: a work claim simply
becomes my lease; a publication right that I discover later may be used for **one** POST if I have
not posted under it (I re-run the review if I no longer hold its body) [R2].
Persistent non-race failures (401/403, a 422 that is not "already exists", or 5 consecutive
not-found) increment `reviewbot_coord_errors_total{op="create"}` and alert [F14, P9].

### 4. Publication protocol (the safety mechanism)

In `post_review`, after the existing checks (head unchanged, `posting-disabled` absent, no marker of
this kind at this head):

1. G = 1 + highest `pubvoid<G'>` (or 1). If `pub<G>` exists and is not mine → **do not post**
   (someone else holds the right; their review is landing or is ambiguous). Release work claim.
2. `create_owned(pub<G>)`. Not owned → do not post.
3. Immediately before the POST: re-list markers — **no authenticated marker of this kind at the
   head** (else `pubx<G>`, `a<N>.done`, stop) [B1, P0]; re-read `pubvoid<G>` (absent), the epoch
   record (unchanged since start) and the mode fence (matches my mode) [R1, R6, R10]; list this account's **PENDING** reviews on
   the PR: none, or only ones this attempt created (deleted first). Any other pending review by the
   bot → do not post, `pubx<G>`, the head is ambiguous: a failed earlier POST leaves an empty pending
   review that the next POST by the same account absorbs (spike 3, Gitea 1.26.1) [X2].
4. Write `pubsent<G>.<t>`; if that write fails, write `pubx<G>` and do not send [P0]. Then POST the
   review — **at most once per held right** (an in-process set of consumed rights).
   Success → `pubok<G>` and `a<N>.done.<t>` (best effort). Exception → `pubx<G>` (best effort) and
   nothing more: the head is now ambiguous by construction (below).

`head_state` of the publication layer:

| Observed | State |
| --- | --- |
| Authenticated marker of this kind at the head | **done** |
| `pub<G>` (current G) exists, no marker, `pubok<G>` absent, `pub<G>` older than `pub_grace_s` (600) | **ambiguous** — never retried automatically; alert; operator `--requeue --force` writes `pubvoid<G>` after checking the PR |
| `pub<G>` exists, younger than `pub_grace_s` | **publishing** — skip |
| `pubok<G>` exists, no marker visible, younger than `pub_grace_s` | **publishing** (marker read lag) |
| `pubok<G>` exists, no marker visible for longer than `pub_grace_s` (the review was deleted, or lost in a restore) | **ambiguous** — alert [R3] |

A crash between `pub<G>` and the POST, a POST that timed out, and a failed `pubok` write all end in
*ambiguous* — today's quarantine semantics, now shared by every instance [F3, C2]. A stalled owner
that wakes after another instance took over its work claim cannot post: it cannot win `pub<G>` [C1].
Two reviews of one kind on one head are impossible while `pubvoid<G>` is written only after the
owner of `pub<G>` is proven gone (section 11) [R1].

The approval upgrade in `maybe_merge` is **not** locked and carries **no marker**: it is an
APPROVED review that satisfies branch protection, not a verdict. A rare duplicate approval is
harmless, and the review-marker invariant ("at most one marker per kind per head") holds literally.
This removes the lease-less `appr` lock and its stuck state [F8, C11, C12, R4].

### 5. Work claims (efficiency, not safety)

Goal: one instance of a kind runs the model for a PR at a time, failures are counted across
instances, hopeless heads quarantine once.

**Clock.** `server_now = Date(last response) + (monotonic_now − monotonic_at_that_response)`;
a response without `Date` (Cloudflare error page) → fail closed for that cycle [F13, C9]. Lease start
is the claim tag's `tagger.date` (server-assigned), not a client field; a `tagger.date` more than
300 s ahead of `server_now` is treated as expired [F7, C8]. Clock errors can only cause a duplicate
model run or a delay (publication protects the post).

**State of a head's work layer** (pure function over the tag list, reading messages only for
outcome-less `a<N>` claims):

- `hw` = highest attempt number ever created (before any `cut` filtering) [C4].
- Counting starts at the latest `cut<K>` (attempts `< K` ignored).
- `charged` = `.fail` + `.failt`; `timeouts` = `.failt`; `released` = `.rel` excluding `moved`/`closed`;
  `abandoned` = outcome-less claims whose lease expired.
- **exhausted** if `timeouts >= 2` or `charged >= 5` or `abandoned >= 3` or `released >= 10`
  or `hw - K + 1 >= 40` (all claims since the last cut, whatever the reason, bounding `moved`/`closed`
  churn) [F4, C7, C28, R8]. Exhausted heads are skipped and counted in a gauge; `--requeue` writes `cut<hw+1>`.
- A claim with a `.done` outcome is finished and never leased [R11].
- **leased** if the newest outcome-less claim's lease (`tagger.date + lease_s`) has not expired;
  if that claim's message carries **my instance name but not my nonce**, it belongs to my dead
  previous process: write `.rel.<t>.restart` and treat as released [F6].
- Otherwise **claimable** at `not_before` = last charged outcome time + `min(3600, 60·2^charged)`;
  next attempt = `hw + 1`.

Late outcomes (an old owner writing `.failt` after a newer claim started) only change counters; they
never stop a current owner, whose post is still governed by publication [C6].

**Claim cycle** for a candidate `(repo, pr)` that is due (`next_check <= server_now`) [F15]:

1. Skip if this instance cannot serve (all seats parked, `inhibit` or `posting-disabled`).
2. `pr_ok`: closed or draft → drop; else head H (and base SHA, section 9).
3. Publication state of H: done → drop. `pub<G>` carries **my** nonce and is not consumed (a create
   that landed late, section 3) → run the review and publish under the held right **without a work
   claim and regardless of caps** (the right, not a lease, authorises it) [B7, P13]. Otherwise ambiguous → drop (counted); publishing → retry later.
4. Work state of H: exhausted → drop; leased → `next_check` = lease end; not yet claimable →
   `next_check` = `not_before`.
5. **Another head of this PR leased** → `next_check` = now + 60 s (re-checked, so an early
   `.done`/`.rel` is seen within a minute) [R11].
6. `create_owned(a<hw+1>)`. Not owned → `next_check` = now + 60 s.
7. Owned: budget = `tagger.date + lease_s − post_margin_s − server_now`. Budget below
   `min_llm_s` (120) → `.rel.<t>.budget` (counts toward `released`) [C10]. Else run `review_job`
   with `llm_timeout_s = min(llm_timeout_s, budget)`.

**Outcomes** (single-writer tags, best effort; a failed write just lets the lease expire):

| Event | Tag |
| --- | --- |
| Review posted (publication succeeded) | `pubok<G>` + `a<N>.done.<t>` [R11] |
| Skip notice posted | `a<N>.done.<t>` (it went through publication too) |
| `RateLimited` | `.rel.<t>.ratelimit` |
| Deadline / `ExpensiveFailure`, **including running out of lease before posting** | `.failt.<t>` [F4] |
| Ordinary failure | `.fail.<t>` |
| Head moved / PR closed or draft | `.rel.<t>.moved` / `.rel.<t>.closed` |
| SIGTERM during the model run | abort, `.rel.<t>.shutdown`, exit (systemd `TimeoutStopSec=120`) |
| SIGTERM after `pub<G>` was won | finish the POST (≤ 60 s) |

### 6. One reviewer per PR across pushes

Step 5 of the claim cycle waits while another head of the PR is leased. To keep that wait short, the
owner runs a **head watcher** thread during the model run: `pr_ok` every 60 s; on a head move,
close or draft it aborts the run (router seat: stop reading the stream; CLI seat: terminate the
`Popen` child) and writes `.rel.<t>.moved`. Worst-case wait for a new head ≈ 60 s plus abort time,
not a lease [F12]. The residual overlap (a claim on a stale head created just after a push) is
harmless: the stale owner's watcher or its pre-post `pr_ok` stops it, and publication is per head.

### 7. Discovery without a leader

Every instance lists open PRs of every allowlisted repository every `reconcile_s` (±20 % jitter),
plus immediately at start, and adds `(repo, pr)` candidates; webhooks add candidates too. There is
**no sweep leader** [F9, C22, C23]: discovery must not depend on another instance. `maybe_merge`
runs on every instance (idempotent; `head_commit_id`). API cost is ~N × today's sweep, acceptable for
N ≤ 3; `reconcile_s` can be raised per instance if needed.

Discovery isolates errors **per PR**: one PR that fails to parse or read is logged and counted, and
the sweep continues with the next [X4].

**Immutable diff (both forges).** The review diff is computed locally from exact commits, because
Gitea 1.26 has no API for a diff between two fixed commits (spike 3) and one code path is simpler
than two. Each instance keeps a **disposable** bare partial clone per repository
(`/var/cache/reviewbot/git/<forge>/<owner>/<name>.git`, `--filter=blob:none`): fetch the base ref and
`refs/pull/<n>/head` (Gitea and GitHub both publish it), verify the expected head SHA is present
(`git cat-file -e`), compute `merge_base = git merge-base <base_tip> <head_sha>`, then
`git diff --no-color <merge_base> <head_sha>` (blobs fetched lazily). A head that moved is detected
(SHA absent or different) and supersedes the job; an A→B→A move cannot substitute content because
every step names SHAs [X1, P4]. Deleting the cache only costs a re-fetch, so the instance stays
stateless. Git authenticates through `GIT_ASKPASS` reading the token file (installation token on
GitHub), never argv. The raw-size bound applies to the computed diff; the GitHub compare API remains
an optional adapter fast path, never the authority.

`iter_reviews` **fails closed** when the tenth page is full (more than 500 reviews) instead of
silently truncating [C20]. The prefix listing is checked for completeness in Phase 0; if it can be
truncated, `head_state` fails closed when the result size equals the page cap [C31, C32].

### 8. Markers, rounds and the merge gate

- **Coverage** goes in a **separate** comment placed *before* the canonical marker:
  `<!-- review-bot:v1.cov persona=<p> head=<sha40> base=<base ref> coverage=<c> -->`. The canonical marker and
  `MARKER_RE` are unchanged, so older code and rollbacks still parse every review [F5, C15].
- **Rounds** = distinct heads of the PR with a canonical marker of this kind, verdict in
  {clean, findings}, **and** a `v1.cov` comment saying `full`, + 1. Markers without a coverage
  comment (everything posted before cut-over) count as **not full** — conservative: a long-lived PR
  may be reviewed one round stricter once [C14, F11]. In `local` mode `review_round` keeps using the
  jobs table, so Phase 1 is a true no-op.
- **Strictest marker wins** in `persona_verdicts`: any authenticated marker of a persona at the head
  with `findings`/`partial`/`skipped` beats `clean` [F2]. With publication, two review markers per
  (kind, head) cannot occur; approval upgrades carry no marker and never enter `persona_verdicts`
  [B9].
- **Base ref.** A marker counts for the merge gate only if its `v1.cov` comment's `base=<ref>`
  equals the PR's current base **ref** (not SHA, which advances on every merge to the target).
  Markers without `v1.cov` (pre-cut-over) are accepted. A retargeted PR therefore waits for a new
  head [R12, C21]. **Residual [B2]:** neither forge's merge API accepts an expected base, so a
  retarget timed between the final check and the merge call (about a second) is not prevented. The
  base ref is re-read immediately before the merge call, and the merging instance first writes
  `mergeint.<base>.<t>` for the head. The audit runs on every instance's sweep over PRs that carry a
  `mergeint` and were merged in the last 7 days (discovery lists recently closed PRs for this): a
  merged base differing from the recorded one raises `ReviewbotMergedUnreviewedBase` (critical) for
  a human to revert. A lost merge response or a restart cannot skip the audit, because the intent is
  in the coordination repository [P7]. This is a
  detection control, stated as such; a server-enforced fence is not available on Gitea or GitHub.
- **Boundary.** Merges read verdicts and then merge with `head_commit_id`; only humans can change a
  bot verdict at a head in between (publication forbids a second bot review), and operator edits of
  bot reviews are out of scope [X5].
- **Merge policy lives in Gitea.** The coordination repository holds `policy.json` (merge personas,
  merge authors, per-repo authors, unattended authors, guarded paths, caps). Every instance re-reads
  it **immediately before each merge** (and each sweep) [R7]; a local config whose kind-level policy hash differs from it **refuses to merge, claim
  or post** and alerts (`ReviewbotPolicyMismatch`). Changing policy = one commit to `policy.json`
  (owner only) followed by the Ansible rollout; instances on the old config stop mutating until they
  are updated, which is the safe direction [C19, C32]. Claims carry the hash (`cfg`) for diagnosis.
  Kind-level Ansible values live in `group_vars/reviewers_<kind>.yml`; the role renders the same
  policy into `config.json` and asserts it equals the repository file.

### 9. Review identity includes the base

The `v1.cov` comment carries `base=<base ref>`, and the merge gate requires it to match the PR's
current base ref (section 8). Re-reviewing a retargeted PR on the same head is not automatic: the
author pushes a new head (an empty commit is enough). [C21, R12]

### 10. Retention (janitor)

Every instance, once a day with random jitter: for each PR **merged** more than 7 days ago (merged
PRs can never be reviewed again), delete all its coordination tags through the adapter (Gitea: `git push --delete`; GitHub:
`DELETE /git/refs`) in batches of 100, continuing past failures and confirming absence through the refs listing (an API 404
does not prove a tag is gone) [X3]; 401/403 alert. Nothing else is ever
deleted: closed-unmerged PRs (reopenable), old heads (force-push back) and every `pub`/`pubvoid`/
`cut`/outcome tag of an open PR stay. Concurrent janitors are safe (idempotent deletes of tags no
one will create again) [F10, C2, C24–C27]. Growth is bounded per **cut window**: at most 40 work claims per head between operator cuts
(`hw - K + 1 >= 40` → exhausted), plus their outcomes [B5].

On Gitea the janitor authenticates to git through `GIT_ASKPASS` reading `/etc/reviewbot/pat`, never
a URL or argv token; on GitHub it uses the adapter's API token.

### 11. Operator actions

- `--coord-state <repo> <pr>`: print publication and work state of every head.
- `--requeue <repo> <pr>`: current head exhausted → write `cut<hw+1>`. Refuses if leased.
- `--requeue <repo> <pr> --force`: current head ambiguous → write `pubvoid<G>` only when **all** hold:
  1. no marker of this kind is visible at the head (re-listed by the tool);
  2. the owner can no longer **send** under `pub<G>`: it wrote `pubx<G>`, or the health URL recorded
     in the `pub<G>` message (host:port) answers with a different boot id or pid, or the operator
     passes `--owner-stopped` after stopping that instance, or the right is migrated (no `health`,
     instance `migrated`). An unreachable owner without `pubx` → refuse [B3, B4, B6, P13];
  3. **never sent** (no `pubsent<G>`): nothing can still commit → void allowed now. **Possibly sent**
     (`pubsent<G>` exists): a POST may still be in flight at the forge. Void only when both
     `now − t(pubsent) ≥ pub_void_min_age_s` (3600) and the tool's own `pubseen<G>` record (written on
     its first observation, operator clock) is at least as old. Forges finish or abort a request
     within minutes, so this is an operational precaution with two independent clocks, not a
     correctness proof; the remaining exposure is a forge that commits a request more than an hour
     after receiving it [B1, P0, P2].
  A `pubok<G>` does not block (the review may have been deleted). `pub_void_min_age_s` is a config
  key (default 3600). Voiding also **deletes the bot's PENDING reviews on the PR** (left by a failed
  POST, spike 3), after the checks above, so the next publisher does not stop on them again [X2].
- Kill switches unchanged.
- The Ansible role asserts that instance names are unique per kind, and an instance refuses to start
  when a live claim in the coordination repo carries its name with another health URL [B3].

### 12. Modes, migration and rollback

- Config `coordination: local | forge` (default `local`).
- **Mode fence.** Cut-over creates `rb1.<kind>.mode.forge.<t>` in the coordination repo. Code from
  Phase 1 on checks it at start, every sweep **and immediately before every POST and merge** [R6]: a `local`-mode instance that sees the fence refuses
  to post or merge and alerts; a `forge`-mode instance requires it. Old (pre-Phase-1) code cannot
  run once Phase 1 is everywhere [C18].
- **Forward (`local` → `forge`), per kind:** stop the instance (drain), run `--export-coord`
  (each local `quarantined` row: `ambiguous POST` → `pub1` with nonce `migrated-<uuid>` and instance
  `migrated`, no `pubok` → ambiguous [B6]; exhausted → matching `.fail`/`.failt` tags), create the mode fence, start in `forge` mode
  [C16, F5].
- **Backward:** stop all instances of the kind, run `--import-local` (every `pub` without a marker,
  whatever its age, and every exhausted head → local `quarantined` rows), delete the mode fence,
  start one instance in `local` [C17, R5].
  Not a config toggle.

### 13. Telemetry

SQLite keeps only `meta` for counters. Series gain an `instance` label. New:
`reviewbot_candidates`, `reviewbot_oldest_candidate_age_seconds`,
`reviewbot_coord_claims_total{layer=work|pub, result=owned|lost|error}`,
`reviewbot_coord_heads{state=ambiguous|exhausted}` (from each instance's own observation; alert uses
`max by (persona)`), `reviewbot_coord_errors_total{op}`, `reviewbot_config_info`.
Alerts become per kind where the meaning is kind-wide (backlog, ambiguous/exhausted heads, all
instances' seats exhausted, stalled) and stay per instance for liveness. New:
`ReviewbotCoordinationFailing`, `ReviewbotAmbiguousHead`, `ReviewbotPolicyMismatch`,
`ReviewbotModeFenceViolation`. promtool tests both ways.

### 14. Failure of the coordination path, and Gitea restores

Gitea down: nothing to review. Coordination repository unreachable while PRs work: **fail closed**
(no model run without a work claim, no post without a publication right) and alert.

**Restore procedure** (runbook) [R10, C36]: stop every reviewer instance first; restore; reconcile
(`--coord-state` on open PRs); write a **fresh random epoch id** (uuid4) to the coordination
repository's `epoch` file (contents API, update conditioned on the file's current blob SHA); start the
instances. Each instance records the epoch id at start and refuses to post or merge when the file's
id differs (checked immediately before each POST and merge). A random id cannot repeat or sort
wrong, whatever the restored state or the operator's clock [R10, B8, P14].

### 15. Rollout

0. **Spike (no code).** Create `cchifor/reviewbot-coord`; with a **bot** PAT (new token,
   `write:repository`): staggered 30-way races × 50 confirming "exactly one git object, readback
   decides"; `git/refs/tags/<prefix>` with 300 matching refs (pagination/truncation);
   `/git/tags/{sha}` message round-trip with quotes, backslashes, Unicode, newlines; `tagger.date`
   is server time (spike 3: yes); `git push --delete` of API-created tags with the bot PAT; concurrent delete +
   create of one name; protected-tag rules absent; side effects per tag (activity feed rows,
   notifications, indexer) acceptable; Gitea version recorded [F17, C30, C31]; how long after a client
timeout a tag can still appear [R2]; the immutable compare-diff endpoint [X1]; pending-review reuse
per (account, PR) and its cleanup [X2]; a Gitea restart during a create burst, **only in an approved
maintenance window** [R9, C30].
0b. **GitHub spike** on a private scratch repository with a test App: concurrent `POST /git/tags` +
   `POST /git/refs` races (exactly one ref, owner = target SHA; statuses observed);
   `matching-refs` pagination at 300 refs; tag object `tagger.date` handling; review POST with
   `line`/`side`; pending-review rule; compare-diff with the diff media type, its size limit and the
   compare-JSON fallback (300-file cap); check runs + statuses rollup on a repository with
   Actions, and **which commit** PR checks attach to; merge with `sha` and 409 on a moved head;
   whether an App APPROVE counts toward required approvals; ETag 304 on PR and review lists;
   rate-limit and secondary-limit headers (with and without `Retry-After`); whether a failed review
   POST can leave a pending review; tag-object retry with a frozen payload gives the same SHA; a
   server-set creation time for refs, if any [P1, P2, P4, P5, P6, P8].
1. **Forge adapter refactor (no behaviour change):** move every Gitea call behind the adapter
   (`GiteaForge`), repositories addressed as `gitea:<owner>/<name>` (bare names keep meaning Gitea),
   existing suites unchanged. Then `GitHubForge` with its fake-forge tests; a GitHub repository can
   be reviewed in `local` mode by a single instance before coordination exists.
1b. **Coordination code, mode `local` default (no-op deploy):** coordination module, publication, work claims,
   head watcher, mode fence check, `v1.cov` comment (written in both modes; harmless to old readers),
   strictest-marker merge gate, `iter_reviews` fail-closed, CLI, metrics, alerts. Deploy to both
   kinds.
2. **Cut over each kind** with its single instance (drain → export → fence → `forge`). One week of
   metrics compared with before.
3. **Second claude instance.** Pre-test: a second process on reviewer-1 (own `instance`, port,
   router key) against a fixture repository. Then a VM — **blocked on a free LAN address**
   (`docs/network-plan.md` has none today).
4. Remove the jobs-table code path after a stable period.

## Corner cases

| # | Situation | Handling | Layer |
| --- | --- | --- | --- |
| C1 | Webhook reaches every instance | Race on `a<N>`; readback picks one | work |
| C2 | Create returns 201/409/500/timeout | Readback by nonce decides; status ignored | both |
| C3 | Create failed for real (not found) | Retry later; persistent → error counter + alert | both |
| C4 | Owner crashes before `pub` | Lease expires → `abandoned`; another instance claims | work |
| C5 | Owner crashes after `pub`, before or during POST | `pub` without marker/`pubok` → ambiguous after grace; operator | pub |
| C6 | Owner stalls past its lease, then resumes | Cannot win `pub<G>` if another posted; if nobody did, it may still post once (allowed: it is the only publisher) | pub |
| C7 | Clock wrong (proxy, skew, jumps) | Duplicate model run or delay only; tagger date > now+300 s → expired | work |
| C8 | Push during a review | Head watcher aborts within ~60 s; `.rel.moved` | work |
| C9 | Force-push back to a reviewed head | Marker → done | pub |
| C10 | Force-push back to an ambiguous or exhausted head | Tags of open PRs are never deleted → still ambiguous/exhausted | both |
| C11 | PR closed, draft, reopened | Drop / `.rel.closed`; reopened → states persist (no deletion until merged) | both |
| C12 | Ambiguous POST | Ambiguous state, no automatic retry, `--requeue --force` → `pubvoid` | pub |
| C13 | `pubok` write fails after a successful POST | Marker visible → done; marker lag → `pubok` absent and young `pub` → publishing → re-check | pub |
| C14 | Both kinds on one PR | Separate `<kind>` namespaces | both |
| C15 | Two approval upgrades | Possible duplicate approval; no marker; harmless | merge |
| C16 | Two instances merge at once | `head_commit_id`; benign 405/409 | merge |
| C17 | An instance's seats are all parked | It does not claim; others do | work |
| C18 | Rate limited mid-attempt | `.rel.ratelimit`; capped by `released` | work |
| C19 | Every instance parked | No claims; kind-level alert | work |
| C20 | Poison PR crashing the process | `abandoned` cap → exhausted | work |
| C21 | Coordination repo failing | Fail closed + alert | both |
| C22 | Duplicate instance names / restarted process | Nonce decides; own-name foreign-nonce live claim → released | work |
| C23 | Operator requeue while an owner is alive | `cut` refuses if leased; `pubvoid` names an exact G | both |
| C24 | Reset numbering | `hw` over all claims; `cut<hw+1>`; never reuse a number | work |
| C25 | Late outcome tags from an old owner | Counters only; current owner unaffected | work |
| C26 | Lease too short after acquisition | `.rel.budget`, counted | work |
| C27 | Janitor crash mid-delete | Only merged PRs are touched; partial deletion is harmless | retention |
| C28 | Delayed delete after recreation | Merged PRs never get new tags | retention |
| C29 | More than 500 reviews on a PR | `iter_reviews` fails closed | pub |
| C30 | Truncated tag listing | Phase 0 measures; fail closed at the page cap | both |
| C31 | Mixed `local`/`forge` instances | Mode fence | both |
| C32 | Policy differs between instances | `policy.json` in the coordination repo is authoritative; mismatch refuses merge/claim/post + alert | merge |
| C33 | Base retargeted, same head | See C46 | merge |
| C34 | Repo renamed or transferred | Repo id in names | both |
| C35 | Bot PAT revoked on one instance | Its creates fail 401 → it stops; alert; others continue | both |
| C36 | Gitea backup restore loses tags | Lost `pub` → a head may be reviewed again if its review was also lost; acceptable after a restore | pub |
| C37 | Legacy markers without coverage | Count as not full (one stricter round at most) | rounds |
| C38 | Rollback to `local` | Drained import of every `pub` without a marker and exhausted heads; mode fence removed | migration |
| C39 | Stalled `pub` owner + operator `--force` | `pubvoid` only after the owner process is proven gone; owner re-reads `pubvoid` before POST | pub |
| C40 | Create times out, tag lands later | Nonce fixed per name; late own right usable once | both |
| C41 | Bot review deleted after `pubok` | Ambiguous after grace; alert; `--force` path | pub |
| C42 | Head A→B→A during diff fetch | Diff by immutable merge-base…head | pub |
| C43 | Foreign PENDING review on the PR (failed earlier attempt) | Do not post; ambiguous | pub |
| C44 | Restore while a publisher is alive | Restore procedure stops instances; epoch check before POST/merge | pub |
| C45 | Push right after a successful publication | `.done` frees the lease; cross-head waits re-checked every 60 s | work |
| C46 | Base retargeted, same head | Merge gate requires `v1.cov` base ref = current base ref | merge |
| C47 | Policy changed between sweeps | `policy.json` re-read immediately before each merge | merge |
| C48 | GitHub `mergeable: null` | Not yet mergeable; retry next sweep | merge |
| C49 | GitHub diff too large (406) | `/files` fallback; patch-less files excluded, verdict capped | pub |
| C50 | Invalid inline comment (GitHub 422; **Gitea 500 + leftover PENDING review**, spike 3) | GitHub 422: definite failure, `pubx`, charged `.fail`. Gitea 500: indistinguishable from a server failure → ambiguous; its pending review is removed by the operator's void. Comments are pre-validated by `parse_hunks`, so this should not occur | pub |
| C51 | GitHub checks vs statuses | CI green needs both rollups (F.2) | merge |
| C52 | GitHub App approval does not count | Merge left to humans where approvals are required; logged once per head | merge |
| C53 | Shared rate limit exhausted / secondary limit | Forge identity parked until reset; no claims, no posts | both |
| C54 | Webhooks unreachable (GitHub, LAN) | Polling with ETags is the intake; webhooks optional via ingress | both |
| C55 | Merge queue repositories | Out of scope: review + approve only | merge |
| C56 | Retarget between final check and merge | Residual; post-merge base audit alert (B2) | merge |
| C57 | Same repo name on both forges | Repositories are forge-qualified everywhere | both |
| C58 | GitHub tag-object retry after a lost ref response | Frozen payload → same SHA; ownership by pointed-object nonce | both |
| C59 | Owner timed out on a POST that is still in flight | `pubsent` → void only after an hour on two clocks | pub |
| C60 | Owner failed before sending | No `pubsent` → `pubx` → void allowed at once | pub |
| C61 | Lost merge response + retarget | `mergeint` persisted → audit still runs | merge |
| C62 | Zero CI on a GitHub head | Not green (at least one check required) | merge |
| C63 | Git cache wiped or corrupt | Re-fetched; no behaviour change (stateless) | pub |
| C64 | `refs/pull/<n>/head` already moved when fetched | Expected SHA absent → superseded / re-queued | pub |

## Critical files

- `ansible/roles/pr_reviewer/tasks/main.yml`: git installed, `/var/cache/reviewbot/git` owned by the
  service user, `GIT_ASKPASS` helper reading the token file.

- `ansible/roles/pr_reviewer/files/`: reviewbot becomes a small stdlib-only package installed by
  the role — `reviewbot.py` (core), `forge_gitea.py`, `forge_github.py` (adapters: API, auth,
  webhook verification, event mapping, review/merge/CI/diff, create-owned ref, list/delete refs,
  clock), GitHub App JWT signing via the `openssl` CLI (no pip dependencies on the VMs).
- `ansible/roles/pr_reviewer/files/reviewbot.py`: `coord` section (`server_now`, `list_tags`,
  `read_tag`, `create_owned`, `pub_state`, `work_state`, `claim`, `outcome`), publication inside
  `post_review`, head watcher + abortable `run_llm` (stream flag; `Popen` for CLIs), candidate
  scheduler replacing `worker_once`/`enqueue` in `forge` mode, `persona_verdicts` strictest-wins,
  `iter_reviews` fail-closed, `v1.cov` comment, janitor, CLI (`--coord-state`, `--requeue`,
  `--export-coord`, `--import-local`), mode fence, SIGTERM handler, metrics.
- `ansible/roles/pr_reviewer/templates/config.json.j2`, `defaults/main.yml`: `coordination`,
  `instance`, `coord_repo`, `lease_overhead_s` (300), `post_margin_s` (90), `pub_grace_s` (600),
  `min_llm_s` (120), caps.
- `ansible/roles/pr_reviewer/templates/reviewbot.service.j2`: `TimeoutStopSec=120`.
- `ansible/roles/pr_reviewer/tasks/main.yml`: coord repo assertions; git + `GIT_ASKPASS` helper.
- `ansible/group_vars/reviewers_claude.yml`, `reviewers_codex.yml` (new); host_vars trimmed;
  `inventory/hosts.yml` groups; per-instance PATs in `reviewbot.sops.yaml` (`.sops.yaml` regex).
- `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` + `.test.yaml`.
- `scripts/tests/test_reviewbot_forge.py` (new): one contract test suite run against both fake
  forges (Gitea and GitHub semantics, including each forge's create-race and status quirks).
- `scripts/tests/test_reviewbot_coord.py` (new) and a fake Gitea that reproduces the observed
  behaviour (create-if-absent git object; the 201 goes to a different caller than the git writer;
  500 to the rest; no release row → API delete 404).
- `docs/runbooks/dev-workers.md`; docs spec `specifications/pr-reviewers/` (follow-up).

## Verification

1. **Pure functions:** `pub_state` and `work_state` tables for every row above; tag-name builder and
   validator; marker + `v1.cov` parsing; strictest-marker merge gate.
2. **Deterministic interleavings** (scripted fake Gitea, step-controlled instances) for C2, C4–C6,
   C12, C13, C22–C26, C39–C45 (including a create whose response is lost and whose write lands after
   the readback): assert no second review per (kind, head) and the expected end state [C-overall].
3. **Randomized simulation:** 3 processes of one kind, fake LLM, fake Gitea with the observed
   201/500 behaviour, injected pauses, kill -9, SIGTERM, lost responses, pushes, closes: never two
   review markers per (kind, head); every open head ends done, ambiguous or exhausted.
4. Existing suites unchanged in `local` mode (370 tests), before and after the adapter refactor.
5. **Forge contract suite** passes against both fake forges; the GitHub spike results are recorded.
5. **Live:** Phase 0 spike results recorded; Phase 2 one-week comparison; Phase 3 pre-test with two
   processes on 10 simultaneous fixture PRs, then kill one mid-review.

<!-- codex-review-status: complete -->
