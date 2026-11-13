# Subagent model routing for dev-worker agents (Claude Code and Codex)

## Context

On 2026-11-13 the owner asked for "the right agent for the right task" on **all dev workers and all
repositories**: subagents should run on a model and effort that fit their job, instead of every subagent
running on the main session's model. The research behind this plan (Anthropic's specifications and the web,
September to November 2026) is summarised under § Evidence. Its main conclusion is that **whether to delegate
at all, and at what effort, matters more than which model a subagent uses**. A cheaper subagent model pays
off only for some jobs, so the routing has to be deterministic and narrow.

**The fleet today** (dev-worker-1..4; read on dev-worker-4 on 2026-11-13, role defaults on `origin/main`
69851ab2):

- **Claude Code** is at or above the role's floor (`dev_worker_claude_code_min_version: "2.1.311"`; dw4 runs
  2.1.318). Each user picks the main model and effort: dw4 has `"model": "opus[1m]"` in
  `~/.claude/settings.json` and runs at `xhigh`. The role sets neither.
  - There are no estate agents and no `CLAUDE_CODE_SUBAGENT_MODEL` or `CLAUDE_CODE_EFFORT_LEVEL` anywhere
    (managed settings, user settings, shell profiles, running processes; checked on all four workers
    2026-11-13). dev-worker-2 has one user agent of its own, `~/.claude/agents/pr-shepherd.md` (`model: sonnet`,
    written by its agent 2026-11-12); the role must never touch it. Without a definition or a per-call model,
    a subagent runs on the main session's model.
  - The built-in `Explore` runs on whatever the `opus` alias resolves to when the session is on a
    subscription. `Plan` and `general-purpose` inherit the main model.
  - `/etc/claude-code/managed-settings.json` carries only `BASH_ENV` (`templates/claude-managed-settings.json.j2`).
- **Codex** is at or above the role's floor (`dev_worker_codex_version: "0.162.0"`; dw4 runs 0.166.0).
  - `model = "gpt-6-astra"` and `model_reasoning_effort = "xhigh"` are pinned as top-level keys by
    `tasks/codex.yml`, with the `llm-router` provider.
  - `features.multi_agent` is stable and on; `multi_agent_v2` is off (`codex features list`).
  - There is no `[agents]` table and no agent roles.
  - Codex's `spawn_agent` accepts only models in its built-in catalog (comment in `tasks/codex.yml`; codex 0.166:
    "Unknown model `qwen-27b` for spawn_agent").
- **Cost** is the subscription's **weekly usage limits** (`weekly_all`, plus a `weekly_scoped` entry for
  Fable; ADR 0025), not dollars. Claude Code's docs say subagents "count toward the same usage limits as your
  main conversation". Codex runs `gpt-6-astra` through llm-router: its limits are the router's ChatGPT seats,
  not Claude's `weekly_all`.
- **Instruction files**: the role already writes managed blocks into `~/.claude/CLAUDE.md` and
  `~/.codex/AGENTS.md` with `blockinfile` and HTML-comment markers (`tasks/openbao.yml`, `tasks/codex.yml`).
  It installs skills by copying files into `~/.claude/skills/` (`tasks/claude_code.yml`, tag `skills`).
- **Repositories**: org `cchifor` has 27 repositories. No checkout on dw4 has a `.claude/agents/` or
  `.codex/agents/` directory, and no `CLAUDE.md` or `AGENTS.md` within three levels mentions delegation
  (`find`, 2026-11-13).
