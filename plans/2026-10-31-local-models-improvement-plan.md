# Local models: fix the timeouts, speed up flash-next, prune cloud3, re-validate

## Context

The owner asked (2026-10-31) for seven things: fix the timeout issues, improve `qwen3.8-flash-next`
(update llama.cpp), remove `local/qwen3.8-27b` (32 tok/s is not enough), remove
`qwen3-coder-30b-a3b` (older and weaker), remove `qwen3.8-27b-w4a16`, validate that the remaining
models work in llm-router, and re-run the benchmarks.

Measured the same day, through the router (`kubectl exec` into the router pod, `127.0.0.1:8787`,
the engine the Assistant and the public API use). Load = request start to llama-swap's
"Health check passed". Decode and prefill are wall-clock over `usage` token counts (reasoning
included), not backend compute timings:

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

`qwen3.8-27b-w4a16` (cloud3's preloaded default, and the holder of llama-swap's `default` alias,
`host/llama-swap.yaml:297`) answers HTTP 200 with degenerate text ("lăn lăn…", 8/8 replies). It was
turned off in the router at config rev 2093 (`disabledModels` on `lan-192.168.0.26-8080`), but it is
still the cloud3 half of LiteLLM's `qwen3.8-27b-vllm-cloud` (`litellm.yaml` and
`litellm-local.yaml`), which is Open WebUI's default, and dsh still lists it.

### Root causes

1. **Assistant "The model route … failed (CANCELLED)" — verified.** llm-router
   `packages/plugins/agent-llm/loop.ts:15` `STEP_TIMEOUT_MS = 90_000` caps each model step at 90 s
   wall-clock, request to end of stream. The failing trajectory (`3853a810…`, config rev 2096) shows
   `client.closed` at 89.99 s, cause `CLIENT_CLOSED`, while cloud3 was loading `qwen3.8-27b`. An
   Assistant step carries 13–15.5k input tokens (recent Codex steps), so a model whose prefill is
   under ~170 tok/s (flash-next today, `.45`) cannot fit 90 s even warm. Two looser bounds already
   exist: the engine's wait 300 s before any output and idle 120 s after, reset by raw upstream bytes
   (`packages/modules/routing/index.ts:20`), and the agent's 10-minute cap on the whole turn
   (`packages/plugins/agent-core/index.ts:31,707`), whose expiry `loop.ts:183` treats like Stop.
2. **A cold `qwen3.5-122b` through the API times out — verified.** Its load (~325 s today, 367 s
   recorded at `host/llama-swap.yaml:387`, plus up to 120 s to unload a vLLM model first) exceeds the
   engine's 300 s wait; the router answers `TIMEOUT` (phase wait). llama-swap keeps loading, so the
   next request works — every cold start after eviction, restart or TTL expiry repeats this.
   Separately, public NON-streaming requests are cut by Cloudflare's edge at ~100–125 s
   (`docs/runbooks/llm-router.md:114`) whatever the router allows; streaming requests get early SSE
   headers and 15 s keepalives.
3. **flash-next prefill is far below what it was — hypothesis, to be confirmed in WS2.1.** The
   2026-09-11 runbook figure (~860 tok/s) is a 30k prompt's 35 s TTFT on a different context setting,
   not an isolated prompt-evaluation measurement, so the size of the regression is not yet
   established. The working explanation: the 26.82 GiB n-gram table (`per_layer_token_embd`) is
   never in VRAM — b10857's `--lazy-mode auto` reads rows on demand through mmap (runbook
   "Flash-Next at UD-Q6_K_XL") — and with 8 VMs on cloud3 (184 GiB assigned, ~49 GiB available) the
   table no longer stays in page cache. Cold load also went from 13.7 s (09-08) to ~62 s. Upstream
   llama.cpp PR #29599 (`llama_prefetch_rows`, merged 2026-09-30) adds madvise prefetch of these rows
   in lazy mode for this architecture (Qwen4Exp); its CUDA reviewer numbers: 467.5 → 826.9 t/s
   prefill. Latest release: b12020 (2026-10-31).

