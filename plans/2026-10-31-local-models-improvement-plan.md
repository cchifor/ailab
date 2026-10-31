# Local models: fix the timeouts, speed up flash-next, prune cloud3, re-validate

## Codex Review

- The observed 90-second cancellation supports fixing the Assistant timeout, and withdrawing consumers before backends is the right dependency.
- Blocking omissions: the existing ten-minute whole-turn cap, dsh’s route/model distinction, migration of llama-swap’s `default` alias, and incorrect post-removal error expectations.
- The flash-next diagnosis remains a hypothesis; historical measurements are not directly comparable, and the proposed RAM footprint needs verification.
- Rollout must gate on deployed consumer changes, preserve actual rollback assets, and avoid making the timeout fix depend on optional performance experiments.
- Verification needs representative tool round trips, public client paths, cancellation, serving-account evidence, and explicit failure criteria.

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
   <!-- codex: The historical ~860 figure is inferred from a 30k prompt's 35 s TTFT, not an isolated prompt-evaluation measurement (cloudlab gitea/main:docs/runbooks/cloud-gpu-cluster.md:1948,1960), and the historical context setting differs from today's 524288 (host/llama-swap.yaml:163). Treat the regression magnitude and page-cache explanation as hypotheses until the same harness, flags, prompt lengths and cache states are compared; a row lookup is not necessarily a major fault or physical NVMe read. -->

## Decisions for the owner (recommendations in bold)

- **D1. cloud3's default (preload) once w4a16 is gone:** **`qwen3.8-flash-next`** (dsh's
  `agent-default-model`, ~62 s load, the model this plan speeds up), or `qwen3.8-27b-fp8` (183 s
  vLLM start), or no preload.
  <!-- codex: Flash-next currently has a 1800 s idle TTL, and startup preload does not restore it after another model evicts it (cloudlab gitea/main:host/llama-swap.yaml:167,203). Make the recommendation conditional on its qualification result and state the intended residency behavior; preload alone does not remove normal daytime cold starts. -->
- **D2. The removed models' files:** delete `/models-local/vllm/models/Qwen3.8-27B-W4A16-AutoRound-fast`
  (15 GiB on the 84%-full local NVMe — the space is needed for the llama.cpp work); **keep the two
  NFS GGUFs (`qwen3-coder-30b-a3b`, `qwen3.8-27b` Q6_K) for 30 days, unserved, then delete**.
  <!-- codex: The claimed build-space dependency is unbudgeted despite 62 GiB already free, and WS3.3 deletes these files only after the build and validation finish. Measure build/staging requirements first, then resolve this ordering contradiction without deleting rollback assets prematurely. -->
- **D3. A fast 27B in the picker:** after `local/qwen3.8-27b` (cloud3 Q6_K) goes, **set
  `exposeModels: true` on `lan-192.168.0.28-18020`** so `local/qwen3.8-27b` resolves to cloud2's
  vLLM copy (~160 tok/s, always loaded). The bare name `qwen3.8-27b` already resolves to the cloud2
  route and is unaffected either way.
  <!-- codex: Exposure applies to the account's entire enabled catalog, and provider/model requests bypass the configured bare-name route's policy (llm-router/packages/modules/routing/index.ts:31,69,108). Inspect the catalog and effective direct-request policy before enabling it, and verify the serving account rather than treating the preserved name as proof of equivalent behavior. -->
- **D4. `qwen3.8-27b-ailab` (17–22 tok/s, 162 tok/s prefill)** fails the same "too slow" bar but
  is NOT on the owner's list: **keep it (it is the LiteLLM fallback and dsh lists it); out of
  scope here**.
- **D5. `qwen3.8-27b-vllm-cloud` becomes cloud2-only** (no second deployment). Accept: cloud3's
  half is serving garbage today, and the gateway's fallback chain stays.
  <!-- codex: This fallback assurance applies only to the main gateway: litellm-local explicitly has no retry/fallback configuration (ailab/kubernetes/apps/apps/ai/litellm-local.yaml:109). Record the diagnostic gateway's cloud2-down behavior separately instead of implying both gateways retain a fallback. -->

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
   <!-- codex: Generate this list and the main LiteLLM checksum with the existing consumer generator rather than editing them independently; these are owned generated spans (ailab/scripts/gen-litellm-consumers.py:18,30). The generator does not stamp litellm-local's checksum, which still needs its documented derivation and verification. -->
