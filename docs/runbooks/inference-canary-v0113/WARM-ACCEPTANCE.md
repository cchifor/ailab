# Bounded warm Claude acceptance and rollback

**Current Router:** [the native output replay release](../relay-router-messages-native-output-20261126.md) defines source `4e4bb34e2f968f9450aabe9cf228d3e70c8bab71`, image `registry.chifor.me/llm-router/router@sha256:f180cf7f1f960782a46dff6bb73cb955ce34dcb6e083daed0b1202f58753b457`, and bridges the unchanged warm/provider source below. Preserve historical proof; completed cold/lifecycle acceptance and the separately assigned exclusive warm window remain required.

Operator decision **Relay bus 2086 (2026-11-26)** selects the original six prompts
with at most **18 minutes / 14 observed upstream generations**, and a separate
capacity phase of **6 minutes / 5 generations**, still two sessions / four prompts.
Allow at most one scoped Relay read per prompt. The 120-second prompt limit,
1024-token cap, denied `pwd`, no-retry rule and existing cleanup are unchanged.
This records the delegated scope decision; merging it does not start a window or
perform a configuration change.

This is a prepared operator procedure, not a live result. Run it only after the
published Relay/holder/Pi and router releases pass cold Messages, native opaque
replay and normal session-control acceptance. It does not change the checked-in
profile or router settings. Qwen and Codex remain outside this Claude process
retention phase.

## Scope, identity and observations

The reviewed holder/Pi successor is v0.13.1 source
`2604c41b69c5d9c50a3324545a51f0bfa38435a0`; the required Router source is
`4e4bb34e2f968f9450aabe9cf228d3e70c8bab71`, while the hub stays e843/schema93.
Published run150672 runtime pins are unchanged. [README.md](README.md) records
the correction from installed, never-approved revision3 to first hub approval
revision1. Exact revision1 root/profile/schema qualification and nine compiled
approval-boundary cases passed offline with the fixture limits in README. The
first-registration preparation and its approval-boundary proof are historical;
verify the current approved profile and preceding live acceptance readbacks.
Prior revision3 root/schema
and the unchanged runtime's five synthetic wire cases against Router4af66b8 do
not establish first approval or live acceptance. The v0.13.0 root and
five-case wire proofs there are historical. [UPGRADE-IDLE.md](UPGRADE-IDLE.md) applies
only to its never-launched preparation stage; do not repeat it on a holder that
has executed commands. Warm acceptance remains held until
the preceding cold/lifecycle gates pass. Opaque replay uses the operator-selected
paired live/synthetic evidence method; final outbound effort/cap fields and opaque
bytes remain unproven live.

The router's `provider-claude` settings apply to **all router-managed Claude
accounts**, not just the canary route. The operator must select an exclusive,
drained maintenance window or an already isolated instance with deliberate seat
ownership. Do not copy a subscription into another writer or change the shared
settings under unrelated active traffic. Preserve the current SQLite database,
account, principal and native history throughout this exercise.

Record the final source/image digests, CLI version 2.1.283, configuration revision,
dedicated route/account, original provider settings and the operator's selected
window. Keep `transport:"sdk-resume"` and native Messages enabled. The proposed
temporary change is `warmSessions:true` and `warmIdleMs:10000`, retaining the
existing `maxRunning` and all other settings. Submit it through the existing
reviewed operator configuration workflow; this document is not a config mutation
or approval. Verify the committed revision and effective settings before prompts.