## Decisions for the owner (recommendations in bold)

- **D1. cloud3 after w4a16:** **retire the `default` alias** (a sweep of ailab and cloudlab found no
  client that requests `default`; re-check in WS0.3; `.26`'s router catalog has no `default` entry
  today) with **no preload in the interim** (WS3.2), then **preload `qwen3.8-flash-next` with the old
  default's idle TTL (86400 s) only at WS2.7, once it has qualified**. Preload happens at boot only:
  any request for another cloud3 model evicts it, and it stays cold until next asked or next boot.
  Alternatives: preload `qwen3.8-27b-fp8` (183 s vLLM start), or nothing at all.
- **D2. Files:** **delete `/models-local/vllm/models/Qwen3.8-27B-W4A16-AutoRound-fast` after WS4
  passes** (hygiene; it is not a prerequisite — 62 GiB is already free and a llama.cpp build needs a
  few GiB); **keep the NFS GGUFs of `qwen3-coder-30b-a3b` and `qwen3.8-27b` Q6_K unserved until
  2026-12-01**, then delete after checking no other host references them (Unsloth no longer
  publishes that plain Q6_K, so deletion ends the rollback path).
- **D3. A fast 27B in the picker:** **set `exposeModels: true` on `lan-192.168.0.28-18020`** in the
  same atomic change that disables cloud3's `qwen3.8-27b`, so `local/qwen3.8-27b` resolves to cloud2's
  vLLM (~160 tok/s, always loaded). That account's catalog is only `qwen3.8-27b`, so nothing else
  becomes visible. The bare name `qwen3.8-27b` is the cloud2 ROUTE either way (dsh's worker and the
  Conductor use it, `kubernetes/apps/apps/dsh/conductor.runtime.json:20`) and is unaffected.
- **D4. `qwen3.8-27b-ailab`** (17–22 tok/s, 162 tok/s prefill) is slow but not on the owner's list:
  **keep it; out of scope**. With WS1 it should complete Assistant turns, slowly (~2–3 min a step).
- **D5. `qwen3.8-27b-vllm-cloud` becomes cloud2-only. Accept:** cloud3's half serves garbage today.
  The main gateway keeps its fallback chain; `litellm-local` has no retry/fallback by design
  (`litellm-local.yaml:109`), so its diagnostic copy fails while cloud2 is down — record that.

## Approach

Order matters: consumers stop naming a model before the router hides it, and the router hides it
before cloud3 deletes it. The timeout fix ships independently of the llama.cpp work.

### P0 — Freeze the benchmark harness and take a baseline (before any change)

