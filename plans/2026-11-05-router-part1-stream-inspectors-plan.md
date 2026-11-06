<!-- status: proposed (plan, revised after codex rounds 1-3; the §5 fix merged as llm-router #137; the rest implemented on branch feat/router-followups-guard, in review) -->
# llm-router Part 1: a stream-inspector registry and the degeneracy guard, append-only replay for new settings, llama.cpp's reasoning items accepted

**Date:** 2026-11-05 · **Scope:** `cchifor/llm-router` at `origin/main` `247b19f` (#136; the live image
`sha256:78915e24…`). **Approved by the owner on 2026-11-05:** a degeneracy guard, rejection of numeric
`replayReasoning.lastTurns`, and acceptance of `encrypted_content: ""`. **Owner directive (2026-11-05):** "all of these
changes must be properly architected, clearly define the modules, plugins, extensions, etc." **Follows:** the router
standard (`plans/2026-11-04-router-standard-design.md`, R0b and §4.7) and native-first
(`plans/2026-11-04-router-native-first-design.md`, Step 3 d2). **Next:** Part 2
(`plans/2026-11-05-router-part2-native-responses-plan.md`) uses this part's registry and conventions.
**Base moved (2026-11-06):** the implementation is rebased onto `1c7ad32` (#137 to #141; the live image
`sha256:5749d8a8…`, config revision 2244). File and line references below stay those read at `247b19f`.

Evidence labels: **[src]** read in source at `247b19f`; **[measured]** run on 2026-11-05; **[rec]** read from the A/B
recordings on dev-worker-1 (the runs of `plans/2026-11-05-router-transparency-and-planning-revalidation.md`; deleted at
11:21Z after ailab #1607 merged, as that PR said they would be).

---

## 0. Summary

Three changes, one release.

1. **A degeneracy guard** for local accounts. On 2026-11-05 qwen3.8-27b on cloud2 streamed 24,035 reasoning deltas of
   `!` for 764 s with HTTP 200 [rec, trajectory `e23b8d9e`]. Deltas kept flowing, so neither the router's idle timer
   nor Codex's fired. The guard finds a channel whose latest window is **an exact repetition of one unit at least 32
   times**. **By default it only journals** (`stream.degenerate`, action `mark`), on every channel. **Stopping** the
   attempt (the upstream closed at once, a retryable `DEGENERATE_OUTPUT` to the client) is a per-account, per-channel
   setting, `enforce`, and turning it on is **an owner decision** (§3.2): no detector can tell a runaway from a request
   for the same bytes, and even reasoning can quote a repetitive excerpt (codex round 3, finding 1).
2. **`replayReasoning.lastTurns` must be `"all"` in any new or changed setting.** A numeric window changes earlier turns
   of the prompt on every request and defeats the prompt cache of these hybrid models (777,850 uncached tokens against
   about 85k, the standard §4.7). A saved numeric value is tolerated while unchanged, so the roll changes nothing; none
   is live.
3. **`encrypted_content: ""` is treated as absent** in a replayed reasoning item, so llama.cpp's own items replay on the
   translated path. Non-empty ciphertext keeps today's handling on every path (§5). **Merged** as llm-router #137
   (both reviewer bots approved; codex round 3: correct and sufficiently covered).

**Architecture** (§2): the guard is the first entry of a new extension point, the **stream-inspector registry**
(`routerStreamInspectors`), on the repository's registry pattern (`routerStrategies`, `routerProviders`). An inspector
reads protocol-neutral observations of the upstream's output: every text, refusal and tool-argument event the engine
receives, and, through a new `observe` callback on the provider invocation (a sibling of `onActivity`), the reasoning
an account does not forward. It may pass, mark (journal only) or fail the attempt, never change what the client gets.

**Default-off, with two declared exceptions:** the guard and every other new setting do nothing until configured; the
two parser and validator corrections act at the roll (§7.1), and neither changes any live request.

---

## 1. Facts this plan rests on

**The degenerate stream** [rec: `rec/S1-qw-2-qwrouter/0038.sse` + `.meta.json`]:
- the router's Responses stream to Codex: `response.created`, one reasoning item, then 24,035
  `response.reasoning_text.delta` events, each delta exactly `"!"`, the first 4.4 s after the send, cut by the client
  at 764.5 s (the harness's 15-minute cap): about **31.6 single-character deltas per second**;
- upstream: cloud2's vLLM 0.28 chat completions (`qwen3.8-27b`, `system-messages-merged`, `reasoning_effort` ignored),
  replay off at the time. The cause is the model (8/10 degenerate with no reasoning in the turn, 0/10 with any), and
  cloud2 now replays append-only (revision 2229): the guard is for the next loop, not this one.

**Where reasoning is decided** [src]: the chat adapter reads `reasoning_content` (else `reasoning`) and yields a
`reasoning` event only when the account sets `forwardReasoning` (`adapters/providers/chatcompletions.ts:171-173, 258,
302`). The engine counts a yielded event as output: `emitted` becomes true (`modules/inference/index.ts:779`), so the
idle timer replaces the wait timer (`:747`) and the attempt is no longer failed over (`:852`). Only the engine calls
`provider.open()` (`:733`). Other engine consumers: the Assistant (`plugins/agent-llm/loop.ts:207`), the admin bridge
(drops `reasoning` itself, `plugins/admin-bridge/index.ts:123`), `test_route` (`plugins/router-engine/tools.ts:29`).

**Tool-call deltas** [src]: a streamed call's first delta carries its `id`, `index` and `name`, continuations only the
`index` (`chatcompletions.ts:217-221`; `tests/reasoning-forwarding.test.ts:71` has such a sequence). A non-streaming
reply's calls carry no `index`, and the adapter defaults it to 0 for every call (`:308-311`): the engine and the front
door's accumulator then see several calls of one reply as one (an existing defect, fixed in §3.1).

**What clients see on a mid-stream failure** [src]:

| Path | Behaviour |
|---|---|
| translated Responses | `response.failed` with `error.code` = the fault's code (`api-openai/index.ts:556`); the HTTP head (200) is sent when the attempt is selected, before any output (`:446-453`) |
| Codex 0.166 on that event | an unknown code → `ApiError::Retryable` (`codex-api/src/sse/responses_error.rs:101`) → `CodexErr::Stream` (`api_bridge.rs:58`) → retried within `stream_max_retries` (`core/src/responses_retry.rs:89`); confirmed by codex's review |
| chat streaming | the error frame, then `[DONE]` (`api-openai:546`); clients decide whether to retry (the repository's own SDK does not) |
| failure before anything was emitted | the engine fails over if the fault is retryable, attempts remain, the route's affinity allows it and the request is not cancelled (`inference:852`) |
| non-streaming | the failure propagates before the response is sent: a retryable 5xx carries `x-should-retry: true` (`kernel/http.ts:482`) |

**Replayed reasoning items, as each server emits them** [rec, `response.output_item.done`; and the Part 2 probes]:

| Source | Item |
|---|---|
| the router (translated) | `{type, id: 'rs_<id>', summary: [], content: [reasoning_text]}` |
| vLLM 0.28 (cloud2) | `{id: '<hex>', summary: [], type, content: [reasoning_text], encrypted_content: null, status: 'completed'}` |
| llama.cpp b12020 | `{type, id: 'rs_<id>', summary: [], content: [reasoning_text], encrypted_content: ''}` |

`replayedReasoning` (`modules/protocol/index.ts:58-66`) accepts the first two and not the third.

**Replay settings and config paths** [src]: `runtime/config.ts:36` accepts `{lastTurns: 1..1000}`;
`modules/compat/index.ts:216-226` honours it. The live configuration (revision 2229) has only `{lastTurns: "all"}`
(cloud2, cloud3) or nothing. A seed file is validated in the entry point before the runtime starts
(`apps/router/main.ts:40`); a saved configuration is loaded **without** validation (`plugins/config-store/index.ts:142`);
every write is structurally validated, trusted writes skip only the plugin validators and checks (`:92`); followers and
`revert` go through the same commit (`:79`, `:128`).

---

## 2. Architecture

### 2.1 Conventions shared by the program's extension points

One set of principles, three kinds of extension point (codex round 2, finding 5):

| Kind | What it holds | Examples | Lifetime and lookup |
|---|---|---|---|
| **Implementation registry** | code that plugins contribute and can withdraw | stream inspectors (Part 1), availability probes (the cloud1 design), providers and strategies (today) | `register(impl) → dispose` inside `ctx.effect`; duplicate id → 409 `<POINT>_CONFLICT`; ordered by id; disposal affects new uses only; `available(id)`; a `validator()` for the config slice that names it |
| **Manifest registry** | versioned data the router decides on | qualification manifests (Part 2) | entries registered by a plugin from data versioned with the golden suite; looked up by key; never mutated at runtime |
| **Static catalog** | names the code owns and cannot be extended | request mutations (Part 2), journal events (§2.6), skip reasons | a constant table in `contracts` or a pure module; extensions get their own name space (`extension:<name>.…`) where they may add entries |

Shared by all three:
- **contracts** (`packages/contracts/<point>.ts`, re-exported by `contracts/index.ts`) hold every shape, setting shapes
  included, so `Account` never imports a module;
- **pure modules** (`packages/modules/<point>/`: relative imports only, time and I/O injected) own defaults,
  resolution and validation, returning structured issues (a path and a sentence);
- **a config slice** per point, absent = off. For **implementation registries**: an implementation's settings are
  parsed by its own parser through the registry's `validator()`; a new or changed entry must name an active
  implementation; an unchanged one is tolerated while its plugin is off, and that state is **reported** (a runtime event
  and the admin view), never silent. Manifest registries and static catalogs have no per-implementation settings;
- **observability:** persisted journal events named in `contracts/journal.ts`; metrics `llm_router_<point>_…`;
- **tests:** a unit suite per module, a **contract kit** per extension point that every implementation runs, an
  end-to-end path through HTTP, and the real Codex CLI in docker where Codex is the client.

### 2.2 Module map (Part 1)

| Unit | Kind | Single responsibility | Public interface | Depends on |
|---|---|---|---|---|
| `contracts/stream.ts` (new) | contracts | the inspector contract and observations | `StreamInspector<S>`, `InspectorInstance`, `InspectorScope`, `Observation`, `InspectorDecision`, `InspectorMarker`; `BoundInvocation.observe?` | contracts |
| `contracts/degeneracy.ts` (new) | contracts | the guard's setting shape | `DegeneracySetting` (§2.5) | none |
| `contracts/journal.ts` (new, PR 1) | contracts (static catalog) | the persisted journal's event names, by owner | `JOURNAL_EVENTS`, `JournalEventType` (§2.6) | none |
| `modules/degeneracy/` (new) | pure | detect an exact repetition loop per channel; resolve and validate the setting | `LoopDetector`, `ChannelMonitor`, `resolveDegeneracy`, `degeneracyIssues`, `degenerateOutput` | contracts |
| `modules/inference/` (changed) | pure | the engine: builds each attempt's observations, runs its inspectors, acts on their decisions | option `inspectors?: { attach(config, account, scope): InspectorInstance[]; yieldNow(): Promise<void> }` (§2.3: the request's configuration snapshot and its account) | contracts |
| `modules/compat/` (changed) | pure | the replay setting rule (new or changed settings append-only) | `replaySettingIssue(next, previous)` | contracts |
| `modules/protocol/` (changed) | pure | `replayedReasoning` accepts `encrypted_content: ''` | unchanged exports | contracts |
| `runtime/stream-inspector-registry.ts` (new) | runtime (implementation registry) | holds inspectors; attaches them per attempt in id order; validates their settings | `StreamInspectorRegistry`: `register`, `list`, `attach(config, account, scope)`, `validator()` | contracts |
| `runtime/config.ts` (changed) | runtime | the structural schema; the replay rule with `previous` | `validateConfig(value, validators, previous)` (unchanged signature) | `modules/compat`, zod |
| `plugins/stream-inspectors/` (new, **required**) | registry plugin | provides `routerStreamInspectors`; registers its validator; reports configured-but-inactive inspectors | service | runtime registry |
| `plugins/degeneracy-guard/` (new, enabled in the catalog, inert until an account opts in) | implementation | registers the `degeneracy-guard` inspector, its zod parser and its support rule | none | `modules/degeneracy`, `routerStreamInspectors` |
| `plugins/router-engine/` (changed) | plugin | passes `routerStreamInspectors.attach` to the engine | `inject` gains `routerStreamInspectors` | the service |
| `plugins/config-store/` (changed) | plugin | trusted writes validate against `previous` too | unchanged | `runtime/config` |
| `plugins/metrics/` (changed) | plugin | one counter from the marker | `llm_router_stream_inspector_marks_total{inspector,account,channel,action}` | `routerRecorder.tap` |
| `adapters/providers/chatcompletions.ts` (changed) | adapter | calls `invocation.observe` with reasoning it does not forward; numbers a non-streaming reply's calls by position | the events it yields are unchanged (except the index fix) | contracts |

**Dependency direction** (no cycles, no module importing runtime, adapters or plugins; the core never imports a plugin):

```
contracts ◄── modules/degeneracy, modules/compat, modules/protocol, modules/inference
modules/* ◄── runtime/config, runtime/stream-inspector-registry
runtime/* ◄── plugins/stream-inspectors, plugins/degeneracy-guard, plugins/config-store, plugins/router-engine
service routerStreamInspectors: plugins/stream-inspectors ──► plugins/degeneracy-guard (registers)
                                                          ──► plugins/router-engine (attaches per attempt)
```

`modules/inference` receives `attach` through its options, as it receives `select` from the strategy registry. A
standalone engine built without it runs no inspector, and nothing it emits changes: reasoning visibility stays the
adapter's decision (codex round 1, finding 1).

### 2.3 Extension point: stream inspectors

```ts
// contracts/stream.ts
/** One piece of upstream output, protocol-neutral: what an inspector reads. Characters are UTF-16 code units. */
export interface Observation {
  readonly channel: 'reasoning' | 'text' | 'refusal' | 'tool';
  readonly text: string;
  /** Tool calls only: the call's position in the reply (its index), the stable key of its channel. */
  readonly call?: number;
  /** Whether the client receives this text (a forwarded event) or not (reasoning the account does not forward). */
  readonly delivered: boolean;
}
/** What an inspector may know about its attempt: primitives only, so it cannot reach or change routing state. */
export interface InspectorScope { readonly accountId: string; readonly provider: string; readonly model: string; readonly attemptId: string; readonly stream: boolean;
  /** The native protocol the attempt forwards as sent (`codex-responses`), when it is one: per-attempt channel availability. */
  readonly native?: string }
export interface StreamInspector<S> {
  readonly id: string;                                              // 'degeneracy-guard'; extensions 'extension:<name>'
  /** The account slice's parser (zod in the plugin): its output is what attach receives. */
  readonly settings: { parse(value: unknown): S };
  /** Whether this inspector can read this account's streams; the registry's validator refuses the key with the reason. */
  supports(account: Readonly<{ provider: string; access: string }>): true | { reason: string };
  /** undefined: not for this attempt. Called only for an account whose slice names this inspector. */
  attach(scope: InspectorScope, settings: S): InspectorInstance | undefined;
}
export interface InspectorInstance { readonly inspectorId: string; observe(o: Observation): InspectorDecision }
export type InspectorDecision = { action: 'pass' } | { action: 'mark'; marker: InspectorMarker } | { action: 'fail'; fault: RouterError; marker: InspectorMarker };
/** The journal event an inspector asks for: names, numbers and booleans only, never content. */
export interface InspectorMarker { type: JournalEventType; data: Readonly<Record<string, string | number | boolean>> }

// contracts/index.ts, BoundInvocation (beside onActivity):
/** The adapter reports upstream output it does not yield (reasoning an account does not forward). Never output.
 *  When it answers a promise, the adapter waits for it before it reads on. (Part 2 §3.4 declares an extension of this.) */
observe?: (o: Observation) => void | Promise<void>;
```

- **The registry** erases `S` only internally. `attach(config, account, scope)` receives **the configuration snapshot
  the request was routed with** (the engine's clone taken when execution began, and the account from it), and caches
  each parsed setting per snapshot object, so a configuration change during an admission wait never applies newer
  settings to an older request (codex round 3, finding 6); `attach` always receives its own parser's output. A setting
  that no longer parses (an unchanged account after a plugin upgrade) attaches nothing and is reported, never fails the
  attempt. Instances are attached in
  **inspector id order** (stable across disable and re-enable); each attempt keeps the instances it attached, so a
  plugin's disposal affects new attempts only.
- **The account slice** is `Account.streamInspectors?: Record<string, unknown>`. The validator parses each entry with
  its inspector's parser (422 with the path, e.g. `accounts.3.streamInspectors.degeneracy-guard.reasoningChars`),
  checks `supports`, refuses an id no active plugin registered on a new or changed account, and tolerates an unchanged
  one while the plugin is off.
- **A configured inspector whose plugin is off** attaches nothing; the registry reports it once per account and change
  (runtime event `stream_inspector.unavailable {inspector, accountId}`). Marking it inactive in `GET /admin/v1/accounts`
  is not in this part: the runtime event is the signal for now.
- **A malformed decision or a throwing inspector** fails the attempt with `STREAM_INSPECTOR_FAILED` (500, not
  retryable): a guard that crashed is not a guard that passed.
- **What the engine does:** for every upstream `text`, `refusal`, `tool` and `reasoning` event, before the output
  limiter, it builds the `Observation` (`delivered: true`) and gives it to each instance; for each `observe` callback,
  the same with `delivered: false`. `finish`, `usage`, `image` and `native` events are not observed (Part 2's native
  tap observes inside its frames). Then:
  - `pass`: nothing;
  - `mark`: the marker is journaled; the stream continues. A marker from `observe` is written at once, so a runaway
    shows in the journal while it runs, and it is not waited for inside the read (the journal's time is not the
    upstream's silence). One shared writer serves every caller, and every terminal path waits for what is queued or in
    flight, so nothing overtakes a marker. A marker is a journal event like any other: a journal that rejects one
    fails the request, whenever that happens and whatever else failed, is not retried, and the markers behind it are
    still tried;
  - `fail`: the engine **latches** the fault in the attempt's watchdog (`Watchdog.fail`, beside `trip`): it aborts the
    attempt **and wakes a pending read**, exactly as a timeout does (`inference:155`); a fault latched before a read is
    armed settles that read at once. The engine checks the latch right after every read settles, before handling the
    event, `done` or `finish`, then journals the marker, then fails the attempt; the existing bounded close
    (`:785-803`) runs; the fault (never `CANCELLED`) is what `attempt.failed` and the client see. A `fail` inside the
    `observe` callback takes the same path (the callback never throws into the adapter), and callbacks after the
    attempt's read has ended are ignored (codex round 3, finding 2: aborting the controller alone left a never-settling
    read pending).
  - **Large observations** (over 64 KiB, e.g. a non-streaming reply) are inspected in 64 KiB pieces with a yield to the
    event loop between them (`yieldNow`, required with `inspectors`), so one event cannot hold the loop for long. Nothing
    is queued: an `observe` callback with such a text answers a promise the adapter waits for, inside the read, so it
    needs no result of that read, order holds, and only that one observation is held. The silence timer stands still
    meanwhile (`Watchdog.hold`); the request's `maxDurationMs` does not. After every await the pieces stop, and nothing
    is forwarded, once the attempt was cancelled, the consumer left, or it failed or timed out.
- **Nothing an inspector does changes what is emitted, counted, timed or failed over,** except a `fail`, which ends the
  attempt like any other failure.

### 2.4 Why a callback for what the client does not receive

The first draft moved `forwardReasoning` into an engine filter, which made the account's disclosure policy depend on an
optional engine option (round 1, finding 1). The second draft added an `observe` event type, which changed every
adapter test that compares event lists, and is an event the engine must remember never to emit (round 2, finding 7).
The callback has neither cost: it is an optional field of `BoundInvocation`, exactly like `onActivity`; an adapter that
never calls it is unchanged; the events every adapter yields are unchanged, so `tests/reasoning-forwarding.test.ts`
passes as it is; and unforwarded reasoning cannot reach a client, because it is not an event.

### 2.5 Config

| Key | Shape (contracts) | Owner of rules and defaults | Default |
|---|---|---|---|
| `Account.streamInspectors['degeneracy-guard']` | `DegeneracySetting`: `true` or `{ reasoning?: 'enforce' \| 'observe', output?: 'observe' \| 'enforce', reasoningChars?: 512-65536, outputChars?: 512-65536 }` | `modules/degeneracy` (resolution, bounds, sentences); the plugin's zod parser; its `supports` rule (§3.4) | absent = off; `true` = `{ reasoning: 'observe', output: 'observe', reasoningChars: 2048, outputChars: 8192 }` (observe-only everywhere; `enforce` is the owner's choice per channel class) |
| `Account.replayReasoning` | `boolean \| { lastTurns: 'all' } \| { /** legacy, tolerated unchanged */ lastTurns: number }` | `modules/compat` (`replaySettingIssues`), applied in `validateConfig` with `previous` | absent = off (unchanged) |

**The replay rule** (codex round 1, findings 9-10; round 2 accepted): a numeric `lastTurns` is refused on a **new or
changed** setting with: *"replayReasoning.lastTurns must be "all": a numeric window replays only the last N turns'
reasoning, so every request changes an earlier part of the prompt and the server's prompt cache cannot be reused (on
2026-11-04, 777,850 uncached tokens against about 85k). Use true or { lastTurns: "all" } (append-only), or false
(off)."*, path `accounts.<i>.replayReasoning.lastTurns`. "Unchanged" compares the **replay setting** of the account with
the same id in `previous`, not the whole account. A tolerated value is honoured as today (the numeric branch of
`adaptRequest` stays, marked legacy), so no saved configuration stops loading or writing and the roll changes no
request. No startup migration (either direction would change behaviour). Consequences, each tested: a seed file with a
number is refused at boot; `revert` to a revision holding one is refused (the runbook's "do NOT revert to revision
2227", enforced); trusted writes pass `previous` (`config-store`), so a tolerated value never blocks an account-health
write; followers and the Assistant's undo use the same commit. `update_account` exposes neither replay nor the guard:
both are configuration-API settings (`If-Match` PUT), as replay is today.

### 2.6 Observability: what is named where

`contracts/journal.ts` (in PR 1, because `InspectorMarker` uses it) names the **persisted request-journal events**,
grouped by owner. Typing the `Recorder` port's `event()` with it, and converting today's callers, is its own later PR
(§7.2). Today's names [src] and owners:

| Owner | Persisted events |
|---|---|
| `modules/trajectory` (the recorder) | `request.received`; the terminal `request.complete` / `request.failed` / `request.deferred` / `request.cancelled` (mapped through a typed table from `'request.' + status`) |
| `modules/inference` (the engine) | `request.authorized`, `request.rejected`, `routing.evaluated`, `model.fallback`, `affinity.rebound`, `admission.queued`, `admission.dequeued`, `admission.queue_full`, `admission.reserved`, `admission.released`, `attempt.started`, `attempt.completed`, `attempt.failed`, `attempt.close_timeout`, `request.parameters_adjusted`, `stream.first_content`, `output.limit_enforced`, `conversation.rekeyed`, `conversation.rekey_skipped`, `retry.scheduled`, `client.closed`, `upstream.closed`, `placement.persistence_failed`, `account.state_persistence_failed` |
| stream inspectors (new) | `stream.degenerate` |

Kept apart, as different stores: the recorder's notification `recording.failed` (`modules/trajectory/index.ts:53`);
telemetry log lines (`request.summary`, `plugins/telemetry/otel.ts:40`; runtime events such as
`stream_inspector.unavailable`); audit actions (`placement.deleted`, `account.*`, `config.*`). They are listed in
separate sections of the same file, for reference. Extensions name their events `extension:<name>.<event>`.

**Metric:** `llm_router_stream_inspector_marks_total{inspector, account, channel, action}` (`action`: `mark` or `fail`)
from `stream.degenerate` through `routerRecorder.tap`.

---

## 3. The degeneracy guard

### 3.1 Detection

**Channels:** `reasoning` (forwarded `reasoning` events and unforwarded `observe` callbacks), `text`, `refusal`, and one
channel **per tool call, keyed by its index** (the stable identity across a streamed call's deltas; a non-streaming
reply's calls are numbered by position by the adapter, the existing defect of §1 fixed in the same PR). At most 32 tool
calls of one reply are inspected; later calls are **not inspected** (a declared limit; `attempt.completed` already
counts every call, so a reply past it is visible), and calls are never merged into a shared detector (round 2,
finding 2).

**The rule:** a channel is a loop at a checkpoint when its last W characters are **exactly periodic** with a period
p ≤ W/32: one unit repeated at least 32 times, verbatim. W is `reasoningChars` (default 2,048) for reasoning and
`outputChars` (default 8,192) for text, refusal and tool arguments.

**The computation** (`modules/degeneracy/detector.ts`, prototyped and measured):
- **Checkpoints** at absolute channel positions that are multiples of K = max(128, W/64), once the channel holds W
  characters. Decisions depend on positions only, never on how the text was split into deltas.
- **Probe:** the smallest period q of the window's last 2·(W/32) characters (prefix function). A window with a period
  p ≤ W/32 has such a suffix with q ≤ p, so q > W/32 rejects the window.
- **Verify:** whether the whole window has period q (one pass). By the Fine-Wilf theorem the suffix (length ≥ p + q)
  has period gcd(p, q), so q divides p and the window has period q itself: a failed verification means no period
  ≤ W/32 exists. No other check is needed, and W need not be a multiple of the period.
- **Guarantee:** an exact loop of period ≤ W/32 lasting at least W + K − 1 characters is found, however the deltas are
  split.
- **Storage, bounded by construction** (codex round 3, finding 3): each channel holds a fixed ring of W + K UTF-16 code
  units (`Uint16Array`) and a fixed scratch of 2·W/32 integers; no string of the input is kept, so a large delta is
  never retained through a slice; after a verdict both are released and later input is ignored. At the defaults one
  attempt's guard holds at most 2,048 + 34 × 8,320 code units (about 0.57 MB) plus the scratch.
- **Cost:** per checkpoint the probe makes at most about 4·W/32 comparisons (the prefix function) and a passing probe
  adds at most W; with K = max(128, floor(W/64)) that is a few comparisons per character received, plus the
  verification when probes pass. **Measured** (node 24.22.2, docker, 1 MiB inputs, no verdict): random binary 85-111 ms
  per MiB; the adversarial near-periodic construction `a^(W−1) b` repeated (most probes pass, verification runs, no
  window qualifies) 281-299 ms per MiB, at W = 8,192 and 65,536: about 0.3 µs per character at worst. A streaming
  channel at the recorded 31.6 characters per second costs nothing measurable; a non-streaming 16 MiB adversarial reply
  would cost about 5 s, spread over 64 KiB pieces (§2.3), so the event loop is held at most about 20 ms at a time.

**Deliberate false negatives** (documented): a unit longer than W/32 (a reasoning loop with a 65-character unit is
never caught at W = 2,048; widen `reasoningChars` to catch longer units); a loop that ends within W + K characters;
approximate (non-verbatim) repetition; a loop split across channels.

**Latency at the recorded rate:** the `!` loop is caught at character 2,048 of reasoning (65 s), against 764 s and still
running.

### 3.2 Calibration, and the policy for legitimate repetition

**The corpora** [measured]:
- **the A/B stream recordings** (dev-worker-1, before deletion; 28 runs, both arms, qwen3.8-27b and flash-next): 3,229
  channels, 3.57M characters (reasoning 2.61M, tool arguments 0.84M, visible text 0.12M). The fewest distinct 8-grams
  in any 2,048-character reasoning window: 788; the longest run of one repeated character anywhere: 34;
- **the built detector over the surviving Codex rollouts** of the same runs (43 sessions; aggregates only): 4,729
  channels, 6.41M characters (reasoning 4.95M, tool arguments 1.30M, text 0.16M), 307 reasoning channels long enough to
  fill a window: **0 detections**.

**Synthetic legitimate repetition** [measured, unit tests past W]: tables with real content, box-drawing tables, 150
similar `case N:` lines, assert lines, hex dumps with offsets, random binary and random hex never fire (no short period).
What fires is exact verbatim repetition ≥ 32 times over at least W characters: a wall of identical dash lines, the same
blank table row, a zero-filled literal past 8 KB; the `!` loop, `show show`, a repeated sentence.

**The policy** (codex rounds 2 and 3, finding 1, blocker): the owner asked for no false positives on legitimate
repetition such as code, tables or long dashes. No detector can guarantee that: a request may ask for verbatim
repetition, and **reasoning can quote it too** (one 2,048-character repetitive excerpt, such as a wall of dashes, is
enough at the default reasoning window). So:
- **every channel is observe-only by default:** a detection is journaled (`action: mark`, once per channel per
  attempt), the stream continues, nothing the client gets changes;
- **`enforce` is per account and per channel class** (`reasoning`, `output`), and turning it on is **an owner
  decision** that accepts the residual class (exact repetition ≥ 32 times over at least W characters of that channel).
  The recommendation is `reasoning: 'enforce'` after the observe period: the measured corpora hold no reasoning window
  that is even close (fewest distinct 8-grams in 2,048 characters: 788, where a loop needs a period ≤ 64), and a
  reasoning loop is what cost 764 s on 2026-11-05; `output` stays observe-only;
- **classifying marks:** a `stream.degenerate` mark carries counts and the period only, and the baseline journal keeps no
  content, so a mark is classified by asking the client's owner (the dev workers keep their Codex rollouts) or by a
  time-boxed content capture of that account (the existing `capture` setting); legitimate cases found become fixtures
  of the unit tests (a case that must not fire), before enforcement;
- **the second real stream (2026-11-06, cloud3, replay on):** 268 s and 2.47 MB with no `stream.first_content`, usage
  `{}`, and no router timeout before the client gave up (the route's `timeoutMs` is 300 s), with vLLM's MTP acceptance
  at 1.00 throughout. So the guard reads reasoning deltas before any content and does not depend on
  `stream.first_content` (a test has this shape). **Only `enforce` frees the admission slot:** under `observe` a runaway
  keeps it until the stream ends, times out or the client goes. A loop unit longer than W/32 (64 characters at the
  default window), or a repetition that is not exact, is not seen: the captured stream, when it comes, is a fixture and
  decides whether the default window holds.

### 3.3 What happens on detection

- **Enforced channel** (only where the owner set `enforce`): `fail` with `RouterError('DEGENERATE_OUTPUT', "The model's <channel> became a repetition loop
  (a <p>-character unit repeated over <W> characters); the router stopped it. Retrying usually works.", 502,
  retryable: true)` and the marker `stream.degenerate {inspector: 'degeneracy-guard', action: 'fail', channel, chars,
  windowChars, period}`; the engine aborts first, then journals, then fails (§2.3).
- **Observe-only channel** (the default): the same marker with `action: 'mark'`, once per channel per attempt; the
  stream continues.
- **What the client sees** follows §1's table: Codex retries the turn; a chat stream ends with the error frame; before
  anything was emitted (an account that does not forward reasoning, looping in its reasoning) the engine fails over when
  the route allows it; streaming clients already have the 200 head, so an exhausted failover is a 200 stream ending with
  the failure event, not an HTTP 502; non-streaming requests get a 502 with `x-should-retry: true` (no time saved: the
  whole reply was already read).

### 3.4 Scope by transport and channel (codex round 2, finding 3)

| Provider / transport | Channels the router can observe | Guard |
|---|---|---|
| `local`, `openai`, `deepseek` (the chat-completions adapter) | reasoning (forwarded, or through `observe`), text, refusal, tool arguments | **supported**; planned on cloud2 and cloud3 |
| `local`, native wires (Part 2) | through the native tap | supported once Part 2's tap reports observations |
| `codex` translated attempts, `anthropic`, `claude` (Claude Code), `claude-seat` | text, refusal, tool arguments (their reasoning is encrypted, or not exposed: `anthropic.ts:254` drops thinking) | **supported on those channels, opt-in, not planned**: no remote degeneracy has been observed, and every remote change is A/B-gated (owner: Codex and Claude quality must not be negatively affected) |
| `codex` native attempts (the same account as its translated attempts) | none (raw frames) | per attempt: the scope's `native` field; the guard declines to attach (codex round 3, finding 4) |

The owner's brief for this item scoped it to local accounts; the contract does not, so a remote account can opt in on
the channels that exist, and a remote enforcement is a separate decision with its own gate.

**Production steps, each on the owner's go after the release gate:** `degeneracy-guard: true` (observe everywhere) on
cloud2 `lan-192.168.0.28-18020` and cloud3 `lan-192.168.0.26-8080` for 48 hours, every mark classified (§3.2); then,
if the owner accepts the residual class, `{ reasoning: 'enforce' }`.

---

## 4. `replayReasoning`: append-only for new settings

As specified in §2.5. `adaptRequest`'s numeric branch stays, documented as legacy, and its tests stay. The README's
two `lastTurns` paragraphs and the ailab runbook's "The config still ACCEPTS a numeric `lastTurns` …" warning are
rewritten in the release's roll PR.

## 5. `encrypted_content: ""` is absent (implemented)

Three paths, three policies (codex round 1, finding 11; round 2: resolved):

| Path | Before | After |
|---|---|---|
| strict translated parsing (`parseRequest`, any non-Codex caller) | `""` refused as unsupported reasoning; ciphertext refused | `""` accepted as plaintext; **ciphertext still refused** |
| Codex adaptation (`adaptCodexResponses`, the Codex CLI on a translated route) | `""` and ciphertext items **dropped**, journaled `input:reasoning` | `""` items kept and replayed when the account replays; **ciphertext still dropped and journaled** |
| native forwarding (codex-native routes; Part 2's wires) | untouched | untouched |

Implemented on `fix/replay-empty-encrypted-content` with `tests/reasoning-items.test.ts` and
`tests/fixtures/reasoning-items.ts` (the three servers' shapes, ids and text replaced): each shape through both parsers
over a four-turn tool loop, ciphertext and a blank `" "` still refused, and the HTTP continuation (replay `"all"`: every
turn's `reasoning_content`, `input:reasoning_replayed:4`; replay off: dropped, `input:reasoning`, never refused). The
three llama.cpp cases failed before the change and pass after; the full suite passes except three
`claude-stream.test.ts` process-group tests that fail identically on unmodified `origin/main` in this docker environment.

---

## 6. Tests, by boundary

All in docker (`node:24`, pnpm 10.32.1), `pnpm verify` plus `check:architecture`.

**The architecture gate** (rounds 2-3, finding 9/7): `check:architecture` already fails on the baseline, one line for
`modules/inference/index.ts` (`Date.now()`, issue #79), and the checker prints one line per file. A test pins the
exemption narrowly: that file may keep at most **the 11 known `Date.now()` occurrences** (on 10 lines), and with them
removed it may hold no `process.env`, `fetch(`, `new Worker(` or `new Context(`; every other module file stays under
the checker's full rule. Fixing #79 is its own change.

| Boundary | What it pins |
|---|---|
| unit: `modules/degeneracy` | the recorded shape (24,035 one-character `!` deltas) fires at character 2,048 of reasoning; `show`, sentence and longer-unit loops at their windows; every synthetic case of §3.2 past W (exact repetition fires, nothing else does); random binary (xorshift) and hex past W never fire; the near-periodic construction never fires; chunk-partition invariance (one delta, single characters, random splits, split surrogate pairs); a loop followed by a diverse suffix inside one delta; the W − 1 and W + K − 1 boundaries; per-call channels keyed by index, a looping call beside a healthy parallel one, the 32-call bound (later calls not inspected); the measured budgets (both worst inputs at W = 8,192 and 65,536); structural storage bounds (only the ring and scratch, released after a verdict); setting resolution and every bound's sentence and path |
| contract kit: every `StreamInspector` | `attach` is never called without the account's slice; the scope is primitives only (frozen); markers carry only names, numbers and booleans; a thrown error or a malformed decision fails the attempt with `STREAM_INSPECTOR_FAILED`; disposal affects only new attempts; parser output reaches `attach`. Run for `degeneracy-guard` and a test inspector |
| unit: registry | duplicate id 409; id order across disable and re-enable; the validator (parse errors with paths, `supports` refusals, unknown id refused on new accounts, tolerated unchanged, the `unavailable` report) |
| engine | an inspector `fail` aborts the attempt before the marker is written (a slow recorder); the fault code survives; a `fail` raised inside the `observe` callback **of an adapter whose read never settles** (the latch wakes it); `mark` changes nothing the client gets; an `observe` callback never yields, counts or moves `emitted`, the timers or the failover boundary; failover before output; large observations in 64 KiB pieces; the scope's `native` field; the configuration snapshot bound at attach; the Assistant, admin bridge and `test_route` paths with an inspector attached and without |
| adapter | `observe` is called exactly for unforwarded reasoning; `tests/reasoning-forwarding.test.ts` unchanged and passing; a non-streaming reply with three calls yields indices 0, 1, 2 (the defect fix), and the front door returns three calls |
| replay | numeric refused on new and changed settings with the sentence and path; tolerated unchanged (compared per setting) and honoured; seed refused; revert refused; a trusted health write with a tolerated value succeeds; a follower revision |
| end to end (HTTP, in-process router, fake vLLM) | the `!` reasoning stream: the upstream connection closed by the router, `/v1/responses` ends with `response.failed` `DEGENERATE_OUTPUT`, chat with the error frame, non-streaming 502 with `x-should-retry: true`; the journal holds `stream.degenerate` then `attempt.failed`; the metric counts one; a looping text stream in observe mode streams to the end with one mark; the plugin disabled: as today, with the `unavailable` report |
| real client (docker) | Codex 0.166 → in-process router → fake upstream: turn 1 loops in its reasoning and is cut, Codex retries, the retry (normal) completes: exit 0, two upstream requests; and with every retry looping: Codex exits non-zero after its budget, each attempt cut |
| corpus replay (dev-worker-1, aggregates only) | done for the prototype (§3.2: 0 detections over 6.41M characters); repeated with the final build before release |

---

## 7. Delivery

### 7.1 Default-off, and the declared exceptions

Every new setting is off until configured. Three changes act at the roll; each is a correction, and none changes a
live request:
- `encrypted_content: ""` accepted: no production client sends it (Codex on the router replays the router's own items or
  vLLM's `null`; flash-next's llama.cpp is retired);
- numeric `lastTurns` refused on new or changed settings: none is live, and saved values are tolerated;
- a non-streaming local reply with several tool calls now returns all of them (today they merge into one).

### 7.2 PRs (llm-router; reviewer bots; a codex implementation review on each)

1. **`encrypted_content: ""`**: llm-router #137 (merged after both reviewer bots approved).
2. **One PR for the rest** (the coordinator's instruction of 2026-11-06, replacing three):
   - **the seam:** `contracts/stream.ts`, `contracts/journal.ts` (the catalog, types only), `BoundInvocation.observe`,
     the registry and its plugin, the engine's inspector step, the chat adapter's `observe` calls and the non-streaming
     index fix, the contract kit, the architecture baseline test;
   - **the guard:** `modules/degeneracy`, `contracts/degeneracy.ts`, `plugins/degeneracy-guard`, the metric, the
     end-to-end tests;
   - **replay:** the rule with `previous`, trusted writes with `previous`, the tests of §2.5.
3. **Before the release:** the real Codex CLI test of the guard and a rerun of the rollout corpus on the final build.
4. **The `Recorder` port typed** with the catalog and today's callers converted: independent, may follow the release.

**One release** after 1-3, through the runbook's release flow up to the ailab roll PR, then a stop for the
validation agent's gate (Codex, Claude and qwen: Layer (a) diff plus short Layer (b), §9.6 margins). The gate expects no
difference on any provider. **After the gate, on the owner's go:** §3.4's production steps.

**Rollback:** the key removed from the accounts (config), then the image reverted to the one live before the roll
(`sha256:5749d8a8…` on 2026-11-06). The old image
strips the unknown key only at its next validated write, so remove it first (the runbook's rule for new account keys).

## 8. Not in scope

Transforming or shaping events (the output limiter stays); turning `enforce` on (an owner decision, per channel);
remote use (opt-in, unplanned); approximate loops; Part 2's native observation, wires, manifests, mutations and
availability probes (Part 2 and the cloud1 design).

## 9. Codex cross-validation

Run on the workstation: `codex exec --profile review` (gpt-6-astra, xhigh, read-only), on the plan and `247b19f`.

**Round 1** (first draft): **rework**, 2 blockers, 11 important, 1 minor.

| # | Finding | Disposition |
|---|---|---|
| 1 | Blocker: visibility made to depend on an optional engine filter | The decision stays in the adapter (§2.4); round 2: resolved |
| 2 | Filter types allow wrong-phase decisions, mutation, no identity | One phase, discriminated decisions, `inspectorId`, runtime validation; round 2: partially (shallow readonly, ordering, typing) → fixed in round 2's 4 |
| 3 | Setting shapes vs contracts; `z.custom`; paths | Shapes in contracts, rules in modules, parsers in plugins, structured issues; round 2: resolved |
| 4 | Journal catalog wrong and untyped | Corrected and separated (§2.6); round 2: resolved (PR order → round 2's 8) |
| 5 | Blocker: distinct n-grams ≠ repetition; "no false positives" | Exact-period evidence (§3.1); round 2: partially → round 2's 1 |
| 6 | Channels, characters, chunk invariance, intermediate windows, non-streaming | UTF-16, absolute checkpoints; round 2: partially → round 2's 2 and 6 |
| 7 | Throw-after-journal does not abort | Abort first; round 2: resolved |
| 8 | Client and HTTP retry claims | §1's table, §3.3; round 2: resolved |
| 9 | Numeric → `"all"` migration | None; refused when new or changed; round 2: resolved |
| 10 | Seeds, trusted writes, followers, revert, undo | Each covered; round 2: resolved (compare the setting, not the account: done) |
| 11 | Three ciphertext policies | §5; round 2: resolved |
| 12 | Seam PR vs the forwarding tests; other consumers | Consumers tested; round 2: partially → round 2's 7 |
| 13 | Scope, unsupported activation, inactive plugin | Matrix, `supports`, the report; round 2: partially → round 2's 3 |
| 14 | `check:architecture` baseline | Round 2: partially → round 2's 9 |

**Round 2** (on the revision): **rework**, 1 blocker, 2 high, 5 important or medium, 1 minor; it confirmed the dependency
direction and the detector's guarantee "for one continuously tracked logical channel".

| # | Finding | Disposition |
|---|---|---|
| 1 | Blocker: observe-first does not resolve known false positives on legitimate repetition; enforcement needs an accepted policy | Policy by channel (§3.2): reasoning enforced (no legitimate case in 4.95M characters of recorded reasoning, and the rollout still starts observe-only), visible output and tool arguments observe-only, their enforcement an explicit owner decision |
| 2 | Tool identity (ids vs indices), a shared overflow detector, non-streaming indices | Channels keyed by index only; calls beyond 32 not inspected, never merged; the non-streaming index defect fixed (§3.1) |
| 3 | Remote protection excluded | Support by transport and channel (§3.4): remote text and tool channels supported opt-in; the brief's local scope stated; remote enforcement a gated decision |
| 4 | Shallow readonly scope, unstable ordering, untyped settings | A primitives-only scope, id order, `StreamInspector<S>` (§2.3) |
| 5 | "One registry design" overstated | Three kinds with shared principles (§2.1) |
| 6 | Cost and memory bounds unsupported (13.9 MiB for gram tables) | No gram tables: a suffix probe and one verification (Fine-Wilf), K scaled with W; measured (§3.1) |
| 7 | The forwarding test asserts whole event lists | An `observe` callback, not an event: the adapter's events are unchanged (§2.4) |
| 8 | PR 1 needs `JournalEventType` | The catalog moves into the seam PR; only the port's typing follows later (§7.2) |
| 9 | The architecture output comparison misses new violations | An occurrence-count baseline test (§6) |

**Round 3** (on the second revision, with the prototype): **rework**, 1 blocker, 3 high, 2 medium, 1 minor. It
confirmed the suffix-probe algorithm (720 cases against a full-window reference), the `W + K − 1` guarantee for one
channel, the dependency direction, and the `encrypted_content` fix.

| # | Finding | Disposition |
|---|---|---|
| 1 | Blocker: reasoning enforcement also needs explicit acceptance of false positives | Observe-only by default on every channel; `enforce` per channel class an owner decision; a classification workflow (§3.2) |
| 2 | Aborting inside `observe` does not wake a pending read | A latched fault (`Watchdog.fail`) aborts and wakes; checked after every read; late callbacks ignored; tested with a read that never settles (§2.3, implemented) |
| 3 | The prototype retained whole large deltas and slices | A fixed `Uint16Array` ring and scratch, released after a verdict; structural tests (§3.1, implemented) |
| 4 | Support cannot tell native from translated attempts; the matrix wrong for `claude-seat` and DeepSeek | `InspectorScope.native`; the guard declines native attempts; the matrix corrected (§3.4, implemented) |
| 5 | Random binary is not the worst case; cost wording; a biased generator | The near-periodic construction measured; the cost restated; xorshift; 64 KiB pieces with event-loop yields (§2.3, §3.1, implemented) |
| 6 | `attach` must use the request's configuration snapshot | The engine's snapshot and its account, cached per snapshot (§2.3, implemented) |
| 7 | The architecture exemption still permits new violations | Only the 11 known occurrences exempted; the full rule on the rest (§6) |

### 9.4 The implementation (`feat/router-followups-guard`)

| Round | Verdict | Findings | Disposition |
|---|---|---|---|
| 1 | rework, 2 high, 4 medium, 1 low | A deferred hidden observation stalled inspection and reordered the channel; pieces ignored cancellation and deadlines; hidden marks lost on a finish; a queued health write failed the replay rule; malformed markers accepted; a "non-streaming" test that was not; reports once per process | No queue: `observe` answers a promise the adapter waits for; a check after every await; flushed before a read's result; a stale revision is a conflict before validation; catalog and shape validation; real tests; reports reset on change |
| 2 | rework, 2 medium, 2 low | Router work charged to the upstream's silence timer; concurrent flushes overtook writes in flight; report cleanup; non-record marker data | The write is not awaited in the read and `Watchdog.hold` stops the timer; one shared writer and `settleMarks`; reconciliation; plain records only |
| 3 | rework, 2 medium, 2 low | A marker stranded between the drain's end and its cleanup; a rejected write abandoned the queue and gave timing-dependent outcomes; report cleanup; a test whose assertion path crashed | The writer clears itself inside its loop; a rejection fails the attempt, whenever it happens, and the queue is still tried; reconciliation before the early return; the journal passed in |
| 4 | rework, 2 medium | A rejected marker could still be retried away; a marker could arrive after the final settle | `recordingFault` is latched apart, wins, and is not retryable; the settle moved to the end of the read's `finally`, after observations stop |
| 5 | approve with changes, 1 medium | A provider whose close throws synchronously skipped the settle | A nested `finally`, with a test. The coordinator capped the review here: no codex round has read this last fix; the reviewer bots are the next check |
