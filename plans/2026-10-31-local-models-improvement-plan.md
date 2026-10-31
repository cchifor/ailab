# Local models: fix the timeouts, speed up flash-next, prune cloud3, re-validate

## Context

The owner asked (2026-10-31) for seven things: fix the timeout issues, improve `qwen3.8-flash-next`
(update llama.cpp), remove `local/qwen3.8-27b` (32 tok/s is not enough), remove
`qwen3-coder-30b-a3b` (older and weaker), remove `qwen3.8-27b-w4a16`, validate that the remaining
models work in llm-router, and re-run the benchmarks.

Measured the same day, through the router (`kubectl exec` into the router pod, `127.0.0.1:8787`,
the engine the Assistant and the public API use). Load = request start to llama-swap's
"Health check passed":

| Router model | Server | Load | Decode tok/s | Prefill tok/s (9.3k) |
|---|---|---|---|---|
| `local/qwen3.8-27b` | cloud3 `.26:8080`, llama.cpp Q6_K (NFS) | 9 s page-cached, ~102 s cold | 32 | ~1,850 |
| `local/qwen3.8-27b-fp8` | cloud3, vLLM TP=4 + MTP | ~183 s | ~140 | ~1,210 |
| `local/qwen3-coder-30b-a3b` | cloud3, llama.cpp (NFS) | ~81 s | ~168 | ~8,500 |
| `local/qwen3.8-flash-next` | cloud3, llama.cpp **b10857**, UD-Q3_K_XL | ~62 s | ~49 | **~99** |
| `local/qwen3.5-122b` | cloud3, llama.cpp (NFS) | **~325 s** | ~67 | ~1,640 |
| `local/qwen3.8-27b-ailab` | ailab `.45:8082`, Vulkan | 31 s | 17–22 | ~162 |
| route `qwen3.8-27b` | cloud2 `.28:18020`, vLLM, always loaded | – | ~160 | ~1,480 |
| (`.46:8082`, `exposeModels:false`, no route uses it) | ailab | 67 s | 22–23 | 172 |

`qwen3.8-27b-w4a16` (cloud3's default, preloaded at boot) answers HTTP 200 with degenerate text
("lăn lăn…", 8/8 replies). It was turned off in the router at config rev 2093 (`disabledModels`
on `lan-192.168.0.26-8080`), but it is still the cloud3 half of LiteLLM's
`qwen3.8-27b-vllm-cloud` (`litellm.yaml` and `litellm-local.yaml`), which is Open WebUI's default.

### Root causes (verified)

1. **Assistant "The model route … failed (CANCELLED)".** llm-router
   `packages/plugins/agent-llm/loop.ts:15` `STEP_TIMEOUT_MS = 90_000` is a TOTAL cap per model
   step, request to end of stream. The failing trajectory (`3853a810…`, config rev 2096) shows
   `client.closed` at 89.99 s, cause `CLIENT_CLOSED`, while cloud3 was swapping `qwen3.8-27b` in. An
   Assistant step sends 13–15.5k input tokens (measured from recent Codex steps), so even a WARM
   model whose prefill is under ~170 tok/s (flash-next, `.45`) cannot fit 90 s. The engine's own
   timers are looser and better shaped: wait 300 s before any output, idle 120 s after, reset by
   raw upstream bytes (`packages/modules/routing/index.ts` `DEFAULT_WAIT_MS`/`DEFAULT_IDLE_MS`).
2. **Cold `qwen3.5-122b` through the API times out.** Its load (~325 s from the QNAP NFS) is
   longer than the engine's 300 s wait; the router returns `TIMEOUT` (phase wait). llama-swap keeps
   loading, so the NEXT request works.
3. **flash-next prefill regressed ~9×** (runbook 2026-09-11: ~860 tok/s at 30k; now ~99 tok/s at
   9.3k). Its 26.82 GiB n-gram table (`per_layer_token_embd`) is never in VRAM: b10857's
   `--lazy-mode auto` reads rows on demand through mmap (runbook "Flash-Next at UD-Q6_K_XL"). Cold
   load also went from 13.7 s (09-08) to ~62 s, and cloud3 now carries 8 VMs (184 GiB assigned,
   ~49 GiB host RAM available), so the table most likely no longer stays in page cache and every
   row read is an NVMe page fault. Unconfirmed until step 2.1 measures it. Upstream llama.cpp PR
   #29599 (`llama_prefetch_rows`, merged 2026-09-30) adds madvise prefetch of PLE rows in lazy mode
   for this architecture (Qwen4Exp); its CUDA reviewer numbers went 467.5 → 826.9 t/s prefill.
   Current upstream release: b12020 (2026-10-31).

## Decisions for the owner (recommendations in bold)

- **D1. cloud3's default (preload) once w4a16 is gone:** **`qwen3.8-flash-next`** (dsh's
  `agent-default-model`, ~62 s load, the model this plan speeds up), or `qwen3.8-27b-fp8` (183 s
  vLLM start), or no preload.
