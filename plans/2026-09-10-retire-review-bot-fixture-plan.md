# Retire cchifor/review-bot-fixture without decapitating the reviewbot sweep

## Context

Operator decision (2026-09-10): `cchifor/review-bot-fixture` is to be removed from the Gitea
estate. It is a 31 KB smoke-test repo created 2026-09-02 with one open PR (#1, author `chifor`,
non-draft) and zero issues.

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

### Live evidence, and its shelf life

Observed 2026-09-10 against both reviewer VMs (192.168.0.24 reviewer-1 / .25 reviewer-2, unit
`reviewbot` active) and Prometheus: **zero `jobs` rows for `cchifor/review-bot-fixture`** in
`/var/lib/reviewbot/state.sqlite` on either host, no quarantine on it, and `last_reconcile` 179 s /
168 s old.

These are dated observations, not durable preconditions. The zero-rows finding in particular is a
snapshot: a webhook delivery or a sweep can create fixture jobs at any time until admission is
actually disabled. **Re-check fixture job state after §3 is deployed, not before** — see
Verification 4. A recent `last_reconcile` proves an earlier pass completed; it does not prove the
forge is answering right now.

## Approach

One PR carrying three IaC changes, deployed to both reviewer VMs and verified, then the repo
retired. Sequencing rationale, stated precisely:

- Once §1 is deployed on both VMs, a fixture 404 no longer threatens the other repos, so the
  allowlist edit is **not** an availability dependency of the deletion.
- It is still done first, for two lesser reasons: it stops fixture webhook admission at
  `enqueue():178`, and it prevents the new per-repo failure alert from firing on a repo we
  deliberately removed.
- Archive-before-delete is a rollback checkpoint, not a proof step (see §5).

### 1. Fault-isolate the reconcile sweep (`reviewbot.py`)

Move the failure boundary inside the repo loop so one bad repo is skipped rather than fatal.
`retire_closed_quarantines()` (`:1532`) is a partial precedent — it guards per row — but a narrow
one: only the `api()` call is inside its `try`; its database read, response inspection and commit
sit outside. The new guard must wrap the **whole** PR-processing body, because the original failure
mode reaches `existing_marker()`, `enqueue()` and response parsing, not just the listing call.

```python
for repo in CFG["repos"]:
    failed = False
    try:
        for pr in api(f"/repos/{repo}/pulls?state=open&limit=50"):
            ...unchanged body...
    except sqlite3.Error:
        raise                      # shared state store — fatal to the cycle, see below
    except Exception as e:
        failed = True
        log(f"reconcile {repo}: {e}")
    record_repo_sweep_result(repo, failed)
```

**Why `sqlite3.Error` re-raises.** A bare `except Exception` would classify a shared-state failure
as a per-repo failure and let the sweep stamp `last_reconcile` as if it had completed. Codex probed
exactly this: with `enqueue()` raising `sqlite3.OperationalError`, the naive loop still stamped
completion. The state store is not repo-scoped, so its failure must stay fatal to the cycle and let
the outer handler retry without advancing the timestamp.

No `continue` — the `except` is already the end of the loop body.

**Two limitations this deliberately does NOT fix**, documented rather than expanded into scope:

- Repo isolation is not PR isolation: a failure on one PR still skips the remaining PRs of that
  repo for that cycle. The next cycle picks them up.
- The listing is still `limit=50`, so a completed sweep does not prove every open PR was seen.
  Pre-existing; a pagination redesign is not part of retiring a fixture repo.

<!-- codex: time.sleep(CFG["reconcile_s"]) remains outside both handlers; a missing, invalid, or negative interval can kill the reconciler thread while the HTTP server and metrics ticker survive. Validate a positive numeric interval at startup; systemd's process restart policy does not restart a dead thread. -->
<!-- opus-pushback: Real but unrelated to this change: reconcile_s is templated from pr_reviewer_reconcile_s (300, int, no host override) through config.json.j2:33, so the failure needs a bad IaC edit to reach production, and the fix is generic startup-config validation. Recorded as a follow-up rather than smuggled into a fixture retirement. -->

### 2. Make a skipped repo visible (`reviewbot.py` + rules)

Isolation without telemetry trades a loud failure for a silent one. The instrument is a
**persisted per-repo last-sweep gauge**, not a counter:

- A monotonic counter answers "did failures ever happen"; the operational question is "is a repo
  being skipped **right now**". A gauge clears on recovery without waiting out a rate window, and
  distinguishes one repo failing every cycle from several failing once.
- Every metric this file emits already carries a `persona` label, and several are appended outside
  the `(key, metric)` tuple list — so the earlier framing of "unlabelled, because the tuple list is
  the only path" was wrong on both counts. With four configured repos, a `repo` label has bounded
  cardinality and lets the alert name the failure without depending on journald retention.
- Counters here persist anyway (`bump_meta():106` commits to SQLite and `main()` never clears
  `meta`), so "counters reset on restart" was not a real argument for either choice.