- **Phase 0 baseline, measured 2026-11-13** (`agent-usage-report --days 14` on each worker; merged PRs by
  author from the Gitea API; raw output in `/workspace/archive/2026-11-subagent-baseline/` on each worker):

  | worker | main sessions | subagent launches | model passed per call | tokens in subagents (all / output) | merged PRs |
  |---|---|---|---|---|---|
  | dev-worker-1 | 16 | 29 (24 general-purpose, 5 Explore) | opus 14, fable 11 | 57% / 66% | 0 |
  | dev-worker-2 | 8 | 486 (456 general-purpose, 22 fork, 5 Explore, 3 Plan) | opus 138, fable 76, sonnet 17 | 83% / 91% | 216 |
  | dev-worker-3 | 29 | 9 (general-purpose) | none | 46% / 59% | 147 |
  | dev-worker-4 | 9 | 0 | none | 0% | 0 |

  The first draft's single data point (dw4, 0 calls) was not representative. Launches equal the subagents that
  ran on every worker (counted per `tool_use` id, and from each subagent's `.meta.json`). Seats: dw2 and dw4
  report identical limits (`weekly_all` 78%, the Fable-scoped limit 87%, same reset), so they share one
  account; dw1 and dw3 had hit their weekly limit (the usage endpoint answered 429).

## Evidence (summary)

**Anthropic, "Optimizing for cost and intelligence"** (current; it covers Fable 5.2). Anthropic's internal
runs, which it calls directional:

- **Starting point.** Start with Opus 5.5 at its default `medium` effort. Use Fable 5.2 ($8/$40 per million
  tokens) only when Opus 5.5 at higher effort still falls short. Use Haiku 5.5 for high-volume work with
  checkable outputs.
- **Cost per solved task on a SWE-bench Pro subset:**

  | Model and effort | Solved | Cost per task |
  |---|---|---|
  | Sonnet 5.5, `high` | 87.6% | $0.11 |
  | Opus 5.5, `low` | 87.4% | $0.12 |
  | Opus 5.5, `medium` | 92.8% | $0.22 |
  | Fable 5.1, default | 92.3% | $1.19 |
- **Coordinator with cheaper workers.** This paid off in two cases:
  - work larger than one context window, where Opus 5.5 with Sonnet 5.5 subagents matched Opus 5.5 alone for
    about a third less;
  - routine work with a long cost tail.

  It did not pay for one dependent chain of work, or for work that fits in one context. There the
  coordinator's model alone, at lower effort, did better. Fable 5.2 as the coordinator cost 19% more for the
  same score.
- **Haiku subagents on coding.** Adding them to Opus 5.5 `medium` cut cost per attempt by 32% and lost about
  4 points of pass rate. That is roughly what mixing `low` and `medium` effort gives with no subagents.
- **Advisor.** A cheaper executor that consults a stronger model scored +1.7 points for 2.1× the cost on
  coding. It pays only when the executor consults often.
- **Effort.** Running at `low` and re-running the failures at `high` solved about 97% at $0.17 per task,
  against 95.3% at $0.29 with everything at `high`.

**Anthropic, "When to fold subagents into your main agent loop"** (2026-10-22):
- Use fewer subagents. Split work by context, not by role.
- One customer ran about 2× faster with a single agent loop.
- Test with about 10 golden tasks: fold one subagent back into the main loop at a time, and keep it only if the
  folded version scores worse.

**Anthropic, "When to use multi-agent systems"** (2026-01): multi-agent designs typically use 3–10× the tokens
of a single agent.

**Anthropic model guidance (Claude API skill, `model-migration.md`):**
- The Opus 5 line delegates readily, which multiplies cost. Anthropic recommends an explicit delegation policy,
  and says verification belongs in the main loop, not in a subagent.
- Fable 5.1 is the exception: it delegates dependably, and Anthropic recommends letting it delegate
  asynchronously.

**Claude Code docs, "Subagents" (current):**
- The subagent model is resolved in this order:
  1. the per-call `model` parameter;
  2. the definition's `model` frontmatter (`inherit` = the main model);
  3. `CLAUDE_CODE_SUBAGENT_MODEL`;
  4. the main model.
- `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` (2.1.257+) makes Claude Code ignore both the frontmatter `model` and
  the per-call `model`: subagents then run on `CLAUDE_CODE_SUBAGENT_MODEL` if set, else on the main model.
- Frontmatter accepts `effort` (`low`..`max`). Claude can also pass `effort` per call (2.1.292+), which
  overrides the frontmatter; `CLAUDE_CODE_EFFORT_LEVEL` overrides both.
- Same-named agents: managed settings, then `--agents`, then `.claude/agents/` (project), then
  `~/.claude/agents/` (user), then plugins. Verified 2026-11-13 on 2.1.322: with a `sonnet` user agent and a
  `haiku` project agent of the same name, the project's ran on Haiku.
- `/agents` in current builds only prints where agent files live; it does not list agents or models.
  Verification uses the transcripts (`message.model`, and `agentType` in each subagent's `.meta.json`).
- A user or project agent named `Explore` replaces the built-in.
- Up to 20 subagents run concurrently, nested up to 3 deep.

**arXiv 2609.28919, "Harness Tokenomics"** (2026-09-26; a **simulated** 10,000-seat company):
- The prompt cache belongs to one model, so pick a model only at session start, at subagent launch, or in a
  separate side session. Never switch inside a running conversation.
- Picking a model per subagent at launch was a small part of the simulated saving (11.2% → 13.4%).
- Expired 5-minute caches made 17–27% of turns pay for the whole conversation again.

**Codex:**
- The config reference documents `agents.default_subagent_model`, `agents.default_subagent_reasoning_effort`,
  `agents.<name>.description` and `agents.<name>.config_file`. All of these strings are in the codex 0.166.0
  binary.
- Users report that Multi-Agent V2 stopped `spawn_agent` from taking a model and effort on some models.
  OpenAI support acknowledged this on 2026-10-11 and pointed to running separate `codex exec` processes with the
  model pinned.

## Decisions

**D1. Routing lives in the agent definitions.**
- The main agent chooses *which* agent from its `description`; the definition pins `model` and `effort`.
- The delegation policy tells the main agent not to pass `model` or `effort` per call unless the user asks,
  because a per-call value overrides the frontmatter.

**D2. Two estate agents for Claude Code:**
- `scout` (Haiku, effort `low`) for searches whose raw output the main session will not reuse. Its tools are
  `Read, Grep, Glob` and nothing else: no shell, so "read-only" is enforced by the tool set rather than by the
  prompt (review of this plan: a `Bash` tool would allow `git commit`, `rm` and installs). A question that
  needs a command (git history, a journal) goes back to the main session.
- `bulk-worker` (Sonnet, default effort) for one mechanical change across many files with an exact rule and a
  check. This is the "larger than one context" case where Anthropic measured a saving.

Everything else (design, non-mechanical implementation, debugging conclusions, review) stays on the main
model.

**D3. No fleet-wide `CLAUDE_CODE_SUBAGENT_MODEL`, and never `_FORCE`.** The first would move `general-purpose`
and `Plan` subagents, which do implementation and review, onto a cheaper model; Anthropic's coding numbers say
that costs quality. `_FORCE` would also override the estate definitions themselves. A test asserts that the
role's templates, tasks and defaults never set either variable, and the fleet must not set
`CLAUDE_CODE_EFFORT_LEVEL`, which would override the definitions' `effort`.

**D4. A delegation policy block in both instruction files.**
- It is a managed block in `~/.claude/CLAUDE.md` and in `~/.codex/AGENTS.md`, adapted from Anthropic's
  sample "Delegating to subagents" prompt.
- It says delegate rarely, never use a subagent to verify your own work, brief once, and keep spawn counts
  low.
- It is a block, not a whole file, so the operator's own notes and the other managed blocks survive.

**D5. The main session's model and effort are not changed.** They are each user's choice
(`~/.claude/settings.json`, `/model`, `/effort`). Anthropic suggests starting Opus 5.5 at `medium`, while
the fleet runs `xhigh`. Changing that is a separate owner decision (Phase 3.3).

**D6. Codex parity is gated on a spike.**
- Codex gets the policy block in Phase 1.
- `[agents]` defaults and a `scout` role ship only after Phase 0.3 shows, on the fleet's Codex version and the
  router provider, that a role's `model` and `model_reasoning_effort` are actually applied. Until then they sit
  behind `dev_worker_codex_subagent_roles_enabled: false`.
- The router and `spawn_agent` must also both accept a cheaper model.

**D7. Repositories inherit the estate default.**
- User-level files cover every repository a worker opens.
- A repository adds project agents only to specialise. A project agent with an estate name (`scout`,
  `bulk-worker`) overrides the estate file for that repository, deliberately.
- Repositories do not add "always delegate" or "verify with a subagent" guidance. Phase 3.4 audits the 27
  repositories.

**D8. Measure weekly-limit burn per completed task, not tokens per request.** Phase 2's acceptance compares
the canary's subscription usage per merged PR and its review outcomes against its own baseline.

## Scope

**In scope**
- The `dev_worker` role: Claude Code agents, policy blocks, tests and docs.
- The Codex `[agents]` table and roles after the spike.
- The repository audit and standard.

**Out of scope**
- The main-session model and effort (D5).
- The reviewer VMs (`pr_reviewer`; they review through llm-router with their own model ladder).
- dsh and llm-router routing.
- `claude-job@` units: they run `claude -p --allowed-tools <list>`, so subagents apply only if a job's
  `allowed-tools.txt` lists `Agent`. They do load `~/.claude/CLAUDE.md`, so the policy block (about 300
  tokens est.) reaches every job; Phase 2 checks one unit's token count before and after.
- Credentials, VMs, IPs, RAM and network.

## Authority

The owner requested this on 2026-11-13, for all dev workers and all repositories.

`plans/2026-11-12-dev-worker-cleanup-gc-reflog-and-link-guard-plan.md` records that dev-worker infrastructure
is **frozen until Relay workers are validated**. This plan changes only per-user agent definition files and
managed instruction blocks. It makes no VM, IP, RAM, threshold or credential change, but it is still a
dev-worker converge. **The implementer confirms with the owner that this request is an exception to the freeze
before converging any worker**, and records that confirmation in the PR.

**Confirmed 2026-11-13.** The owner, to the operator: "Analyze, validate, implement and apply the following plan:
https://git.chifor.me/cchifor/ailab/pulls/1744". That is the exception for this plan's scope (agent files,
managed instruction blocks, the report script), nothing wider.

## Phase 0: baseline and spikes (read-only)

**0.1 Baseline, per worker, over the last 14 days.** Write a read-only report script,
`ansible/roles/dev_worker/files/agent-usage-report`, as the first commit of the Phase 1 PR. Run it from the
branch before converging anything. It reads `~/.claude/projects/**/*.jsonl` (main transcripts) and
`<session>/subagents/*.jsonl` (subagent transcripts) and prints:
- subagent calls by `subagent_type`, and the per-call `model` when one was passed;
- input, cache-read and output tokens by `message.model`, for main and subagent transcripts separately;
- the share of tokens spent in subagents;
- sessions per day.

Also record:
- each seat's `weekly_all` percentage, from `GET https://api.anthropic.com/api/oauth/usage` (the call
  `pr_reviewer/files/claude-usage.py` already makes; it consumes no quota);
