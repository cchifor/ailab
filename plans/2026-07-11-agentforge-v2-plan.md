# AgentForge v2 — Self-Service Control Plane (multi-tenant, cloud-native, sandboxed)

## Context

AgentForge v1 (built + merged, ADR 0018) runs as host systemd services on 6 Proxmox dev-worker VMs,
driven by a hand-edited JSON config in a Gitea repo, SOPS-baked secrets. It works but setup is
manual/IaC: no self-service, no real isolation of agent-generated code, no dynamic sizing, no UI
beyond a monitoring dashboard.

**v2 turns it into a self-service platform**: a web app where a user logs in, connects
**Infrastructure** (a k8s cluster; the platform analyzes resources and provisions the right number of
sandboxed workers + CI runners), creates **Workspaces** (repos + access keys), and gets a
**Monitoring** dashboard over the fleet. Access keys live in a real secrets vault every worker/runner
can reach.

Settled scope (user decisions): **multi-user + arbitrary external clusters** (true SaaS shape),
**dedicated Kata-capable Talos node pool** for compute, **OpenBao + External Secrets Operator** for
the keystore, **GitOps provisioning** (web app → Gitea commit → Flux + a kro operator reconcile). The
ailab cluster is "tenant-zero"; multi-tenant from day 1. **v1 is reused, not rewritten** — the
orchestrator, role handlers, codex alignment gates, epoch-locked claim lock, GiteaClient, Ledger are
unchanged; v2 adds a control plane, containerizes + sandboxes the worker, and swaps two clean seams
(config source, exec).

## Governing principles

- **Web app writes desired state; a reconciler converges it.** No standing broad mutation rights on
  the web app — it commits CRs/manifests to git; Flux (hub) / a kro RGD / a spoke agent (external
  clusters) apply. Auditable, drift-correcting, matches the existing Flux+Gitea.
- **Security boundary = credential-split, not Kata alone.** Untrusted agent/test code runs in a
  container that mounts ZERO high-value secrets, in a separate PID/mount namespace from the
  orchestrator, inside a Kata microVM, behind default-deny egress. This *is* ADR 0018's v1.1 gate.
- **Forge/DB are the source of truth; workers stay stateless.** v1's claim lock already dedups across
  pods, so autoscaling needs no new coordination.

## Approach

### Two repos

- `cchifor/agentforge` (v1, extended): orchestrator/roles/gates unchanged **+** new
  `ControlPlaneConfigSource`, `Executor` port, `deploy/Dockerfile`, `dispatcher` subcommand. Runs as
  the worker image on the agent node pool / spokes.
- `cchifor/agentforge-platform` (NEW): control-plane API (FastAPI + Authelia OIDC), webapp SPA (3
  sections), kro RGDs + `ConnectedCluster` reconciler, resource analyzer, OpenBao/git/kube adapters.
  Runs on the hub cluster, ns `agentforge`. Depends on v1 for the config schema only
  (`AgentForgeConfig` + `compatibility()`), so hub↔worker skew stays gated.

### Control plane

- **Auth**: FastAPI OIDC RP against Authelia (`sso.chifor.me`, auth-code + PKCE/S256); add a client
  block to `kubernetes/apps/apps/auth/authelia-config.yaml` (PBKDF2 hash in the ConfigMap, plaintext
  SOPS secret in the app ns); `groups` claim → org RBAC. Exposed at `agentforge.chifor.me` via
  cloudflared + CF Access (`allow_me`; per-org policies in P3).
- **DB**: new `agentforge_platform` DB+role on CNPG `infra-pg` (`infra-pg-rw.databases.svc:5432`),
  DSN in a SOPS secret (precedent `open-webui-db.sops.yaml`); bump the 5Gi PVC. Multi-tenant with
  **Postgres RLS** (`SET LOCAL app.current_org` per request): orgs, users, memberships(role),
  connected_clusters, workspaces, agent_worker_pools, secret_refs (OpenBao pointers — never values),
  workspace_config_versions, cluster_node_snapshots, audit_log. Isolation = RLS (data) × k8s
  namespace per workspace × OpenBao namespace per org.