Implementation:

- `record_repo_sweep_result(repo, failed)` writes `reconcile_repo_failed:<repo>` = 0/1 into `meta`.
  Written for **every currently-configured repo on every sweep** — recovered zeros alongside
  failures — so the series is a current statement, not a latch. Not via `record_gauge()`, which
  would add an unwanted lifetime `_max` companion.
- `write_metrics()` renders it as `reviewbot_reconcile_repo_failed{persona,repo}`, iterating
  `CFG["repos"]` so a de-configured repo's stale row is simply not emitted.
- **The rendering fix that the tuple list alone would miss:** `gauges` is populated by an explicit
  `SELECT k,v FROM meta WHERE k IN (...)` whitelist at `:1348-1352`. Adding a tuple-list entry
  without extending that fetch exports zero forever (codex confirmed by probe). The per-repo keys
  need a prefix read (`k LIKE 'reconcile_repo_failed:%'`), not an addition to the `IN` list.

`last_reconcile` **changes meaning** and the plan says so explicitly: it becomes "a sweep attempt
completed", where previously any escaping repo error suppressed the stamp. That is only safe
because it is now paired with the per-repo failure gauge, and because `sqlite3.Error` and a failure
in `retire_closed_quarantines()` or the completion write remain fatal to the cycle — so the
timestamp cannot advance on a sweep whose state writes failed.

Two alerts, complementary rather than redundant — a caught repo failure advances the timestamp
(so the stale alert stays silent), while a hung sweep freezes the gauge at its last value (so the
repo alert must not be the only signal):

- **`ReviewbotReconcileStale`** — the sweep is not completing. Needs three branches, per instance:
  a stale timestamp, a **structural zero**, and a **missing series**. The `> 0` guard is kept, but
  the earlier justification mis-cited `agentforge-rules.yaml:28`: that precedent concerns an
  exported zero. Here `reviewbot.py:1386` omits the line entirely until the key exists, so a
  reconciler that has never completed a first sweep produces *no series at all* and scalar
  arithmetic yields nothing to alert on. The missing branch must be anchored to the expected
  reviewer targets (`up{job="reviewer-node"}`) with a startup grace period.
- **`ReviewbotReconcileRepoFailing`** — `reviewbot_reconcile_repo_failed == 1`, gated on a recent
  completed sweep so a frozen gauge is reported by the stale alert instead of double-paging here.

Thresholds are set from **sweep runtime + `reconcile_s` sleep + metrics tick + scrape interval**,
not from `reconcile_s` alone: the deployed interval is 300 s (`config.json.j2:33`, no host
override), the sleep happens *after* serial API work, and each request carries a 60 s timeout. Both
alerts specify threshold, `for`, severity and an actionable annotation.

Both need cases in `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.test.yaml`, which
currently covers all **ten** alerts in the manifest (an earlier draft said seven — stale, taken
from the 2026-09-04 plan).

### 3. Remove the fixture from the allowlist (`defaults/main.yml`)

Drop `- cchifor/review-bot-fixture` from `pr_reviewer_repos`, leaving the four real repos, and fix
the surrounding comment, which says "Real repos joined 2026-09-02" and implies the fixture is the
non-real one.

`config.json.j2:6` serializes this into `CFG["repos"]`, which has **two** consumers: `reconciler()`
and the admission gate at `enqueue():178`. So the edit also rejects fixture webhook deliveries and
head-moved enqueues — that is intended, and wants its own regression test. It does not touch the
org webhooks or the PAT's grants.

**Admission removal does not cancel work already in the DB.** `worker_once()`, `review_job()` /
`pr_ok()` and `requeue()` never re-check membership, and `retire_closed_quarantines()` discovers
repos from the `jobs` table independently of `CFG["repos"]` — so a quarantined fixture row would
keep 404ing forever after deletion. Today there are zero such rows; re-confirm after deploy and
resolve anything found before deleting.

### 4. Retarget the pending quarantine drill

`plans/2026-09-04-reviewbot-timeout-and-queue-plan.md:261` (finalized, item 8) names this repo as
the quarantine-drill target. That plan is a historical record and is not rewritten; this plan
supersedes the target. Scope discipline: **retargeting the reference is the deliverable here;
actually executing the drill is separate operational work and is not a prerequisite for retiring
the fixture.**

The drill needs a disposable PR, not a dedicated repo. Retarget to a scratch branch + PR on
`cchifor/ailab`. Safety: `pr_reviewer_automerge` is `true` and `chifor`/`cchifor` are both in
`pr_reviewer_merge_authors`, so such a PR is automerge-eligible. Use a **disposable base branch**
and apply the `no-automerge` label **before** the PR becomes reviewable — closing it afterwards
cannot undo an accidental merge.

### 5. Retire the repo

Only after §3 is deployed and verified on **both** VMs.

