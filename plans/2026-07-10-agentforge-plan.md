# AgentForge — Autonomous Agentic Development System on ailab (Gitea + dev-workers)

## Context

Build a fully autonomous, label-driven multi-agent software development system on existing ailab
infrastructure: Gitea 1.26 (`git.chifor.me`, org `cchifor`) as forge + source of truth, the 6
dev-worker VMs (dw1–dw6, 192.168.0.8–.13, user c4) as agent hosts, act_runner CI
(`self-hosted-hv`), and litellm for local-model access.

Lifecycle (user spec) — issue labels are the state machine:
`state: 1-needs-plan` →Planner→ `state: 2-needs-tests` (skippable) →Tester→
`state: 3-ready-to-code` →Implementer→ `state: 4-in-review` →Reviewer→ merge → `state: 5-completed`,
with Reviewer able to bounce 4→3. Webhook-driven; JSON config in a Gitea repo maps workers→roles.

Settled decisions:
- **LLM engines = existing subscriptions + local models ONLY**: 2× Claude Max (dev-workers'
  `claude` OAuth logins; Max#1 on dw1/dw2, Max#2 on dw3/dw4) + 1× Codex Pro (`codex` OAuth on
  dw5/dw6) + local qwen via litellm. **litellm may route ONLY local models for this system**
  (app-side whitelist {qwen3.6-35b-a3b, qwen3.5-122b}; no cloud keys through litellm).
- Default role engines: Planner=Max#1 · Implementer=Max#2 · Reviewer=Max#1 ·
  Tester=qwen3.6-35b-a3b (litellm) · **Codex Pro = dedicated cross-reviewer**.
- **Codex cross-review alignment gates (binding)**: the plan, implementation, and review stages
  each end with a Claude↔Codex loop iterating until alignment, hard cap 3 rounds; breach →
  `needs-human`. The same rule governs building the system itself.
- App code in NEW repo `cchifor/agentforge`; this ailab PR carries only infra/deploy changes.
- Vue 3 dashboard in scope (Playwright e2e target).
- Full auto-merge by reviewer-bot on config-allowlisted repos, with guardrails.

**Governing principle**: Gitea is the only durable state store. Webhooks are hints; labels are
levels; a reconcile poll is the guarantee. Agents never write to the forge — the orchestrator does
all forge writes. Stateless role workers; no Redis/queue infra.

## Approach

### Service (new repo `cchifor/agentforge`)

One Python 3.12 service deployed identically to all 6 workers as a host systemd unit (`User=c4`) —
NOT containerized in prod (it drives host tools: `claude`/`codex` CLIs + OAuth homes, git creds,
docker, `/workspace`). In-process `asyncio.Queue`; a 60s reconciler re-discovers all work from
labels after any crash.

- **Events**: 6 org-level Gitea webhooks (one per worker, `http://192.168.0.{8..13}:8700/webhook`,
  shared HMAC, events issues/issue_comment/pull_request/pull_request_review/push). Workers filter
  by "my roles handle this state" → enqueue IssueRef hint. Level-triggered: re-read the issue on
  dequeue, act on current labels, never the payload. Dedup on `X-Gitea-Delivery` (LRU).
- **Claim lock**: labels/assignees are last-write-wins, but issue comments are append-only with
  ordered ids → claim = `<!-- af:claim {role,worker,job,expires} -->` comment as the role's bot;
  lowest unexpired claim id newer than the last transition marker wins. Heartbeat = PATCH own
  claim's `expires` (60s); lease TTL 45min; reaper clears expired claims. Assignee + `af:wip:dwN`
  are cosmetic mirrors. Contract tests against real Gitea validate the primitive.