- **k8s access** = Headlamp's shape: dedicated ns+SA, `headlamp-readonly` ClusterRole (analyzer +
  status) + a SEPARATE scoped-write role (patch Flux CRs to nudge reconcile) — never
  create/update/delete on workloads; NetworkPolicy admits only cloudflared.
- **3-section webapp** (vue-router over v1 dashboard components): Infrastructure (connect cluster;
  analyzer view; Provision → git), Workspaces (add repo; access key → OpenBao; per-workspace config;
  Gitea bootstrap via v1 `bootstrap.py`), Monitoring (v1 kanban+feed+worker-strip, fleet/per-workspace
  + utilization; fed by workers pushing to `POST /api/ingest/events`, CP keeps the board read model +
  rebroadcasts SSE).
- **Provisioning**: CP renders `Workspace`/`AgentWorkerPool` CRs + manifests, commits via GiteaClient
  to `kubernetes/apps/apps/agentforge/tenants/<org>/<workspace>/`; hub Flux reconciles; a **kro RGD**
  expands each into ns/SA/ESO SecretStore+ExternalSecret/KEDA ScaledObject/worker Deployment/RBAC/
  NetworkPolicy. `ConnectedCluster` (cross-cluster) reconciled by the CP itself.
- **External clusters (hub-spoke, pull-based)**: each spoke runs its own Flux (or the "our agent"
  bundle), pulls only its tenant's desired state from a per-tenant git target; spoke→hub limited to
  worker-config fetch, event ingest, node-snapshot push (per-workspace-bearer scoped). Hub's only
  outward privilege = a scoped git push target, never a spoke kubeconfig.
- **Resource analyzer** = CP async read-only job: node allocatable (metrics-server + kube-state-metrics;
  spokes push snapshots), 60–70% p95 headroom, +~128 MiB/Kata-pod, `recommended_workers` → advisory in
  DB; Provision → git commit sets `AgentWorkerPool.maxReplicas` → kro → `ScaledObject.maxReplicaCount`.

### Worker + sandbox

- **Config seam**: `adapters/config/control_plane.py` (`ControlPlaneConfigSource`, ~100 lines, mirrors
  `gitea_repo.py`). `AF_CONFIG_SOURCE=control_plane`; fetches
  `GET /api/v1/workspaces/{ws}/pools/{pool}/config` with a per-workspace bearer (OpenBao-minted,
  ESO-synced). Reuses v1 poll loop; a CP "config changed" hint triggers immediate refresh.
- **Image** (`deploy/Dockerfile`, multi-stage): dashboard build → uv venv → runtime (python3.12 + git
  + Node + `@anthropic-ai/claude-code` + `@openai/codex` + docker CLI + tini), non-root uid 1000,
  volumes state_dir + jobs_root. Built+pushed to `registry.chifor.me` (anonymous-pull, no
  imagePullSecret) by a new `image` job in `release.yml`; pin references `worker@sha256:…`.
- **Three-tier privilege split per pod** (the airtight boundary): orchestrator container holds bot
  PATs/HMAC/git-push/OpenBao-SA; agent container (claude/codex CLI) holds inference OAuth only, no PAT
  mount; test_cmd/setup_cmd run in a DinD container with `--network none` and NO cred mount.
- **Credential injection (OpenBao+ESO)**: `af-forge-creds` → orchestrator only; `af-claude-oauth`
  (`CLAUDE_CODE_OAUTH_TOKEN`, ~1yr, no auto-refresh → rotate on the yearly auth-canary alert; P3
  apiKeyHelper-via-broker) → agent env only; `af-codex-auth` (`~/.codex/auth.json`, auto-refreshes →
  writable emptyDir seeded by init) → agent only; `af-runner-token` → CI runners only. Wire the
  runners' `home=` seam from `AF_CLAUDE_HOME`/`AF_CODEX_HOME`; add `CLAUDE_CODE_OAUTH_TOKEN` to
  `scrubbed_env` passthrough (agent children only, never test_cmd).