Commit `scripts/bench-router-local.mjs` to ailab (from today's `bench.mjs`): it reads the router URL
and a router key from the environment, never the admin token, and sends no production prompts.
The key is an **all-routes `api-keys` key with a 7-day expiry, revoked at the end**. A key scoped to
named routes cannot make direct-model requests (`packages/modules/inference/index.ts:234`), and every
api-key principal has role `client`, which `/admin/` refuses (`packages/plugins/api-keys/keys.ts:162`).
It inherits the issuing admin's access, so it can reach subscription models too; the harness only
names local ids. It also has a **backend mode** (a server URL instead of the router) for WS2
qualification. Every request gets its own wall-clock deadline (900 s) with abort and stream cleanup
(a heartbeat- or reasoning-only response can keep the engine alive indefinitely,
`tests/stream-timeouts.test.ts:85`); a deadline hit is recorded as a result, not a hang. It records
per run: router config revision, backend build, flags, sampling, `max_tokens`, token counts, cache
state (process-cold / file-cold / warm), and the spread of three runs. Metrics:
- load (llama-swap journal), client time to first usable content or tool delta (SSE keepalives and
  hidden reasoning excluded), and backend compute rates (llama.cpp `timings`; vLLM metrics) kept
  separate from client-visible latency;
- prefill at 9.3k and at 15k tokens (the Assistant's size), decode at short and at 15k depth;
- a fixed quality set (below) instead of the "250 words" prompt, which sent Qwen3.8 into
  word-counting.
Run it once against the current state (b10857, current flags) for every model that stays.

### WS0 — Stop pointing at the removed models (ailab PR)

1. `kubernetes/apps/apps/ai/litellm.yaml` and `litellm-local.yaml`: delete the cloud3
   (`qwen3.8-27b-w4a16`, `.26:8080`) deployment of `qwen3.8-27b-vllm-cloud` from both blocks (they
   must stay in agreement, `scripts/check-litellm-mirrored-routes.py`), and delete the
   `qwen3-coder-30b-a3b-cloud` entry. Regenerate Open WebUI's `model_ids` and litellm.yaml's
   `checksum/config` with `scripts/gen-litellm-consumers.py` (it owns both spans); derive
   litellm-local's checksum by its documented recipe.
2. `kubernetes/apps/apps/dsh/settings.seed.yaml` `llm-router-local.models`: drop
   `qwen3.8-27b-w4a16` and `qwen3-coder-30b-a3b`; keep `qwen3.8-27b` (the cloud2 route). Run
   `scripts/tests/test_dsh_router_models.py` (it ties the five places a dsh model is named). Read the
   live dsh settings for a saved default or Conductor role naming a removed model; if one exists,
   extend `reconcile-default-model.js` to treat removed model ids like retired providers rather than
   hand-editing the PVC. Sessions already pinned to a removed model fail until their user picks
   another — say so in the PR.
3. Sweep before merging: `git grep` ailab, cloudlab **`gitea/main`**, and current cchifor/platform and
   agentforge* checkouts for `qwen3-coder-30b-a3b`, `qwen3.8-27b-w4a16`, `w4a16`, `default` aimed at
   cloud3, and `.26:8080`. Classify each match: `.26:8080` stays valid for the retained models, and
   historical measurements and `plans/` stay as written; update live config and current docs
   (`docs/runbooks/model-registration.md`, `litellm-vkeys.yaml`, Gatus `configmap.yaml`, ADR 0039's
   current-state text).
4. Gate before WS3: the repo checks the CI runs (`.gitea/workflows/manifests.yaml`: kustomize builds,
   inline hashes, the consumer generator in check mode, mirrored routes, dsh model tests), then after
   merge: Flux reconciled the commit, the litellm, litellm-local, open-webui and dsh rollouts
   completed, and the live objects show the change (LiteLLM model list, dsh live provider block).

### WS1 — Timeouts (llm-router PR + image release; independent of WS2)

1. **Remove the 90 s step timer** from `agent-llm/loop.ts`. A step then runs under the turn signal
   (`TURN_CAP_MS`, Stop) and the engine's wait/idle timers, which already end a dead or stalled
   upstream. Changes:
   - **raise `TURN_CAP_MS` from 10 to 15 minutes.** WS1.2's 600 s wait is the time one cold step
     may legitimately spend before its first byte (≤120 s vLLM unload + ~367 s 122B load + prefill);
     a 10-minute turn would leave ~113 s for every other step, prefill, reasoning and tool of that
     turn. Make the host deadline testable (agent-core builds it with native `AbortSignal.timeout`,
     `agent-core/index.ts:707`, which fake timers do not drive) by injecting the timer;
   - when the turn CAP (not the user) ends a step, show "The model did not finish within 15 minutes"
     exactly once instead of ending silently; an explicit Stop still ends the turn silently with no
     fallback (U13) and releases the request;
   - after 15 s with no event, set the existing working label (`t.working`, `loop.ts:152,168`) to
     "Waiting for <model>'s answer… Ns", refreshed every 5 s and cleared on any text, tool, usage,
     finish, error or abort, so a late tick cannot overwrite a finished step. It says "waiting", not
     "loading": hidden reasoning and a cold load look the same at this layer.
   Tests (vitest, fake timers): a step whose first event arrives at 150 s completes; a silent upstream
   ends through the engine wait and falls back to guided mode; a stall after output ends through the
   idle timer; a reasoning- or heartbeat-only upstream (`tests/stream-timeouts.test.ts:85` pattern)
   is bounded by the turn cap with the new message; the real host turn deadline (not only the loop
   fixture) produces that message exactly once; Stop during the wait aborts the upstream and
   releases the admission slot; a cold multi-step tool round trip completes; the label timer is
   cleared on every exit path. `loop.ts` is in the vendorable set: keep
   `scripts/check-architecture.mjs` and `tests/library-boundary.test.ts` passing and note the change
   for cchifor/platform's vendored copy (`scripts/sync-llm-router.mjs`).
2. **Per-account wait for slow cold loads (same PR).** Add optional `Account.timeouts { waitMs,
   idleMs }`, validated (1 s–30 min). It applies to direct-model (virtual) policies — which is how
   pinned Assistant requests and `local/<model>` API calls resolve (`agent-llm/index.ts:88`) — as the
   largest override among the served accounts, else `defaults`, else the built-ins. Named routes keep
   their own `timeoutMs` (the main Codex/Claude routes set 300 s explicitly today). The engine fixes
   the timeouts once, from the requested policy, for every attempt (`inference/index.ts:391`), so the
   largest override also governs a faster sibling's attempt; accepted, because every local model here
   is served by one account (`.45`/`.46` share a name, and `.46` is unexposed). Tests: mixed-account
   failover, a `waitMs`-only override, a disabled-model replacement, and named routes unchanged. Then
   set `lan-192.168.0.26-8080` to `waitMs: 600000` (≤120 s vLLM unload + ~367 s worst 122B NFS load
   + prefill). Rejected alternative: raising the router-wide `defaults.timeouts.waitMs` would also
   lengthen every direct subscription request (dsh asks for `opus` and `sonnet` directly).
3. **Public clients must stream for cold local models.** Document in `docs/runbooks/llm-router.md`
   and the router's agent skill that a non-streaming request through `router.chifor.me` is cut by
   Cloudflare at ~100–125 s; streaming survives through keepalives. WS4 confirms dsh streams.
4. Ship by the image release recipe (Relay CI build, ailab digest bump, `validate-live` against its
   known 6/9 baseline), with the codex review of the PR.

### WS2 — flash-next and llama.cpp on cloud3 (cloudlab PRs)

1. **Diagnose (no config change; ~30 min of reserved cloud3 time).** Announce the window; confirm
   nothing else is using cloud3. Send matched prompts DIRECT to llama-server (the router rejects
   `cache_prompt`): a process-cold run, then a warm run with `cache_prompt:false`. For each, collect
   backend `timings` (`prompt_ms`, `cache_n`), llama-server `majflt` (`/proc/<pid>/stat`), `iostat
   -x 1` on the nvmedata device, `/proc/pressure/{memory,io}`, and the LXC cgroup `memory.stat` and
   `memory.events`. Never `drop_caches` on the VM host. Output: the page-cache hypothesis confirmed or
   rejected, and the decode figure at 15k depth.
2. **Codify the build.** No build script exists in git (b10857 was hand-built). Add
   `host/build-llama-cpp.sh <tag> <commit>`: clone at the frozen tag and verify the commit, `cmake
   -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86`, `-j` bounded to 16 of cloud3's 48 cores, install to
   `/models-local/llama-<tag>/` with an rpath to its own libraries, and write the tag, commit, `nvcc`
   and compiler versions and cmake options beside the binary. Freeze **b12020** in the PR (no
   silent move to a newer tag). Check: `ldd` resolves to the new lib directory, `llama-server
   --version`, and a real CUDA model load on CUDA 12.4 / driver 550.
3. **Compatibility first.** If the new build refuses the current UD-Q3_K_XL shards or `mmproj`, do
   WS2.4 before any qualification; otherwise skip it.
4. **(Conditional) New weights.** Pin the HF revision; stage on NFS `/models` (4.4 TiB free);
   record `sha256` in `host/qwen3.8-flash-next.SHA256SUMS`; keep a verified NFS copy of the current
   shards and `mmproj` before touching `/models-local` (62 GiB free cannot hold two 84 GiB copies, so
   the local swap is copy-then-rename, not atomic). New weights restart WS2.5.
5. **Qualify, one variable at a time,** on a temporary llama-swap entry that never reaches the
   router: the P0 harness in backend mode talks to llama-swap directly; the entry is `unlisted` if
   llama-swap v250 supports that per-model option (check first); nobody refreshes the `.26` catalog
   during qualification (the health monitor only reports new server models,
   `upstream-health/monitor.ts:183`; an explicit refresh would import it enabled,
   `models-api.ts:73`); and after cut-over `.26`'s `allowedModels` is checked for the temporary name.
   Steps: (a) new build, current flags; (b) + `-ub 2048 -b 4096`; (c) only if WS2.1 showed faults
   dominate: `--lazy-mode off --no-mmap`, with measured placement (per-card VRAM, LXC anon and file
   peaks) and abort criteria watched during startup AND inference: the LXC's own cgroup (headroom
   under its 160 GiB `memory.max` below 16 GiB, or any new `high`/`max` in `memory.events`), host
   `MemAvailable` < 16 GiB, or memory PSI `some avg10` > 10. Each step runs the P0 harness, the
   quality set, a vision probe (flash-next has `mmproj`) and two concurrent requests.
   **Target:** prefill ≥ 600 tok/s at 9.3k and 15k, decode ≥ 55 tok/s. **Minimum to ship:** prefill
   at least 1.5× the P0 baseline median at BOTH 9.3k and 15k, outside the baseline's three-run spread,
   with matched cache states; decode no worse than −5 % at short AND at 15k depth, each measured
   separately; every quality, vision and concurrency check passing. Meeting the minimum but not the
   target ships, with the gap recorded as a follow-up; missing the minimum keeps b10857.
6. **MTP is optional.** Only if the frozen mainline build loads Unsloth's MTP draft
   (`MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf`, 2.79 GB) for this checkpoint, reports acceptance
   statistics and passes the quality comparison; Unsloth documents it on a custom branch
   (`danielhanchen/llama.cpp` `qwen4exp/mtp`), which is never deployed. Otherwise skip and record.
7. **Cut over** in its own cloudlab PR and deploy gate: point `qwen3.8-flash-next` at the new
   binary, flags and (if changed) weights; apply D1's flash-next preload and TTL now that it has
   qualified; delete the temporary entry; deploy by the runbook
   (timestamped on-box backup, `pct push 5101 … /etc/llama-swap.yaml`, restart at a quiet moment);
   verify the running binary (`/proc/<pid>/exe`, the version in the log), the flags, health and a real
   completion; then a rollback smoke (switch to b10857 with its flags and assets, verify, switch
   back). Keep b10857, its config block and the old assets for 14 days.

### WS3 — Remove the three cloud3 models (items 3, 4, 5)

1. **Router, before cloud3 changes (after the WS0 gate).** Snapshot the config. In ONE
   `PUT /admin/v1/config` with `If-Match` (no partial state, no lost concurrent edit): add
   `qwen3.8-27b` and `qwen3-coder-30b-a3b` to `.26`'s `disabledModels` (w4a16 is already there), and
   D3's `exposeModels: true` on `.28`. Confirm first that no route deployment, `disabledModelFallback`
   (none on `.26` today) or capability list names the three. Verify with a request that
   `local/qwen3.8-27b` is served by `lan-192.168.0.28-18020` (trajectory `accountId`).
2. **cloudlab `host/llama-swap.yaml` (removal PR):** delete the `qwen3.8-27b`,
   `qwen3-coder-30b-a3b` and `qwen3.8-27b-w4a16` entries; apply D1's interim state (the `default`
   alias retired, no preload — flash-next's preload waits for WS2.7);
   update README's cloud3 table, the runbook, any monitoring rule or rule test naming them, and retire
   `scripts/cloud3-w4a16-gate.sh` with a pointer in the runbook. Deploy (WS2.7 procedure) and verify
   `/v1/models`, `/running` and a completion on each retained model.
3. **Router, after that deploy:** drop the three from `.26`'s `allowedModels` and `disabledModels`
   (one If-Match PUT). The retired request ids are exactly `qwen3-coder-30b-a3b`,
   `local/qwen3-coder-30b-a3b`, `qwen3.8-27b-w4a16` and `local/qwen3.8-27b-w4a16`: they now answer
   404 `MODEL_NOT_FOUND` (before this step: 400 `MODEL_NOT_SUPPORTED`). `local/qwen3.8-27b` is NOT
   retired (D3: it is now cloud2), nor is the bare `qwen3.8-27b` route; on cloud3 itself, `default`
   and the three names answer 404 from llama-swap.
4. **Files per D2.**

### WS4 — Validate the remaining models (item 6)

Models: `local/qwen3.8-27b-fp8`, `local/qwen3.8-flash-next`, `local/qwen3.5-122b`,
`local/qwen3.8-27b-ailab`, `local/qwen3.8-27b` (cloud2, D3), and the route `qwen3.8-27b`.
Inference checks go through the PUBLIC URL with the P0 key. Assistant checks need a management
session, which an api-key cannot give: they use the router's admin bearer from INSIDE the pod (it
never leaves the pod and is never given to the harness), which also reads trajectories.
1. **Catalog:** `/v1/models` is exactly the intended set; the retired ids answer as WS3.3 says;
   `local/qwen3.8-27b` is served by `.28`.
