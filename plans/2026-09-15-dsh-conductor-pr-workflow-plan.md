# dsh conductor workflow: plan → implement → review → PR → iterate to merge

## Codex Review

- **P1 — The second spawn tool is plausible, but the existing rows do not prove it.** Two are disabled. Verify the deployed package, disable model selection on both fixed-route tools, and test independent continuation and depth enforcement.
- **P1 — PR delivery lacks prerequisites.** The runbook records read-only repository grants and a read-only PAT; Git authentication does not authenticate the REST API. Specify write provisioning, API credential handling, and the actual reviewer/merge handoff.
- **P1 — Keep the first workflow bounded.** Repeated model-driven polling adds cost without durable scheduling. Start with an explicitly invoked delivery/update pass; unattended monitoring needs separate lifecycle and event integration.
- **P1 — Preserve cloud3's requested default and preload.** Flash-Next is already addressable by name. A default change neither dedicates the host nor prevents other named requests from evicting it.
- **P2 — Split the change.** Prove routing and review first, add authenticated PR operations second, and decide unattended orchestration and host allocation separately. The deployment wiring and failure tests also need corrections.

<!-- codex: Review basis: the requested plan was found in .claude/worktrees/codex-20260915-140822/plans/, absent from the current checkout's plans/. Reviewed against the requested current-checkout files and the sibling cloudlab/host/llama-swap.yaml. Upstream links below describe current master; the deployed 0.1.5-alpha.2 package was not available for inspection, so its mount and runtime behavior remain acceptance gates. -->

## Context

The operator wants a dsh team that runs a full delivery loop with three differently-routed
models:

| Role | Model | LiteLLM route | Backend |
|---|---|---|---|
| Conductor | GPT-6 Astra | `gpt-6-astra` | paid, external |
| Worker (implement, tools, research, PR) | Qwen3.8-27B-W4A16 | `qwen3.8-27b-vllm-cloud` | cloud2 `:18020`, dedicated vLLM TP=4 |
| Reviewer (plan + implementation) | Qwen3.8-Flash-Next | `qwen3.8-flash-next-cloud` | cloud3 `:8080`, llama-swap |

Flow: **Plan** (conductor) → **Implement** (worker) → **Review** (reviewer) → **PR** (worker) →
**watch the PR, address feedback** (worker implements, reviewer re-reviews) → iterate until the
Gitea reviewer agents merge it.

<!-- codex: P1 — Specify the handoff contract: which bot watches which repositories/events/labels, what triggers review after a push, required checks and approvals, and who is allowed to merge. docs/runbooks/agentforge.md documents an existing forge lifecycle with distinct bot identities; it does not establish that any PR opened by dsh will be merged. Add a plan-review gate BEFORE implementation: the flow currently omits the reviewer's stated plan-review role. -->

**Half of this already exists.** `team-conductor` (preset "Conductor", order 20) is exactly
conductor-on-Codex delegating to a qwen worker: its `tool-subagent` row already carries
`agentOptions: {provider: litellm, model: qwen3.8-27b-vllm-cloud}`. The genuinely new parts are
(a) a second, differently-routed child for review and (b) the PR lifecycle.

This plan therefore **extends `team-conductor` into a new `team-delivery`** rather than building
anything from scratch.

## Approach

### 1. A second routed child — use the existing pattern, not new machinery

`@deepseek-ai/dsh-tool-subagent` is already instantiated **three times** in `team-conductor`
(`subagent`, `subagent_codex`, `subagent_claude_code`), each with its own `id`, `toolName` and
config. A reviewer is a fourth instantiation:

<!-- codex: P1 — These are three declarations, only one enabled; the Codex and Claude Code rows are disabled and use different providers. They do not demonstrate two live consumers of spawn. Current upstream explicitly supports distinct toolName values and per-instance child defaults, so sharing the single host spawn provider is a sound candidate; do not register a second provider or control registry. Prove both tools mount together on the deployed version, including two parent sessions. See the [upstream delegation contract](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/tool-subagent/README.md). -->