At required router source
[`4af66b8c7c3cc83ddd10e326c554ad728caad72e`](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/adapters/claude-sdk/warm.ts#L149),
each provider pool has maximum two retained workers. Age is measured from
[Worker creation, before process startup](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/adapters/claude-sdk/warm.ts#L19).
At the next reuse attempt, a worker aged >=300000 ms or with >=32 completed turns
is [retired instead of reused](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/adapters/claude-sdk/warm.ts#L168);
these checks are not an active-generation kill deadline. The separate idle timer
starts after a reusable turn settles; [idle retention is configurable from 1000
to 300000 ms](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/plugins/provider-claude/index.ts#L25).
These bounds are distinct from the pod-wide `maxRunning` and account admission
limits. Do not invent configurable age, turn or pool-size fields.

Use a fresh normal Relay Claude session and its existing managed binding, then
the owned lease and command/permission receipts described in
[LIVE-ACCEPTANCE.md](LIVE-ACCEPTANCE.md). Keep effort, model, system and tool
configuration unchanged across measured turns. Renew the lease every 20 seconds.
Do not directly read or copy rendered credentials. Keep peer traffic and other
callers out of the selected session/account window; do not send messages to
trigger a read or add warmup traffic. Deliver no messages to the fresh session
during this phase; this excludes peer text, not the normal inbox-read reminder.

Apply the shared [scoped Relay communication-read rule](LIVE-ACCEPTANCE.md#scoped-relay-communication-reads)
throughout this phase and the capacity phase below. Ordinary prompts retain
Relay's inbox reminder, including those whose exact text says not to use tools.
The owner may separately allow each fully inspected
`mcp__relay__relay_messages_read` request once, only for the own inbox or exact
approved channel and bounded arguments in that rule. Re-read the pending detail
before deciding. Inbox reads may set `read_at` and publish a notification; they
do not acknowledge or send messages or grant access. The `pwd` denial remains a
separate shell decision; a read approval cannot satisfy it.

Allow at most one such read per prompt; a second read stops advancement through
normal cancellation/cleanup. All read round trips, upstream generations and owner
decision time consume the phase limits and idle window. Keep the exact prompts below;
do not ignore extra calls, add prompts or repeat a turn to manufacture reuse.
Record every observed generation and process transition. If a read prevents
establishing the required timing, reuse or capacity outcome, mark that claim
unobserved or inconclusive and stop the affected phase. A permission title,
native `in_progress` status or completed read does not establish worker reuse.
These manual instructions do not reopen a stopped Phase A run or assign its
replacement window. Follow only the separately reviewed
[owner-driver replacement procedure](OWNER-PHASE-A.md#review-the-known-stopped-run-replacement)
for that known stopped run; it preserves historical evidence and spent counters.
Warm and capacity execution still require their own preceding gates and window.

Before execution identify an operator-approved, read-only observation of the
router process tree in its PID namespace. Record only process PID, parent PID,
start time, RSS bytes and observation time. Do not collect command lines,
environments, memory contents or raw model traffic. Identify the router and its
Claude descendants in the exclusive window, including descendants of a reaping
process. PID alone cannot establish identity: compare start time as well. If
process identity or disappearance cannot be established, leave reuse/release
unobserved. Never kill a process to manufacture the result.

The [bounded process metadata collector](../warm-process-observer.md) supports
this observation using the operator-supplied root PID and start ticks. Its
samples cover only visible, observed ancestry; they do not establish provider
identity, prove reaping or exclude processes between samples. Keep unknown
ancestry, gaps and the final stop record with the report. Collector exit0 is not
a warm-acceptance verdict, and its use does not authorize another workload.

Pair these observations with settled scoped trajectory metadata, normal Relay
receipts and request-correlated native outcome. Record full completion latency
from accepted prompt submission until the completed native turn, separately from
queue time if available. Also record each upstream attempt's start/end and latency,
and owner permission-decision time separately; do not attribute review time to
provider inference or call completion latency time-to-first-token. Record actual
cache read/write/input/output counters only when supplied; a cache miss is not a
continuity failure. Warm-pool statistics are internal at this source, not an
assumed HTTP endpoint. Trajectory metadata alone proves neither process reuse nor
absence of work before trajectory creation.

## Six-prompt warm and rollback phase

Allocate at most 18 minutes, six prompt commands and 14 observed upstream
generations. No warmup, retry or padding loop. Keep the existing 120-second
request limit and 1024-token output cap. Record baseline process/RSS observations
before enabling retention and after each settled row. Use current generation,
epoch and lease values; every logical command has a distinct operation UUID.

Before each prompt, reserve its bounded continuation allowance against the
remaining total: two generations for each ordinary prompt, three for the `pwd`
prompt (one read, the shell request and post-denial continuation). These reserve
13 of the 14 allowed generations; unused headroom authorizes no extra prompt,
second read, unexpected tool or retry. Before each permission decision, including
denial, recheck remaining time and continuation budget without double-counting an
existing reservation. Count every observed attempt and in-flight generation,
including failed/cancelled work. Unknown coverage or exhausted bounds stops
advancement; cancel through normal cleanup and record any racing completion.

At the [pinned source](https://git.chifor.me/cchifor/llm-router/src/commit/4e4bb34e2f968f9450aabe9cf228d3e70c8bab71/packages/adapters/claude-sdk/warm.ts#L119), a worker is retained only after successful `end_turn`
without `tool_use`. A tool boundary reaps the producing worker before returning
its terminal result; it is never held across owner permission waits. Reuse also
requires the same account/conversation, fingerprint and extending native history.
For prompt 2, compare its **first upstream attempt** with prompt 1's final retained
worker using PID **and start ticks**, within 10000 ms of prompt 1's final native
completion. A Relay read can reap that worker and require a new process for the
post-read generation. The final prompt PID need not match. If sampling cannot
establish first-attempt identity, leave reuse unobserved; no later PID, cache hit
or prompt completion substitutes for that observation.

| Prompt | Exact text and timing | Required observation |
| --- | --- | --- |
| 1 | `Reply exactly WARM_FIRST_OK. Do not use tools or modify files.` | Successful initial completion; record newly retained Claude process identity, completion latency and RSS. |
| 2 | Within the 10000-ms idle window after prompt 1's final native completion: `Reply exactly WARM_SECOND_OK. Do not use tools or modify files.` | Observe the first upstream attempt on prompt 1's final retained PID/start ticks within that interval. Record per-attempt latency/RSS and unchanged route/account/native history, plus any read-boundary retirement and reconstructed continuation. Final-PID equality is not required. A missed interval is cold reconstruction; missing first-attempt identity is unobserved reuse. |
| 3 | After observing that worker gone at idle expiry, within 20 seconds of the previous completion: `Reply exactly AFTER_IDLE_OK. Do not use tools or modify files.` | No retained worker after expiry; successful continuation in a new process. Record elapsed idle time and RSS after release. If reaping is unresolved, stop. |
| 4 | `Request permission to run the shell command pwd once. Do not run any other command or modify files.` | When the normal shell permission is visible, verify the producing Claude process is gone. Hold the decision for 15 seconds, exceeding the configured idle timeout, then **deny** within the existing 30-second permission bound. Require no shell execution and no parked Claude process during the wait; record bounded model continuation after denial. |
| 5 | `Without using tools or modifying files, count upward from 1, one integer per line, until 500.` | Once this generation is observed active, the operator commits `warmSessions:false` through the same normal workflow, retaining sdk-resume, Messages and all other settings. Record config revision/time and completion or cancellation of the old generation. Do not force a restart. |
| 6 | Only after prompt 5 completes successfully with a complete router receipt and warm:false is read back: `Reply exactly COLD_ROLLBACK_OK. Do not use tools or modify files.` | Successful cold continuation of the same conversation/native history, with the old provider generation drained and its retained processes released. Record latency, RSS and the final zero retained-worker observation. |

Measure prompt 3's twenty-second interval from prompt 2's final native generation
completion, after observing its retained worker gone at idle expiry. A missed
interval stops that timed claim; do not repeat a prompt to reset the clock.
For prompt 5, the configuration change must overlap an observed active upstream
generation, not a Relay-read permission wait. Record which attempt was active.

For enable/disable and final idle-value restoration, use a fresh
`GET /admin/v1/plugins/provider-claude/config`, preserve complete current `values`,
and `PATCH` that endpoint with `{values:FULL_CURRENT_VALUES}` changing only the
intended settings. `If-Match` is the fresh **64-hex plugin-settings revision**,
not the integer global config revision. Read back effective settings; do not
restore a stale snapshot, submit partial values or retry a conflict/lost response
without inspection. The operator performs these existing authorized mutations.

`pwd` requests only the selected session workspace and is always denied. Apart
from separately reviewed communication reads under the rule above, an unexpected
tool, command, missing permission or early process loss stops that row; do not
substitute an arbitrary model-selected command. If prompt 5 finishes
before the configuration change, the active-generation rollback claim is
unexercised, even if prompt 6 succeeds. Cancellation or an uncertain router
receipt stops the phase; settled cancellation does not authorize prompt 6.
Reconstruction is outside this six-prompt budget. Do not restart or repeat to force overlap.
If the configuration update fails or work becomes uncertain, inspect the normal
receipt and preserve history; do not silently reconcile or downgrade transport.

Compare process RSS as observations, not a promised memory saving or latency SLA.
The two successful text turns are one cold/warm pair, not a percentile benchmark.
Idle expiry is a live claim; age/turn bounds remain the separately cited offline
test evidence unless a distinct bounded phase actually exercises them.

## Separate capacity phase

Capacity qualification must name the exact limit it exercises. Account admission,
pod run slots and the two-worker warm pool are different gates. The same-session
busy check is not a pool-capacity test. Do not saturate shared production slots.

Only in the operator's exclusive scope, use the current documented account
admission limit and at most two fresh managed canary sessions, four prompt commands,
five observed upstream generations and six minutes. Do not raise account
concurrency or lower a shared pod limit merely to manufacture refusal. If the
existing account limit cannot be reached within this bounded scope, leave live capacity
unexercised and retain the existing isolated automated capacity evidence.

The optional
[Claude account-capacity window](CLAUDE-CAPACITY-WINDOW.md) documents a separate
operator-approved exception for the observed account limit 4: temporarily 1, then
restore 4 using fresh revisions. It uses the same two-session/six-minute/five-generation budget
and global run cap; documentation merge does not authorize execution. Without
that explicit window decision, this default remains unchanged and the row stays
unexercised when the existing limit is unreachable.

Apply the same at-most-one scoped read per prompt and full time/generation
accounting. Reserve two generations for A, one for B's refusal probe (any
unexpected upstream execution counts and fails that refusal claim), zero for
the explicit local reconciliation command, and two for the released continuation.
Recheck before permissions/continuations; do not omit reads to fit the budget.
Submit B only while A is observed holding upstream admission, never merely while
A waits for a read permission. No peer traffic may occupy the account.

Start a bounded no-tools counting prompt on session A. While it is observed
active, submit one text-only `Reply exactly CAPACITY_PROBE. Do not use tools or
modify files.` prompt on session B. Require the expected configured-capacity
refusal, identified by its actual status/code and settled router outcome, with no
additional upstream execution or Claude child. Observe the scoped trajectory
attempt delta separately. A pod-slot timeout or warm-pool refusal occurs after
the router records an attempt and does not qualify this pre-provider account
admission check. A queued request or a completed first request is not a refusal
proof. Cancel A through normal Relay control if still active and require settled
cancellation/reaping. Inspect B's router outcome: a refused request can leave its
coordinated branch uncertain even without upstream execution. After inspecting
effects, the operator may explicitly submit `/relay-reconcile confirmed` on idle B
as the third prompt, recording inspection/reconstruction and no upstream execution.
Only with a complete or explicitly reconstructed branch after capacity release,
one `Reply exactly CAPACITY_RELEASED_OK. Do not use tools or modify files.` prompt
on B must succeed (fourth prompt when reconciliation was needed). Do not
automatically reconcile or retry a refused generation. The bounded phase supplies
no recipe to occupy three warm slots;
do not claim that specific pool limit passed from a different admission refusal.

## Close, restore and record

Close the canary sessions and release owned leases through normal controls.
Leave warm retention disabled after rollback. Restore the original idle value
through the operator workflow if changed, preserving `warmSessions:false` until
promotion is separately decided. Confirm settings and process disappearance.
Credential revocation follows the existing owner cleanup workflow; it is not a
substitute for warm-to-cold rollback.

Preserve the release/configuration identities, scope and window, command receipts,
native outcome, exact process identities/RSS/timestamps, latency definition and
measurements, actual usage counters, capacity gate and attempt deltas, and the
active-transition/cold-continuation evidence. Mark each claim passed, failed,
blocked or unexercised separately. Do not restore an old router database, delete
holder histories, downgrade a v2-connected hub or relabel a partial result as
complete acceptance.

Source references: router `packages/plugins/provider-claude/index.ts`,
`packages/adapters/claude-sdk/warm.ts`, `tests/claude-warm-real.test.ts`, and
`docs/runbooks/inference-session-canary.md`. Actual published-source review and
operator readbacks are prerequisites; this prepared procedure records no live
operation or result.