- merged PRs per worker identity in the window, from the Gitea API.

Keep the raw output in `/workspace/archive/2026-11-subagent-baseline/` and put the per-worker summary table in
the PR.

**Gate:** if subagent tokens are under about 5% of a worker's Claude usage, Phase 1 still ships. The policy
and `scout` cost little and guard against over-delegation. Phase 3 tuning then waits for a reason.

**0.2 Claude Code spike** (one worker, a throwaway user-level agent, then delete it):
- A `model: haiku` / `effort: low` agent's transcript shows a `message.model` that starts with `claude-haiku-`
  and differs from the main session's, and `effort` `low`. Record the actual ID; never assert a fixed one.
- A same-named project agent overrides the user agent.
- Record which model the `fable`, `sonnet` and `haiku` aliases resolve to on the installed CLI.

**Result (2026-11-13, workstation, Claude Code 2.1.322; the same CLI the fleet runs):** a project agent with
`model: haiku`, `effort: low` and a YAML comment line in its frontmatter loaded and ran on `claude-haiku-5-5`
at effort `low` while the main session ran `claude-opus-5-5` at `xhigh`; the main agent passed no per-call
model. A same-named user agent (`sonnet`) lost to the project agent. `sonnet` resolves to `claude-sonnet-5-5`.
`fable` was not exercised (its scoped limit is the scarce one; the docs map it to Fable 5.2).

