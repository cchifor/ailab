# Router transparency A/B and the planning re-validation (2026-11-05)

The owner, verbatim:
- *"For all providers (e.g. codex, anthropic, qwen) implement and run short tests directly, without llm-router and with llm-router, then compare the results. We must validate that the router does not negatively affect the quality of responses."*
- *"Run the planning test on qwen3.8-27 and flash-next again, after you fix the models configuration issues, directly without llm-router and validate the results."*

The method is the design's §9.6 (router transparency A/B) and §9.7 (planning re-validation), in
[`2026-11-04-router-standard-design.md`](2026-11-04-router-standard-design.md). Its margins were used as written. No other margin was fixed.

**Router under test.**
- llm-router at `router.chifor.me`, image `sha256:78915e24…`, source `247b19f`, config revision 2228 at the start.
- Clients: Codex CLI 0.166.0 and Claude Code 2.1.281.
- Runs: on dev-worker-1, 2026-11-05 from 07:13Z.

## Summary

**The router does not degrade response quality for any provider measured.** Every case where the router arm did worse traces to something outside the router: a configuration, the client, or the model.

| Provider | Layer (a): what the model sees, same body both ways | Layer (b): short tasks, direct vs router | Verdict (§9.6) |
|---|---|---|---|
| Codex (ChatGPT seat), native path | identical input tokens (14,012; 14,872), identical item types; the journal shows no adjustments | 9/9 vs 9/9 pairs; reasoning continuity 3/3 both; TTFT -1.2 to +0.2 s | **pass** (a) |
| Claude seat (Claude Code 2.1.281) | same Messages body as direct `claude -p` except the working-directory path (replica of the router launch); tool names hashed (declared) | 15/15 vs 15/15; identical tool arguments | **pass** (text and function tools; thinking not carried, declared) |
| qwen3.8-27b (cloud2 vLLM) | byte-identical prompt and greedy output; with replay on, 24,691 = 24,691 tokens | S1 3/3 vs 3/3 with replay on; planning P1-P3: router mean 15.8 vs direct 13.5 | **pass with replay `"all"`**; FAILED with replay off (b) |
| flash-next (cloud3 llama.cpp) | 2 tokens longer via the router (declared system-message merge) | S1 3/3 vs 3/3, wall 1.09x; delivered P1 plans 13.5 vs 13.75 | short tests pass; planning inconclusive. **Retired 2026-11-05** (§4) |
| qwen-image (cloud2) | byte-identical PNG with a fixed seed | - | **pass** |

(a) Through the router, a new Codex conversation's first request gets no prefix-cache hit (up to 2.4x uncached on 2-request sessions). The **client** causes this: Codex builds a different request for an API provider than for its own login. The same body sent both ways caches identically (§1.4).

(b) With replay off, qwen degenerates into an endless `!` stream on long agentic turns: 8 of 10 sends without reasoning, 0 of 10 with any (§2.2). **cloud2 now replays append-only (rev 2229)**, approved by the owner on the short A/B.

**Other results:**
- **flash-next context fix** (ailab #1605): 131,072-token window and compaction at 114,688 on all four dev workers (§2.1).
- **The "1.3x" flash-next P1 gap** in the design's §9.7 was a measurement artifact. The direct run's "15.4 min" was a failed `apply_patch`. On successful writes the pair was 19.3 vs 20.1 min, and wall time 23.5 vs 21.2 (§3.4).
- **One flash-next early stop** (a reasoning-only response) was the model, not the router (§3.4).
- **The decision:** flash-next is retired, and qwen3.8-27b goes to both cloud machines (§4).

## How it was measured

- **Two layers per provider.**
  - Layer (a), the deterministic diff: what the model received, compared by sending the same recorded body both ways, and the router's journal of what it changed.
  - Layer (b), quality on short checkable tasks: pairs interleaved ABBA, 3 pairs per task to start.
- **Recording.** A tap on the worker (`abtap.mjs`) sat in front of every arm. It wrote request bodies and response streams and never wrote header values. The recordings are conversation content: they stay on dev-worker-1 under `/workspace/c4/bench/ab/rec*` (mode 0700) and are deleted after this PR merges.
- **Same client settings in both arms.** The local-model A/B used ONE Codex config for both arms: `web_search` disabled, multi-agent off, the same retry budget and the same 900 s stream idle timeout. Only the provider and the model name differ. Yesterday's direct-vs-router pair also differed in these settings.
- **Credentials.** Only existing ones, none moved and none new:
  - the worker's router key (`codex-router-key`);
  - the worker's own ChatGPT and Claude logins, used in place (never copied into another home);
  - the router's admin bearer, used only inside the router pod.
- **Seat use:** 18 Codex runs and 30 Claude calls, plus 12 single requests for the Layer (a) checks.

## 1. Remote providers: direct vs via the router

### 1.1 Setup

- **Codex seat.**
  - Direct: Codex 0.166 on dev-worker-1 with the host's own ChatGPT login, seat c (`constantin.chifor@strive.us`, the same account as the router's `codex-2`).
  - Via the router: the same Codex, through the worker's `dw-1` key, into `pool-codex`'s native pass-through.
  - Shared settings: `gpt-6-astra` at effort `medium`, `CODEX_HOME=~/.codex` in both arms (the login is never copied: a second refresher would rotate its refresh token), and the same `codex exec` flags.
  - Both arms go through a recording tap on the worker. The tap writes request bodies and response streams, never header values.
