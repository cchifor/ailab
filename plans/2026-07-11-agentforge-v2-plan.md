# AgentForge v2 — Self-Service Control Plane (multi-tenant, cloud-native, sandboxed)

## Codex Review

- The v2 direction is plausible, but the current plan overclaims the pod-level security boundary: same-pod network identity, service account token handling, Docker daemon access, and shared writable volumes must be closed before calling it airtight or ADR 0018-compliant.
- KEDA `maxReplicaCount` is useful capacity control, not a strict subscription semaphore by itself; rollout surge, terminating pods, internal concurrency, and multi-role accounting still need explicit controls.
- P1 is not a coherent shippable slice as written because it depends on `kro` and credential-minting pieces scheduled for P2, and "multi-tenant from day 1" conflicts with org/RLS enforcement deferred to P3.
- The hub-spoke and GitOps models reduce direct kubeconfig exposure, but the hub's GitOps write path, Flux apply identity, RBAC precedent, and untrusted spoke input need admission controls, payload validation, and rate limits.
- The two seam claims are only clean if agent CLI execution and repo test execution remain separate trust classes; forcing OAuth-bearing runner subprocesses through the same executor path risks breaking auth or leaking inference credentials.

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
<!-- codex: "Multi-tenant from day 1" contradicts the phasing section, where org RBAC, RLS enforcement, quotas, and external tenants are deferred to P3. Either enforce tenant isolation in P1 or scope P1 explicitly to tenant-zero only. -->

## Governing principles

- **Web app writes desired state; a reconciler converges it.** No standing broad mutation rights on
  the web app — it commits CRs/manifests to git; Flux (hub) / a kro RGD / a spoke agent (external
  clusters) apply. Auditable, drift-correcting, matches the existing Flux+Gitea.
<!-- codex: A GitOps write token is still an indirect cluster-mutation capability if Flux applies from that path with broad permissions. The plan needs admission policy, path scoping, and Flux service-account scoping so a compromised CP cannot commit arbitrary cluster-admin manifests. -->
- **Security boundary = credential-split, not Kata alone.** Untrusted agent/test code runs in a
  container that mounts ZERO high-value secrets, in a separate PID/mount namespace from the
  orchestrator, inside a Kata microVM, behind default-deny egress. This *is* ADR 0018's v1.1 gate.
<!-- codex: Same-pod containers share the pod network namespace and usually the same pod service account identity, so NetworkPolicy and localhost controls are not per-container boundaries. This does not yet satisfy ADR 0018's v1.1 gate unless the pod spec proves no shared process namespace, no default SA token, no agent access to the Docker API, and no shared writable path that becomes a confused-deputy channel. -->
- **Forge/DB are the source of truth; workers stay stateless.** v1's claim lock already dedups across
  pods, so autoscaling needs no new coordination.
<!-- codex: Claim deduplication prevents duplicate work on one issue, but it does not enforce subscription concurrency. The v1 semaphore came from fixed topology plus per-worker concurrency; v2 still needs explicit per-account admission or hard pod/concurrency controls. -->

## Approach

### Two repos

- `cchifor/agentforge` (v1, extended): orchestrator/roles/gates unchanged **+** new
  `ControlPlaneConfigSource`, `Executor` port, `deploy/Dockerfile`, `dispatcher` subcommand. Runs as
  the worker image on the agent node pool / spokes.
- `cchifor/agentforge-platform` (NEW): control-plane API (FastAPI + Authelia OIDC), webapp SPA (3
  sections), kro RGDs + `ConnectedCluster` reconciler, resource analyzer, OpenBao/git/kube adapters.
  Runs on the hub cluster, ns `agentforge`. Depends on v1 for the config schema only
  (`AgentForgeConfig` + `compatibility()`), so hub↔worker skew stays gated.
<!-- codex: Depending on "config schema only" is optimistic: CP-generated configs now carry tenant, secret, pool, engine-plan, and executor policy semantics that workers must enforce. Compatibility needs contract tests across CP and worker versions, not just schema parsing. -->

### Control plane

- **Auth**: FastAPI OIDC RP against Authelia (`sso.chifor.me`, auth-code + PKCE/S256); add a client
  block to `kubernetes/apps/apps/auth/authelia-config.yaml` (PBKDF2 hash in the ConfigMap, plaintext
  SOPS secret in the app ns); `groups` claim → org RBAC. Exposed at `agentforge.chifor.me` via
  cloudflared + CF Access (`allow_me`; per-org policies in P3).
