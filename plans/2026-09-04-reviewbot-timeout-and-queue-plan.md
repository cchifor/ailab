# reviewbot (claude persona): fix the 300 s LLM deadline and the queue amplification it causes

## Codex Review

- A 900 s deadline is a reasonable initial value, but `max_timeout_attempts: 2` does not bound a head to 30 minutes: a near-timeout primary failure plus a full fallback can consume almost 30 minutes in one attempt and nearly 60 minutes across two.
- Comparing all failures with the timeout cap can quarantine after only one actual timeout. Track timeout attempts separately so fast transient failures do not consume the expensive-failure allowance.
- Deduplicating quarantined heads closes the loop but permanently strands that head. The plan needs an explicit recovery path, and the quarantine metric must exclude or retire historical heads to avoid a permanently firing alert.
- A single ticker is safe with the existing `db_lock`; the worker's metrics call must be removed to prevent two writers racing on the fixed `.tmp` path.
- The proposed alerts do not reliably detect this incident: the backlog threshold exceeds the observed 49.5-minute peak, queued age excludes the running job, heartbeat no longer measures worker progress, and no-success can be absent, idle, or masked. Add direct timeout/failure and running-duration telemetry plus automated regression tests.

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
   gives up. **Raising the deadline to 900 s makes this strictly worse (5 x 900 = 75 min)
   unless retries are capped at the same time.** The two changes are a package.
3. Reconcile/cold-start bursts enqueue every open PR across 5 repos at once. The 09-03 07:00
   peak of 17 is exactly this: restart at 06:52:20, then the first two jobs drawn
   (agentforge-platform#177 at 192 KB, #175 at 59 KB) each ate a 300 s timeout first.
4. **Latent infinite loop.** `enqueue()` dedupes against
   `('queued','running','posting','retry','done')` — `quarantined` is absent. A job that
   exhausts its attempts is re-enqueued by the next reconcile 300 s later, forever. It has
   not fired yet only because pushes kept superseding jobs first.

**Two defects found alongside, both of which hid the above:**

- **Metrics freeze for the whole LLM run.** `write_metrics()` is called only at the top of
  the worker loop, so every `reviewbot_*` series stops updating while a job runs. Measured
  against live Prometheus: max heartbeat staleness over 6 h is **296 s** for claude (vs 79 s
  for codex) — i.e. it tracks the LLM deadline exactly. At 900 s a heartbeat alert would need
  a >30 min threshold to avoid false positives, which makes it worthless.
- **Errors are misreported.** On nonzero exit the code raises `llm exit {rc}: {stderr[-300:]}`,
  but the claude CLI puts the real error in the **stdout** JSON envelope; stderr holds only a
  warning. So ailab#482 logged `attempt 2 failed: llm exit 1: Permission deny rule "LS"
  matches no known tool` — which says nothing about what actually broke.
- That warning is itself a real typo: `LS` matches no tool in claude CLI 2.1.197, so it is
  printed on **every** run. (Verified in the same run that only `LS` is unknown; `Bash`,
  `Edit`, `Write`, `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `NotebookEdit`, `Task`
  and `Agent` all match and are denied.) Not a security hole — directory listing is covered
  by the `Glob`/`Bash`/`Read` denials — but it is the noise that masked the real errors.
- **No alerting at all.** `reviewbot_*` feeds the dashboard and the `reviewers-node` scrape,
  but there is no PrometheusRule on any of it. Nothing pages when a reviewer stalls, which is
  why this surfaced as a manual observation days later.

## Approach

Six changes, in dependency order. (1) and (2) are a package — do not ship (1) alone.

1. **Raise the role-default deadline 300 -> 900 s** (`defaults/main.yml`). Not a host_vars
   override on reviewer-1: 300 is wrong for *every* claude persona, and a default that only
   works because two hosts happen to override it is the trap that produced this outage.
   reviewer-2 keeps its explicit 600 (codex-specific, already commented, no timeouts observed
   — max heartbeat staleness 79 s). 900 gives 75 % headroom over the largest measured run.
   <!-- codex: One successful 515 s replay does not establish the upper tail. Treat 900 s as an initial operational value and add duration/output-token telemetry so it can be validated against more than one long run. -->

