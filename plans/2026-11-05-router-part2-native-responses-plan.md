<!-- status: proposed (plan, revised after codex rounds 1-3; no llm-router code for it merged yet) -->
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
tap** that feeds the stream inspectors. The availability probe (the cloud1 design) is its own PR, as §2.6 fits it into
the same registries.

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

**Merge against activation** (codex round 3, finding 6). These gates are all before the **canary**, not before a merge. A
default-off first merge needs the deterministic coverage, the node 24 memory checks, fail-closed activation and the
regressions for the existing Codex and Claude paths, and claims no live support.

| Gate | In docker, with fixtures | Needs the real deployment |
|---|---|---|
| 1. Published identity | Reader, matching and fail-closed tests | Publishing and verifying cloud2's document (cloudlab), no inference |
| 2. Byte fidelity | Replay and assertions | The first recordings from cloud2, and Codex's replay bodies |
| 3. Render equality | All of it: pinned vLLM preprocessing, tokenizer, template, no weights | Only the separate cache measurement |
| 4. Transport and session | Synthetic 400/429/503, exhaustion, truncation, timers, router cancellation | Backend cleanup, generation-config behaviour |
| 5. Concurrent compaction | Scripted client mechanics | Semantic and resource qualification |
| Feature qualification | Parsing and replay | Schema enforcement, forced choice, image understanding |

Live gates run on an **isolated qualification route**, created for them and removed after: the canary route exists only once
every gate has passed (§7.3).

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
| `adapters/backend/vllm.ts` | adapter | **the one owner of the composition:** one reader per engine, which makes both reads (`/version` and `/v1/models`, never a request that can load a model, and the published deployment metadata) and answers one identity whose `observedAt` is the older of the two | `BackendIdentityReader` | contracts |
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
- **Identity** (codex round 1, finding 10; shapes aligned in round 3, finding 4): `BackendIdentity = { fingerprint: { engine,
  version, model, checkpoint, maxModelLen, templateDigest, parsers, generationConfig }, generation, observedAt }`. The
  manifest key **is** the fingerprint. `generation` is not in the key: it is freshness only, for telling a restart apart. A read replaces the cached entry and re-matches its fingerprint; nothing else about it depends on `generation`.
  - **Observed, never declared** (codex round 2, finding 3): one reader per engine composes two reads. It reads `/version`
    and `/v1/models` (engine, version, served model, checkpoint root, `max_model_len`), and the document cloudlab
    publishes per backend (§1.4 gate 1: parsers, template digest, generation-config limits, and a generation counter
    bumped on every restart or reconfiguration). Every key field of the manifest entry must be observed; a field only declared, or a document that
    cannot be read, makes the identity `unknown` and the wire ineligible. A checklist is not a substitute.
  - A restart or reconfiguration is seen at the next read, so within the 60-second cache below. Until
    then a cached identity is stale by at most that window, or less when the restart fails a connection, which invalidates
    it at once. That window is accepted: a new read with an **unchanged fingerprint** stays eligible, and a changed one is
    ineligible until a re-qualification run writes its manifest entry.
    Each backend has its own observed identity: cloud3 sharing the model name qualifies nothing.
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

### 2.6 Availability probes (the cloud1 design): its own PR

Adopted from the cloud1 plan (cloudlab PR #55, head `4c2b71f`, `plans/2026-11-05-cloud1-flex-host-plan.md` §6, which
replaces the `4c3ef04` version) as an implementation registry, default-off, its own PR after Part 1, cloud1 joining the
route only after it ships and passes the A/B gate. **That section is the contract, and its names are used here:** the
types (`AccountAvailability`, `ModelAvailability`, `AvailabilityObservation`, `AvailabilityProbe<S>`, `Eligibility`), the
llama-swap truth table, the tests. This section says how it fits the registries and the engine, and where it differs.

| Unit | Kind | Owns |
|---|---|---|
| `contracts/availability.ts` | contracts | the types, one file per extension point (Part 1 §2.1), so the closure pins name it; `Account.availability?` |
| `runtime/availability-registry.ts` | implementation registry | `register(probe)` with a disposer, `available(kind)`, `validator()`: a new or changed account names a registered probe and passes its schema; an unchanged one is tolerated while the plugin is off |
| `modules/availability/` | pure | the scheduler and cache: one check in flight, deadline and abort, staleness, hysteresis, the policy |
| `plugins/availability/` | service plugin | `ctx.routerAvailability`: drives the scheduler, wires the engine, wakes waiters, the admin route, metrics, telemetry, the Assistant read tool |
| `plugins/availability-llama-swap/`, `adapters/availability/llama-swap.ts` | implementation, adapter | kind `llama-swap`; the HTTP reads, with absolute deadlines and bounded bodies |
| `modules/inference/`, `modules/routing/` (changed) | pure | engine option `availability?: (account, model) => Eligibility`; the reason joins `evaluate()`'s reasons; the capacity rule |