3. `kubernetes/apps/apps/dsh/settings.seed.yaml` `llm-router-local.models`: drop
   `qwen3.8-27b-w4a16` and `qwen3-coder-30b-a3b` (and `qwen3.8-27b` only if D3 is declined — with D3
   it now means cloud2, which is what dsh's comment already intends). The provider block is
   re-spliced into the live settings on every pod start (`reconcile-provider.js`), so the rollout
   that the ConfigMap change triggers updates the live dsh.
   <!-- codex: Do not remove dsh's bare `qwen3.8-27b` when D3 is declined: it already selects the cloud2 route independently of direct model exposure (ailab/kubernetes/apps/apps/dsh/settings.seed.yaml:135,149; llm-router/packages/modules/routing/index.ts:108). D3 changes the Assistant's provider/model picker entry, not this dsh route. -->
4. Sweep every remaining consumer before merging: `git grep` across ailab, cloudlab, and the
   cchifor/platform and agentforge* checkouts for `qwen3-coder-30b-a3b`, `qwen3.8-27b-w4a16`,
   `w4a16`, `qwen3.8-27b-cloud`, `.26:8080` (`scripts/gen-litellm-consumers.py`, Gatus
   `configmap.yaml`, `docs/runbooks/model-registration.md`, `litellm-vkeys.yaml`, ADR 0039 text).
   Update docs; do not rewrite `plans/` (historical).
   <!-- codex: Use cloudlab's gitea/main for this sweep and base its implementation checkout on current main; its existing working tree is an old branch. Classify matches before changing them: `.26:8080` remains a valid backend for the retained models, and historical measurements should remain attributable to their original configuration. -->

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
   <!-- codex: Blocking: agent-core already imposes a ten-minute cap on the ENTIRE turn, including subsequent model/tool steps (llm-router/packages/plugins/agent-core/index.ts:31,707), and loop.ts:183 treats its expiry like Stop. Define the relationship between host turn limits and step limits, avoid a redundant timer, and test a cold multi-step tool round trip plus deadline expiry separately from explicit cancellation. -->
   <!-- codex: A heartbeat-only or reasoning-only upstream can keep the engine alive without visible progress; the existing tests deliberately demonstrate this beyond ten minutes (llm-router/tests/stream-timeouts.test.ts:85). Cover that case, a genuinely silent wait, a post-output idle stall, and upstream/account cleanup after Stop, rather than testing only a delayed first event. -->
   <!-- codex: Reuse the existing working-label channel (llm-router/packages/plugins/agent-llm/loop.ts:152,168) and describe waiting for an answer, since suppressed reasoning is indistinguishable from loading at this layer. Specify timer cleanup on text, tools, finish, errors and cancellation so a late status update cannot overwrite a completed step. -->
2. **Engine wait for slow cold loads (config, no release).** Set the router config's
   `defaults.timeouts.waitMs` to 420 000 so a cold 122B (~325 s) is not cut off. It is router-wide
   (direct-model requests take the defaults); the cost is that a hung Codex or Claude upstream takes
   7 min instead of 5 to fail over. Alternative if that is unacceptable: leave 300 s and accept that
   only the FIRST cold 122B request fails. Record the revision in the llm-router runbook.
   <!-- codex: Budget for unloading the previous model, queueing, loading and prefill before accepting 420 s; cloud3 permits a 120 s vLLM unload and documents a historical 367 s 122B cold load (cloudlab gitea/main:host/llama-swap.yaml:44,387). Test a busy-model-to-122B transition, and replace “only the FIRST” with every applicable cold start after eviction, restart or TTL expiry. -->
   <!-- codex: Explicit route timeouts override defaults, so inspect actual Codex/Claude policies before asserting their failover changes (llm-router/packages/modules/routing/index.ts:23). Preserving their current limits with explicit overrides is an existing-config alternative to accepting broken cold requests; a local named-route override alone would not fix pinned Assistant requests, which use direct model resolution (packages/plugins/agent-llm/index.ts:88). -->

### WS2 — flash-next and llama.cpp on cloud3 (cloudlab PRs)

1. **Confirm the cause first (read-only, ~30 min).** During a 9.3k-token prefill on
   flash-next: sample llama-server `majflt` (`/proc/<pid>/stat`), `iostat -x 1` on the nvmedata
   device, `/proc/pressure/{memory,io}`, the LXC cgroup `memory.stat` `file`, and `/slots`
   (`timings.cache_n`). Repeat the same prompt with `cache_prompt:false`: a much faster second run
   means the page cache, not the kernels. Also measure decode at 15k depth.
   <!-- codex: A faster repeat is not sufficient to isolate page cache from prompt reuse, GPU warmup or contention; collect matched backend timings and cache evidence for both runs. `cache_prompt` is rejected by the router's request parser, so use a documented backend diagnostic path for that control (llm-router/packages/modules/protocol/index.ts:25,30). -->
   <!-- codex: These probes load and potentially evict a production model even though configuration remains unchanged; reserve the shared GPU service and define cold-process versus cold-file-cache runs. Avoid host-wide cache dropping on the VM host, and correlate host/cgroup memory pressure and memory events with process faults rather than treating one counter as proof. -->
2. **Codify the llama.cpp build** (none exists in git; b10857 was hand-built). Add
   `host/build-llama-cpp.sh <tag>` to cloudlab: clone the pinned tag, `cmake -DGGML_CUDA=ON
   -DCMAKE_CUDA_ARCHITECTURES=86`, build into `/models-local/llama-<tag>/`, write the source sha
   next to the binary. Build b12020 (or the newest tag at implementation time) BESIDE b10857;
   confirm it builds against cloud3's CUDA 12.4 / driver 550.
   <!-- codex: Freeze one reviewed tag and resolved commit before qualification instead of silently selecting a newer release during implementation. Record the compiler/toolkit and build options, bound build parallelism on the VM host, and verify the installed binary's library resolution and an actual CUDA model load; the existing separation relies on rpath and ldd checks (cloudlab gitea/main:host/llama-swap.yaml:135). -->
3. **Gate the new build** with a temporary llama-swap entry (`qwen3.8-flash-next-next`) on the same
   GGUF and flags, then variants: `--lazy-mode auto` (prefetch), `-ub 2048 -b 4096`, and — only if
   WS2.1 shows page faults dominate and host memory allows — `--lazy-mode off --no-mmap` (~27 GiB
   of LXC anon memory while resident; check host `available` and PSI with the CI VMs busy). Pass
   bar: prefill ≥ 600 tok/s at 9.3k and at 15k, decode ≥ 55 tok/s, the quality prompt set (below)
   correct, per-card VRAM within the current headroom, no sustained memory PSI. Run the gate with
   llama-swap holding nothing else.
   <!-- codex: The ~27 GiB anonymous-memory estimate is unverified: disabling lazy loading changes tensor residency, and steady-state table size does not bound loading buffers, page-cache overlap or peak host/VRAM use. Establish placement and abort thresholds before this variant; the source model is 83.80 GiB and earlier runs reached the 160 GiB cgroup ceiling (cloudlab gitea/main:docs/runbooks/cloud-gpu-cluster.md:1923,1957). -->
   <!-- codex: A temporary entry in the production catalog can become a permanently advertised router model because refresh adds enabled entries and never removes them (llm-router/packages/plugins/provider-registry/models-api.ts:73). Keep it unexposed during qualification and explicitly remove any temporary router catalog entry after deleting it from llama-swap. -->
   <!-- codex: Specify what happens if the build is correct and materially faster but misses 600/55; the cited prefetch result does not establish either threshold on this host, particularly decode. Keep batch-size tuning as a separately measured variable so a failed optimization does not strand the removals or timeout fix. -->
4. **MTP is optional and gated separately.** Unsloth's page builds it from a custom branch
   (`danielhanchen/llama.cpp` `qwen4exp/mtp`), not mainline. Enable `--spec-type draft-mtp` with
   `MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf` (2.79 GB) ONLY if the mainline build's
   `llama-server --help` offers it for this architecture; never deploy a personal fork.
   <!-- codex: A generic help flag does not prove support for this architecture, draft format or checkpoint pairing; require a successful compatible draft load, acceptance statistics and a correctness/performance comparison. If that support is absent, skip MTP explicitly rather than making branch investigation another prerequisite for the requested upgrade. -->
5. **Re-download only if needed.** If the new build refuses the existing UD-Q3_K_XL shards, stage the
   new ones on the NFS `/models` (4.4 TiB free), verify `sha256` against the HF metadata into a
   `host/*.SHA256SUMS` file, then swap into `/models-local` (62 GiB free + 15 GiB from D2 does
   not fit a second 84 GiB copy side by side).
   <!-- codex: This compatibility branch must precede completion of WS2.3, and replacement weights must restart qualification because the binary is no longer the only changed variable. Pin the HF revision and retain a verified NFS copy of the old shards and projector before removing local files; copying across filesystems is not an atomic swap, and keeping b10857 alone cannot restore its old inputs. -->
6. **Cut over:** point the `qwen3.8-flash-next` entry at the new binary and winning flags, delete the
   temporary entry, keep `/models-local/llama-b10857` for a 14-day rollback window. Deploy by the
   runbook procedure (timestamped on-box backup, `pct push 5101 … /etc/llama-swap.yaml`, restart
   llama-swap at a quiet moment), then confirm the repo and the box are byte-identical (`sha256sum`).
   <!-- codex: File equality proves deployment of configuration, not successful execution: verify the running binary/version, selected flags, model health and a real completion after restart. Include an actual rollback smoke test, and account for startup preload before assuming the GPUs are empty for a subsequent gate. -->

### WS3 — Remove the three cloud3 models (items 3, 4, 5)

1. **Router (runtime config via the admin API, after WS0 merges):** on `lan-192.168.0.26-8080`,
   add `qwen3.8-27b` and `qwen3-coder-30b-a3b` to `disabledModels` (w4a16 is already there). Apply
   D3 (`exposeModels: true` on `.28`). After WS3.2 deploys and cloud3 no longer lists them, drop the
   three from `allowedModels` and `disabledModels` (a catalog refresh only adds, never removes).
   Record each revision.
   <!-- codex: Snapshot and inspect runtime route deployments, replacement-model settings and capability lists as well as catalogs; a git sweep cannot find these SQLite-owned references (llm-router/packages/contracts/index.ts:45,54,66,79). In particular, a configured disabledModelFallback can silently serve another model instead of refusing the removed one (packages/modules/routing/index.ts:55). -->
   <!-- codex: Apply cloud3 disabling and cloud2 exposure atomically against the current revision so the reused direct name has no unintended transition state. The full-config API supports this through PUT with If-Match, which also prevents overwriting concurrent administrator changes (llm-router/packages/plugins/api-admin/index.ts:29). -->
2. **cloudlab `host/llama-swap.yaml`:** delete the `qwen3.8-27b`, `qwen3-coder-30b-a3b` and
   `qwen3.8-27b-w4a16` entries; set `hooks.on_startup.preload` per D1; update README (the cloud3
   table), the runbook, and any monitoring rule or test that names them
   (`kubernetes/apps/infrastructure/monitoring/cloud-vllm-rules*.yaml`); retire
   `scripts/cloud3-w4a16-gate.sh` with a one-line pointer in the runbook. Deploy as WS2.6.
   <!-- codex: Blocking: changing preload does not move the `default` alias, which is currently attached to the deleted w4a16 entry (cloudlab gitea/main:host/llama-swap.yaml:297). Explicitly assign or retire that alias, retain useModelName rewriting if FP8 receives it (host/llama-swap.yaml:388), and test both the canonical model name and `default`. -->
3. **Files per D2**, after the deploy and a 24 h quiet period.
   <!-- codex: Distinguish the 24-hour w4a16 deletion from the promised 30-day NFS retention, with dated cleanup ownership and a last-use/reference check across hosts. Removing a model from cloud3 does not establish that its shared NFS file is unused elsewhere, and deleting it closes the corresponding rollback path. -->

### WS4 — Validate the remaining models in llm-router (item 6)

For each remaining local model (`local/qwen3.8-27b-fp8`, `local/qwen3.8-flash-next`,
`local/qwen3.5-122b`, `local/qwen3.8-27b-ailab`, `local/qwen3.8-27b` → cloud2 if D3, route
`qwen3.8-27b`):
1. `/v1/models` lists exactly the intended set; the removed ids answer 400 `MODEL_NOT_SUPPORTED`.
   <!-- codex: The expected error changes with lifecycle: known disabled models without replacement return 400 MODEL_NOT_SUPPORTED, but names removed from all catalogs/routes return 404 MODEL_NOT_FOUND (llm-router/packages/modules/routing/index.ts:65,120). Also exclude the retained cloud2 route and, if D3 is accepted, the intentionally reused `local/qwen3.8-27b` from negative assertions. -->
2. Cold and warm chat completions with a quality prompt that does not trigger word counting (e.g.
   "Explain how a Fresnel lens concentrates light, in about 120 words") — visible content correct
   and coherent; one tool-call request (the Assistant needs tools).
   <!-- codex: Define a small fixed correctness set and require a complete tool-call → tool-result → final-answer exchange with valid arguments, not merely emitted tool syntax. Check nonempty final content and finish reason with an adequate reasoning/output budget; a single prose answer cannot establish that HTTP-200 degeneration or tool-template regressions are absent. -->
   <!-- codex: The binary upgrade also affects the projector and two-slot configuration (cloudlab gitea/main:host/llama-swap.yaml:160,163), neither exercised by a serial text probe. Add an image probe where vision is advertised and a bounded two-request/swap-transition check to catch VRAM peaks, queueing and eviction interactions. -->
3. **One real Assistant turn per model, pinned, starting COLD for each cloud3 model** (drive the
   chat API from the router pod as the admin principal) — no guided-mode fallback. Expected
   exceptions recorded, not hidden: a model whose 15k-token prefill alone exceeds the engine wait.
   <!-- codex: Explicitly unload between the preceding warm checks and this cold test, then record actual input tokens, serving account, elapsed time and the completed tool round trip from the trajectory. A short fresh conversation does not reproduce the measured 13–15.5k workload, and the bare cloud2 route is not itself a pinnable catalog model when D3 is declined (llm-router/packages/plugins/agent-core/index.ts:119,123). -->
   <!-- codex: Use a dedicated test conversation with automatic application disabled and a harmless read-only tool task; an admin Assistant can apply model-proposed changes in auto mode (llm-router/packages/plugins/agent-core/index.ts:1303). Assert no configuration mutation occurred and keep credentials and sensitive conversation/configuration payloads out of committed benchmark output. -->
4. dsh: from the dsh pod, one request per model it lists; Open WebUI default
   (`qwen3.8-27b-vllm-cloud`) answers through both gateways.
   <!-- codex: Exercise the public URL with the actual client credential scopes and streaming behavior, plus browser Assistant Stop/reconnect; localhost admin probes bypass authorization restrictions, ingress and client deadlines. Nonstreaming cold requests can still exceed Cloudflare's roughly 100–125 s limit despite the router change (ailab/docs/runbooks/llm-router.md:114). -->
   <!-- codex: A successful response can conceal fallback to ailab, so assert the intended serving backend while cloud2 is up and separately test the main gateway's cloud2-down fallback. Verify litellm-local's expected failure separately, since gateway-level fallback behavior is deliberately not mirrored (ailab/scripts/check-litellm-mirrored-routes.py:22). -->
5. Repo checks: `kubectl kustomize` on every touched overlay, `check-litellm-mirrored-routes.py`
   and its test, cloudlab rule tests.
   <!-- codex: Run repository checks before each merge/deploy, not only at WS4, including consumer generation, inline hashes, vkey references, dsh model wiring and the existing manifest schema checks (ailab/.gitea/workflows/manifests.yaml:56,69,237,250; scripts/tests/test_dsh_router_models.py:134). Rendering alone does not validate generated cross-file contracts or the live rollout. -->

### WS5 — Benchmarks again (item 7)

Commit the harness as `scripts/bench-router-local.mjs` (today's `bench.mjs`: runs inside the
router pod, never sends the router admin token to an upstream), with the quality prompt replacing
the 250-word one. Per model: cold, two warm, 9.3k prefill, a 15k Assistant-sized prompt (TTFT),
and decode at 15k depth. Loads from the llama-swap journals. Publish the before/after table in the
llm-router runbook and the cloudlab README; update the memory note.
<!-- codex: Freeze and run this harness against b10857 before tuning or replacing weights; changing the prompt only after deployment leaves no comparable baseline. Record exact model/build/config revisions, sampling and output limits, actual token counts, cache state and run spread, separating process-cold from storage-cold measurements. -->
<!-- codex: Define TTFT as first usable content/tool output, excluding SSE keepalives, and distinguish it from upstream first-token time and server prompt-evaluation time. Decode measured from visible deltas omits suppressed reasoning, while usage may include it (llm-router/packages/contracts/index.ts:214); use backend timings for compute throughput and report client-visible latency separately. -->
<!-- codex: Use a short-lived inference-scoped credential for completion benchmarks and isolate any admin-only trajectory lookup, with bounded requests and cleanup. Do not commit a harness that depends on unrestricted pod credentials or retains production prompts merely because it avoids forwarding the admin token upstream. -->

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
<!-- codex: Replace “WS0 merges” with observed Flux reconciliation, completed rollouts and live consumer checks before disabling/removing backends; a merged manifest can still be unapplied or leave old replicas serving traffic. The dsh content hash does trigger a rollout (ailab/kubernetes/apps/apps/dsh/kustomization.yaml:39), but that is not evidence the rollout succeeded. -->
<!-- codex: Ship and verify the timeout correction independently of the optional llama.cpp experiments, and capture the benchmark baseline before either build or weight replacement. Split WS3.1 into pre-removal disabling/exposure and post-deploy catalog cleanup; the latter cannot occur at step 2, and each cloudlab PR needs its own deployment gate rather than the combined step 4. -->
Rollback: LiteLLM/dsh/Open WebUI by git revert; router config by reverting to the recorded
revision (`/admin/v1/config/revisions/:n`); llama-swap from the on-box backup; flash-next back to
b10857 by one line; the router image by the previous digest (release recipe).
<!-- codex: The revisions URL is GET-only; restoration requires a mutation, and the full-config PUT requires the current If-Match revision (llm-router/packages/plugins/api-admin/index.ts:29,35). Prefer reversing this plan's fields against current state, or explicitly reconcile intervening changes before restoring a whole historical snapshot. -->
<!-- codex: Rollback order must restore and verify required files/backends before re-enabling router entries and consumers, and must not automatically reintroduce the known-degenerate w4a16 deployment. A one-line binary rollback is insufficient if flags or GGUF/projector files changed; retain and restore the complete compatible configuration and assets. -->

## Risks

- dsh sessions pinned to a removed model fail with `MODEL_NOT_SUPPORTED` until the user picks
  another — mitigated by WS0 landing first.
  <!-- codex: Updating the provider list does not migrate existing sessions or a user-selected default within the still-active llm-router-local provider; the default reconciler only handles retired providers (ailab/kubernetes/apps/apps/dsh/reconcile-default-model.js:1). Specify recovery for both saved defaults and old sessions, and expect MODEL_NOT_FOUND after catalog pruning rather than always MODEL_NOT_SUPPORTED. -->
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
<!-- codex: Recording an exception does not satisfy “every remaining model works”; define pass/fail outcomes and the explicit disposition of a retained model that still cannot complete a representative Assistant turn. Before acceptance, repeat a bounded warm/cold check after normal VM activity and an idle/eviction cycle so a briefly warmed page cache cannot masquerade as a durable performance fix. -->

<!-- codex-review-status: complete -->