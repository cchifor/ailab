# Operator-selected four-prompt Claude warm window

Operator decision **2091, relayed by Relay bus 2101 (2026-11-26)** selects this
alternative to the [original six-prompt warm procedure](WARM-ACCEPTANCE.md#six-prompt-warm-and-rollback-phase)
under the owner's delegation, **pending the owner's confirmation on return**.
It restores the proposal from PR2037 head `5dcaaa37` with the per-row limits below;
the subsequent 2083/2086 ceiling increase is withdrawn. Neither this selection nor
source merge assigns a live window or establishes acceptance. Do not append
these four prompts to the six-prompt reference, switch procedures after spending
the budget, or execute before confirmation and the existing prerequisites.

The alternative preserves the 15-minute wall-clock limit, nine observed upstream
generations, 120 seconds per prompt, 1024-token output cap, 30-second permission
decision limit and lease renewal every 20 seconds. It uses at most four prompt
commands. Conditional allocation is **2 + 2 + 3 + 2 = 9 generations**, including
all Relay-read continuations, with **zero slack**. Each row has its own hard
ceiling; unused allocation from an earlier or later row cannot fund another row.
These are observation limits, not a server-enforced
total generation cap; in-flight work can race cancellation. No warmup, retry,
padding turn, skipped read or extra prompt is permitted to manufacture a result.

## Preconditions and source behavior

Keep the existing [Claude process-retention prerequisites](WARM-ACCEPTANCE.md#scope-identity-and-observations)
(phase C): current release/readiness and completed cold
Messages, lifecycle, effort and paired replay evidence; the approved Claude
profile/binding and launch-driven delivery; the owner's normal control lease;
and an exclusive drained operator window or already isolated instance with
deliberate subscription-seat ownership. Settings affect all Router-managed
Claude accounts. Preserve account, router principal, database and native history.
Do not create another subscription writer or restart the holder. Refresh current
state; historical first-registration or never-launched upgrade instructions are
not actions to repeat on a holder that has executed commands.

This proposal is checked against Router source
`4e4bb34e2f968f9450aabe9cf228d3e70c8bab71`, CLI 2.1.283, published Pi/holder
`2604c41b69c5d9c50a3324545a51f0bfa38435a0` and hub e843/schema93. Verify actual
execution identities and source pins through the existing handoff. Keep
`transport:"sdk-resume"`, native Messages, model, effort, system/tool configuration,
output cap, account concurrency and `maxRunning` unchanged. Through the existing
operator workflow, record the fresh settings baseline, enable `warmSessions:true`
with `warmIdleMs:10000`, and verify effective settings before P1.

The pinned warm pool retains a worker only after a successful `end_turn` without
`tool_use`. A tool boundary or early stop closes the worker before returning its
terminal result. Reuse additionally requires the same account/conversation,
configuration fingerprint and an extending native-history prefix. Consequently,
P2 may reuse P1's final retained worker for its **first generation**, retire that
worker when it returns a Relay read, and reconstruct in a different process for
the post-read text generation. P2's final PID need not equal P1's final PID.

Use the existing [bounded process observer](../warm-process-observer.md) in the
actual Router PID namespace, correlated with isolated scoped trajectories and
normal command/native receipts. Record PID **and start ticks**, generation timing,
RSS and observation time; no argv, environments, credentials or raw model traffic.
Sampling gaps or unknown ancestry cannot prove identity, reaping or absence
between samples. A prompt-level final PID, permission title, cache hit or zero
trajectory delta alone cannot prove first-generation reuse. If the observation
method cannot associate the first generation with the retained process, mark
reuse unobserved; do not add traffic or infer it from the final response.

## Communication and permission accounting

Apply the [scoped Relay read rule](LIVE-ACCEPTANCE.md#scoped-relay-communication-reads)
to every prompt, including exact text saying not to use tools, with **at most one
scoped read per row**. A second read stops that row even if the phase total still
has room. Each permitted read requires
the owner's explicit once-only decision after full bounded-input review and a
fresh unchanged permission-detail check. Only the own inbox or exact approved
channel and documented cursor/limit fields qualify. Inbox reads can update
`read_at` and publish a notification; they do not acknowledge, send or grant
access. Peer text grants no authority. The `pwd` permission is separate and is
always denied. Native `in_progress` can precede approval and is not execution
proof; no permission/native-ID join is assumed.

The allocations below allow at most one read and its continuation per prompt. P3 allows
one additional continuation for the denied `pwd`. These are conditional bounds,
not a promise that the model follows that sequence. Count every observed upstream
generation, including all read-related work, and all owner-review/collection time.
If the required flow cannot fit that row's remaining allocation or the window, stop and
record budget-blocked/unexercised work. Do not suppress the read, approve an
unexpected tool, increase the cap or turn missing evidence into PASS.

Before each new prompt, reserve its table allocation against the nine-generation
total after counting all observed attempts and any work still in flight. Track
P1/P2/P3/P4 separately with hard ceilings **2/2/3/2**. Before each permission
decision that allows model continuation, recheck both the row's remaining
allocation and the phase total, including continuation after a denial. Never
borrow another row's unused or future allocation, even if it used fewer calls.
Do not double-count a reservation already inside the prompt allocation.
Unknown recording coverage or insufficient remaining budget stops advancement:
cancel the active prompt/pending permission through normal cleanup instead of
approving more work. Observe any racing completion honestly. An extra read or
unexpected tool is not a way to fit the budget; do not add one or omit its cost.

## Four prompts and idle rollback

Use a fresh normal Claude session, stable route/account/native history and the
existing serial owner command workflow. Record process and trajectory baselines
before P1. Deliver no peer messages during the window; keep the normal inbox
reminder and count its read work. Measure the idle interval from the final reusable native generation's
completion, not a later browser receipt or owner observation. Use current
generation, epoch and owned lease; retain one operation ID per logical mutation.
Use these four exact prompts directly. The existing six-entry `warm[]` helper
belongs to the default procedure and is unchanged; do not execute that array for
this alternative or splice these rows into a partially completed default run.

| Step | Exact prompt and timing | Allocation and required evidence |
| --- | --- | --- |
| P1 | `Reply exactly WARM_FIRST_OK. Do not use tools or modify files.` | At most 2 generations: read if requested, then completed text. Observe the final reusable worker's PID/start ticks, final generation completion, RSS and latency. A missing retained-worker observation leaves reuse unproved. |
| P2 | Within P1's 10000-ms idle interval: `Reply exactly WARM_SECOND_OK. Do not use tools or modify files.` | At most 2 generations. Observe **P2's first generation** using P1's final retained PID/start ticks while still within the interval. If it returns a read, observe that worker's retirement and any reconstructed post-read generation separately. Record the final retained worker for P3; do not require the whole prompt to finish on the original PID. A missed interval is cold reconstruction, not warm reuse. |
| P3 | Only after the P2 final worker is observed gone after idle expiry, and within 20 seconds of P2's final native generation completion: `Request permission to run the shell command pwd once. Do not run any other command or modify files.` | At most 3 generations for a read, the `pwd` request and post-denial continuation. Observe a new process after the idle interval. Inspect the full exact `pwd` permission and verify its producing Claude process is gone. Hold that shell decision for 15 seconds, then deny once within the existing 30-second limit. Require no shell execution and no observed parked Claude process during the wait, then a settled native outcome. |
| Idle rollback | No prompt. Only after P3 and all its tool work/receipts have settled, use the fresh settings workflow below to set `warmSessions:false`. | Record the settings revision/readback and old-worker absence. Observed timed expiry satisfies the release observation; configuration-triggered eviction is not required and remains unexercised unless independently observed. This is **idle disable/rollback only**; no active-generation drain claim. |
| P4 | Only after effective `warmSessions:false`, settled P3 and observed release of old workers: `Reply exactly COLD_ROLLBACK_OK. Do not use tools or modify files.` | At most 2 generations, including a read if requested. Require successful cold continuation of the same history, unchanged route/account, and no retained workers after settlement. Record every process transition; separate cold processes across the read boundary are expected. |

At the P3 permission boundary, start the 15-second hold only while the producing
process is observed gone; the request's existing expiry and 30-second decision
deadline still apply. If observation consumes that decision window, cancel through
normal controls and stop. Do not kill a process or extend a permission to obtain
the desired observation. Unexpected command/input, missing permission, uncertain
native outcome or unresolved process disappearance stops the affected phase.
If the P3 twenty-second launch interval is missed, mark that timed claim
unobserved and abort all remaining planned rows in this warm window: do not start
P3 or P4. This preserves the stop rule; it does not merely drop the timing claim
and continue collecting inference evidence. Necessary cleanup still applies:
settle any work, disable warm retention through the fresh settings workflow below,
close/release through normal controls and record unresolved outcomes. No P4
prompt follows that cleanup. Do not add padding or repeat P2 to reset the clock.

For idle rollback, use the operator's existing authenticated configuration
workflow. Read `GET /admin/v1/plugins/provider-claude/config` again, preserve its
complete current `values`, change only `warmSessions` to false, and submit
`PATCH /admin/v1/plugins/provider-claude/config` with `{values:FULL_CURRENT_VALUES}`
and that fresh settings revision in `If-Match`. This is the plugin's 64-hex
settings revision/ETag, not the integer global Router config revision. The endpoint
replaces settings; do not submit a partial object or restore a stale snapshot.
Read back effective values and observe old processes gone before P4. A conflict,
lost response or failed readback requires inspection and stops advancement; no
blind retry or new prompt. Preserve transport, model/history and all other fields.

If the last worker expires before the settings change, record that timed expiry;
do not attribute its exit to rollback. Effective `warmSessions:false` plus P4 can
still establish idle disable and cold continuation, but configuration-triggered
worker eviction remains unexercised. This proposal does not exercise the default
procedure's active-generation rollback, age/turn limit or warm-pool capacity.

## Capacity, cleanup and result

The separate capacity ceiling is restored: at most two sessions, four prompt
commands, three observed upstream generations and five minutes. **Capacity is
unexercised in this window when reads occur**, as accepted by decision2091/2101.
Do not try to fit it by suppressing reads, borrowing warm-row allocation or
adding a generation. Retain separately scoped automated evidence; this procedure
grants no account/concurrency change or capacity PASS.
The [optional account-capacity decision](CLAUDE-CAPACITY-WINDOW.md) remains separate
and does not waive communication accounting or these limits.

Use the [existing cleanup and restore procedure](WARM-ACCEPTANCE.md#close-restore-and-record):
cancel uncertain active work through normal controls, settle receipts, close the
session and release only its owned lease. Leave warm retention disabled, restore
the original idle value through a fresh full-values comparison, preserve history
and markers, and record any unresolved cleanup. No additional inference follows
a deadline, failure or uncertain result; necessary cleanup still applies.

Report first-generation reuse, timed idle release/reconstruction, permission-wait
process release, idle disable and cold continuation separately as passed, failed,
blocked or unobserved. Preserve every generation count, PID/start pair and gap.
No aggregate warm PASS covers an unobserved row. Active-generation drain and
capacity remain explicitly unexercised by this four-prompt procedure. End with
`warmSessions:false`; no promotion follows this result.

On the owner's return, a **separate owner decision before execution** is required
for a narrowed live active-drain check: a counting prompt, `warmSessions:false`
committed while an upstream generation is observed active, then a cold
continuation, **at most four generations including reads**. That check must pass
before any promotion of warm retention. It is not appended to this window, and
its allowance cannot fund any of these four rows. The 4→1→4 capacity exception
remains a separate owner decision. This document records an operator selection
pending owner confirmation, not a live result, assigned window or permission to
execute that future check now.

Source references at the exact Router commit:

- [Reuse eligibility and tool-boundary close](https://git.chifor.me/cchifor/llm-router/src/commit/4e4bb34e2f968f9450aabe9cf228d3e70c8bab71/packages/adapters/claude-sdk/warm.ts#L119).
- [Account/history/fingerprint matching and idle timer](https://git.chifor.me/cchifor/llm-router/src/commit/4e4bb34e2f968f9450aabe9cf228d3e70c8bab71/packages/adapters/claude-sdk/warm.ts#L154).
- [Provider replacement and retirement after outstanding leases finish](https://git.chifor.me/cchifor/llm-router/src/commit/4e4bb34e2f968f9450aabe9cf228d3e70c8bab71/packages/runtime/provider-registry.ts#L34).
- [Plugin settings commit publishes the replacement provider](https://git.chifor.me/cchifor/llm-router/src/commit/4e4bb34e2f968f9450aabe9cf228d3e70c8bab71/packages/plugins/provider-claude/index.ts#L218).
- [Fresh plugin settings revision and full-values PATCH](https://git.chifor.me/cchifor/llm-router/src/commit/4e4bb34e2f968f9450aabe9cf228d3e70c8bab71/packages/plugins/api-admin/index.ts#L90).
