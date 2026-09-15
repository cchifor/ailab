# dsh conductor workflow: plan → implement → review → PR → iterate to merge

## Context

The operator wants a dsh team that runs a delivery loop with three differently-routed models:

| Role | Model | LiteLLM route | Backend |
|---|---|---|---|
| Conductor | GPT-6 Astra | `gpt-6-astra` | paid, external |
| Worker (implement, tools, research, PR) | Qwen3.8-27B-W4A16 | `qwen3.8-27b-vllm-cloud` | cloud2 `:18020`, dedicated vLLM TP=4 |
| Reviewer (plan **and** implementation) | Qwen3.8-Flash-Next | `qwen3.8-flash-next-cloud` | cloud3 `:8080`, llama-swap |

Flow: **Plan** (conductor) → **plan review** (reviewer) → **Implement** (worker) → **impl review**
(reviewer) → **PR** (worker) → address feedback (worker implements, reviewer re-reviews) → iterate
until the forge reviewer agents merge.

Note the plan-review gate before implementation: the operator asked for the reviewer to review
"plan and worker implementation", and the first draft of this plan omitted the plan half.

**Half of the delegation already exists.** `team-conductor` (preset "Conductor", order 20) is
conductor-on-Codex delegating to a qwen worker. The new parts are a second, differently-routed
child for review, and the PR lifecycle.

## Prerequisites — neither is satisfied today

These gate Stage 2. They are not implementation details.

**1. dsh cannot write to the forge.** `docs/runbooks/dsh.md` records the account holding **read
grants only**, a **`read:repository`** PAT, denied issue creation and **no commit identity**, and
states that enabling pushes is *two* changes — write grants on the repos **and** a
`write:repository` PAT. The read-only posture is deliberate: the runbook cites a Gitea CVE
(CVSS 8.1) that skips repository-scope enforcement on Git Smart HTTP, so *"an account holding only
read grants has nothing to unlock."* Granting write is a security decision for the operator, not a
step this plan may assume.

Enabling it is four things, not one: repository write grants, a scoped replacement PAT, a commit
identity, and the documented OpenBao patch/rotation verification. And `agents.seed.md` states the
credential helper **does not cover REST** — `curl` never invokes Git's credential helper, so Git
push working proves nothing about API authority. Specify a reusable API wrapper that reads the
credential internally at use time, restricts the destination host, and keeps secrets out of
arguments, logs and tool output; confirm the deployed Gitea endpoint scopes for PR creation and
comments.

**2. There is no defined handoff contract with the forge reviewer agents.** "Iterate until merged
by the Gitea reviewer agents" assumes a contract that does not exist in writing: which bot watches
which repositories, what event or label triggers review after a push, which checks and approvals
gate merge, and who is permitted to merge. `docs/runbooks/agentforge.md` documents a forge
lifecycle with distinct bot identities but does not establish that a PR opened by dsh will be
picked up at all. A run that ends waiting because no merge bot is configured is a **normal
outcome**, not a failure.

## Approach — three independently shippable stages

### Stage 1 — Routing spike + review, no forge writes

Prove the two-route topology and the review role. Nothing here needs new credentials.

Add `team-delivery` as a new pair alongside the existing teams, copied from `team-conductor` with
these changes:

```yaml
- id: tool-subagent            # worker
  name: '@deepseek-ai/dsh-tool-subagent'
  config:
    provider: spawn
    toolName: subagent
    modelSelectionSettings: false      # was true — see below
    backgroundMode: continuable
    agentOptions: { provider: litellm, model: qwen3.8-27b-vllm-cloud }
    maxDepth: 2

- id: tool-subagent-reviewer   # reviewer
  name: '@deepseek-ai/dsh-tool-subagent'
  config:
    provider: spawn
    toolName: subagent_reviewer
    modelSelectionSettings: false
    backgroundMode: continuable
    agentOptions: { provider: litellm, model: qwen3.8-flash-next-cloud }
    maxDepth: 2
```

Preset `order: 40`. **Not 30** — `team-review.preset.yml` already holds 30, and two teams at one
order have no defined placement.

`modelSelectionSettings: false` on **both** rows is load-bearing, not tidiness. The inherited
worker row has it `true`, which lets caller-supplied provider/model fields override the configured
route — silently putting the worker back on the paid conductor model. Each enabled instance also
registers the fixed tool name `list_subagent_models` regardless of `toolName`, so two enabled
instances collide. Verify that an attempted route override is **rejected**, not merely absent from
the displayed schema.

**Close every other paid-inheritance path, not just the two spawn rows.** `subagent_fork` and
`tool-ralph` stay dropped (neither can be re-aimed). The copy also carries
`workflow-worker-thread` and `tool-workflow`, whose `agent()` routes come from each workflow script
and otherwise inherit Codex — drop both from v1 unless needed.