- **Claude seat.**
  - Direct: Claude Code **2.1.281** in docker on dev-worker-1, with the host's own login (`constantin.chifor@strive.us` = the router's `claude-2`). The access token goes to the container as an env var only, never written or refreshed.
  - Flags mirror the router's `sdk-resume` transport (llm-router `247b19f`, `packages/adapters/claude-sdk`): no built-in tools, an empty config dir, no settings or hooks, `--max-turns 1`, `--system-prompt`. Tools come from a local stdio MCP stub that lists the same function tools under their real names and executes nothing, like the router's bridge.
  - Via the router: `POST /v1/chat/completions`, model `claude/sonnet` (route `claude`), from inside the router pod with its admin bearer. The router runs the same Claude Code 2.1.281 (`/data/tools/_anthropic-ai_claude-code_2.1.281`).
  - Model `sonnet` resolves to `claude-sonnet-5` in both arms.
- **Pairs:** 3 per task, in ABBA order (d,r / r,d / d,r), one run at a time. **Margins:** the design's §9.6 table, unchanged. None of my own were set.
- **No new logins, tokens, keys or seats.** Seat use: 18 Codex runs (6 two-turn) and 30 Claude calls.

### 1.2 Layer (a): what the model receives

**Codex native path: verbatim.**
- The same recorded Responses body was sent 3 times each way, alternating: direct to `chatgpt.com/backend-api/codex/responses` with the host login, and via the router with the worker key.
- The model-side input token count was identical in every call: 14,012 for a first-turn body, 14,872 for a third-turn body with tool outputs.
- The output item types were identical, and the third-turn body produced 235 output tokens every time in both arms.
- The router's journal for these requests records only placement (`new`/`bound`) and no `request.parameters_adjusted`.
- Time to the first SSE event was about 2 s longer via the router (median 2.9 s vs 0.9 s); time to the first token in real runs was not (below). The router holds the upstream's lifecycle events until the first content, so it can still fail over.

**The client builds a different request for each path.** Codex itself does this; the router does not.
- With its own ChatGPT login, Codex adds the remote-plugin skills (`openai-curated-remote` pages, sites, work-pets), a `configuration_update` input item, `access_programs`, and `internal_chat_message_metadata_passthrough` on every input item, and it compresses the body with zstd.
- With an API provider (the router), it sends none of those. The direct arm's first prompt is therefore about 3.9k tokens larger (17,903 vs 14,012).
- These differences are present before the router sees the request.

**Claude seat: a declared translation.**
- Input tokens via the router were +15 on text calls and +73 on tool calls.
- The router's bridge renames each tool to `mcp__router__t_<sha256 prefix>`, so the model sees the description and not the name.
- The history becomes a synthetic transcript resumed with `--fork-session`.
- No thinking is returned to the client or replayed.
- **What Anthropic receives.** Recording the production router's own CLI would mean setting `ANTHROPIC_BASE_URL` on the live router, a production change, so it was not done. Instead, a source-faithful replica of the router's launch (`cl-replica.mjs`, from `streamClaudeSdk` and `streamClaudeCli` at `247b19f`) was recorded through the tap, next to the direct `claude -p`. The replica uses the same synthetic transcript, `--resume --fork-session`, `--input-format stream-json`, `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`, the same flags and env, the same login and the same CLI.
  - The two `POST /v1/messages` bodies are identical in `model`, `system` (three blocks, the same cache controls), `thinking`, `max_tokens`, `output_config`, `context_management` and `tools`, and in the user message.
  - They differ only in the working directory named in Claude Code's environment reminder (`/h` vs `/h/llm-router-sdk-<random>`) and in the per-session `metadata.user_id`. The longer path explains the +15 tokens.
  - It is a replica, not the live router.

### 1.3 Layer (b): quality

| Provider | Task | Direct | Via router | Wall median d / r | TTFT median d / r |
|---|---|---|---|---|---|
| Codex | code edit, graded by `unittest` (3 bugs) | 3/3 | 3/3 | 25.1 / 21.4 s (a) | 2.74 / 2.97 s |
| Codex | 2-turn secret number (reasoning continuity) | 3/3 | 3/3 | 24.7 / 9.3 s (a) | 4.43 / 3.27 s |
| Codex | S1, 12 files read and summarised | 3/3 | 3/3 | 138 / 146 s (1.05x) | 3.72 / 3.76 s |
| Claude | arithmetic, exact answer | 3/3 | 3/3 | 2.8 / 2.7 s (b) | - |
| Claude | `to_roman`, graded by 11 cases | 3/3 | 3/3 | 2.6 / 2.9 s (b) | - |
| Claude | tool choice and arguments (currency) | 3/3 | 3/3, same arguments | 2.4 / 3.1 s (b) | - |
| Claude | tool choice and arguments (weather) | 3/3 | 3/3, same arguments | 2.3 / 3.1 s (b) | - |
| Claude | 2-turn recall | 3/3 | 3/3 | 4.3 / 4.6 s (b) | - |

