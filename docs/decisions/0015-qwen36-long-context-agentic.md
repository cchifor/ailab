# ADR 0015 — Qwen3.6-35B-A3B extended to native 256K context for agentic flows

**Status:** ACCEPTED (2026-07-01). Applied LIVE on node1 CT 5001 via `scripts/lxc-exec.py` (re-provision
of the `qwen36` instance): `CTX=262144 PARALLEL=1 CACHE_TYPE_K=q8_0 CACHE_TYPE_V=q8_0`. Gemma-4 stopped
(`systemctl disable --now llama-server-gemma4`) → on-demand. `provision.sh` gained first-class
`CACHE_TYPE_K/V` knobs; `litellm.yaml` `qwen3.6-35b-a3b` `max_input_tokens` 30000 → **245760**, gemma-4
entry commented out (on-demand). Validated: health, tool-call, 34K/56K needle-retrieval, VRAM/GTT.
**Relates to:** ADR 0008 (AI-LLM appliance), `docs/runbooks/ai-host-setup.md` (launch args — keep in sync).

**AMENDED 2026-07-09 — PARALLEL 1 → 2 (per-request context 256K → 128K).** With `--parallel 1` the single
slot head-of-line-blocked under Open WebUI's per-message request fan-out (measured: a deep request queue on
one node). Both node1+node2 re-provisioned to `PARALLEL=2` (total `CTX=262144` unchanged → **131072 (128K)
per concurrent slot**, 2 requests/node), and `litellm.yaml` `max_input_tokens` **245760 → 114688**. This
trades max single-request context (256K→128K) for concurrency; agentic flows needing >128K must go back to
`PARALLEL=1` (and accept 1 request/node). Also disabled Open WebUI title/tags/follow-up task generation and
switched litellm `routing_strategy` to `least-busy`. See `docs/runbooks/ai-model-swap.md`.

## Context
Qwen3.6-35B-A3B (node1 `:8081`, the coder+vision model that replaced qwen3-coder-30b) served a **32,768**
context — its `-c 32768` was well under the model's real capacity. Tool-heavy agentic prompts brushed and
overflowed that ceiling: the config history already records `"list workflows" ≈ 20k tok → 400
ContextWindowExceededError` after a `PARALLEL=2` split gave only 16K/slot. The goal: extend the usable
window so the model is comfortable in agentic flows with large tool definitions + multi-turn tool history.

