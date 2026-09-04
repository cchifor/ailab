# reviewbot (claude persona): fix the 300 s LLM deadline and the queue amplification it causes

## Context

The `claude` persona on reviewer-1 (192.168.0.24) has been failing to review PRs, and its
queue has been running deep. Diagnosed 2026-09-04 against the live host.

**Root cause: `llm_timeout_s: 300` is shorter than the persona's real run-time distribution.**
Re-running the exact failing job on reviewer-1 with the identical argv and a 900 s cap:

```
platform#1067: diff 341,672 B -> prompt 348,271 B (~87k tok)
ELAPSED 515.0s  rc=0  is_error=False  json parse: OK  findings: 3
```

The review succeeds. `reviewbot.py` SIGKILLs it at 300 s and reports a failure.

Evidence that this is a budget problem and not a model/auth/host problem:

- A trivial prompt on the same host returns in **3.3 s** (rc=0) — auth, the `claude-fable-5`
  pin, and the subscription are all fine. `max_over_time(reviewbot_quarantined_jobs[7d]) == 0`.
- No OOM, ~3.3 GiB available, zero memory/CPU PSI on the VM.
- **Run length is output-bound, not diff-bound.** The 515 s run emitted **40,948 output
  tokens** (nearly all reasoning) for a 3,374-char answer. That is why `ailab#482` — a
  **2 KB** diff — also timed out on 2026-09-03. Duration tracks how hard the model thinks,
  so a size-based cap is not the lever.
- Successful jobs already run 19-220 s. 220 s is 73 % of the budget, so the tail crosses
  300 s routinely: **12 timeouts in 3 days**.
- reviewer-2 (codex) sets `pr_reviewer_llm_timeout_s: 600` in host_vars and does not have
  this failure. It reviewed the same #1067 successfully at 10:29:46, verdict=clean. Only the
  claude persona failed on it. reviewer-1 has no override and inherits the role default 300.

**User-visible consequence:** #1067 was attempted 9 times across 3 heads over 71 minutes and
never produced a claude verdict, so the "all personas clean" automerge gate could never close.
The operator merged it by hand at 11:03:09Z.

**Why the queue climbs.** Peak depth 18 over 48 h; max oldest-job age 49.5 min (claude) vs
28.8 (codex). Contributors, by weight:

1. Every timeout burns the full 300 s of the single-threaded worker and yields nothing.
   Job 177 alone spent 4 x 300 s = 20 min producing zero output.
2. `max_attempts: 5` — one un-completable PR costs up to 25 min of worker time before it
   gives up. **Raising the deadline to 900 s makes this strictly worse unless retries are
   capped at the same time.** The changes below are a package.
3. Reconcile/cold-start bursts enqueue every open PR across 5 repos at once. The 09-03 07:00
   peak of 17 is exactly this: restart at 06:52:20, then the first two jobs drawn
   (agentforge-platform#177 at 192 KB, #175 at 59 KB) each ate a 300 s timeout first.
4. **Latent infinite loop.** `enqueue()` dedupes against
   `('queued','running','posting','retry','done')` — `quarantined` is absent. A job that
   exhausts its attempts is re-enqueued by the next reconcile 300 s later, forever. It has
   not fired yet only because pushes kept superseding jobs first.

**Defects found alongside, all of which hid the above:**

- **Metrics freeze for the whole LLM run.** `write_metrics()` is called only at the top of
  the worker loop, so every `reviewbot_*` series stops updating while a job runs. Measured
  against live Prometheus: max heartbeat staleness over 6 h is **296 s** for claude (vs 79 s
  for codex) — i.e. it tracks the LLM deadline exactly.
- **Errors are misreported.** On nonzero exit the code raises `llm exit {rc}: {stderr[-300:]}`,
  but the claude CLI puts the real error in the **stdout** JSON envelope; stderr holds only a
  warning. So ailab#482 logged `attempt 2 failed: llm exit 1: Permission deny rule "LS"
  matches no known tool` — which says nothing about what actually broke.
- That warning is itself a real typo: `LS` matches no tool in claude CLI 2.1.197, so it is
  printed on **every** run. (Verified in the same run that only `LS` is unknown; `Bash`,
  `Edit`, `Write`, `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `NotebookEdit`, `Task`
  and `Agent` all match and are denied.) Not a security hole — directory listing is covered
  by the `Glob`/`Bash`/`Read` denials — but it is the noise that masked the real errors.
- **No alerting at all, and no failure telemetry to alert on.** `reviewbot_*` feeds the
  dashboard and the `reviewers-node` scrape, but there is no PrometheusRule on any of it, and
  no metric counts failures, timeouts, or run duration. Nothing pages when a reviewer stalls,
  which is why this surfaced as a manual observation days later.

