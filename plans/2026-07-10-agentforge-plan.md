# AgentForge — Autonomous Agentic Development System on ailab (Gitea + dev-workers)

## Codex Review

- The overall direction is strong: Gitea as durable state, level-triggered reconciliation, fake/contract test tiers, and explicit auto-merge guardrails are the right foundations for this system.
- The comment-based claim lock is not race-free as written; it needs state/base-marker binding, post-claim confirmation, loser withdrawal, full pagination, and pre-transition revalidation.
- The LLM topology is viable but under-specified for headless subscription auth, subscription rate-window cooldowns, local-only LiteLLM enforcement, and CLI version drift.
- The self-update path and webhook fan-out need more failure-mode verification: atomic updates, skew tolerance, package immutability, stopped-worker webhook behavior, and Gitea package/API smoke tests.
- The ailab PR file list is close but missing some required convention files, and the current `User=c4`/host-tool execution model is too permissive for untrusted repo tests plus auto-merge.

## Context

Build a fully autonomous, label-driven multi-agent software development system on existing ailab
infrastructure: Gitea 1.26 (`git.chifor.me`, org `cchifor`) as forge + source of truth, the 6
dev-worker VMs (dw1–dw6, 192.168.0.8–.13, user c4) as agent hosts, act_runner CI
(`self-hosted-hv`), and litellm for local-model access.
<!-- codex: `CLAUDE.md` is stale at .37-.39, while Terraform and monitoring endpoints already use .8-.13. The PR should update every operator-facing reference, including stale comments in `dev-workers-node.yaml`, not just the visible table. -->

Lifecycle (user spec) — issue labels are the state machine:
`state: 1-needs-plan` →Planner→ `state: 2-needs-tests` (skippable) →Tester→
`state: 3-ready-to-code` →Implementer→ `state: 4-in-review` →Reviewer→ merge → `state: 5-completed`,
with Reviewer able to bounce 4→3. Webhook-driven; JSON config in a Gitea repo maps workers→roles.

Settled decisions:
- **LLM engines = existing subscriptions + local models ONLY**: 2× Claude Max (dev-workers'
  `claude` OAuth logins; Max#1 on dw1/dw2, Max#2 on dw3/dw4) + 1× Codex Pro (`codex` OAuth on
  dw5/dw6) + local qwen via litellm. **litellm may route ONLY local models for this system**
  (app-side whitelist {qwen3.6-35b-a3b, qwen3.5-122b}; no cloud keys through litellm).
<!-- codex: The existing `litellm.yaml` includes OpenAI/Anthropic cloud routes, so "app-side whitelist" is not a security boundary. Use a separate local-only LiteLLM config/deployment or an enforceable model-restricted key before exposing a LAN NodePort. -->
- Default role engines: Planner=Max#1 · Implementer=Max#2 · Reviewer=Max#1 ·
  Tester=qwen3.6-35b-a3b (litellm) · **Codex Pro = dedicated cross-reviewer**.
- **Codex cross-review alignment gates (binding)**: the plan, implementation, and review stages
  each end with a Claude↔Codex loop iterating until alignment, hard cap 3 rounds; breach →
  `needs-human`. The same rule governs building the system itself.
<!-- codex: The hard cap needs one shared budget definition across stage-internal alignment and PR review bounces. Otherwise a stage can consume 3 invisible alignment rounds and still enter the separate 3-review-round guardrail. -->
- App code in NEW repo `cchifor/agentforge`; this ailab PR carries only infra/deploy changes.
- Vue 3 dashboard in scope (Playwright e2e target).
<!-- codex: The dashboard is useful but not on the critical path for safe autonomy. Keep it feature-flagged or defer polish if it competes with claim-lock, auth, and deploy hardening. -->
- Full auto-merge by reviewer-bot on config-allowlisted repos, with guardrails.

**Governing principle**: Gitea is the only durable state store. Webhooks are hints; labels are
levels; a reconcile poll is the guarantee. Agents never write to the forge — the orchestrator does
all forge writes. Stateless role workers; no Redis/queue infra.
<!-- codex: Later features imply cross-worker coordination, especially per-account semaphores, release rollout state, and rate cooldowns. Specify which of these are encoded in Gitea comments/config versus disposable caches so this principle remains true. -->

