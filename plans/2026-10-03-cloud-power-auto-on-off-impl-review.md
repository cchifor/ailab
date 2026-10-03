# Implementation review - cloud-power-auto-on-off (ailab) - round 1

<!-- codex-impl-review-status: complete -->

## Findings

### Settings saves bypass pending cancellation and rejection rollback
**Location:** kubernetes/apps/apps/cloud-power/app.py:1144, kubernetes/apps/apps/cloud-power/app.py:1262  
**Severity:** blocker
**Accepted, fixed in d47e6336:** reproduced; `_require_usable()` and `tick()` reconcile whenever intent is pending, a repeated CANCEL reports the landed one (test_settings_save_after_an_unconfirmed_cancel_still_cancels).

### ON succeeds while an Auto OFF claim remains executable
**Location:** kubernetes/apps/apps/cloud-power/app.py:1118, kubernetes/apps/apps/cloud-power/app.py:1194  
**Severity:** blocker
**Accepted, fixed in d47e6336:** reproduced; `_consume_slot()` sees the due slot regardless of trigger eligibility (unsaved claims included), ON persists it durably or fails 503 (test_on_after_a_failed_claim_write_*, test_on_fails_when_the_withdrawal_cannot_be_saved).

### Persisted claims can retry failed attempts after restart
**Location:** kubernetes/apps/apps/cloud-power/app.py:1216, kubernetes/apps/apps/cloud-power/app.py:1246, kubernetes/apps/apps/cloud-power/app.py:1157  
**Severity:** important
**Accepted, fixed in d47e6336:** a claim this process did not make is a spent attempt; a settings change finishes an unfinished claim (test_a_claim_left_by_a_previous_controller_is_never_retried, test_reenabling_inside_the_window_never_fires).

### CANCEL can be followed immediately by a new Auto OFF
**Location:** kubernetes/apps/apps/cloud-power/app.py:1058, kubernetes/apps/apps/cloud-power/app.py:1269  
**Severity:** important
**Accepted, fixed in d47e6336:** CANCEL consumes the due slot in its own write (test_cancel_at_the_slot_does_not_start_an_auto_off).

### Auto result failures do not stop the phase machine
**Location:** kubernetes/apps/apps/cloud-power/app.py:1208, kubernetes/apps/apps/cloud-power/app.py:1269  
**Severity:** important
**Accepted, fixed in d47e6336:** tick() skips the phase machine when automation left the state unloaded/dirty (test_a_conflicting_result_write_stops_the_tick_and_never_refires).

### The final pool push blocks ON until shutdown starts
**Location:** kubernetes/apps/apps/cloud-power/app.py:1325, kubernetes/apps/apps/cloud-power/app.py:1846, kubernetes/apps/apps/cloud-power/app.py:1755  
**Severity:** important
**Accepted, fixed in d47e6336:** the in-lock push is gone: a bounded gate (<= 3 ticks) waits for MIRROR.settled() while the worker pushes outside the scheduler lock; every mirror attempt has a 20 s PVE budget (PolicyGateTests).

### Older mirror requests can overwrite a newer saved policy
**Location:** kubernetes/apps/apps/cloud-power/app.py:385, kubernetes/apps/apps/cloud-power/app.py:1459, kubernetes/apps/apps/cloud-power/app.py:1755  
**Severity:** important
**Accepted, fixed in d47e6336:** desired_fn is evaluated inside the mirror lock from the published snapshot (test_a_queued_attempt_pushes_the_latest_setting).

### Applied status can describe the previous settings
**Location:** kubernetes/apps/apps/cloud-power/app.py:389, kubernetes/apps/apps/cloud-power/app.py:426, kubernetes/apps/apps/cloud-power/app.py:1569  
**Severity:** important
**Accepted, fixed in d47e6336:** `applied` compares the last read with the CURRENT desired value (test_applied_follows_the_current_setting_not_the_last_attempt).

### Failed read-back preserves a policy already known to be obsolete
**Location:** kubernetes/apps/apps/cloud-power/app.py:403, kubernetes/apps/apps/cloud-power/app.py:412  
**Severity:** important
**Accepted, fixed in d47e6336:** every successful read is recorded at once; a write makes it unknown until read back (test_a_failed_read_back_is_uncertain_not_the_old_value).

### An empty stored value writes defaults into PVE
**Location:** kubernetes/apps/apps/cloud-power/app.py:694, kubernetes/apps/apps/cloud-power/app.py:1187  
**Severity:** important
**Accepted, fixed in d47e6336:** only an ABSENT key means defaults; an empty string is invalid and preserved (test_empty_stored_value_is_invalid_not_default).

### Invalid result timestamps break otherwise independent manual actions
**Location:** kubernetes/apps/apps/cloud-power/app.py:715, kubernetes/apps/apps/cloud-power/app.py:925  
**Severity:** important
**Accepted, fixed in d47e6336:** result_at must be finite and < 2100; local_label never raises (test_unrepresentable_timestamps_*).

### The tests miss the claimed persistence guarantees
**Location:** scripts/tests/test_cloud_power.py:951, scripts/tests/test_cloud_power.py:962, scripts/tests/test_cloud_power.py:1043, scripts/tests/test_cloud_power.py:1181  
**Severity:** important
**Accepted, fixed in d47e6336:** added AutoReviewRegressionTests (operation counts, landed-reply-lost claim, restart with an inherited claim, DST ticks through the scheduler), mirror concurrency and the policy gate; 7 of them fail on 2ceafe77.

### Polling can overwrite a time edit before change fires
**Location:** kubernetes/apps/apps/cloud-power/app.py:1559, kubernetes/apps/apps/cloud-power/app.py:1591  
**Severity:** nit
**Accepted, fixed in d47e6336:** a time field is protected from focus/first keystroke; blur without a change hands it back to the poll.

### The standing shutdown warning is hidden in a tooltip
**Location:** kubernetes/apps/apps/cloud-power/app.py:1496, kubernetes/apps/apps/cloud-power/app.py:1584  
**Severity:** important
**Accepted, fixed in d47e6336:** the row itself shows 'next Sat 22:00 EEST - drains CI jobs, then stops EVERY VM/LXC'; the zone is in every label.

## Diff stat

```text
 kubernetes/apps/apps/cloud-power/app.py          | 536 ++++++++++++++++++++++-
 kubernetes/apps/apps/cloud-power/deployment.yaml |  15 +-
 kubernetes/apps/apps/homepage/configmap.yaml     |   4 +-
 kubernetes/apps/apps/homepage/deployment.yaml    |   2 +-
 scripts/tests/test_cloud_power.py                | 482 +++++++++++++++++++-
 5 files changed, 1022 insertions(+), 17 deletions(-)
```