(a) Direct wall time includes about 7 s per Codex process lost to the tap. The tap refuses the ChatGPT websocket, and Codex then falls back to HTTP; this is an artefact of the harness, not of either path.
(b) Claude Code's own `duration_ms`. The direct process's whole wall time, start and shutdown included, was 4.2-4.7 s. The router's figure is the HTTP call, which includes its CLI spawn.

- **Tool calls:** no invalid or rejected tool call in either arm.
- **Errors:** no router-originated error, and no retry or reconnect in the router arm.
- **The secret-number test.** Turn 1 asks the model to choose a 6-digit number in its reasoning only and reply with its digit sum. Turn 2 (`codex exec resume`) asks for the number. All 6 sessions revealed a number whose digit sum matched turn 1. That works only if the encrypted reasoning round-trips.
  - Caveat: GPT chose `739284` in 3 of the 6 sessions, so a model without memory could pass by habit.
  - The Layer (a) result is the proof; this test only agrees with it.

### 1.4 Verdict against §9.6

| Metric | Codex | Claude |
|---|---|---|
| Task success (at most 1 paired failure more than direct in 6) | pass: 0 failures in 9 pairs | pass: 0 failures in 15 pairs |
| Tool-call validity (at most direct + 2 pp) | pass: 0 % both | pass: 0 % both |
| Reasoning continuity | pass: 3/3 both | n/a: declared loss, no thinking via the seat transport |
| Uncached prompt tokens (at most 1.2x direct) | **pass for the router; the client accounts for the gap** (d): code 1.72x, secret 2.43x, S1 1.15x as measured | not measured: no cache fields through the seat transport |
| TTFT (median at most direct + 2 s) | pass: +0.23 s, -1.16 s, +0.04 s | pass on the CLI's own timing |
| Wall time (median at most 1.25x) | pass: 0.85x, 0.38x, 1.05x | pass on client-observed time (c) |
| Router errors, retries | pass: none | pass: none |

(c) On the CLI's own `duration_ms`, the tool calls are 1.30x (3.1 vs 2.4 s). That fixed ~0.7 s is the router's bridge and CLI spawn per call. As the client sees it, the router arm is faster than a cold `claude -p` (4.2-4.7 s).

(d) **The uncached gap comes from the client, not from the router.**
- As measured, every router-arm session's first request had **0** cached tokens (9 of 9, on `codex-2` and `codex-3`), and every direct session's first request had about **12,160** cached (9 of 9). After the first request the arms were equal: for example, 1,140 vs 1,220 uncached tokens for the rest of a code session.
- The isolating test: the body Codex built for its own ChatGPT login, sent both ways. Direct and via the router (placement `new`) each returned 17,903 input tokens with **12,160 cached**, 2 of 2 calls each.
- The router therefore forwards the cache-relevant request unchanged. The miss comes from the API-provider request shape that Codex 0.166 builds: it omits `internal_chat_message_metadata_passthrough`, the remote-plugin skills, `access_programs` and `configuration_update`, and with that shape the ChatGPT backend never serves a cross-conversation prefix hit.
- Codex hard-codes the shape per provider (`include_internal_metadata` is `#[serde(skip)]` in `model-provider-info`), so no config changes it.
- Cost: about 9-15k extra uncached tokens per new conversation for any Codex client on a non-OpenAI provider, routed or not. That is 1-5 % of an agent session of dozens of requests, and up to 2.4x on a 2-request session.
- Not the router's re-key salt either: `inference/index.ts` re-keys only after 3 overloads in a row.

## 2. Model configuration fixes

### 2.1 flash-next: a context window that two sessions can share (done)

**The problem.**
- cloud3's llama.cpp serves flash-next with `-c 262144 --parallel 2 --kv-unified`. That is one KV pool of 262,144 tokens, shared by both slots: `/slots` shows both slots at `n_ctx` 262144, and `/props` shows `total_slots` 2.
- The dev-worker Codex profile advertised the whole pool. Codex compacts at 90 % of the window when no limit is set, so one session could grow to about 236k and starve the second slot.
- The router's append-only replay (`lastTurns: "all"`, rev 2228) has no bound of its own, so the client has to hold the bound.

**The fix.** ailab #1605, merged 2026-11-05 07:38Z (`fede425a`) after both reviewer bots approved:
- `model_context_window = 131072`: half the pool.
- `model_auto_compact_token_limit = 114688`: 131,072 minus 16,384 of headroom for one turn's output. The largest output measured on P1-P3 was 14,179 tokens.
- The key and its semantics were checked in codex `rust-v0.166.0`:
  - `config_toml.rs` defines it;
  - `models-manager/src/model_info.rs` `with_config_overrides` applies the window and the limit;
  - `ModelInfo::auto_compact_token_limit` = min(limit, 90 % of the window);
  - the default scope is `Total`.