## Approach

### Service (new repo `cchifor/agentforge`)

One Python 3.12 service deployed identically to all 6 workers as a host systemd unit (`User=c4`) —
NOT containerized in prod (it drives host tools: `claude`/`codex` CLIs + OAuth homes, git creds,
docker, `/workspace`). In-process `asyncio.Queue`; a 60s reconciler re-discovers all work from
labels after any crash.
<!-- codex: Running agents and repository tests as `c4` gives untrusted code access to OAuth homes, forge credentials, Docker, and likely passwordless sudo. Use a separate non-sudo service user, per-run token/env scoping, and workspace/container isolation before enabling auto-merge beyond playground. -->

- **Events**: 6 org-level Gitea webhooks (one per worker, `http://192.168.0.{8..13}:8700/webhook`,
  shared HMAC, events issues/issue_comment/pull_request/pull_request_review/push). Workers filter
  by "my roles handle this state" → enqueue IssueRef hint. Level-triggered: re-read the issue on
  dequeue, act on current labels, never the payload. Dedup on `X-Gitea-Delivery` (LRU).
<!-- codex: Six org webhooks mean one forge event fans out six times; delivery-id dedup only removes retries per endpoint, not cross-worker duplicates. That is acceptable only if the claim primitive is the real admission control and stopped-worker retry/backlog behavior is tested. -->
- **Claim lock**: labels/assignees are last-write-wins, but issue comments are append-only with
  ordered ids → claim = `<!-- af:claim {role,worker,job,expires} -->` comment as the role's bot;
  lowest unexpired claim id newer than the last transition marker wins. Heartbeat = PATCH own
  claim's `expires` (60s); lease TTL 45min; reaper clears expired claims. Assignee + `af:wip:dwN`
  are cosmetic mirrors. Contract tests against real Gitea validate the primitive.
<!-- codex: The protocol is not race-free as written: a worker can read state N, another worker transitions, then the first posts a claim whose id is newer than the marker and incorrectly wins the new epoch. Include the observed state and base transition-marker id in the claim, elect only claims matching the current epoch, and re-read/confirm before work and before transition. -->
<!-- codex: Pagination and sort semantics are load-bearing. The implementation must page all relevant comments and contract-test Gitea read-after-write, ordering, and long ledger histories so the real lowest claim or last marker is never hidden. -->
<!-- codex: Losing claimants should immediately withdraw or expire their own claim. Otherwise a live but non-owning claim can block recovery until TTL or become the apparent winner after the real winner expires with no worker actually owning it. -->
<!-- codex: A 45-minute lease makes crash recovery slow relative to a 60s reconciler. Tie TTL to per-run timeout or use shorter renewable leases if stuck-issue latency matters. -->
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
<!-- codex: Subscription OAuth durability is a day-0 risk: systemd services need explicit HOME/XDG/CLAUDE_HOME ownership, boot-time noninteractive auth canaries, and documented re-login handling when refresh tokens expire or are revoked. -->
<!-- codex: A fleet-wide per-account semaphore conflicts with "Gitea only durable state" unless it is implemented through the same forge-backed lease primitive or another explicitly accepted state source. Rate-window cooldowns should be persisted and reconciled, not handled only as concurrency=1. -->
  2. `ClaudeCodeRunner(auth=litellm_local)` — same CLI, `ANTHROPIC_BASE_URL=http://192.168.0.41:30400`
     (new litellm-lan NodePort), `ANTHROPIC_MODEL=qwen3.6-35b-a3b`,
     `ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3.6-35b-a3b`, `API_TIMEOUT_MS=900000`. Config validator
     hard-rejects non-local models on this engine.
<!-- codex: Verify Claude Code actually honors these Anthropic env vars against LiteLLM under systemd; this is a CLI behavior dependency, not just configuration. Also include auth/rate-limit/error envelope fixtures for this path. -->
  3. `CodexRunner` — `codex exec` headless (read-only sandbox for critique, `--cd <ws>`,
     `--skip-git-repo-check`, `--output-last-message <file>`) with Codex Pro OAuth.
