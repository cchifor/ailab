# llm-router: a real Responses event while the upstream thinks (progress events)

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

1. **The engine exposes upstream activity.** `Execution` gains an optional `activityAt?: () => number | undefined`:
   the epoch ms of the latest raw upstream chunk of the attempt now running (undefined before the first one). The
   engine sets it in the same `onActivity` callback that calls `watchdog.activity()`, and resets it when an attempt
   starts, so a failover's earlier attempt does not count. Additive and optional: other consumers of `Execution`
   (chat-api, the Assistant loop, the vendored copy in cchifor/platform) are unaffected.
2. **api-openai writes progress events.** In the Responses stream of a non-native route only, the existing keepalive
   `tick` decides what a quiet period gets:
   - `response.in_progress` when `response.created` has gone out, no terminal event has started, the stream has had
     no *event* (comments do not count) for `streamProgress.intervalMs` (15 s, exported and settable for tests like
     `streamKeepalive`), and `activityAt()` is later than the last event written;
   - otherwise `: keepalive`, as today.
   The event goes through the same writer as every other event, so `sequence_number` stays strictly increasing. It
   is written synchronously from the timer, never while a write awaits drain (the existing `draining` guard), and
   never after the terminal event (`routerTerminal` / `closed`).
3. **Payload.** `{type: 'response.in_progress', sequence_number, response: responseObject(id, model, a, 'in_progress',
   codexAdapted)}`: the same object `response.created` carries, with the output so far. Before the first output that
   is a few hundred bytes. It stays truthful for a client that replaces its snapshot on this event.
4. **Unchanged:** Chat Completions streams (no standard heartbeat chunk; their clients' read timeouts are satisfied by
   comments), native Codex routes (the upstream's own events pass through), non-streaming requests, the journal and
   metrics (no event per heartbeat), and the engine's own timers.
5. **A silent upstream stays silent.** No upstream bytes since the last event (a queue, a prefill, a cold model load,
   a dead server) means no progress event. The client's idle timer and the router's wait timer keep bounding it, as
   today.

## Out of scope (follow-ups, named so they are not lost)

- Forwarding the reasoning itself (Responses reasoning items, `reasoning_content` on chat). It would make an agent's
  thinking visible, but adds output items that clients store and send back as input, which the subset parser refuses
  for non-Codex callers. Separate design.
- cloud3 cold loads: llama-swap sends no bytes while it loads a model (up to the account's `waitMs` 600 s), so a
  Codex client still gives up at 300 s. That is the dev_worker profile's `stream_idle_timeout_ms` (ailab), not this.
- The 429s on cloud2 (no router queue, 4 slots) and the edge-proxy roll that orphaned upstream legs: separate.
- Bounding how long the model thinks (the router drops `reasoning_effort` for local servers; cloud2's template
  defaults to xhigh): a serving decision for the owner.

## Critical files

llm-router (branch `fix/responses-progress-events` off `main` `6e8104a`):
- `packages/contracts/index.ts` - `Execution.activityAt` (optional, documented).
- `packages/modules/inference/index.ts` - record the attempt's latest activity in the `onActivity` callback; expose it.
- `packages/plugins/api-openai/index.ts` - `streamProgress`, the `tick` decision, `lastEventAt` bookkeeping in the
  event writer.
- `tests/stream-timeouts.test.ts` - new cases under "HTTP: early headers and keepalive" (its `sseBody` and `server`
  helpers script a timed upstream).
- `tests/codex-chat-routes.test.ts` - one case with the Codex 0.166 request fixture: a reasoning-only phase gets
  progress events, and the final items and `response.completed` are unchanged.
- `docs/implementation-status.md` (the streaming line) and the API docs that describe SSE comments.

ailab (after the image exists): `kubernetes/apps/apps/llm-router/router.yaml` (digest, source-commit, release
annotations) and a `docs/runbooks/llm-router.md` release section with rollback.

## Verification

1. **Failing tests first** (on `main`, then passing on the branch):
   - Responses stream, upstream streaming `reasoning_content` chunks every 50 ms for 600 ms then text, with
     `streamKeepalive.intervalMs = 30` and `streamProgress.intervalMs = 100`: at least 3 `response.in_progress`
     events between `response.created` and the first `response.output_item.added`; all `sequence_number`s strictly
     increasing; output items and `response.completed` identical to a run without the quiet phase.
   - Upstream silent (no bytes) for 600 ms, then text: zero `response.in_progress`, keepalive comments only.
   - Upstream reasoning only after some text has streamed: progress events carry the output so far.
   - Chat Completions with the same reasoning upstream: no new chunks, comments only.
   - Native route: no injected events (existing native passthrough tests stay green).
   - Failover: an attempt that fails after activity, then a silent second attempt: no progress events from the stale
     activity.
   - Engine: `activityAt()` follows a provider's `onActivity` calls and resets per attempt.
2. `pnpm verify` and `pnpm check:architecture` in docker (node:24, pnpm 10.32.1, repo mounted read-only, output in a
   container volume).
3. **The real CLI end to end** (docker): the branch router started in-process on a port (the
   `createRuntime` + `createHttpServer` setup of `tests/codex-chat-routes.test.ts`), one local account pointing at a
   fake llama.cpp-style server that streams `reasoning_content` for 45 s and then a tool call / text, Codex 0.162.1 and
   0.166.0 with `stream_idle_timeout_ms = 20000`. Expected: `main`'s router fails exactly as production
   ("idle timeout waiting for SSE"); the branch completes in one request with exit 0.
4. **Live acceptance after the roll:** on a dev worker, `codex exec -p qwen-27b -c
   model_providers.llm-router.stream_idle_timeout_ms=60000` on a task that makes a turn think longer than 60 s; the
   journal shows that turn's `stream.first_content` later than 60 s and the run completes without a reconnect. Plus
   `validate-live` (baseline 6/9) and the public checks.

## Release

The standard image flow (llm-router PR → reviewer bots merge → archive in a volume-free pod → Relay v0.2.0 assets →
Relay `deploy-images` dispatch → anonymous pull check → backup + `validate-live` baseline → ailab roll PR). Rollback:
the previous digest `sha256:f3390a7c7e13e9ff79e3ddced468dc173935d79f63e0cfae48c061db770df5be` (source `6e8104a4`,
`router-0.1.0-20261101-reply-shape`). No schema, config or secret change, so the rollback is the digest alone.

## Risks

- **A Responses client that rejects repeated `response.in_progress`.** The event is standard (OpenAI sends it once);
  Codex 0.162.1 and 0.166.0 were measured to accept repeats. Check the OpenAI Node and Python SDK stream helpers
  against the fake server in docker as part of step 3.
- **Hiding a stuck upstream.** Only upstream bytes produce events, so a server that streams forever without output
  (a degenerate reasoning loop) now runs until the route's idle/total caps or the client's own limit, instead of being
  cut at 300 s by Codex. The route's `maxDurationMs` and the client's turn limits remain the bound; this is the
  intended trade.

<!-- codex-review-status: pending -->