Phase-0 measurement (read-only, from the live `llama-server`) established the facts the plan keyed off:
- **Native `n_ctx_train` = 262144 (256K)** — the log's `n_ctx_seq (32768) < n_ctx_train (262144) -- the
  full capacity of the model will not be utilized` confirms 256K is reachable with **no YaRN/RoPE scaling**;
  just raise `-c`.
- **Hybrid Gated-DeltaNet + SWA architecture.** The journal is full of `forcing full prompt re-processing
  due to lack of cache data (likely due to SWA or hybrid/recurrent memory)`. Consequence: **long-context KV
  is extremely cheap** (most layers carry a fixed-size recurrent state; only the few full-attention layers
  grow with context), but prompt-prefix reuse is conditional (see Consequences).
- **Reasoning model.** Output arrives as a `<think>` trace in `reasoning_content` **before** the answer /
  tool-call. Under-budgeting `max_tokens` yields an empty `content` with `finish_reason:"length"`.

## Decision
Run Qwen3.6 at its **native 256K** window, single slot, with a **q8_0 K/V cache**, and make room on node1 by
demoting **Gemma-4 to on-demand** (Qwen3.6 is itself image+video multimodal, so no steady-state vision is
lost). Approach **A** of the design: keep the separate `general` (qwen3-30b-a3b) daily driver co-resident;
`general` + Qwen3.6 weights ≈ 40 GiB leaves ample VRAM for KV.

1. **llama-server launch** (via `provision.sh`, `INSTANCE=qwen36`): `-c 262144 --parallel 1
   --cache-type-k q8_0 --cache-type-v q8_0`, keeping `-ngl 99 --flash-attn auto --jinja --metrics --mmproj`.
   No YaRN (native ≥ target). No `--ctx-shift` (silent middle-drop would corrupt tool-call structure; prefer
   fail-fast at the gateway).
2. **`provision.sh` gains `CACHE_TYPE_K` / `CACHE_TYPE_V` env knobs** (default empty ⇒ f16, backward-compatible)
   so the KV-quant config is legible durable IaC rather than buried in `EXTRA_ARGS`. On hybrid models only the
   attention-KV honors this; the recurrent/conv state stays f32.
3. **Free memory:** stop + disable `llama-server-gemma4` on node1 `:8082` (frees ~15.4 GiB VRAM).
4. **Gateway:** `litellm.yaml` `qwen3.6-35b-a3b` `max_input_tokens` → **245760** (256K − ~16K reserved for
   reasoning + answer, since it's a reasoning model). Gemma-4 entry commented out so the router never
   advertises the down backend (mirrors the on-demand-heavyweight policy).

## Measured outcome (node1, live)
| Metric | Value |
|---|---|
| Runtime `n_ctx` | 262144 (256K), `total_slots` 1 |
| VRAM, all-3-models before | 57.75 GiB / 64 |
| VRAM after Gemma-4 stop | 42.32 GiB (freed ~15.4) |
| VRAM at 128K q8_0 KV | 43.11 GiB (+0.8 for the KV) |
| VRAM at **256K q8_0 KV** | **44.57 GiB** (+2.2 over 32K baseline), GTT 0.30 |
| Prefill (cold/divergent) | ~895 tok/s @ 10K → ~549 tok/s @ 56K (tapers with length) |
| Prefill (matching prefix) | ~1 s for a repeat 34K prompt (recurrent state reused) |
| Decode | ~60 tok/s |
| Tool-call | `get_weather({"city":"Paris"})`, `finish_reason:"tool_calls"` ✓ |
| 34K needle | retrieved `ZEBRA-7391-QUARTZ`, `finish:"stop"`, ~250 reasoning tokens ✓ |

The KV is so cheap (256K ≈ 2.2 GiB) that memory was never the binding constraint once Gemma-4 was freed —
per-turn **prefill latency** is. A large `-c` is free until actually used; cost scales with the real prompt
length, not the configured window.

## Alternatives rejected
- **YaRN/RoPE scaling** — unnecessary; native `n_ctx_train` is already 262144. ❌
- **B: dedicate node1 to Qwen3.6** (retire `general` too) — would free ~34 GiB, but the hybrid KV is cheap
  enough that 256K fits with `general` resident, so the bigger routing change wasn't needed. Kept as the
  documented fallback if a future config can't co-fit. ⏸
- **Stop at 128K** — lower per-turn worst-case latency, but 128K vs 256K costs only ~1.4 GiB more VRAM and
  256K is free until used, so no reason to cap below native. Used 128K only as a staged validation gate. ❌
- **F16 KV** — higher fidelity but the recurrent state is f32 regardless and q8_0 is near-lossless with
  flash-attn; q8_0 keeps headroom for future concurrency. (F16 would also fit at these sizes.) ↔

## Consequences
- **Prefill dominates long-context cost, and prefix reuse is conditional.** For a *single, growing*
  conversation on the one slot, llama.cpp reuses the retained recurrent state → cheap incremental prefill
  (measured ~1 s to re-attach a 34K prefix). But a **conversation switch, edited history, or interleaved
  request** forces a full re-prefill (`forcing full prompt re-processing …`), which at ~550–895 tok/s is
  ~1–5 min at 64K–256K fill. Agentic flows that hold one sustained session win; bursty multi-tenant use on
  this single slot pays repeated full prefills. **Top future lever:** track llama.cpp for improved
  hybrid/SWA prompt-cache + state-checkpoint support and bump the pinned build when it lands — that would
  make the divergent-prefill case cheap too.
- **Reasoning model ⇒ generous output budget.** Callers must set a real `max_tokens`; the LiteLLM input cap
  (245760) leaves ~16K for `reasoning_content` + answer. Too small a budget returns empty `content`.
- **Gemma-4 is on-demand.** Its steady-state video-vision endpoint is down; Qwen3.6 covers image+video.
  Re-provision node1 `:8082` (runbook) and uncomment the LiteLLM block to restore it.
- **Gateway not yet live.** The llama-server change is live immediately (SSH-applied). The `litellm.yaml`
  input-cap change only takes effect once merged to `main` and Flux reconciles + the LiteLLM pod reloads;
  until then the gateway still caps Qwen3.6 input at 30000 even though the model serves 256K directly on :8081.
- **Rebuild reproducibility.** The steady-state `qwen36` launch is a runbook command, not a committed unit;
  the extended-context args now live in `ai-host-setup.md` and must stay in sync with `litellm.yaml`.

## Update (2026-07-01) — consolidated onto :8080, retired qwen3-30b-a3b (supersedes "Approach A")
The original decision kept the separate `general` **qwen3-30b-a3b** daily driver co-resident on node1
:8080 (Approach A). Since the hybrid Qwen3.6 KV turned out to be tiny (~2.2 GiB at 256K), keeping two
30B-class models on node1 bought nothing, and **Qwen3.6 is a strict upgrade** over qwen3-30b-a3b (coding +
image/video vision). So we completed **Approach B**: **qwen3-30b-a3b is removed** and Qwen3.6 is now node1's
sole daily driver, moved from the `qwen36` :8081 instance onto the **default `:8080` instance** (applied
live via `lxc-exec.py` with the same 256K/q8_0/mmproj env; the default instance re-runs base setup
idempotently). Sequenced to avoid a gap: switch :8080 → Qwen3.6 first (both ports served it), deploy the
router repoint, then retire :8081.
- **Removed:** the `qwen3-30b-a3b` LiteLLM entry; the `llama-server-qwen36.service` :8081 unit; the
  `llm-qwen36` Service+Endpoints. **Repointed:** LiteLLM `qwen3.6-35b-a3b` `api_base` → the `llm` Service
  (`llm.ai.svc:8080`); `checksum/config` bumped again. **provision.sh** default `MODEL/MODEL_ALIAS` → Qwen3.6
  (CTX/PARALLEL/MMPROJ/CACHE_TYPE defaults left generic so they don't leak into node2/3 heavyweight
  re-provisions). The Qwen3-30B **GGUF is kept on NFS** for revert.
- **Result:** node1 runs **only Qwen3.6** (~21 GiB weights + KV ⇒ VRAM ~23 GiB / 64, GTT ~0) + Gemma-4
  on-demand. The `llm` general endpoint now fronts Qwen3.6; nothing else referenced qwen3-30b-a3b or the
  `llm-qwen36` Service (Open WebUI / homepage / gatus / cloudflared all go through LiteLLM :4000).