**0.3 Codex spike** (codex at or above the floor, router provider, a scratch `CODEX_HOME`):
- (a) Does `agents.default_subagent_reasoning_effort` apply to spawned agents?
- (b) Does a role declared as `[agents.scout]` with `description` and `config_file = "agents/scout.toml"`,
  where that file sets `model`, `model_reasoning_effort` and `developer_instructions`, get spawned by name and
  run with that model and effort? Check the session log or the router's request log.
- (c) Which cheaper model do the router *and* `spawn_agent`'s catalog both accept?
- (d) Is `features.multi_agent_v2` still off on the fleet version, and does turning it on change (a) and (b)?

Record the codex version. If (b) or (c) fails, Codex keeps only the policy block and D6 stays gated.

**Status (2026-11-13): not run.** Its only consumer is Phase 3.2, and until it passes the outcome is the same
as a failed spike: the Codex roles stay off and Codex gets the policy block only. Run it before Phase 3.2.

## Phase 1: the role change (one PR, tag `subagents`)

### Files

1. **`ansible/roles/dev_worker/files/subagents/scout.md`** and **`.../bulk-worker.md`**. The content is in
   § Agent definitions. The frontmatter carries the marker comment
   `# Managed by ansible (role: dev_worker, subagent routing)`. YAML comments are valid in frontmatter.

