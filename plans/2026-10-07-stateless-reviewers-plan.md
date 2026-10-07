# Stateless reviewers: several instances per kind, coordinated only through Gitea

## Context

reviewbot runs one instance per kind (`claude` on reviewer-1, `codex` on reviewer-2). Each instance
keeps its work queue, failure memory and round history in a local SQLite file
(`/var/lib/reviewbot/state.sqlite`), so a second instance of the same kind would review the same
pull requests (wasted model runs, a small double-post window) and would miscount convergence rounds.
Measured over 7 days (2026-10-07): claude p90 enqueue-to-posted 6.8 min, codex 0.8 min; merges wait
for both, so claude throughput is the bottleneck.

Goal: run N instances of one kind on different machines, guarantee that **at most one instance of a
kind reviews a given pull request at a time**, keep every instance **stateless**, and use **Gitea as
the only synchronization point**. No shared database. A local SQLite file MAY remain for telemetry
only; losing it must never change review behaviour.

Verified on 2026-10-07 against `cchifor/primes-lab` (the spike, `scratchpad/tag_race.py`):

- 30 concurrent `POST /repos/{o}/{r}/tags` (and `/branches`) for one name, 5 rounds each: exactly one
  `201` per round. Losers of a race get **500**; a sequential duplicate gets **409**
  `tag already exists`.
- An annotated tag's message is read back exactly (the winner's); `GET /repos/{o}/{r}/git/refs/tags/<prefix>`
  lists every tag under a prefix in one call.
- `DELETE /repos/{o}/{r}/tags/{name}` returns **404 for names containing `/`** (204 for flat names).
- Responses carry a `Date` header usable as the server clock.

## Approach

### 1. Two sources of truth, both in Gitea

| Fact | Where it lives | Already today? |
| --- | --- | --- |
| "This kind reviewed head H" + verdict | The authenticated marker on the PR review | Yes |
| Coverage of that review (for round counting) | **New** `coverage=` field in the marker | No |
| Who is reviewing (repo, PR, head) now, attempt history, quarantine | **Lock tags** in a coordination repository | No |

Everything else (queue, backoff timers, park tables, counters) is per-instance, in memory or in the
telemetry-only SQLite, and is rebuilt from Gitea on start.

### 2. Coordination repository

`cchifor/reviewbot-coord`: private, `auto_init` with one commit (the tag target), **no push mirror**,
write access only for the bot accounts (`reviewer-claude`, `reviewer-codex`) and the owner. No
webhooks, no Actions. Created by hand once (documented); its existence and settings are asserted by
the Ansible role (GET repo: `private`, not `mirror`; push-mirror list empty, read with the
hook-check owner token).

### 3. Lock tag naming and content

Flat names (no `/`, so the delete API works; valid `git check-ref-format`):

```
rb1.<kind>.r<repo_id>.p<pr>.<head_sha40>.<event>
```

- `<repo_id>`: Gitea's numeric repository id (stable across renames, no escaping problem).
- `<event>` is one of:

| Event | Meaning | Charged? |
| --- | --- | --- |
| `a<N>` | Claim of attempt N (N = 1, 2, ...) | — |
| `a<N>.ok` | Attempt N posted its review (informational; the marker is the truth) | — |
| `a<N>.fail-t` | Attempt N failed on the deadline class (TimeoutExpired / ExpensiveFailure) | yes, deadline cap |
| `a<N>.fail` | Attempt N failed, ordinary class | yes, ordinary cap |
| `a<N>.rel` | Attempt N released without charge (rate limit, graceful shutdown, head moved, PR closed) | no |
| `a<N>.q` | Attempt N's POST was ambiguous: quarantine this head, never retry automatically | — |
| `appr` | Claim for the one-off "Approving per clean verdict" upgrade at this head | — |
| `reset<K>` | Operator requeue K: attempts numbered below it are ignored | — |

Tag message: one JSON object, validated on read:

```json
{"v": 1, "kind": "codex", "instance": "reviewer-2b", "repo": "cchifor/ailab", "pr": 1112,
 "head": "<sha40>", "attempt": 3, "issued_at": 1791371653, "lease_s": 1200,
 "class": "ordinary", "note": "short reason, no secrets"}
```