- **D2. The removed models' files:** delete `/models-local/vllm/models/Qwen3.8-27B-W4A16-AutoRound-fast`
  (15 GiB on the 84%-full local NVMe — the space is needed for the llama.cpp work); **keep the two
  NFS GGUFs (`qwen3-coder-30b-a3b`, `qwen3.8-27b` Q6_K) for 30 days, unserved, then delete**.
- **D3. A fast 27B in the picker:** after `local/qwen3.8-27b` (cloud3 Q6_K) goes, **set
  `exposeModels: true` on `lan-192.168.0.28-18020`** so `local/qwen3.8-27b` resolves to cloud2's
  vLLM copy (~160 tok/s, always loaded). The bare name `qwen3.8-27b` already resolves to the cloud2
  route and is unaffected either way.
- **D4. `qwen3.8-27b-ailab` (17–22 tok/s, 162 tok/s prefill)** fails the same "too slow" bar but
  is NOT on the owner's list: **keep it (it is the LiteLLM fallback and dsh lists it); out of
  scope here**.
- **D5. `qwen3.8-27b-vllm-cloud` becomes cloud2-only** (no second deployment). Accept: cloud3's
  half is serving garbage today, and the gateway's fallback chain stays.

## Approach

Five workstreams. Order matters: consumers stop naming a model BEFORE the model disappears.

### WS0 — Stop pointing at the removed models (ailab PR, first)

1. `kubernetes/apps/apps/ai/litellm.yaml` and `litellm-local.yaml`: delete the cloud3
   (`qwen3.8-27b-w4a16`, `.26:8080`) deployment of `qwen3.8-27b-vllm-cloud`; delete the
   `qwen3-coder-30b-a3b-cloud` entry. Keep the two `qwen3.8-27b-vllm-cloud` blocks in agreement
   (`scripts/check-litellm-mirrored-routes.py` and its test). Recompute the `checksum/config`
   annotation as the file's comment describes.
2. `kubernetes/apps/apps/ai/open-webui.yaml` `OPENAI_API_CONFIGS` `model_ids`: drop
   `qwen3-coder-30b-a3b-cloud`.