1. **Fail closed for accounts that opt in** (replacing the fail-open `unknown` of the earlier version). `available`
   is eligible; `unavailable` is excluded with its reason; `unknown` is excluded with `availability_unknown`: a failed
   or timed-out check, a stale verdict, no check yet, the plugin disabled or the probe kind not registered. **The core
   engine enforces this itself:** an account with `availability` and no callback wired is excluded. This inverts the
   convention for an inspector, whose missing plugin attaches nothing: an inspector only reads, a probe gates. Accounts
   without `availability` are never excluded by it, which is the default-off claim for exclusion; the capacity rule of
   point 3 is the one declared exception. cloud1 is extra capacity, so doubt costs
   throughput, never correctness, and the drain cannot be lost to a probe fault. The cost to watch: a route whose
   targets all opted in goes unserved while the probe is down, hence the alerts (`unknown` over 5 minutes by day,
   `model_not_resident` over 20 minutes by day).
2. **The scheduler enforces the contract, not the probe.** Fixed-rate ticks, skipped while a check runs; a deadline
   below the interval, with abort; a throw, a timeout or a malformed observation is `unknown` (`probe_failed`); a model
   absent from an observation is `unknown`; the scheduler owns the timestamps; a verdict older than `maxAgeMs`
   (default 3 × `intervalMs`) is `unknown` (`stale`); a late result from an older configuration or registration
   generation is dropped. Hysteresis: to excluded at once, back only after 2 consecutive completed `available` checks, per model. So
   exclusion takes effect within `intervalMs` + the deadline (3 s + 2 s) after llama-swap lists the pending request, plus
   the requests already past selection.
