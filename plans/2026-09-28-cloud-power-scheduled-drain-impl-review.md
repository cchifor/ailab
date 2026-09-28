# Implementation review — cloud-power-scheduled-drain — round 1

<!-- codex-impl-review-status: complete -->

## Summary

- All 45 unit tests pass. Fault injection reproduced five gaps described below.
- Blocking issues: malformed Gitea responses can permit shutdown, and failed rollback persistence can resurrect a rejected OFF after restart.
- Runner adoption violates persist-before-action; ON is not fully serialized with scheduling.
- Single replica, Recreate, scoped RBAC, separate encrypted secret, CSRF checks, and textContent rendering match the plan. The secret was not decrypted.
- Kustomize rendering was blocked by filesystem permissions; live behavior remains unverified.

## Findings

### Reject malformed and incomplete Gitea lists before counting a clear poll
**Location:** kubernetes/apps/apps/cloud-power/app.py:381  
**Severity:** blocker
<!-- codex: A missing collection key passes validation because d.get(key, []) defaults to an empty list. An empty page also terminates pagination despite total_count indicating more entries. Returning {"total_count":1} for jobs on two polls reproduced shutdown while the fake Gitea still held a running cloud job. Item validation checks field presence only: runner_name:null also passes and silently excludes the job. Require collection keys, validate field types, and reject incomplete pagination with GiteaError so the clear count resets. Add regression tests for malformed envelopes, null fields, and premature empty pages. -->

### Failed rollback persistence can resurrect a rejected OFF
**Location:** kubernetes/apps/apps/cloud-power/app.py:649  
**Severity:** blocker
<!-- codex: _to_releasing calls _save_quiet and proceeds to re-enable runners even when persisting releasing fails. If the final idle write also fails during the same ConfigMap outage, memory reports idle while durable state remains draining. Idle ticks never retry that write, contrary to _save_quiet's comment. Reproduced a failed pause returning an error, followed by restart loading the old drain and dispatching shutdown. Require successful persistence before release side effects, retain failed writes for retry, and avoid reporting completion while durable state still authorizes OFF. Add a restart test covering ConfigMap failures throughout pause rollback. -->

### Persist ownership before pausing runners discovered during drain
**Location:** kubernetes/apps/apps/cloud-power/app.py:756  
**Severity:** important
<!-- codex: A newly discovered runner is PATCHed before being added to the persisted owned set. A crash or failed state write can therefore strand it disabled. More directly, when PATCH succeeds remotely but its reply times out, execution skips the ownership append; subsequent polls see an already-disabled runner and never adopt it. Reproduced CANCEL reaching idle while cloud-ci-7 remained paused. Persist ownership successfully before PATCH and retain it for rollback even when the PATCH outcome is unknown. Test both reply loss and restart between adoption steps. -->

### Serialize ON with scheduling instead of consulting the published snapshot
**Location:** kubernetes/apps/apps/cloud-power/app.py:1083  
**Severity:** important
<!-- codex: The wake handler decides whether to cancel using SCHED.view outside the scheduler lock. During schedule(), durable state can already be draining while the published snapshot still says idle because runner PATCHes have not finished. A concurrent ON skips cancellation and returns 200; OFF subsequently completes and powers down the hosts. This sequence reproduced with the first pause PATCH blocked. Move the wake decision, cancellation, and dispatch into a scheduler operation under the same lock, using current state. Surface cancellation failures rather than silently returning ordinary wake success. Add a concurrent OFF/ON test. -->

### Measure controller staleness against a clock that continues advancing
**Location:** kubernetes/apps/apps/cloud-power/app.py:944  
**Severity:** important
<!-- codex: The warning compares c.now with c.last_tick, but both timestamps come from the same published snapshot. If the worker hangs or stops publishing, both freeze and repeated status requests never make the warning appear. A snapshot remained apparently fresh after advancing the test clock by ten minutes. Compare last_tick with browser time or a fresh server response timestamp, and derive the threshold from three configured poll intervals rather than hard-coding 90 seconds. Add a UI test that repeatedly renders an unchanged snapshot as time advances. -->

## Diff stat

```text
 .../0032-opportunistic-cloud-ci-runners.md         |  35 +
 docs/runbooks/ci-runners.md                        |  29 +-
 kubernetes/apps/apps/cloud-power/app.py            | 806 ++++++++++++++++++++-
 kubernetes/apps/apps/cloud-power/deployment.yaml   |  32 +-
 .../apps/apps/cloud-power/kustomization.yaml       |   2 +
 kubernetes/apps/apps/cloud-power/rbac.yaml         |  48 ++
 .../apps/apps/cloud-power/secret-gitea.sops.yaml   |  43 ++
 scripts/tests/test_cloud_power.py                  | 558 ++++++++++++++
 8 files changed, 1513 insertions(+), 40 deletions(-)
```