<!-- codex: Codex Pro needs the same headless auth canary and reauth runbook as Claude. dw5/dw6 sharing one subscription also needs explicit cooldown behavior when the subscription refuses more work. -->
  Structured output via prompt-embedded JSON schema + pydantic validation (+1 repair pass via
  litellm-local); `--json-schema` when the CLI supports it (feature-detect). Least-privilege tools:
  Planner/Reviewer `Read,Glob,Grep`; Tester + `Write,Edit,Bash(<test_cmd>:*)`; Implementer full
  edit+Bash; NO git push / WebFetch for any role — the orchestrator commits/pushes as the role's
  bot and does every forge write via per-bot authenticated GiteaClients.
<!-- codex: Current dev-worker defaults install CLI packages by presence and allow runtime self-update, so output formats and flags can drift underneath parser code. Agentforge should assert tested CLI versions or pin/roll them deliberately. -->
- **Trust-but-verify orchestrator-side**: Tester — verify tests FAIL and diff ⊆ `test_paths` before
  pushing. Implementer — handler loop: agent → run `test_cmd` → on red re-invoke with failure tail
  (`--resume`), max 4 iterations → on green push + PR (`Closes #N`). Reviewer — structured verdict;
  orchestrator posts REQUEST_CHANGES review w/ inline comments (4→3) or APPROVED + squash-merge.
  Review rounds = count(REQUEST_CHANGES reviews by reviewer-bot) via API; `af:round:N` is display.
<!-- codex: Counting only REQUEST_CHANGES reviews can miss Codex alignment bounces before a PR review is posted. Use one shared round counter across Claude review, Codex review, and 4→3 transitions. -->
- **Codex alignment gates**: each gated stage runs produce → codex critique (structured verdict
  `{aligned, blocking_concerns[]}`) → primary agent addresses → repeat until aligned or 3 rounds.
  Plan gate (before 1→2/3 transition), implementation gate (tests green, before PR/state 4),
  review gate (Claude Reviewer AND independent Codex PR review must both approve before merge;
  disagreement feeds the 4→3 bounce). Cap breach at any gate → `needs-human` with disagreement
  summary. All rounds recorded as `af:xrev` ledger comments.
<!-- codex: The loop needs a deterministic stop rule for repeated unchanged blocking concerns or "no patch possible" cases, plus a wall-clock budget per stage. Three rounds across three stages can take many hours under subscription cooldowns before escalation appears. -->
- **Ledger**: append-only `af:run` comments (role, worker, engine, usage/turns/duration,
  transition). Issue totals = sum of comments; the forge is the audit log.
- **Guardrails**: max 3 review rounds; per-run max_turns + wall-clock timeout; per-issue total-runs
  cap (12); per-Max-account semaphore; implementer iteration cap (4); protected_paths diff →
  `needs-human`; auto-merge only on allowlisted repos; `needs-human` = global stop; `FORGE_PAUSED`
  = fleet kill switch. Gitea-side: branch protection required_approvals=1, reviewer-bot on
  approvals allowlist, impl-bot NOT on merge allowlist.
<!-- codex: `needs-human` and `FORGE_PAUSED` must be checked before every agent invocation and every forge write, not only at dequeue. Clarify where `FORGE_PAUSED` lives; an env-only flag is not an immediate fleet kill switch without service restarts. -->
- **Bots**: 4 Gitea users (planner-bot, tester-bot, impl-bot, reviewer-bot), scoped PATs.
<!-- codex: Because the orchestrator host can run untrusted tests, bot PATs should not be broadly present in child process environments. Scope tokens minimally and verify they are never exposed to agent subprocesses or repo test commands. -->
- **Dashboard**: dw1 runs extra `dashboard` role — sqlite event cache (disposable), REST + SSE,
  Vue 3 + Vite + Pinia SPA served by FastAPI StaticFiles; demo mode (FakeForge + scripted events)
  is the Playwright/local-dev target. Other workers fire-and-forget POST events to it.
- **Config**: repo `cchifor/agentforge-config`, single `agentforge.json` — `release` pin
  (deployment control plane), workers→roles (incl. `cross-reviewer`), `accounts` block,
  repos allowlist (+ setup_cmd/test_cmd/test_paths/protected_paths/auto_merge), per-role
  engine/model/max_turns/timeouts, `cross_review {engine, max_rounds: 3, stages}`, guardrails,
  labels, intervals, port. Refresh: org webhook on config push + 2-min poll. Invalid → last-good.
