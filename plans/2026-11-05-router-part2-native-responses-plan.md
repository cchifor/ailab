<!-- status: proposed (plan, revised after codex rounds 1 and 2; no llm-router code changed yet) -->
# llm-router Part 2: native Responses pass-through for `qwen3.8-27b` on cloud2 (native-first step 1, the thin slice)

**Date:** 2026-11-05 · **Scope:** `cchifor/llm-router` at `origin/main` `247b19f`, after Part 1
(`plans/2026-11-05-router-part1-stream-inspectors-plan.md`, whose conventions, §2.1, and inspector registry this plan
uses). **Design:** `plans/2026-11-04-router-native-first-design.md` (D1-D6, §7-§11) and
`plans/2026-11-04-router-standard-design.md` (§4.4, §4.7, §9.6). **Codex's review of the native-first design** (its
items 1-10) is answered in §9. **Owner (verbatim):** "robust implementation ... works with both local models (e.g. qwen)
and remote models (e.g. claude, codex) ... Avoid workarounds"; "validate that codex and claude response quality is not
negatively affected".

Evidence labels: **[probe]** a request straight at cloud2's vLLM on 2026-11-05 (`scratchpad/qual/probe_vllm.py` and
`probe_images.py`, 29 small synthetic requests, sequential; results recorded as summaries, not byte fixtures);
**[A/B]** the 2026-11-05 direct-vs-router runs (`plans/2026-11-05-router-transparency-and-planning-revalidation.md`);
**[src]** source read at the stated tag; **[pending]** required before the canary, not yet measured.

---

## 0. Summary

