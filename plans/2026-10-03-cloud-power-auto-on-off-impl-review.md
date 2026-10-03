# Implementation review - cloud-power-auto-on-off (ailab) - round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Blocking failures remain: a settings save can bypass pending CANCEL/rejection rollback, and ON can succeed immediately before Auto OFF starts.
- Resuming persisted `claimed` slots deviates from the plan’s one-attempt guarantee and permits retries after restart.
- Pool reconciliation can overwrite newer settings, falsely report them applied, and block cancellation during the final drain.
- All 110 existing tests passed; additional read-only, in-memory fault injection reproduced the failures below.
- No new CSRF, MODE=wol routing, token-pinning, or DOM HTML-injection bypass was identified; persistence, concurrency, and policy reporting remain the principal concerns.

## Findings

### Settings saves bypass pending cancellation and rejection rollback
**Location:** kubernetes/apps/apps/cloud-power/app.py:1144, kubernetes/apps/apps/cloud-power/app.py:1262  
**Severity:** blocker
<!-- codex: After a CANCEL write fails, set_auto() calls _require_usable(), reloads the still-draining schedule and sets loaded=True without reconciling cancel_wanted; subsequent ticks skip _reconcile_intent() and power off, and the same failure reproduces for a rejected schedule whose draining write landed but rollback was deferred. Reconcile remembered operator intent before accepting settings changes, and make tick honor pending intent independently of loaded. -->

### ON succeeds while an Auto OFF claim remains executable
**Location:** kubernetes/apps/apps/cloud-power/app.py:1118, kubernetes/apps/apps/cloud-power/app.py:1194  
**Severity:** blocker
<!-- codex: If the slot claim write fails, dirty=True makes _auto_off_due() return None, so ON succeeds without consuming the slot and the next successful tick starts Auto OFF; a durable claimed slot also escapes suppression because its key already equals auto.off.slot, while a failed suppression write is silently ignored by _save_quiet(). Determine suppression independently of trigger eligibility, consume unfinished claims, and return an explicit uncertain/failure response unless suppression is durable. -->

### Persisted claims can retry failed attempts after restart
**Location:** kubernetes/apps/apps/cloud-power/app.py:1216, kubernetes/apps/apps/cloud-power/app.py:1246, kubernetes/apps/apps/cloud-power/app.py:1157  
**Severity:** important
<!-- codex: When schedule() fails before its first write, such as on the initial Gitea GET, and saving the failure result also fails, storage retains claimed and a restart calls schedule() again for the same slot; disabling and re-enabling an existing claimed slot also starts it immediately because set_auto() leaves its result unchanged. Restore the plan’s rule that an already-persisted claim consumes the attempt, including claims recovered after ambiguous writes, and let settings changes terminally consume any current unfinished claim. -->

### CANCEL can be followed immediately by a new Auto OFF
**Location:** kubernetes/apps/apps/cloud-power/app.py:1058, kubernetes/apps/apps/cloud-power/app.py:1269  
**Severity:** important
<!-- codex: Start a manual OFF before 22:00, then successfully CANCEL at 22:00:05 before the worker’s first due-slot tick: cancel() finishes and clears the schedule without consuming the slot, so the next tick immediately schedules Auto OFF again. Consume the due slot atomically with cancellation and preserve that suppression through deferred cancel_wanted reconciliation, which can also finish before _tick_auto_off() runs. -->

### Auto result failures do not stop the phase machine
**Location:** kubernetes/apps/apps/cloud-power/app.py:1208, kubernetes/apps/apps/cloud-power/app.py:1269  
**Severity:** important
<!-- codex: _auto_result() can encounter a 409 and set loaded=False, yet tick continues into the old draining state: when another writer has cancelled and released that schedule, _tick_draining() re-pauses its previously owned runners, then the next reload finds no schedule and leaves those runners stranded, as reproduced with an injected result-write conflict. Stop processing immediately whenever automation leaves state unloaded or dirty, and reconcile before allowing further Gitea or shutdown effects. -->

### The final pool push blocks ON until shutdown starts
**Location:** kubernetes/apps/apps/cloud-power/app.py:1325, kubernetes/apps/apps/cloud-power/app.py:1846, kubernetes/apps/apps/cloud-power/app.py:1755  
**Severity:** important
<!-- codex: tick holds Scheduler.lock throughout pre_power_off(), including waiting for PoolMirror.lock and all PVE calls, so an ON/CANCEL arriving during that push cannot withdraw the drain before _begin_power_off() runs; wait=20/30 limits only lock acquisition, and repeated eight-second calls across nodes can exceed the promised total budget substantially. Perform reconciliation outside Scheduler.lock with an overall deadline, then reacquire the lock and revalidate the schedule, cancellation intent and drain conditions before transitioning. -->

