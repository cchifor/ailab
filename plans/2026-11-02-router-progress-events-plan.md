# llm-router: a real Responses event while the upstream thinks (progress events)

## Codex Review

- Changes requested: the existing writer does not safely coordinate timer-originated backpressure or failures with the main loop.
- Activity needs attempt ownership and cleanup semantics; received bytes can also be upstream keepalives without useful model progress.
- Full snapshots matter for SDK compatibility, but stable timestamps, payload size, and repeated-event handling need explicit verification.
- There is no total-duration cap by default. The local routes’ live caps are unverified; make a finite `maxDurationMs` an explicit release decision.
- Add boundary tests for drain, cancellation, terminal events, and late activity after failover. This was a read-only review against `6e8104a`; tests and the supplied CLI experiments were not rerun.

## Context

Codex agents on the local models (`qwen-27b` → cloud2 vLLM, `flash-next` → cloud3 llama.cpp) die with
`stream disconnected before completion: idle timeout waiting for SSE` after 8 reconnects whenever one turn thinks
for more than 300 s. Diagnosed on 2026-11-02 from the router journal, the router image's code, cloud2's vLLM metrics
and the real CLI; nothing below needs re-deriving.

- **The router drops reasoning.** `packages/adapters/providers/chatcompletions.ts` `runSse` yields only
  `delta.content`, `delta.refusal` and `delta.tool_calls`. vLLM/llama.cpp reasoning deltas are read and reset the
  engine's `Watchdog` through `onActivity` (`packages/modules/inference/index.ts:587`), but nothing reaches the client.
- **The client sees only comments.** `packages/plugins/api-openai/index.ts:437-444` sends `: ok`, then
  `response.created`, then `: keepalive` SSE comments every 5 s (`streamKeepalive`) until the first output.
  <!-- codex: The keepalive timer also runs during quiet periods after output, until the streaming handler exits; it does not stop at first output (packages/plugins/api-openai/index.ts:442,513). This matters because the proposed change also affects partially accumulated responses. -->