<!-- codex: "Invalid → last-good" requires a persisted last-good config on each worker. In-memory last-good fails after restart or fresh install when the config repo contains a bad commit. -->
<!-- codex: Add `schema_version` and minimum compatible agent version to config. Self-update plus partial fleet skew otherwise lets old workers interpret new labels, lock comments, or guardrails incorrectly. -->

### Testing (all deterministic in CI; no live LLM gates a PR)

- Unit (pytest + hypothesis; MockAgentRunner incl. malformed outputs; FakeForgeClient) — ≥95%
  branch cov on domain/, 85% overall. Vitest for the SPA.
- Contract tier vs real `gitea/gitea:1.26-rootless` in compose (claim primitive under race,
  self-approval 422, branch protection, webhook HMAC/delivery-id semantics) + fake-honesty suite
  (same behavioral tests parameterized over fake AND real client).
<!-- codex: Add contract cases for stale-epoch claims posted after a transition marker, loser withdrawal, lease expiry recovery, and comment pagination over a long ledger. "Claim primitive under race" is too broad unless these exact TOCTOU cases are included. -->
- Integration: compose `test` profile — gitea + 4 role containers (one image, FORGE_ROLE env) +
  llm-stub (canned Anthropic/OpenAI responses, scenario YAML, fault injection, record mode vs real
  litellm w/ sanitized committed traces) + fake-claude/fake-codex CLI shims (apply fixture patches,
  print faithful envelopes). Canonical scenario: seed issue at state 1 → assert full walk to
  merged + state 5 with audit comments. 21 failure-mode cases incl. gate convergence, cap-breach
  escalation, codex-down escalation.
<!-- codex: Include subscription-specific fake CLI failures: OAuth expired, browser-login required, rate-window cooldown text, partial JSON on timeout, and CLI flag/output changes. These are more likely operational failures than model reasoning quality. -->
- E2E: Playwright (in `mcr.microsoft.com/playwright` container) — 5 scenarios: kanban truth, feed,
  usage counters, escalation surface, reconnect resilience.
- CI (`.github/workflows/ci.yml`, runner `self-hosted-hv`): lint → unit → contract → integration →
  e2e → build; always-run `ci-gate` aggregator = sole required check; NO upload-artifact (broken on
  Gitea) — failure diagnostics to versitygw S3. `release.yml`: tag → tarball → Gitea generic
  package + config-repo pin bump. `model-drift.yml`: nightly vs real qwen3.6, non-blocking.
<!-- codex: Direct S3 diagnostics need a tested Gitea-compatible upload path and credentials because `upload-artifact@v4` is known broken here. Make this part of CI validation rather than assuming the fallback works. -->

### Deployment (this ailab PR)

- `kubernetes/apps/apps/gitea/gitea.yaml`: add `webhook: {ALLOWED_HOST_LIST: "192.168.0.0/24"}`
  under `values.gitea.config` — day-0 blocker (Gitea refuses webhook delivery to private IPs).
<!-- codex: Setting `ALLOWED_HOST_LIST` only to `192.168.0.0/24` may block existing or future external webhook targets if any exist. Verify Gitea 1.26 CIDR/private-host syntax and decide whether to preserve the default external allowance. -->
- NEW `kubernetes/apps/apps/ai/litellm-lan.yaml`: NodePort 4000→30400 (mirrors prometheus-lan
  pattern + rationale) + kustomization entry. Used ONLY for local qwen models.
<!-- codex: If this Service selects the existing LiteLLM Deployment, it exposes the shared cloud-enabled proxy to the LAN. The PR needs either a separate local-only Deployment/ConfigMap or an enforceable model-scoped key, plus a smoke test that cloud model requests are rejected. -->
- `ansible/roles/dev_worker/`: NEW `tasks/agentforge.yml` + templates (`agentforge.service.j2`,
  `agentforge.env.j2`, memory-cap drop-in MemoryHigh=6G/MemoryMax=8G, `agentforge-update.{service,
  timer}` + `files/agentforge-update.sh`): `/opt/agentforge/releases/<ver>` + `current` symlink,
  `uv sync --frozen`, 2-min self-update from config-repo `release` pin → Gitea generic package
  tarball (keep last 3; rollback = revert pin commit). Wire into `tasks/main.yml` after jobs,
  gated `when: dev_worker_enable_agentforge | bool`; defaults; firewall allow tcp/8700 from LAN;
  restart handler.