2. **Correctness, per model, cold then warm, streaming** (a cold load through the public URL only
   survives as a stream): a fixed set (one factual question, one short
   reasoning question, "Explain how a Fresnel lens concentrates light, in about 120 words") with
   `max_tokens` 4096: `finish_reason` `stop`, non-empty content, no repetition loop; a full tool round
   trip (a call with valid JSON arguments → the tool result → a final answer); a vision probe where
   the model advertises vision; on cloud3, two concurrent requests and one swap transition. Separately,
   one public NON-streaming request to a cold cloud3 model, recorded as the expected Cloudflare edge
   cut (a negative test, not part of the pass matrix).
3. **Assistant, per model, starting cold** (cloud3 models explicitly unloaded through llama-swap
   first): a dedicated conversation with Apply set to ask, and a read-only task that uses a tool
   ("list the router's routes"). **Pass:** the turn answers without the guided-mode fallback, within
   the turn cap. Record input tokens, serving account and elapsed time from the trajectory, and assert
   the config revision did not change. Plus the worst real transition, NOT pre-unloaded: with
   `qwen3.8-27b-fp8` resident, a pinned `local/qwen3.5-122b` Assistant turn with a tool round trip
   (13–15.5k input tokens, as every Assistant step carries) must complete inside the 15-minute turn.
   Then Stop a running turn through `POST /admin/v1/chat/stop`, confirm `GET /admin/v1/chat` shows it
   ended and the upstream request was released (the browser Stop/reconnect path is not automated
   here). A model that fails is a FAIL, not a footnote: it goes back to the owner to fix or remove.
