# Runbook — AgentForge v2 activation (100% IaC, no manual/physical ops)

Activates the dormant v2 stack (built on `feat/p2-unlock`) via `just` + `tofu` + Flux + GitOps merges.
There are NO ad-hoc console commands — every step is codified. Design + rationale: `plans/2026-07-13-
iac-activation-plan.md` (codex-reviewed). Cluster: `kubectl --context admin@ai`. Hosts: `scripts/node-ssh.py`.

## Preconditions (verified 2026-07-13)
- Nested virt live on all hosts (`just nested-virt-verify` → PASS) — Kata has NO reboot gate.
- cert-manager `ailab-ca` ClusterIssuer Ready → OpenBao internal TLS.
- **etcd Secret encryption at rest is NOT configured** (accepted homelab residual): the OpenBao unseal key
  in Secret `openbao-keys` is recoverable from a raw etcd/disk read. Acceptable for tenant-zero; enabling
  Talos disk encryption (CP rolling reboots) is a separate hardening.

## Staged sequence (⛔ = irreversible; stop + confirm before each)

### Stage 0 — bootstrap images
CI (`.gitea/workflows/images.yml` in agentforge + agentforge-platform) builds+pushes to registry.chifor.me.
Then pin the two bootstrap-class digests (own commit, un-gates nothing):
```
just pin-bootstrap sha256:<orchestrator> sha256:<agentforge-platform>
```

### ⛔ Stage 1 — operators/security merge (triggers the live OpenBao init)
Merge the openbao/eso/keda/kro/security subset of `feat/p2-unlock` to `main`. Flux deploys OpenBao (TLS via
`openbao-tls`), then the Jobs run automatically:
- `openbao-init` → initialize + unseal, writing `openbao-keys` (unseal key + cluster_id) and
  `openbao-bootstrap-token` (root). Idempotent + fail-closed (never re-inits over a live vault; cluster_id
  disagreement or partial-death → hard fail).
- `openbao-unsealer` Deployment → re-unseals on any restart (reads `openbao-keys` only).
- `openbao-provision` → `af` KV mount + `kubernetes` auth backend (server-SA TokenReview, no static JWT) +
  base policies + canary role/seed + scoped `openbao-provisioner-token`; writes the `openbao-state`
  sentinel; **revokes root + deletes `openbao-bootstrap-token`**.
- `agentforge-provisioner` controller → per-tenant policies/roles.
Verify (`just openbao-status`): init+provision Jobs Succeeded; `openbao-state.provisioned=true`;
`openbao-bootstrap-token` gone; the **`openbao-canary` ExternalSecret Ready=True** (end-to-end k8s-auth proof).
`flux diff` before merging.

### ⛔ Stage 2 — Kata agent-node pool
```
just nested-virt-verify        # gate (already passes)
just agent-nodes-apply         # tofu creates .47–.49 on the Kata image (depends on the gate)
```
Verify: nodes Ready; `kubectl get runtimeclass kata gvisor`; a probe pod on the pool sees `/dev/kvm`.
(Host RAM headroom: .2/.3 ~24–26G free, agent-node = 16G — reduce `agent_node_memory_mib` if a host is tight.)

### ⛔ Stage 3 — agentforge layer merge
Merge the agentforge-broker/sandbox/workers/ci-runners/runtimeclasses/tenants subset to `main`. Workloads
stay gated (unlisted manifests + paused ScaledJob + placeholder digests). **KNOWN pre-Stage-3 TODO:** the 4
operator SecretStores (broker/ci/reaper/dispatcher) carry a `caProvider` referencing `openbao-tls` in their
OWN namespaces — the ailab-ca CA must be distributed there (per-ns cert-manager Certificate or trust-manager)
before they go Ready. The Stage-1 canary is unaffected (same ns as the cert).

### ⛔ Stage 4 — un-gate workloads
`just pin-workloads <img>=sha256:… …` (separate commit) then a commit re-listing the gated manifests — ONLY
after ExternalSecrets Ready, KEDA targets present, ledger schema/grants applied. tenant-zero worker scales
0→N on `forge_pending`.

### ⛔ Stage 5 — boundary tests → v1.1
ADR-0018 canary (no cred mounts, `--network none`, Kata guest kernel, egress matrix) all green → flip
`privilege_hardening: v1.1`. **Rollback on canary failure:** pause ScaledObjects/ScaledJobs, re-comment
Deployments, confirm no sandbox Jobs remain, do NOT flip.