2. **`ansible/roles/dev_worker/defaults/main.yml`:**

   ```yaml
   # ---- Subagent routing (plans/2026-11-13-agent-subagent-model-routing-plan.md) ----
   # Estate Claude Code agents, files/subagents/<name>.md -> ~/.claude/agents/<name>.md for every user in
   # dev_worker_users. [] removes every managed agent file; files without the managed marker are never touched.
   dev_worker_claude_subagents: []                  # the canary's host_vars: [scout, bulk-worker]
   # The "Delegation" managed block in ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md. false removes both blocks.
   dev_worker_delegation_policy_enabled: false      # the canary's host_vars: true
   # Codex [agents] defaults + roles (Phase 3.2). Stays false until the Phase 0.3 spike passes.
   dev_worker_codex_subagent_roles_enabled: false
   dev_worker_codex_subagent_model: ""             # "" = neither written nor touched
   dev_worker_codex_subagent_reasoning_effort: ""  # "" = neither written nor touched
   dev_worker_codex_scout_model: ""                # the scout role's model; "" = not written
   dev_worker_codex_scout_reasoning_effort: "low"
   dev_worker_agent_usage_report_path: /usr/local/bin/agent-usage-report
   ```

   **The defaults are off for the fleet.** The scheduled daily converge (`ailab-fleet-converge`, 06:35, runbook
   § "Daily fleet converge") applies the full role from `origin/main` to every worker, so a default that is on
   IS a fleet rollout the next morning. The canary turns both on in its `host_vars`; the fleet rollout after
   Phase 2 acceptance changes the defaults.

3. **`ansible/roles/dev_worker/tasks/subagents.yml`**, imported from `tasks/main.yml` with `tags: [subagents]`:
   - **Agent files.** For every user in `dev_worker_users`:
     - Ensure `~/.claude/agents` exists (owned by the user, 0755).
     - Copy each listed file to `~/.claude/agents/<name>.md` (0644).
     - Find files in that directory that carry the managed marker but are not in the list, and remove them.
       This is the same "find by header, then remove" pattern the local-model Codex profiles use in
       `tasks/codex.yml`.
     - Never touch a file without the marker. Those are the user's own agents.
   - **Policy blocks.** Write them with `blockinfile` and `marker: "<!-- {mark} ANSIBLE MANAGED - delegation policy -->"`,
     `create: true`, owned by the user:
     - into `~/.claude/CLAUDE.md`, with the Claude text in § Delegation policy blocks;
     - into `~/.codex/AGENTS.md`, with the Codex text, whose role line renders only when
       `dev_worker_codex_subagent_roles_enabled`.

     `state: absent` for both blocks when `dev_worker_delegation_policy_enabled` is false.
   - **Codex `[agents]` keys** (only when `dev_worker_codex_subagent_roles_enabled`, Phase 3.2):
     - Write them with `community.general.ini_file`, `section: agents`, in place and table-aware, like the model
       pin. A key whose variable is `""` is neither written nor touched: a value someone set by hand stays.
     - The `scout` role is `[agents.scout]` with `description` and `config_file`, plus
       `~/.codex/agents/scout.toml` from a template. The template's header marks it as managed.
     - When the flag is false, remove the role table and the managed role file. A `default_subagent_*` key is
       removed only while its variable is non-empty and the key still holds exactly that value; with the
       variable `""` it is left alone. Removal edits only a `config.toml` that exists on the worker (a `stat`,
       not a controller-side test).
   - **Settings.** Nothing is added to `/etc/claude-code/managed-settings.json` or to `~/.claude/settings.json`
     (D3, D5).
   - **The report.** `files/agent-usage-report` is installed at `dev_worker_agent_usage_report_path` on every
     worker (read-only, harmless while the agents are off), so Phase 2 re-runs it in place.