4. **Consumers:** from the dsh pod, one streaming request per model dsh lists; Open WebUI's default
   (`qwen3.8-27b-vllm-cloud`) through the main gateway, served by cloud2 (check the LiteLLM log);
   litellm-local's diagnostic copy; the main gateway's fallback chain is checked in config only (no
   deliberate cloud2 outage).
5. **Durability:** the next day, after normal VM activity and an idle unload, re-run one cold and one
   warm flash-next check: the prefill fix must hold without a primed page cache.

### WS5 — Benchmarks again (item 7)

Run the P0 harness on the final state, same prompts and settings, three runs each. Publish the
before/after table (P0 baseline vs final) in `docs/runbooks/llm-router.md` and cloudlab's README
cloud3 table; update the memory note.

## Critical files

- llm-router: `packages/plugins/agent-llm/loop.ts`, `packages/plugins/agent-core/index.ts`
  (`TURN_CAP_MS`, an injectable deadline), `packages/contracts/` (`Account.timeouts`),
  `packages/modules/routing/index.ts` (`timeoutsFor` for direct policies), config validation, tests
  under `tests/` (new + `stream-timeouts.test.ts`)
- ailab: `kubernetes/apps/apps/ai/litellm.yaml`, `litellm-local.yaml`, `open-webui.yaml` (generated
  span), `kubernetes/apps/apps/dsh/settings.seed.yaml` (+ `reconcile-default-model.js` if needed),
  `kubernetes/apps/apps/llm-router/router.yaml` (digest), `docs/runbooks/llm-router.md`,
  `docs/runbooks/model-registration.md`, `scripts/bench-router-local.mjs` (new)