`issued_at` is the Gitea server time (`Date` header) of the prefix listing made just before the
create, so it is never later than the real create time. All lease arithmetic uses Gitea's clock
(the `Date` header of the most recent response), never the local clock.

**Authenticity.** A lock tag counts only if its tagger is `reviewer-<kind>`. A tag with a valid name
but another tagger is treated as hostile: the head is not reviewed by anyone (fail closed),
`reviewbot_coord_foreign_tags` increments and an alert fires. (Tagger identity to be confirmed in
Phase 0: the tag object's `tagger` field.)

### 4. Deriving the state of a head (pure function)

`head_state(tags, now)` over the tags under `rb1.<kind>.r<id>.p<pr>.<sha>.`, ignoring attempts below
the latest `reset<K>` (attempt numbers continue upward across resets):

1. Any authentic `a<N>.q` → **quarantined (ambiguous)**.
2. Let `charged` = attempts with `.fail`/`.fail-t`, `timeouts` = attempts with `.fail-t`,
   `abandoned` = claims with no outcome tag whose lease has expired.
3. `timeouts >= max_timeout_attempts` (2) or `charged >= max_attempts` (5) or
   `abandoned >= max_abandoned` (3, new) → **quarantined (exhausted)**.
4. Highest claim `a<M>` with no outcome tag and `now < issued_at + lease_s` → **leased** by its
   instance.
5. Otherwise → **claimable**, at `not_before` = last outcome's `issued_at` +
   `min(3600, 60 * 2^charged)` for a charged failure, immediately after `.rel` or an expired lease.
   Next attempt number = M + 1 (or 1 when there is none).

This replaces `next_failure_state`'s storage, the `jobs` table and restart recovery; the policy
values are unchanged.

### 5. Claim protocol (per candidate pull request)

Candidates come from webhooks and sweeps into an in-memory set `{(repo, pr)}`; nothing about a
candidate is persisted.

```
1. RATE_LIMITED_UNTIL in the future (every local seat parked)  -> skip, keep candidate
2. pr_ok(repo, pr): closed/draft -> drop; else head H
3. own authenticated marker at H                              -> drop (done)
4. tags = list rb1.<kind>.r<id>.p<pr>.   (all heads of this PR; one call)
5. a live lease exists on ANOTHER head of this PR             -> skip until it expires or its owner releases
6. s = head_state(tags for H, server_now)
   quarantined -> drop (counted in a gauge); leased -> skip until lease end; not yet claimable -> skip until not_before
7. create tag a<N> with message {instance, issued_at = server_now, lease_s}
   201                     -> owned
   409                     -> lost, skip
   500 / timeout / other   -> GET the tag: message.instance == me -> owned; exists otherwise -> lost; 404 -> skip (retry next cycle)
8. owned: run the review (today's review_job) with the LLM deadline capped at
   issued_at + lease_s - post_margin - server_now
```

`lease_s` = `llm_timeout_s` + `lease_overhead_s` (default 300: diff fetch, API calls, POST);
`post_margin` = 90 s.

### 6. Fencing before every mutation

Immediately before the review POST (and before the approval upgrade), in `post_review`:

1. `server_now < issued_at + lease_s - post_margin`, else release (`.rel`) and do not post.
2. Re-list this head's tags: my `a<N>` still exists and still authentic, no `a<N+1>`, no `.q`, no
   `reset` after it. Else do not post.
3. Existing checks: head unchanged (`pr_ok`), `posting-disabled` absent, no marker for (kind, head).
4. POST. Success → `a<N>.ok`. Exception → `a<N>.q` (ambiguous; same semantics as today).

**Residual race, bounded and documented.** A takeover requires the lease to expire first, and the
taker then runs a whole review (minutes) and repeats steps 1–3 including the marker check; the
original owner cannot pass step 1 after expiry. Two posts for one head therefore need an owner that
stalls inside the few hundred milliseconds between its step 1 and its POST for longer than
`post_margin` plus the taker's entire review. Accepted.

### 7. Outcomes and release

| Event during an owned attempt | Outcome tag |
| --- | --- |
| Review posted | `a<N>.ok` (best effort; a missing `.ok` is harmless because the marker wins) |
| Skip notice posted (over cap, unparsable) | `a<N>.ok` (the skip carries a marker) |
| `RateLimited` (all seats refused, or budget floor) | `a<N>.rel` |
| Deadline failure | `a<N>.fail-t` |
| Other failure | `a<N>.fail` |
| Head moved / PR closed or draft detected | `a<N>.rel` |
| Lease about to expire before posting | `a<N>.rel` |
| SIGTERM (deploy) during the model run | abort the run, `a<N>.rel`, exit |
| SIGTERM after the POST started | finish the POST (bounded 60 s), then `a<N>.ok` or `.q`, exit |
| Crash / kill -9 / VM loss | nothing: the lease expires and counts as `abandoned` |

If writing the outcome tag fails, the lease simply expires (abandoned, uncharged unless the abandon
cap is reached). systemd `TimeoutStopSec` is raised to 120 s.

### 8. Per-PR exclusivity across heads

Step 5 of the claim refuses to start head H2 while another head of the same PR is leased. The only
overlap left: instance A reads head H1, a push lands, B sees H2 and lists before A's create for H1
arrives. Both then run, A on a stale head; A's pre-diff and pre-post `pr_ok` checks detect the move
and release without posting. So: at most one review **posted** per (kind, head), and at most one
**active** per (kind, PR) except during that push race, where the stale one cancels itself.

### 9. Rounds from markers (no local history)

Marker becomes `<!-- review-bot:v1 persona=<p> head=<sha40> verdict=<v> coverage=<c> -->`
(`coverage` optional in `MARKER_RE`, so old markers still parse). `review_round` counts distinct
heads with an authenticated marker of this kind, `verdict in {clean, findings}` and
`coverage in {full, absent}`, + 1. The approval-upgrade review carries `coverage=full` only if the
original review did (it copies the original marker's fields).

### 10. Sweeps, merges, janitor

- **Sweep leader per slot.** Every `reconcile_s` each instance tries to create
  `rb1.<kind>.sweep.<slot>` where `slot = floor(server_now / reconcile_s)`. The winner sweeps (list
  open PRs, add candidates, `maybe_merge`, janitor); others only add candidates from their own
  webhooks. A crashed leader costs at most one slot. (Candidates from a sweep are added only on the
  leader; the claim protocol makes it safe for every instance to also sweep, so this is a load
  optimisation, switchable off.)
- **Merges** are unchanged and idempotent (`head_commit_id`, benign 405/409). The approval upgrade is
  claimed with `appr` (create-if-absent) so two instances never both post an approval.
- **Janitor** (leader only, each sweep, bounded to 200 deletes): delete lock tags whose PR is closed
  and whose newest tag is older than `lease_s`; delete tags of heads that are no longer the PR head and
  are older than 24 h; delete `sweep.<slot>` tags older than 1 h. Deletes are idempotent (404 ok).
  An owner whose claim tag disappears treats it as lost at fencing step 2.

### 11. Operator actions

- `--requeue <repo> <pr>`: for the current head, refuse if leased; refuse `.q` without `--force`;
  else create `reset<K>` (K = highest + 1). Create-only, auditable, no deletes.
- Kill switches (`inhibit`, `posting-disabled`) unchanged, per instance.
- New `--coord-state <repo> <pr>`: print the derived state of every head (for diagnosis).

### 12. Instances, identity and configuration

- New config keys: `coordination: "local" | "gitea"` (default `local` = today's behaviour),
  `instance` (default hostname), `coord_repo`, `lease_overhead_s`, `post_margin_s`, `max_abandoned`,
  `sweep_leader` (bool).
- Every instance of a kind uses the **same** kind configuration (prompt, ladder, caps, repos,
  merge policy): move the kind-level `pr_reviewer_*` values from host_vars into
  `group_vars/reviewers_<kind>.yml`; host_vars keep only seats, router key and instance identity.
  Mixed caps would make `head_state` disagree between instances.
- Bot account stays one per kind; **one PAT per instance** for that account (independent revocation).
- One org webhook per instance (the role's hook assertion already checks by IP).
- Seats and router keys stay per instance (own capacity, own parks).

### 13. Telemetry without the jobs table

SQLite keeps only `meta` (counters, gauges, timestamps) for metric continuity. New/changed series,
all with `instance` as well as `persona`:
`reviewbot_candidates`, `reviewbot_oldest_candidate_age_seconds` (in-memory, resets on restart),
`reviewbot_coord_claims_total{result=won|lost|readback_won|error}`,
`reviewbot_coord_quarantined_heads` (computed by the sweep leader over open PRs),
`reviewbot_coord_foreign_tags_total`, `reviewbot_coord_errors_total`, `reviewbot_sweep_leader`.
Removed: `reviewbot_queue_depth`, `reviewbot_quarantined_*` (replaced). Alert rules change from
per-instance to per-kind where the meaning is kind-wide (`max by (persona)` /
`sum by (persona)`): queue backlog, quarantine, stalled, all-seats-exhausted (a kind is exhausted
only when every instance is). promtool tests updated both ways.

### 14. Failure of the coordination path

Gitea down: nothing to review anyway; workers idle. Coordination repo unreachable or tag API erroring
while Gitea PRs work: **fail closed** (no review without a claim), `reviewbot_coord_errors_total` and
an alert `ReviewbotCoordinationFailing`. Never fall back to uncoordinated reviewing.

### 15. Rollout

0. **Spike (no code change).** Create `cchifor/reviewbot-coord`; with the bot account's PAT
   confirm: tagger identity on API-created tags, tag message round trip, prefix listing with 100+
   matches (pagination behaviour), create-if-absent under 30-way concurrency with the bot token,
   flat-name delete, `Date` header present on every endpoint used.
1. **Code behind `coordination: local`.** Marker `coverage=` field (both modes), `head_state`,
   claim/fencing/outcomes, sweep leader, janitor, CLI, metrics. Default unchanged; deploy is a no-op.
2. **Switch each kind to `gitea` with its single existing instance.** Behaviour should be identical;
   compare a week of latency and verdict metrics. Rollback = set `local` (the jobs table is still
   there and still written in local mode).
3. **Add a second instance of the claude kind.** Needs a VM and a LAN address: `docs/network-plan.md`
   has no free static address today, so an address must be released first (blocking dependency).
   As a cheap pre-test, run a second process on reviewer-1 with a different `instance`, port and
   router key against a fixture repository.
4. **Remove the local queue code path** after a stable period; keep `meta` for telemetry.

Migration rule: all instances of a kind switch mode together (an uncoordinated `local` instance would
ignore the locks). Today each kind has one instance, so step 2 is atomic per kind.

## Corner cases

| # | Situation | Handling |
| --- | --- | --- |
| C1 | Same webhook reaches every instance | All race to create `a1`; exactly one wins (verified) |
| C2 | Create response lost or 500 | Read the tag back; owner by `instance` field |
| C3 | Create genuinely failed (500, tag absent) | Readback 404 → skip, retry next cycle |
| C4 | Owner crashes or VM dies | Lease expires → `abandoned` (uncharged up to `max_abandoned`) |
| C5 | Owner hangs past its lease | Fencing step 1 stops its POST; another instance takes over after expiry |
| C6 | Local clock skew | Not used: all lease maths on Gitea's `Date` |
| C7 | Push during a review | `pr_ok` before diff and before POST; `.rel`; new head claimed normally |
| C8 | Push race (two heads active briefly) | Stale owner self-cancels; never posts (section 8) |
| C9 | Force-push back to an already reviewed head | Marker present → done |
| C10 | Force-push back to a quarantined head | Still quarantined (tags persist) until requeue; same as today |
| C11 | PR closed, draft or reopened | Closed/draft → drop/`.rel`; reopened → marker decides |
| C12 | Ambiguous POST | `a<N>.q`; no instance retries; requeue needs `--force` |
| C13 | Marker appears mid-review (defence in depth) | Pre-post marker check → no post, `.ok` not written, `.rel` |
| C14 | Both kinds on the same PR | Separate `<kind>` namespace; independent |
| C15 | Two instances try the approval upgrade | `appr` create-if-absent; one posts |
| C16 | Two instances (or kinds) merge at once | `head_commit_id`; loser gets benign 405/409 |
| C17 | An instance's seats are all parked | It does not claim; other instances do |
| C18 | Rate limited mid-attempt | `a<N>.rel` (uncharged); another instance can claim at once |
| C19 | Every instance of a kind is parked | No claims; candidates wait; kind-level alert |
| C20 | Poison PR that crashes the process | `abandoned` cap → quarantined (exhausted) |
| C21 | Coordination repo or tag API failing | Fail closed + alert; never review uncoordinated |
| C22 | Hostile or mistaken tag by a non-bot user | Tagger check; head held, alert (fail closed) |
| C23 | Janitor deletes a live claim (PR closed mid-review) | Owner sees its claim missing at fencing → no post |
| C24 | Requeue while leased | CLI refuses |
| C25 | Deploy restart | SIGTERM → `.rel`; candidates rebuilt by an immediate startup sweep |
| C26 | Mixed configurations between instances of a kind | Prevented by group_vars per kind; config hash in the claim message, mismatch logged + alert |
| C27 | Mixed `local` and `gitea` instances of one kind | Migration rule: switch together; role asserts one mode per kind |
| C28 | Tag name limits | Flat, ASCII, `rb1.` prefix, ≤ 120 chars; validated before create |
| C29 | Many tags per head (long rate-limit storm) | Uncharged `.rel` attempts grow N; janitor + cap on attempts per head per hour (`max_claims_per_hour`, default 12) |
| C30 | Sweep leader crashes | Next slot elects another; candidates also arrive by webhook |
| C31 | Round counting after restarts or across instances | From markers only (section 9) |
| C32 | Old markers without `coverage` | Treated as full (as today's NULL coverage) |
| C33 | Gitea restore loses tags or reviews | Lost tags → heads become claimable again; marker check prevents duplicates where reviews survived; duplicates possible where reviews were lost too (acceptable after a restore) |
| C34 | Repo renamed or transferred | Repo id in the tag name is stable |
| C35 | Bot PAT revoked on one instance | That instance's creates fail (401) → it stops claiming; alert; others continue |

## Critical files

- `ansible/roles/pr_reviewer/files/reviewbot.py`: marker `coverage`; `coord` module section
  (`coord_list`, `coord_create`, `head_state`, `claim`, `fence`, `outcome`); worker loop over
  in-memory candidates; sweep leader + janitor; `post_review` fencing; SIGTERM handler; CLI
  `--requeue` (gitea mode) and `--coord-state`; metrics.
- `ansible/roles/pr_reviewer/templates/config.json.j2`, `defaults/main.yml`: new keys.
- `ansible/roles/pr_reviewer/templates/reviewbot.service.j2`: `TimeoutStopSec=120`, `KillSignal=SIGTERM`.
- `ansible/roles/pr_reviewer/tasks/main.yml`: coord repo assertion, one-mode-per-kind assertion.
- `ansible/group_vars/reviewers_claude.yml`, `reviewers_codex.yml` (new), host_vars trimmed;
  `inventory/hosts.yml` groups.
- `ansible/secrets/reviewbot.sops.yaml`: per-instance PATs (`reviewbot_<instance>_pat`), regex update
  in `.sops.yaml`.
- `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` + `.test.yaml`: per-kind rules,
  `ReviewbotCoordinationFailing`, `ReviewbotCoordForeignTag`.
- `scripts/tests/test_reviewbot_coord.py` (new): fake Gitea with atomic tags (including 500-on-race),
  multi-process instance simulation.
- `docs/runbooks/dev-workers.md` § reviewers; docs spec `specifications/pr-reviewers/` (follow-up PR).

## Verification

1. **Unit:** `head_state` table tests for every row of section 4 and corner cases C1–C35 that are
   pure logic; tag-name builder/validator; marker parse with and without `coverage`.
2. **Simulation:** a fake Gitea (stdlib HTTP server) with atomic create-if-absent tags that answers
   500 to race losers, PR/reviews/markers, injectable delays and failures. Run 3 reviewbot processes
   of one kind with a fake LLM; assert over hundreds of randomized runs (pushes, crashes via kill -9,
   SIGTERM, rate limits, lost responses): never two markers for one (kind, head); every open head
   eventually gets exactly one review or a quarantine; no review posted by a non-owner.
3. **Existing suites** pass unchanged in `local` mode (370 tests).
4. **Live, phase 2:** one instance per kind in `gitea` mode for a week; metrics equal to before;
   `--coord-state` on sample PRs matches reality.
5. **Live, phase 3 pre-test:** two claude processes on reviewer-1 against a fixture repository with
   10 PRs opened at once: each PR reviewed exactly once; kill one process mid-review: its PR is
   reviewed by the other after the lease.

<!-- codex-review-status: pending -->