<!-- codex: The update script needs atomic download/extract/symlink switch, checksum or signature validation, `flock`, post-switch health check, and automatic rollback. A half-downloaded tarball or failed `uv sync` should never replace `current`. -->
<!-- codex: A 2-minute pull timer creates partial-fleet version skew by design. Include release compatibility rules and metrics showing each worker's active version before shipping protocol-changing releases. -->
<!-- codex: Generic package delivery depends on Gitea packages being enabled, immutable enough for release use, and readable from LAN workers with a package-scoped token. Add a package API smoke test and define overwrite behavior for an existing version. -->
- `ansible/dev-workers.yml`: extend SOPS pre_task `when:` with the agentforge toggle (also fix the
  existing gap: git_forge.yml's `dev_worker_gitea_token` isn't in the list today).
- `.sops.yaml`: extend dev-worker `encrypted_regex` with `|dev_worker_agentforge_.*`. New keys:
  `dev_worker_agentforge_webhook_secret`, `dev_worker_agentforge_litellm_key`,
  `dev_worker_agentforge_{planner,tester,impl,reviewer}_token`.
<!-- codex: Extending `.sops.yaml` is not enough; update `ansible/secrets/dev-worker.sops.yaml.example` and ensure the real encrypted secret file contains these keys. Otherwise contributors can add plaintext values or run playbooks without the expected variables. -->
- NEW `kubernetes/apps/infrastructure/monitoring/agentforge.yaml` (Service+Endpoints .8-.13:9464 +
  ServiceMonitor, cloned from dev-workers-node.yaml) + `agentforge-rules.yaml` (ForgeWorkerDown,
  ForgeIssueStuck, ForgeNeedsHumanPending, ForgeWebhookHMACFailures, ForgeReconcileDriftHigh).
<!-- codex: Add `kubernetes/apps/infrastructure/monitoring/kustomization.yaml` to the PR scope or these new resources will not reconcile. Also fix the existing `dev-workers-node.yaml` comment that still says 3 workers/.37-.39. -->
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
<!-- codex: Webhooks should be reconciled by stable name/URL and updated or deleted when topology changes; get-then-create alone can leave stale duplicate fan-out. Include test delivery, HMAC validation, and behavior when one worker endpoint is down. -->
<!-- codex: The bootstrap must not log PATs except the intentional print-once path, and the SOPS ingestion path should be part of the runbook. Bot PAT scopes should exclude admin/package delete unless a specific operation requires them. -->

### Milestones

M1 domain core (TDD) → M2 GiteaClient+ClaimService+contract tier → M3 orchestrator+webhook+
reconciler with FakeRunner (full 1→5 lifecycle, no LLM; codex cross-review of the nervous system)
→ M4 runners + handlers + alignment gates → M5 ledger+SSE+dashboard+Playwright+CI → M6 this ailab
PR + codex Phase B + push agentforge to Gitea + PRs.
<!-- codex: Put claim-lock proof before broad orchestration work, not just inside M2, because many later components assume it is correct. The stale-transition and pagination cases are the highest-risk part of the design. -->

### Rollout (day 0 → autonomous)