- **Executor port** (`ports/executor.py`): both untrusted call sites route through it —
  `Workspace.run_cmd` and the runner CLI subprocesses. `LocalExecutor` = today's subprocess
  (dev/tests); `SandboxExecutor` = `docker run` into a per-pod DinD sidecar (safe in the Kata
  microVM), `--network none` default, `--cap-drop ALL`, `--read-only`, pids/mem limits,
  kill-on-timeout. The shared jobs volume must mount at an identical path in orchestrator + agent +
  DinD (the `-v` resolves in the DinD daemon's FS — the load-bearing pod-spec detail). Opt-in
  `repo.sandbox: {kind|vcluster}` for k8s-sandbox tests.
- **Egress (Cilium default-deny + allowlist)**: per-Deployment CiliumNetworkPolicy (Claude→anthropic,
  Codex→openai, tester→litellm-local, orchestrator→forge/OpenBao/litellm); per-exec test_cmd→
  `--network none` (opt-in pull-through package proxy).
- **Deployment/role model**: one image, roles via config, one Deployment per OAuth account
  (`af-claude-max1` planner/reviewer, `af-claude-max2` implementer +DinD, `af-codex` cross-reviewer,
  `af-tester` litellm +DinD, `af-dashboard` trusted/runc). Only implementer+tester get DinD. Distinct
  `AF_WORKER_NAME` per pod via downward API.
- **Autoscaling (KEDA scale-to-zero)**: `maxReplicaCount = accounts[X].max_parallel` — the KEDA cap IS
  v1's per-account semaphore (claim lock dedups; no new code). Signal from an always-on read-only
  `af-dispatcher` (scale-to-zero can't self-report): polls Gitea, exports `forge_pending{role,repo}`,
  Prometheus scaler. Cron warm-floor for interactive roles; pre-pull DaemonSet for cold start.
- **CI runners (k8s-native)**: KEDA `ScaledJob` — one ephemeral Kata runner + privileged DinD per
  queued job (token from OpenBao/ESO, label `self-hosted-hv` with `docker://` schema). Same sandbox
  mechanism as workers; deletes v1's workspace-reclaim machinery.

### Infrastructure (ailab)

- **Dedicated Talos agent node pool** (new tofu `kubernetes/infra/agent-nodes/`, mirrors
  `dev-workers/main.tf`): Proxmox VMs, `cpu type=host` (nested virt), joined as workers
  (`machine_type=worker`, new `worker.yaml.tftpl`) with Kata + gVisor Talos system extensions, kernel
  modules `vhost_net`/`vhost_vsock`, label `ailab.io/agent-pool` + taint `dedicated=agent`. Nested virt
  must be enabled on the Proxmox hosts (`kvm_amd nested=1`) or Kata `/dev/kvm` is absent → gVisor
  fallback for compute-only roles.
- **RuntimeClasses** `kata` (QEMU) + `gvisor` (runsc), agent-pool nodeSelector/toleration.
- **New operators (Flux)**: OpenBao, External Secrets Operator, KEDA, kro under
  `kubernetes/apps/infrastructure/{security,autoscaling}/`.

## Phasing

- **P1 — tenant-zero, hub only, full vertical slice**: agentforge-platform repo (CP API + Authelia
  OIDC + Postgres schema + 3-section webapp); connect one cluster = the hub (Headlamp-style SA);
  Workspaces CRUD → CRs → kro materializes (secrets still SOPS, no OpenBao yet); worker image +
  ControlPlaneConfigSource; the dedicated agent node pool (plain, no Kata yet); monitoring
  fleet-aggregation; expose `agentforge.chifor.me`. One shadow Deployment (planner, merge disabled,
  playground) proving auth-canary + config fetch + a 1→2 transition.
- **P2 — secrets + scaling + sandbox (the unlock)**: OpenBao + ESO + KEDA + kro installed; migrate
  secrets off SOPS into OpenBao (namespaced SecretStore per workspace); Kata node-pool extensions +
  RuntimeClasses; full per-account Deployment set + DinD + SandboxExecutor + Cilium egress + KEDA
  scale-to-zero + af-dispatcher + k8s-native CI runners. All sandbox-boundary tests green → flip
  `privilege_hardening: v1.1` → unlock non-playground repos.
- **P3 — external clusters + multi-user + fleet**: spoke "install our agent" onboarding (pull model);
  Authelia groups → org RBAC + Postgres RLS enforced; per-org quotas; per-tenant LiteLLM virtual keys
  w/ budget caps; dogfood (`cchifor/agentforge` PRs flow through the deployed system); optional Claude
  apiKeyHelper-via-broker hardening.

## Critical files

- agentforge (v1 seams): `src/agentforge/main.py`, `app/workspace.py`,
  `adapters/runners/{_envelope,claude_code,codex}.py`, `infra/{settings,metrics}.py`, NEW
  `adapters/config/control_plane.py`, NEW `deploy/Dockerfile`, `.github/workflows/release.yml`.
- agentforge-platform (new): `src/agentforge_platform/{api,domain,adapters,operator}/**`, `webapp/**`,
  `crds/**`, `deploy/**`.
- ailab: `kubernetes/infra/agent-nodes/**` + `machine-config/worker.yaml.tftpl`,
  `kubernetes/apps/apps/agentforge/**`, `kubernetes/apps/infrastructure/{security,autoscaling}/**`,
  `kubernetes/apps/apps/auth/authelia-config.yaml`, `kubernetes/apps/databases/**`, cloudflared +
  `cloudflare/{access,dns}.tf`, `docs/decisions/0019-agentforge-v2-control-plane.md`.

## Verification

- **Sandbox boundary (the #1 gate)**: a canary malicious repo + prompt-injected issue whose
  test_cmd/agent try to read bot PATs, inference OAuth, OpenBao, orchestrator `/proc/1/environ`, and
  egress to the internet — assert every attempt fails (no cred mounts, separate ns, `--network none`);
  assert `uname -r` shows the Kata guest kernel; assert the egress allowlist matrix; assert privileged
  DinD can't escape the microVM.
- **KEDA 0→N→0**: seed K issues → scale to `min(K, maxReplicaCount)`, process (claims don't collide —
  reuse `contract-claims`), return to 0 after cooldown; assert the cap holds.
- **Credential injection**: pod auth-canary passes non-interactively from ESO creds; rotate OpenBao →
  ESO re-sync → pod roll → canary passes; Codex refresh persists then re-seeds.
- **Platform**: OIDC login; create workspace → git commit → kro materializes; analyzer recommends vs
  live allocatable; monitoring aggregates the fleet; RLS blocks cross-org reads (P3).
- **k8s/GitOps**: `kubectl --context admin@ai` sees tenant namespaces, RuntimeClasses, ScaledObjects,
  synced ExternalSecrets; Flux reconciles the committed tenant tree; cloudflared serves the app.

## Risks

1. **Multi-tenant secret isolation (highest)** — enforce in the kro RGD: always a namespaced
   SecretStore (never Cluster), OpenBao k8s-auth role pinned to exact namespace+SA, policy scoped to
   `af/<org>/<workspace>/*`, NetworkPolicy so workers reach only ESO/OpenBao/CP.
2. **External-cluster trust boundary** — pull-based only; hub's sole outward privilege = scoped git
   push; config endpoint per-workspace-bearer scoped; every spoke→hub call is untrusted input.
3. **Subscription-OAuth sharing across tenants** — `claude_max` is tenant-zero only; external tenants
   get per-tenant LiteLLM virtual keys with per-org budget caps; allowed engines gated by org plan.
4. **Cold-start latency** — fat image + Kata boot + auth canary; pre-pull DaemonSet + cron warm-floor;
   true zero overnight.
5. **Scope** — multi-month platform; P1 near-term, P2 unlocks safe non-playground use, P3 full
   SaaS/fleet. Execute per phase.

<!-- codex-review-status: pending -->