4. **`ansible/roles/dev_worker/tests/test-subagent-routing.sh`**, plus a step in
   `.gitea/workflows/dev-worker-scripts.yaml` (add the new paths to its `paths:` triggers). It follows
   `test-npm-prefix-guard.sh`: converge into a temporary HOME with the repo's ansible venv.
   - Each `files/subagents/*.md` has frontmatter that parses as YAML. `name` equals the file name.
     `description` is non-empty and at most 400 characters. `model` is one of `haiku|sonnet|opus|fable|inherit`.
     `effort` is absent or one of `low|medium|high|xhigh|max`. The managed marker is present. `scout`'s `tools`
     is a non-empty subset of `Read, Grep, Glob` (no `Bash`, `Edit`, `Write`, `NotebookEdit`; omitting `tools`
     would grant every tool).
   - Running the converge twice is idempotent: the second run reports `changed=0`.
   - A user's own `~/.claude/agents/mine.md` survives every run. Dropping `bulk-worker` from the list removes
     only `bulk-worker.md`, and `[]` removes both estate files.
   - `dev_worker_delegation_policy_enabled: false` removes both policy blocks and leaves the
     `openbao credentials` block in the same file byte-identical.
   - No non-comment line of the role's templates, tasks or defaults sets `CLAUDE_CODE_SUBAGENT_MODEL` (D3).
   - With roles on and a managed default set, turning the roles off removes that default, the role table and the
     role file, and keeps a hand-set key.
   - `agent-usage-report` on synthetic transcripts: launches counted once per `tool_use` id, a response's
     usage once per `message.id`, the window honoured, `agentType` read from `.meta.json`.
   - With roles disabled, `~/.codex/config.toml` gains no `[agents]` table. With roles enabled and the variables
     `""`, it gains only the role table, and the scout role file parses as TOML (`python3 -c 'import tomllib'`).

5. **Docs:**
   - `docs/runbooks/dev-workers.md` gets a § "Subagent routing": what is installed, how to add an estate agent,
     how a repository overrides one, how to verify on a worker (`agent-usage-report`, transcript
     `message.model` and `.meta.json`; not `/agents`, which no longer lists agents), and rollback.
   - New `docs/standards/agent-delegation.md`, the repository standard for D7: what a repository may add under
     `.claude/agents/` or `.codex/`, naming, and the guidance it must not add.

### Agent definitions

`files/subagents/scout.md`:

```markdown
---
# Managed by ansible (role: dev_worker, subagent routing). Local edits are overwritten on the next converge.
name: scout
description: Read-only search of code, config and log files. Use when you need locations and short excerpts back (files, symbols, call sites, config values, log lines) and will not reuse the raw output. Never edits. Not for review, design, or debugging conclusions.
tools: Read, Grep, Glob
model: haiku
effort: low
---
You find things in a codebase, its config, or its log files, and report where they are.

- Answer only what was asked. For each hit give `path:line` and a one-line excerpt.
- You have no shell and no write tools. If the answer needs a command (git history, a service's journal, a
  build), say so and stop: the main session runs it.
- When something is not found, say so and list where you searched.
- No analysis or recommendations beyond what identifies each hit. Keep the report under 40 lines unless asked for more.
```

`files/subagents/bulk-worker.md`:

```markdown
---
# Managed by ansible (role: dev_worker, subagent routing). Local edits are overwritten on the next converge.
name: bulk-worker
description: Applies one precisely specified mechanical change across many files (a rename, an API migration, moving a config key, the same fix repeated). Give it the exact rule, the files or glob, and the check to run. Not for design decisions, debugging, or anything the rule does not fully specify.
model: sonnet
---
You apply one mechanical change across many files, exactly as specified.

- Follow the rule you were given. If a file does not fit the rule, leave it unchanged and list it with the reason. Do not improvise.
- Run the check you were given (tests, build, linter, grep) and report its result as it is.
- Do not commit, push, or open pull requests unless the task says to.
- Report: the files changed, the files skipped with reasons, and the check's result.
```

### Delegation policy blocks

`~/.claude/CLAUDE.md`:

```markdown
## Delegation (subagents)

Subagents cost time and the same subscription limits as this session: each one rebuilds context and reports
back, and you then read its report. Delegate only when the payoff clearly beats that.

Delegate:
- A search whose raw output you will not need again (many files, logs, call sites): the `scout` agent.
- One mechanical change across many files, with an exact rule and a check: the `bulk-worker` agent.
- Genuinely independent, sizeable tracks (unrelated modules, a wide investigation): a general-purpose
  subagent. Launch independent agents in one message.

Do it yourself:
- Anything you can finish in a few tool calls, and sequential steps that share context.
- Design, non-mechanical implementation, debugging conclusions and code review.
- Verification of your own work. Do not spawn a subagent to double-check.

When you delegate, brief once with the paths, constraints and report format; keep spawn counts low; do not
redo the subagent's work. Do not pass a model or effort on the call unless the user asks: the agent
definitions choose them.
```