**The thin slice** (codex round 1's recommendation, adopted):
- **one backend:** cloud2's vLLM 0.28.0 serving `qwen3.8-27b`; cloud3 and cloud1 get their own manifest entries later
  (cloud3 moved to vLLM and joined the plain `qwen3.8-27b` route on 2026-11-06; that does not put it on the canary:
  a backend joins only with its own measured manifest entry);
- **one protocol, streaming only:** Responses with `stream: true`; `stream: false` is refused on the wire by name;
- **one route, native only:** a canary route with `nativeWires: true` and `translate: 'never'`. A request it cannot
  serve natively is refused loudly (400, naming the feature), never translated: there is no qualified translated path
  to fall back to (codex round 1, blocker 2), so none is pretended;
- **homogeneous chains:** a native route's whole resolved chain (its fallbacks, through pointers) is native-only too, and
  no translated route falls back into a native one, so one request always has one representation (codex round 2,
  finding 1);
- **rollback by one atomic configuration change,** between requests: `nativeWires` removed **and** `translate` back to
  its default in the same `If-Match` PUT, so the next turn goes translated. That is a real continuation only for the
  request classes whose rendered prompts were shown equal both ways (§1.4); namespace tools, whose translated rendering
  loses them, are not in the canary's scope (round 1, blocker 3);
- **every other route, every Codex seat and every Claude seat is untouched:** the switch is per route and off by
  default.

**What the slice delivers:** a Codex request on the canary route reaches vLLM's `/v1/responses` as the bytes the client
sent, with `model` patched and nothing else; the client gets vLLM's frames as vLLM wrote them, plus SSE comments.

**Qualification** (§1): vLLM 0.28.0 with Qwen3.8-27B (W4A16, `qwen3` reasoning parser, `qwen3_coder` tool parser,
template `sha256:c3cf9e34…`) supports, measured, what plain Codex sessions use: reasoning items in and out, function
calls (parallel too), the replayed reasoning of the router's, vLLM's and llama.cpp's item shapes, usage with cached and
reasoning tokens, leading and mid-conversation developer messages, and images with `detail`. Codex ran the A/B's direct
arm against it: S1 3/3, P1-P3 delivered. Losses and refusals are handled by name. **The canary is not yet qualified:**
§1.4 lists the gates that must pass first: published deployment identity, byte-fidelity fixtures, rendered-token
equality across a flip, and the real-Codex transport and session gates.

**Architecture** (§2): Part 1's three kinds of extension point. A **manifest registry** of qualification entries; an
**implementation registry** of backend identity readers; a **static catalog** of request mutations; a pure **planner**
that the engine calls per candidate before selection; a **native transport** with typed outcomes; a pure **Responses
tap** that feeds the stream inspectors. The availability probe (the cloud1 design) is its own PR with the contract
changes of §2.6.

---

## 1. Qualification by measurement

### 1.1 The backend

[probe, read-only] cloud2 LXC 5103, vLLM **0.28.0** (`/version`), served name `qwen3.8-27b`, checkpoint
`Qwen3.8-27B-W4A16-AutoRound-fast` (`/v1/models` `root`), `max_model_len` 262,144; flags `--reasoning-parser qwen3
--enable-auto-tool-choice --tool-call-parser qwen3_coder --enable-prompt-tokens-details --enable-prefix-caching
--mamba-cache-mode align --speculative-config {"method":"mtp",…} --tensor-parallel-size 2 --max-num-seqs 8
--max-num-batched-tokens 2048`, no `--chat-template` (the model directory's `chat_template.jinja`, sha256
`c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041`). The flags are read from the running process; they
are not yet published by cloudlab's IaC (the service runs a vendor recipe script), which matters for identity (§2.4).

### 1.2 Feature results

Each row says whether it was **measured** (a probe or the A/B), **inferred** from source, or **pending**.

| Feature | Evidence | Result | Verdict for the canary |
|---|---|---|---|
| text and reasoning output | measured | a `reasoning` item (`content: [reasoning_text]`, `encrypted_content: null`, `status`), then the message (with a `phase` key); non-standard `response.reasoning_part.added/done` events | supports |
| `reasoning.effort` none / low / medium / xhigh | measured | 200 | supports |
| `reasoning.effort` high | measured | 400 "Unexpected reasoning effort high. Supported types are xhigh (default), medium, and low." | refused by the router before the network, naming it (the translator drops effort today; Codex's profiles send `xhigh`) |
| one function call; two parallel calls | measured (final response) | `function_call` items with `arguments, async, call_id, caller, id, name, namespace, status, type` | supports; the streamed `output_item.done` items Codex consumes are **pending** (§1.4) |
| `parallel_tool_calls: false`, a forced `tool_choice` | pending | | pending |
| `namespace` tool group | measured (final response) | flattened, the call returned with `namespace` | **out of scope** for the canary: a native→translated flip loses namespace tools and their calls (§4) |
| `web_search` tool | measured | 200, the tool silently not offered | refused unless the wire's `allowLoss` names `tool:web_search` (the canary's Codex profile does not send it) |
| replayed reasoning: vLLM's own, the router's, `encrypted_content: ""` | measured | rendered (input 388 against 365 without it, all three) | supports |
| replayed reasoning with ciphertext | measured | 400 "Encrypted content is not supported." | refused by the router before the network |
| content-less reasoning item | measured | accepted, not rendered | supports |
| truncation (`max_output_tokens`) | measured | `response.completed` with `status: incomplete`, `incomplete_details.reason: max_output_tokens` | the tap journals `length`; **Codex treats it as a normal completion** (it reads the event type, not the status: `codex-api/src/sse/responses.rs:451`, `:111`), as it does talking to vLLM directly: a known client/backend incompatibility, unchanged by the router and not rewritten (§3.4). Codex sends no output limit, but vLLM may apply the model's generation-config limit when a request omits one (`api_utils.py:153`): **pending**, measured on cloud2's checkpoint (§1.4) |
| usage | measured | `input_tokens`, `output_tokens`, `cached_tokens`, `reasoning_tokens`, plus vLLM's `*_per_turn` arrays | supports |
| `model` in the response | measured | the served name | verbatim (D3) |
| streamed vs. final item ids | measured | differ | supports (Codex builds items from `output_item.done`) |
| `stream: false` | measured | 200 JSON | **out of scope** for the slice: refused on the wire (§3.4) |
| leading and mid-conversation `developer` messages | measured | 200, the instruction followed | supports; how the template renders the mid-conversation one is **pending** (the render comparison of §1.4) |
| `input_image` with `detail` | measured | 200; the model described the pixel's colour (a user message and a tool output) | supports |
| `input_image` without `detail` | measured | 400 (`detail` required) | refused by the router before the network (Codex always sends `detail` except under `use_responses_lite`, `core/src/client_common.rs:110`) |
| unknown top-level field | measured | ignored | forwarded (D2) |
| `chat_template_kwargs` | measured | honoured | forwarded, reservable per wire (§3.3) |
| `priority` | measured | 200 | operator-reserved (refused, §3.3) |
| `previous_response_id` | measured | 404 | refused by the router (no server state) |
| `store` omitted | source (vLLM 0.28 `responses/protocol.py:165`, `serving.py:191`) | vLLM defaults it to true and stores when its store switch is on | the envelope requires `store: false` explicitly (Codex always sends it); an omitted or true `store` is refused (codex round 2, finding 3) |
| `text.format` | inferred (native-first §5.2) | | pending |
| cancellation (client disconnect mid-stream) | pending | | pending |
| two concurrent long sessions through Codex compaction and continuation | pending | | pending (codex's native-first item 1) |

### 1.3 What the A/B adds

Codex 0.166 with one config for both arms (web search and multi-agent off) ran S1 3/3 direct to this endpoint and
P1-P3 delivered direct, with no 400 and no retry loop [A/B]. The translated arm with append-only replay gave the same
input token count on one recorded body (24,691 both ways) and the same greedy output. **That is evidence for one
request class** (function tools, append-only replay, a leading developer message, no images, one effort), not a proof
that the two paths render the same tokens in general (codex round 1, finding 4): token counts and greedy outputs can
agree while prompts differ. §1.4 replaces it with a direct measurement.

### 1.4 Before the canary [pending]

**Mandatory gates** (not waivable; a failure here blocks the canary, it does not narrow it) (codex round 2, finding 6):
1. **Published deployment identity** (§2.4): cloudlab publishes, for cloud2's vLLM, a deployment metadata document
   (engine and version, checkpoint, `max_model_len`, the reasoning and tool parsers, the template digest, the
   generation-config limits, a generation counter that changes on every restart or reconfiguration), readable without
   loading anything. Until it exists, the identity is `unknown` and the wire is ineligible.
2. **Byte-fidelity fixtures:** raw request bytes and raw SSE response bytes, recorded straight from cloud2 for each
   supported row of §1.2, and the request bodies Codex 0.166 actually replays after each (the CLI in docker against a
   recording proxy). They become the golden suite's fixtures (synthetic prompts only). The probes so far recorded
   summaries, not bytes.
3. **Rendered-token equality across a flip,** for each request class the canary allows: the token ids (and multimodal
   inputs) vLLM 0.28 produces for the native Responses body and for the translated chat body of the same history,
   computed with **vLLM 0.28's own preprocessing** (its Responses conversion, the model directory's tokenizer and
   template) in a docker container, no weights needed. A prefix-cache hit is not used as evidence of equality: full
   blocks only are cached, the last token is excluded, and hybrid caches can retain different prefixes (codex round 2,
   finding 2). Cache reuse across a flip is then measured **separately**, with isolated baselines and same-path controls.
   Classes: function tools, parallel calls, images in tool outputs (the translator moves them), a mid-conversation
   developer message (the translator merges it), each effort level, a changed tool inventory.
4. **Real Codex, transport and session:** cancellation (the upstream closed and cleaned up), the upstream rejecting
   with 400, 429 and 503 and an exhausted failover (each becomes the router's `response.failed`, which Codex retries
   when it does not know the code, `responses_error.rs:101`: recorded, not rewritten), truncation during text,
   reasoning and tool arguments, and the generation-config limit of §1.2.
5. **Two concurrent sessions through Codex compaction and a useful continuation** on the canary route.

**Feature qualification** (a row that fails moves to `refuses` or `out_of_scope`, and the canary starts without it):
the streamed `output_item.done` items Codex consumes for every supported row; `parallel_tool_calls: false`; a forced
`tool_choice`; `text.format` schema enforcement (checked against the schema, not a 200); image understanding,
reproducibly, with the result logged.

### 1.5 The manifest entry

```jsonc
{
  "key": { "engine": "vllm", "version": "0.28.0", "model": "qwen3.8-27b",
           "checkpoint": "Qwen3.8-27B-W4A16-AutoRound-fast",
           "templateDigest": "sha256:c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041",
           "parsers": { "reasoning": "qwen3", "tools": "qwen3_coder" },
           "maxModelLen": 262144, "generationConfig": "sha256:<published>" },
  "path": "native", "protocol": "responses",
  "features": {                                   // each: measured | inferred | pending, and supports | loss | refuses
    "text": "measured:supports", "stream": "measured:supports", "tool:function": "measured:supports",
    "tool:parallel": "measured:supports", "reasoning:replay": "measured:supports",
    "reasoning:empty_encrypted": "measured:supports", "reasoning:contentless": "measured:supports",
    "effort:none": "measured:supports", "effort:low": "measured:supports", "effort:medium": "measured:supports",
    "effort:xhigh": "measured:supports", "effort:high": "measured:refuses", "reasoning:encrypted": "measured:refuses",
    "image:with_detail": "measured:supports", "image:without_detail": "measured:refuses",
    "developer:leading": "measured:supports", "developer:mid_conversation": "measured:supports",
    "tool:web_search": "measured:loss", "tool:namespace": "out_of_scope", "non_streaming": "out_of_scope",
    "tool:custom": "inferred:loss", "text.format": "pending", "tool_choice:forced": "pending"
  },
  "finish": "payload_status",
  "runs": ["2026-11-05 probe_vllm.py + probe_images.py on cloud2", "ailab #1607 direct arm"]
}
```

Only `measured:supports`, and `measured:loss` named by an `allowLoss`, make a request eligible; `pending`,
`inferred:supports` and `inferred:loss` do not (round 2, finding 6). D2's unknown-field forwarding is separate: it
covers fields the facts extractor does not know, never a known feature the manifest does not qualify.

---

## 2. Architecture

### 2.1 Conventions

Part 1 §2.1: implementation registries, manifest registries and static catalogs, with shared principles (contracts
hold shapes, pure modules own rules and defaults, a config slice per point that is off when absent, journal events named
in `contracts/journal.ts`, a contract kit per extension point).

### 2.2 Module map

| Unit | Kind | Single responsibility | Public interface | Depends on |
|---|---|---|---|---|
| `contracts/wire.ts` | contracts | the account's wire declaration and the per-attempt plan | `WireDeclaration`, `WireProtocol`, `WirePlan` (`{ path: 'native', protocol, entryKey, mutations } \| { skip: SkipReason }`), `AttemptOutput` (`'events' \| 'native-frames'`) | contracts |
| `contracts/qualification.ts` | contracts | the manifest entry and its feature verdicts | `QualificationEntry`, `QualificationKey`, `FeatureVerdict` | contracts |
| `contracts/backend.ts` | contracts | what the router knows of a live backend, wherever it learned it | `BackendIdentity` (§2.4), `BackendIdentityReader` (the port) | contracts |
| `contracts/mutation.ts` | contracts (static catalog) | request and response mutations by name | `MUTATIONS`, `MutationName`, `PlannedMutation`, `AppliedResponseMutation` | contracts |
| `contracts/native.ts` | contracts | the native transport's outcomes, provider → engine → writer (§3.4) | `NativeFrame` (raw bytes, `lifecycle` or `output`, observations), `NativeTerminal` (`completed` / `incomplete` with reason / `failed` with code; usage), `NativeHttpFailure` (classified by the adapter: `retryable`, status, `retryAfter`, `upstreamCode`, bounded body), applied response mutations | contracts |
| `contracts/stream.ts` (Part 1, extended) | contracts | observations gain `item` (the Responses item id) beside `call` | `Observation` | contracts |
| `modules/wire/` | pure | the planner; the byte patcher; the structural pre-scan; digest helpers | `planAttempt(input) → WirePlan`, `patchTopLevel(bytes, key, value) → bytes \| refusal`, `scanTopLevel(bytes) → { keys, duplicates, nodes, depth }`, `applyDeclared(bytes, plan)` | contracts, `modules/qualification` (an explicit acyclic pure dependency) |
| `modules/qualification/` | pure | match an identity to an entry; check facts against it | `matchEntry(entries, identity)`, `eligibility(facts, entry, allowLoss)` | contracts |
| `modules/protocol/` (extended) | pure | read-only facts of a Responses body; the envelope guards | `responsesFacts(body) → RequestFacts`, `nativeEnvelopeIssues(body)` | contracts |
| `modules/responses-tap/` | pure | split SSE frames over raw bytes; observe Responses events | `FrameSplitter`, `ResponsesTap.observe(frame) → TapFacts` (output or lifecycle, observations with item identity, terminal status, usage, errors) | contracts |
| `modules/inference/` (changed) | pure | calls the planner per candidate before selection, freezes the plan at reservation with the provider generation, journals `request.wire` | engine options `plan?`, `identity?` | contracts |
| `runtime/qualification-registry.ts` | runtime (manifest registry) | qualification entries by key | `register(entry) → dispose`, `lookup(key)`, `entries()` | contracts |
| `runtime/backend-identity-registry.ts` | runtime (implementation registry) | identity readers by engine; a cache with freshness | `register(reader) → dispose`, `identity(account, model) → BackendIdentity \| unknown`, `invalidate(accountId)` | contracts |
| `plugins/wires/` (required) | registry plugin | provides `routerWires` (both registries and the planner's inputs); validates `Account.wire` and the route keys; invalidates identities on config change and disposal | service | runtime registries |
| `plugins/qualification-vllm/` | implementation | registers the vLLM manifest entries (data in the repository) and the vLLM identity reader | none | `routerWires` |
| `adapters/backend/vllm.ts` | adapter | reads `/version` and `/v1/models` (never a request that can load a model) | `BackendIdentityReader` | contracts |
| `adapters/providers/native-responses.ts` | adapter | the native transport | an `InferenceProvider` path, typed outcomes (§3.4) | contracts, `modules/wire`, `modules/responses-tap` |
| `plugins/provider-local/` (changed) | provider plugin | `support.native: ['openai-responses']`; opens the native transport for a native plan | descriptor | adapters |
| `plugins/api-openai/` (changed) | front door | the native envelope for a native-only route; raw bytes kept only for such routes; the writer for `native-frames` output | HTTP | engine, `routerHttp` |

**Seams.** The front door owns authentication, raw-body retention and the envelope guards. The **planner** (pure) gives
each candidate's plan or skip reason; the engine calls it while it evaluates candidates (`inference:584`), so a target
that cannot serve the request natively is skipped before strategy selection and admission, and re-checks it with the
acquired provider generation (`:615`) before the attempt (codex round 1, finding 5). The writer consumes the attempt's
`AttemptOutput`; in this slice a route is either native-only or translated, so one request has one representation.
Mixing them in one chain needs a per-attempt writer switch, deferred with translated qualification.

**The dependency rule** is Part 1's: modules are pure; the engine receives the planner and the identity lookup through
its options; plugins meet through services. **Boundary check:** `check:architecture` checks import patterns, not this
module map; the plan adds a test that the new modules import only `contracts` and their own files, and that no plugin
imports another plugin; `modules/wire → modules/qualification` is the one declared module-to-module edge.

### 2.3 Config

| Key | Owner | Shape | Default |
|---|---|---|---|
| `Account.wire` | `plugins/wires` validator | `[{ models: string[], protocol: 'responses', allowLoss?: string[], reserve?: string[], allowReserved?: string[], holdMs?: number }]` | absent = translated, as today |
| `RoutePolicy.nativeWires` | `plugins/wires` validator | `boolean` | absent = false: an account's `wire` acts only on routes that set it (codex's item 5) |
| `RoutePolicy.translate` | engine | `'any' \| 'never'` (`'qualified'` deferred with translated manifests) | absent = `any` (today) |

Rules: a `wire` entry's models ⊆ `allowedModels`; `allowLoss` names only features some entry marks `measured:loss`;
`reserve` adds any top-level field name except the invariant guards (§3.1), `allowReserved` exempts only fields of the
effective denylist (§3.3); `holdMs` 1000-120000; a route with `nativeWires` and `native: ['codex-responses']` is
refused; `nativeWires: true` requires `translate: 'never'` in this slice.

**Homogeneous chains** (codex round 2, finding 1): the validator resolves each route's chain (the route that serves,
through pointers, and every fallback, transitively) and refuses a chain that mixes a native-only route with a translated
one, in either direction. So a request has one representation from its first attempt to its last, and the front door
can choose the envelope before execution, as it does today. **Rollback** is one `If-Match` PUT that removes
`nativeWires` and `translate` from the canary route together (and from its fallbacks, if any are native); a PUT that
removes only one of them is refused by the same rule. Tested: pointers, fallback edits, and a conversation bound by
placement across the change.