### Older mirror requests can overwrite a newer saved policy
**Location:** kubernetes/apps/apps/cloud-power/app.py:385, kubernetes/apps/apps/cloud-power/app.py:1459, kubernetes/apps/apps/cloud-power/app.py:1755  
**Severity:** important
<!-- codex: The desired string is captured before acquiring PoolMirror.lock, so request A can capture Auto ON disabled, request B can save and apply enabled, and delayed A can subsequently acquire the lock and write disabled back; an in-flight attempt can similarly finish after the scheduler has become uncertain. Associate mirror work with a durable settings generation, discard superseded queued work and reconcile changes arriving during an attempt before treating the latest policy as applied. -->

### Applied status can describe the previous settings
**Location:** kubernetes/apps/apps/cloud-power/app.py:389, kubernetes/apps/apps/cloud-power/app.py:426, kubernetes/apps/apps/cloud-power/app.py:1569  
**Severity:** important
<!-- codex: When a new Auto ON setting is saved while the mirror lock remains busy, sync() can time out without updating desired, and view() still returns applied=True for the previous policy; the page then combines that flag with the newly saved enabled/time values and falsely promises the new wake behavior. Invalidate application status when the saved policy changes and calculate applied against the current durable policy or generation, including timeout responses and status snapshots. -->

### Failed read-back preserves a policy already known to be obsolete
**Location:** kubernetes/apps/apps/cloud-power/app.py:403, kubernetes/apps/apps/cloud-power/app.py:412  
**Severity:** important
<!-- codex: After previously verifying ON, a later GET can read an externally changed OFF comment, followed by an unsuccessful PUT and failed read-back; because verified is updated only after the entire sequence, it remains ON and applied stays true despite positive evidence that the pool differed, which the page’s applied branch hides. Record every successful read immediately and invalidate the verification when a write or read-back leaves the current policy uncertain. -->

### An empty stored value writes defaults into PVE
**Location:** kubernetes/apps/apps/cloud-power/app.py:694, kubernetes/apps/apps/cloud-power/app.py:1187  
**Severity:** important
<!-- codex: parse_auto() treats a present auto="" exactly like an absent key, so a damaged or partially restored ConfigMap silently produces a valid default Auto ON policy and the worker overwrites an existing disabled pool policy with auto-on=1 wake=08:00. Distinguish missing keys from present invalid strings, preserve the latter verbatim and suspend both automation and mirroring until explicitly repaired, as the plan requires. -->

### Invalid result timestamps break otherwise independent manual actions
**Location:** kubernetes/apps/apps/cloud-power/app.py:715, kubernetes/apps/apps/cloud-power/app.py:925  
**Severity:** important
<!-- codex: A stored result_at of 1e100 passes validation but raises OverflowError in local_label(), causing every publish() to fail and potentially breaking HTTP responses after manual OFF/CANCEL has already changed state, instead of isolating unreadable automation settings. Validate finite, representable timestamps and keep status formatting defensive so corrupt automation metadata cannot disrupt the existing scheduler API. -->

### The tests miss the claimed persistence guarantees
**Location:** scripts/tests/test_cloud_power.py:951, scripts/tests/test_cloud_power.py:962, scripts/tests/test_cloud_power.py:1043, scripts/tests/test_cloud_power.py:1181  
**Severity:** important
<!-- codex: The restart-after-claim test actually restarts after a complete tick and cancellation, the lost-claim test injects a write that never lands, the Refused test would pass with repeated scheduling attempts because it never counts them, and the DST grace test’s interval does not cross the clock change. Add operation-count assertions and fault injection at each claim/schedule/result boundary with landed and unlanded writes, actual intermediate restarts, pending operator intent, concurrent mirror requests and scheduler ticks crossing DST boundaries. -->

### Polling can overwrite a time edit before change fires
**Location:** kubernetes/apps/apps/cloud-power/app.py:1559, kubernetes/apps/apps/cloud-power/app.py:1591  
**Severity:** nit
<!-- codex: edited becomes true only in onchange, so a status poll arriving while the operator is still editing a time field can reset its value before the browser commits the change event, contrary to the promised protection for pending edits. Track input/focus state and reject stale poll responses, then render the confirmed stored value after the save completes. -->

### The standing shutdown warning is hidden in a tooltip
**Location:** kubernetes/apps/apps/cloud-power/app.py:1496, kubernetes/apps/apps/cloud-power/app.py:1584  
**Severity:** important
<!-- codex: The warning that Auto OFF stops every non-CI guest without asking appears only in a title tooltip and transient text after saving, so an operator using touch or keyboard can enable the standing shutdown policy without seeing the safeguard that justified omitting manual preflight. Put the warning visibly beside the switch before enabling it, and show the actual configured time zone visibly rather than only hard-coding Europe/Bucharest in the tooltip. -->

## Diff stat

```text
 kubernetes/apps/apps/cloud-power/app.py          | 536 ++++++++++++++++++++++-
 kubernetes/apps/apps/cloud-power/deployment.yaml |  15 +-
 kubernetes/apps/apps/homepage/configmap.yaml     |   4 +-
 kubernetes/apps/apps/homepage/deployment.yaml    |   2 +-
 scripts/tests/test_cloud_power.py                | 482 +++++++++++++++++++-
 5 files changed, 1022 insertions(+), 17 deletions(-)
```