`~/.codex/AGENTS.md`:

```markdown
## Delegation (sub-agents)

Sub-agents cost time and context: each one rebuilds context, runs on this session's quota, and reports
back. Spawn one only for a search whose raw output you will not reuse, for genuinely independent and
sizeable tracks, or for work too large for one context. Do small tasks, sequential steps that share
context, design, review and verification of your own work yourself. Brief once, keep spawn counts low,
and do not redo a sub-agent's work.
{% if dev_worker_codex_subagent_roles_enabled %}For searches, spawn the `scout` agent.{% endif %}
{% if (dev_worker_codex_router_enabled | bool) and (dev_worker_codex_local_models_enabled | bool) %}
Agents on the local models are unaffected: they run through `local-agent` (see "Local models through the
router").{% endif %}
```

The Codex text applies Anthropic's evidence by analogy. Coordination overhead and context isolation do not
depend on the model, but Codex is measured separately in Phase 2.

## Phase 2: canary, then the fleet

1. **Canary.** Pick the worker with the most Claude usage in Phase 0: **dev-worker-2** (486 launches, 83% of
   its tokens in subagents). Its `host_vars/dev-worker-2.yml` sets `dev_worker_claude_subagents: [scout,
   bulk-worker]` and `dev_worker_delegation_policy_enabled: true`; the daily converge then keeps it so.
   - Dry run first: `ansible-playbook dev-workers.yml -l <canary> -t subagents --check --diff`.
   - Then converge the same command without `--check`.
   - Only new sessions read the new agents and blocks. Running sessions, including the operator's, are not
     restarted.
2. **Observe for 7 days**, one weekly-limit window. Re-run the Phase 0 report on the canary and compare with
   its own baseline:
   - the subagent call mix (`scout` / `bulk-worker` / `Explore` / `general-purpose`);
   - the share of subagent tokens on Haiku or Sonnet;
   - `weekly_all` burn per merged PR;
   - quality signals: the share of the canary's PRs with changes requested by the reviewers, reverts, and owner
     reports.