### 2.4 Qualification and identity

- **Eligibility** (`modules/qualification`, per candidate): the route sets `nativeWires`; the account declares a `wire`
  for the model and protocol; the live identity matches an entry's key; every feature of the request's facts is
  `measured:supports`, or `loss` named by `allowLoss`. Otherwise the candidate is skipped with `wire:unqualified`,
  `wire:identity_unknown`, `wire:feature_unsupported:<feature>` or `wire:out_of_scope:<feature>`. These reasons are
  **permanent and request-caused** (`compat`'s `PERMANENT`/`REQUESTED`), except `wire:identity_unknown`, which is
  transient (the identity may be read on the next try), so with no target left the client gets a 400 naming the feature,
  or a retryable 503 when only the identity is missing.
- **Identity** (codex round 1, finding 10): `BackendIdentity = { engine, version, model, checkpoint, maxModelLen,
  templateDigest?, parsers?, observedAt, source: 'api' | 'host-metadata', generation }`.
  - **Observed, never declared** (codex round 2, finding 3): an identity is the observed values of two readers. The
    vLLM API reader reads `/version` and `/v1/models` (engine, version, served model, checkpoint root,
    `max_model_len`). The **deployment-metadata reader** reads the document cloudlab publishes per backend (§1.4 gate 1:
    parsers, template digest, generation-config limits, and a generation counter bumped on every restart or
    reconfiguration). Every key field of the manifest entry must be observed; a field only declared, or a document that
    cannot be read, makes the identity `unknown` and the wire ineligible. A checklist is not a substitute.
  - The generation counter is part of the key: a restart or reconfiguration changes it, and the wire stays ineligible
    until the re-qualification run writes a manifest entry for the new generation (or the deployment's fingerprint shows
    that nothing qualified changed).
  - The cache holds an identity for at most 60 seconds (a cheap read; the generation counter makes staleness
    detectable), and is invalidated on an account's `baseUrl` change, on a connection failure to it, and on the plugin's
    disposal. An unreadable identity is `unknown`, never assumed.
  - **No identity read goes through llama-swap's `/upstream/<model>/…`:** that path is dispatched through llama-swap's
    scheduler and can load the model (`internal/server/api.go:479`, `internal/router/base.go:487` at v250). cloud3 and
    cloud1 need host-published metadata before their entries.

### 2.5 Mutations: a static catalog

| Name | Direction | What | Where | Reported |
|---|---|---|---|---|
| `model` | request | patch the top-level `model` to the deployment's model | every native wire | planned: `x-router-mutations` and `request.wire.mutations` |
| `router-terminal` | response | the router's own `response.failed` when the upstream sent no terminal event (a timeout, an inspector fault, a budget, an upstream HTTP error after the 200 head, a stream cut) | every native wire | applied: journaled `response.mutation {name}` only (the head is gone by then) |
| `drain-cut` | response | during drain, a stream with no terminal event is cut without one | every wire | applied: journaled |

`cache-key-salt` and `overload-code` belong to the codex transport, but the salting **lives in the engine** today and
accepts any native request (`inference:114`, `:532`): the plan scopes rekey eligibility, overload state and the salt's
application to the `codex-responses` protocol explicitly, and tests a local native wire past the rekey threshold and
across a route flip (codex round 2, finding 8). **Digests:** `request.wire` records
`clientSha256` and `upstreamSha256`, and the invariant is `upstreamSha256 == sha256(applyDeclared(client, planned))`.

### 2.6 Availability probes (the cloud1 design): its own PR, and the contract changes

Adopted from the cloud1 agent's design (cloudlab `feat/cloud1-flex-host` `4c3ef04`, plan §6) as an implementation
registry, default-off, its own PR after Part 1, cloud1 joining the route only after it ships and passes the A/B gate.
Changes to its contract, to relay to the cloud1 agent:
1. **Reasons prefixed and classed:** `availability:swap_pending`, `availability:model_not_resident`,
   `availability:model_loading`, `availability:model_unloading`: transient and configuration-caused, never spill
   reasons (a bound conversation moves).
2. **The engine's answer when they exclude everything is explicit:** today an exhausted route falls through to a 429
   `CAPACITY_UNAVAILABLE`, not retryable (`inference:671`), and new reason names do not change that. The PR adds the
   mapping: only availability (and offline) reasons left → 503 `MODEL_UNAVAILABLE`, retryable, `Retry-After` = the
   probe interval.
3. **The registry enforces the probe contract:** an absolute deadline around `check()` (a probe that hangs is
   `unknown`), validation of the returned verdict's shape (invalid = `unknown`), and **expiry**: a verdict older than two
   intervals is `unknown`. `unknown` adds no reason (fail-open), which is safe only because cloud1's host invariant
   (a fast retryable 503 `model_swapped_out`, never an eviction) holds without the router.
4. **The partial-read rule changes:** `/running` readable but the in-flight snapshot unreadable gives `unknown` for
   `swap_pending`, not `available` (a pending swap must not be missed).
5. **No identity through the probe:** `/upstream/<model>/version` can load a model (§2.4); the probe stays on
   `/running` and `/api/events`. A model's identity on a llama-swap host comes from host-published metadata.
6. **Journal and cache:** `availability.changed` and `availability.check_failed` are runtime events; a move caused by
   an availability reason is journaled with its placement outcome `moved` and `cache.reset:moved`; a backend reload
   seen by the probe (a model's process restarted) invalidates its identity (§2.4).
7. **The exclusion-result matrix** (codex round 2, finding 9), when no candidate remains, by precedence: a strict binding
   keeps its own answer (as today); any candidate refused only as busy → the busy path (the admission queue, else the
   429 with `Retry-After`), as today; otherwise any permanent, request-caused reason → 400 naming it; otherwise only
   transient reasons (availability, offline, cooldown, identity unknown) → 503 `MODEL_UNAVAILABLE`, retryable,
   `Retry-After` = the shortest probe interval among them; `already_attempted` counts as busy for the failover it
   follows. Each row is a test, including cloud1 loading while cloud2 is busy.
8. **Reloads:** `/running` exposes no process generation (`internal/server/api.go:313` at v250), so polling can miss a
   restart between two checks. The verdict carries a per-model `generation` when the host publishes one (the deployment
   metadata of §2.4); without it, a reload is undetectable and the contract says so. Late results after a configuration
   change or disposal are discarded; after `unknown`, recovery needs two consecutive `available` verdicts (the design's
   hysteresis).

---

## 3. The native path, end to end

### 3.1 Front door (`api-openai`)

1. **Route selection** as today. A route that sets `nativeWires` takes the **native envelope**. Raw retention is a
   kernel contract change (codex round 2, finding 4): today it is registered per endpoint and captured before the body
   selects a route (`kernel/http.ts:99`, `:423`). The kernel keeps the raw bytes **provisionally** for a principal that
   is native-eligible (§5), and the front door **releases** them at once when the selected route is translated; the
   reservation follows the bytes.
2. **Envelope guards** (`nativeEnvelopeIssues`, the invariant guards no wire can relax): one top-level JSON object;
   **no duplicate top-level key** (JSON parsers disagree on which one wins, so a duplicate `model`, `stream` or `store`
   would let the router and vLLM read different requests: refused, 400, naming the key); `model` a bounded string;
   `stream: true` (this slice); **`store: false` stated** (vLLM's default is true, §1.2); `previous_response_id`,
   `conversation`, `background` absent; `input` a string or an array. The parsed object is used for facts and routing
   only.
3. **No subset parse** on a native-only route.
4. **The writer:** for `native-frames` output, the adapter's frame bytes, as received; the only additions are SSE
   comments (`: ok`, `: keepalive`). No `response.created` of the router's own, no rebuilt `data:` lines.

### 3.2 Facts (`responsesFacts`, read-only)

Tool kinds (`function`, `namespace`, `web_search`, `custom`, `tool_search`), input item kinds, reasoning items
(ciphertext, empty, content-less), images (with and without `detail`, in messages and in `function_call_output`),
`developer`/`system` positions, `reasoning.effort`, `text.format`, `tool_choice`, `parallel_tool_calls`, `stream`, the
reserved fields present. The vision, tools and Assistant gates read facts on native requests (today the vision gate
reads `request.messages`, which is empty for a native body); a gate-parity test checks them on native and translated
forms of every fixture.

### 3.3 Reserved fields (vLLM)

The **effective denylist** of a wire = the engine's built-in list + the wire's `reserve` (any top-level field name an
operator adds, e.g. `chat_template_kwargs` for a fixed thinking policy) − the wire's `allowReserved` (only members of
that effective list); the invariant guards of §3.1 are not on it and cannot be exempted (codex rounds 1-2,
finding 15/7). Built-in for vLLM: `priority`, `request_id`, `cache_salt`, `kv_transfer_params`,
`vllm_xargs`, `chat_template`, `add_generation_prompt`, `continue_final_message`, `add_special_tokens`, `echo`,
`return_token_ids`, `return_tokens_as_token_ids`, `prompt_logprobs`, `logits_processors`, pinned against vLLM 0.28's
Responses request schema in the qualification run. A request carrying one is skipped for that candidate
(`wire:reserved:<field>`) and, with no other candidate, refused (400) by name; never stripped.

### 3.4 Transport (`adapters/providers/native-responses.ts`)

- **Request:** the client's bytes with `model` patched in place (`patchTopLevel`: a bounded JSON scanner finds the one
  top-level `model` member and replaces its value; every other byte as received). No `JSON.stringify` (codex's item 6).
- **Typed outcomes** (`contracts/native.ts`, PR 1; codex rounds 1-2, findings 6/5): the adapter **classifies** an HTTP
  failure before the stream itself, with today's rules (`adapters/providers/errors.ts:54`, `:121`: 429 and 503
  retryable, 401/403 auth, any other 4xx or 5xx not, a connect-phase failure retryable, an ambiguous network failure
  never replayed) and reports a `NativeHttpFailure` carrying the classification; the engine never imports the adapter's
  error module. A stream yields `NativeFrame`s and ends with exactly one `NativeTerminal`: `completed` or `incomplete`
  becomes the attempt's finish (with the raw status kept), `failed` becomes a fault with the upstream's code, **never a
  finish** (`inference:760` treats a finish as success). Nothing broadens the retry policy.
- **The HTTP head:** as today, the front door sends the 200 head and `: ok` when the attempt is selected, before the
  upstream answers. An upstream HTTP error then reaches the client as the router's `response.failed` with the upstream's
  status and code (`router-terminal`, journaled), the convention the codex native path uses today. Verbatim upstream
  status codes would need the head to wait for the upstream's, a change to the writer for every protocol: deferred, and
  declared here as the slice's one deviation from verbatim. **Its effect on Codex:** Codex retries a `response.failed`
  whose code it does not know (`responses_error.rs:101`), so an upstream rejection that is not retryable over HTTP
  can be retried by Codex after the conversion; §1.4 gate 4 records the behaviour, and no code is rewritten to hide it.
- **One read loop, governed by downstream backpressure:** read a chunk, split frames (bounded: one frame at most 4 MiB,
  the stream at most the existing 256 MiB budget), give the tap each frame, hand the frame to the engine, and read again
  only when the consumer pulls.
- **Inspectors before exposure:** the adapter calls Part 1's `observe` callback with the tap's observations of a frame
  (`delivered: true`: the frame reaches the client) **before** it yields the frame: reasoning, text, refusal and
  tool-argument deltas, each with its item id, and `call` = the output index for a function call; an item whose text
  arrives only in its `output_item.done` (or the terminal response) is observed there, once. The engine does not observe
  `native` events itself, so nothing is counted twice (rounds 1-2, finding 12/5).
- **The hold rule and the timers** (finding 8): `response.created`, `queued` and `in_progress` frames are held until the
  first other frame or `holdMs` after the first held frame (canary: 30 s), whichever comes first; held frames are
  bounded (64 KiB). The hold timer races the pending upstream read, so it fires while the read waits. **Three moments are
  separated:** the first flush to the client is the protocol commitment (the failover boundary); the first model output
  (`output_item.added` or a delta) switches the engine's timer from the wait to the idle phase; lifecycle frames flushed
  by the hold do **not** switch it. So a long prefill keeps the 300 s wait timer, as on the translated path.
- **Terminal:** the tap reads the terminal payload's `status` and `incomplete_details.reason` for the journal
  (`completed` → `stop`; `incomplete` + `max_output_tokens` → `length`; anything else → the raw value, as
  `upstream_finish_reason`). The client gets vLLM's frame unchanged (§1.2's truncation row).
