# dsh conductor workflow: plan → implement → review → PR → iterate to merge

## Context

The operator wants a dsh team that runs a delivery loop with three differently-routed models:

| Role | Model | LiteLLM route | Backend |
|---|---|---|---|
| Conductor | GPT-6 Astra | `gpt-6-astra` | paid, external |
| Worker (implement, tools, research, PR) | Qwen3.8-27B-W4A16 | `qwen3.8-27b-vllm-cloud` | cloud2 `192.168.0.28:18020`, dedicated vLLM TP=4 |
| Reviewer (plan **and** implementation) | Qwen3.8-Flash-Next | `qwen3.8-flash-next-cloud` | cloud3 `192.168.0.26:8080`, llama-swap |

Flow: **Plan** (conductor) → **plan review** (reviewer) → **Implement** (worker) → **impl review**
(reviewer) → **PR** (worker) → address feedback (worker implements, reviewer re-reviews) → iterate
until the forge reviewer agents merge.

Note the plan-review gate before implementation: the operator asked for the reviewer to review
"plan and worker implementation", and the first draft of this plan omitted the plan half.

**Half of the delegation already exists.** `team-conductor` (preset "Conductor", order 20) is
conductor-on-Codex delegating to a qwen worker. The new parts are a second, differently-routed
child for review, and the PR lifecycle.

### Verified against the running estate (2026-09-15)

Stated here so the rest of the plan is not built on inherited assertions:

- **Deployed dsh is `0.1.5-alpha.2`** (install job `dsh-install-0-1-5-alpha-2-glibc-ps1`); pod
  `dsh-6dd8f97897-cnq2k`, 2/2 Running.
- **`/running` is the endpoint that reports residency**, not `/v1/models`. cloud3 answered
  `/v1/models` with six models and `/running` with exactly one. This settles the round-1 question of
  which endpoint exposes loaded state.
- **The live Flash-Next process confirms the flags**: `-c 524288 --parallel 2`, `--mmproj
  mmproj-BF16.gguf`, `--image-min-tokens 1024 --image-max-tokens 2240`. 524288/2 = 262144 per slot;
  262144 − 32768 output headroom = the advertised `max_input_tokens: 229376`.
- **The worker→reviewer eviction path is real, not theoretical**: `qwen3.8-27b-vllm-cloud` →
  `192.168.0.28:18020` (cloud2, alive, serving `qwen3.8-27b`); its first fallback
  `qwen3.5-122b-cloud` → `192.168.0.26:8080`, which *is* cloud3's llama-swap. One model resident at
  a time, so the fallback evicts the reviewer by construction.
- **The deployed ConfigMap `dsh-relay-mk4h7d69bt` carries exactly three team pairs**
  (conductor/review/solo, two files each), confirming that team files are enumerated, not globbed.
- **No pooled deployments exist today** — every `model_name` in `litellm.yaml` is unique, so
  `routing_strategy: least-busy` currently has no multi-deployment model to act on. See the warning
  under *Interaction with the pending W4A16 routing question* below.

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
identity, and the documented OpenBao patch/rotation verification.

**2. There is no defined handoff contract with the forge reviewer agents.** `docs/runbooks/
agentforge.md` documents a forge lifecycle with distinct bot identities but does not establish that
a PR opened by dsh will be picked up at all. The contract the operator must fill in, field by field:

| Field | Must state |
|---|---|
| Watcher identity | which bot account reviews PRs in which repositories |
| Trigger | PR opened / label applied / push to an existing PR branch |
| Required checks | which CI jobs must pass, on which ref |
| Required approvals | how many, from which identities |
| Merge authority | who merges, by what method (this repo squash-merges) |
| No-bot behaviour | a run that ends waiting because nothing is subscribed is a **normal outcome**, reported as such, not a failure |

## Approach — three independently shippable stages

### Stage 1 — Routing spike + review, no forge writes

Prove the two-route topology and the review role. Nothing here needs new credentials: Stage 1 makes
no REST calls and no pushes, and repository reads already work through the existing helper.

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