3. **Acceptance** (rates per merged PR, so a low or zero baseline still has a meaning):
   - no quality signal worse than baseline;
   - subagent launches per merged PR not higher than baseline (dw2: 486 / 216 = 2.25);
   - launches that pass a per-call `model` fall to near zero (baseline 231 of 486);
   - launches other than `scout` and `bulk-worker` per merged PR not higher than baseline, and `scout` takes
     searches that went to `Explore` or `general-purpose` before;
   - `weekly_all` burn per merged PR not higher than baseline. dw2 shares its account with dw4, so read the two
     workers' merged PRs together.

   Then change the role defaults to the canary's values; the next daily converge (or `-l dev_workers -t
   subagents`, after `--check --diff`) rolls the fleet.
4. **Report.** Post the before and after table in the PR, or in a follow-up issue if the PR has merged.

## Phase 3: follow-ups (each is its own decision)

- **3.1 Built-in `Explore`.** If the canary shows broad searches still going to the built-in `Explore`, which
  runs on Opus, decide whether to ship a user-level `Explore.md` on Haiku. A user agent named `Explore`
  replaces the built-in.
  - Risk: it replaces Anthropic's tuned Explore prompt.
  - Measure it sequentially on the canary: one weekly window with the built-in `Explore`, then one with the
    user-level `Explore.md`, compared with the Phase 0 report. A user-level file applies to every session of
    that user, so the sessions of one window cannot be split.
- **3.2 Codex roles.** After spike 0.3 passes, set `dev_worker_codex_subagent_roles_enabled: true` and the
  chosen model and effort variables in `group_vars`. Converge `-t subagents`, then run
  `codex app-server daemon restart` per user (the same restart `-t codex` changes need; never forced).
- **3.3 Main-session effort.** Anthropic suggests starting Opus 5.5 at `medium`, and the fleet runs `xhigh`.
  That is an owner decision. If taken, measure it like Phase 2: one worker, one weekly window, with the same
  report.
- **3.4 Repository audit (all repositories):**
  - Scan the 27 `cchifor` repositories, plus any other repositories the workers commit to, for:
    - `.claude/agents/*` and `.claude/settings*.json` that set `model`, `effort` or `CLAUDE_CODE_SUBAGENT_MODEL*`;
    - `.codex/` config with an `[agents]` table;
    - `CLAUDE.md` and `AGENTS.md` lines that conflict with D4, such as "use a subagent to verify",
      "delegate as much as possible", or pinned subagent models.
  - Open one issue per repository with findings, and fix them through normal PRs against
    `docs/standards/agent-delegation.md`.
  - A repository with no findings needs no change: the user-level files already apply to it.

## Rollback

- **Per worker, or the whole fleet:** set `dev_worker_claude_subagents: []` and
  `dev_worker_delegation_policy_enabled: false` (and `dev_worker_codex_subagent_roles_enabled: false`) in
  `host_vars` or `group_vars` (for the canary: delete its two lines), then converge `-t subagents`. That removes the managed files, the blocks and the
  Codex keys and role. Nothing else holds state.
- **Emergency without Ansible:** delete `~/.claude/agents/{scout,bulk-worker}.md` and the managed block. The
  next converge puts them back unless the variables are flipped.

## Risks

- **`scout` gets analysis work.** The main agent may hand `scout` work that is really analysis, and Haiku
  answers it shallowly.
  - The description limits it to locations and excerpts.
  - The policy keeps conclusions in the main session.
  - Phase 2 watches review outcomes.
- **The policy is too strict.** Less parallelism means slower wall-clock on wide tasks. Compare sessions per
  day and time to merge in Phase 2.
- **Model aliases move.** `haiku` and `sonnet` follow Claude Code's alias table, which is intended (current
  generation). Repeat the Phase 2 comparison after a model generation change.
- **Fable sessions.** Anthropic's guidance for Fable 5.1 is to let it delegate independent subtasks
  asynchronously. The block still allows independent, sizeable tracks. If a worker runs Fable as its main
  model, check its subagent mix in Phase 2 before tightening anything.
- **Precedence surprises.** These would override the definitions: a project agent with an estate name, a
  per-call `model`, or `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`. The tests assert the role never sets the last one,
  and the policy tells the agent not to pass the per-call model.
- **Codex churn.** The V2 regression shows that per-spawn model selection changes between releases.
  Mitigations: the D6 gate, and the spike repeated on each `dev_worker_codex_version` bump while the roles are
  on.

## Open questions for the owner

1. ~~Is this request the exception to the dev-worker freeze (§ Authority), and which worker is the canary?~~
   Answered 2026-11-13: yes (§ Authority); the canary is dev-worker-2 by the Phase 0 rule.
2. Phase 3.3: should the main-session effort change? This plan does not change it.
3. Phase 3.1: should a Haiku `Explore` override ship if the canary shows the built-in still takes the searches?
4. Phase 0.3(c): which cheaper Codex model, if any, should Codex sub-agents use?

## References

- Anthropic, Optimizing for cost and intelligence:
  https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence
- Anthropic, Models overview (Fable 5.2, prices, default effort): https://platform.claude.com/docs/en/about-claude/models/overview
- Claude Code docs, Subagents: https://code.claude.com/docs/en/sub-agents.md
- Anthropic, When to fold subagents into your main agent loop (2026-10-22):
  https://claude.com/resources/articles/building-agents-when-to-fold-subagents-into-your-main-agent-loop
- Anthropic, When to use multi-agent systems (2026-01):
  https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them
- Kwartler, Aqrawi, Abbasi, Harness Tokenomics (arXiv 2609.28919v2, 2026-09-26): https://arxiv.org/html/2609.28919v2
- Codex config reference: https://learn.chatgpt.com/docs/config-file/config-reference
- OpenAI forum, Codex can no longer spawn subagents with specific models (staff reply 2026-10-11):
  https://community.openai.com/t/codex-cli-can-no-longer-spawn-subagents-with-specific-models-or-reasoning/1386290
- In this repository: ADR 0025 (subscription weekly limits), `ansible/roles/dev_worker/tasks/{claude_code,codex,openbao}.yml`,
  `plans/2026-11-12-dev-worker-cleanup-gc-reflog-and-link-guard-plan.md` (freeze).
