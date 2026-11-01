# Router local models: follow-ups (Assistant list, images, MTP, dev-worker access)

## Codex Review

- A2's pre-parser rewrite can break conversation placement and cannot report its drops through the existing execution journal without explicit plumbing; it also needs stricter tool and input validation.
- A1/A3 need clearer empty-state and live-authorization semantics, while B4 must verify that the existing worker principals actually have local access.
- B1 and C still schedule cloud3 qualification loads during D's power-safety pause, and the cloud2 image-cap assumption needs reconciliation and enforcement.
- D1/D2 need a reproducible artifact pin and deterministic acceptance criteria for the known stochastic tool corruption.
- Rollback and verification are incomplete: the old image cannot remove routes through the new PATCH API, and stale qwen routes/profiles can remain selectable.

## Context

The 2026-10-31 plan (`plans/2026-10-31-local-models-improvement-plan.md`, results in `docs/runbooks/llm-router.md`
"Results of the local-models plan") left four items for the owner. On 2026-11-01 the owner asked for:

1. **Hide `qwen3.8-27b-fp8` and `qwen3.5-122b` from the Assistant.** Both answer as plain chat but fail as Assistant
   models: fp8 reasons until the 15-minute turn cap and never calls a tool; 122B leaks template text (`</think>`)
   into tool arguments.
2. **Implement the follow-ups:** (a) the router refuses images for every local account (`capabilities.vision: false`,
   no `visionModels`), although several local models load a vision projector; (b) MTP speculative decoding for
   `qwen3.8-flash-next` on cloud3 (llama.cpp b12020 offers `--spec-type draft-mtp`).
3. **Update the llm router:** ship the code changes below as a new router release.
4. **Dev workers can use `qwen3.8-27b` and `qwen3.8-flash-next`** from each worker.

Facts verified on 2026-11-01:

- Live router config revision 2178. Local accounts: `lan-192.168.0.26-8080` (cloud3 llama-swap: `qwen3.5-122b`,
  `qwen3.8-27b-fp8`, `qwen3.8-flash-next`; `timeouts.waitMs` 600000), `lan-192.168.0.28-18020` (cloud2 vLLM
  `qwen3.8-27b`, `exposeModels: false`, served through route `qwen3.8-27b`), `lan-192.168.0.45-8082` and
  `lan-192.168.0.46-8082` (`qwen3.8-27b-ailab`, Strix Halo llama.cpp; .46 `exposeModels: false`). All four have
  `capabilities.tools: true`, `vision: false`, `systemMessages: merge`.
- The Assistant's model list (`packages/plugins/agent-core/index.ts` `routerModelCatalog`) and its route choice
  (`packages/plugins/agent-llm/index.ts` `agentRoute`) qualify a model by its ACCOUNT's `capabilities.tools`. One
  cloud3 account serves fp8, 122B and flash-next, and flash-next passes the Assistant test. Hiding two of its three
  models needs a per-model setting: there is none today.
- Vision projectors: flash-next passes `--mmproj` (cloudlab `host/llama-swap.yaml`); the ailab `qwen3.8-27b` entry
  passes `mmproj` on both instances (`kubernetes/infra/ai-lxc/models.yaml`); cloud2's vLLM serves images with a
  1-image-per-prompt cap (cloudlab runbook, `MM_IMAGES`); 122B has no `--mmproj`; fp8 (vLLM) is untested.
- Dev workers: Codex CLI 0.166.0, provider `llm-router` (`wire_api = "responses"`), each worker's key limited to
  `routes: [dw-N]`, `defaultRoute: dw-N`; `dw-N` points to the smart pool `pool-codex` (Codex-native pass-through,
  six-model allowlist). Router keys have no update endpoint (`packages/plugins/api-keys/index.ts`: GET, POST,
  DELETE, reveal).
