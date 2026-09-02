---
name: feature-implementation
description: "End-to-end feature delivery on this estate: codex-gated plan, TDD implementation, workload-class validation, PR watched to merge. Use when the user asks to analyze, improve, or implement a feature in a repo (/feature-implementation <feature ask>)."
---

# Feature implementation

Drive a feature from ask to merged PR. The target repo's `AGENTS.md`, `TESTING.md`, and
`CLAUDE.md` are binding and override anything here. State plainly at the end which phases
ran and what was skipped, and why.

## Phase 0 — ground
- Fetch and branch off latest `origin/main`; never implement on main.
- Read the repo's `AGENTS.md` + `TESTING.md` and `plans/` for prior art.
- **Reuse first**: before designing new components, check the platform repo
  (https://git.chifor.me/cchifor/platform) for existing ones. Extract or generalize a
  component ONLY when a second consumer exists or is named in the ask — no speculative
  genericity.

## Phase 1 — plan (gated)
- Write the plan to `plans/YYYY-MM-DD-<slug>-plan.md`.
- Cross-review it with codex (`codex exec -m gpt-5.6-sol`), passing the plan content
  INLINE — never let a reviewer read a possibly-stale checkout. Max 2 rounds, then
  escalate remaining disagreements to the operator.
- UI/UX proposals: deliver as an HTML page via the design skill BEFORE implementation.

## Phase 2 — implement
- TDD: production code lands with a test that failed first.
- Unit + integration tests for every change; Playwright e2e where UI is touched.
- Stage explicit paths (`git status --porcelain` before every commit; never `git add -A`);
  no AI attribution in commits or PRs.

## Phase 3 — validate (workload-class routing)
- S-class (lint/types/unit) and M-class (small per-checkout compose tiers) run locally.
- Playwright / heavy compose: NEVER directly on a dev-worker — lease a pool env (`tep`, or
  the repo's wrapper, e.g. `dashboard/e2e-pool.sh`).
- Kubernetes-facing validation happens in a leased env or with explicit operator approval —
  never ad hoc against the estate cluster.

## Phase 4 — PR to merge
- PRs live on the gitea forge (API/`tea`; `gh` cannot reach it). Opening the PR is not the
  finish line: watch CI, address every review comment, codex cross-review the final diff,
  iterate until merged. Report test-tier coverage honestly.

## Orchestration
- Inside herdr (`test "$HERDR_ENV" = 1`): use the conductor pattern — the codex reviewer in
  its OWN labeled pane, implementers in labeled worktree workspaces (the herdr skill has the
  primitives). Name every workspace and agent at creation.
- Outside herdr: native worktree-isolated subagents.
- Either way: at most ~2 concurrent Claude agents ACROSS ALL machines (one shared
  subscription; 429 storms have stalled this estate), yield to interactive work, and let
  subagents inherit the session model — pick effort tiers, not model names.

## Deliberately absent
No persona preambles, no hardcoded model menus, no blanket "make it generic" mandates —
concrete constraints beat titles, model lists rot, and unrequested abstraction is debt.