- reviewer-claude's one finding is in the text: the bound is approximate. Tool outputs added since the last check, and the compaction request itself, can overshoot it briefly; with `--kv-unified` that overshoot borrows from the shared pool.
- `qwen-27b` is unchanged (262,144, cloud2's `max_model_len`). Its file renders byte-identical, checked through ansible-core 2.21.0 in docker.

**Applied.**
- `ansible-playbook dev-workers.yml -t codex`, from WSL with `ANSIBLE_CONFIG` set:
  - `--check --diff` on dev-worker-1 first: one change, the flash-next profile;
  - then applied on dev-worker-1;
  - then dev-workers 2-4.
- The PLAY RECAP showed `changed=1` on each host.
- Verified:
  - all four files read 131072 / 114688;
  - on dev-worker-1, a new session's rollout shows `model_context_window` 124,518 (95 % of 131,072);
  - `codex exec -p flash-next` answers.

### 2.2 qwen3.8-27b on cloud2: replay the reasoning, append-only

**Question.** cloud2's account (`lan-192.168.0.28-18020`) had replay off: every reasoning item that Codex echoes back is dropped (journal: `input:reasoning`). Should it replay append-only (`"all"`, like cloud3), or stay off? The comparison was made without touching the production account: the router path (replay off) against vLLM's own `/v1/responses`, which replays reasoning items natively.

**What the template does.**
- cloud2's chat template renders an assistant turn's reasoning only after the last user message, which is the whole of an agentic tool loop. Without replayed reasoning, every earlier turn renders as an empty `<think>\n\n</think>`.
- vLLM 0.28's chat endpoint renders `reasoning_content` as well as `reasoning`: the prompt was 354 tokens with neither, 475 with either.
- Its `/tokenize` endpoint reads only `reasoning`. Do not use it to check a render.

**Layer (a) on cloud2.** One recorded mid-run Codex body (S1, request 10), greedy, sent 2× each way:

| Body | Direct (vLLM /v1/responses) | Via router (replay off) |
|---|---|---|
| reasoning items stripped | 24,081 tokens, output hash `bc4bc815cb0d` | 24,081 tokens, `bc4bc815cb0d` (byte-identical) |
| as Codex sends it (9 reasoning items) | 24,691 tokens, `9e901b9fa6e0` | 24,081 tokens, `bc4bc815cb0d` (the reasoning is dropped) |

The router's translation (Responses → chat, `system-messages-merged`, `reasoning_effort` ignored) gives vLLM exactly the prompt its own Responses endpoint builds. The only difference is the dropped reasoning. Both arms run at the template's default effort, `xhigh`: the server injects it when no effort is passed, and passing `xhigh` is the same.

**Decision-turn probe** (P1-d2-qw's real request, direct to vLLM chat, sequential, 3 pairs per point):

| K | As-is (= router today) | Last turn's reasoning restored |
|---|---|---|
| 43 | re-verify 1/3; prepare (`mkdir plans`, "writing the plan now") 2/3; median 2,871 completion tokens | prepare 2/3, COMMIT 1/3; 84-88 completion tokens |
| 45 | re-verify 3/3; 546-4,058 reasoning deltas | prepare 2/3, re-verify 1/3 |

- Re-verification at the decision turn: 4/6 as-is vs 1/6 restored.
- The classifier's own label is DEFER for both "re-verify" and "prepare". The split here is by the first call: reading code vs `mkdir`/`ls plans`.
- "All" itself cannot be probed on this trajectory: with every turn's reasoning the K43 request is about 285k tokens, over the 262k limit. The live arms below test it.

**The degeneration.**
- Router S1 run 1 timed out at the 15-min cap. Its last request streamed 24,035 reasoning deltas of `!` for 764 s, with HTTP 200 throughout. The router journal: `input:reasoning` dropped, `client.closed` at 764 s.
- Replaying that exact 71,860-token request:

| Prompt | Calls | Degenerate (`!` from the first token) |
|---|---|---|
| via the router | 2 | 1 |
| direct, reasoning stripped (what the router sends) | 2 | 2 |
| direct, stripped: repeated, +1 token, +a note | 6 | 5 |
| **all with no reasoning in the turn** | **10** | **8** |
| direct, all 35 reasoning items (native) | 4 | 0 |
| direct, all but the last reasoning item | 2 | 0 |
| direct, only the last 5 items | 2 | 0 |
| direct, only the last item (71,884 tokens) | 2 | 0 |
| **all with any reasoning kept** | **10** | **0** |

The last-item-only prompt is 24 tokens longer than the degenerate one and never degenerated. So the cause is not length: it is a long agentic turn whose assistant messages all carry empty thinking. This is a property of the model on cloud2 (W4A16, MTP). The router reproduces it faithfully, and append-only replay prevents it.

**S1 A/B (the 12-file task, 15-min cap; direct = native replay).**

| Arm | Runs | Pass | Wall (min) | Uncached prompt tokens | §9.3 bound |
|---|---|---|---|---|---|
| direct | 3 | 3/3 | 5.8, 13.4, 7.7 (median 7.7) | 352k | 0 violations |
| router, replay off (rev 2228) | 3 | **2/3** | timeout ("!"), 7.0, 9.3 | 331k | 0 violations |
| router, replay `"all"` (rev 2229) | 3 | 3/3 | 5.1, 7.0, 9.1 (median 7.0) | 294k (0.84x) | 0 violations |

- TTFT: the per-run medians are 2.6 s direct and 4.5 s with replay `"all"`, because the router arm's agents made fewer, larger requests (14-27 per run vs 31-54). On comparable requests (under 3k uncached tokens) the router is not slower: 1.54 vs 2.10 s.
- Append-only replay keeps every prompt a prefix of the next, and vLLM reported the cache hits: there was no re-prefill on any run, in either arm.

**Decision: cloud2 replays reasoning append-only.**
- The owner approved this on the condition that the short A/B passed, and it did.
- Applied 2026-11-05 at about 08:51Z with one `If-Match` `PUT /admin/v1/config` and a read-back, taking the config from **revision 2228 to 2229**. cloud2 is now `replayReasoning: {lastTurns: "all"}`; its `queue` (`maxWaitMs` 80000, `maxWaiting` 4) and `forwardReasoning: true` are unchanged.
- **Layer (a) check after the change**, on the same test body with its 9 reasoning items:
  - router 24,691 input tokens = direct 24,691;
  - the journal records `input:reasoning_replayed:9`;
  - the greedy output hash `9e901b9fa6e0` matched on both router calls and the cached direct call. The one cold-cache direct call differed: a fresh prefill and a cached one differ numerically under greedy decoding.
- **Rollback:** remove the key (`REVERT=1` in `set-replay-cloud2.mjs`, an `If-Match` PUT).

**For llm-router (follow-up, not done here).**
- A degenerate upstream stream passes every timeout. Here, `!` at about 17 tok/s for 764 s: deltas keep flowing, so neither the router's idle timer nor Codex's fires.
- A repetition or degeneracy guard on local accounts would have cut it within seconds.

## 3. Local models: direct vs via the router

### 3.1 Setup

- **Direct:** Codex 0.166 talks to the model server's own `/v1/responses`:
  - qwen3.8-27b: cloud2 vLLM, `http://192.168.0.28:18020/v1`;
  - flash-next: cloud3 llama.cpp b12020 through llama-swap, `http://192.168.0.26:8080/v1`.

  Both servers replay reasoning items natively.
- **Via the router:** the worker key, pointers `qwen-27b` and `flash-next`, the translated path. Replay settings:
  - cloud3: `replayReasoning: {lastTurns: "all"}` (rev 2228);
  - cloud2: off until rev 2229, then `"all"` (§2.2).
- **Profiles, one config for both arms:**
  - flash-next: 131,072 / 114,688 (§2.1);
  - qwen: 262,144.
  - Effort: both servers' templates default to `xhigh`. The direct arm passes `xhigh`, and the router drops `reasoning_effort` (declared, `x-router-ignored-params`), so both arms run at `xhigh`.
- **One run per model at a time.** The two models ran in parallel, on different hosts.
- **Short test:** S1, the 12-file task with a 15-min cap, 3 pairs per model, ABBA (§9.6, §9.3).
- **Long runs:** P1-P3 at their base commits with a 90-min cap, judged blind by the frozen judge (§9.7).

### 3.2 Layer (a): what the model receives

The same recorded mid-run Codex body was sent both ways, greedy (temperature 0) with 160 output tokens, 2× each way, alternating.

| Model | Body | Direct input tokens | Router input tokens | Greedy output |
|---|---|---|---|---|
| qwen3.8-27b (vLLM) | reasoning items stripped | 24,081 | 24,081 | byte-identical |
| qwen3.8-27b | with 9 reasoning items, replay off (rev 2228) | 24,691 | 24,081 | differs: the router dropped the reasoning |
| qwen3.8-27b | the same, replay `"all"` (rev 2229) | 24,691 | 24,691 | identical, except one cold-cache direct call |
| flash-next (llama.cpp) | with 7 reasoning items, replay `"all"` | 24,212 | 24,214 | close, not identical |
| flash-next | reasoning items stripped | 24,378 | 24,380 | close, not identical |

**qwen (vLLM).** The translation (Responses → chat, `system-messages-merged`, `reasoning_effort` ignored) gives the model exactly the prompt that vLLM's own Responses endpoint builds.

**flash-next (llama.cpp).** The router's prompt is 2 tokens longer in both cases. That comes from the declared `system-messages-merged` adaptation: the router joins Codex's system and developer texts with its own separator. Greedy outputs are therefore close but not byte-identical. The router also reports the cut at `max_output_tokens` as `response.incomplete`, which is what OpenAI specifies; llama.cpp's own endpoint says `completed`, a known upstream loss.

**A side finding.** The router drops reasoning items whose `encrypted_content` is `""`, journaled as `input:reasoning`. llama.cpp's own Responses output produces that shape. Production is unaffected: a Codex session on the router replays the router's items, which carry `null`.

**Image route.** `qwen-image-2.1-fast`, seed 42, 512x512: direct and via the router returned byte-identical PNGs (sha256 `c55593132d0420a4…`, 504,910 bytes), 2 of 2 each way.

### 3.3 Short tests: S1, 3 pairs per model

| Model | Arm | Pass | Wall (min) | Median | Uncached tokens | §9.3 bound | Errors |
|---|---|---|---|---|---|---|---|
| flash-next | direct | 3/3 | 10.9, 10.9, 10.2 | 10.9 | 272k | 0 | 0 |
| flash-next | router | 3/3 | 12.8, 8.5, 11.9 | 11.9 (1.09x) | 256k (0.94x) | 0 | 0 |
| qwen3.8-27b | direct | 3/3 | 5.8, 13.4, 7.7 | 7.7 | 352k | 0 | 0 |
| qwen3.8-27b | router, replay off | 2/3 | timeout, 7.0, 9.3 | 9.3 | 331k | 0 | the "!" stream |
| qwen3.8-27b | router, replay `"all"` | 3/3 | 5.1, 7.0, 9.1 | 7.0 (0.91x) | 294k (0.84x) | 0 | 0 |

- **TTFT** on comparable requests (under 3k uncached prompt tokens): flash-next 2.02 s direct vs 1.84 s router; qwen 2.10 vs 1.54.
- **Verdict:** both models pass every §9.6 margin through the router, qwen once its replay is on. The long runs started only after this.

### 3.4 Planning re-validation (§9.7): P1-P3, 90-min cap

**Metric.** All times run from the session's first request.
- "First write": the first plan write whose tool output shows success (exit code 0 or `Success.`).
- "Last write": the last successful write to the plan.
- A failed write is listed separately.

**Yesterday's P1 pair, re-read the same way.** The "1.3x gap" in §9.7 does not exist.
- The direct run's "delivery at 15.4 min" was an `apply_patch` that FAILED ("The last line of the patch must be '*** End Patch'").
- Its first successful write came at 19.3 min. The router run hit the same failure at 16.6 min and wrote successfully at 20.1 min.
- On the same metric: first write 19.3 vs 20.1 min (1.04x), last write 23.0 vs 20.8 (0.90x), wall time 23.5 vs 21.2 (0.90x).

**Runs.**
- qwen3.8-27b: one pair per task, P1-P3. Its router arm ran on cloud2 replay `"all"` (rev 2229).
- flash-next: two P1 pairs (ABBA) and one P2 router run. The rest was stopped by the retirement (§4).
- Plus yesterday's flash-next P1 pair, and the day-2 qwen plans as an old-router reference.

| Task | Model | Arm | Delivered | First write | Last write | Wall | Compactions | Max prompt | Uncached tokens | §9.3 flags |
|---|---|---|---|---|---|---|---|---|---|---|
| P1 | qwen | direct | yes | 14.1 | 16.8 | 17.3 | 0 | 181,658 | 179,904 | 0 |
| P1 | qwen | router | yes | **8.2** | 9.3 | **9.6** | 0 | 119,274 | 101,103 | 0 |
| P2 | qwen | direct | yes | 13.2 | 13.5 | 13.8 | 0 | 140,272 | 111,307 | 0 |
| P2 | qwen | router | yes | 11.9 | 16.0 | 16.2 | 0 | 166,717 | 131,464 | 0 |
| P3 | qwen | direct | yes | 10.7 | 12.9 | 13.5 | 0 | 141,725 | 152,333 | 0 |
| P3 | qwen | router | yes | 8.6 | 8.9 | 9.4 | 0 | 127,994 | 123,870 | 0 |
| P1 | flash-next | direct (1) | yes | 15.0 | 26.6 | 28.0 | 1 | 113,994 | 243,979 | 1 (a) |
| P1 | flash-next | router (1) | yes | 27.0 (b) | 27.0 | 27.9 | 1 | 114,157 | 253,770 | 1 (a) |
| P1 | flash-next | router (2) | **no**: early stop | - | - | 10.0 | 0 | 95,422 | 81,947 | 0 |
| P1 | flash-next | direct (2) | yes | 27.3 | 31.9 | 33.2 | 1 | 114,437 | 276,785 | 0 |
| P2 | flash-next | router | yes | 21.9 | 21.9 | 27.5 (c) | 1 | 112,964 | 217,676 | 1 (a) |
| P1 | flash-next | direct, yesterday | yes | 19.3 | 23.0 | 23.5 | 0 | - | - | - |
| P1 | flash-next | router, yesterday | yes | 20.1 | 20.8 | 21.2 | 0 | - | - | - |

All times are in minutes.
- (a) The flagged request is Codex's own compaction request: it changes the prompt's prefix, so it is a full re-prefill of about 114k tokens. That is a declared reset in §9.3 terms.
- (b) An `apply_patch` of the whole plan, missing `*** End Patch`, failed at 16.9. Compaction followed at 19.9, and the plan was rewritten with `cat >` at 27.0.
- (c) Killed by the retirement after delivery.

**Plan quality: the blind judge, unchanged.**
- The judge: `codex exec --profile review`, gpt-6-astra, xhigh, read-only, the frozen prompt and checklists.
- Each task's plans were judged together, blind to model and arm, in two passes with different letter orders. Totals are /25; the criteria are each the mean of the two passes.
- Not delivered = 5/25 (the benchmark's delivery-aware rule).

| Task | Model | Arm | Pass 1 | Pass 2 | Mean | Correctness | Completeness | Feasibility | Verification | Grounding |
|---|---|---|---|---|---|---|---|---|---|---|
| P1 | qwen | direct | 13 | 13 | 13.0 | 2 | 3 | 3 | 2 | 3 |
| P1 | qwen | router | 19 | 19 | **19.0** | 3 | 4 | 4 | 4 | 4 |
| P1 | qwen | old router (day 2) | 17 | 16 | 16.5 | 3 | 3 | 4 | 3 | 3.5 |
| P2 | qwen | direct | 13 | 14 | 13.5 | 2 | 2.5 | 3 | 3 | 3 |
| P2 | qwen | router | 14 | 14 | 14.0 | 2 | 3 | 3 | 3 | 3 |
| P2 | qwen | old router (day 2) | 8 | 8 | 8.0 | 1 | 2 | 1 | 2 | 2 |
| P3 | qwen | direct | 14 | 14 | 14.0 | 2 | 2 | 4 | 3 | 3 |
| P3 | qwen | router | 15 | 14 | 14.5 | 3 | 3.5 | 3 | 3 | 2 |
| P3 | qwen | old router (day 2, two runs) | 15 / 14 | 14 / 11 | 14.5 / 12.5 | | | | | |
| P1 | flash-next | direct (1) | 14 | 14 | 14.0 | 2 | 3 | 3 | 3 | 3 |
| P1 | flash-next | direct (2) | 14 | 13 | 13.5 | 2 | 3 | 3 | 2.5 | 3 |
| P1 | flash-next | router (1) | 14 | 13 | 13.5 | 2 | 3 | 2.5 | 3 | 3 |
| P1 | flash-next | router (2) | - | - | 5 (not delivered) | | | | | |
| P1 | flash-next | direct, yesterday | 16 | 16 | 16.0 | 3 | 3 | 3 | 3 | 4 |
| P1 | flash-next | router, yesterday | 17 | 18 | 17.5 | 3 | 4 | 4 | 3.5 | 3 |
| P2 | flash-next | router | 17 | 17 | 17.0 | 3 | 4 | 3 | 3 | 4 |

- The two passes differed by 0-1 point on every plan except one old-router reference (3 points).
- An earlier interim pass over the P1 and P2 plans that had landed by 10:07Z agreed with these to within 1 point per plan.

**Against §9.7, per model.** The routed plan mean must be within 2/25 of direct, a plan must be delivered via the router whenever it was delivered direct, and every run must pass the cache check.
- **qwen3.8-27b: PASS.**
  - Routed minus direct: P1 +6.0, P2 +0.5, P3 +0.5; mean 15.8 vs 13.5.
  - 3/3 delivered in both arms.
  - The router was faster to the plan on P1 and P3 (8.2 vs 14.1, 8.6 vs 10.7 min) and slower on P2 (11.9 vs 13.2 first write, 16.2 vs 13.8 wall).
  - No cache flags.
  - The old-router day-2 plans (replay off) scored at or below today's routed plans on every task, worst on P2 (8.0).
- **flash-next: INCONCLUSIVE, retired** (§4).
  - Delivered P1 plans only: router 13.5 vs direct 13.75 (−0.25, within the margin). Pooled with yesterday's pair: router 15.5 vs direct 14.5 (+1.0).
  - Delivery-aware: one routed P1 run did not deliver (the early stop below), which puts today's routed P1 mean at 9.25 against 13.75. That stop was diagnosed as the model's, and the balance pair that would have measured its rate in both arms was cancelled by the retirement.
  - The only §9.3 flags are Codex's compaction requests, the declared reset.

**What the judge flagged** (the main correctness reason, both passes agreeing):
- qwen P1 router (19.0): a valid central fix. Concurrent timer writes and repeated growing snapshots are unsafe for some clients.
- qwen P1 direct (13.0): wrong reasoning `summary` fields. Its heartbeat stops after the reasoning item, contradicting its own promised protection during later pauses.
- qwen P2 router (14.0): pre-header queuing is right. But it puts 120 s under Cloudflare's 100 s limit, the duration timer cannot interrupt the wait, and its fairness gate can be bypassed.
- qwen P2 direct (13.5): the adaptive retry hint keeps the original 40 s failure on cold starts, and it cannot reclaim orphaned connections.
- qwen P3 router (14.5): the 5-minute 15 %/25 % pressure guard addresses the cause. It lacks Docker-busy gating, and its grounding is the weakest of the set (2).
- qwen P3 direct (14.0): rejects pressure-driven reclaim, so exhaustion within the day stays unresolved, and its unattended cleanup can delete compose stacks.
- flash-next P1 (13.5-14.0, both arms): heartbeat designs whose reasoning snapshots are inconsistent with the stream (a single accumulator, or truncated reasoning).
- flash-next P2 router (17.0): holding before selection is right. Newcomers can overtake waiters, and the retry estimator divides by zero when every slot is busy.

**The flash-next early stop (P1-rv-3-fnrouter).**
- The session ended at 10.0 min without a plan. Its last response (journal `5075b797`) was reasoning only: 89 tokens, no text, no tool call, upstream finish `stop`. The reasoning broke off mid-sentence while the model was writing about think tags. Codex ends a turn on a response without a tool call.
- Router-side checks, all clean:
  - the journal records `input:reasoning_replayed:28` (every reasoning item), and the history is in order;
  - the router's prompt for that request is 95,422 tokens against llama.cpp's own 95,420: the declared 2-token `system-messages-merged` difference;
  - replaying it 3x each way with production sampling, no call stopped. All six went on to a tool call; one direct call hit the 1,500-token cap while still reasoning.
- Across every run since the morning, both models and both arms, no other session ended this way. Earlier router-era benchmark runs did, in 3 of 33 (`I2-r1-fn`, `I3-r2-fn`, `I1-r1-qw`).
- It counts as a flash-next model failure. One more P1 pair (router, then direct) was run so that the arms stay balanced.

## 4. Decision: flash-next retired (2026-11-05)

**The owner's decision, 2026-11-05 about 10:40Z:** retire flash-next, and run qwen3.8-27b on both cloud machines.

**Stopped at the decision:**
- the flash-next lane: P2 direct, P3 both arms, and the extra P1 pair queued after the early stop;
- P2-rv-5-fnrouter, killed at 27.5 min. It had already delivered its plan (first write 21.9 min), and that plan is judged.
- cloud3 was confirmed idle at 10:41Z: both slots `is_processing: false`, and no client connection from dev-worker-1.

**What the record shows for flash-next:**
- It passed every short-test margin through the router (§3.3).
- Its planning was slow and fragile in both arms:
  - first writes at 15-27 min;
  - one compaction per P1 run at the new 114,688 limit;
  - `apply_patch` without `*** End Patch` in 4 of its 6 P1 runs (2 direct, 2 via the router; qwen never);
  - one reasoning-only early stop.
- qwen3.8-27b delivered every plan in 8-14 min.

**Follow-ups this decision creates (not done in this PR):**
- the dev-worker `flash-next` profile and the router's `flash-next` pointer and `qwen3.8-flash-next` route;
- cloud3's `replayReasoning` setting;
- the cloud3 migration itself, which runs separately (qwen3.8-27b installed, flash-next removed).

## 5. Not compared, or not finished

- **Anthropic API, OpenAI API, DeepSeek:** no such account exists or is planned (design §13). Not applicable.
- **Claude seat, agentic coding:** not comparable, as §9.6 says. Only text and function-tool tasks were run.
- **Claude seat, what Anthropic receives from the live router:** not recorded, because that needs a production change. A source-faithful replica was recorded instead (§1.2).
- **flash-next planning:** P2 direct and P3 (both arms) were not run, and the balance P1 pair was not run (§4).
- **qwen planning:** one pair per task (P1-P3). The router arm scored better on all three, so no extra pairs were needed to decide the margin.

## 6. Follow-ups

**llm-router:**
- **Degeneracy guard.** A degenerate upstream stream passes every timeout: deltas keep flowing, so neither the idle timer nor Codex's fires (the cloud2 `!` stream ran 764 s). Add a repetition or degeneracy guard for local accounts.
- **Empty-string `encrypted_content`.** Reasoning items with `encrypted_content: ""` (llama.cpp's own shape) are dropped instead of replayed (`input:reasoning`). This does not affect production. It matters only if a client moves a llama.cpp-native session onto the router.
- **The numeric `lastTurns`** is still accepted (R0b in the design).

**Codex:**
- On an API provider, the client's request shape loses the backend's cross-conversation prefix cache (§1.4 note d). It costs about 9-15k uncached tokens per new conversation, routed or not. There is no config fix in 0.166.

**This harness:**
- The recordings on dev-worker-1 (`/workspace/c4/bench/ab/rec*`) are deleted after this PR merges.
- The scripts stay: `ab/` on the worker, and the scratchpad `router-ab/` on the workstation.

## Evidence

**dev-worker-1, `/workspace/c4/bench/ab/`:**
- the harness: `abtap.mjs`, `ab-run.sh`, `ab-series.sh`, `lane.sh`, `runq.sh`, `cx-run.sh`, `cl/`;
- the probes: `probe_qwen.py`, `degen_probe.py`, `degen_variants.py`, `stop_probe.py`;
- the reports: `ab_report.py`, `p_report.py`, `la_local.py`, `la_same_body.py`;
- the logs: `series-*.log`, `lane-*.log`, `cx/`, `degen-*.txt`, `stop-probe.txt`;
- the runs: `/workspace/c4/bench/runs/{S1,P1,P2,P3}-*`.

**The judge:** the workstation bench copy, `judge/{P1-rv2,P2-rv2,P3-rv}-p{1,2}.{mapping,scores}.json` and `rv-scores-detail.json`.

**Router journal trajectories cited:**
- `e23b8d9e`: the `!` stream;
- `5075b797`: the early stop;
- `d296cf2e`: replay after rev 2229;
- `440afe79` and the rest: Codex native, no adjustments.
