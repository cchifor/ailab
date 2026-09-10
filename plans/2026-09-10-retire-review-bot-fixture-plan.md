# Retire cchifor/review-bot-fixture without decapitating the reviewbot sweep

## Codex Review

- The first-repo failure and missing reconcile alert are confirmed; fixing both is justified.
- The exception boundary needs explicit treatment of shared database failures. The proposed metric also needs its SQL fetch updated.
- Counters already survive restarts. A persisted per-repo last-sweep failure gauge better describes current failures; skip and stale alerts cover distinct problems.
- Replace production fault injection with isolated tests. Allowlist removal also affects webhook admission, and archive/delete ordering is overstated.
- Twenty targeted existing tests passed, and isolated probes confirmed the main implementation concerns. Live VM, forge, Prometheus, and backup observations were not independently verified.

## Context

Operator decision (2026-09-10): `cchifor/review-bot-fixture` is to be removed from the Gitea
estate. It is a 31 KB smoke-test repo created 2026-09-02 with one open PR (#1, author `chifor`,
non-draft) and zero issues.
<!-- codex: These live facts cannot be established from the worktree; retain dated evidence for repository contents, VM state, Prometheus observations, and backup status before treating them as execution preconditions. -->

Deleting it naively breaks the reviewbot, for three compounding reasons found while auditing the
org:

1. **The reconcile sweep is not fault-isolated.** `ansible/roles/pr_reviewer/files/reviewbot.py:1568`:

   ```python
   def reconciler():
       while True:
           try:
               for repo in CFG["repos"]:
                   for pr in api(f"/repos/{repo}/pulls?state=open&limit=50"):
                       ...
               retire_closed_quarantines()
               ...stamp last_reconcile...
           except Exception as e:
               log("reconcile error:", e)
           time.sleep(CFG["reconcile_s"])
   ```

   The `except` is **outside** the `for`, and `api()` (`:148`) is a bare `urllib.request.urlopen`
   that raises `HTTPError` on 404. The fixture is **first** in the allowlist
   (`ansible/roles/pr_reviewer/defaults/main.yml:22`), so a 404 on it aborts the whole sweep every
   cycle: `cchifor/ailab`, `cchifor/agentforge`, `cchifor/platform` and
   `cchifor/agentforge-platform` are never polled, `retire_closed_quarantines()` never runs, and
   `last_reconcile` never advances. Webhook-driven reviews keep working; the reconcile safety-net
   that the role's own comment says "silently covers for" missed webhook deliveries does not.

2. **That failure is invisible.** `reviewbot_last_reconcile_timestamp_seconds` is emitted
   (`reviewbot.py:1387`) and live in Prometheus for both personas, but **no alert consumes it** —
   the only `last_reconcile` rules in the estate are the agentforge worker's
   (`forge_last_reconcile_timestamp`). The two rules that might seem to cover it do not:
   `ReviewbotHeartbeatStale` watches the metrics ticker, which runs on its own thread and keeps
   ticking while the reconciler is dead; `ReviewbotStalled` is gated on `reviewbot_queue_depth > 0`,
   and a webhook-fed queue keeps draining to zero. A dead sweep therefore fires nothing.

3. **The bug is generic, not fixture-specific.** Any allowlisted repo that 404s — deleted, renamed,
   made private, or a PAT that loses its grant — takes the sweep down, and takes down every repo
   positioned after it in the list. A transient Gitea 5xx does the same for that cycle.

So the fixture removal is the trigger, but the estate defect is the missing fault isolation and the
missing alert. Fixing only the allowlist would leave the same landmine armed for the next repo.

Confirmed clean starting state (checked live, both reviewer VMs 192.168.0.24 / .25, `reviewbot`
active): **zero `jobs` rows for `cchifor/review-bot-fixture`** in `/var/lib/reviewbot/state.sqlite`,
and no quarantine on it — so there is no orphaned job or quarantine state to clean up. The sweep is
currently healthy (`last_reconcile` 179 s / 168 s old at time of audit), which confirms the repo
answers 200 today and the loop completes.
<!-- codex: This is a snapshot, not a durable guarantee: new webhook deliveries or sweeps can create fixture jobs before removal reaches both services. Recheck all fixture job states after admission is disabled; a recent timestamp proves an earlier completed pass, not current API availability. -->

## Approach