```yaml
- id: tool-subagent-reviewer
  name: '@deepseek-ai/dsh-tool-subagent'
  config:
    provider: spawn
    toolName: subagent_reviewer
    backgroundMode: continuable
    agentOptions:
      provider: litellm
      model: qwen3.8-flash-next-cloud
    maxDepth: 2
```

The conductor then has two aimed tools: `subagent` (worker) and `subagent_reviewer` (reviewer).
No fork, no ralph — see the inheritance trap below.

<!-- codex: P1 — Explicitly set modelSelectionSettings: false on BOTH delivery rows. The inherited worker currently has true: with an enabled session policy, caller-supplied provider/model fields override its configured defaults. The proposed reviewer omits the setting, whose upstream default is false. Copying true into it introduces a separate collision: each enabled instance registers the fixed name list_subagent_models, regardless of toolName. See [route override handling](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/tool-subagent/src/model-selection.ts) and [discovery registration](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/tool-subagent/src/list-models.ts). Test that attempted route overrides are rejected, not merely absent from the displayed schema. -->

### 2. Dedicate one host per model — the single most important operational change

The worker is on **cloud2** (dedicated vLLM engine, always resident). The reviewer is on
**cloud3**, which runs **llama-swap and holds exactly one model at a time** against 96 GiB of VRAM.

cloud3's `default` alias currently points at `qwen3.8-27b-w4a16` and is preloaded at startup. In
this workflow the reviewer is called on **every** plan and every implementation, so any other
cloud3 traffic evicts Flash-Next and pays an ~84 GiB cold load to swap back — and vice versa.

**Move cloud3's `default` alias and `hooks.on_startup.preload` back to `qwen3.8-flash-next`**, so
each host is dedicated:

- cloud2 → `qwen3.8-27b-vllm-cloud` (worker), always resident
- cloud3 → `qwen3.8-flash-next` (reviewer), preloaded, `ttl: 7200`

`qwen3.8-27b-w4a16` stays configured on cloud3 and loadable by name as a fallback; it just stops
being the default. This resolves the eviction tension directly rather than living with thrash.

<!-- codex: P1 — Reject this change as part of the team rollout. The operator explicitly requested W4A16 as cloud3's default, and cloudlab/host/llama-swap.yaml currently gives it both the alias and startup preload. The reviewer route already sends openai/qwen3.8-flash-next, so changing default is unnecessary for routing. Preload runs at startup and TTL controls idle unloading; neither reserves GPUs against named requests for W4A16, FP8, 122B, or the coder model. Keep the requested defaults, measure shared-host latency, and consider an explicitly agreed review window or separate capacity only if measurements justify it. The 83.8 GiB checkpoint size is not a latency measurement: litellm.yaml records a 13.7 s cold load and 128 tok/s prompt evaluation; use a representative review prompt. -->

### 3. The PR loop

The worker already has forge access: the shell can clone/fetch/push `https://git.chifor.me/...`
via the OpenBao-backed git credential helper (`af/dsh/credentials` → `GITEA_USER`/`GITEA_PAT`),
with no token handed to the model. So "open a PR" and "read PR comments" are `bash` + `curl`
against the Gitea API, not new tooling.

<!-- codex: P1 — Authentication plumbing exists, write authority does not follow from it. docs/runbooks/dsh.md records the dsh account's pull-only grants, read:repository PAT, denied issue creation, and absent commit identity. Delivery requires explicit repository write grants, an appropriately scoped replacement PAT, commit identity, and the documented OpenBao patch/rotation verification. Separately, agents.seed.md explicitly says the helper does not cover REST: curl never invokes Git's credential helper. Specify a reusable API wrapper that reads credentials internally at use time, restricts the destination, and avoids secrets in arguments, logs, and tool output; verify the deployed Gitea endpoint scopes for PRs and comments. Treat these as a separate prerequisite, not something a new preset enables. -->

Iteration uses `backgroundMode: continuable` (already set on the worker row): the conductor
re-enters the same child session with new feedback rather than spawning a fresh one, preserving
context and prefix cache.