- cloudlab: `host/llama-swap.yaml`, `host/build-llama-cpp.sh` (new), `host/*.SHA256SUMS` (if new
  weights), `README.md`, `docs/runbooks/cloud-gpu-cluster.md`, monitoring rules naming removed models
- router runtime config (SQLite, no git source): changed by If-Match PUTs, each revision recorded in
  the llm-router runbook

## Rollout order

0. P0 harness + baseline.
1. WS0 PR → merge → gate (reconciled, rolled out, live checks).
2. WS3.1 router change.
3. WS1 PR → release → `validate-live` (in parallel with steps 4–5).
4. WS3.2 cloudlab removal PR → deploy → verify → WS3.3 router cleanup.
5. WS2.1 diagnosis → WS2.2–2.6 build and qualify → WS2.7 cut-over PR → deploy.
6. WS4, WS5; the WS4.5 durability check the next day.
7. WS3.4 file deletion (w4a16 after WS4; NFS GGUFs on 2026-12-01).

## Rollback

Restore in dependency order: backend files and llama-swap config first, then router entries, then
consumers. Consumers: `git revert`, but never re-add the w4a16 deployment. Router config: reverse
this plan's fields with an If-Match PUT against the CURRENT revision (the revisions endpoint is a
read-only reference; a whole historical snapshot would undo unrelated later edits). llama-swap: the
on-box backup plus `git revert`. flash-next: the b10857 binary, flags and old assets together. Router
image: the previous digest (release recipe).