## Approach

Seven changes. (1)-(3) are one package — do not ship (1) alone; raising the deadline without
bounding retries makes the queue strictly worse.

### 1. Raise the role-default deadline 300 -> 900 s

In `defaults/main.yml`, not as a host_vars override on reviewer-1: 300 is wrong for *every*
claude persona, and a default that only works because two hosts happen to override it is the
trap that produced this outage. reviewer-2 keeps its explicit 600 (codex-specific, already
commented, no timeouts observed — max heartbeat staleness 79 s).

900 s is an **initial operational value**, not a measured SLO: one 515 s replay establishes a
lower bound on the tail, not the tail itself. Change (6) adds the duration and output-token
telemetry needed to revise it from more than one sample, and change (7) alerts on the timeout
rate so an under-budgeted deadline is visible within the hour rather than after three days.

### 2. Give primary + fallback ONE wall-clock budget

`llm_timeout_s` currently applies **per subprocess**, and `run_llm()` can run a near-full
primary, get a nonzero exit, and then start a *full* fallback run — the existing code comment
accepts "2x llm_timeout_s" explicitly. At 300 s that was a 10-minute worst case; at 900 s it
becomes 30 minutes **inside a single attempt**, which is what makes the naive "2 attempts =
30 min" arithmetic false.

So `llm_timeout_s` becomes the budget for the whole `run_llm()` operation: a deadline is
computed on entry, the primary gets the full remainder, and the fallback gets whatever is
left. If less than `pr_reviewer_llm_fallback_min_s` (default 60 s) remains, the fallback is
skipped and the primary's error is raised. One attempt is then bounded at ~900 s.

### 3. Count deadline failures separately from fast failures

New `pr_reviewer_max_timeout_attempts: 2` alongside the existing `max_attempts: 5`. A timeout
costs the entire budget and yields nothing; other failures (API errors, bad JSON) fail in
seconds and are worth retrying 5x. Retrying a deadline **is** worth it once —
agentforge-platform#177 timed out on attempt 1 and succeeded on attempt 2 — but not five
times.

The two counters are **separate**: comparing the total attempt count against the lower cap
would quarantine a job that hit one fast transient error and then one real timeout, which is
a mis-quarantine rather than a conservative policy. This needs a new `timeout_attempts`
column, added by a guarded `ALTER TABLE` (checked via `PRAGMA table_info`) so existing
databases migrate in place. `max_attempts` remains the total-failure ceiling.

Worst case per head is then 2 x 900 s = 30 min of worker time, genuinely.

### 4. Quarantine: close the loop, and make the strand recoverable and visible

Four parts, which only work together:

- `enqueue()` dedupes against `'quarantined'`, closing the infinite re-enqueue loop.
- **A new head retires the old strand.** `enqueue()` already supersedes `queued`/`retry` rows
  for the PR when a new head arrives; it will now supersede `quarantined` rows too. Without
  this, a push does *not* clear the database row, so the gauge — and any alert on it — stays
  up forever after a single quarantine.
- **A supported recovery path**: `reviewbot.py <config> --requeue <repo> <pr>` flips that
  PR's quarantined rows back to `queued`. This is safe for **both** quarantine classes
  (`attempts exhausted` and the deliberate `ambiguous POST` one) because `review_job()`
  re-checks `existing_marker()` in Gitea before doing any work and returns `done` if the
  review actually landed — so a requeue of an ambiguous POST costs one API call, not a double
  post. The command prints the class it is re-running so the operator knows which it is.
- **A bounded gauge to alert on.** `reviewbot_quarantined_jobs` counts every row in that
  state; the alert instead reads a new `reviewbot_quarantined_recent_jobs` (quarantined and
  `updated` within 24 h), so it self-clears even for a PR that was closed rather than pushed
  to. The cumulative gauge stays for the dashboard.

### 5. Move metrics to a dedicated ticker thread

A 15 s ticker calls `write_metrics()`; **the worker's own call is removed** — two writers on
the fixed `.tmp` path would race. `write_metrics()` already takes `db_lock` and opens its own
connection, so it is thread-safe as written and the short reads add negligible contention.

The heartbeat then proves *process* liveness rather than worker progress, so this change also
adds `reviewbot_running_job_age_seconds` (age of the oldest `running`/`posting` row, 0 when
idle) — otherwise a permanently wedged worker looks perfectly healthy behind a fresh
heartbeat. This is derivable from `jobs.updated`, which is already stamped on the transition
to `running`; no schema change.