2. **Cap retries specifically for deadline failures**: new
   `pr_reviewer_max_timeout_attempts: 2` (vs `max_attempts: 5` for everything else). A
   timeout costs the entire budget and yields nothing, whereas other failures (API errors,
   bad JSON) fail in seconds and are worth retrying 5x. Retrying a deadline **is** worth it
   once — agentforge-platform#177 timed out on attempt 1 and succeeded on attempt 2 — but not
   five times. Worst case per head becomes 2 x 900 = 30 min instead of 75.
   <!-- codex: The 30-minute bound is false for the current run_llm structure. A primary can run for almost 900 s, exit nonzero, and trigger a second full 900 s fallback inside the same worker attempt; two timeout-classified attempts can therefore approach 60 minutes. Give the whole primary-plus-fallback operation one wall-clock budget, or skip/limit the fallback after an expensive primary failure. -->
   Implementation: the worker distinguishes `subprocess.TimeoutExpired` and applies the lower
   cap. This deliberately compares the *total* attempt count against the lower cap when the
   *current* failure is a deadline (rather than counting deadlines separately, which would
   need a schema change); a job that burned attempts on fast errors first and then hits a
   deadline quarantines sooner. That is the desired bias: worker time is the scarce resource.
   <!-- codex: This mixes unlike failure classes and can quarantine on the first actual timeout after one fast failure. That is a mis-quarantine, not merely a conservative timeout policy. Add a timeout_attempts column with a migration, or another durable per-job counter, and apply max_timeout_attempts to that counter while retaining max_attempts as the total-failure ceiling. -->

3. **Close the quarantine re-enqueue loop**: add `'quarantined'` to the `enqueue()` dedupe
   states. Semantics become "we tried N times at this exact head, stop" — a new push creates
   a new `head_sha` and is reviewed normally. The risk this introduces is the opposite one: a
   PR quarantined by a transient outage is then never reviewed and its merge lane stalls
   silently. That is precisely what change (6)'s `ReviewbotQuarantinedJobs` alert covers, so
   the two ship together.
   <!-- codex: An alert makes the strand visible but does not recover it. Define a supported requeue/acknowledgement operation and distinguish exhausted retries from ambiguous POST quarantines, since the former can be retried deliberately while the latter requires marker/API inspection. Also retire or exclude quarantined rows for obsolete heads and closed PRs; the current metric counts every historical quarantined row forever, so one quarantine makes the alert permanently noisy even after a push. -->

4. **Write metrics from a dedicated 15 s ticker thread** instead of the worker loop, so
   heartbeat/queue-depth/job-running stay truthful during a long run. `write_metrics()`
   already takes `db_lock` and opens its own connection, so it is thread-safe as written.
   This is a prerequisite for (6): without it the heartbeat alert cannot be tighter than the
   LLM deadline.
   <!-- codex: SQLite access is serialized safely by db_lock, and the short reads should add negligible contention. Ensure the worker's existing write_metrics() call is removed and exactly one ticker invokes it: the fixed textfile ".tmp" name is not safe with two concurrent metrics writers. The heartbeat will then prove ticker/process liveness, not worker progress, so a separate running-job start timestamp is needed. -->

5. **Report the real error**: on nonzero exit, parse the stdout JSON envelope and surface its
   `subtype`/`result`, falling back to the stderr tail. Truncate. Applies to both the primary
   and the fallback log line.
   **And drop `LS`** from `--disallowedTools`.
   <!-- codex: Preserve the exit code and handle malformed, truncated, non-object, or non-string stdout fields without replacing the original LLM failure with a JSON-parsing exception. Tests should cover both primary and fallback error envelopes. -->

6. **Add `reviewbot-rules.yaml`** (new PrometheusRule, `release: kube-prometheus-stack`) with
   four rules, thresholds chosen against the measured baseline above:
   - `ReviewbotDown` — `up{job="reviewer-node"} == 0` is already covered by nothing; the
     reviewbot-specific signal is the heartbeat: `time() - reviewbot_heartbeat_timestamp_seconds > 300`,
     for 5m, warning. Only meaningful after (4).
     <!-- codex: This does not cover up==0 or an absent heartbeat series, and after the ticker change it cannot detect a stuck worker. Combine heartbeat staleness with absent/up coverage as appropriate, and add a separate worker-progress/running-duration signal. With ">300 for 5m", the earliest heartbeat notification is also roughly ten minutes after the ticker stops. -->
   - `ReviewbotQueueBacklog` — `reviewbot_oldest_job_age_seconds > 3600` for 15m, warning.
     Age, not depth: a burst of 18 that drains in 20 min is healthy (that is the normal
     reconcile shape), a single job stuck an hour is not.
     <!-- codex: This threshold would not have fired for this incident's 49.5-minute maximum and would notify only after 75 minutes. The metric also explicitly excludes state="running", so a single stuck job with nothing queued reports age zero, contrary to this rationale. Calibrate below the observed bad peak but above the 28.8-minute healthy comparator, and add running-job age rather than relying on queued age alone. -->
   - `ReviewbotQuarantinedJobs` — `reviewbot_quarantined_jobs > 0` for 15m, warning. The
     safety net for (3): a head that gave up is now permanent until a human or a push clears
     it, so it must be visible.
     <!-- codex: A push does not clear the existing database row: enqueue only supersedes queued/retry rows, while write_metrics counts all quarantined rows. This alert will remain firing after the PR moves or closes. Export actionable-current quarantines or implement acknowledgement/cleanup semantics before alerting on the gauge. -->
   - `ReviewbotNoSuccess` — `time() - reviewbot_last_success_timestamp_seconds > 21600` (6 h)
     for 30m, warning. Catches the "alive, dequeuing, but every job fails" shape that this
     incident actually had — heartbeat and queue depth both looked fine throughout.
     <!-- codex: This neither matches the 71-minute incident nor proves failures: it false-positives during six idle hours, is absent before the first recorded success, and can be reset by any state="done", including an existing marker or an over-size skipped review. Gate it on actual work and export attempt/timeout/failure counters so the observed 12 timeouts in three days can be alerted on directly. -->

   Every expression must be validated against live Prometheus before commit (an ailab rule
   set has previously shipped pinned to a job with zero targets).
   <!-- codex: Confirming that expressions select series and are false now is insufficient. Add promtool rule tests covering positive, absent-series, idle-queue, stuck-running, historical-quarantine, and recovery cases. -->