- **No terminal frame** (a cut, a timeout, an inspector fault): the router's `response.failed`, only if vLLM sent none.
- **`stream: false`:** refused on the wire in this slice (`wire:out_of_scope:non_streaming`).

### 3.5 Engine

The per-candidate plan, frozen at reservation; `request.wire {path, protocol, entryKey, mutations, clientSha256,
upstreamSha256}` (in `JOURNAL_EVENTS`); failover only before the first flush and only for retryable faults; the output
limiter never on local wires; stream inspectors on the tap's observations.

---

## 4. Rollback, both directions

| Direction | When | What the receiving path gets | Works when |
|---|---|---|---|
| native → translated | the atomic rollback PUT (§2.3) | Codex's replay of vLLM-produced items: reasoning items (`encrypted_content: null`), `function_call`, `function_call_output` | the request class rendered the same tokens both ways (§1.4 gate 3); the body is at most 2 MiB (the translated limit, §5) |
| translated → native | the canary configuration again (§7.3) | the router's own items (`rs_…`, no `encrypted_content`) | the same, the other way |

- **Namespace tools are out of the canary's scope** (codex round 1, blocker 3): the translator drops namespace tool
  definitions and keeps only a call's bare name (`protocol/index.ts:111`, `:178`), so a namespaced session flipped to
  translated loses callable tools and can merge distinct `namespace/name` calls. A journal entry does not restore that,
  so the canary refuses `namespace` tools (`wire:out_of_scope:tool:namespace`), and its Codex profile disables
  multi-agent tools, as the A/B's did. Native namespace support needs a namespace-preserving translator first (the
  standard's IR, R1), qualified with real calls after both flips, including equal function names in two namespaces.