## Activation outcome — tenant-zero playground (as of 2026-07-21)
Live-driven on the seeded playground repo **`cchifor/agentforge-playground`** (uv/pytest scaffold on `main`),
pushing issues through the state machine. Accounts (all `max_parallel: 1`): `claude-max-1` = planner/reviewer,
`claude-max-2` = implementer, `codex-pro` = cross-reviewer, `qwen-local` = tester.

- **Planner slice PROVEN end-to-end:** Kata sandbox agent → **pod-IP** (a Kata microVM is off Cilium's
  socket-LB path, so the orchestrator hands it a live broker **pod IP**, not the ClusterIP) → per-account
  broker (`broker-anthropic-max1`, ns `agentforge-broker`) → capability-verify → OAuth-inject → **real
  Anthropic `200`** → valid plan JSON `{plan_md, tests_needed}` → tree import → issue state advances.
- **Codex cross-review gate PROVEN live:** the plan stage is a `cross_review` gate stage; the planner's plan is
  handed to the **codex** cross-reviewer, whose broker call (`broker-openai-codex`, `/v1/responses`) returns
  **`decision:granted status:200`** — capability + kid-policy + gateway model checks all PASS and the request
  forwards. Real critiques post to the issue, e.g. `🔀 cross-review [plan] round 1: N blocking concern(s)`.
- **Net:** the credential-broker path returns **`granted 200` for BOTH** `anthropic` (planner) and `codex`
  (cross-reviewer) on tenant-zero. The core v2 thesis — a credential-injecting broker for both providers, in
  front of the Kata sandbox, with the alignment gate — is demonstrated live. Per-account scoping holds: the
  agent only ever holds ITS `aud`'s broker pod IP, and each broker rejects a capability minted for another
  account (`agentforge-sandbox/cilium-egress.yaml`, `broker-openai-codex.yaml` ingress CNP).

## Activation blocker ladder (resolved, in order) — symptom → root cause → fix
The slice cleared a chain of blockers, each fixed before the next surfaced. Repos: **agentforge** = the
orchestrator/worker image code (fix ships via CI rebuild of `p1-worker` → repin the digests in this repo →
Flux roll); **ailab** = this repo (manifests); **agentforge-config** = the live-polled `agentforge.json`
(orchestrator polls `config_poll_s=120` → NO image rebuild, effective ~2 min after merge).

1. **Planner escalates `no structured output`.** The role expects `result.output = {plan_md, tests_needed}`
   but `run_agent` built the AgentJob with NO `output_schema` and the prompt never asked for JSON, so the
   agent emitted prose → `extract_last_json_object` found nothing → `output=None`. Fix: per-role output
   schemas threaded through `run_agent` + prompt instructs the JSON envelope — **agentforge #42**.
2. **Gitea HTTP 500 `database is locked`.** Gitea runs `DB_TYPE=sqlite3` (single-writer) on an ext4 block PVC;
   under AgentForge polling load the default 500 ms busy-timeout + rollback-journal starves writers →
   `CreateIssueComment`/`EditIssueComment` 500 → orchestrator reconcile crashes → issues never advance. Fix:
   SQLite **WAL journal + 10 s busy-timeout** (`GITEA__database__SQLITE_JOURNAL_MODE=WAL`,
   `GITEA__database__SQLITE_TIMEOUT=10000`; safe on a block PVC) — **ailab #67**. (Durable follow-up: CNPG
   Postgres migration.)
3. **Plan gate fails `agent sandbox profile requires a COMPLETE typed capability contract; missing/empty:
   model, budget`.** `CrossReviewConfig` defaults `model=""` / `token_budget=0` and they were unset in config,
   so the broker's `_reject_incomplete_agent_contract` rejected the cross-reviewer AgentJob. Fix (config-only,
   live-polled): set `model` + `token_budget` on `cross_review` and on the roles that were also empty
   (implementer/tester/cross-reviewer) in **agentforge-config**.
4. **Codex critic gets broker `404 Not Found` on `/responses`.** The codex CLI (`wire_api="responses"`) POSTs
   to `{base_url}/responses` and does NOT prepend `/v1`, but the broker serves the route at inbound
   `/v1/responses` (claude works because it appends the full `/v1/messages` itself). Fix: `broker_launch.py`
   `_codex_config_toml` sets `base_url = {broker_url}/v1` — **agentforge #43**.
5. **Codex `403 model-not-allowed`.** The broker enforces request `model ∈ capability.model_set` (built from
   `cross_review.model`); with config `gpt-5.5` the codex CLI still sent its own default → mismatch → 403. Fix
   (config): set the capability `cross_review.model` = `gpt-5.6-sol` in **agentforge-config**.