### 6. Failure and duration telemetry (new — nothing counts failures today)

Persisted in the existing `meta` table (no schema change), exposed by `write_metrics()`:

| metric | meaning |
|---|---|
| `reviewbot_llm_timeouts_total` | counter, deadline failures |
| `reviewbot_llm_failures_total` | counter, non-deadline failures |
| `reviewbot_llm_seconds_last` / `_max` | run duration — the data that validates or revises the 900 s in (1) |
| `reviewbot_llm_output_tokens_last` / `_max` | from the claude envelope's `usage.output_tokens`; the 40,948-token run is the amplification mechanism, so it gets measured even though capping it is out of scope |
| `reviewbot_running_job_age_seconds` | from (5) |
| `reviewbot_quarantined_recent_jobs` | from (4) |

This is what makes "12 timeouts in 3 days" alertable at all. Note the amplification mechanism
is now *bounded* (2 attempts x 900 s) and *visible* (timeout counter + alert), not merely
moved — but the telemetry is what makes the eventual `MAX_THINKING_TOKENS` experiment
evidence-based rather than a guess.

### 7. Report the real error, and drop `LS`

On nonzero exit, parse the stdout JSON envelope and surface its `subtype`/`result` alongside
the exit code, falling back to the stderr tail. The parse is fully defensive — malformed,
truncated, non-object or non-string stdout must not replace the LLM failure with a
`JSONDecodeError`. Applies to both the primary and the fallback log line. And `LS` comes out
of `--disallowedTools`.

### 8. Alerts: `reviewbot-rules.yaml` (new PrometheusRule)

Thresholds calibrated against the measured 48 h baseline, **not** guessed. Minutes the
condition held over the last 48 h for the claude persona are given per rule.

| alert | expression | for | sev | why this threshold |
|---|---|---|---|---|
| `ReviewerNodeDown` | `up{job="reviewer-node"} == 0` | 10m | critical | If a reviewer VM dies the textfile series vanish entirely, so every rule below silently stops matching — absent series never fire. Without this the whole set is defeated by the failure it most needs to survive. Mirrors `DevWorkerNodeDown`. |
| `ReviewbotHeartbeatStale` | `time() - reviewbot_heartbeat_timestamp_seconds > 300` | 5m | warning | Process liveness only, and only meaningful after (5) — the ticker writes every 15 s, so 300 s is 20 missed ticks. |
| `ReviewbotWorkerStuck` | `reviewbot_running_job_age_seconds > 2400` | 5m | warning | The progress signal the heartbeat no longer provides. One attempt is bounded at ~900 s by (2), so 2400 s means wedged, not busy. |
| `ReviewbotReviewTimeouts` | `increase(reviewbot_llm_timeouts_total[1h]) >= 3` | 5m | warning | **The detector for this incident.** 6 timeouts fell in the 09:59-11:00 hour; 12 in 3 days. Nothing else below fires early enough. |
| `ReviewbotQueueBacklog` | `reviewbot_oldest_job_age_seconds > 1800` | 15m | warning | Measured: `>3600 s` held for **0 minutes** in 48 h (peak 3008 s) — the obvious threshold would never have fired. `>1800` held 40 min, `>900` held 101 min. 1800 fires on the incident, above the 28.8-min healthy comparator. |
| `ReviewbotQuarantined` | `reviewbot_quarantined_recent_jobs > 0` | 15m | warning | The safety net for (4): dedupe makes a strand permanent until a push or `--requeue`, so it must be visible. Reads the 24 h-windowed gauge so it self-clears. |
| `ReviewbotStalled` | `(time() - reviewbot_last_success_timestamp_seconds > 3600) and (reviewbot_queue_depth > 0)` | 30m | warning | Ungated at 6 h this held for **369 minutes** in 48 h — over six hours of firing on an idle night with no PRs open. Gated on queued work it held 25 sample-minutes across 5 non-contiguous ~5 min blocks, all filtered by `for: 30m`. Honest scope: a backstop for a total stall, **not** the detector for this incident (small PRs kept succeeding throughout it). |

`reviewbot_oldest_job_age_seconds` counts only `queued`/`retry`, so a single stuck job with an
empty queue reports 0 — that gap is why `ReviewbotWorkerStuck` exists rather than a lower
backlog threshold.

Explicitly **not** doing:
- Not adding worker concurrency. Single-threaded is deliberate (serializes subscription
  use), and utilization is ~5 % (68 jobs/24 h at ~60 s each). Bursts drain fine once
  timeouts stop wasting the budget.