**Preserve first.** Gitea's git data is on the `gitea` PVC but its **database is on `infra-pg` in
the `databases` namespace** (`kubernetes/apps/apps/gitea/gitea.yaml:142-145`), so a `gitea`
namespace restore would bring back refs and *not* PR/review metadata. The earlier claim that Velero
covers this was wrong. Either capture `git bundle` of all refs plus the PR/review JSON via the API,
or record explicitly that the fixture's content is disposable — do not rely on a production-wide
restore as the recovery path.

**Archive, then delete.** Archiving is a reversible rollback checkpoint. It is **not** a proof step:
Gitea does not guard the PR-list GET with `mustNotBeArchived` (only mutations like merge), so an
archived repo keeps answering 200 and a flat metric during that window proves nothing about
references. The isolation test in Verification 2 is what proves the 404 case.

## Critical files

| Path | Role |
|---|---|
| `ansible/roles/pr_reviewer/files/reviewbot.py` | `reconciler()` `:1568` — fault isolation; `write_metrics()` `:1326` — rendering, **including the `SELECT ... WHERE k IN (...)` fetch at `:1348-1352`**; `bump_meta()` `:106` |
| `ansible/roles/pr_reviewer/defaults/main.yml` | `pr_reviewer_repos` `:22` — the allowlist (the only thing that admits a repo) |
| `scripts/tests/test_reviewbot.py` | 114 existing tests; home for the new behavior tests |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` | the two new alerts |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.test.yaml` | promtool cases |
| `ansible/reviewers.yml` | the play; deploy tag is `reviewbot` |

Reference (not modified): `ansible/roles/pr_reviewer/templates/config.json.j2` (`repos` at `:6`,
`reconcile_s` at `:33`); `kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:28,197`
for the staleness-gate precedent and its limits.

## Verification

1. **Rules lint** — `scripts/rules-lint.sh` is ONE wrapper that already invokes
   `promrule-spec.py`, `promtest-refs.py` and the digest-pinned promtool container; run it in its
   supported Bash/Docker environment. Helper success alone is not a PromQL test.
2. **Python behavior tests** (`scripts/tests/test_reviewbot.py`) — this replaces the live
   fault-injection step an earlier draft proposed, which was unsafe: restarting `reviewbot` requeues
   `running`/`posting` jobs and resets retry timers (`:1663-1671`), and the resumed worker can post
   reviews or merge real PRs; the inhibit flag does not stop the reconciler. Cover: first-repo and
   middle-repo HTTP failure; `existing_marker()` failure; several repos failing; every repo failing;
   recovery back to zero; `sqlite3.Error` preserving the previous completion stamp; a
   `retire_closed_quarantines()` failure likewise; emitted metric values **and** repo labels;
   persistence across a module reload against the same DB; and `enqueue()` rejecting a de-allowlisted
   repo.
3. **Promtool cases** — healthy idle sweep; single blip; sustained failure; recovery; timestamp
   advancing *despite* a skipped repo; timestamp frozen with no gauge change; and fresh / missing /
   structural-zero timestamp per reviewer instance. Assert both before and after each `for` duration.
4. **Deploy** — `ansible-playbook reviewers.yml -t reviewbot` from WSL with `ANSIBLE_CONFIG` set
   explicitly (`/mnt/c` is world-writable, so `ansible.cfg` is dropped silently). Note the
   `reviewbot` tag also runs the codex-CLI version floor and the live org-webhook assertions, which
   need the owner hook-check credential — a later assertion failure can leave changed files on disk
   *without* the notified restart, so confirm the handler actually fired and both processes are
   running the intended code and config. Then re-check fixture job rows on both VMs (§3) and resolve
   anything found.
5. **Sweep health** — allow sweep runtime + `reconcile_s` + 15 s metrics tick + scrape interval, not
   one bare `reconcile_s`. Confirm non-empty samples for **both** reviewer instances, an advancing
   completion timestamp, and `reviewbot_reconcile_repo_failed == 0` for all four repos.
6. **Rules loaded** — both alerts present in `/api/v1/rules`. `inactive` is not sufficient on its
   own: it also describes a rule whose selector matches nothing. Confirm rule evaluation health, that
   the loaded expressions match the intended revision, and that each underlying metric returns real
   samples for both instances.
7. **Retire** — preserve per §5, archive, then delete. Verify the authenticated delete actually
   succeeded and the repo is absent, rather than inferring it from healthy metrics.
8. **Post-deletion assertion (the point of the whole change)** — on both personas, a *completed*
   sweep after deletion, `last_reconcile` still advancing, no fixture job retries, and the four real
   repos still at `reviewbot_reconcile_repo_failed == 0`.
9. **Regression guard** — assert an actual posted review at the current head of an intended PR for
   both personas. `reviewbot_jobs_done` alone is insufficient: it is a DB-derived count that also
   includes skipped and marker-deduplicated jobs. Use an already-intended PR, or the protected
   scratch-PR setup from §4, so verification cannot accidentally merge a test change.

<!-- codex-review-status: complete -->