- **Codex counts events, not bytes.** Its `stream_idle_timeout_ms` (default 300 000; the dev_worker role does not set
  it) is reset only by parsed SSE events. Measured in docker on 2026-11-02 with a fake Responses server, idle timeout
  6 s and a 15 s silent phase:

  | Codex CLI | During the silent phase | Result |
  |---|---|---|
  | 0.162.1 (the workers' version) | `: keepalive` comments every 2 s | cut at 6.0 s, one reconnect, exit 1 "idle timeout waiting for SSE" |
  | 0.162.1 | `response.in_progress` events every 2 s | completed, exit 0, one request |
  | 0.166.0 | comments | same failure |
  | 0.166.0 | `response.in_progress` events | completed, exit 0 |

- **Production evidence.** 2026-11-02 09:29-10:15Z, conversation `ba741bcd` (dev-worker-2's key): 9 identical
  requests, each `CLIENT_CLOSED` at 300.0 s with no `stream.first_content`, router utilization 0.25, while cloud2's
  vLLM had 2 requests running, 0 waiting, 0 preemptions and KV cache at most 44 %. 10 such kills on 11-01, 9 on
  11-02 on cloud2, 1 on cloud3 (flash-next, 11:16Z). The sibling run's turns reached first content at 218-234 s
  (18-22k reasoning tokens), so the 300 s cliff is routinely close. Each retry repeats the whole thinking phase.

## Approach

Send a real, standard Responses event, `response.in_progress`, on a quiet Responses stream **while the upstream is
producing bytes**, so a client's idle timer tracks upstream liveness instead of visible output.

<!-- codex: Raw activity includes comments, pings, and incomplete SSE frames before parsing, so this gate establishes transport activity rather than useful reasoning or even a completed event (packages/contracts/index.ts:260; packages/adapters/providers/sse.ts:161). Keeping the gate preserves silent-upstream detection better than unconditional progress, but upstream-generated keepalives can still conceal a stalled model; document that limitation explicitly. -->

1. **The engine exposes upstream activity.** `Execution` gains an optional `activityAt?: () => number | undefined`:
   the epoch ms of the latest raw upstream chunk of the attempt now running (undefined before the first one). The
   engine sets it in the same `onActivity` callback that calls `watchdog.activity()`, and resets it when an attempt
   starts, so a failover's earlier attempt does not count. Additive and optional: other consumers of `Execution`
   (chat-api, the Assistant loop, the vendored copy in cchifor/platform) are unaffected.
   <!-- codex: P1: Resetting only at attempt start leaves stale activity exposed during upstream cleanup, failure recording, and selection; an abandoned provider can also invoke its callback after the next attempt starts (packages/modules/inference/index.ts:635,644,683,698; tests/stream-timeouts.test.ts:140). Make activity attempt-owned, invalidate it when that attempt stops, and ignore callbacks from inactive or aborted attempts. -->
   <!-- codex: Specify availability before iteration and after completion/cancellation: execute builds and returns Execution before its generator runs (packages/modules/inference/index.ts:349,354,367). Also correct the consumer inventory: chat-api calls routerAgent indirectly, while agent-llm/index.ts:95, admin-bridge/index.ts:118, agent-image/index.ts:75, and router-engine/tools.ts:27 consume executions directly; compatibility of the separate platform copy was not verified here. -->
2. **api-openai writes progress events.** In the Responses stream of a non-native route only, the existing keepalive
   `tick` decides what a quiet period gets:
   - `response.in_progress` when `response.created` has gone out, no terminal event has started, the stream has had
     no *event* (comments do not count) for `streamProgress.intervalMs` (15 s, exported and settable for tests like
     `streamKeepalive`), and `activityAt()` is later than the last event written;
   - otherwise `: keepalive`, as today.
   <!-- codex: Define this as activity since the previous event, rather than continuous or necessarily recent activity: one chunk followed by a stall can authorize one delayed heartbeat, which extends the client's idle window once. Test that it cannot authorize a second heartbeat without new activity, and that regular visible events suppress progress. -->
   The event goes through the same writer as every other event, so `sequence_number` stays strictly increasing. It
   is written synchronously from the timer, never while a write awaits drain (the existing `draining` guard), and
   never after the terminal event (`routerTerminal` / `closed`).
   <!-- codex: P1: The existing guard is one-directional: tick checks draining, but sendLine writes without checking it, so a timer event whose write returns false can overlap a subsequent main-loop write (packages/plugins/api-openai/index.ts:442,451). Share one drain state between both producers and allow at most one pending heartbeat; reusing responseEvent alone does not provide this coordination. -->
   <!-- codex: P1: responseEvent returns a promise, and sendLine may reject on drain timeout or cancellation; calling it from a timer without awaiting/handling that promise bypasses the handler's catch (packages/plugins/api-openai/index.ts:451,455,458,491). Specify how timer failures reach request cleanup even while the main loop awaits upstream, and how a pending timer write settles before raw.end(). -->
   <!-- codex: Allocate sequence_number and update lastEventAt at actual frame submission, including write(false), rather than at enqueue time or after drain; comments must update only lastWrite (packages/plugins/api-openai/index.ts:443,451,458). If serialization defers a heartbeat, recheck eligibility and construct its snapshot at dispatch so it cannot follow newer deltas with stale output. -->
   <!-- codex: P1: routerTerminal covers normal completion, terminal is set separately on errors, and closed is set only in finally; neither alone covers disconnect/shutdown during upstream cleanup (packages/plugins/api-openai/index.ts:449,462,489,491,513). Gate progress on both terminal states, both abort signals, the whole-request deadline, and the writable's ended/destroyed state, with boundary tests for failure, incomplete completion, client close, and shutdown. -->
3. **Payload.** `{type: 'response.in_progress', sequence_number, response: responseObject(id, model, a, 'in_progress',
   codexAdapted)}`: the same object `response.created` carries, with the output so far. Before the first output that
   is a few hundred bytes. It stays truthful for a client that replaces its snapshot on this event.
   <!-- codex: responseObject recomputes created_at from Date.now() on every call, so each 15-second heartbeat would give the same response a different creation time (packages/plugins/api-openai/index.ts:244). Capture one creation timestamp and preserve it across created, progress, and terminal envelopes. -->
   <!-- codex: Accurate output is necessary for the pinned OpenAI Node helper: its [v7.23.0 response accumulator](https://github.com/openai/openai-node/blob/v7.23.0/src/internal/responses/response-accumulator.ts#L1200) replaces the snapshot on lifecycle events, so an empty-output heartbeat after text or tool deltas can break later accumulation. Test text, refusal, and partial tool arguments with stable item identities and indexes, including further deltas after the heartbeat. -->
   <!-- codex: The payload can grow to the accumulator's 8 MiB character limit, and repeated serialization/transmission is not bounded by that retained-content check (packages/plugins/api-openai/index.ts:243,485). Decide explicitly whether the initial release covers only pre-output silence or also accepts large repeated snapshots; the latter needs a slow-reader/large-payload test and a documented resource bound without silently truncating snapshots. -->
4. **Unchanged:** Chat Completions streams (no standard heartbeat chunk; their clients' read timeouts are satisfied by
   comments), native Codex routes (the upstream's own events pass through), non-streaming requests, the journal and
   metrics (no event per heartbeat), and the engine's own timers.
   <!-- codex: Comments satisfy byte-oriented network read timeouts, but not every Chat Completions client's application-level event/content timeout; the blanket client claim is unsupported. Describe the unchanged wire behavior and retain explicit protocol/native exclusions at the shared tick, since all streaming branches use it (packages/plugins/api-openai/index.ts:442,466,469,472). -->
5. **A silent upstream stays silent.** No upstream bytes since the last event (a queue, a prefill, a cold model load,
   a dead server) means no progress event. The client's idle timer and the router's wait timer keep bounding it, as
   today.

## Out of scope (follow-ups, named so they are not lost)

- Forwarding the reasoning itself (Responses reasoning items, `reasoning_content` on chat). It would make an agent's
  thinking visible, but adds output items that clients store and send back as input, which the subset parser refuses
  for non-Codex callers. Separate design.
- cloud3 cold loads: llama-swap sends no bytes while it loads a model (up to the account's `waitMs` 600 s), so a
  Codex client still gives up at 300 s. That is the dev_worker profile's `stream_idle_timeout_ms` (ailab), not this.
  <!-- codex: Named routes ignore Account.timeouts; those settings apply to virtual direct-model policies (packages/contracts/index.ts:85; packages/modules/routing/index.ts:69). For flash-next, cite the resolved route's timeoutMs instead: wt-progress-events/docs/runbooks/llm-router.md:2820 records 600000 on qwen3.8-flash-next, with the pointer recorded at line 2821. -->
- The 429s on cloud2 (no router queue, 4 slots) and the edge-proxy roll that orphaned upstream legs: separate.
- Bounding how long the model thinks (the router drops `reasoning_effort` for local servers; cloud2's template
  defaults to xhigh): a serving decision for the owner.

## Critical files

llm-router (branch `fix/responses-progress-events` off `main` `6e8104a`):
- `packages/contracts/index.ts` - `Execution.activityAt` (optional, documented).
  <!-- codex: Optional is still an exported contract change; AGENTS.md:42 requires asking first for contract changes. Include that approval before implementation rather than treating additive compatibility as an exemption. -->
- `packages/modules/inference/index.ts` - record the attempt's latest activity in the `onActivity` callback; expose it.
- `packages/plugins/api-openai/index.ts` - `streamProgress`, the `tick` decision, `lastEventAt` bookkeeping in the
  event writer.
- `tests/stream-timeouts.test.ts` - new cases under "HTTP: early headers and keepalive" (its `sseBody` and `server`
  helpers script a timed upstream).
  <!-- codex: These helpers are separate harnesses: server registers a scripted mock provider, whereas sseBody exercises the HTTP adapter through a stubbed fetch (tests/stream-timeouts.test.ts:59,90,245). Use provider callbacks for deterministic writer/engine tests and retain an actual chatcompletions adapter path for reasoning-only bytes; one harness alone does not establish both. -->
- `tests/codex-chat-routes.test.ts` - one case with the Codex 0.166 request fixture: a reasoning-only phase gets
  progress events, and the final items and `response.completed` are unchanged.
- `docs/implementation-status.md` (the streaming line) and the API docs that describe SSE comments.
  <!-- codex: Name README.md:341-351 explicitly: it documents timeout semantics and the five-second SSE comments. Clarify that synthesized progress does not count as engine output, first content, or upstream activity. -->

ailab (after the image exists): `kubernetes/apps/apps/llm-router/router.yaml` (digest, source-commit, release
annotations) and a `docs/runbooks/llm-router.md` release section with rollback.

## Verification

1. **Failing tests first** (on `main`, then passing on the branch):
   - Responses stream, upstream streaming `reasoning_content` chunks every 50 ms for 600 ms then text, with
     `streamKeepalive.intervalMs = 30` and `streamProgress.intervalMs = 100`: at least 3 `response.in_progress`
     events between `response.created` and the first `response.output_item.added`; all `sequence_number`s strictly
     increasing; output items and `response.completed` identical to a run without the quiet phase.
     <!-- codex: Compare semantic output after normalizing request/item IDs, timestamps, and sequence numbers: executions receive distinct IDs and injected events necessarily shift the terminal sequence (packages/plugins/api-openai/index.ts:420,458,489). Also test streaming delivery before releasing the upstream gate, since inspecting app.inject's final body alone cannot prove timely heartbeat delivery. -->
   - Upstream silent (no bytes) for 600 ms, then text: zero `response.in_progress`, keepalive comments only.
     <!-- codex: Add upstream comments, fragmented reasoning frames, activity that stops after one burst, and an Execution with no activityAt method; headers alone must not qualify (packages/contracts/index.ts:260; packages/adapters/providers/sse.ts:161). Use multiple simultaneous requests to establish that one active upstream cannot keep another silent request alive. -->
   - Upstream reasoning only after some text has streamed: progress events carry the output so far.
   - Chat Completions with the same reasoning upstream: no new chunks, comments only.
   - Native route: no injected events (existing native passthrough tests stay green).
   - Failover: an attempt that fails after activity, then a silent second attempt: no progress events from the stale
     activity.
     <!-- codex: Include a heartbeat actually sent before the first attempt fails, and a late onActivity callback from that attempt after closeGrace expires and attempt two starts (tests/stream-timeouts.test.ts:140). Verify failover remains allowed and stream.first_content is still recorded only for real content: engine emitted currently becomes true on every yielded InferenceEvent, so progress must remain outside that event stream (packages/modules/inference/index.ts:613,628,696). -->
   - Engine: `activityAt()` follows a provider's `onActivity` calls and resets per attempt.
     <!-- codex: The existing fake() advances setTimeout/clearTimeout but not Date, so it cannot directly validate epoch-based freshness or lastEventAt comparisons (tests/stream-timeouts.test.ts:55). Use a consistent clock, cover same-millisecond activity/event writes, and restore both exported interval objects explicitly because restoreAllMocks does not undo ordinary property assignments. -->
   <!-- codex: Add a Responses HTTP case with continuous reasoning-only activity and a finite maxDurationMs, asserting response.failed/TIMEOUT, upstream closure, released admission, and no later progress. Existing total-cap coverage streams visible text or floods a socket, so it does not exercise a heartbeat-originated drain or an indefinitely awaited reasoning phase (tests/stream-timeouts.test.ts:230,258,277). -->
2. `pnpm verify` and `pnpm check:architecture` in docker (node:24, pnpm 10.32.1, repo mounted read-only, output in a
   container volume).
   <!-- codex: Make the container command reproducible, including frozen-lockfile installation and writable dependency/build/test locations: the build writes relative dist paths (tsconfig.build.json:1; scripts/copy-assets.mjs:2). Include build:lib for the exported contract and test:production for the shipped artifact, and report unavailable PostgreSQL auth integration separately as skipped per AGENTS.md:17. -->
3. **The real CLI end to end** (docker): the branch router started in-process on a port (the
   `createRuntime` + `createHttpServer` setup of `tests/codex-chat-routes.test.ts`), one local account pointing at a
   fake llama.cpp-style server that streams `reasoning_content` for 45 s and then a tool call / text, Codex 0.162.1 and
   0.166.0 with `stream_idle_timeout_ms = 20000`. Expected: `main`'s router fails exactly as production
   ("idle timeout waiting for SSE"); the branch completes in one request with exit 0.
   <!-- codex: A completed tool call normally causes another model request, so distinguish a text-only one-request case from a tool-then-text case with one request per intended turn and zero replayed requests (tests/codex-chat-routes.test.ts:28,33). Pin retry settings and the fake server's per-turn behavior so main's expected failure and the branch's successful request counts are deterministic. -->
4. **Live acceptance after the roll:** on a dev worker, `codex exec -p qwen-27b -c
   model_providers.llm-router.stream_idle_timeout_ms=60000` on a task that makes a turn think longer than 60 s; the
   journal shows that turn's `stream.first_content` later than 60 s and the run completes without a reconnect. Plus
   `validate-live` (baseline 6/9) and the public checks.
   <!-- codex: Cover both affected backends and correlate each acceptance turn with client retry logs and router request IDs; first-content latency alone does not establish that no replay occurred. Record the three expected baseline failures explicitly, and confirm admission is released after both successful completion and a deliberately cancelled long-thinking request. -->

## Release

The standard image flow (llm-router PR → reviewer bots merge → archive in a volume-free pod → Relay v0.2.0 assets →
Relay `deploy-images` dispatch → anonymous pull check → backup + `validate-live` baseline → ailab roll PR). Rollback:
the previous digest `sha256:f3390a7c7e13e9ff79e3ddced468dc173935d79f63e0cfae48c061db770df5be` (source `6e8104a4`,
`router-0.1.0-20261101-reply-shape`). No schema, config or secret change, so the rollback is the digest alone.

<!-- codex: If the release adds the finite route cap recommended below, the “no config change” and digest-only rollback statements become incomplete. Record the configuration revision and an explicit decision to retain or restore each cap on rollback; this is an operational policy change, not a schema migration. -->

## Risks

- **A Responses client that rejects repeated `response.in_progress`.** The event is standard (OpenAI sends it once);
  Codex 0.162.1 and 0.166.0 were measured to accept repeats. Check the OpenAI Node and Python SDK stream helpers
  against the fake server in docker as part of step 3.
  <!-- codex: The [official event reference](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.in_progress) defines the event and response envelope, but does not establish a periodic-heartbeat compatibility guarantee or an exactly-once restriction. Treat repeat handling as a versioned compatibility claim, not a consequence of the event's standard name. -->
  <!-- codex: Make helper verification a release gate using the branch router's actual envelopes, before and after partial text/tool output, through Node finalResponse() and Python get_final_response(); compare against main to distinguish existing subset incompatibilities. The [Python helper's current lifecycle handling](https://github.com/openai/openai-python/blob/main/src/openai/lib/streaming/responses/_responses.py#L370) differs from Node's snapshot replacement, so pin the Python version and test final text, parsed arguments, usage, and failure/incomplete endings rather than just accepting events. -->
  <!-- codex: LiteLLM is missing from the matrix: the deployment pins v1.106.6 (wt-progress-events/kubernetes/apps/apps/ai/litellm.yaml:1199), whose [Responses transformation](https://github.com/BerriAI/litellm/blob/v1.106.6/litellm/llms/openai/responses/transformation.py#L620) recognizes in_progress, but that alone does not verify proxy hooks or Responses-to-Chat conversion. Test the relevant deployed path for duplicate output, filtering, and timeout behavior; if broad compatibility remains unresolved, restrict the initial change to the existing codexCaller branch rather than adding client-specific machinery (packages/plugins/api-openai/index.ts:401). -->
- **Hiding a stuck upstream.** Only upstream bytes produce events, so a server that streams forever without output
  (a degenerate reasoning loop) now runs until the route's idle/total caps or the client's own limit, instead of being
  cut at 300 s by Codex. The route's `maxDurationMs` and the client's turn limits remain the bound; this is the
  intended trade.
  <!-- codex: P1: Idle/wait timers restart on activity, maxDurationMs has no default, and ordinary Responses execution supplies no caller duration cap (packages/modules/inference/index.ts:113,394,415; packages/modules/routing/index.ts:25; packages/plugins/api-openai/index.ts:413). Neither a client turn-count limit nor the router Assistant's MAX_STEPS bounds one outstanding response; the Assistant explicitly has no per-step deadline (packages/plugins/agent-llm/loop.ts:13). -->
  <!-- codex: The checked rollout records show a wait timeout for the local flash-next route and the qwen-27b/flash-next pointers, but do not establish current maxDurationMs values for either resolved route (wt-progress-events/docs/runbooks/llm-router.md:2820-2821). Before rollout, obtain a redacted live readback and set owner-approved finite caps on the resolved local routes if absent, or explicitly accept the lack of a wall-clock bound; deferring this decision while removing the client's effective cutoff is not a bounded release. -->
  <!-- codex: The local chat adapter does have aggregate limits of 16 MiB and 200,000 SSE events, so “forever” is not literally unlimited data, but those are not useful duration guarantees for sparse reasoning or comments (packages/adapters/providers/sse.ts:29,134,163). Document them separately from a time cap and account for the longer occupancy of scarce local-model admission slots. -->

<!-- codex-review-status: complete -->