Phase 0: merge ailab PR (webhook allowlist + litellm-lan) → subscription logins (Max#1 dw1/dw2,
Max#2 dw3/dw4, Codex dw5; `claude setup-token`) → bootstrap_gitea.py → SOPS secrets → CI green →
tag v0.1.0 → `release: 0.1.0` pin → `dev_worker_enable_agentforge: true` → `just dev-workers` →
webhook test-deliveries 2xx ×6.
<!-- codex: Add an unattended auth validation after reboot/systemd restart on every worker. Interactive CLI success in a shell does not prove the agent service can refresh or use subscription credentials. -->
<!-- codex: Webhook test deliveries should verify HMAC handling, UFW source IP, and Gitea retry behavior for one intentionally stopped worker. Six happy-path 2xx responses only prove reachability. -->
Phase 1: dw1 all roles, playground only, merge disabled (shadow) → pilot issue → inspect → enable
merge → full lifecycle smoke.
<!-- codex: "dw1 all roles" conflicts with the dedicated Codex Pro topology unless Phase 1 routes cross-review to dw5, disables Codex gates, or logs Codex into dw1. Keep the pilot topology consistent with the binding alignment gate. -->
Phase 2: chaos (kill mid-task/lease recovery, manual label flip, low caps).
Phase 3: role-per-worker fleet-wide, 2 implementers (live claim contention), ~10-issue 48h soak.
Phase 4: dogfood — agentforge PR#1 merged BY HAND (codex+human review; no circular trust), then
PR#2+ flow through the deployed system; reviewer-bot merges with dual Claude+Codex approval.
Phase 5: onboard repos one at a time; weekly smoke timer. Rollback anywhere: `FORGE_PAUSED` or
`systemctl stop agentforge` — state lives in Gitea.
<!-- codex: `systemctl stop agentforge` is per-worker and can leave claims active until lease expiry. The rollback runbook should include claim cleanup/expiry and a fleet-wide pause mechanism with observed propagation time. -->

## Critical files

- ailab (this PR): `kubernetes/apps/apps/gitea/gitea.yaml`, `kubernetes/apps/apps/ai/litellm-lan.yaml`
  (+ kustomization), `ansible/roles/dev_worker/tasks/{main,agentforge,firewall}.yml` + new
  templates/files, `ansible/roles/dev_worker/defaults/main.yml`, `ansible/dev-workers.yml`,
  `.sops.yaml`, `ansible/secrets/dev-worker.sops.yaml`,
  `kubernetes/apps/infrastructure/monitoring/{agentforge,agentforge-rules}.yaml`,
  `docs/decisions/0018-*.md`, `docs/runbooks/agentforge.md`, `CLAUDE.md`.
<!-- codex: This list is missing `ansible/group_vars/dev_workers.yml`, `ansible/secrets/dev-worker.sops.yaml.example`, and `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`; all are required by repo conventions. If `litellm-lan.yaml` is new, name `kubernetes/apps/apps/ai/kustomization.yaml` explicitly instead of only saying "(+ kustomization)". -->
- agentforge repo: `src/agentforge/app/orchestrator.py` (core loop), `app/claims.py` (comment
  lock — the novel mechanism, proven first), `adapters/gitea/client.py`, `domain/states.py`,
  runners, handlers, `compose/`, `scripts/bootstrap_gitea.py`, `.github/workflows/*`.

## Verification

- `uv run pytest` (unit, <60s) · `docker compose --profile contract up --wait && pytest -m contract`
  · `--profile test` integration full-lifecycle + failure modes · `npx playwright test` (demo +
  compose) · CI green on Gitea Actions with `ci-gate` aggregator.
<!-- codex: Add explicit claim-lock verification with concurrent real Gitea clients for stale post-transition claims, loser withdrawal, lease expiry recovery, and comment pagination. This should be a named required contract subset. -->
- ailab k8s: Flux reconciles; `kubectl --context admin@ai -n ai get svc litellm-lan`; webhook
  test-delivery 2xx; ServiceMonitor targets up on prometheus-lan :30090.
<!-- codex: Add negative LiteLLM verification: a request for a cloud model through `litellm-lan` must fail, while qwen succeeds. Also verify Gitea `ALLOWED_HOST_LIST` did not break any intended existing webhook target. -->
- Live smoke `scripts/smoke-ailab.sh`: canary issue with production engines, per-stage SLOs,
  distinct-bot authorship/approval/merge asserted, cleanup + reset.
<!-- codex: Add Ansible/systemd verification: check-mode or equivalent, `systemd-analyze verify` for new units/timers, reboot a worker, and prove the service starts with valid Claude/Codex auth. -->
<!-- codex: Add update-path verification: publish vA/vB tarballs, bump the config pin, simulate one failed worker update, rollback the pin, and confirm workers never run a broken `current` symlink. -->
- Dogfood criterion: an agentforge feature issue flows 1→5 autonomously; reviewer-bot merges;
  release pipeline deploys it.

<!-- codex-review-status: complete -->