3. **The capacity rule** (replacing this section's earlier 503 `MODEL_UNAVAILABLE` and its matrix). Today a route with
   any reason outside `BUSY` falls through to a non-retryable 429 `CAPACITY_UNAVAILABLE` (`inference:671`), so a
   saturated route with cloud1 excluded would stop being retryable. The capacity decision classes each target:
   **occupied** (`concurrency_exhausted`), **temporarily unavailable** (the availability reasons,
   `availability_unknown`, what `BUSY` already holds) or **out** (offline, disabled, a permanent request refusal). When no
   target is eligible and at least one is occupied or temporarily unavailable, the answer is the **retryable** busy 429.
   **A target is classed over its complete reason set** (`evaluate()` accumulates them, so check order decides nothing; codex
   round 3, finding 5): any permanent, request-caused, offline or disabled reason makes it **out**; otherwise any availability
   reason makes it **temporarily unavailable**, even with a full slot, because a free slot alone cannot restore it; otherwise
   `concurrency_exhausted` makes it **occupied**. So `tools_unsupported + swap_pending + concurrency_exhausted` is out,
   `offline + concurrency_exhausted` is out, and `swap_pending + concurrency_exhausted` is temporarily unavailable. The 400
   naming a refusal stands when no target could ever serve the request. The queue keeps its one-pool rule, a strict binding
   keeps its own answer, the last retryable fault keeps its precedence, and `already_attempted` counts as busy for the failover
   it follows. **Waiters:** admission wakes on slots and queue changes, and its pump needs capacity in the queued pool, so a
   transition to available is a new input to it: `routerAvailability` calls one engine hook that re-evaluates the whole chain
   of each waiter, one queued on another pool included. **This rule corrects a route with no `availability` too** (offline +
   full today is a non-retryable 429), so that part is an intentional capacity correction, declared, not default-off. Its
   regression test pins the client-visible change: a route with a target offline and the rest full now answers the retryable
   429, and a Codex or Claude route with no target offline answers exactly as before.
   Each row is a test:

   | Targets | Answer |
   |---|---|
   | cloud1 swapping, cloud2 and cloud3 full | retryable 429, `Retry-After: 5` |
   | cloud1 swapping, cloud2 offline, cloud3 full | retryable 429, `Retry-After: 5` |
   | cloud1 swapping, cloud2 and cloud3 offline | retryable 429, `Retry-After: 30` (the swap is the only way back) |
   | all offline | as today |
   | every target refuses the request itself | 400, as today |

4. **Reasons:** `swap_pending`, `model_not_resident`, `model_loading`, `model_unloading`, `availability_unknown`, as the
   cloud1 plan names them (the `availability:` prefix of the earlier version is dropped); one set in `modules/compat`,
   beside `BUSY`, classes them. None is a spill reason: a bound conversation is `moved`, journaled with `cache.reset:moved`,
   and not moved back when cloud1 recovers.
5. **The llama-swap probe:** `/running`, then the first in-flight snapshot of `/api/events`, both under the deadline;
   `/running` alone never decides, so an unreadable snapshot is `unknown`. **No identity through the probe:**
   `/upstream/<model>/…` can load a model (§2.4). `/running` exposes no process generation
   (`internal/server/api.go:313` at v250) and `AvailabilityObservation` carries none, so a reload between two checks is
   undetectable here and the probe invalidates no identity; a llama-swap host gets a manifest entry only with
   host-published metadata (§2.4).
6. **Observability:** per request, the existing events with new values (`routing.evaluated`, the placement outcome
   `moved`); runtime `availability.changed` in the catalog (Part 1 §2.6); the three `llm_router_availability*` metrics;
   `GET /admin/v1/availability` and an Assistant read tool `server_availability`.
7. **The route** needs what the cloud1 plan's §6.9 lists, notably `maxAttempts` of at least 2, so a race's 503 is retried.

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
- **The HTTP head, as llm-router #145 made it** (codex round 3, finding 1): the adapter yields the existing `head` event as
  soon as the upstream accepts, ahead of its lifecycle hold and with `{}` when no header qualifies, and the front door's
  head waits for `Execution.head`, the first event or failure, or 30 s. There is no second writer. This wire declares its own
  response-header policy through the same allowlist helper (vLLM sends none of the Codex headers today, so it relays none),
  and `TurnStateSeal` applies to any token a wire relays. Upstream HTTP statuses are still not passed through: the writer
  sends 200 and an upstream rejection reaches the client as the router's `response.failed` (`router-terminal`, journaled),
  the convention of the Codex native path, declared as the slice's one deviation from verbatim. **The first flush is the
  first upstream frame written to the client,** never the HTTP head, `: ok` or a frame still held, so a failure before it
  can still fail over, with the client having seen only the 200 head. **A hold-timer release is a first flush:** it writes
  lifecycle frames to the client, so it commits the attempt. That is the trade `holdMs` sets, progress for the client after
  that long against failover after it, and a wire that wants failover through a long load sets it longer. **Its effect on Codex:** Codex retries a `response.failed`
  whose code it does not know (`responses_error.rs:101`), so an upstream rejection that is not retryable over HTTP
  can be retried by Codex after the conversion; §1.4 gate 4 records the behaviour, and no code is rewritten to hide it.
- **One read loop, governed by downstream backpressure:** read a chunk, split frames (bounded: one frame at most 4 MiB,
  the stream at most the existing 256 MiB budget), give the tap each frame, hand the frame to the engine, and read again
  only when the consumer pulls.
- **Inspectors before exposure:** the adapter calls Part 1's `observe` callback with the tap's observations of a frame
  (`delivered: true`: the frame reaches the client) **before** it yields the frame: reasoning, text, refusal and
  tool-argument deltas, each with its item id, and `call` = the output index for a function call; an item whose text
  arrives only in its `output_item.done` (or the terminal response) is observed there, once. The engine does not observe
  `native` events itself, so nothing is counted twice (rounds 1-2, finding 12/5). **This extends Part 1's contract**,
  which reports only what the adapter does not yield and has the engine stamp `delivered: false`: with this slice the
  adapter states `delivered` and the engine stops stamping it, `BoundInvocation.observe`'s comment loses "never output",
  and `Observation` gains the item id (`contracts/stream.ts`, PR 1 here). Three more consequences of llm-router #142 and #143
  (codex round 3, finding 2):
  - **The guard attaches to tapped protocols only.** It declines every `scope.native` today. The new protocol is allowed by
    name; untapped Codex-native attempts stay excluded.
  - **The reasoning hold does not apply to native attempts.** `holdReasoningChars` buffers `reasoning` and `usage` events, and
    a native frame releases it, so it protects nothing here and the engine ignores it on a native attempt. The short window
    still detects. So a native loop cannot fail over once a frame has gone out: `enforce` ends that attempt, and the client
    gets `response.failed`. Both qwen accounts are in `observe` today, so this slice promises no automatic recovery.
  - **Commitment and timer phase separate.** The engine's one `emitted` flag today means both "no failover" and "use the idle
    timer". This slice splits it into the first flush (commitment) and the first model output (phase), as the next point says.
    Only a native wire can make them differ: every other attempt sets both at the point `emitted` is set today, the same
    failover cutoff and the same idle-timer start. A regression test pins that for the translated, Codex-native and Claude
    paths (`tests/engine-commitment.test.ts`, in PR 2).
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
  refused with 413 by name.
- **The allocation model** (codex round 3, finding 3): the earlier `nodes × 96 bytes + raw bytes` was not a bound. A long
  mostly-ASCII string with one non-Latin-1 character costs two bytes per character, which codex measured as about 16 MiB of
  parsed heap for an 8 MiB body, and the kernel decodes the body before parsing. So, per request:
  `charge = (raw + decoded + parsed) + patched × maxAttempts`, with `decoded = 2 × raw`, `parsed = nodes × 96 + 2 × raw` and
  `patched = raw`. The first group is shared by the attempts. `patched` is one copy per attempt, held as serialized bytes
  (`patchTopLevel` works on bytes), never as a string. Attempts can overlap, because the engine moves on after a close timeout
  while the old iterator may live on. The factors are validated on node 24 with adversarial shapes before release.
- **Reservation:** the whole ceiling is reserved at admission, before any of it is allocated, so no attempt can fail for
  budget halfway through a request. At that point the body has not been read, so the ceiling comes from `Content-Length` when
  it is present, else the route's maximum native body size, with `nodes` at the structural limit. Once the pre-scan has run,
  the reservation shrinks to the measured `raw` and `nodes`. The per-attempt term is small beside the shared one, so the cost
  in admitted concurrency is small too. A request whose charge does not fit is refused at admission, as today.
- **Accounting:** that charge goes against the existing body budget, from admission through the queue wait, the attempt, the
  bounded close and drain, as `RouterHttp.hold` does. **An iterator that outlives its attempt keeps the request charged:** the
  reservation is reference-counted, one reference per attempt's iterator and one for the request, because an iterator can
  retain the shared body through its invocation as well as its own copy. The whole charge goes back when the last reference
  does, so abandoned bodies cannot accumulate uncharged across requests.
- **Translated limit:** a body over 2 MiB on a translated route is a 413, as today (the rollback envelope of §4).
- **Tests:** chunked bodies, a saturated queue with large bodies waiting, drain releasing the reservation, a close timeout
  with attempts overlapping, an abandoned iterator keeping the whole charge past teardown until it settles, a chunked body with no `Content-Length`, and the measured heap for a 16 MiB body against its charge, a mostly-ASCII two-byte string included.

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

Three PRs (codex round 3, finding 7), each with its fixtures and tests, extending the existing `InferenceProvider`,
`InferenceEvent`, `Execution.head` and error handling rather than a parallel transport framework:

| PR | Contents | Behaviour change |
|---|---|---|
| 1 | Foundations: the contracts, the scanner and patcher, facts, qualification matching (an internal file of `modules/wire`, no module of its own), the responses tap | none (pure modules and tests) |
| 2 | One default-off integration: registries and readers, validation, the transport in `provider-local`, the engine's planner calls and split commitment, the raw writer and admission, the guard's attachment, the head and seal regressions, the real-client docker tests | none until a route sets `nativeWires` |
| 3 (independent, own PR) | Availability: registry, scheduler, service, the llama-swap probe, capacity classification, waiter re-evaluation | the capacity correction of §2.6 for every route; the rest none until an account sets `availability` |

One release for PRs 1-2 (3 may go first), through the runbook's flow up to the ailab roll PR, then the gate (Codex, Claude
and qwen: Layer (a) plus short Layer (b)). Deployment metadata and the qualification evidence are collected alongside; the
canary configuration only on the owner's go, after the gate and every §1.4 gate.

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

