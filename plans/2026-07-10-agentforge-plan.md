# AgentForge — Autonomous Agentic Development System on ailab (Gitea + dev-workers)

## Codex Review

- ALIGNED: The five round-2 concerns are resolved in-plan, with no round-3 blocker or new contradiction found.
- The v1 playground enforcement and Phase 4/Phase 5 rollout now agree: non-playground allowlists are blocked until `privilege_hardening: "v1.1"` after dedicated-user/container hardening.
- The marker-first `af:run` transition, litellm-local child-only token wiring, and update restart/version/MainPID contract are each reflected in corresponding tests/smokes and consistent with the rest of the plan.

## Context

Build a fully autonomous, label-driven multi-agent software development system on existing ailab
infrastructure: Gitea 1.26 (`git.chifor.me`, org `cchifor`) as forge + source of truth, the 6
dev-worker VMs (dw1–dw6, 192.168.0.8–.13) as agent hosts, act_runner CI (`self-hosted-hv`), and
litellm for local-model access. The companion docs fix updates EVERY stale operator-facing
dev-worker reference (.37–.39 → .8–.13): CLAUDE.md table AND stale comments in
`kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml`.

Lifecycle (user spec) — issue labels are the state machine:
`state: 1-needs-plan` →Planner→ `state: 2-needs-tests` (skippable) →Tester→
`state: 3-ready-to-code` →Implementer→ `state: 4-in-review` →Reviewer→ merge → `state: 5-completed`,
with Reviewer able to bounce 4→3. Webhook-driven; JSON config in a Gitea repo maps workers→roles.

Settled decisions:
- **LLM engines = existing subscriptions + local models ONLY**: 2× Claude Max (dev-workers'
  `claude` OAuth logins; Max#1 on dw1/dw2, Max#2 on dw3/dw4) + 1× Codex Pro (`codex` OAuth on
  dw5/dw6) + local qwen. **Local-only litellm is enforced structurally, not by app-side filtering
  alone**: a NEW separate `litellm-local` Deployment (same image, its own ConfigMap listing ONLY
  {qwen3.6-35b-a3b, qwen3.5-122b}, its own master key, NO cloud-key env vars) is what the LAN
  NodePort selects — the cloud-enabled litellm stays ClusterIP-only. The agent config validator
  additionally whitelists local models (defense in depth).
- Default role engines: Planner=Max#1 · Implementer=Max#2 · Reviewer=Max#1 ·
  Tester=qwen3.6-35b-a3b (litellm-local) · **Codex Pro = dedicated cross-reviewer**.
- **Codex cross-review alignment gates (binding)** with ONE shared budget definition (see
  Guardrails): plan, implementation, and review stages each end with a Claude↔Codex loop
  iterating until alignment, hard cap 3 rounds per stage; breach → `needs-human`. The same rule
  governs building the system itself.
- App code in NEW repo `cchifor/agentforge`; this ailab PR carries only infra/deploy changes.
- Vue 3 dashboard in scope (user-binding; Playwright e2e target) — but OFF the critical path:
  `dashboard` is a feature-flagged optional role, built last (M5), never blocking autonomy
  hardening work.
- Full auto-merge by reviewer-bot on config-allowlisted repos, with guardrails.

**Governing principle**: Gitea is the only durable state store. Webhooks are hints; labels are
levels; a reconcile poll is the guarantee. Agents never write to the forge — the orchestrator does
all forge writes. Stateless role workers; no Redis/queue infra.
**State taxonomy (explicit)**: durable-in-Gitea = labels, claim/transition/`af:run`/`af:xrev`
comments, config repo (incl. `release` pin + `FORGE_PAUSED`). Static-by-config = account→worker
topology (the "per-account semaphore" reduces to disjoint worker sets × per-worker concurrency 1 —
no cross-worker runtime coordination exists). Disposable-local = in-memory queue, delivery-id LRU,
rate-cooldown backoff timers, dashboard sqlite cache. Nothing else.

## Approach

### Service (new repo `cchifor/agentforge`)

One Python 3.12 service deployed identically to all 6 workers as a host systemd unit — NOT
containerized in prod (it drives host tools: `claude`/`codex` CLIs + OAuth homes, git creds,
docker, `/workspace`). In-process `asyncio.Queue`; a 60s reconciler re-discovers all work from
labels after any crash.