<!-- codex: P1 — Record worker and reviewer child IDs separately and continue through send_message(agent_id, ...). Invoking either spawn tool again creates another child. Current upstream's continuable BACKGROUND path returns a durable ID; an explicit foreground call disposes its run after collection. send_message acknowledges acceptance, not completion, and siblings cannot message each other directly. Keep one shared control-tool registration and route exchanges through the conductor. Session history can persist without a warm KV cache after eviction/restart. See [execution paths](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/tool-subagent/src/index.ts) and [control semantics](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/tool-subagent-control/README.md). -->

**The loop is conductor-driven, not a daemon.** Each cycle is: conductor asks the worker to poll
the PR → worker returns state (checks, review comments, mergeable) → conductor decides → dispatches
worker (fix) and/or reviewer (re-review) → repeat. Termination conditions must be explicit:
merged, closed, or a round cap.

<!-- codex: P1 — Prefer one explicit delivery/update invocation, ending at a PR link or a waiting/blocked checkpoint, for the first release. A team supplies agent tools and instructions; continuable children do not supply a durable PR watcher. For a bounded watch, poll with deterministic code and invoke models only on actionable changes, with concrete poll interval/backoff, wall-clock deadline, inference budget, repair-round cap, and stop handling. Overnight/unattended operation belongs in a separate design: compare an authenticated webhook consumer plus reconciliation polling with existing AgentForge integration before adding a scheduler. Persist repo/PR, branch/head SHA, child IDs, processed feedback IDs, round count and next action; resume must re-read forge state and avoid duplicate PRs, comments and fixes. A saved child transcript alone cannot recover the delivery state machine. -->

### 4. Ship it as `team-delivery`, leaving `team-conductor` untouched

Add `team-delivery.agent.cordis.yml` + `team-delivery.preset.yml` (order 30) alongside the
existing three teams. `team-conductor` keeps working for people who want plain conduct→work.

<!-- codex: P2 — Split implementation into independently verifiable changes: (1) routing spike plus a delivery preset supporting plan/implementation review, without forge writes or host-default changes; (2) write provisioning, API wrapper and one bounded PR/update operation; (3) unattended monitoring only after the handoff and restart contract are proven. Any cloudlab host-allocation change needs its own rationale and rollback. Also, order 30 is already used by team-review.preset.yml; choose a distinct order if stable placement is intended. -->

The preset description must repeat the warning the Conductor preset already carries: **selecting
the team is half the job — the session model must also be set to `gpt-6-astra`**, or the conductor
runs on the `agent-default-model` (`litellm/qwen3.8-27b-vllm-cloud`) and it is qwen conducting qwen.

<!-- codex: P1 — The proposed plugin list does not encode the delivery procedure. The copied persona is generic. Name the team-scoped instruction source and define role dispatch, plan approval, review results and stop behavior there. Give each fresh spawn a self-contained brief; require reviews to identify the exact plan revision or commit SHA, findings, verdict and validation evidence. Keep one writer per checkout and prevent the reviewer from modifying the implementation; the conductor header explicitly says all children share cwd. A different model supplies neither workspace isolation nor a separate forge approval identity. Re-review after every head change and preserve the copied plan-mode approval gate. -->

## Constraints this plan must respect

- **The inheritance trap.** Spawn children inherit the parent route unless overridden, so on a
  Codex conductor an unconfigured child is *another Codex agent*, billed accordingly. Every
  delegation row must be explicitly routed or dropped. `subagent_fork` must stay **dropped** (it
  preserves the parent route by design, for KV reuse — it cannot be re-aimed), and `tool-ralph`
  must stay **dropped** (its config exposes no route knob at all).

<!-- codex: P1 — The copy also retains workflow-worker-thread and tool-workflow. The conductor header explicitly warns that agent() routes come from each workflow script and otherwise inherit Codex; 4/64 bounds do not pin them. Drop this alternate delegation path from delivery v1 unless it is needed, or specify and verify the route on every agent() call. Pinning only the two spawn rows does not close all paid-child inheritance paths. -->