**2026-11-06:** §2.6 was rewritten on the cloud1 plan's current version (cloudlab PR #55, `4c2b71f`). That withdraws two
answers above: the 503 mapping of round 1's finding 11 and the exclusion-result matrix of round 2's finding 9 give way to
the capacity rule, and `unknown` is now fail-closed.

### 9.4 Round 3 on the revision: approve with changes, 3 high, 3 medium, 1 low

| # | Finding | Disposition |
|---|---|---|
| 1 | §3.4 predates llm-router #145's head | The wire yields the existing `head` event; `Execution.head`; the first flush defined; no second writer (§3.4) |
| 2 | #142 and #143: guard attachment, the hold, split `emitted` | Attach by protocol; no reasoning hold on native attempts; commitment and phase split (§3.4) |
| 3 | The memory formula is not a bound | An allocation model with string width, decoding and overlapping attempts, reserved before allocating (§5) |
| 4 | Identity shapes disagree | The manifest key is the fingerprint; `generation` is freshness only; reader composition; per-backend identity (§2.4) |
| 5 | "Worst reason" needs precedence and wakeups; default-off claim | Classification over the reason set; an engine hook re-evaluates waiters; the capacity correction declared (§2.6) |
| 6 | Merge gates against deployment gates | A table per gate; an isolated qualification route (§1.4) |
| 7 | Fewer PRs | Three PRs (§8) |

**Round 4:** not planned unless the implementation finds something high; each PR has its own review.