- **The size envelope:** a native conversation whose request exceeds 2 MiB cannot continue translated (413). The canary
  runs short sessions; the runbook states the envelope.
- **Tests:** golden fixtures (§1.4 gate 2) through both paths; live, §7.3 step 4, after three or more native turns,
  checked by the rendered-token comparison, the separate cache measurement and a successful next tool call.
- **Epochs:** a flip is journaled per request (`request.wire.path` changes); an `epoch` with storage belongs to the
  standard's provenance ledger (R2), not this slice.

---

## 5. Admission and body size

- **Pre-read eligibility:** a principal may send a body above 2 MiB to `/v1/responses` when a route it may use is
  codex-native or sets `nativeWires` with an account that declares a wire (`nativeCaller` extended); raw retention is
  registered only then.
- **A bound before parsing** (codex round 2, finding 4): parsed-heap amplification is unbounded for arbitrary JSON
  (an array of empty objects parsed at about 21× its size in codex's probe). So the pre-scan `scanTopLevel` (one pass
  over the raw bytes, no allocation per node) counts the body's nodes and depth before it is parsed, and a native body
  over the structural limits (at most 262,144 nodes and depth 64, sized from the recorded Codex bodies with a margin) is
  refused with 413 by name. With the node count bounded, the parsed heap is bounded by `nodes × 96 bytes + raw bytes`
  (each limit measured on node 24 with adversarial shapes before release).