6. **Codex `403 forbidden — capability policy rejected: model(s) outside kid policy: ['gpt-5.6-sol']`.** A
   SECOND allowlist — the broker's OPERATOR kid-policy (`broker-openai-codex-kids`, `registry.json`,
   `.kids.<kid>.allowed_models`, synced from OpenBao) — permits `{gpt-5.3-codex, gpt-5.5, gpt-5.6}` but NOT
   `gpt-5.6-sol`. Fix (code + config, chosen over editing the operator kid-policy): make codex send the
   already-allowed `gpt-5.6` via `-c model=<job.model>` — **agentforge #44** — plus config `cross_review.model`
   = `gpt-5.6` in **agentforge-config**.
7. **Codex upstream `401 — "Provided authentication token is expired. Please try signing in again."`** The
   broker grants + forwards (model PASSED), but chatgpt.com rejects the stored OAuth: the broker uses a STATIC
   `access_token` with no self-refresh, the ChatGPT token is a ~10-day JWT, and the auto-refresher CronJob is
   failing (see the open step below). Fix: reload a fresh codex OAuth token into OpenBao — **the current open
   step**.

## Current open step — reload the codex OAuth token (STOPGAP) + create the refresher role (durable)
Blocker #7 above is the sole remaining item; everything upstream/downstream is proven. The fresh token is
~10-day, so there is no urgency, but the broker upstream stays `401` until it is reloaded.

**Refresh** a fresh auth.json from this box's live codex creds (codex auto-refreshes `~/.codex/auth.json`
locally):
```
cd ~/work/home/agentforge && \
  uv run python -m agentforge.broker.codex_refresh --in ~/.codex/auth.json --out <fresh> --force
# → a fresh auth.json, exp ≈ now + 864000 s (~10 days)
```

**Write** it to OpenBao at `af/operator/broker/openai/codex-pro/oauth` (KV **v2**, mount `af`, field
`auth.json` — this is exactly the `broker-openai-codex-oauth` ExternalSecret's `remoteRef` key/property).
Pass the token + payload via **stdin, never argv**:
```
bao kv patch af/operator/broker/openai/codex-pro/oauth auth.json=@<fresh>   # KV v2 CLI abbreviates /data/
```
**Authorize the write with an OPERATOR-scoped token, NOT root:** OpenBao **2.5.5 has DISABLED
`generate-root`** (`sys/generate-root/*` returns `405 unsupported operation`; POST → 403), so there is NO
root-token recovery from the `openbao-keys` unseal key here — the prior "generate-root viable on 2.5.5" note
is WRONG. Use the running `agentforge-provisioner` (ns `openbao`, SA `agentforge-provisioner`, k8s-auth to
`https://openbao-0.openbao-internal.openbao.svc:8200`), which CAN write operator paths, or any operator-scoped
token you hold. (Do NOT patch the k8s Secret `broker-openai-codex-oauth` directly — ESO `creationPolicy=Owner`
drift-corrects it back within seconds; OpenBao is the source of truth.)

**Propagation:** ESO re-syncs `broker-openai-codex-oauth` from OpenBao and the broker reloads the mounted
credential on change (~5 min) → codex upstream returns `200`.

**Durable fix — create the `af-codex-refresher` OpenBao role.** The nightly `af-codex-refresh` CronJob (ns
`agentforge-broker`; `kubernetes/apps/infrastructure/agentforge-codex-refresh/cronjob.yaml`) k8s-auth-logs-in
under role `af-codex-refresher` and CAS-writes the same `af/…/oauth` field — but it currently fails
`codex-refresh FAILED: HTTP 400` because that OpenBao role is MISSING (bootstrap-sentinel gap). Creating the
`af-codex-refresher` operator role makes the token self-refresh and retires this manual reload. That role
addition rides the OpenBao re-bootstrap / provisioner-fix path (an operator-scoped provisioning capability,
since `generate-root` is unavailable) — see the designed provisioner IaC fix.

## Disaster notes
- `openbao-init` refuses to re-init if `openbao-keys` exists but the vault is uninitialized (stale key / lost
  PV) — it fails loudly rather than minting a NEW vault that orphans every stored secret.
- Losing `openbao-keys` after init = unrecoverable seal → restore the vault PV from backup or re-key.
- The unseal key + (pre-revocation) root live only in the `openbao` namespace; the unsealer reads the unseal
  key only; the provisioner uses a scoped token, never root.