Preset `order: 40`. **Not 30** — `team-review.preset.yml` already holds 30 (20 conductor, 10 solo),
and two teams at one order have no defined placement.

`modelSelectionSettings: false` on **both** rows is load-bearing, not tidiness. The inherited
worker row has it `true`, which lets caller-supplied provider/model fields override the configured
route — silently putting the worker back on the paid conductor model. Each enabled instance also
registers the fixed tool name `list_subagent_models` regardless of `toolName`, so two enabled
instances collide.

**Close every other paid-inheritance path, not just the two spawn rows.** `subagent_fork` and
`tool-ralph` stay dropped (neither can be re-aimed). The copy also carries `workflow-worker-thread`
and `tool-workflow`, whose `agent()` routes come from each workflow script and otherwise inherit
Codex — drop both from v1.

Only one enabled spawn row exists in `team-conductor` today (the Codex and Claude Code rows are
`disabled: true` and use different providers), so two live spawn consumers is a **candidate, not a
demonstrated pattern**. Keep one shared subagent-control registration; do not register a second
control registry.

### Stage 2 — Authenticated PR operations (blocked on prerequisites)

Once write grants and a `write:repository` PAT exist, and the handoff contract above is filled in,
add the PR step. **One explicit delivery/update invocation**, ending at a PR link or a recorded
waiting/blocked checkpoint. No unattended polling in this stage.

**REST needs its own wrapper.** `agents.seed.md` states the credential helper does not cover REST —
`curl` never invokes Git's credential helper — so a working `git push` proves nothing about API
authority. Note that **`scripts/forge.sh`, which `CLAUDE.md` names as the forge tool, does not
exist in this repository**; only `af-db.sh`, `node-ssh.py` and similar are present. So Stage 2 must
either locate it in a sibling repo or create it. Required shape:

- a single entry point, e.g. `scripts/forge-api.sh <method> <api-path> [body.json]`
- reads the PAT **internally at call time**; never accepted as an argument or echoed
- hard-pins the host to `git.chifor.me`; refuses any other destination
- emits only the response body plus an exit status — no headers, no token, in stdout or logs
- covers exactly the verbs delivery needs: create PR, list PR comments, post comment, read checks

### Stage 3 — Unattended iteration (separate design)

Only after Stages 1-2 are proven. A continuable child is **not** a durable PR watcher. This stage
needs its own design comparing an authenticated webhook consumer plus reconciliation polling
against existing AgentForge integration. Resume must re-read forge state and avoid duplicate PRs,
comments and fixes. Poll with deterministic code; invoke models only on actionable changes.

### Team instructions — where they actually live

There is no separate instruction file. The team-scoped instruction source is the **`persona` row
(`@deepseek-ai/dsh-persona`) inside `team-delivery.agent.cordis.yml`**, which carries `prefix` and
`suffix` strings, plus the **`agent-instructions` row** (`@deepseek-ai/dsh-agent-instructions`,
`maxBytes: 65536`) that reads the repo's own agent instruction file.

The important constraint: **the persona row is shared by every agent in the team** — its
`{{model}}` and `{{cwd}}` placeholders render per-agent, but the text is one string. It therefore
cannot carry per-role instructions. So:

- **The persona** describes the protocol visible to everyone: the stages, the gates, that reviews
  must cite a SHA, and that only the implementer writes to the checkout.
- **Per-child role assignment goes in the spawn-time brief** — the only per-child text channel.
  Every fresh spawn gets a self-contained brief; a new child inherits no context.
- **The `agent-instructions` budget is for durable repo conventions**, not delivery protocol.

Rules the persona must state:

- **One writer per checkout.** The conductor header says all children share `cwd`. A different
  model is not workspace isolation and not a separate forge approval identity. The reviewer must
  not modify the implementation.
- **Every review names the exact plan revision or commit SHA** it reviewed, plus findings, a
  verdict, and validation evidence. A review of an older SHA cannot approve a newer head.
- **Preserve the copied plan-mode approval gate.**

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