Only one enabled spawn row exists in `team-conductor` today (the Codex and Claude Code rows are
`disabled: true` and use different providers), so two live spawn consumers is a **candidate, not a
demonstrated pattern**. Keep one shared subagent-control registration; do not register a second
control registry.

### Stage 2 — Authenticated PR operations (blocked on prerequisites)

Once write grants and a `write:repository` PAT exist, and the reviewer-bot contract is written
down, add the PR step. **One explicit delivery/update invocation**, ending at a PR link or a
recorded waiting/blocked checkpoint. No unattended polling in this stage.

### Stage 3 — Unattended iteration (separate design)

Only after Stages 1-2 are proven. A continuable child is **not** a durable PR watcher. This stage
needs its own design comparing an authenticated webhook consumer plus reconciliation polling
against existing AgentForge integration, and must persist delivery state — repo/PR, branch head
SHA, child IDs, processed feedback IDs, round count, next action — because a saved child transcript
cannot recover a state machine. Resume must re-read forge state and avoid duplicate PRs, comments
and fixes. Poll with deterministic code and invoke models only on actionable changes. Every loop
needs a poll interval with backoff, a wall-clock deadline, an inference budget, a repair-round cap,
and stop handling.

### Delegation mechanics (Stages 1-2)

Record worker and reviewer child IDs **separately** and continue each through
`send_message(agent_id, ...)`; invoking either spawn tool again creates *another* child. Only the
continuable **background** path returns a durable ID — a foreground call disposes its run after
collection. `send_message` acknowledges acceptance, not completion, and siblings cannot message
each other, so all exchanges route through the conductor. Session history can persist without a
warm KV cache after eviction or restart.

Depth: `toolName` does not create a separate depth budget, and depth 2 still permits arbitrarily
many siblings. For v1 allow **one active implementer plus a reviewer**, and do not assume
`interrupt_agent` stops descendants recursively.

### Team instructions — the part a plugin list cannot express

A plugin list grants tools; it does not encode a procedure, and the copied conductor persona is
generic. Name the team-scoped instruction source and define there: role dispatch, plan approval,
how review results are returned, and stop behaviour.

- **One writer per checkout.** The conductor header states all children share `cwd`. A different
  model is not workspace isolation and not a separate forge approval identity. The reviewer must
  not modify the implementation.
- **Every fresh spawn gets a self-contained brief** — a new child has no inherited context.
- **Every review names the exact plan revision or commit SHA** it reviewed, plus findings, a
  verdict, and validation evidence. A review of an older SHA cannot approve a newer head.
- **Preserve the copied plan-mode approval gate.**

## Constraints

- **The worker's own fallback can evict the reviewer.** `litellm.yaml` falls
  `qwen3.8-27b-vllm-cloud` back to `qwen3.5-122b-cloud` — which is **on cloud3** — and then to
  `qwen3.6-35b-a3b-local`. So a cloud2 hiccup can silently answer with a different model *and*
  evict Flash-Next from the reviewer's host. Decide explicitly whether delivery accepts and
  **reports** fallback execution or pauses when the designated worker is unavailable. A successful
  request does not prove the requested topology ran.
- **Keep cloud3's default and preload as the operator set them** (`qwen3.8-27b-w4a16`). The
  reviewer route already addresses `openai/qwen3.8-flash-next` by name, so changing the default is
  unnecessary for routing — and it would not dedicate the host anyway, since named requests for
  W4A16, FP8, the 122B or the coder model still evict. Measure shared-host latency first; consider
  an agreed review window or separate capacity only if measurements justify it. The 83.8 GiB
  checkpoint size is not a latency measurement.
- **Day-only backends.** cloud1/2/3 power off nightly (RTC wake ~08:00). Define a *checkpointed
  unavailable* state and who resumes it; do not infer automatic wake/resume from continuable mode.
- **No-cancel.** LiteLLM 1.91.0 does not propagate client disconnects, so a request keeps decoding
  after the client gives up. Both backends now run ≥2 slots — cloud3's Flash-Next is
  `-c 524288 --parallel 2`, 262144 tokens per slot, matching its advertised 229376 input limit (the
  stale `litellm.yaml` comment claiming `--parallel 1` is corrected on
  `fix/flash-next-ctx-comment`). Two slots bound one orphan, not repeated ones: set server-side
  completion limits and a retry policy that accounts for requests still decoding after a client
  timeout. cloud2's eight sequences are a backend capacity, **not** a team-wide concurrency budget.