Explicitly **not** doing:
- Not adding worker concurrency. Single-threaded is deliberate (serializes subscription
  use), and utilization is ~5 % (68 jobs/24 h at ~60 s each). Bursts drain fine once
  timeouts stop wasting 300 s each.
- Not touching `max_diff_bytes` (400 KB). Run time is output-bound; the cap is not the lever.
- Not capping thinking via `MAX_THINKING_TOKENS`. It would cut latency but changes review
  quality, and there is no evidence about the quality cost. Separate experiment if wanted.
  <!-- codex: Deferring the quality tradeoff is reasonable, but the 40,948-token run is the identified amplification mechanism. This plan should at least record output tokens and duration, define an acceptable ceiling/SLO, and schedule the experiment; otherwise 900 s only moves the same unbounded failure mode. -->
- Not changing reviewer-2/codex.

## Critical files

| Path | Change |
|---|---|
| `ansible/roles/pr_reviewer/defaults/main.yml` | `llm_timeout_s` 300->900; new `max_timeout_attempts: 2` |
| `ansible/roles/pr_reviewer/templates/config.json.j2` | render `max_timeout_attempts` |
| `ansible/roles/pr_reviewer/files/reviewbot.py` | metrics ticker thread; timeout-aware attempt cap; `quarantined` in enqueue dedupe; stdout-aware error text; drop `LS` |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` | NEW — 4 alerts |
| `kubernetes/apps/infrastructure/monitoring/kustomization.yaml` | add the new rules file |

`ansible/host_vars/reviewer-1.yml` is intentionally **not** modified (see approach 1).

## Verification

1. **Static**: `python -m py_compile reviewbot.py`; `ansible-playbook reviewers.yml --syntax-check`;
   confirm the rendered `config.json` parses.
   <!-- codex: Add automated unit/integration coverage for primary-plus-fallback wall-clock budgeting, two actual timeouts, mixed fast-error/timeout sequences, same-head quarantine dedupe, new-head behavior, quarantine cleanup/requeue, concurrent ticker/worker database access, and malformed stdout error envelopes. Static compilation cannot protect the new state-machine semantics. -->
2. **Alert expressions against live Prometheus BEFORE commit** — every one of the four
   queried at `:30090` and confirmed to (a) select the intended series and (b) evaluate
   false right now. A rule that matches zero series is the failure mode to rule out.
3. **Deploy**: `ansible-playbook dev-workers.yml`-equivalent for reviewers —
   `ansible-playbook reviewers.yml -l reviewer-1 -t reviewbot` (from WSL with
   `ANSIBLE_CONFIG` set explicitly; /mnt/c is world-writable so ansible.cfg is dropped
   silently). Confirm `/etc/reviewbot/config.json` shows `"llm_timeout_s": 900` and
   `"max_timeout_attempts": 2`, and the unit restarted.
4. **Live proof of the fix**: re-trigger a review of a large PR (the #1067 shape, ~340 KB
   diff) and confirm it completes with a posted review rather than a 300 s timeout. Absent a
   live large PR, replay the #1067 head through the same code path.
5. **Metrics thread**: confirm `max_over_time((timestamp(reviewbot_heartbeat_timestamp_seconds)
   - reviewbot_heartbeat_timestamp_seconds)[1h:30s])` for claude drops to <60 s (from 296 s)
   — including while a job is running.
   <!-- codex: Also verify that running-job age/progress advances during the replay; a fresh heartbeat alone can conceal a permanently blocked worker once metrics have their own thread. -->
6. **Rules loaded**: after Flux reconciles, confirm the 4 rules appear in
   `/api/v1/rules` and are `inactive`, not `firing`.
7. **Regression guard**: confirm a normal small PR still reviews in <60 s and that
   `reviewbot_jobs_done` keeps incrementing.
   <!-- codex: Add an operational runbook verification: deliberately quarantine a fixture head, confirm the alert fires, exercise the supported requeue/ack path, and confirm both the job and alert recover. -->

<!-- codex-review-status: complete -->