- **Accounting:** a native request is charged `raw + parsedBound + patchedCopy` (the raw bytes kept for patching, the
  bound above, one patched copy per attempt; a retry's copy replaces the previous one) against the existing body
  budget, from admission through the queue wait, the attempt, the bounded close and drain, as `RouterHttp.hold` does.
- **Translated limit:** a body over 2 MiB on a translated route is a 413, as today (the rollback envelope of §4).
- **Tests:** chunked bodies, a saturated queue with large bodies waiting, drain releasing the reservation, and the
  measured heap for a 16 MiB body against its charge.

## 6. Capture

The capture contract stays bounded and redacted; a native request's capture is labelled `replayable: false` when
anything was truncated or redacted. The exact-body diagnostic mode is deferred; the validation agent's recording tap
provides Layer (a).

## 7. Tests and gates

### 7.1 Deterministic, in docker, every PR

Golden native fixtures (§1.4 gate 2): `upstreamBytes == applyDeclared(clientBytes, [model])` byte for byte, and the
client stream equals vLLM's recorded stream modulo SSE comments and the declared response mutations. Transport:
fragmented UTF-8, multiline `data:` frames, `id:` and `retry:` lines, a slow reader, a client disconnect, shutdown and
drain, the hold timer during a pending read, the three timer moments of §3.4 (a prefill crossing the hold and the idle
threshold). Outcomes: 400/429/503 before the stream, exhausted failover, the classification table. The tap: every
terminal shape, usage, output vs lifecycle, observations with item identity for interleaved parallel calls. The
scanner: escaped keys, nested `model`, duplicate keys refused, whitespace, huge strings. Facts and gate parity. The
planner and eligibility for every row of §1.2. Identity: cache expiry, invalidation, `unknown`. Admission. The
module-boundary test of §2.2. The degeneracy guard on a recorded native `!` stream.

### 7.2 Real client, in docker

Codex 0.166 → in-process router → a fake vLLM replaying the byte fixtures: a text turn, a tool loop, parallel calls, an
image output, a session continued across a flip in each direction; truncation (Codex completes, the journal says
`length`); idle cases with `stream_idle_timeout_ms` 20 s: 60 s of reasoning deltas (Codex must not time out: they are
parsed events) and 60 s of prefill silence (Codex times out on both paths: vLLM sends nothing during prefill, a known
unchanged limit; production uses Codex's 300 s).

### 7.3 Live canary, short tests first (on the owner's go, after the release gate and every §1.4 gate)

1. Config: route `qwen3.8-27b-native` (cloud2's deployment only, `nativeWires: true`, `translate: 'never'`), cloud2's
   account `wire: [{ models: ['qwen3.8-27b'], protocol: 'responses', holdMs: 30000 }]`. First verify in the journal
   that every other route's requests stay `request.wire.path: translated` (or have no `request.wire`).
2. Layer (a) (the validation agent): the same recorded body direct and via the canary: equal bytes upstream (the
   journal's digests, only `model` patched), equal greedy output.
3. Codex with the canary profile (web search and multi-agent off): a text turn, a tool loop, parallel calls.
4. A session continued across a flip in each direction after three or more native turns.
5. S1 direct vs via the canary, **3 pairs** (the standard §9.6's rule), then its rule for more.

Then the long runs and the 48-hour watch (native-first §11).

## 8. Delivery

| PR | Contents | Behaviour change |
|---|---|---|
| 1 | contracts (`wire.ts`, `qualification.ts`, `backend.ts`, `mutation.ts`), `modules/wire`, `modules/qualification`, `modules/responses-tap`, facts | none (pure modules and tests) |
| 2 | the registries and `plugins/wires`, `plugins/qualification-vllm`, the vLLM identity reader, config keys and validators, the engine's planner calls and skip reasons, `request.wire` | none until a route sets `nativeWires` |
| 3 | the native transport, provider-local's native path, the front door's envelope and writer, admission | none until configured |
| 4 | §1.4's fixtures, the real-client docker tests, the golden suite | none |
| 5 (own PR, after Part 1) | the availability registry and the llama-swap probe, with §2.6's changes | none until an account sets `availability` |

One release for PRs 1-4 (5 may follow), through the runbook's flow up to the ailab roll PR, then the gate (Codex, Claude
and qwen: Layer (a) plus short Layer (b)): nothing changes for any route at the roll. The canary config only on the
owner's go, after the gate and §1.4.

## 9. Codex cross-validation

### 9.1 The native-first design review's items, in this slice

| # | Item | Status |
|---|---|---|
| 1 | Context control | vLLM's window is per sequence out of a paged pool (no llama.cpp shared window); concurrent compaction and continuation is §1.4 item 5, before the canary |
| 2 | Rollback with `encrypted_content: ""` | Part 1 (implemented); rollback scoped and measured (§4); namespace out of scope |
| 3 | Enforced qualification | measured / inferred / pending per feature (§1.5); only measured support is eligible; reasons classed (§2.4); no translated fallback in this slice |
| 4 | Admission | §5, with charged amplification and the rollback envelope |
| 5 | Route-scoped switch | `nativeWires` on the route; pointers and fallbacks pinned (§2.3) |
| 6 | Byte preservation | §3.4 |
| 7 | Non-streaming | out of scope in this slice, refused by name |
| 8 | Terminal status | the tap; Codex's handling recorded as a client/backend incompatibility (§1.2) |
| 9 | Capture | §6 |
| 10 | Short tests first | §7.3, 3 pairs |

### 9.2 Round 1 on this plan: rework, 2 blockers, 11 high, 3 medium

| # | Finding | Disposition |
|---|---|---|
| 1 | Registries not one coherent contract; missing ports; no cycle check | Three kinds (Part 1 §2.1); typed ports for planning, qualification lookup and identity; identity types in `contracts/backend.ts`; a module-boundary test (§2.2) |
| 2 | Blocker: no qualified translated path behind `translate: 'qualified'` | The slice is native-only (`translate: 'never'`); refusals by name; translated manifests deferred (§0, §2.3) |
| 3 | Blocker: namespace rollback not functional; no epoch owner | Namespace out of the canary's scope until a preserving translator is qualified; epochs left to the ledger (§4) |
| 4 | "Same prompt" overgeneralised | Restricted to the tested class; the warm-cache flip measurement before the canary (§1.3, §1.4) |
| 5 | Native decision before execution; writer per request; planning before selection | The planner per candidate before selection, frozen at reservation; native-only routes, so one representation (§2.2) |
| 6 | Verbatim HTTP errors and non-streaming need transport contracts | Typed outcomes; the 200-head convention kept and declared; non-streaming out of scope (§3.4) |
| 7 | "Any failure before flush" broadens retries | Today's classification, unchanged (§3.4) |
| 8 | The hold can halve prefill tolerance | Commitment, first output and timer phase separated; the hold timer races the read; bounded (§3.4) |
| 9 | Codex ignores `status: incomplete` | Recorded as the same incompatibility as direct; tested with real Codex; not rewritten (§1.2, §7.2) |
| 10 | Identity stale-prone; llama-swap read can load | API-only reads on vLLM; freshness and invalidation; host metadata for llama-swap hosts; a re-qualification rule (§2.4) |
| 11 | Availability's stated failure behaviour not delivered | An explicit 503 mapping, registry-enforced deadlines, verdict validation and expiry, the partial-read rule (§2.6) |
| 12 | Native observations lose per-call identity | Item ids and the output index; inspectors before the frame is exposed (§3.4) |
| 13 | Planned vs applied mutations; codex salting; digests | Planned in the header, applied in the journal; salting scoped to codex; the digest invariant stated (§2.5) |
| 14 | Admission accounting; rollback size | Charged at 3× raw, amplification measured; the 2 MiB envelope (§4, §5) |
| 15 | The denylist cannot be extended; per-wire evaluation; duplicate keys | `reserve` and `allowReserved`; per-candidate; duplicate top-level keys refused (§3.1, §3.3) |
| 16 | Probe summaries are not byte fixtures; under-measured features; 2 pairs | Byte fixtures and the pending list before the canary (§1.4); 3 pairs (§7.3) |

### 9.3 Round 2 on the revision: rework, 6 high, 3 medium

| # | Finding | Disposition |
|---|---|---|
| 1 | A native route may fall back to a translated one; rollback ambiguous | Homogeneous chains enforced by the validator, both directions; rollback one atomic PUT; tests (§0, §2.3) |
| 2 | Cache hits cannot prove prompt equality | Rendered-token equality with vLLM 0.28's own preprocessing in docker; cache reuse measured separately with controls (§1.4 gate 3) |
| 3 | Declared parser and template values stand in for identity; `store` default | Observed identity only: the API reader plus published deployment metadata with a generation counter, a gate before the canary; `store: false` required (§1.2, §2.4, §3.1) |
| 4 | The 3× charge is neither sufficient nor a bound; raw retention per endpoint | A structural pre-scan before parsing bounds the parsed heap; the charge restated; provisional retention released for translated routes (§3.1, §5) |
| 5 | The transport contract incomplete; terminal failures as finishes; observation path; module edges | `contracts/native.ts` in PR 1; failed terminals are faults; classification in the adapter; observations through Part 1's callback before the frame; the `wire → qualification` edge declared (§2.2, §3.4) |
| 6 | §1.4 needs non-waivable gates; semantics not 200s; structured-output gate; measured vs inferred loss; generation-config limits | Mandatory gates separated from feature qualification; semantic checks; the structured-output gate in the parity test; only `measured:loss` allowable; the generation-config limit measured (§1.2, §1.4, §1.5) |
| 7 | `reserve` cannot add anything | Any top-level field name except the invariant guards; `allowReserved` only within the effective list (§3.3) |
| 8 | Salting lives in the engine for any native request | Scoped to `codex-responses` in the engine, with tests past the threshold and across a flip (§2.5) |
| 9 | Availability: mixed exclusions; reload detection | The exclusion-result matrix; a per-model generation when the host publishes one, else declared undetectable; late results; recovery (§2.6) |

The 200-head convention's effect on Codex retries is recorded (§3.4) and tested (§1.4 gate 4).

**Round 3:** on this revision.