- **Day-only backends.** cloud1/2/3 power off nightly (RTC wake ~08:00). A PR-watch loop that
  spans the shutdown will fail on both worker and reviewer. The loop needs to fail soft and resume,
  not hang.

<!-- codex: P1 — Define a checkpointed unavailable state and who resumes it; do not infer automatic wake/resume from continuable mode. Also, the worker route already falls back to qwen3.5-122b-cloud on cloud3 and then qwen3.6-35b-a3b-local. It may therefore keep answering with a different model, and its first fallback can evict the reviewer. Decide whether delivery accepts and reports fallback execution or pauses when the designated worker is unavailable. Merely observing that a request succeeded cannot prove the requested topology. -->

- **No-cancel.** LiteLLM 1.91.0 does not propagate client disconnects, so an abandoned request
  keeps decoding. Both backends now run ≥2 slots (cloud2 `--max-num-seqs 8`, cloud3 flash-next
  `--parallel 2`), so an orphan costs capacity rather than blocking the model.

<!-- codex: P1 — Reconcile the records before claiming this mitigation is verified: litellm.yaml's Flash-Next comment still says -c 262144 / --parallel 1, while the sibling cloudlab host config says -c 524288 / --parallel 2. The latter preserves 262144 tokens per slot and matches the advertised 229376 input limit; verify the deployed launch args and correct the stale comment. Two slots do not bound repeated orphaned requests. Set server-side completion limits and a retry policy that accounts for requests still decoding after client timeout; cloud2's eight sequences are not a team-wide concurrency budget. -->

- **HTTP 200 is not health.** cloud2 has a recorded fault where the engine served well-formed
  200s containing only token 0 (`!`). A review step that accepts any 200 as success will happily
  consume garbage; the loop should sanity-check review output is non-empty and non-degenerate.

<!-- codex: P1 — Apply content validation to BOTH children, especially the worker on the historically affected cloud2 route. Reject empty/truncated/error output and require a valid review verdict tied to the current artifact; non-empty prose is not evidence of completed review or passed tests. The route comments also warn about structured-output/tool-call interactions, so exercise real dsh tool use and avoid using response_format: json_schema for the health probe. Validate ordinary returned text locally and retain the existing content-health evidence. -->

- **`maxDepth: 2`.** Conductor → worker is depth 1; anything the worker spawns is depth 2. The
  reviewer is a sibling of the worker (both depth 1), not nested, so the budget holds — but a
  worker that itself delegates consumes the remaining depth.

<!-- codex: P1 — This depth interpretation is consistent with the documented absolute cap, and toolName does not create a separate depth budget. It still allows arbitrarily many siblings; workflow bounds apply only inside workflow runs, and the conductor header says a gateway cap is unproven. For v1, remove child delegation where unnecessary and allow only one active implementer plus a reviewer after writes settle. Test rejection beyond depth 2 through both tool names, role continuation after resume, and independent ownership across parent sessions. Do not assume interrupt_agent recursively stops descendants. -->

- **Cost.** `gpt-6-astra` is paid and the conductor runs every turn. Keep the conductor's job to
  routing and decisions; do not let it do research or file reading that the worker can do.

