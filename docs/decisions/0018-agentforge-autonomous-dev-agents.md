# ADR 0018 — AgentForge: autonomous label-driven dev agents on Gitea + dev-workers

**Status:** PROPOSED (2026-07-10). Plan adversarially reviewed (codex, finalized); this ailab PR
carries only the infra/deploy companion — the application lands in the new repo
`cchifor/agentforge`. Full plan: `plans/2026-07-10-agentforge-plan.md`. **Relates to:** ADR 0017
(Gitea master forge — the state store and CI), ADR 0013 (self-hosted runners — CI capacity the
agents consume), 0012 (Authelia SSO), 0008/0015 (local LLM appliance — the qwen engines).

## Context

The lab has 6 idle-capable dev-worker VMs (dw1–dw6, 192.168.0.8–.13) with logged-in `claude` /
`codex` subscription CLIs, a self-hosted Gitea 1.26 forge (ADR 0017), CI runners, and local qwen
models behind litellm. We want a fully autonomous multi-agent development system: a human files an
issue, agents plan, write tests, implement, review, and merge — with the forge as the audit trail
and hard guardrails around spend, privilege, and runaway behavior. No new stateful infra
(queues/Redis/DBs) and no per-token cloud spend: engines are the existing Claude Max / Codex Pro
subscriptions plus the local models.

## Decision

Build **AgentForge** (repo `cchifor/agentforge`): one Python 3.12 service deployed identically to
all 6 workers as a host systemd unit (it drives host CLIs, OAuth homes, git, docker — not
containerized in prod), orchestrating a **label state machine** on Gitea issues.

1. **Labels are the state machine; Gitea is the only durable state store.** `state: 1-needs-plan`
   →Planner→ `2-needs-tests` (skippable) →Tester→ `3-ready-to-code` →Implementer→ `4-in-review`
   →Reviewer→ merge → `5-completed`, with a 4→3 bounce. Webhooks (6 org webhooks, one per worker,
   shared HMAC) are *hints*; a 60s reconcile poll is the *guarantee*; workers re-read the issue on
   dequeue and act on current state, never the payload. Agents never write to the forge — the
   orchestrator does all forge writes via 4 minimally-scoped bot accounts (planner/tester/impl/
   reviewer-bot).
2. **Epoch-bound claim lock over issue comments.** Labels/assignees are last-write-wins; issue
   comments are append-only with ordered ids — the only usable coordination primitive. A claim
   embeds `{state, base}` (base = latest transition-marker comment id) and is valid only while
   both still match; election is lowest-comment-id among valid claims; losers withdraw
   immediately; leases expire (TTL = run timeout × 1.5) and a reaper recovers crashes. **The
   transition marker IS the transition** (single atomic write, marker-first): state is derived
   from the latest `af:run` marker, the label is a human-visible mirror the reconciler heals.
   This protocol is the highest-risk novel mechanism and must pass a named `contract-claims`
   suite against real Gitea before any orchestrator work builds on it.
3. **Engines = subscriptions + local models only.** Max#1 (dw1/dw2) = Planner+Reviewer, Max#2
   (dw3/dw4) = Implementer, Codex Pro (dw5/dw6) = dedicated cross-reviewer, Tester =
   qwen3.6-35b-a3b via **litellm-local**. Local-only is **structural**: a separate `litellm-local`
   Deployment (own ConfigMap with only the two qwen models, own master key, no cloud-key env vars)
   is what the LAN NodePort (:30400) selects; the cloud-enabled litellm stays ClusterIP-only. The
   config validator additionally whitelists local models (defense in depth).
4. **Codex alignment gates.** Plan, implementation, and review stages each end with a
   Claude↔Codex critique loop (structured verdicts) under ONE shared budget: ≤3 rounds/stage,
   ≤9 codex rounds/issue, ≤3 review bounces, ≤8h wall-clock/issue — breach of any →
   `needs-human`. Merge requires Claude Reviewer AND independent Codex approval.
5. **Guardrails.** Per-run max_turns + timeout; per-issue total-runs cap (12); implementer
   iteration cap (4); `protected_paths` diff → `needs-human`; auto-merge only on config-allowlisted
   repos; `needs-human` = global stop; `FORGE_PAUSED` lives in the config JSON (webhook push +
   2-min poll — no restart) and is checked before every agent invocation and forge-write batch.
   Gitea-side: branch protection required_approvals=1, reviewer-bot on the approvals allowlist,
   impl-bot NOT on the merge allowlist.
6. **Deployment control plane = the config repo.** `cchifor/agentforge-config`/`agentforge.json`
   carries `release` + `release_sha256` pins, `min_agent_version` + `schema_version` (skewed or
   too-old workers mark degraded and claim nothing), topology, allowlists, budgets. Workers
   self-update every 2 min via an atomic updater (download → sha256 → `uv sync --frozen` →
   symlink flip → restart → version-matched health check → rollback on failure; releases are
   immutable Gitea generic packages). Last-good config is persisted so a bad remote config can
   never brick a restart.

## Threat model (explicit)

