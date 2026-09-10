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

Confirmed clean starting state (checked live, both reviewer VMs 192.168.0.24 / .25, `reviewbot`
active): **zero `jobs` rows for `cchifor/review-bot-fixture`** in `/var/lib/reviewbot/state.sqlite`,
and no quarantine on it — so there is no orphaned job or quarantine state to clean up. The sweep is
currently healthy (`last_reconcile` 179 s / 168 s old at time of audit), which confirms the repo
answers 200 today and the loop completes.

## Approach

One PR carrying three IaC changes, deployed to both reviewer VMs and verified, and only then the
repo deletion. The ordering is the point: the allowlist entry must be gone from **both** running
services before the repo stops answering 200.

### 1. Fault-isolate the reconcile sweep (`reviewbot.py`)

Move the failure boundary inside the repo loop so one bad repo is skipped rather than fatal. This
is not a new pattern for this file — the sibling sweep `retire_closed_quarantines()` (`:1532`)
already does exactly this, per-row, with an explicit docstring justifying it ("A transient API
error leaves the row alone"). The reconciler is the inconsistent one.

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

The outer `try` stays — it still guards `retire_closed_quarantines()`, the `meta` write, and any
non-repo failure. `last_reconcile` keeps its current meaning: "the sweep ran to completion",
where a skipped repo does not abort completion.

### 2. Make a skipped repo visible (`reviewbot.py` + rules)

Isolation without telemetry trades a loud failure for a silent one, so the skip gets a counter and
an alert. Deliberately **unlabelled**: every existing metric in this file renders through one
hardcoded `(meta_key, metric_name)` tuple list (`:1370-1382`) from a flat `meta` k/v table, so a
per-repo label would require a second rendering path for no operational gain — the journal line
`reconcile <repo>: <err>` already names the repo, and the alert only needs to say "the sweep is
skipping something".

- `reviewbot.py`: add `("reconcile_repo_errors_total", "reviewbot_reconcile_repo_errors_total")`
  to the tuple list.
- New alert **`ReviewbotReconcileRepoErrors`** — the counter is advancing, i.e. a repo has been
  failing across cycles (not a single blip).
- New alert **`ReviewbotReconcileStale`** — `reviewbot_last_reconcile_timestamp_seconds` has stopped
  advancing, i.e. the sweep is dead for a reason isolation does not cover. This one closes the
  observability gap that exists **today**, independently of this cleanup.

  It MUST carry the `> 0` gate that `agentforge-rules.yaml:28` documents: an absent/zero timestamp
  makes `time() - ts > N` evaluate to ~1.79e9 and the alert true forever. Same shape:

  ```yaml
  expr: time() - (reviewbot_last_reconcile_timestamp_seconds > 0) > <threshold>
  ```

  Threshold sized off the deployed `reconcile_s`, with margin for several missed cycles.

Both alerts need cases in `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.test.yaml`,
which already covers the existing seven rules.

### 3. Remove the fixture from the allowlist (`defaults/main.yml`)

Drop `- cchifor/review-bot-fixture` from `pr_reviewer_repos`, leaving the four real repos. Update
the surrounding comment, which currently says "Real repos joined 2026-09-02" and implies the
fixture is the non-real one.

### 4. Retarget the pending quarantine drill

`plans/2026-09-04-reviewbot-timeout-and-queue-plan.md:261` (finalized, item 8) still names this
repo as the quarantine-drill target, and the drill was never executed. That plan is a historical
record and is not rewritten; this plan supersedes the target. The drill does not actually need a
dedicated Gitea *repo* — it needs a disposable PR. Retarget it to a scratch branch + PR on
`cchifor/ailab` (already allowlisted), closed once the drill completes.

### 5. Delete the repo

Only after §3 is deployed and verified on **both** VMs. Archive first
(`PATCH /repos/cchifor/review-bot-fixture {"archived": true}`), observe one reconcile interval, then
delete. Archiving is reversible; deletion is only recoverable via a whole-namespace Velero restore
of `gitea`, and the estate's recent dailies are `PartiallyFailed`/`Failed`/`FailedValidation`.

## Critical files

| Path | Role |
|---|---|
| `ansible/roles/pr_reviewer/files/reviewbot.py` | `reconciler()` `:1568` — fault isolation; `:1370-1382` — counter rendering |
| `ansible/roles/pr_reviewer/defaults/main.yml` | `pr_reviewer_repos` `:22` — the allowlist (the only thing that admits a repo) |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml` | the two new alerts |
| `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.test.yaml` | promtool cases for them |
| `ansible/reviewers.yml` | the play; deploy tag is `reviewbot` |

Reference (not modified): `kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:28,197`
for the `> 0` staleness-gate precedent.

## Verification

1. **Rules lint + unit tests** — `scripts/rules-lint.sh`, `scripts/promrule-spec.py`,
   `scripts/promtest-refs.py`; promtool cases for both new alerts pass.
2. **Isolation proof, before touching the forge** — on reviewer-1, temporarily add a
   known-nonexistent repo (`cchifor/does-not-exist`) as the FIRST allowlist entry, restart, and
   confirm from the journal that the four real repos are still swept, `last_reconcile` keeps
   advancing, and `reviewbot_reconcile_repo_errors_total` increments. This reproduces the exact
   failure the deletion would cause and proves the fix under it. Revert.
3. **Deploy** — `ansible-playbook reviewers.yml -t reviewbot` from WSL with `ANSIBLE_CONFIG` set
   explicitly (`/mnt/c` is world-writable, so `ansible.cfg` is dropped silently). Confirm on BOTH
   VMs that `/etc/reviewbot/config.json` `repos` no longer contains the fixture and the unit
   restarted.
4. **Sweep health** — `reviewbot_last_reconcile_timestamp_seconds` advances on both personas within
   one `reconcile_s`; `reviewbot_reconcile_repo_errors_total` stays flat at its pre-deploy value.
5. **Rules loaded** — after Flux reconciles, both new alerts appear in `/api/v1/rules` as
   `inactive`.
6. **Archive, then observe** — archive the repo; confirm one full reconcile interval passes with
   the counter still flat (proving nothing still references it).
7. **Delete, then re-confirm** — delete the repo; confirm `last_reconcile` still advances and the
   error counter is still flat on both personas. This is the assertion that the whole change was
   for: repo gone, sweep unaffected.
8. **Regression guard** — a normal PR on an allowlisted repo still gets reviewed, and
   `reviewbot_jobs_done` keeps incrementing.

<!-- codex-review-status: pending -->