- **Clean architecture** (`src/agentforge/`): `domain/` (states, models, policy — pure), `ports/`
  (ForgeClient, AgentRunner, ConfigSource, EventSink Protocols), `adapters/` (gitea
  client+webhook+labels, runners claude_code+codex+litellm_chat, config gitea_repo, events
  sqlite+http_push), `app/` (orchestrator, claims, workspace, ledger, prompts/*.j2,
  handlers/{planner,tester,implementer,reviewer}), `infra/` (FastAPI api, reconciler, sse,
  settings, logging), `main.py` composition root.
- **Three runner adapters** (per-role `engine` in config):
  1. `ClaudeCodeRunner(auth=subscription)` — `claude -p … --output-format json --permission-mode
     dontAsk --max-turns N --allowedTools <role list>` with the worker's Max OAuth; per-account
     fleet-wide concurrency semaphore (default 1) to respect subscription rate windows.
  2. `ClaudeCodeRunner(auth=litellm_local)` — same CLI, `ANTHROPIC_BASE_URL=http://192.168.0.41:30400`
     (new litellm-lan NodePort), `ANTHROPIC_MODEL=qwen3.6-35b-a3b`,
     `ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3.6-35b-a3b`, `API_TIMEOUT_MS=900000`. Config validator
     hard-rejects non-local models on this engine.
  3. `CodexRunner` — `codex exec` headless (read-only sandbox for critique, `--cd <ws>`,
     `--skip-git-repo-check`, `--output-last-message <file>`) with Codex Pro OAuth.
  Structured output via prompt-embedded JSON schema + pydantic validation (+1 repair pass via
  litellm-local); `--json-schema` when the CLI supports it (feature-detect). Least-privilege tools:
  Planner/Reviewer `Read,Glob,Grep`; Tester + `Write,Edit,Bash(<test_cmd>:*)`; Implementer full
  edit+Bash; NO git push / WebFetch for any role — the orchestrator commits/pushes as the role's
  bot and does every forge write via per-bot authenticated GiteaClients.
- **Trust-but-verify orchestrator-side**: Tester — verify tests FAIL and diff ⊆ `test_paths` before
  pushing. Implementer — handler loop: agent → run `test_cmd` → on red re-invoke with failure tail
  (`--resume`), max 4 iterations → on green push + PR (`Closes #N`). Reviewer — structured verdict;
  orchestrator posts REQUEST_CHANGES review w/ inline comments (4→3) or APPROVED + squash-merge.
  Review rounds = count(REQUEST_CHANGES reviews by reviewer-bot) via API; `af:round:N` is display.
- **Codex alignment gates**: each gated stage runs produce → codex critique (structured verdict
  `{aligned, blocking_concerns[]}`) → primary agent addresses → repeat until aligned or 3 rounds.
  Plan gate (before 1→2/3 transition), implementation gate (tests green, before PR/state 4),
  review gate (Claude Reviewer AND independent Codex PR review must both approve before merge;
  disagreement feeds the 4→3 bounce). Cap breach at any gate → `needs-human` with disagreement
  summary. All rounds recorded as `af:xrev` ledger comments.
- **Ledger**: append-only `af:run` comments (role, worker, engine, usage/turns/duration,
  transition). Issue totals = sum of comments; the forge is the audit log.
- **Guardrails**: max 3 review rounds; per-run max_turns + wall-clock timeout; per-issue total-runs
  cap (12); per-Max-account semaphore; implementer iteration cap (4); protected_paths diff →
  `needs-human`; auto-merge only on allowlisted repos; `needs-human` = global stop; `FORGE_PAUSED`
  = fleet kill switch. Gitea-side: branch protection required_approvals=1, reviewer-bot on
  approvals allowlist, impl-bot NOT on merge allowlist.
- **Bots**: 4 Gitea users (planner-bot, tester-bot, impl-bot, reviewer-bot), scoped PATs.
- **Dashboard**: dw1 runs extra `dashboard` role — sqlite event cache (disposable), REST + SSE,
  Vue 3 + Vite + Pinia SPA served by FastAPI StaticFiles; demo mode (FakeForge + scripted events)
  is the Playwright/local-dev target. Other workers fire-and-forget POST events to it.
- **Config**: repo `cchifor/agentforge-config`, single `agentforge.json` — `release` pin
  (deployment control plane), workers→roles (incl. `cross-reviewer`), `accounts` block,
  repos allowlist (+ setup_cmd/test_cmd/test_paths/protected_paths/auto_merge), per-role
  engine/model/max_turns/timeouts, `cross_review {engine, max_rounds: 3, stages}`, guardrails,
  labels, intervals, port. Refresh: org webhook on config push + 2-min poll. Invalid → last-good.

### Testing (all deterministic in CI; no live LLM gates a PR)

- Unit (pytest + hypothesis; MockAgentRunner incl. malformed outputs; FakeForgeClient) — ≥95%
  branch cov on domain/, 85% overall. Vitest for the SPA.
- Contract tier vs real `gitea/gitea:1.26-rootless` in compose (claim primitive under race,
  self-approval 422, branch protection, webhook HMAC/delivery-id semantics) + fake-honesty suite
  (same behavioral tests parameterized over fake AND real client).
- Integration: compose `test` profile — gitea + 4 role containers (one image, FORGE_ROLE env) +
  llm-stub (canned Anthropic/OpenAI responses, scenario YAML, fault injection, record mode vs real
  litellm w/ sanitized committed traces) + fake-claude/fake-codex CLI shims (apply fixture patches,
  print faithful envelopes). Canonical scenario: seed issue at state 1 → assert full walk to
  merged + state 5 with audit comments. 21 failure-mode cases incl. gate convergence, cap-breach
  escalation, codex-down escalation.
- E2E: Playwright (in `mcr.microsoft.com/playwright` container) — 5 scenarios: kanban truth, feed,
  usage counters, escalation surface, reconnect resilience.
- CI (`.github/workflows/ci.yml`, runner `self-hosted-hv`): lint → unit → contract → integration →
  e2e → build; always-run `ci-gate` aggregator = sole required check; NO upload-artifact (broken on
  Gitea) — failure diagnostics to versitygw S3. `release.yml`: tag → tarball → Gitea generic
  package + config-repo pin bump. `model-drift.yml`: nightly vs real qwen3.6, non-blocking.

### Deployment (this ailab PR)

- `kubernetes/apps/apps/gitea/gitea.yaml`: add `webhook: {ALLOWED_HOST_LIST: "192.168.0.0/24"}`
  under `values.gitea.config` — day-0 blocker (Gitea refuses webhook delivery to private IPs).
- NEW `kubernetes/apps/apps/ai/litellm-lan.yaml`: NodePort 4000→30400 (mirrors prometheus-lan
  pattern + rationale) + kustomization entry. Used ONLY for local qwen models.
- `ansible/roles/dev_worker/`: NEW `tasks/agentforge.yml` + templates (`agentforge.service.j2`,
  `agentforge.env.j2`, memory-cap drop-in MemoryHigh=6G/MemoryMax=8G, `agentforge-update.{service,
  timer}` + `files/agentforge-update.sh`): `/opt/agentforge/releases/<ver>` + `current` symlink,
  `uv sync --frozen`, 2-min self-update from config-repo `release` pin → Gitea generic package
  tarball (keep last 3; rollback = revert pin commit). Wire into `tasks/main.yml` after jobs,
  gated `when: dev_worker_enable_agentforge | bool`; defaults; firewall allow tcp/8700 from LAN;
  restart handler.
- `ansible/dev-workers.yml`: extend SOPS pre_task `when:` with the agentforge toggle (also fix the
  existing gap: git_forge.yml's `dev_worker_gitea_token` isn't in the list today).
- `.sops.yaml`: extend dev-worker `encrypted_regex` with `|dev_worker_agentforge_.*`. New keys:
  `dev_worker_agentforge_webhook_secret`, `dev_worker_agentforge_litellm_key`,
  `dev_worker_agentforge_{planner,tester,impl,reviewer}_token`.
- NEW `kubernetes/apps/infrastructure/monitoring/agentforge.yaml` (Service+Endpoints .8-.13:9464 +
  ServiceMonitor, cloned from dev-workers-node.yaml) + `agentforge-rules.yaml` (ForgeWorkerDown,
  ForgeIssueStuck, ForgeNeedsHumanPending, ForgeWebhookHMACFailures, ForgeReconcileDriftHigh).
- `docs/decisions/0018-agentforge-autonomous-dev-agents.md` + `docs/runbooks/agentforge.md`.
- CLAUDE.md dev-worker IP table fix (.37-.39 → .8-.13, 6 workers).

### Gitea provisioning

`scripts/bootstrap_gitea.py` in the agentforge repo (admin PAT; every step get-then-create):
repos `agentforge-config` + `agentforge-playground`; 4 bot users + scoped tokens (Sudo mint,
print-once → SOPS); org team `agentforge-bots`; org labels (5 state + needs-human + af:*);
6 org webhooks; branch protection on playground (required_approvals=1,
approvals_whitelist=[reviewer-bot,chifor], merge_whitelist=[reviewer-bot,chifor],
block_on_rejected_reviews, required context `CI / ci-gate*`). Same module seeds the compose stack.
Note (from forge migration experience): Gitea issue API needs int64 label IDs.

### Milestones

M1 domain core (TDD) → M2 GiteaClient+ClaimService+contract tier → M3 orchestrator+webhook+
reconciler with FakeRunner (full 1→5 lifecycle, no LLM; codex cross-review of the nervous system)
→ M4 runners + handlers + alignment gates → M5 ledger+SSE+dashboard+Playwright+CI → M6 this ailab
PR + codex Phase B + push agentforge to Gitea + PRs.

### Rollout (day 0 → autonomous)

Phase 0: merge ailab PR (webhook allowlist + litellm-lan) → subscription logins (Max#1 dw1/dw2,
Max#2 dw3/dw4, Codex dw5; `claude setup-token`) → bootstrap_gitea.py → SOPS secrets → CI green →
tag v0.1.0 → `release: 0.1.0` pin → `dev_worker_enable_agentforge: true` → `just dev-workers` →
webhook test-deliveries 2xx ×6.
Phase 1: dw1 all roles, playground only, merge disabled (shadow) → pilot issue → inspect → enable
merge → full lifecycle smoke.
Phase 2: chaos (kill mid-task/lease recovery, manual label flip, low caps).
Phase 3: role-per-worker fleet-wide, 2 implementers (live claim contention), ~10-issue 48h soak.
Phase 4: dogfood — agentforge PR#1 merged BY HAND (codex+human review; no circular trust), then
PR#2+ flow through the deployed system; reviewer-bot merges with dual Claude+Codex approval.
Phase 5: onboard repos one at a time; weekly smoke timer. Rollback anywhere: `FORGE_PAUSED` or
`systemctl stop agentforge` — state lives in Gitea.

## Critical files

- ailab (this PR): `kubernetes/apps/apps/gitea/gitea.yaml`, `kubernetes/apps/apps/ai/litellm-lan.yaml`
  (+ kustomization), `ansible/roles/dev_worker/tasks/{main,agentforge,firewall}.yml` + new
  templates/files, `ansible/roles/dev_worker/defaults/main.yml`, `ansible/dev-workers.yml`,
  `.sops.yaml`, `ansible/secrets/dev-worker.sops.yaml`,
  `kubernetes/apps/infrastructure/monitoring/{agentforge,agentforge-rules}.yaml`,
  `docs/decisions/0018-*.md`, `docs/runbooks/agentforge.md`, `CLAUDE.md`.
- agentforge repo: `src/agentforge/app/orchestrator.py` (core loop), `app/claims.py` (comment
  lock — the novel mechanism, proven first), `adapters/gitea/client.py`, `domain/states.py`,
  runners, handlers, `compose/`, `scripts/bootstrap_gitea.py`, `.github/workflows/*`.

## Verification

- `uv run pytest` (unit, <60s) · `docker compose --profile contract up --wait && pytest -m contract`
  · `--profile test` integration full-lifecycle + failure modes · `npx playwright test` (demo +
  compose) · CI green on Gitea Actions with `ci-gate` aggregator.
- ailab k8s: Flux reconciles; `kubectl --context admin@ai -n ai get svc litellm-lan`; webhook
  test-delivery 2xx; ServiceMonitor targets up on prometheus-lan :30090.
- Live smoke `scripts/smoke-ailab.sh`: canary issue with production engines, per-stage SLOs,
  distinct-bot authorship/approval/merge asserted, cleanup + reset.
- Dogfood criterion: an agentforge feature issue flows 1→5 autonomously; reviewer-bot merges;
  release pipeline deploys it.

<!-- codex-review-status: pending -->