<!-- codex: `allow_me` and P3 per-org policies mean the external multi-user access boundary is not present in P1/P2. FastAPI must still enforce org membership on every API route because CF Access is only an outer gate. -->
- **DB**: new `agentforge_platform` DB+role on CNPG `infra-pg` (`infra-pg-rw.databases.svc:5432`),
  DSN in a SOPS secret (precedent `open-webui-db.sops.yaml`); bump the 5Gi PVC. Multi-tenant with
  **Postgres RLS** (`SET LOCAL app.current_org` per request): orgs, users, memberships(role),
  connected_clusters, workspaces, agent_worker_pools, secret_refs (OpenBao pointers — never values),
  workspace_config_versions, cluster_node_snapshots, audit_log. Isolation = RLS (data) × k8s
  namespace per workspace × OpenBao namespace per org.
<!-- codex: The existing CNPG manifest notes postInitSQL only ran at first bootstrap, so adding this DB/role needs an explicit migration/manual SQL job path. RLS also needs tests for missing `SET LOCAL`, background jobs, connection-pool reuse, and service/admin bypass paths. -->
- **k8s access** = Headlamp's shape: dedicated ns+SA, `headlamp-readonly` ClusterRole (analyzer +
  status) + a SEPARATE scoped-write role (patch Flux CRs to nudge reconcile) — never
  create/update/delete on workloads; NetworkPolicy admits only cloudflared.
<!-- codex: The Headlamp readonly precedent grants wildcard read including Secrets, which is not acceptable for a multi-tenant control plane analyzer. The Flux patch precedent also documents field-level escape risk; patching Flux CRs can redirect sources or impersonation unless admission policy constrains fields. -->
- **3-section webapp** (vue-router over v1 dashboard components): Infrastructure (connect cluster;
  analyzer view; Provision → git), Workspaces (add repo; access key → OpenBao; per-workspace config;
  Gitea bootstrap via v1 `bootstrap.py`), Monitoring (v1 kanban+feed+worker-strip, fleet/per-workspace
  + utilization; fed by workers pushing to `POST /api/ingest/events`, CP keeps the board read model +
  rebroadcasts SSE).
<!-- codex: Worker event ingest is an untrusted multi-tenant write path and needs idempotency keys, schema limits, tenant/workspace authorization, replay protection, and cardinality/rate limits. Otherwise a malicious spoke or worker can corrupt the read model or overload metrics/SSE. -->
- **Provisioning**: CP renders `Workspace`/`AgentWorkerPool` CRs + manifests, commits via GiteaClient
  to `kubernetes/apps/apps/agentforge/tenants/<org>/<workspace>/`; hub Flux reconciles; a **kro RGD**
  expands each into ns/SA/ESO SecretStore+ExternalSecret/KEDA ScaledObject/worker Deployment/RBAC/
  NetworkPolicy. `ConnectedCluster` (cross-cluster) reconciled by the CP itself.
<!-- codex: Tenant paths alone are not a security boundary if Flux applies them with one broad identity. The plan needs generated-only manifests, server-side validation/admission for allowed GVKs/fields, and a decision on whether tenants can ever write to these Git paths directly. -->
- **External clusters (hub-spoke, pull-based)**: each spoke runs its own Flux (or the "our agent"
  bundle), pulls only its tenant's desired state from a per-tenant git target; spoke→hub limited to
  worker-config fetch, event ingest, node-snapshot push (per-workspace-bearer scoped). Hub's only
  outward privilege = a scoped git push target, never a spoke kubeconfig.
<!-- codex: This protects the hub from holding spoke kubeconfigs, but it cannot make a malicious spoke trustworthy: it can fake status, ignore desired state, run modified workers, replay events, or exfiltrate secrets intentionally delivered to it. The hub must treat every spoke payload as hostile and use read-only spoke deploy keys plus per-workspace, rotatable credentials. -->
- **Resource analyzer** = CP async read-only job: node allocatable (metrics-server + kube-state-metrics;
  spokes push snapshots), 60–70% p95 headroom, +~128 MiB/Kata-pod, `recommended_workers` → advisory in
  DB; Provision → git commit sets `AgentWorkerPool.maxReplicas` → kro → `ScaledObject.maxReplicaCount`.