One PR carrying three IaC changes, deployed to both reviewer VMs and verified, and only then the
repo deletion. The ordering is the point: the allowlist entry must be gone from **both** running
services before the repo stops answering 200.
<!-- codex: WRONG as an availability requirement once the isolation fix runs on both VMs: a fixture 404 should no longer prevent other repositories being processed. Removing both allowlist entries first still stops fixture admission and avoids expected error alerts, but archive-before-delete is an optional operational checkpoint rather than a dependency of the fix. -->

### 1. Fault-isolate the reconcile sweep (`reviewbot.py`)

Move the failure boundary inside the repo loop so one bad repo is skipped rather than fatal. This
is not a new pattern for this file — the sibling sweep `retire_closed_quarantines()` (`:1532`)
already does exactly this, per-row, with an explicit docstring justifying it ("A transient API
error leaves the row alone"). The reconciler is the inconsistent one.
<!-- codex: The precedent is narrower: retire_closed_quarantines() catches only the API call; its initial database read, response inspection, and update/commit remain outside that guard. A non-dict response can therefore still abort cleanup, while database failures should remain fatal to the current cycle. -->

```python
for repo in CFG["repos"]:
    try:
        for pr in api(f"/repos/{repo}/pulls?state=open&limit=50"):
            ...unchanged body...
    except Exception as e:
        log(f"reconcile {repo}: {e}")
        bump_meta("reconcile_repo_errors_total")
        continue
```

<!-- codex: This is sufficient for cross-repository exception isolation only because the entire PR-processing body is inside the new try: existing_marker(), enqueue(), response parsing, and any escaping maybe_merge() exception are covered. Catching only the pulls-list request would leave the original failure mode through existing_marker() and subsequent processing. -->
<!-- codex: The broad catch also wrongly classifies shared SQLite failures as repository failures: an isolated probe made enqueue() raise sqlite3.OperationalError and the proposed loop still stamped last_reconcile. Re-raise sqlite3.Error to the outer handler, and validate shared configuration separately, so a failed state store cannot be reported as a completed sweep. -->
<!-- codex: continue is redundant here: the except block is already the final statement in the for-repo body, so falling through starts the next iteration identically. -->
<!-- codex: maybe_merge() already swallows most failures internally (:1034–1087), so those failures will not increment this metric; its pre-try guard and error-handler code can still raise into the new repository boundary. Describe the metric as incomplete repository processing, not all reconcile or merge errors. -->
<!-- codex: A failure on one PR still skips every later PR in that same repository for the cycle; repository isolation does not provide PR isolation. Log the current PR and operation where available, and document or test this remaining boundary before deciding whether a separate per-PR guard is necessary. -->
<!-- codex: The pulls listing still reads only the first 50 open PRs, so completing a sweep does not establish coverage of every open PR. This is an existing limitation to document, not a reason to expand this retirement into an unrelated pagination or concurrency redesign. -->

The outer `try` stays — it still guards `retire_closed_quarantines()`, the `meta` write, and any
non-repo failure. `last_reconcile` keeps its current meaning: "the sweep ran to completion",
where a skipped repo does not abort completion.
<!-- codex: WRONG to call the meaning unchanged: previously any escaping repository error prevented the stamp; now it advances even if every repository fails. Define it explicitly as completed sweep attempts, pair it with the failure state, and leave quarantine database failures and final completion-write failures fatal to the cycle so the outer handler retries without advancing the timestamp. -->
<!-- codex: time.sleep(CFG["reconcile_s"]) remains outside both handlers; a missing, invalid, or negative interval can kill the reconciler thread while the HTTP server and metrics ticker survive. Validate a positive numeric interval at startup; systemd's process restart policy does not restart a dead thread. -->

### 2. Make a skipped repo visible (`reviewbot.py` + rules)

Isolation without telemetry trades a loud failure for a silent one, so the skip gets a counter and
an alert. Deliberately **unlabelled**: every existing metric in this file renders through one
hardcoded `(meta_key, metric_name)` tuple list (`:1370-1382`) from a flat `meta` k/v table, so a
per-repo label would require a second rendering path for no operational gain — the journal line
`reconcile <repo>: <err>` already names the repo, and the alert only needs to say "the sweep is
skipping something".
<!-- codex: WRONG: write_metrics() already renders several metrics outside that tuple list, and all emitted metrics carry persona, so “unlabelled” really means “without repo attribution.” With four remaining configured repositories, a repo label has bounded cardinality and lets the alert identify the failure without depending on journal access, retention, or log shipping; “no operational gain” is a false economy. -->
<!-- codex: A restart-reset assumption is WRONG here: bump_meta() (:106–125) commits to persistent SQLite and main() does not clear meta; a fresh-module probe retained the counter value. Ansible restarts on notified changes rather than every no-op play, and ordinary service restarts do not reset these counters. -->
<!-- codex: A monotonic counter is valid for failure events, but a persisted last-sweep failure gauge better answers whether repositories are currently being skipped and clears on recovery without waiting for a rate window. Prefer one 0/1 series per configured repo, aggregating it when a skipped-repository count is needed; meta stores TEXT converted to floats, so neither instrument needs a schema change ([Prometheus instrumentation](https://prometheus.io/docs/practices/instrumentation/#counter-vs-gauge-summary-vs-histogram)). -->
<!-- codex: If using gauges, publish recovered zeros and failed ones together with the completion timestamp after the sweep, preserving the previous completed result during an unfinished cycle or restart. Emit only currently configured repositories, and avoid an unnecessary metric framework or record_gauge()'s automatic lifetime-max companion. -->

- `reviewbot.py`: add `("reconcile_repo_errors_total", "reviewbot_reconcile_repo_errors_total")`
  to the tuple list.
  <!-- codex: This implementation is incomplete: the explicit SELECT k,v ... WHERE k IN (...) at :1348–1352 must also fetch the new key. Adding only the tuple exports zero forever via gauges.get(key, 0), confirmed by an isolated probe with a stored value of one. -->
- New alert **`ReviewbotReconcileRepoErrors`** — the counter is advancing, i.e. a repo has been
  failing across cycles (not a single blip).
  <!-- codex: Counter growth does not establish repeated failing cycles: several repositories failing once can produce several increments, and increase(...[window]) > 0 with for: can remain true after one transient event. Specify the expression and recovery behavior; if retaining a counter, use reset-aware increase() and test real resets, while recognising that a last-sweep gauge with a suitable hold better matches this stated intent ([Prometheus increase](https://prometheus.io/docs/prometheus/latest/querying/functions/#increase)). -->
- New alert **`ReviewbotReconcileStale`** — `reviewbot_last_reconcile_timestamp_seconds` has stopped
  advancing, i.e. the sweep is dead for a reason isolation does not cover. This one closes the
  observability gap that exists **today**, independently of this cleanup.
  <!-- codex: These two signals are complementary, not redundant: caught repository failures advance the completion timestamp, while a hung sweep can leave the failure gauge at zero or the error counter flat. If a frozen failure gauge would duplicate the stale alert, gate the repository alert on a recent completed sweep. -->

  It MUST carry the `> 0` gate that `agentforge-rules.yaml:28` documents: an absent/zero timestamp
  makes `time() - ts > N` evaluate to ~1.79e9 and the alert true forever. Same shape:
  <!-- codex: WRONG for an absent timestamp: scalar arithmetic over an absent vector produces no alerting series; only a present zero produces the epoch-sized age. reviewbot.py:1386 omits the timestamp until the key exists, whereas the agentforge precedent explicitly concerns an exported structural zero ([Prometheus operators](https://prometheus.io/docs/prometheus/latest/querying/operators/#arithmetic-binary-operators)). -->

  ```yaml
  expr: time() - (reviewbot_last_reconcile_timestamp_seconds > 0) > <threshold>
  ```

  <!-- codex: This expression remains permanently blind to a reconciler that never completes its first sweep on a fresh database, and the > 0 filter also hides a persistent zero. Extend the same stale alert with a per-instance missing/zero branch anchored to expected reviewer targets and a startup grace period; test one missing persona while the other remains healthy. -->

  Threshold sized off the deployed `reconcile_s`, with margin for several missed cycles.
  <!-- codex: The checked-in interval is 300 seconds, passed through config.json.j2:33 without a host override, but the loop sleeps after its serial API work, whose individual requests use a 60-second timeout. Specify the threshold, for duration, severity, and actionable annotations using sweep runtime plus sleep and scrape delay, rather than treating 300 seconds as the completion period. -->

Both alerts need cases in `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.test.yaml`,
which already covers the existing seven rules.
<!-- codex: WRONG count: the current manifest defines ten alerts, and its fixture references all ten; seven is stale historical information. -->

### 3. Remove the fixture from the allowlist (`defaults/main.yml`)

Drop `- cchifor/review-bot-fixture` from `pr_reviewer_repos`, leaving the four real repos. Update
the surrounding comment, which currently says "Real repos joined 2026-09-02" and implies the
fixture is the non-real one.
<!-- codex: config.json.j2:6 serializes this variable into CFG["repos"], which is consumed both by reconciler() and enqueue():178; removal also rejects fixture webhook deliveries and head-moved enqueue attempts. Add an admission regression check, and note that the shared organization webhooks and PAT permissions are not changed by this allowlist edit. -->
<!-- codex: Admission removal does not cancel existing jobs: worker_once(), review_job()/pr_ok(), and requeue() do not recheck repository membership, while retire_closed_quarantines() discovers repositories from jobs independently of CFG["repos"]. After both running services load the removal, recheck fixture rows and resolve any active or quarantined state explicitly before deletion; never treat a 404 as proof an ambiguous POST can be cleared. -->

### 4. Retarget the pending quarantine drill

`plans/2026-09-04-reviewbot-timeout-and-queue-plan.md:261` (finalized, item 8) still names this
repo as the quarantine-drill target, and the drill was never executed. That plan is a historical
record and is not rewritten; this plan supersedes the target. The drill does not actually need a
dedicated Gitea *repo* — it needs a disposable PR. Retarget it to a scratch branch + PR on
`cchifor/ailab` (already allowlisted), closed once the drill completes.
<!-- codex: Retargeting the reference is a reasonable dependency cleanup; actually executing the old quarantine drill is separate operational work and need not become a prerequisite for retiring this fixture. The worktree confirms the old target, but not whether the drill was ever executed. -->
<!-- codex: A scratch PR on ailab can automerge because automerge defaults to true and chifor/cchifor are merge-eligible authors. Use a disposable base branch and apply no-automerge before making the PR reviewable, then clean up its branches and test state; closing it afterwards cannot undo an accidental merge. -->

### 5. Delete the repo

Only after §3 is deployed and verified on **both** VMs. Archive first
(`PATCH /repos/cchifor/review-bot-fixture {"archived": true}`), observe one reconcile interval, then
delete. Archiving is reversible; deletion is only recoverable via a whole-namespace Velero restore
of `gitea`, and the estate's recent dailies are `PartiallyFailed`/`Failed`/`FailedValidation`.
<!-- codex: WRONG recovery assumption: kubernetes/apps/apps/gitea/gitea.yaml:142–145 places Gitea's database on infra-pg in the databases namespace, so restoring only the gitea namespace cannot restore deleted PR/repository metadata. Preserve the fixture's Git refs and any required PR/review metadata before deletion, or explicitly record that the data is disposable; do not prescribe a production-wide rollback as the sole recovery path. -->

## Critical files

| Path | Role |
|---|---|
| `ansible/roles/pr_reviewer/files/reviewbot.py` | `reconciler()` `:1568` — fault isolation; `:1370-1382` — counter rendering |
| `ansible/roles/pr_reviewer/defaults/main.yml` | `pr_reviewer_repos` `:22` — the allowlist (the only thing that admits a repo) |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` | the two new alerts |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.test.yaml` | promtool cases for them |
| `ansible/reviewers.yml` | the play; deploy tag is `reviewbot` |

<!-- codex: Add scripts/tests/test_reviewbot.py as an implementation/verification file and config.json.j2 as a reference dependency; the metrics entry must also identify the SQL fetch at :1348–1352. -->

Reference (not modified): `kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:28,197`
for the `> 0` staleness-gate precedent.

## Verification

1. **Rules lint + unit tests** — `scripts/rules-lint.sh`, `scripts/promrule-spec.py`,
   `scripts/promtest-refs.py`; promtool cases for both new alerts pass.
   <!-- codex: Python behavior tests are missing: extend scripts/tests/test_reviewbot.py for first/middle repository HTTP failures, existing_marker() failures, multiple/all repositories failing, recovery, and shared database/cleanup failures preserving the old completion stamp. Also assert actual emitted values and repository labels, persistence after reloading the module against the same database, and rejection of enqueue attempts for a removed repository. -->
   <!-- codex: Promtool cases must cover healthy idle sweeps, a single blip, sustained failure, recovery, an advancing timestamp despite skipped repositories, a frozen timestamp with no counter growth, and fresh/missing/zero timestamp cases per reviewer. Include restart/series-gap behavior and a true counter reset if retaining the counter, plus assertions before and after each for duration. -->
   <!-- codex: scripts/rules-lint.sh already invokes both Python helpers and the digest-pinned promtool container, so they are not three independent verification commands. Run the wrapper in its supported Bash/Docker environment and the Python tests separately; helper success alone is not a PromQL test. -->
2. **Isolation proof, before touching the forge** — on reviewer-1, temporarily add a
   known-nonexistent repo (`cchifor/does-not-exist`) as the FIRST allowlist entry, restart, and
   confirm from the journal that the four real repos are still swept, `last_reconcile` keeps
   advancing, and `reviewbot_reconcile_repo_errors_total` increments. This reproduces the exact
   failure the deletion would cause and proves the fix under it. Revert.
   <!-- codex: This is unsafe as a reversible production test: restarting requeues running/posting jobs and resets retry timers (:1663–1671), and the resumed worker/reconciler can post reviews or merge real PRs. Restoring the config cannot undo those effects; the inhibit flag alone is insufficient because the reconciler does not consult it. -->
   <!-- codex: Replace this with the existing test_reviewbot.load() harness using temporary config/SQLite/textfile paths, dummy credentials, and mocked API responses: first repo raises HTTPError(404), later repos return synthetic PRs, and no real forge or LLM call is allowed. Invoke one candidate sweep through a small reconcile_once() helper or a patched sleep sentinel, without main(), then assert later-repo calls/jobs, cleanup execution, completion, and failure telemetry. -->
   <!-- codex: The current journal does not log every successful repository poll, so it cannot establish that all four were swept; a completion timestamp alone also permits all four to have been skipped. The isolated test should assert the exact API/enqueue calls, and it must exercise the candidate code rather than the old deployed binary before step 3. -->
3. **Deploy** — `ansible-playbook reviewers.yml -t reviewbot` from WSL with `ANSIBLE_CONFIG` set
   explicitly (`/mnt/c` is world-writable, so `ansible.cfg` is dropped silently). Confirm on BOTH
   VMs that `/etc/reviewbot/config.json` `repos` no longer contains the fixture and the unit
   restarted.
   <!-- codex: The reviewbot tag also runs the Codex CLI version floor and live org-webhook assertions requiring the owner hook-check credential; these are deployment dependencies beyond copying the allowlist. Verify the play and restart handlers succeeded and that both processes run the intended code/config, since a later assertion failure can leave changed files on disk without the notified restart. -->
4. **Sweep health** — `reviewbot_last_reconcile_timestamp_seconds` advances on both personas within
   one `reconcile_s`; `reviewbot_reconcile_repo_errors_total` stays flat at its pre-deploy value.
   <!-- codex: Allow sweep runtime plus the configured sleep, 15-second metrics tick, and 30-second scrape interval; completion within exactly one reconcile_s is not guaranteed. Verify nonempty samples for both reviewer instances, with an advancing completion timestamp and zero last-sweep failures if adopting the gauge; a newly introduced counter has no pre-deploy exported baseline. -->
5. **Rules loaded** — after Flux reconciles, both new alerts appear in `/api/v1/rules` as
   `inactive`.
   <!-- codex: Inactive also describes an alert whose selector matches nothing: check rule evaluation health and query each underlying metric for both expected instances. Confirm the loaded rule expressions match the intended revision and that their healthy evaluations are based on real samples. -->
6. **Archive, then observe** — archive the repo; confirm one full reconcile interval passes with
   the counter still flat (proving nothing still references it).
   <!-- codex: WRONG proof: archiving does not reproduce deletion's missing-repository response; Gitea's PR-list GET is not guarded by mustNotBeArchived, while mutations such as merging are ([Gitea API routes](https://github.com/go-gitea/gitea/blob/v1.26.1/routers/api/v1/api.go#L1244-L1280)). A flat metric during this optional observation window therefore cannot establish absence of references; use the deployed allowlists, fixture-job checks, and isolated missing-repository test. -->
7. **Delete, then re-confirm** — delete the repo; confirm `last_reconcile` still advances and the
   error counter is still flat on both personas. This is the assertion that the whole change was
   for: repo gone, sweep unaffected.
   <!-- codex: Also verify the authenticated deletion succeeded and the fixture is absent, then observe a completed post-deletion sweep on each VM with healthy failure telemetry and no fixture-job retries. Healthy metrics alone do not establish that the repository was actually deleted. -->
8. **Regression guard** — a normal PR on an allowlisted repo still gets reviewed, and
   `reviewbot_jobs_done` keeps incrementing.
   <!-- codex: Assert an actual review at the current head on the intended PR for both personas; reviewbot_jobs_done is a database-derived count that also includes skipped reviews and marker-deduplicated jobs. Use an already intended PR or the protected scratch-PR setup above so verification does not accidentally merge a test change. -->

<!-- codex-review-status: complete -->