- **HTTP 200 is not health.** cloud2 has a recorded fault where the engine served well-formed 200s
  containing only token 0. Apply content validation to **both** children — reject empty, truncated
  or degenerate output and require a valid verdict tied to the *current* artifact. Non-empty prose
  is not evidence of a completed review or a passed test. Do **not** use
  `response_format: json_schema` for the health probe: the route comments warn about
  structured-output/tool-call interactions. Exercise real dsh tool use and validate ordinary
  returned text locally.
- **Bound spend and elapsed time per delivery**, including reasoning tokens, retries and unchanged
  polls. A small conductor token fraction is not a spend bound and can coexist with a runaway
  worker. Let the conductor read the minimum authoritative diff/check evidence it needs — a blanket
  ban on file reads would make it depend entirely on worker summaries.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/apps/dsh/agent-teams/team-delivery.agent.cordis.yml` | **new** — plugin list, two routed spawn rows |
| `kubernetes/apps/apps/dsh/agent-teams/team-delivery.preset.yml` | **new** — name/description/**order 40** |
| `kubernetes/apps/apps/dsh/kustomization.yaml` | **must be edited** — its `configMapGenerator` enumerates both files for every team explicitly. `test_agent_teams.py` already asserts this per team, so forgetting it fails the test rather than shipping a team that mounts nothing |
| `scripts/tests/test_agent_teams.py` | **must be edited** — `DELEGATION_ROWS` and `REQUIRED_BOUNDS` are keyed by row id and have no `tool-subagent-reviewer`, so its `maxDepth` would go unasserted. Add it, and gate the pinned reviewer route, `modelSelectionSettings: false` on both rows, and the absence of the unintended delegation rows |
| `kubernetes/apps/apps/dsh/deployment.yaml` | confirm the seed-settings loop projects the new pair |
| `docs/runbooks/dsh.md` | document the team, the two-part selection, the write-grant prerequisite and the loop's termination conditions |
| `kubernetes/apps/apps/ai/litellm.yaml` | reference — routes, fallbacks, and the day-only / no-cancel / 200-is-not-health constraints |

## Verification

1. **Both tools mount on the deployed package.** Resolve and mount the candidate against the
   deployed dsh version (0.1.5-alpha.2 was not inspectable at review time), and inspect **stderr**
   as well as the roster and exposed tools — the runbook warns `dump-config` can exit zero with
   loader errors. Run `test_agent_teams.py` and render the dsh kustomization. Confirm both tool
   names appear, across **two parent sessions**.
2. **Routing is actually split.** Dispatch one `subagent` and one `subagent_reviewer` task and
   correlate **child IDs and resolved routes** with gateway/backend requests — aggregate counters
   alone are ambiguous on a shared host, and one agent task may issue several requests. Cover both
   initial calls and continuations, and account explicitly for LiteLLM fallback execution. This is
   the test that catches the inheritance trap.
3. **Route overrides are rejected.** Attempt a caller-supplied provider/model override on both
   tools and confirm rejection.
4. **Depth and ownership.** Test rejection beyond depth 2 through both tool names, role
   continuation after resume, and independent ownership across parent sessions.
5. **Shared-host behaviour, measured properly.** Five uncontended calls prove nothing. Verify which
   deployed llama-swap endpoint actually reports *loaded* state (`/v1/models` may list availability,
   not residency), then measure cold and warm review latency on a representative review prompt,
   long-prompt prefill, an **intervening named-model request**, and the worker-fallback path —
   corroborated with process/load logs and GPU state. These numbers inform any later
   host-allocation decision.
6. **Fallback behaviour is explicit.** Force the worker route to fail and confirm delivery either
   reports the fallback model or pauses — and observe whether the 122B fallback evicted the reviewer.
7. **Degenerate output is caught.** Use a stubbed endpoint or controlled fault injection — not a
   prompt, which is not a deterministic fixture — covering empty content, HTTP-200 repeated-token
   output, truncation, malformed verdicts and tool errors, and confirm the step fails rather than
   passing.
8. **End-to-end (Stage 2 only).** Against a scratch repo provisioned with the intended grants,
   branch protection, CI and reviewer-bot subscription — the documented dsh account cannot create
   repositories, so the operator provisions it. Prove: the plan is reviewed before coding; a review
   for an older SHA cannot approve a newer head; duplicate feedback is actioned once; only the
   designated forge actor merges, after checks pass on the current head; and a run that ends
   waiting because no merge bot is configured is reported as a normal outcome.
9. **Failure injection, not shared-host disruption.** Use an isolated failing endpoint rather than
   powering off shared cloud3. Cover cloud2 failure and fallback, API 401/403/429/5xx, a closed PR,
   cap and deadline exhaustion, stop with work in flight, and a host or session restart between a
   forge write and its checkpoint. Assert no duplicate forge writes and no timeout-driven request
   pile-up.

<!-- codex-review-status: round-1 addressed; pending round 2 -->