<!-- codex: +128 MiB per Kata pod is likely too low once the guest kernel, agent image, DinD daemon, Docker graph storage, and runner/test containers are included. External clusters may not have metrics-server/KSM or may report malicious snapshots, so recommendations must be advisory with conservative floor/ceiling validation. -->

### Worker + sandbox

- **Config seam**: `adapters/config/control_plane.py` (`ControlPlaneConfigSource`, ~100 lines, mirrors
  `gitea_repo.py`). `AF_CONFIG_SOURCE=control_plane`; fetches
  `GET /api/v1/workspaces/{ws}/pools/{pool}/config` with a per-workspace bearer (OpenBao-minted,
  ESO-synced). Reuses v1 poll loop; a CP "config changed" hint triggers immediate refresh.
<!-- codex: This P1/P2 dependency is inconsistent: P1 says no OpenBao/ESO yet, but this seam depends on an OpenBao-minted, ESO-synced bearer. P1 needs an explicit SOPS/static-token fallback and the same persisted last-good behavior as v1 for CP outages. -->
- **Image** (`deploy/Dockerfile`, multi-stage): dashboard build → uv venv → runtime (python3.12 + git
  + Node + `@anthropic-ai/claude-code` + `@openai/codex` + docker CLI + tini), non-root uid 1000,
  volumes state_dir + jobs_root. Built+pushed to `registry.chifor.me` (anonymous-pull, no
  imagePullSecret) by a new `image` job in `release.yml`; pin references `worker@sha256:…`.
<!-- codex: The fat image is a cold-start and supply-chain risk under scale-to-zero; pre-pull helps image transfer only, not Kata boot, DinD startup, auth canary, or nested test image pulls. Pinning by digest is good, but the plan should add vulnerability scanning/signing and separate images if dashboard/worker/DinD tooling diverge. -->
- **Three-tier privilege split per pod** (the airtight boundary): orchestrator container holds bot
  PATs/HMAC/git-push/OpenBao-SA; agent container (claude/codex CLI) holds inference OAuth only, no PAT
  mount; test_cmd/setup_cmd run in a DinD container with `--network none` and NO cred mount.
<!-- codex: "Airtight" is not justified while all tiers are in one pod: localhost, pod-level egress policy, pod SA token projection, and any shared Docker API/socket can cross tiers. Inference OAuth is also a high-value tenant-zero credential, and prompt-injected agent tools may read it from their own env/home unless the runner prevents tool-env inheritance or uses a broker. -->
- **Credential injection (OpenBao+ESO)**: `af-forge-creds` → orchestrator only; `af-claude-oauth`
  (`CLAUDE_CODE_OAUTH_TOKEN`, ~1yr, no auto-refresh → rotate on the yearly auth-canary alert; P3
  apiKeyHelper-via-broker) → agent env only; `af-codex-auth` (`~/.codex/auth.json`, auto-refreshes →
  writable emptyDir seeded by init) → agent only; `af-runner-token` → CI runners only. Wire the
  runners' `home=` seam from `AF_CLAUDE_HOME`/`AF_CODEX_HOME`; add `CLAUDE_CODE_OAUTH_TOKEN` to
  `scrubbed_env` passthrough (agent children only, never test_cmd).