**Privilege model (staged, explicit)**: v1 runs as `c4` restricted to the playground allowlist,
with strict credential scoping: agent subprocesses and repo `test_cmd` run with a SCRUBBED env
(no bot PATs, no webhook HMAC, no litellm key beyond the role's need); bot PATs live only in the
orchestrator process; git push auth is injected per-invocation via `-c http.extraHeader`, never
written to disk or child env. **Gate for onboarding any repo beyond playground (v1.1, tracked as
an ADR-0018 condition): dedicated non-sudo `agentforge` service user + repo `test_cmd` executed in
a docker container (workspace-mounted, no creds, no network by default).** The ADR documents this
threat model, EXPLICITLY including the same-UID residual risk: under one UID, env scrubbing is
hygiene, not a boundary (same-UID processes can read /proc/<pid>/environ, plant watchers, race
command lines). Therefore v1's playground-only limit is ENFORCED IN CODE, not convention: the
config validator refuses any allowlist beyond `cchifor/agentforge-playground` until the config
carries an explicit `privilege_hardening: "v1.1"` acknowledgment (set only after the dedicated
user + containerized test_cmd land).

- **Events**: 6 org-level Gitea webhooks (one per worker, `http://192.168.0.{8..13}:8700/webhook`,
  shared HMAC, events issues/issue_comment/pull_request/pull_request_review/push). Workers filter
  by "my roles handle this state" → enqueue IssueRef hint. Level-triggered: re-read the issue on
  dequeue, act on current labels, never the payload. Dedup on `X-Gitea-Delivery` (LRU). The 6×
  fan-out is intentional: the claim protocol is the real admission control (cross-worker duplicate
  suppression), and stopped-worker webhook retry/backlog behavior is explicitly tested in rollout.
- **Claim lock — epoch-bound protocol (v2, race-analyzed)**. Gitea labels/assignees are
  last-write-wins; issue comments are append-only with ordered ids — the only usable primitive.
  1. Worker reads the issue: current state label + `base` = comment id of the latest
     `af:run` transition marker (0 if none). All comment reads PAGINATE to exhaustion
     (contract-tested: read-after-write, ordering, long histories).
  2. Claim = post `<!-- af:claim {role, worker, job, state, base, expires} -->` as the role's bot.
     A claim is VALID only if its embedded `state`+`base` match the issue's current state and
     latest transition-marker id — a claim posted after someone else's transition is stale by
     construction (TOCTOU closed).
  3. Election: among valid, unexpired, unreleased claims for this role, lowest comment id wins.
     After posting, the worker re-reads and confirms: state/base unchanged AND own claim is the
     winner; otherwise it immediately WITHDRAWS (PATCH own claim `released:true`) and drops the
     item. Losers always withdraw at once — no zombie claims blocking recovery.
  4. Heartbeat = PATCH own claim's `expires` every 60s. Lease TTL is derived: `run timeout × 1.5`
     (min 10 min) — not a fixed 45 min, so crash recovery latency tracks actual run length.
     Reaper (reconciler) treats expired claims as dead. Graceful shutdown (SIGTERM) releases all
     held claims before exit.
  5. **The transition marker IS the transition (single atomic write).** State is derived
     MARKER-FIRST: current state = the `to` of the latest `af:run` transition marker; the state
     label is only authoritative for issues with no marker yet (fresh human intake). Deterministic
     write order: post the marker (authoritative, atomic) → then flip the label (human-visible
     mirror/trigger). There is no two-write race: claim validity keys on (state-from-markers,
     latest-marker-id), so a worker that reads a stale or early label still resolves the same
     epoch from the marker chain; the reconciler heals marker↔label divergence (re-flips the label
     to the marker's state after a crash between the two writes). This exact gap — crash/read
     between marker post and label flip — is a named contract-claims test case.
  6. Before EVERY transition, the worker revalidates: own claim still valid+winning AND
     marker-derived state/base unchanged; any foreign change → abort the step, release, requeue.
     Assignee + `af:wip:dwN` remain cosmetic mirrors.
- **Clean architecture** (`src/agentforge/`): `domain/` (states, models, policy — pure), `ports/`
  (ForgeClient, AgentRunner, ConfigSource, EventSink Protocols), `adapters/` (gitea
  client+webhook+labels, runners claude_code+codex+litellm_chat, config gitea_repo, events
  sqlite+http_push), `app/` (orchestrator, claims, workspace, ledger, prompts/*.j2,
  handlers/{planner,tester,implementer,reviewer}), `infra/` (FastAPI api, reconciler, sse,
  settings, logging), `main.py` composition root.
- **Three runner adapters** (per-role `engine` in config):
  1. `ClaudeCodeRunner(auth=subscription)` — `claude -p … --output-format json --permission-mode
     dontAsk --max-turns N --allowedTools <role list>` with the worker's Max OAuth. Headless auth
     durability is a first-class requirement: the unit pins HOME/CLAUDE_HOME/CODEX_HOME (mirroring
     `claude-job@.service.j2`), startup + `/readyz` run a NON-INTERACTIVE AUTH CANARY (cheap
     `claude -p` probe), and the runbook documents re-login when refresh tokens expire.
     Rate-window handling: on rate-limit/auth-refusal the worker backs off locally (disposable
     state), posts an `af:run` failure, and the reconciler retries later; 2 consecutive auth
     failures → `needs-human` + alert.
  2. `ClaudeCodeRunner(auth=litellm_local)` — same CLI, `ANTHROPIC_BASE_URL=http://192.168.0.41:30400`
     (litellm-local NodePort), `ANTHROPIC_AUTH_TOKEN=<litellm-local master key>` (from
     /etc/agentforge env; injected into THIS child env only — never into subscription-mode or
     test_cmd envs), `ANTHROPIC_MODEL=qwen3.6-35b-a3b`,
     `ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3.6-35b-a3b`, `API_TIMEOUT_MS=900000`. That the CLI honors
     these env vars against litellm under systemd is a VERIFIED dependency (M4 smoke: real CLI vs
     llm-stub AND an AUTHENTICATED call vs real litellm-local), with error-envelope fixtures (auth
     expired/401, rate-limit text, partial JSON on timeout).
  3. `CodexRunner` — `codex exec` headless (read-only sandbox for critique, `--cd <ws>`,
     `--skip-git-repo-check`, `--output-last-message <file>`) with Codex Pro OAuth; same auth
     canary + reauth runbook; on subscription refusal the gate DEFERS (backoff + retry, ledger
     entry) rather than skipping — and escalates after the stage wall-clock budget.
  **CLI version drift control**: config carries `expected_cli_versions` (semver ranges); startup
  asserts `claude --version` / `codex --version` in range else the worker marks itself degraded
  (`/readyz` fail + alert, no claims). Envelope-format regression fixtures pin the parsed shapes.
  Structured output via prompt-embedded JSON schema + pydantic validation (+1 repair pass via
  litellm-local); `--json-schema` when the CLI supports it (feature-detect). Least-privilege
  tools: Planner/Reviewer `Read,Glob,Grep`; Tester + `Write,Edit,Bash(<test_cmd>:*)`; Implementer
  full edit+Bash; NO git push / WebFetch for any role — the orchestrator commits/pushes as the
  role's bot and does every forge write via per-bot authenticated GiteaClients.
- **Trust-but-verify orchestrator-side**: Tester — verify tests FAIL and diff ⊆ `test_paths`
  before pushing. Implementer — handler loop: agent → run `test_cmd` → on red re-invoke with
  failure tail (`--resume`), max 4 iterations → on green push + PR (`Closes #N`). Reviewer —
  structured verdict; orchestrator posts REQUEST_CHANGES review w/ inline comments (4→3) or
  APPROVED + squash-merge.
- **Codex alignment gates + ONE shared budget**: each gated stage runs produce → codex critique
  (structured verdict `{aligned, blocking_concerns[], unresolvable}`) → primary agent addresses →
  repeat. Stop rules (deterministic): aligned; OR 3 rounds in the stage; OR the same blocking
  concern repeats verbatim-equivalent twice; OR `unresolvable=true`; OR stage wall-clock budget
  exhausted (default 2h/stage — subscription cooldowns count against it). Gate placement: plan
  gate (before 1→2/3), implementation gate (tests green, before PR/state 4), review gate (Claude
  Reviewer AND independent Codex PR review must both approve before merge; disagreement feeds the
  4→3 bounce). **Unified round accounting** in the `af:xrev` ledger: per-stage gate rounds ≤3,
  per-issue total codex rounds ≤9, review bounces (4→3) ≤3, per-issue wall-clock ≤8h — breach of
  ANY → `needs-human` with a disagreement summary comment. Review-bounce count = REQUEST_CHANGES
  reviews by reviewer-bot ∪ review-gate disagreements (both recorded as `af:xrev`, counted
  together).
- **Ledger**: append-only `af:run` comments (role, worker, engine, usage/turns/duration,
  transition) + `af:xrev` (gate rounds). Issue totals = sum of comments; the forge is the audit
  log.
- **Guardrails**: the unified budget above; per-run max_turns + wall-clock timeout; per-issue
  total-runs cap (12); implementer iteration cap (4); protected_paths diff → `needs-human`;
  auto-merge only on allowlisted repos; `needs-human` = global stop; `FORGE_PAUSED` lives IN THE
  CONFIG JSON (webhook push → near-instant propagation; 2-min poll floor; no restart needed) and,
  with `needs-human`, is checked by `policy.pre_check` BEFORE every agent invocation AND before
  every forge write batch (not only at dequeue). Gitea-side: branch protection
  required_approvals=1, reviewer-bot on approvals allowlist, impl-bot NOT on merge allowlist.
- **Bots**: 4 Gitea users (planner-bot, tester-bot, impl-bot, reviewer-bot), minimally-scoped PATs
  (no admin, no package-delete). PATs never appear in agent/test child envs (see privilege model).
- **Dashboard** (feature-flagged role, dw1): sqlite event cache (disposable), REST + SSE, Vue 3 +
  Vite + Pinia SPA served by FastAPI StaticFiles; demo mode (FakeForge + scripted events) is the
  Playwright/local-dev target. Other workers fire-and-forget POST events to it.
- **Config**: repo `cchifor/agentforge-config`, single `agentforge.json` — `schema_version` +
  `min_agent_version` (a worker older than min, or seeing a newer schema_version than it
  understands, marks degraded and claims nothing — prevents skewed-fleet misinterpretation),
  `release` pin + `release_sha256` (deployment control plane), `FORGE_PAUSED`, workers→roles
  (incl. `cross-reviewer`, `dashboard`), `accounts` topology, repos allowlist (+ setup_cmd/
  test_cmd/test_paths/protected_paths/auto_merge), per-role engine/model/max_turns/timeouts,
  `cross_review` budget block, `expected_cli_versions`, guardrails, labels, intervals, port.
  Refresh: org webhook on config push + 2-min poll. **Last-good config is PERSISTED**
  (`/var/lib/agentforge/config-lastgood.json`): bad remote config after restart/fresh-install →
  run on persisted last-good, `/readyz` degraded; no persisted copy → refuse to claim.

### Testing (all deterministic in CI; no live LLM gates a PR)

- Unit (pytest + hypothesis; MockAgentRunner incl. malformed outputs; FakeForgeClient) — ≥95%
  branch cov on domain/, 85% overall. Vitest for the SPA.
- Contract tier vs real `gitea/gitea:1.26-rootless` in compose + fake-honesty suite (same
  behavioral tests parameterized over fake AND real client). **Named required subset
  `contract-claims`** (gates M3): stale-epoch claim posted after a foreign transition loses;
  loser immediate-withdrawal; lease expiry recovery; comment pagination over a 100+-comment
  ledger; read-after-write + ordering; self-approval 422; branch protection; webhook
  HMAC/delivery-id semantics.
- Integration: compose `test` profile — gitea + 4 role containers (one image, FORGE_ROLE env) +
  llm-stub (canned Anthropic/OpenAI responses, scenario YAML, fault injection, record mode vs real
  litellm w/ sanitized committed traces) + fake-claude/fake-codex CLI shims (apply fixture
  patches, print faithful envelopes — INCLUDING subscription-operational failures: OAuth expired,
  browser-login-required, rate-window cooldown text, partial JSON on timeout, changed
  flags/output). Canonical scenario: seed issue at state 1 → assert full walk to merged + state 5
  with audit comments. Failure-mode list (24): duplicate delivery; claim race; stale-epoch claim;
  SIGKILL mid-task + lease recovery + no duplicate PR; garbage/timeout LLM; round caps; run-count
  cap; Gitea 500 between writes; Gitea restart; bad config (startup + hot-reload + persisted
  last-good); merge conflict on bounce; bad HMAC; lost webhook→reconciler; self-approval 422;
  long-task heartbeat; human label flip mid-flight; crash after PR-create; usage accumulation
  race; gate convergence round 2; gate cap breach → needs-human; repeated-concern stop rule;
  codex-down defer-then-escalate; CLI version mismatch degrade; FORGE_PAUSED propagation.
- E2E: Playwright (in `mcr.microsoft.com/playwright` container) — 5 scenarios: kanban truth, feed,
  usage counters, escalation surface, reconnect resilience.
- CI (`.github/workflows/ci.yml`, runner `self-hosted-hv`): lint → unit → contract → integration →
  e2e → build; always-run `ci-gate` aggregator = sole required check; NO upload-artifact (broken
  on Gitea) — failure diagnostics via a versitygw S3 upload step that is ITSELF validated by a CI
  self-test (upload+HEAD a probe object; on S3 failure degrade to inline log dump, never fail the
  job for diagnostics). `release.yml`: tag → tarball + sha256 → Gitea generic package (REFUSES to
  overwrite an existing version — releases are immutable) + config-repo pin bump (`release` +
  `release_sha256`). `model-drift.yml`: nightly vs real qwen3.6, non-blocking.

### Deployment (this ailab PR)

- `kubernetes/apps/apps/gitea/gitea.yaml`: add under `values.gitea.config`:
  `webhook: { ALLOWED_HOST_LIST: "external, 192.168.0.0/24" }` — day-0 blocker (Gitea refuses
  webhook delivery to private IPs by default). `external` PRESERVES the default allowance for any
  existing/future public webhook targets; the CIDR adds the LAN receivers. Syntax verified against
  Gitea 1.26 docs (comma-separated names/CIDRs); verification includes a no-regression check on
  existing webhooks.
- NEW `kubernetes/apps/apps/ai/litellm-local.yaml`: separate minimal litellm Deployment
  (local-models-only ConfigMap: qwen3.6-35b-a3b → llm-node1/2, qwen3.5-122b → llm-qwen35; own
  master key Secret `litellm-local-secret`; NO cloud-key env) + NodePort Service `litellm-lan`
  4000→30400 selecting THIS deployment (header comment mirrors prometheus-lan rationale). Explicit
  entry in `kubernetes/apps/apps/ai/kustomization.yaml`. Negative verification: cloud model names
  must 4xx through the NodePort while qwen succeeds.
- `ansible/roles/dev_worker/`: NEW `tasks/agentforge.yml` + templates (`agentforge.service.j2`,
  `agentforge.env.j2`, memory-cap drop-in MemoryHigh=6G/MemoryMax=8G,
  `agentforge-update.{service,timer}` + `files/agentforge-update.sh`): `/opt/agentforge/releases/
  <ver>` + `current` symlink, `uv sync --frozen`, 2-min self-update from config-repo pin. The
  update script is HARDENED — the atomic update contract is: `flock` singleton; download to tmp;
  sha256 verified against the config's `release_sha256`; extract + `uv sync` in the versioned dir;
  atomically flip `current`; **`systemctl restart agentforge`; then poll `/healthz` (≤60s) and
  require it to report `version == <pinned release>`** (the endpoint serves the running build's
  version, so a check can never pass against the old in-memory process) **and a changed systemd
  MainPID; on any failure: flip the symlink back AND restart again onto the previous release**
  (rollback includes its own restart + version-matched health check); beacon on rollback; keep
  last 3 releases. Version skew is bounded by config `min_agent_version` (old workers degrade
  rather than misbehave) and observable via `forge_build_info{version}` per worker;
  protocol-changing releases follow pause → pin-bump → resume. Wire into `tasks/main.yml` after
  jobs, gated `when: dev_worker_enable_agentforge | bool`; defaults; firewall allow tcp/8700 from
  LAN; restart handler.
- `ansible/dev-workers.yml`: extend SOPS pre_task `when:` with the agentforge toggle (also fix the
  existing gap: git_forge.yml's `dev_worker_gitea_token` isn't in the list today).
- `.sops.yaml`: extend dev-worker `encrypted_regex` with `|dev_worker_agentforge_.*`. New keys in
  BOTH `ansible/secrets/dev-worker.sops.yaml` (real, re-encrypted) AND
  `ansible/secrets/dev-worker.sops.yaml.example` (documented):
  `dev_worker_agentforge_webhook_secret`, `dev_worker_agentforge_litellm_key` (the litellm-LOCAL
  master key), `dev_worker_agentforge_{planner,tester,impl,reviewer}_token`.
- `ansible/group_vars/dev_workers.yml`: `dev_worker_enable_agentforge: true` at go-live.
- NEW `kubernetes/apps/infrastructure/monitoring/agentforge.yaml` (Service+Endpoints .8-.13:9464 +
  ServiceMonitor, cloned from dev-workers-node.yaml) + `agentforge-rules.yaml` (ForgeWorkerDown,
  ForgeIssueStuck, ForgeNeedsHumanPending, ForgeWebhookHMACFailures, ForgeReconcileDriftHigh) —
  BOTH added to `kubernetes/apps/infrastructure/monitoring/kustomization.yaml` (required for Flux
  to reconcile them). Fix the stale 3-workers/.37-.39 comment in `dev-workers-node.yaml`.
- `docs/decisions/0018-agentforge-autonomous-dev-agents.md` (incl. threat model + v1.1 hardening
  gate) + `docs/runbooks/agentforge.md` (bootstrap, pause, re-login, rollback, claim cleanup,
  canary smoke).
- CLAUDE.md dev-worker fixes (.37-.39 → .8-.13, 6 workers).

### Gitea provisioning

`scripts/bootstrap_gitea.py` in the agentforge repo (admin PAT; idempotent RECONCILE, not just
get-then-create): repos `agentforge-config` + `agentforge-playground`; 4 bot users + minimal
scoped tokens (Sudo mint, print-once — PATs never logged elsewhere; SOPS ingestion documented in
the runbook); org team `agentforge-bots`; org labels (5 state + needs-human + af:*; int64 label
IDs per forge-migration experience); **webhooks reconciled by URL** (update-in-place, delete
stale af-managed hooks — no duplicate fan-out) + test-delivery incl. HMAC validation and
one-worker-down retry observation; branch protection on playground (required_approvals=1,
approvals_whitelist=[reviewer-bot,chifor], merge_whitelist=[reviewer-bot,chifor],
block_on_rejected_reviews, required context `CI / ci-gate*`). Package-registry smoke: upload +
token-download + immutability probe of a test artifact. Same module seeds the compose stack.

### Milestones

M1 domain core (TDD) → **M2 GiteaClient+ClaimService — HARD GATE: the `contract-claims` subset
must be green against real Gitea before ANY orchestrator work starts** (the claim lock is the
highest-risk novel mechanism; everything downstream assumes it) → M3 orchestrator+webhook+
reconciler with FakeRunner (full 1→5 lifecycle, no LLM; codex cross-review of the nervous system)
→ M4 runners + handlers + alignment gates → M5 ledger+SSE+dashboard+Playwright+CI → M6 this ailab
PR + codex Phase B + push agentforge to Gitea + PRs.

### Rollout (day 0 → autonomous)

Phase 0: merge ailab PR (webhook allowlist + litellm-local) → subscription logins (Max#1 dw1/dw2,
Max#2 dw3/dw4, Codex dw5+dw6; `claude setup-token`) → **unattended-auth validation: reboot each
worker and prove the agentforge service passes its auth canary non-interactively** →
bootstrap_gitea.py (incl. package + webhook smokes: HMAC-valid delivery, HMAC-invalid rejected,
UFW source check, one intentionally stopped worker → observe Gitea retry/backlog behavior) → SOPS
secrets → CI green → tag v0.1.0 → pin `release: 0.1.0` + sha256 →
`dev_worker_enable_agentforge: true` → `just dev-workers` (×2; plus `ansible-playbook --check`
first and `systemd-analyze verify` on the new units).
Phase 1: **dw1 (Claude roles + dashboard) + dw5 (cross-reviewer, Codex login)** — topology stays
consistent with the binding alignment gates; playground only, merge disabled (shadow) → pilot
issue → inspect → enable merge → full lifecycle smoke.
Phase 2: chaos (kill mid-task/lease recovery, manual label flip, low caps, update-path drill:
publish vA/vB, pin-bump, one worker's update deliberately failed → health-check rollback proves
`current` never breaks, then pin revert).
Phase 3: role-per-worker fleet-wide, 2 implementers (live claim contention), ~10-issue 48h soak.
Phase 4: **v1.1 privilege hardening lands FIRST** (dedicated non-sudo `agentforge` service user,
containerized `test_cmd`, ansible follow-up) — this is the gate for ANY repo beyond playground,
and the dogfood target is exactly such a repo (its code runs on the workers and touches the
deploy pipeline). Config's `privilege_hardening: "v1.1"` ack is set only now.
Phase 5: dogfood — agentforge PR#1 merged BY HAND (codex+human review; no circular trust), then
`cchifor/agentforge` enters the allowlist and PR#2+ flow through the deployed system;
reviewer-bot merges with dual Claude+Codex approval.
Phase 6: onboard further repos one at a time; weekly smoke timer. Rollback anywhere: set
`FORGE_PAUSED` in config FIRST (propagation ≤2 min incl. webhook push; workers finish/release
claims and stop claiming), then `systemctl stop agentforge` if needed; SIGTERM releases held
claims, expired leases cover crashes; runbook documents observed propagation time.

## Critical files

- ailab (this PR): `kubernetes/apps/apps/gitea/gitea.yaml` ·
  `kubernetes/apps/apps/ai/litellm-local.yaml` + `kubernetes/apps/apps/ai/kustomization.yaml` ·
  `ansible/roles/dev_worker/tasks/{main,agentforge,firewall}.yml` + new templates/files ·
  `ansible/roles/dev_worker/defaults/main.yml` · `ansible/dev-workers.yml` ·
  `ansible/group_vars/dev_workers.yml` · `.sops.yaml` · `ansible/secrets/dev-worker.sops.yaml` +
  `ansible/secrets/dev-worker.sops.yaml.example` ·
  `kubernetes/apps/infrastructure/monitoring/{agentforge,agentforge-rules}.yaml` +
  `kubernetes/apps/infrastructure/monitoring/kustomization.yaml` +
  `kubernetes/apps/infrastructure/monitoring/dev-workers-node.yaml` (stale comment) ·
  `docs/decisions/0018-agentforge-autonomous-dev-agents.md` · `docs/runbooks/agentforge.md` ·
  `CLAUDE.md`.
- agentforge repo: `src/agentforge/app/orchestrator.py` (core loop), `app/claims.py` (epoch-bound
  comment lock — the novel mechanism, proven first), `adapters/gitea/client.py`,
  `domain/states.py`, runners, handlers, `compose/`, `scripts/bootstrap_gitea.py`,
  `.github/workflows/*`.

## Verification

- `uv run pytest` (unit, <60s) · `docker compose --profile contract up --wait && pytest -m
  contract` — incl. the named `contract-claims` subset (stale-epoch claims, loser withdrawal,
  lease expiry recovery, pagination) run with CONCURRENT real Gitea clients ·
  `--profile test` integration full-lifecycle + the 24 failure modes · `npx playwright test`
  (demo + compose) · CI green on Gitea Actions with `ci-gate` aggregator (incl. the S3
  diagnostics self-test).
- ailab k8s: Flux reconciles; `kubectl --context admin@ai -n ai get svc litellm-lan` + NEGATIVE
  test (cloud model 4xx via NodePort, qwen 200) + `ALLOWED_HOST_LIST` no-regression check on
  existing webhooks; ServiceMonitor targets up on prometheus-lan :30090.
- Ansible/systemd: `ansible-playbook --check` clean; `systemd-analyze verify` on new units/timers;
  worker reboot → service starts AND auth canary passes unattended.
- Update path: vA→vB pin-bump converges the fleet; deliberately failed update on one worker →
  automatic rollback, `current` never broken; pin revert converges back.
- Live smoke `scripts/smoke-ailab.sh`: canary issue with production engines, per-stage SLOs,
  distinct-bot authorship/approval/merge asserted, cleanup + reset.
- Dogfood criterion: an agentforge feature issue flows 1→5 autonomously; reviewer-bot merges;
  release pipeline deploys it.

<!-- codex-review-status: complete -->