- Not touching `max_diff_bytes` (400 KB). Run time is output-bound; the cap is not the lever.
- Not capping thinking via `MAX_THINKING_TOKENS`. It would cut latency but changes review
  quality with no evidence about the cost. Change (6) instruments it first; the experiment is
  a follow-up.
- Not changing reviewer-2/codex.

## Critical files

| Path | Change |
|---|---|
| `ansible/roles/pr_reviewer/defaults/main.yml` | `llm_timeout_s` 300->900; new `max_timeout_attempts: 2`, `llm_fallback_min_s: 60` |
| `ansible/roles/pr_reviewer/templates/config.json.j2` | render the two new keys |
| `ansible/roles/pr_reviewer/files/reviewbot.py` | shared run_llm budget; `timeout_attempts` column + migration; quarantine dedupe/retire/`--requeue`; metrics ticker; new counters; stdout-aware errors; drop `LS` |
| `scripts/tests/test_reviewbot.py` | NEW — stdlib `unittest` (the CI runner has no pytest; `broker-inventory.yaml` runs `unittest discover -s scripts/tests` on every push) |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` | NEW — 7 alerts |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.test.yaml` | NEW — `promtool test rules` fixtures |
| `scripts/rules-lint.sh` | run `promtool test rules` over `*-rules.test.yaml` after the existing check |
| `kubernetes/apps/infrastructure/monitoring/kustomization.yaml` | add the rules file |
| `docs/runbooks/dev-workers.md` | reviewer quarantine/requeue recovery procedure |

`ansible/host_vars/reviewer-1.yml` is intentionally **not** modified (see approach 1).

## Verification

Static compilation cannot protect new state-machine semantics, so the first two are tests,
not smoke checks.

1. **`scripts/tests/test_reviewbot.py`** (stdlib unittest, runs in CI automatically) covering:
   primary+fallback sharing one wall-clock budget and the fallback being skipped under
   `llm_fallback_min_s`; two real timeouts quarantining at `max_timeout_attempts`; a mixed
   fast-error-then-timeout sequence **not** quarantining early; same-head quarantine dedupe;
   a new head retiring a quarantined row; `--requeue` restoring one; malformed/truncated/
   non-object stdout error envelopes not raising `JSONDecodeError`; concurrent ticker/worker
   database access.
2. **`promtool test rules`** fixtures asserting each of the 7 alerts fires on the incident
   shape and stays silent on the healthy shape — specifically the two thresholds that were
   wrong in the first draft of this plan (backlog at 3600 s, ungated no-success), plus
   absent-series, idle-queue, stuck-running and recovery cases. Wired into `rules-lint.sh`,
   which already gates every PR.
3. **Static**: `python -m py_compile reviewbot.py`; `ansible-playbook reviewers.yml
   --syntax-check`; rendered `config.json` parses.
4. **Live expression check**: all 7 queried at `:30090` and confirmed to select the intended
   series and evaluate false now. (Done for the original 4; redo for the final set. An ailab
   rule set has previously shipped pinned to a job with zero targets.)
5. **Deploy**: `ansible-playbook reviewers.yml -l reviewer-1 -t reviewbot` (from WSL with
   `ANSIBLE_CONFIG` set explicitly; /mnt/c is world-writable so ansible.cfg is dropped
   silently). Confirm `/etc/reviewbot/config.json` shows `"llm_timeout_s": 900` and
   `"max_timeout_attempts": 2`, and the unit restarted.
6. **Live proof of the fix**: replay the #1067 head (~340 KB diff, the run measured at 515 s)
   through the deployed service and confirm a posted review rather than a deadline kill.
   During that replay, confirm `reviewbot_running_job_age_seconds` **advances** — a fresh
   heartbeat alone would conceal a blocked worker now that metrics have their own thread.
7. **Metrics thread**: `max_over_time((timestamp(reviewbot_heartbeat_timestamp_seconds) -
   reviewbot_heartbeat_timestamp_seconds)[1h:30s])` for claude drops to <60 s (from 296 s),
   including while a job is running.
8. **Quarantine drill** on `cchifor/review-bot-fixture`: force a head into quarantine,
   confirm `ReviewbotQuarantined` fires, exercise `--requeue`, confirm the job runs and the
   alert clears. Then push a new head and confirm that retires the row too.
9. **Rules loaded**: after Flux reconciles, confirm all 7 appear in `/api/v1/rules` as
   `inactive`, not `firing`.
10. **Regression guard**: a normal small PR still reviews in <60 s and `reviewbot_jobs_done`
    keeps incrementing.

<!-- codex-review-status: complete -->