## Risks

- dsh users with a saved default or a session on a removed model (WS0.2).
- Public non-streaming clients on cold local models still hit Cloudflare's ~100–125 s edge cut.
- `qwen3.8-27b-vllm-cloud` loses its second deployment; `litellm-local` has no fallback (D5).
- `--no-mmap` variant: host memory pressure with the CI VMs; abort thresholds in WS2.5.
- A newer llama.cpp may change flags or GGUF handling: the side-by-side gate and rollback cover it.
- A temporary llama-swap entry leaking into the router catalog (WS2.5).
- cloud3 runs daytime only; cloud3 steps need the box up and reserved.

## Out of scope

The w4a16 root cause (the model is being removed); `qwen3.8-27b-ailab`'s speed (D4); showing hidden
reasoning in the Assistant; moving `qwen3.5-122b` to local NVMe (no space); driver 580 on cloud3.

## Verification

Done when: the retired ids answer 404 from the router (WS3.3's list) and the three models are gone
from LiteLLM, Open WebUI, dsh and cloud3, while `local/qwen3.8-27b` now reaches cloud2;
every remaining model passes WS4.2 and WS4.3 (any failure is decided by the owner, not recorded and
passed); a 90-second cold load no longer fails an Assistant turn and a cold 122B no longer fails a
streaming API call; flash-next meets at least the WS2.5 minimum and holds it in WS4.5; the
before/after benchmark table is published.

<!-- codex-review-status: complete -->
