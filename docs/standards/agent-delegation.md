# Standard: agent delegation in repositories (subagents and sub-agents)

**Status:** adopted 2026-11-13. Plan and review trail: `plans/2026-11-13-agent-subagent-model-routing-plan.md`
(decision D7). Operations: `docs/runbooks/dev-workers.md` § "Subagent routing".

This standard says what a repository may add to steer how coding agents delegate work to subagents (Claude Code)
and sub-agents (Codex). The estate default lives at user level on every dev-worker: the agents `scout` and
`bulk-worker` in `~/.claude/agents/`, and a "Delegation" block in `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`.
It applies to every repository a worker opens, so **a repository with nothing to specialise adds nothing**.
Statements with **MUST** are checked by the repository audit (plan Phase 3.4).

## 1. Why the default is narrow

Each subagent rebuilds context and reports back, and its tokens count against the same subscription limits as the
main session. Anthropic measured multi-agent designs at 3 to 10 times the tokens of one agent, and found cheaper
subagents pay off only for work larger than one context or routine high-volume work. On 2026-11-13 the busiest
dev-worker spent 83% of its Claude tokens in subagents. So the estate delegates by job: a cheap read-only `scout`
for searches, `bulk-worker` for mechanical changes, the main model for everything that needs judgement.

## 2. What a repository MAY add

- **A project agent** in `.claude/agents/<name>.md` when the repository has a recurring job the estate agents do
  not cover (a domain-specific search, a generator that must run a certain way). Its frontmatter MUST set `name`,
  a `description` that says when to use it and when not, and `model` explicitly (`haiku`, `sonnet`, `opus`,
  `fable` or `inherit`). Read-only agents MUST list their tools and MUST NOT include `Bash`, `Edit`, `Write` or
  `NotebookEdit`: omitting `tools` grants every tool.
- **An override of an estate agent** with the same name (`scout`, `bulk-worker`). A project agent beats the user
  agent of the same name in that repository. The file MUST say in its body why the repository needs its own
  version.
- **Codex roles** in the repository's `.codex/` only after the estate's Codex roles are on (plan Phase 3.2); until
  then a role's model and effort are not known to apply.

## 3. What a repository MUST NOT add

- Guidance to delegate as much as possible, or to verify, review or double-check work with a subagent.
  Verification stays in the main session.
- A pinned subagent model for all subagents: `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` or
  `CLAUDE_CODE_EFFORT_LEVEL` in `.claude/settings*.json`, or an equivalent `[agents]` default in `.codex/`. They
  move implementation and review subagents onto a model chosen for searches, or override every definition's
  `effort`.
- Instructions to pass a `model` or `effort` on every Agent call. A per-call value overrides the definitions.

## 4. Audit

Plan Phase 3.4 scans every `cchifor` repository for `.claude/agents/*`, `.claude/settings*.json` that set a
model, effort or `CLAUDE_CODE_SUBAGENT_MODEL*`, `.codex/` `[agents]` tables, and `CLAUDE.md`/`AGENTS.md` lines
against section 3. It opens one issue per repository with findings; a repository with none needs no change.