<!-- codex: P2 — Bound total spend and elapsed time per delivery, including reasoning tokens, retries and unchanged polls. A small conductor token fraction is not a spend bound and can coexist with runaway workers. Let the conductor inspect the minimum authoritative diff/check evidence needed for its decisions; prohibiting all file reads would make it depend entirely on worker summaries. -->

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/apps/dsh/agent-teams/team-delivery.agent.cordis.yml` | **new** — plugin list: conductor tools + two routed subagent rows |
| `kubernetes/apps/apps/dsh/agent-teams/team-delivery.preset.yml` | **new** — name/description/order (30) |
| `kubernetes/apps/apps/dsh/agent-teams/team-conductor.agent.cordis.yml` | reference only — the row shapes are copied from here |
| `kubernetes/apps/apps/dsh/reconcile-bundles.js` | how presets/teams reach the pod; confirm a new pair is picked up with no code change |
| `kubernetes/apps/apps/dsh/settings.seed.yaml` | provider/model rows; confirm both routes are present and visible |
| `host/llama-swap.yaml` (cchifor/cloudlab) | cloud3 default + preload move back to `qwen3.8-flash-next` |
| `docs/runbooks/dsh.md` | document the team, the two-part selection, and the loop's termination conditions |

<!-- codex: P1 — Add kubernetes/apps/apps/dsh/kustomization.yaml: its configMapGenerator explicitly enumerates both files for every team. deployment.yaml's seed-settings loop projects that pair into /dsh-teams; reconcile-bundles.js manages installed plugin bundles, not team discovery. Update scripts/tests/test_agent_teams.py too: REQUIRED_BOUNDS is keyed by row ID and currently does not cover tool-subagent-reviewer. Gate reviewer routing, disabled model selection and the absence of unintended delegation paths. Include the team instruction source and, in phase 2, the API wrapper and its meaningful authentication/idempotency tests. -->

## Verification

1. **Routing is actually split.** Start a session on `team-delivery` with the session model set to
   `gpt-6-astra`. Dispatch one `subagent` and one `subagent_reviewer` task. Confirm on the
   backends, not in the UI: cloud2's `/metrics` request counter moves for the worker call, cloud3's
   for the reviewer call. This is the test that catches the inheritance trap — if a child silently
   ran on Codex, neither counter moves.

<!-- codex: P1 — First resolve and mount the candidate using the deployed dsh package; inspect stderr as well as the roster and exposed tools, since the runbook warns dump-config can exit zero with loader errors. Run test_agent_teams.py and render the dsh kustomization. Then correlate child IDs and resolved routes with gateway/backend requests; aggregate counters alone are ambiguous on shared hosts and one agent task may issue several requests. Verify both initial calls and continuations, explicitly accounting for LiteLLM fallback execution. -->

2. **No swap thrash.** With cloud3 dedicated, run five consecutive review calls and confirm
   `/v1/models` shows `qwen3.8-flash-next` stays `loaded` throughout and VRAM does not drop to 3 MiB
   between calls.

<!-- codex: P2 — Five consecutive calls with no competing traffic cannot establish exclusivity or lack of thrash. Verify which deployed llama-swap status endpoint actually exposes loaded state; /v1/models may describe available models. Measure cold/warm review latency, long-prompt prefill, an intervening named-model request, and the worker-fallback path. Use process/load logs and GPU state as corroboration. These measurements should inform any later host-allocation decision. -->

3. **End-to-end on a throwaway repo.** Run the full loop against a scratch Gitea repo: conductor
   plans, worker implements and opens a PR, reviewer reviews, worker addresses a deliberately
   planted review comment, loop terminates when merged.

<!-- codex: P1 — Have the scratch repository provisioned with the intended grants, branch protection, CI and reviewer-bot subscription; the documented dsh account cannot create repositories. Prove the plan is reviewed before coding, a review for an older SHA cannot approve a newer head, duplicate feedback is handled once, and only the designated forge actor merges after current-head checks pass. Include a normal waiting outcome when no merge bot is configured. -->

4. **Failure modes.** (a) Power off cloud3 mid-loop and confirm the loop reports a clear failure
   rather than hanging. (b) Point the reviewer at a prompt that returns an empty body and confirm
   the loop does not treat it as a passed review.

<!-- codex: P1 — Use an isolated failing endpoint or controlled fault injection instead of powering off shared cloud3. A prompt is not a deterministic empty-response fixture; stub empty content, HTTP-200 repeated-token output, truncation, malformed verdicts and tool errors. Also cover cloud2 failure/fallback, API 401/403/429/5xx, closed PR, cap/deadline exhaustion, stop with work in flight, and host/session restart between a forge write and checkpoint. Assert no duplicate writes and no timeout-driven request pile-up. -->

5. **Cost sanity.** After one full loop, check the conductor's token usage against the worker's.
   The conductor should be a small fraction; if it dominates, work has leaked upward.

<!-- codex-review-status: complete -->