**v1 privilege reality: everything shares the `c4` UID.** The orchestrator, the agent CLIs, the
repo `test_cmd`, and the interactive human session run as one user. Credential scoping (bot PATs
only in the orchestrator process; git push auth injected per-invocation via `-c http.extraHeader`,
never on disk or in child env; scrubbed env for agent/test subprocesses; litellm key only in the
litellm-runner child env) is **hygiene, not a boundary**: under one UID, any compromised or
prompt-injected subprocess can read `/proc/<pid>/environ` of the orchestrator, plant file watchers,
or race command lines to recover every secret the process holds. Prompt-injected repo content
(README instructions, test fixtures, malicious diffs) is the realistic attack vector — the agent
runs tools against attacker-influenceable input.

**Therefore v1 is playground-only, ENFORCED IN CODE, not convention:** the config validator
refuses any repo allowlist beyond `cchifor/agentforge-playground` until the config carries an
explicit `privilege_hardening: "v1.1"` acknowledgment.

**v1.1 hardening gate (the condition for onboarding ANY repo beyond the playground):** a dedicated
non-sudo `agentforge` service user (splits the UID from the human session and its passwordless
sudo) **and** repo `test_cmd` executed in a docker container (workspace-mounted, no credentials,
no network by default). Only after both land — and the config ack is set — may real repos (the
dogfood target `cchifor/agentforge` itself first) enter the allowlist. Residual risks accepted at
v1.1: the orchestrator itself remains trusted code with PATs in memory; the LAN is single-operator
(NodePort/webhook exposure per the prometheus-lan trade-off); bot PATs are minimally scoped and
revocable centrally.

## Rejected / out of scope

- **Queue/Redis/DB coordination infra** — Gitea comments + reconcile give at-least-once semantics
  with zero new stateful services; disposable local state (in-memory queue, LRU, backoff) is
  acceptable loss on crash.
- **API-key cloud engines via litellm** — per-token spend with autonomous agents is an unbounded
  budget risk; subscriptions are already paid for and rate-window-bounded.
- **Kubernetes-hosted agents** — the agents drive host CLIs with OAuth homes, docker, and
  `/workspace`; containerizing the *orchestrator* would re-open every credential/tooling seam the
  host unit gets for free. (Containerizing `test_cmd` is, conversely, required — see v1.1.)
- **Agent-side forge writes** — one writer (the orchestrator, per-bot clients) keeps authorship,
  auditing, and revocation coherent.

## Consequences

- A human issue can flow 1→5 with no human touch on allowlisted repos; every action is an
  auditable `af:run`/`af:xrev` comment by a distinct bot identity.
- The dev-workers gain a permanent system service + 2-min timer; interactive use coexists under
  the agentforge memory cap (MemoryHigh 6G / MemoryMax 8G per worker).
- Gitea gains org webhooks to the LAN (webhook.ALLOWED_HOST_LIST), 4 bot users, org labels, and
  branch protection on managed repos; forge load grows with agent chatter (bounded by caps).
- The cluster gains a second LiteLLM deployment (litellm-local) + LAN NodePort :30400 — exposure
  trade-off documented in `kubernetes/apps/apps/ai/litellm-local.yaml` (no cloud creds by
  construction).
- Subscription rate windows throttle throughput (backoff + `af:run` failure entries; 2 consecutive
  auth failures → `needs-human`); OAuth refresh expiry requires occasional human re-login
  (runbook).
- New operational surface: release pins, config repo, claim hygiene — see
  `docs/runbooks/agentforge.md`; alerts in `monitoring/agentforge-rules.yaml`.

## Rollout (phases 0–6, gated)

0. **Foundations:** merge this PR (webhook allowlist, litellm-local, role, monitoring) →
   subscription logins (Max#1 dw1/2, Max#2 dw3/4, Codex dw5/6; `claude setup-token`) → reboot
   each worker and prove the auth canary passes unattended → `bootstrap_gitea.py` (repos, bots,
   labels, webhooks + HMAC/negative smokes, branch protection, package-registry smoke) → SOPS
   secrets → CI green → tag v0.1.0 → pin → `dev_worker_enable_agentforge: true` → `just
   dev-workers` (×2, plus `--check` and `systemd-analyze verify` first).
1. **Shadow pilot:** dw1 (Claude roles) + dw5 (Codex cross-reviewer) on the playground, merge
   disabled → pilot issue → inspect → enable merge → full lifecycle smoke.
2. **Chaos:** kill mid-task (lease recovery), manual label flips, low caps, update-path drill
   (vA/vB pin bump, one deliberately failed update → automatic rollback, pin revert).
3. **Fleet:** role-per-worker on all 6, two implementers (live claim contention), ~10-issue 48h
   soak.
4. **v1.1 privilege hardening lands FIRST** (dedicated user + containerized test_cmd) — the gate
   for any repo beyond the playground; only now is `privilege_hardening: "v1.1"` acked.
5. **Dogfood:** agentforge PR#1 merged by hand (codex+human review; no circular trust), then
   `cchifor/agentforge` enters the allowlist and PR#2+ flow through the deployed system.
6. **Onboard further repos one at a time**; weekly smoke timer. Rollback anywhere: set
   `FORGE_PAUSED` first (≤2 min propagation; workers finish/release claims), then
   `systemctl stop agentforge` if needed — SIGTERM releases claims, lease expiry covers crashes.