## Decisions

Round 1 named these as open. They are decided here, because a plan that restates a requirement has
not resolved it.

### D1 — On fallback: **detect and pause.** Do not continue.

The worker route's first fallback lands on the reviewer's host (verified above), so a silent
fallback both changes the worker's model *and* evicts the reviewer. Continuing would make the
three-model topology unprovable.

Every worker call compares the served model against the pinned route; on mismatch the delivery
**stops, checkpoints and reports** — it does not retry into the fallback.

**Open, and must be resolved in Stage 1 before this is relied on:** whether the response's `model`
field reports the *served deployment* or merely echoes the requested name. If it echoes, use the
`x-litellm-model-id` response header; if neither distinguishes, detection must come from the
backends' own counters. Until one of these is shown to work, fallback detection is unimplemented —
not merely unwritten.

### D2 — On day-only backends: **manual resume for Stages 1-2.**

The conductor writes a checkpoint after every stage transition containing: stage, repo, PR number,
branch head SHA, worker child ID, reviewer child ID, processed feedback IDs, round count, next
action, timestamp. It lives in the delivery working tree and is **never committed to the PR
branch**.

When a backend is unavailable, delivery writes `state: unavailable` and **stops**. It does not
retry into the night. **The operator resumes**, by re-invoking delivery against the checkpoint.
No automatic wake/resume in Stages 1-2; Stage 3 may revisit this.

### D3 — Bounds. Starting values, to tune after the first real delivery.

| Bound | Stage 1 | Stage 2 |
|---|---|---|
| Delivery wall-clock deadline | 60 min | 120 min |
| Repair rounds | 3 | 3 |
| Reviewer invocations per delivery | 6 | 6 |
| Worker tasks spawned per delivery | 8 | 8 |
| Active children | 1 implementer + 1 reviewer | same |

The estate's `max_budget: 50 USD / 30d` is a **global** third-party spend guard shared by every
consumer — it is not a per-delivery bound and must not be cited as one. Conductor tokens are the
paid component; reasoning tokens, retries and unchanged polls all count against the deadline.

### D4 — Content validation, both children. Reject if any holds:

- content is empty;
- `finish_reason == "length"` **with** empty content — the documented reasoning-budget failure,
  which is not a vision or routing fault and must not be reported as one;
- output is degenerate by distinct-token ratio — the 2026-09-10 cloud2 fault served well-formed
  HTTP 200s containing only token 0;
- a review does not name the SHA it reviewed, or names one that is not the current head.

Validate **ordinary returned text**. Do not use `response_format: json_schema` for the health
probe: the route comments warn about structured-output/tool-call interactions, so a schema failure
would be indistinguishable from an unhealthy engine. Exercise real dsh tool use separately.

### D5 — Override rejection must be an explicit error.

A caller-supplied provider/model override must fail **at spawn time with a visible error in the
tool result**. A silent no-op is a failing outcome, not a passing one — it is indistinguishable
from an override that was accepted and ignored.

## Constraints

- **Keep cloud3's default and preload as the operator set them** (`qwen3.8-27b-w4a16`). The
  reviewer route already addresses `openai/qwen3.8-flash-next` by name, so changing the default is
  unnecessary for routing — and it would not dedicate the host anyway, since named requests for
  W4A16, FP8, the 122B or the coder model still evict. The 83.8 GiB checkpoint size is not a
  latency measurement. Measure first; consider an agreed review window or separate capacity only if
  measurements justify it.
- **Interaction with the pending W4A16 routing question.** The operator earlier asked how best to
  serve W4A16 across cloud2 and cloud3 — pooling both under one `model_name` so `least-busy` can
  balance them. That change is **not** applied today (no duplicate `model_name` exists), and it
  interacts badly with this plan: pooling would let ordinary W4A16 traffic land on cloud3 and evict
  Flash-Next, turning reviewer eviction from an exceptional fallback into routine behaviour. If
  pooling proceeds, the reviewer needs dedicated capacity or an agreed window first. These two
  decisions must be taken together.