- **Captured on dev-worker-1 against a local mock (no real model):** for a non-OpenAI model name, Codex 0.166 sends
  `POST /v1/responses` with top-level `client_metadata`, `include: ["reasoning.encrypted_content"]`, `input`,
  `model`, `parallel_tool_calls: true`, `prompt_cache_key`, `reasoning: {"summary":"auto"}`, `store: false,
  stream: true, `tool_choice: "auto", tools`; no `instructions` (the ~17k-character system text is a `developer`
  message). Tools: function tools (`exec_command`, `write_stdin`, `request_user_input`, `view_image`, three
  goal tools, each with `strict`), one `{"type":"namespace","name":"multi_agent_v1","tools":[...]}` group, and
  `{"type":"web_search","external_web_access":false}`. Input items carry `id` (and `status` on `function_call`);
  `function_call_output.output` is a string. The router's Responses subset (`packages/modules/protocol/index.ts`
  `parseRequest`) refuses `include`, `prompt_cache_key`, `client_metadata`, `reasoning.summary`, the namespace tool
  and the web_search tool, so today a worker's Codex cannot use any chat-backed route at all. The router's own
  Responses SSE rendering (created, output_item.added/done, output_text.delta, function_call items, completed) is
  what Codex consumes; it already accepted the mock's events of the same shapes.

## Approach

Five workstreams. A is router code (one llm-router PR, one release); B is router configuration; C is the dev-worker
role; D is cloud3 MTP; E is the record. Every repo change goes through its PR with a codex review of the diff.

### A. llm-router code (one PR, release `router-0.1.0-20261101-local-followups`)

**A1. `Account.assistantModels` (per-model Assistant eligibility).**
- Contract (`packages/contracts/index.ts`): `assistantModels?: string[]`. Set, it alone decides which of the
  account's models the Assistant may run on; unset, every model of a tools-capable account (today's behavior).
  Same shape and doc style as `visionModels`.
- Config schema (`packages/runtime/config.ts`): same bounds as `visionModels`; names must be in `allowedModels`
  (the same check `visionModels` gets at line ~85).
<!-- codex: Define and test empty versus unset explicitly: an empty assistantModels array should exclude every model (or be rejected), while unset preserves today's fallback. Cover mixed accounts, pinned routes, fallbacks, and assistant_excluded decisions, not just the catalog. -->
- Enforced in three places, through one helper next to `seesImages` (`packages/modules/accounts/index.ts`):
  1. `routerModelCatalog`: a model the account does not allow is not listed at all (hidden, not "unavailable");
     with several accounts serving one id, it is listed when any allowing account serves it.
  2. `agentRoute`, pinned path and route path: a deployment qualifies only if its account allows its model.
  3. Routing, for the Assistant's own requests (`agentId: 'router-agent'`, `agent-llm/index.ts:91`): a target whose
     account does not allow the model is excluded with reason `assistant_excluded`, so a route mixing allowed and
     excluded targets never serves the Assistant on an excluded one. Other clients are unaffected (the reason only
     applies to that agent id; a client claiming it only restricts itself).
- Visible: `inspect_account` (Assistant tool) shows the list; `update_account` accepts `assistantModels`
  (nullable, like `visionModels`). The console's account editor is not changed (configuration API and the
  Assistant's tool suffice; recorded as a ruling).
- Rollback property: the previous image's account schema is not strict and strips the field, so after an image
  revert the two models reappear in the list (no failure).

**A2. Codex CLI requests on chat-backed routes (`/v1/responses`, route not Codex-native).**
<!-- codex: originator is a caller-supplied header and is spoofable. Treat this only as client-declared compatibility mode and ensure it cannot influence authorization, route eligibility, or any privileged behavior. -->
- When the request comes from the Codex CLI (`originator` header starting `codex_`) and the selected route is not
  native, the body is adapted to the subset BEFORE `parseRequest`, which then applies every existing check:
<!-- codex: Preserve the raw request (or extract conversation metadata before rewriting). api-openai calls conversationOf on req.body after parsing; removing prompt_cache_key/client_metadata first loses thread, parent/fork placement, and prompt-cache rekey identity. -->
  - dropped (hints with no meaning for a chat backend): `prompt_cache_key`, `client_metadata`, `reasoning.summary`,
    `reasoning.context`, `text.verbosity`; `include` when every entry is `reasoning.encrypted_content` (the router
    emits no encrypted reasoning), refused as today otherwise;
<!-- codex: Dropping fields from reasoning must normalize an empty object (and similarly partially stripped text/include shapes), otherwise parseRequest still rejects the adapted request. -->
  - tools: `function` tools kept (`strict` kept or dropped per what the subset accepts); `web_search`, `namespace`,
    and any other non-function tool dropped; a `tool_choice` naming a dropped tool refused;
<!-- codex: Specify strict normalization and validation for nested function tools and additional_tools: reject malformed shapes, duplicate or invalid call IDs, mixed content parts, and ambiguous ordering before passing the body to parseRequest. -->
  - input items: `reasoning` items dropped; `additional_tools` items' function tools merged into `tools`, the rest
    dropped; `id`/`status` stripped from `function_call` and `function_call_output`; a `function_call_output` whose
    `output` is a content array becomes its text, and an image part becomes a following user message with the image
    (then the normal per-account vision check applies); `custom_tool_call`, `custom_tool_call_output` and
    `local_shell_call` refused with a message naming the item (Codex sends them only for OpenAI model families).
- Disclosed: response header `x-router-adapted: codex-responses` and the journal's adjusted-parameters record list
  what was dropped (names only, never values).
<!-- codex: Existing adjusted-parameter data is populated inside inference after account adaptation. A pre-parser adapter must carry its drop list into Execution.served and the recorder, or the promised header/journal disclosure will be false. -->
- Requests from other clients, and every Codex-native route, are unchanged. No body-size change: the subset's
  2 MiB limit stays (recorded as a risk: a long Codex session with screenshots can reach it).
<!-- codex: The 2 MiB ingress cap bounds the raw body, not necessarily the expanded adapted representation (merged tools/messages and image parts). Enforce a post-adaptation item/tool/message and serialized-size budget so a boundary-sized request cannot amplify memory or upstream work. -->
- Tests: the two captured 0.166 bodies as fixtures (identifiers replaced with synthetic ones); adapter unit tests per
  rule; an HTTP test through a chat-backed route to a fake OpenAI-compatible backend asserting the upstream chat body
  (tools = the function tools only, messages in order, tool result linked) and the Responses events back
  (`function_call` item with `call_id`, `response.completed` with usage); a non-Codex request with `include` still
  gets 400; a native route still passes through unchanged.
- Before merge, the real Codex 0.166 CLI on a dev worker runs `codex exec` through a locally started router (this
  branch) to a fake chat backend that returns one `exec_command` call and then text: the CLI must run the command
  and finish the turn (the precedent of the Codex-native release).
<!-- codex: Extend this fixture to assert conversation/thread and parent metadata survive adaptation, mixed text/image tool output, multiple tool calls, and ordering; one command-call turn will miss the highest-risk regressions. -->

**A3. `PATCH /admin/v1/keys/:id` (a key's routes, in place).**
- Body `{routes?: string[] | "all", defaultRoute?: string | null}`; administrators only; the same validation as
  `POST /admin/v1/keys` (`routesArg`, every route exists in the key's namespace, `defaultRoute` one of `routes`);
  refuses revoked or expired keys; the token, prefix, id, expiry and usage history are unchanged; audit entry
  `key.updated` with the route lists before and after; returns the key's public view.
<!-- codex: Define PATCH omission semantics separately from explicit all/null: omitted routes/defaultRoute must preserve current values, while a route change must validate the resulting default route atomically. -->
- Why: the workers' keys live in OpenBao and the workers read them live; changing routes in place avoids re-minting
  four secrets and re-seeding `devworker-seeds.sops.yaml`.
- Tests: update, validation refusals, revoked key, viewer refused, the next request authorizes against the new
  routes (cache, if any, invalidated).
<!-- codex: KeyService registers a Principal snapshot and does not re-register an existing id. PATCH must unregister/re-register (or atomically replace) the live HTTP principal; state persistence alone leaves old route authorization active. Test every non-admin role, namespace ownership, default-route retention, and immediate authorization changes. -->

**A4. Release.** PR on llm-router (reviewbot merges); image built by relay CI from the merged commit; ailab PR rolls
the digest with the usual backup (`/data/backups/pre-local-followups-20261101`), `validate-live` before and after
(known 6/9 baseline), runbook section "Local follow-ups release (2026-11-01)" with rollback.

### B. Router configuration (If-Match `PUT /admin/v1/config`, one change per step, read back each)

- **B1 images (current image, before A).** First test each backend DIRECTLY with one PNG whose content must be read
<!-- codex: This probes cloud3 (flash-next/fp8) despite D's explicit no-further-qualification-load pause after the power failures. Gate or defer the cloud3 probes on the owner's power decision; .45/.46/cloud2 can be tested independently. -->
  (a large printed number) and read the answer: .45, .46, cloud2 `qwen3.8-27b`, cloud3 flash-next, cloud3 fp8.
  Then set `visionModels` only on the models that read it correctly (expected: .45/.46 `qwen3.8-27b-ailab`, cloud2
  `qwen3.8-27b`, cloud3 `qwen3.8-flash-next`; fp8 if it passes; never 122B). Verify through the router with the
  harness's vision check; record cloud2's 1-image cap in the runbook.
<!-- codex: This treats the cloud2 one-image cap as documentation only, and cloudlab `main` currently documents `MM_IMAGES=64`; reconcile which setting is live. If any cap remains, enforce/refuse multi-image requests in the router or encode it as a capability, otherwise `visionModels` advertises support the route cannot safely provide. -->
- **B2 Assistant list (after the A roll):** cloud3 account `assistantModels: ["qwen3.8-flash-next"]`.
- **B3 route `qwen3.8-flash-next`:** priority, `access: [local]`, one target `lan-192.168.0.26-8080:qwen3.8-flash-next`,
  `timeoutMs` 600000 (a swap from fp8 is unload + load + prefill), `idleTimeoutMs` 120000, `maxAttempts` 1.
- **B4 dev-worker keys (after the A roll):** `PATCH` each of `key_056782fff988`, `key_f68566fbf013`,
  `key_23f0823b6934`, `key_dc874b9d0a75` to `routes: [dw-N, qwen3.8-27b, qwen3.8-flash-next]`, `defaultRoute`
  unchanged. Codex's default model (`gpt-6-astra`) is not one of the key's routes, so it still takes `dw-N` in
  model mode; a request naming `qwen3.8-27b` or `qwen3.8-flash-next` is route mode on that route.
<!-- codex: Route membership does not grant access to a target whose access kind is local; verify each existing worker key's access includes local before PATCH. If not, this plan has no safe in-place migration path because PATCH cannot change access. -->

### C. Dev-worker role (ailab `ansible/roles/dev_worker/tasks/codex.yml`)

- Two profile files per user, like the existing `chatgpt.config.toml`, only while the router is enabled:
<!-- codex: Only while router enabled needs explicit absent tasks for both new profile files when disabled; otherwise stale qwen profiles remain selectable and may send work to a disabled or changed provider. Verify profile merge, ownership/mode, and removal behavior. -->
  `~/.codex/qwen3.8-27b.config.toml` and `~/.codex/qwen3.8-flash-next.config.toml`, each setting `model` and
  `model_context_window` (cloud2 262144 = its live `/v1/models` `max_model_len`; flash-next 262144 = `-c 524288
  --parallel 2`), and
  nothing else unless the CLI needs it (verified on a worker before the PR). Usage: `codex -p qwen3.8-27b`.
- Converge the four workers with the role's usual recipe; then on each worker `codex exec -p <profile>` with a task
  that needs one shell call and one file write in a scratch directory, both models; `scripts/validate-codex-fleet.sh
  dev_workers` still passes for the default path.
<!-- codex: These per-worker flash-next runs can cold-load or swap cloud3 models, so they are also qualification loads blocked by D's power-safety pause. Make the model-specific tests owner-gated or run only after the pause is lifted. -->
<!-- codex: Include daemon state in the rollout check: a running Codex app-server may retain the old provider/config. Either restart it under the documented safe conditions or prove these profile tests bypass the daemon. -->
- Runbook `docs/runbooks/dev-workers.md` § "Codex through the router": the two profiles, the routes on the keys,
  cloud3's one-model-at-a-time swap cost, and that these runs use no subscription quota.

### D. cloud3: MTP for flash-next (cloudlab)

**Status 2026-11-01 11:50Z — PAUSED for the owner.** cloud3 lost power abruptly twice in 18 hours, each time
seconds after a heavy multi-GPU model start that my qualification triggered (2026-10-31 15:58Z, fp8 cold vLLM;
2026-11-01 09:47:45Z, the MTP n-max 3 cold load). The second time cloud2 (last log 09:47:21Z) and the
workstation went down too, while the ailab nodes and cloud1 stayed up (all rebooted ~10:53Z; cloud2/cloud3/workstation
back ~11:00Z): a shared circuit, not a host fault. Temperatures were normal (NVMe 70.8 C, Tctl 76 C). No further
qualification load on cloud3 until the owner has looked at the power side. What D found before the pause:
- D0 (durability) PASSED: file-cold prefill 1,696/1,718 tok/s (10-31: 1,633/1,677), decode 62 tok/s.
- MTP on mainline b12020 is unproven: Unsloth's MTP README (HF revision 766911a6, 2026-10-06) says mainline has no
  MTP graph for `qwen4exp`; upstream #28243 closed unmerged. The draft (`shared-Q8_0`, sha256 5ff54097...) is on
  `/models-local/qwen3.8-flash-next/MTP/`. The n-max 2 entry exited during load four times ("upstream command exited
  prematurely"; the log buffer was lost with the reboot). If mainline cannot run it, MTP needs Unsloth's fork:
  an owner decision (the 10-31 plan said never to deploy a personal fork).
- **Answer quality (pre-existing, not a b12020 regression):** on the harness's tool-result turn, flash-next garbles
  the copied time ("16:aka 4:42 PM", "16:</think>") in 11-12 of 20 samples on BOTH b10857 and b12020 at its
  configured sampler (temp 1.0); 15/20 at temp 0.6 and 0.3; greedy 3/3 clean (b12020). `--lazy-mode off` does not
  change it. An f16-KV variant is deployed but untested. This matters for Codex on dev workers (command text).
- cloud3 still runs the temporary config (unlisted qualification entries; listed models unchanged). Restoring the
  production file needs a llama-swap restart, which starts an fp8 cold vLLM load: done only with the owner's OK.

Original D steps, for when it resumes:

- **D0 durability check (the open WS4.5 item):** backend-mode run of today's flash-next (b12020, current flags):
  warm, then file-cold (fadvise-evicted, as in WS2.1). Pass: prefill within 15% of 2026-10-31's 1,633/1,677 tok/s.
- **D1 draft:** download `MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf` (~2.79 GB) from `unsloth/Qwen3.8-Flash-Next-GGUF`
  at a pinned commit to `/models-local/qwen3.8-flash-next/MTP/`; record its sha256.
<!-- codex: Pinned commit is not a reproducible supply-chain pin unless the exact HF revision and complete expected SHA-256 are recorded before download; the abbreviated hash in Context cannot verify the artifact. -->
- **D2 qualification (backend mode, thermal guard v2: abort at NVMe > 85 °C or Tctl > 92 °C, kill by Windows PID):**
  variants (a) current, (b) `-md <draft> --spec-type draft-mtp --spec-draft-n-max 3`, (c) the same with 5. Measured
  with the frozen harness: decode, prefill 9k/15k, depth, quality, tools (arguments read by hand), vision (MTP with
  `--mmproj` must still load and answer), concurrency (`--parallel 2`), VRAM. Accept the best variant if decode
  improves by >= 20% with no quality/tools/vision failure and prefill within 10%. If MTP refuses to load with
  `--mmproj`: stop and ask the owner (images or MTP), do not drop either silently.
<!-- codex: Define the frozen baseline, sample count, pass/fail metric, and tolerance for quality/tools. The known stochastic tool corruption makes a single hand-read tool turn insufficient and can make the >=20% comparison irreproducible. -->
- **D3 cloudlab PR:** llama-swap entry, runbook section 13e, README; deploy with backup and box sha256 check;
  verify through the router (harness router mode for flash-next) and one Assistant turn on flash-next (must still
  pass: MTP must not change the tool behavior).

### E. Record

- Runbooks: ailab `llm-router.md` (release section, configuration revisions, results), `dev-workers.md`; cloudlab
  runbook 13e. Memory note updated. No `plans/` rewrite of the 2026-10-31 records.

## Order

B1 (config, independent) runs while A is implemented and reviewed; D waits for the owner (see D). Then A roll →
B2, B3, B4 → C → D3 → final router-mode benchmark of the changed models (flash-next; the others only if a change
touched them) → E.
<!-- codex: This ordering is unsafe: B1 and C still load cloud3 while D is paused, and D3 is listed after them. Split non-cloud3 work from owner-gated cloud3 work and make the gate explicit. -->

## Critical files

- llm-router: `packages/contracts/index.ts`, `packages/runtime/config.ts`, `packages/modules/accounts/index.ts`,
  `packages/plugins/agent-core/index.ts`, `packages/plugins/agent-llm/index.ts`, `packages/modules/routing/index.ts`,
  `packages/plugins/config-store/tools.ts`, `packages/modules/protocol/index.ts` (adapter beside `parseRequest`),
  `packages/plugins/api-openai/index.ts` (where the adapter is applied), `packages/plugins/api-keys/index.ts` (+ its
  service/store), tests, README.
- ailab: `kubernetes/apps/apps/llm-router/` (image digest + annotations), `ansible/roles/dev_worker/tasks/codex.yml`,
  `docs/runbooks/llm-router.md`, `docs/runbooks/dev-workers.md`.
- cloudlab: `host/llama-swap.yaml`, `docs/runbooks/cloud-gpu-cluster.md`, `README.md`.

## Verification

- A: full llm-router suite green; the new tests above RED before GREEN; the real Codex CLI against the branch's router.
- After the roll: Assistant model list (`POST /admin/v1/chat/conversations` backend models) shows no fp8/122B and
  still shows flash-next, .45, cloud2 route; pinning `local/qwen3.8-27b-fp8` is refused with the "needs a route
  whose model can use tools" reason; an Assistant turn on flash-next still passes; `validate-live` = baseline.
- Images: harness vision check through the router passes on each enabled model; a 122B image request is refused
  before any upstream call.
<!-- codex: Vision verification must include multiple images and mixed text/image content, with expected refusal behavior for the cloud2 limit; one positive check does not verify the documented cap. -->
- Dev workers: per worker and model, `codex exec -p ...` completes a shell call and a file write; the journal shows
  `route: qwen3.8-27b` / `qwen3.8-flash-next` served by the expected account with `x-router-adapted: codex-responses`;
  default Codex still served by `pool-codex`.
<!-- codex: Repeat each model check across several tool turns and mixed targets, and verify the adapted journal/header on every attempt; one shell plus one file write can pass despite the known stochastic corruption. -->
- MTP: D2 table recorded; after deploy, router-mode benchmark of flash-next; Assistant turn on flash-next passes.
<!-- codex: One Assistant turn and one benchmark are too weak to detect intermittent tool corruption or mixed-target regressions; require repeated tool calls and fallback/route-path checks. -->

## Rollback

- Image: the reviewed GitOps revert to `dad0e721…`. The old image strips `assistantModels` (models reappear),
  refuses Codex requests on chat routes again with 400 (the workers' default path is unaffected), and has no key
  `PATCH` (keys keep the routes already written).
<!-- codex: This rollback is internally inconsistent: after the old image is live, its API has no PATCH endpoint, so the later remove-extra-key-routes step cannot run. Remove routes before the image revert (or retain an admin path) and verify key state. -->
- Config: remove `visionModels` / `assistantModels` / the route / the extra key routes (same `PUT`/`PATCH`).
<!-- codex: Reverting the image makes assistantModels disappear but leaves qwen routes and profile files selectable; old-image chat-backed requests then fail again. Disable/remove the new routes and profiles before or as part of rollback, and restart affected daemons. -->
- Dev workers: the profile files are inert unless `-p` names them; removed with the role change revert.
- MTP: revert the llama-swap entry (backup file), the draft file stays until deleted.

## Risks

- cloud3 went dark on 2026-10-31 during heavy file-cold testing (NVMe 90.9 °C). D runs few, guarded runs.
- cloud3 serves one model at a time: a dev worker on flash-next evicts fp8 (boot preload) and vice versa; each swap
  costs the next caller the load time.
- Qwen on Codex's ~10k-token tool prompt may call tools poorly; the per-worker validation decides whether the
  profiles ship as "available" or "experimental" in the runbook.
- The adapter covers Codex 0.166's request shape; an unknown future field is refused with a clear 400, never
  silently dropped.

<!-- codex-review-status: complete -->