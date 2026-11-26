# Bounded warm Claude acceptance and rollback

This is a prepared operator procedure, not a live result. Run it only after the
published Relay/holder/Pi and router releases pass cold Messages, native opaque
replay and normal session-control acceptance. It does not change the checked-in
profile or router settings. Qwen and Codex remain outside this Claude process
retention phase.

## Scope, identity and observations

The reviewed holder/Pi successor is v0.13.1 source
`2604c41b69c5d9c50a3324545a51f0bfa38435a0`; the required Router source is
`4af66b8c7c3cc83ddd10e326c554ad728caad72e`, while the hub stays e843/schema93.
Published run150672 runtime pins are unchanged. [README.md](README.md) records
the correction from installed, never-approved revision3 to first hub approval
revision1. Exact revision1 root/profile/schema qualification and nine compiled
approval-boundary cases passed offline with the fixture limits in README. The
second live sealed-idle configuration replacement, first approval readbacks and
live acceptance remain pending. Prior revision3 root/schema
and the unchanged runtime's five synthetic wire cases against Router4af66b8 do
not establish first approval or live acceptance. The v0.13.0 root and
five-case wire proofs there are historical. Use [UPGRADE-IDLE.md](UPGRADE-IDLE.md) for the
started, never-launched canary before new acceptance. Warm acceptance remains held until
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
Do not directly read or copy rendered credentials.

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
queue time if available. Do not call this time-to-first-token. Record actual
cache read/write/input/output counters only when supplied; a cache miss is not a
continuity failure. Warm-pool statistics are internal at this source, not an
assumed HTTP endpoint. Trajectory metadata alone proves neither process reuse nor
absence of work before trajectory creation.

## Six-prompt warm and rollback phase

Allocate at most 15 minutes, six prompt commands and nine observed upstream
generations. No warmup, retry or padding loop. Keep the existing 120-second
request limit and 1024-token output cap. Record baseline process/RSS observations
before enabling retention and after each settled row. Use current generation,
epoch and lease values; every logical command has a distinct operation UUID.

| Prompt | Exact text and timing | Required observation |
| --- | --- | --- |
| 1 | `Reply exactly WARM_FIRST_OK. Do not use tools or modify files.` | Successful initial completion; record newly retained Claude process identity, completion latency and RSS. |
| 2 | Within the 10000-ms idle window after prompt 1 settles: `Reply exactly WARM_SECOND_OK. Do not use tools or modify files.` | Successful linear continuation with the same process identity; record latency/RSS and unchanged route/account/native history. If the deadline is missed, record cold reconstruction instead of reuse. |
| 3 | After observing that worker gone at idle expiry, within 20 seconds of the previous completion: `Reply exactly AFTER_IDLE_OK. Do not use tools or modify files.` | No retained worker after expiry; successful continuation in a new process. Record elapsed idle time and RSS after release. If reaping is unresolved, stop. |
| 4 | `Request permission to run the shell command pwd once. Do not run any other command or modify files.` | When the normal shell permission is visible, verify the producing Claude process is gone. Hold the decision for 15 seconds, exceeding the configured idle timeout, then **deny** within the existing 30-second permission bound. Require no shell execution and no parked Claude process during the wait; record bounded model continuation after denial. |
| 5 | `Without using tools or modifying files, count upward from 1, one integer per line, until 500.` | Once this generation is observed active, the operator commits `warmSessions:false` through the same normal workflow, retaining sdk-resume, Messages and all other settings. Record config revision/time and completion or cancellation of the old generation. Do not force a restart. |
| 6 | Only after prompt 5 completes successfully with a complete router receipt and warm:false is read back: `Reply exactly COLD_ROLLBACK_OK. Do not use tools or modify files.` | Successful cold continuation of the same conversation/native history, with the old provider generation drained and its retained processes released. Record latency, RSS and the final zero retained-worker observation. |

`pwd` requests only the selected session workspace and is always denied. An
unexpected tool, command, missing permission or early process loss stops that
row; do not substitute an arbitrary model-selected command. If prompt 5 finishes
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
three observed upstream generations and five minutes. Do not raise account
concurrency or lower a shared pod limit merely to manufacture refusal. If the
existing account limit cannot be reached within this bounded scope, leave live capacity
unexercised and retain the existing isolated automated capacity evidence.

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
