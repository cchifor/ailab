# Design — Extend Qwen3.6-35B-A3B context for agentic flows

**Date:** 2026-07-01 · **Status:** implemented (see ADR 0015) · **Scope:** node1 CT 5001 `qwen36` instance.

## Problem
Qwen3.6 (node1 `:8081`) served a 32K context. Tool-heavy agentic prompts (large tool schemas + multi-turn
tool history) overflowed it → `400 ContextWindowExceededError`. Goal: a context window comfortable for
agentic flows with tooling.

## Decisions (from brainstorming)
| Question | Choice |
|---|---|
| Target context | **256K / native max** |
| Concurrency | **1 slot** (`--parallel 1`, full window per request) |
| Memory strategy | **Drop Gemma-4 co-residency** (→ on-demand) |
| KV precision | **q8_0** (near-lossless with flash-attn) |
| Rollout | **A** — keep `general` co-resident; staged verify → 128K → 256K |

## Phase-0 findings (the gate)
- Native `n_ctx_train` = **262144** → **no YaRN needed**, just raise `-c`.
- Architecture is **hybrid Gated-DeltaNet + SWA** → long-context KV is cheap; prefix reuse is conditional.
- Qwen3.6 is a **reasoning model** (`reasoning_content` before answer) and is **image+video** multimodal
  (so Gemma-4, previously kept for video, is redundant at steady state).

## As-built
1. `provision.sh`: added `CACHE_TYPE_K` / `CACHE_TYPE_V` env knobs (default empty ⇒ f16) → `--cache-type-k/v`.
2. Live re-provision of `qwen36`: `CTX=262144 PARALLEL=1 CACHE_TYPE_K=q8_0 CACHE_TYPE_V=q8_0`
   (keeps `-ngl 99 --flash-attn auto --jinja --metrics --mmproj`). No `--ctx-shift`.
3. `systemctl disable --now llama-server-gemma4` on node1 (freed ~15.4 GiB VRAM).
4. `litellm.yaml`: `qwen3.6-35b-a3b` `max_input_tokens` 30000 → **245760**; gemma-4 entry commented out
   (on-demand — router must not advertise a down backend).

## Validation (live, node1)
- `n_ctx` 262144, `/health` ok, `general` still ok.
- VRAM **44.57 GiB / 64** at 256K q8_0 KV (KV ≈ +2.2 GiB over 32K); GTT 0.30 GiB — no meaningful spill.
- Tool-call: `get_weather({"city":"Paris"})`, `finish_reason:"tool_calls"`.
- Needle-in-haystack at 34K: retrieved `ZEBRA-7391-QUARTZ`, `finish:"stop"`. 56K prompt processed with no
  overflow (past the old 32K wall).
- Throughput: prefill ~895 tok/s @10K → ~549 @56K; decode ~60 tok/s; ~1 s to re-attach a matching 34K prefix.

## Known limitations / follow-ups
- **Prefill dominates at high fill**; prefix reuse only for a single growing conversation on the slot.
  Divergent/interleaved requests force full re-prefill. **Top lever:** newer llama.cpp build with better
  hybrid/SWA prompt-cache + state checkpoints → bump the pin when available.
- **Reasoning model** needs generous `max_tokens` or `content` comes back empty.
- **Gateway go-live:** the `litellm.yaml` cap change only applies after merge to `main` + Flux reconcile
  + LiteLLM pod reload; until then the gateway still caps input at 30000 (model already serves 256K on :8081).
- **Fallback B** (retire `general`, dedicate node1) documented but not needed — the hybrid KV is cheap.