<!-- codex: Adding OAuth tokens to agent child envs weakens the scrubbed-env model because Claude/Codex tool subprocesses may inherit and expose them through Bash, files, or model output. Treat this as a temporary tenant-zero-only exception with tests proving test_cmd and repo shell tools cannot read it, or move directly to a broker/helper. -->
- **Executor port** (`ports/executor.py`): both untrusted call sites route through it —
  `Workspace.run_cmd` and the runner CLI subprocesses. `LocalExecutor` = today's subprocess
  (dev/tests); `SandboxExecutor` = `docker run` into a per-pod DinD sidecar (safe in the Kata
  microVM), `--network none` default, `--cap-drop ALL`, `--read-only`, pids/mem limits,
  kill-on-timeout. The shared jobs volume must mount at an identical path in orchestrator + agent +
  DinD (the `-v` resolves in the DinD daemon's FS — the load-bearing pod-spec detail). Opt-in
  `repo.sandbox: {kind|vcluster}` for k8s-sandbox tests.
<!-- codex: The runner CLI and repo test_cmd are different trust classes: Claude/Codex need OAuth homes and network egress, while tests need no credentials and default-deny network. A single executor path will either break runner auth/networking or accidentally grant repo-controlled Bash access to inference credentials. -->
<!-- codex: Privileged DinD inside Kata is a blast-radius reduction, not proof of safety; it depends on the RuntimeClass applying to the whole pod, Kata privileged-device behavior, no hostPath mounts, and no exposed Docker TCP socket. The Docker API must be reachable only by the orchestrator, because `--network none` on child containers does not protect against another pod container using dockerd directly. -->
<!-- codex: The identical-path shared volume detail can work for DinD bind mounts only if the volume is mounted into the DinD daemon container at that exact path with correct ownership and propagation semantics. It also creates a shared writable channel from untrusted code back to the orchestrator, so outputs need strict ownership, path, and symlink validation. -->
- **Egress (Cilium default-deny + allowlist)**: per-Deployment CiliumNetworkPolicy (Claude→anthropic,
  Codex→openai, tester→litellm-local, orchestrator→forge/OpenBao/litellm); per-exec test_cmd→
  `--network none` (opt-in pull-through package proxy).
<!-- codex: Pod-level network policy cannot distinguish orchestrator, agent, and DinD containers inside the same pod, so allowing orchestrator egress to Forge/OpenBao also allows agent code in that pod to attempt the same destinations. Per-container egress requires separate pods, a service-mesh/proxy design, or another enforceable mechanism. -->
- **Deployment/role model**: one image, roles via config, one Deployment per OAuth account
  (`af-claude-max1` planner/reviewer, `af-claude-max2` implementer +DinD, `af-codex` cross-reviewer,
  `af-tester` litellm +DinD, `af-dashboard` trusted/runc). Only implementer+tester get DinD. Distinct
  `AF_WORKER_NAME` per pod via downward API.
<!-- codex: One Deployment per account is the right shape for KEDA caps, but planner+reviewer sharing `af-claude-max1` means scaling and pending metrics must be account-aware, not only role-aware. Also set worker internal concurrency to 1 and rolling-update `maxSurge: 0` if the pod count is intended to be the hard account cap. -->
- **Autoscaling (KEDA scale-to-zero)**: `maxReplicaCount = accounts[X].max_parallel` — the KEDA cap IS
  v1's per-account semaphore (claim lock dedups; no new code). Signal from an always-on read-only
  `af-dispatcher` (scale-to-zero can't self-report): polls Gitea, exports `forge_pending{role,repo}`,
  Prometheus scaler. Cron warm-floor for interactive roles; pre-pull DaemonSet for cold start.
<!-- codex: `maxReplicaCount` caps desired replicas for a ScaledObject, but it is not a strict runtime semaphore during rollouts, termination grace, HPA/KEDA handoff, or if each pod can run multiple claims. Keep an app-level per-account lease or add explicit Deployment strategy, termination, and concurrency tests that count Running+Terminating pods. -->
<!-- codex: The dispatcher metric shape omits account/pool, claim state, and tenant, which can over-scale shared-account deployments or leak tenant workload shape through Prometheus. The oracle should subtract valid in-flight claims and expose labels that match the exact ScaledObject/account being controlled. -->
- **CI runners (k8s-native)**: KEDA `ScaledJob` — one ephemeral Kata runner + privileged DinD per
  queued job (token from OpenBao/ESO, label `self-hosted-hv` with `docker://` schema). Same sandbox
  mechanism as workers; deletes v1's workspace-reclaim machinery.
<!-- codex: CI jobs are tenant-controlled code with a runner token and Docker access, so they need a separate threat model from workers: short-lived registration tokens, no shared cache secrets, bounded egress, and cleanup of DinD graph storage. KEDA ScaledJob concurrency also needs a per-org cap, not just queue depth. -->

### Infrastructure (ailab)

- **Dedicated Talos agent node pool** (new tofu `kubernetes/infra/agent-nodes/`, mirrors
  `dev-workers/main.tf`): Proxmox VMs, `cpu type=host` (nested virt), joined as workers
  (`machine_type=worker`, new `worker.yaml.tftpl`) with Kata + gVisor Talos system extensions, kernel
  modules `vhost_net`/`vhost_vsock`, label `ailab.io/agent-pool` + taint `dedicated=agent`. Nested virt
  must be enabled on the Proxmox hosts (`kvm_amd nested=1`) or Kata `/dev/kvm` is absent → gVisor
  fallback for compute-only roles.
<!-- codex: Mirroring `dev-workers/main.tf` is only a rough pattern: Talos worker joining needs machine secrets, install image/extensions, node cert bootstrap, CNI readiness, and safe rolling replacement. gVisor fallback cannot cover privileged DinD roles, so scheduling must fail closed when Kata is unavailable. -->
- **RuntimeClasses** `kata` (QEMU) + `gvisor` (runsc), agent-pool nodeSelector/toleration.
<!-- codex: RuntimeClass is pod-scoped, so every sidecar in the pod must run under Kata for the DinD safety claim to hold. Namespace PSA/PodSecurity exemptions for privileged DinD should be narrowly scoped, otherwise tenant namespaces become broadly privileged. -->
- **New operators (Flux)**: OpenBao, External Secrets Operator, KEDA, kro under
  `kubernetes/apps/infrastructure/{security,autoscaling}/`.

## Phasing

- **P1 — tenant-zero, hub only, full vertical slice**: agentforge-platform repo (CP API + Authelia
  OIDC + Postgres schema + 3-section webapp); connect one cluster = the hub (Headlamp-style SA);
  Workspaces CRUD → CRs → kro materializes (secrets still SOPS, no OpenBao yet); worker image +
  ControlPlaneConfigSource; the dedicated agent node pool (plain, no Kata yet); monitoring
  fleet-aggregation; expose `agentforge.chifor.me`. One shadow Deployment (planner, merge disabled,
  playground) proving auth-canary + config fetch + a 1→2 transition.
<!-- codex: P1 cannot both rely on `kro materializes` and defer `kro installed` to P2. Either install kro in P1 with SOPS/static-secret templates, or make P1 commit plain manifests without kro. -->
<!-- codex: The shadow worker also needs a config bearer and OAuth secret path, but P1 says no OpenBao/ESO yet while the Worker section assumes OpenBao-minted ESO-synced credentials. Define the P1 credential source explicitly so the vertical slice is actually shippable. -->
- **P2 — secrets + scaling + sandbox (the unlock)**: OpenBao + ESO + KEDA + kro installed; migrate
  secrets off SOPS into OpenBao (namespaced SecretStore per workspace); Kata node-pool extensions +
  RuntimeClasses; full per-account Deployment set + DinD + SandboxExecutor + Cilium egress + KEDA
  scale-to-zero + af-dispatcher + k8s-native CI runners. All sandbox-boundary tests green → flip
  `privilege_hardening: v1.1` → unlock non-playground repos.
<!-- codex: Flipping `privilege_hardening: v1.1` after P2 is only valid if the tests prove the same guarantees ADR 0018 required: no credential read, no network by default for test_cmd, and no same-UID/same-pod escape. As written, same-pod networking and Docker API access leave the gate unsatisfied. -->
- **P3 — external clusters + multi-user + fleet**: spoke "install our agent" onboarding (pull model);
  Authelia groups → org RBAC + Postgres RLS enforced; per-org quotas; per-tenant LiteLLM virtual keys
  w/ budget caps; dogfood (`cchifor/agentforge` PRs flow through the deployed system); optional Claude
  apiKeyHelper-via-broker hardening.
<!-- codex: Subscription-OAuth gating must be enforced before external tenants exist, not only as P3 policy. The CP config generator and worker validator should reject `claude_max` for non-tenant-zero orgs, and the corresponding Secrets must not exist outside tenant-zero namespaces. -->

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
<!-- codex: Add admission/policy manifests and contract tests to the critical file list; they are central to the GitOps, tenant, and credential boundaries. Without them, several key controls exist only as renderer convention. -->

## Verification

- **Sandbox boundary (the #1 gate)**: a canary malicious repo + prompt-injected issue whose
  test_cmd/agent try to read bot PATs, inference OAuth, OpenBao, orchestrator `/proc/1/environ`, and
  egress to the internet — assert every attempt fails (no cred mounts, separate ns, `--network none`);
  assert `uname -r` shows the Kata guest kernel; assert the egress allowlist matrix; assert privileged
  DinD can't escape the microVM.
<!-- codex: `/proc/1/environ` is not enough: in separate PID namespaces, `/proc/1` is the local container init, not the orchestrator. Tests must also attempt pod SA token use, localhost service access, Docker API access, shared-volume symlink/hardlink attacks, and egress to every destination allowed for any container in the pod. -->
- **KEDA 0→N→0**: seed K issues → scale to `min(K, maxReplicaCount)`, process (claims don't collide —
  reuse `contract-claims`), return to 0 after cooldown; assert the cap holds.
<!-- codex: This verification should count active claims and Running+Terminating pods during rollouts, rapid scale up/down, auth-canary delays, and pod crashes. Otherwise it only proves KEDA's desired replica cap, not the subscription concurrency cap. -->
- **Credential injection**: pod auth-canary passes non-interactively from ESO creds; rotate OpenBao →
  ESO re-sync → pod roll → canary passes; Codex refresh persists then re-seeds.
<!-- codex: Add negative credential tests across tenants and containers: agent cannot read forge creds, test_cmd cannot read inference creds, worker SA cannot list Secrets, one workspace bearer cannot fetch another workspace config, and non-tenant-zero cannot resolve subscription OAuth. -->
- **Platform**: OIDC login; create workspace → git commit → kro materializes; analyzer recommends vs
  live allocatable; monitoring aggregates the fleet; RLS blocks cross-org reads (P3).
<!-- codex: RLS should not wait for P3 if the DB schema exists in P1; test it from the first migration. Also include background jobs, ingest endpoints, and admin/service code paths, which often bypass request-scoped `SET LOCAL`. -->
- **k8s/GitOps**: `kubectl --context admin@ai` sees tenant namespaces, RuntimeClasses, ScaledObjects,
  synced ExternalSecrets; Flux reconciles the committed tenant tree; cloudflared serves the app.
<!-- codex: GitOps verification should include a malicious manifest attempt in a tenant path and prove admission/Flux scoping rejects it. Seeing objects reconcile is not enough to prove the CP cannot mutate unrelated cluster resources. -->

## Risks

1. **Multi-tenant secret isolation (highest)** — enforce in the kro RGD: always a namespaced
   SecretStore (never Cluster), OpenBao k8s-auth role pinned to exact namespace+SA, policy scoped to
   `af/<org>/<workspace>/*`, NetworkPolicy so workers reach only ESO/OpenBao/CP.
<!-- codex: Namespaced SecretStore is necessary but insufficient: ESO still materializes Kubernetes Secrets, and any pod with secret RBAC or mounted/token access can read them. Add automountServiceAccountToken=false by default, projected tokens only where needed, no worker RBAC to Secrets, and admission that prevents ClusterSecretStore/path escapes. -->
2. **External-cluster trust boundary** — pull-based only; hub's sole outward privilege = scoped git
   push; config endpoint per-workspace-bearer scoped; every spoke→hub call is untrusted input.
<!-- codex: This risk should explicitly state the residual limitation: the platform cannot attest that an external spoke ran the intended manifests or protected tenant secrets once delivered. Hub APIs need replay protection, schema validation, per-spoke quotas, and audit trails that distinguish reported state from trusted state. -->
3. **Subscription-OAuth sharing across tenants** — `claude_max` is tenant-zero only; external tenants
   get per-tenant LiteLLM virtual keys with per-org budget caps; allowed engines gated by org plan.
<!-- codex: "Allowed engines gated by org plan" must be enforced in the CP API, generated config, worker startup validation, and admission policy; UI-only gating is bypassable. Also ensure tenant-zero OAuth Secrets are never referenced by shared templates or external-cluster Git targets. -->
4. **Cold-start latency** — fat image + Kata boot + auth canary; pre-pull DaemonSet + cron warm-floor;
   true zero overnight.
<!-- codex: This risk is understated economically: pre-pull does not warm Kata microVMs, DinD graph layers, package installs, or auth canaries, and external clusters may not support image pre-pull. Define acceptable first-issue latency and when interactive roles should use minReplicaCount instead of true zero. -->
5. **Scope** — multi-month platform; P1 near-term, P2 unlocks safe non-playground use, P3 full
   SaaS/fleet. Execute per phase.
<!-- codex: P1 should be reduced to a truly coherent slice: CP API/UI, DB/RLS, static hub manifests or installed kro, SOPS-backed tenant-zero secret path, one shadow worker, and no claims about full multi-tenancy or v1.1 hardening until P2 tests pass. -->

<!-- codex-review-status: complete -->