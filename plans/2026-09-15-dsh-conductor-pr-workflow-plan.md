# dsh conductor workflow: plan → implement → review → PR → iterate to merge

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

### 3. The PR loop

The worker already has forge access: the shell can clone/fetch/push `https://git.chifor.me/...`
via the OpenBao-backed git credential helper (`af/dsh/credentials` → `GITEA_USER`/`GITEA_PAT`),
with no token handed to the model. So "open a PR" and "read PR comments" are `bash` + `curl`
against the Gitea API, not new tooling.

Iteration uses `backgroundMode: continuable` (already set on the worker row): the conductor
re-enters the same child session with new feedback rather than spawning a fresh one, preserving
context and prefix cache.

**The loop is conductor-driven, not a daemon.** Each cycle is: conductor asks the worker to poll
the PR → worker returns state (checks, review comments, mergeable) → conductor decides → dispatches
worker (fix) and/or reviewer (re-review) → repeat. Termination conditions must be explicit:
merged, closed, or a round cap.

### 4. Ship it as `team-delivery`, leaving `team-conductor` untouched

Add `team-delivery.agent.cordis.yml` + `team-delivery.preset.yml` (order 30) alongside the
existing three teams. `team-conductor` keeps working for people who want plain conduct→work.

The preset description must repeat the warning the Conductor preset already carries: **selecting
the team is half the job — the session model must also be set to `gpt-6-astra`**, or the conductor
runs on the `agent-default-model` (`litellm/qwen3.8-27b-vllm-cloud`) and it is qwen conducting qwen.

## Constraints this plan must respect

- **The inheritance trap.** Spawn children inherit the parent route unless overridden, so on a
  Codex conductor an unconfigured child is *another Codex agent*, billed accordingly. Every
  delegation row must be explicitly routed or dropped. `subagent_fork` must stay **dropped** (it
  preserves the parent route by design, for KV reuse — it cannot be re-aimed), and `tool-ralph`
  must stay **dropped** (its config exposes no route knob at all).
- **Day-only backends.** cloud1/2/3 power off nightly (RTC wake ~08:00). A PR-watch loop that
  spans the shutdown will fail on both worker and reviewer. The loop needs to fail soft and resume,
  not hang.
- **No-cancel.** LiteLLM 1.91.0 does not propagate client disconnects, so an abandoned request
  keeps decoding. Both backends now run ≥2 slots (cloud2 `--max-num-seqs 8`, cloud3 flash-next
  `--parallel 2`), so an orphan costs capacity rather than blocking the model.
- **HTTP 200 is not health.** cloud2 has a recorded fault where the engine served well-formed
  200s containing only token 0 (`!`). A review step that accepts any 200 as success will happily
  consume garbage; the loop should sanity-check review output is non-empty and non-degenerate.
- **`maxDepth: 2`.** Conductor → worker is depth 1; anything the worker spawns is depth 2. The
  reviewer is a sibling of the worker (both depth 1), not nested, so the budget holds — but a
  worker that itself delegates consumes the remaining depth.
- **Cost.** `gpt-6-astra` is paid and the conductor runs every turn. Keep the conductor's job to
  routing and decisions; do not let it do research or file reading that the worker can do.

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

## Verification

1. **Routing is actually split.** Start a session on `team-delivery` with the session model set to
   `gpt-6-astra`. Dispatch one `subagent` and one `subagent_reviewer` task. Confirm on the
   backends, not in the UI: cloud2's `/metrics` request counter moves for the worker call, cloud3's
   for the reviewer call. This is the test that catches the inheritance trap — if a child silently
   ran on Codex, neither counter moves.
2. **No swap thrash.** With cloud3 dedicated, run five consecutive review calls and confirm
   `/v1/models` shows `qwen3.8-flash-next` stays `loaded` throughout and VRAM does not drop to 3 MiB
   between calls.
3. **End-to-end on a throwaway repo.** Run the full loop against a scratch Gitea repo: conductor
   plans, worker implements and opens a PR, reviewer reviews, worker addresses a deliberately
   planted review comment, loop terminates when merged.
4. **Failure modes.** (a) Power off cloud3 mid-loop and confirm the loop reports a clear failure
   rather than hanging. (b) Point the reviewer at a prompt that returns an empty body and confirm
   the loop does not treat it as a passed review.
5. **Cost sanity.** After one full loop, check the conductor's token usage against the worker's.
   The conductor should be a small fraction; if it dominates, work has leaked upward.

<!-- codex-review-status: pending -->