3. `kubernetes/apps/apps/dsh/settings.seed.yaml` `llm-router-local.models`: drop
   `qwen3.8-27b-w4a16` and `qwen3-coder-30b-a3b` (and `qwen3.8-27b` only if D3 is declined — with D3
   it now means cloud2, which is what dsh's comment already intends). The provider block is
   re-spliced into the live settings on every pod start (`reconcile-provider.js`), so the rollout
   that the ConfigMap change triggers updates the live dsh.
4. Sweep every remaining consumer before merging: `git grep` across ailab, cloudlab, and the
   cchifor/platform and agentforge* checkouts for `qwen3-coder-30b-a3b`, `qwen3.8-27b-w4a16`,
   `w4a16`, `qwen3.8-27b-cloud`, `.26:8080` (`scripts/gen-litellm-consumers.py`, Gatus
   `configmap.yaml`, `docs/runbooks/model-registration.md`, `litellm-vkeys.yaml`, ADR 0039 text).
   Update docs; do not rewrite `plans/` (historical).

### WS1 — Timeouts

1. **llm-router code (the root fix).** In `agent-llm/loop.ts`, stop capping a model step at 90 s
   wall-clock. The engine already ends a dead upstream (300 s with no bytes, 120 s idle) and the
   user has Stop. Replace the 90 s total with: (a) no agent-side cap tighter than the engine's
   timers — keep only a generous safety total (proposed 10 min, a named constant); (b) after 15 s
   with no event, a visible status ("Waiting for <model> — it may be loading, Ns") so a cold load
   is not a silent spinner. Tests (vitest, fake timers): a step whose first event arrives at 150 s
   completes; a stalled upstream still ends through the engine's timeout and falls back to guided
   mode; Stop during the wait ends the turn with no fallback (keeps U13). `loop.ts` is in the
   vendorable set (`scripts/check-architecture.mjs`, `tests/library-boundary.test.ts`;
   cchifor/platform vendors it via `scripts/sync-llm-router.mjs`): keep it inside those rules and
   note the change for the platform pin. Ship through the image release recipe (Relay CI build,
   ailab digest bump, `validate-live`), with the codex PR review.
2. **Engine wait for slow cold loads (config, no release).** Set the router config's
   `defaults.timeouts.waitMs` to 420 000 so a cold 122B (~325 s) is not cut off. It is router-wide
   (direct-model requests take the defaults); the cost is that a hung Codex or Claude upstream takes
   7 min instead of 5 to fail over. Alternative if that is unacceptable: leave 300 s and accept that
   only the FIRST cold 122B request fails. Record the revision in the llm-router runbook.

### WS2 — flash-next and llama.cpp on cloud3 (cloudlab PRs)

1. **Confirm the cause first (read-only, ~30 min).** During a 9.3k-token prefill on
   flash-next: sample llama-server `majflt` (`/proc/<pid>/stat`), `iostat -x 1` on the nvmedata
   device, `/proc/pressure/{memory,io}`, the LXC cgroup `memory.stat` `file`, and `/slots`
   (`timings.cache_n`). Repeat the same prompt with `cache_prompt:false`: a much faster second run
   means the page cache, not the kernels. Also measure decode at 15k depth.
2. **Codify the llama.cpp build** (none exists in git; b10857 was hand-built). Add
   `host/build-llama-cpp.sh <tag>` to cloudlab: clone the pinned tag, `cmake -DGGML_CUDA=ON
   -DCMAKE_CUDA_ARCHITECTURES=86`, build into `/models-local/llama-<tag>/`, write the source sha
   next to the binary. Build b12020 (or the newest tag at implementation time) BESIDE b10857;
   confirm it builds against cloud3's CUDA 12.4 / driver 550.
3. **Gate the new build** with a temporary llama-swap entry (`qwen3.8-flash-next-next`) on the same
   GGUF and flags, then variants: `--lazy-mode auto` (prefetch), `-ub 2048 -b 4096`, and — only if
   WS2.1 shows page faults dominate and host memory allows — `--lazy-mode off --no-mmap` (~27 GiB
   of LXC anon memory while resident; check host `available` and PSI with the CI VMs busy). Pass
   bar: prefill ≥ 600 tok/s at 9.3k and at 15k, decode ≥ 55 tok/s, the quality prompt set (below)
   correct, per-card VRAM within the current headroom, no sustained memory PSI. Run the gate with
   llama-swap holding nothing else.
4. **MTP is optional and gated separately.** Unsloth's page builds it from a custom branch
   (`danielhanchen/llama.cpp` `qwen4exp/mtp`), not mainline. Enable `--spec-type draft-mtp` with
   `MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf` (2.79 GB) ONLY if the mainline build's
   `llama-server --help` offers it for this architecture; never deploy a personal fork.
5. **Re-download only if needed.** If the new build refuses the existing UD-Q3_K_XL shards, stage the
   new ones on the NFS `/models` (4.4 TiB free), verify `sha256` against the HF metadata into a
   `host/*.SHA256SUMS` file, then swap into `/models-local` (62 GiB free + 15 GiB from D2 does
   not fit a second 84 GiB copy side by side).
6. **Cut over:** point the `qwen3.8-flash-next` entry at the new binary and winning flags, delete the
   temporary entry, keep `/models-local/llama-b10857` for a 14-day rollback window. Deploy by the
   runbook procedure (timestamped on-box backup, `pct push 5101 … /etc/llama-swap.yaml`, restart
   llama-swap at a quiet moment), then confirm the repo and the box are byte-identical (`sha256sum`).

### WS3 — Remove the three cloud3 models (items 3, 4, 5)

1. **Router (runtime config via the admin API, after WS0 merges):** on `lan-192.168.0.26-8080`,
   add `qwen3.8-27b` and `qwen3-coder-30b-a3b` to `disabledModels` (w4a16 is already there). Apply
   D3 (`exposeModels: true` on `.28`). After WS3.2 deploys and cloud3 no longer lists them, drop the
   three from `allowedModels` and `disabledModels` (a catalog refresh only adds, never removes).
   Record each revision.
2. **cloudlab `host/llama-swap.yaml`:** delete the `qwen3.8-27b`, `qwen3-coder-30b-a3b` and
   `qwen3.8-27b-w4a16` entries; set `hooks.on_startup.preload` per D1; update README (the cloud3
   table), the runbook, and any monitoring rule or test that names them
   (`kubernetes/apps/infrastructure/monitoring/cloud-vllm-rules*.yaml`); retire
   `scripts/cloud3-w4a16-gate.sh` with a one-line pointer in the runbook. Deploy as WS2.6.
3. **Files per D2**, after the deploy and a 24 h quiet period.

### WS4 — Validate the remaining models in llm-router (item 6)

For each remaining local model (`local/qwen3.8-27b-fp8`, `local/qwen3.8-flash-next`,
`local/qwen3.5-122b`, `local/qwen3.8-27b-ailab`, `local/qwen3.8-27b` → cloud2 if D3, route
`qwen3.8-27b`):
1. `/v1/models` lists exactly the intended set; the removed ids answer 400 `MODEL_NOT_SUPPORTED`.
2. Cold and warm chat completions with a quality prompt that does not trigger word counting (e.g.
   "Explain how a Fresnel lens concentrates light, in about 120 words") — visible content correct
   and coherent; one tool-call request (the Assistant needs tools).
3. **One real Assistant turn per model, pinned, starting COLD for each cloud3 model** (drive the
   chat API from the router pod as the admin principal) — no guided-mode fallback. Expected
   exceptions recorded, not hidden: a model whose 15k-token prefill alone exceeds the engine wait.
4. dsh: from the dsh pod, one request per model it lists; Open WebUI default
   (`qwen3.8-27b-vllm-cloud`) answers through both gateways.
5. Repo checks: `kubectl kustomize` on every touched overlay, `check-litellm-mirrored-routes.py`
   and its test, cloudlab rule tests.

### WS5 — Benchmarks again (item 7)

Commit the harness as `scripts/bench-router-local.mjs` (today's `bench.mjs`: runs inside the
router pod, never sends the router admin token to an upstream), with the quality prompt replacing
the 250-word one. Per model: cold, two warm, 9.3k prefill, a 15k Assistant-sized prompt (TTFT),
and decode at 15k depth. Loads from the llama-swap journals. Publish the before/after table in the
llm-router runbook and the cloudlab README; update the memory note.

## Critical files

- llm-router: `packages/plugins/agent-llm/loop.ts` (+ tests under `tests/`), the agent status text
- ailab: `kubernetes/apps/apps/ai/litellm.yaml`, `litellm-local.yaml`, `open-webui.yaml`,
  `kubernetes/apps/apps/dsh/settings.seed.yaml`, `kubernetes/apps/apps/llm-router/router.yaml`
  (digest bump), `docs/runbooks/llm-router.md`, `docs/runbooks/model-registration.md`,
  `scripts/bench-router-local.mjs` (new)
- cloudlab: `host/llama-swap.yaml`, `host/build-llama-cpp.sh` (new), `README.md`,
  `docs/runbooks/cloud-gpu-cluster.md`, `kubernetes/apps/infrastructure/monitoring/cloud-vllm-rules*.yaml`
- router runtime config (no git source; changed through the admin API, revisions recorded)

## Rollout order and rollback

1. WS0 (ailab PR) → 2. WS3.1 router disable + D3 → 3. WS2.1 diagnosis → 4. WS3.2 + WS2 cloudlab
PRs (one for removals + preload, one for the llama.cpp build + flash-next) → 5. WS1.2 config →
6. WS1.1 llm-router PR + release → 7. WS4 → 8. WS5 → 9. WS3.3 file deletion.
Rollback: LiteLLM/dsh/Open WebUI by git revert; router config by reverting to the recorded
revision (`/admin/v1/config/revisions/:n`); llama-swap from the on-box backup; flash-next back to
b10857 by one line; the router image by the previous digest (release recipe).

## Risks

- dsh sessions pinned to a removed model fail with `MODEL_NOT_SUPPORTED` until the user picks
  another — mitigated by WS0 landing first.
- `qwen3.8-27b-vllm-cloud` loses its second deployment; cloud2 is an on-demand host (D5).
- `--no-mmap` adds ~27 GiB of anon memory on a host with ~49 GiB available; gate it on PSI.
- A newer llama.cpp may change flags or GGUF handling; the side-by-side gate and the b10857
  rollback cover it.
- Cloud3 is opportunistic (daytime); schedule cloud3 steps while it is up.

## Out of scope

The w4a16 root cause (the model is being removed); `qwen3.8-27b-ailab`'s speed (D4); showing
reasoning in the Assistant; moving `qwen3.5-122b` to local NVMe (no space).

## Verification

WS4 and WS5 are the verification: every remaining model answers correctly through the router,
cold and warm; an Assistant turn on each completes (or its exception is recorded with the measured
reason); the removed ids are gone from the router, LiteLLM, Open WebUI and dsh; flash-next meets
the WS2.3 bar; and the new benchmark table is published next to today's.

<!-- codex-review-status: pending -->