- **No-cancel.** LiteLLM 1.91.0 does not propagate client disconnects, so a request keeps decoding
  after the client gives up. Two slots bound *one* orphan, not repeated ones: set server-side
  completion limits and a retry policy that accounts for requests still decoding after a client
  timeout. cloud2's eight sequences are a backend capacity, **not** a team-wide concurrency budget.
- **`fix/flash-next-ctx-comment` must land before Stage 1 ships.** It corrects `litellm.yaml`'s
  stale `--parallel 1` comment to the `-c 524288 --parallel 2` the host actually runs. The branch is
  pushed and its one commit touches one file; it is not yet merged.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/apps/apps/dsh/agent-teams/team-delivery.agent.cordis.yml` | **new** — plugin list, two routed spawn rows, and the `persona` row carrying the delivery protocol |
| `kubernetes/apps/apps/dsh/agent-teams/team-delivery.preset.yml` | **new** — name/description/**order 40** |
| `kubernetes/apps/apps/dsh/kustomization.yaml` | **must be edited** — its `configMapGenerator` enumerates both files for every team explicitly. `test_agent_teams.py` already asserts this per team, so forgetting it fails CI rather than shipping a team that mounts nothing |
| `scripts/tests/test_agent_teams.py` | **must be edited, and this is CI-blocking.** `DELEGATION_ROWS` and `REQUIRED_BOUNDS` are keyed by row id and have no `tool-subagent-reviewer`, so its `maxDepth` would go unasserted. Add it, and gate the pinned reviewer route, `modelSelectionSettings: false` on both rows, and the absence of the dropped delegation rows |
| `kubernetes/apps/apps/dsh/deployment.yaml` | confirm the seed-settings loop projects the new pair |
| `docs/runbooks/dsh.md` | document the team, the two-part selection, the write-grant prerequisite and the loop's termination conditions |
| `kubernetes/apps/apps/ai/litellm.yaml` | reference — routes, the `fallbacks` block, and the day-only / no-cancel / 200-is-not-health constraints |

## Verification

1. **Both tools mount on the deployed package.** Resolve and mount the candidate against dsh
   `0.1.5-alpha.2`, and inspect **stderr** as well as the roster and exposed tools — the runbook
   warns `dump-config` can exit zero with loader errors. Run `test_agent_teams.py` and render the
   dsh kustomization. Confirm both tool names appear, across **two parent sessions**.
2. **Routing is actually split.** Dispatch one `subagent` and one `subagent_reviewer` task and
   correlate **child IDs and resolved routes** with gateway/backend requests — aggregate counters
   alone are ambiguous on a shared host, and one agent task may issue several requests. Cover both
   initial calls and continuations. This is the test that catches the inheritance trap.
3. **Route overrides are rejected** per D5 — explicit error at spawn time, not a silent no-op.
4. **Depth and ownership.** Test rejection beyond depth 2 **through each tool name separately**,
   role continuation after resume, and independent ownership across parent sessions.
5. **Shared-host behaviour, measured properly.** Five uncontended calls prove nothing. Use
   `/running` for residency (`/v1/models` lists availability — verified above). Measure cold and
   warm review latency on a representative review prompt, long-prompt prefill, an **intervening
   named-model request**, and the worker-fallback path — corroborated with process/load logs and
   GPU state. These numbers inform any later host-allocation decision.
6. **Fallback detection works at all** (D1's open question), then that delivery pauses on a forced
   worker-route failure — and observe whether the 122B fallback evicted the reviewer.
7. **Degenerate output is caught.** Use a stubbed endpoint or controlled fault injection — not a
   prompt, which is not a deterministic fixture — covering empty content, HTTP-200 repeated-token
   output, truncation, malformed verdicts and tool errors.
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
10. **Checkpoint/resume (D2).** Kill delivery mid-stage, resume from the checkpoint, and confirm it
    neither reruns a completed stage nor loses the child IDs.

<!-- codex-review-status: rounds 1-2 addressed; converged -->
