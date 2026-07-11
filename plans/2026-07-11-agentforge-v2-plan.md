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
the keystore, **GitOps provisioning** (web app → Gitea commit → Flux + a kro operator reconcile).
**The data/auth model is multi-tenant and RLS+org-membership is enforced from P1; only the
*compute + secrets vault* start as tenant-zero (the ailab cluster) and generalize in P2/P3.** v1 is
reused, not rewritten - the orchestrator, role handlers, codex alignment gates, epoch-locked claim
lock, GiteaClient, Ledger are unchanged; v2 adds a control plane, containerizes the worker, and
puts untrusted execution in an isolated sandbox pod.

## Governing principles

- **Web app writes desired state; a reconciler converges it — behind BOTH a server-side write scope
  AND an admission gate.** The CP bot pushes ONLY to a **separate `cchifor/agentforge-tenants`
  Gitea repo** (Gitea repo-level permissions enforce this server-side — the CP bot has **no write
  to `cchifor/ailab`**, where the root Flux config, the tenant Flux Kustomization definitions, and
  the admission policies live). A dedicated **tenant Flux Kustomization (its own minimal-RBAC SA,
  `wait: false`)** reconciles `agentforge-tenants`, and a **ValidatingAdmissionPolicy (or Kyverno)**
  — itself defined in the CP-unwritable `ailab` repo — restricts that path to an allowlist of GVKs
  and fields (no ClusterRole/RBAC/`*`-verb objects, no privileged pods outside the sandbox
  namespace, no changes to Flux `sourceRef`/`serviceAccountName`). So even a fully compromised CP
  can neither redirect the reconciler nor commit cluster-admin: its token physically cannot reach
  the root/admission paths, and admission rejects anything outside the tenant allowlist.
- **The security boundary is the POD, not the container.** Containers in one pod share the network
  namespace and (by default) the SA token, and any pod container can reach a sidecar Docker socket -
  so a "three-tier split inside one pod" is a blast-radius reduction, **not** a boundary. Untrusted
  agent + `test_cmd` execution therefore runs in a **separate ephemeral Kata sandbox pod** with its
  own scoped SA (`automountServiceAccountToken: false`, zero Secret RBAC), its own default-deny
  CiliumNetworkPolicy (model endpoint only), and no path to the orchestrator's creds, forge route,
  or Docker API. This is what actually satisfies ADR 0018's v1.1 gate — proven by the boundary tests.
- **Forge/DB are the source of truth; workers stateless — but subscription concurrency is an
  explicit lease, not an emergent property.** v1's claim lock dedups work per issue; it does NOT cap
  per-account parallelism. v2 adds a **forge-backed per-account lease that reuses v1's EXACT
  epoch-bound claim protocol** (`ClaimService`, already contract-tested): a lease is an append-only
  comment carrying an **owner epoch + worker id + TTL/`expires`**; acquisition is the same
  lowest-valid-id election, held leases **heartbeat** their `expires`, the reconciler/reaper
  **stale-breaks** expired leases (a crashed pod can't strand the semaphore), and **release is
  compare-on-owner** (a late release cannot clear a newer epoch's lease). N valid account leases =
  at most N concurrent runs on that account. KEDA `maxReplicaCount` + `maxSurge: 0` + per-pod
  concurrency 1 are the coarse cap; the lease is the correct, crash/scale-to-zero-safe one.
- **Every boundary is enforced in code + admission, never UI-only.** Engine gating, org membership,
  tenant scoping, and allowed-GVK are enforced at the CP API, the generated config, worker startup
  validation, AND admission policy.

## Architecture

### Two repos

- `cchifor/agentforge` (v1, extended): orchestrator/roles/gates unchanged **+** new
  `ControlPlaneConfigSource`, `Executor` port (routes untrusted exec to the sandbox), `deploy/`
  images, `dispatcher` subcommand, `sandbox-runner` entrypoint. Runs as the worker image.
- `cchifor/agentforge-platform` (NEW): control-plane API (FastAPI + Authelia OIDC), webapp SPA (3
  sections), kro RGDs + `ConnectedCluster` reconciler, resource analyzer, OpenBao/git/kube adapters,
  admission policies. Runs on the hub, ns `agentforge`. Depends on v1 for the config schema.
  **CP↔worker compatibility is contract-tested** (not just schema-parsed): a versioned fixture suite
  asserts every CP-generated config a worker of version X must enforce (tenant/pool/engine/executor
  policy), run in both repos' CI.

### Worker topology — orchestrator pod + ephemeral sandbox pod (the boundary)

- **Orchestrator pod** (per OAuth account, KEDA-scaled): runs the Python orchestrator loop, holds
  the forge bot PATs / git-push token / config bearer, does ALL forge writes + git push. It runs
  **no untrusted code and no agent CLI**. Egress: forge, OpenBao, CP, model-routing only. It has
  scoped RBAC to create/delete **sandbox Pods in a dedicated `af-sandbox-<tenant>` namespace only**.
  That create-pod surface is closed by a **ValidatingAdmissionPolicy on the sandbox namespace that
  PINS the pod shape** — any sandbox Pod the orchestrator submits MUST have: `runtimeClassName:
  kata`; images from the **allowed digest allowlist only** (the pinned sandbox image); a **fixed
  `serviceAccountName` with `automountServiceAccountToken: false`**; **no `hostPath`/`hostNetwork`/
  `hostPID`/`hostIPC`/host ports**; volumes restricted to `emptyDir`/`projected`/the per-job PVC;
  and the **required NetworkPolicy/Cilium labels** (so the model-only egress policy always binds).
  The privileged DinD container is allowed **only** in this namespace (narrow PodSecurity exemption)
  and only in the pinned shape. A compromised orchestrator therefore cannot mint an escalated pod
  (host mounts, extra creds, broad egress, or a foreign image) — admission rejects it.
- **Sandbox pod** (ephemeral, Kata, one per job; warm pool to hide boot): runs the **agent CLI
  (claude/codex) AND `test_cmd`**. It holds **only the inference OAuth** (mounted just for the agent
  container) and the job checkout (per-job volume). Its SA has **no Secret RBAC and no automounted
  token**; its CiliumNetworkPolicy allows **only the model endpoint** (Anthropic / OpenAI /
  litellm-local) - never forge, never OpenBao, never the orchestrator. `test_cmd` runs in a nested
  DinD container with `--network none` + no cred mount; DinD's socket is reachable only within the
  sandbox pod (which itself can't reach anything but the model), so the confused-deputy path is
  closed. The orchestrator streams the prompt in and the diff out over a scoped exec channel, then
  reaps the pod. The inference OAuth is a **tenant-zero-only** high-value credential (see gating);
  P3 replaces even that with an `apiKeyHelper`→broker so the agent env never holds a durable token.
- **Latency**: a **warm sandbox pool** (a few pre-booted Kata pods per account, `minReplicaCount≥1`
  for interactive roles) hides Kata boot + image pull + auth canary; true scale-to-zero only for
  non-interactive/overnight. First-issue latency SLO: < 30s warm, < 120s cold.

### Control plane

- **Auth**: FastAPI OIDC RP against Authelia (auth-code + PKCE/S256); client block in
  `authelia-config.yaml`, plaintext in a SOPS secret. **The CP does its own OIDC, so it is NOT
  behind CF Access** (that would double-login and estate convention leaves OIDC apps Access-free);
  exposure is cloudflared → CP. **Org membership + role are enforced by FastAPI on every route** from
  P1 (CF Access is not the boundary; there is none in front of the API). `groups` claim → org/role.
- **DB**: new `agentforge_platform` DB+role on CNPG `infra-pg`. `postInitSQL` does NOT run on the
  live cluster → a **one-shot migration Job** (alembic + a bootstrap SQL that creates the role/DB)
  is the provisioning path; SOPS DSN secret; bump the PVC (verify `allowVolumeExpansion`). Model:
  orgs, users, memberships(role), connected_clusters, workspaces, agent_worker_pools, secret_refs
  (OpenBao pointers - never values), workspace_config_versions, cluster_node_snapshots, audit_log.
  **RLS is on from the first migration** (`SET LOCAL app.current_org`), with tests for the bypass
  traps codex named: missing-SET, background jobs, pooled-connection reuse, ingest endpoints, and
  admin/service paths (a dedicated non-RLS admin role is explicit and audited, never the default).
- **k8s access**: dedicated ns+SA + a **tightened read ClusterRole** — NOT Headlamp's wildcard: the
  analyzer reads nodes/pods/metrics/kube-state (allocatable + usage) but **has no `secrets` read**;
  status reads are per-tenant-namespace scoped. The scoped-write role is **only** `patch` on Flux
  Kustomizations to nudge reconcile, and an admission policy constrains which fields (no
  `spec.sourceRef`/`serviceAccountName` changes → no source-redirect/impersonation). NetworkPolicy
  admits only cloudflared.
- **3-section webapp** (vue-router over v1 dashboard components): Infrastructure (connect cluster;
  analyzer; Provision → git), Workspaces (add repo; access key → OpenBao; per-workspace config;
  Gitea bootstrap via v1 `bootstrap.py`), Monitoring (v1 kanban+feed+worker-strip, fleet/per-workspace
  + utilization). **Event ingest (`POST /api/ingest/events`) is a hardened untrusted write path**:
  per-workspace bearer authz (a worker can only write its own workspace's events), idempotency key +
  replay window, strict schema + field-size caps, per-workspace rate + cardinality limits; the CP
  keeps the board read model and rebroadcasts SSE. Malicious worker/spoke input cannot corrupt the
  model or overload metrics.
- **Provisioning**: CP renders `Workspace`/`AgentWorkerPool` CRs **from templates only** (never
  arbitrary user YAML) and commits via GiteaClient to
  `kubernetes/apps/apps/agentforge/tenants/<org>/<workspace>/`. A **per-tenant Flux Kustomization
  (its own minimal-RBAC SA, `wait: false` so tenant churn can't wedge the apps layer)** reconciles
  it; the **admission policy rejects any GVK/field outside the tenant allowlist** — tested with a
  malicious-manifest attempt. In P1, kro is NOT yet installed, so the CP renders **plain
  hand-authored manifests** (ns/SA/Deployment/RBAC/NetworkPolicy) directly; **P2 introduces the kro
  RGD** to DRY the expansion. `ConnectedCluster` is reconciled by the CP itself.
- **External clusters (hub-spoke, pull-based - P3)**: each spoke runs its own Flux (or the "our
  agent" bundle) and pulls only its tenant's desired state via a **read-only spoke deploy key**;
  spoke→hub is limited to worker-config fetch, event ingest, node-snapshot push, all per-workspace
  bearer + the ingest hardening above. **The hub treats every spoke payload as hostile** and cannot
  attest a spoke actually ran the intended manifests or protected delivered secrets - the platform
  distinguishes *reported* from *trusted* state, and per-workspace creds are short-lived + rotatable.
  Hub's only outward privilege = the scoped git push target.
- **Resource analyzer** = CP async read-only job: node allocatable (metrics-server + kube-state-metrics;
  spokes push snapshots - validated with conservative floors/ceilings since a spoke may lie or lack
  metrics-server). Per-worker footprint budgets the **full sandbox cost** (Kata guest kernel + agent
  image + DinD daemon + Docker graph storage + test containers ≈ **512 MiB–1 GiB/job**, not 128 MiB)
  at 60–70% p95 headroom → advisory `recommended_workers` in DB. "Provision" → git commit sets
  `AgentWorkerPool.maxReplicas`. Advisory only; never auto-applies destructive changes.

### Worker + sandbox (agentforge extended)

- **Config seam**: `adapters/config/control_plane.py` (`ControlPlaneConfigSource`, mirrors
  `gitea_repo.py`: `current/degraded/refresh()`, persisted last-good, `compatibility()`). Fetches
  `GET /api/v1/workspaces/{ws}/pools/{pool}/config` with a bearer. **P1 credential source = a SOPS
  static per-workspace token** (no OpenBao/ESO dependency); **P2 switches to an OpenBao-minted,
  ESO-synced bearer.** Same persisted-last-good so a CP outage keeps the worker draining, `readyz`
  degraded, no new claims. `serve()` always builds the `GiteaClient` (issues/PRs) regardless of
  config source - the source only swaps where the config JSON comes from.
- **Images**: multi-stage, split by concern to bound the fat-image/supply-chain risk - an
  **orchestrator image** (python + git + the orchestrator; no agent CLIs, no docker) and a
  **sandbox image** (agent CLIs claude/codex + docker CLI + test toolchain). Both non-root uid 1000,
  built+pushed to `registry.chifor.me` by a `release.yml` image job, **scanned by the existing ailab
  Trivy** and referenced by digest (`@sha256:…`, immutable). Pre-pull DaemonSet on the agent pool.
- **Executor port** (`ports/executor.py`): the ONE place untrusted exec is dispatched.
  `ExecSpec{argv|shell, cwd, env, timeout_s, stdin, creds: none|inference, egress: none|model,
  image}`. **`LocalExecutor` is the DEFAULT** (`executor or LocalExecutor()`), preserving today's
  subprocess behavior so the entire existing suite stays green with zero edits (verified: only
  test_workspace/test_handlers_workspace/test_runners_* touch the seam and pass because behavior is
  preserved; litellm runner is HTTP, not routed). `SandboxExecutor` (P2) dispatches into the
  ephemeral sandbox pod. **The agent CLI and `test_cmd` are DISTINCT trust classes**: the agent CLI
  runs with `creds=inference, egress=model`; `test_cmd`/`setup_cmd` run with `creds=none,
  egress=none` — the port carries the class so they can never be conflated. Unify the Windows kill
  path on `taskkill /T` (strict superset). Shared checkout volume is written only by the sandbox and
  read back by the orchestrator with **strict path/ownership/symlink validation** (the diff is the
  only channel out).
- **Credential injection (P2, OpenBao+ESO)**: `af-forge-creds` (PATs/HMAC/git-push/config-bearer) →
  **orchestrator pod only**; `af-claude-oauth` (`CLAUDE_CODE_OAUTH_TOKEN`) + `af-codex-auth`
  (`~/.codex/auth.json`, writable emptyDir seeded by init) → **sandbox pod's agent container only**;
  `af-runner-token` → CI runners only. Because the sandbox is a separate pod with model-only egress
  and no Secret RBAC, a prompt-injected agent holds only the inference token and can exfil it *at
  most to the model API* - accepted for tenant-zero, removed by the P3 broker.
  `CLAUDE_CODE_OAUTH_TOKEN` is added to the agent container's env only (never `test_cmd`), proven by
  a `test_cmd` dump-env negative test.
- **Closing the readback exfil channel (P2, gating the v1.1 flip).** Model-only Cilium egress does
  NOT make the model API the only exfil path: the diff/logs/stdout the orchestrator reads back and
  publishes to the forge is itself an egress channel, and a prompt-injected agent that can read the
  inference OAuth could smuggle it into the diff. Two defenses, BOTH required before the sandbox is
  "closed" (moved earlier from P3):
  1. **Brokered ephemeral inference creds** — the sandbox agent gets NO durable token; Claude's
     `apiKeyHelper` (and the Codex equivalent) fetch a short-lived token over a unix socket from a
     broker sidecar in the sandbox pod that holds the OpenBao identity. There is no long-lived
     secret in the agent env to smuggle. (This was P3; it is now a **P2 prerequisite** for the flip.)
  2. **Mandatory outbound redaction** — everything the orchestrator publishes (PR diffs, `af:*`
     comments, logs, events) passes a secret-scanner/redactor (token shapes + known credential
     fingerprints) before any forge write. Defense-in-depth even after (1).
  The v1.1 gate flips only when the boundary tests below prove both a durable-token exfil attempt
  (no token to steal) and a redaction bypass attempt fail.
- **Egress = per-POD Cilium policy** (now correct, since orchestrator and sandbox are separate pods):
  orchestrator pod → forge/OpenBao/CP/model-routing; sandbox pod → the one model endpoint only;
  `test_cmd` container → `--network none` (opt-in pull-through package proxy).
- **Deployment/role + concurrency**: one orchestrator image, roles via config, **one orchestrator
  Deployment per OAuth account**; `maxSurge: 0`, per-pod concurrency 1, and a **forge-backed
  per-account lease** as the true semaphore (KEDA cap is coarse). Scaling + the dispatcher metric are
  **account-aware, not role-aware** (planner+reviewer share max1). Distinct `AF_WORKER_NAME` per pod
  via the downward API.
- **Autoscaling (KEDA)**: interactive roles use `minReplicaCount≥1` (warm), non-interactive scale to
  zero. The always-on read-only **`af-dispatcher`** exports `forge_pending{account,pool,role,repo}`
  **minus valid in-flight claims** (so it doesn't over-scale shared accounts or leak workload shape).
  `maxReplicaCount = accounts[X].max_parallel`; the lease enforces the hard cap. KEDA verification
  counts **Running+Terminating** pods across rollouts/termination, not just desired replicas.
- **CI runners (P2, k8s-native)** — a **separate threat model** from workers: KEDA `ScaledJob`, one
  ephemeral Kata runner + DinD per queued job, **short-lived registration token** from OpenBao/ESO,
  no shared cache secrets, bounded egress, DinD graph-storage cleanup on teardown, and a **per-org
  concurrency cap** (not just queue depth). Label `self-hosted-hv` with the `docker://` schema.

### Infrastructure (ailab)

- **Dedicated Talos agent node pool**: Talos **worker** nodes must reuse the existing cluster
  `machine_secrets` bundle (in `infra/terraform.tfstate`) - a fresh `talos_machine_secrets` forks
  the PKI and never joins. Design: read `infra/` remote state via sensitive outputs (or fold the
  pool into `infra/`); a new `worker.yaml.tftpl` (nodeLabels `ailab.io/agent-pool`, taint
  `dedicated=agent`, kernel modules `vhost_net`/`vhost_vsock`); **Kata + gVisor as Image-Factory
  extensions** (not machine-config patches - verify gVisor extension availability). **Nested virt is
  a new Proxmox-host prerequisite** (`kvm_amd nested=1`, configured nowhere today) + `cpu type=host`;
  without `/dev/kvm`, Kata pods fail - and **scheduling of DinD/sandbox roles must fail closed** (not
  silently fall back to gVisor, which can't host privileged DinD).
- **RuntimeClasses** `kata` (QEMU, pod-scoped → every sandbox sidecar runs under Kata) + `gvisor`,
  agent-pool nodeSelector/toleration; narrow PodSecurity exemption for the privileged DinD scoped to
  the sandbox namespaces only.
- **New operators (Flux, P2)**: OpenBao, ESO, KEDA, kro under
  `kubernetes/apps/infrastructure/{security,autoscaling}/` (CRDs before operators; note ordering).

## Phasing (revised for coherent slices)

- **P1 — tenant-zero compute, real multi-tenant data/auth, no OpenBao/kro/Kata yet**:
  agentforge-platform repo (CP API + Authelia OIDC + Postgres schema **with RLS + org enforcement +
  migration Job**); the 3-section webapp; connect one cluster = the hub (tightened read SA, no Secret
  read); Workspaces CRUD → **plain hand-authored manifests** committed to the tenant path behind the
  **admission policy + per-tenant Flux Kustomization**; worker orchestrator image +
  `ControlPlaneConfigSource` with a **SOPS static token**; the dedicated agent node pool (plain, no
  Kata); monitoring aggregation with hardened ingest; expose `agentforge.chifor.me` (no CF Access).
  One **shadow orchestrator Deployment** (planner, merge disabled, playground) proving OIDC login →
  create workspace → git commit → Flux applies → config fetch → a 1→2 transition. **No claim of
  sandbox hardening or full multi-tenant compute yet.**
- **P2 — secrets + scaling + the sandbox boundary (the unlock)**: OpenBao + ESO + KEDA + kro; secrets
  → OpenBao (namespaced SecretStore, `automountServiceAccountToken:false`, no worker Secret RBAC,
  admission preventing ClusterSecretStore/path escapes); Kata node extensions + RuntimeClasses;
  **the ephemeral sandbox-pod SandboxExecutor** + the **admission-pinned sandbox pod shape** +
  per-pod Cilium egress + **brokered ephemeral inference creds (apiKeyHelper→broker sidecar)** +
  **outbound diff/comment/log redaction** + the epoch-safe forge-backed account lease + KEDA +
  `af-dispatcher` + k8s-native CI runners; kro RGD DRYs the tenant expansion. **All
  sandbox-boundary tests green (below) → flip `privilege_hardening: v1.1` → unlock non-playground
  repos.** The flip is valid only because untrusted exec is now a separate pod with no creds, no
  Docker API reachability, and default-deny+model-only egress.
- **P3 — external clusters + full multi-tenant compute + fleet**: spoke onboarding (pull model,
  read-only deploy keys, hostile-payload handling); per-org quotas (ResourceQuota, lease caps,
  OpenBao namespace policies); **per-tenant LiteLLM virtual keys w/ budget caps**; dogfood (the
  inference-cred broker already landed in P2). **`claude_max` is
  rejected for non-tenant-zero orgs at the CP API, the generated config, worker startup, AND
  admission - and tenant-zero OAuth Secrets are never referenced by shared templates or spoke git
  targets.**

## Critical files

- agentforge (v1 seams): `src/agentforge/main.py`, `app/workspace.py`,
  `adapters/runners/{_envelope,claude_code,codex}.py`, `infra/{settings,metrics}.py`, NEW
  `ports/executor.py` + `adapters/exec/{local,sandbox}.py`, NEW `adapters/config/control_plane.py`,
  NEW `deploy/{orchestrator,sandbox}.Dockerfile`, `.github/workflows/release.yml`, NEW
  `sandbox-runner`/`dispatcher` entrypoints.
- agentforge-platform (new): `src/agentforge_platform/{api,domain,adapters,operator}/**`, `webapp/**`,
  `crds/**`, `admission/**` (VAP/Kyverno), `deploy/**`, `tests/contract/**` (CP↔worker).
- ailab: `kubernetes/infra/{agent-nodes or infra}/**` + `machine-config/worker.yaml.tftpl`,
  `kubernetes/apps/apps/agentforge/**` (RuntimeClasses, orchestrator + sandbox RBAC/NetworkPolicy,
  admission policies, per-tenant Flux Kustomization + tenants/ subtree), Proxmox nested-virt task,
  `kubernetes/apps/infrastructure/{security,autoscaling}/**`, `authelia-config.yaml`,
  `kubernetes/apps/databases/**` (+ migration Job), cloudflared + `cloudflare/dns.tf` (no Access app),
  `docs/decisions/0019-agentforge-v2-control-plane.md`.

## Verification

- **Sandbox boundary (the #1 gate — full matrix, not just `/proc/1/environ`)**: a canary malicious
  repo + prompt-injected issue whose agent/`test_cmd` attempt to: read bot PATs / inference OAuth /
  any Secret; **use the sandbox pod SA token** (must be absent/unmounted); reach the orchestrator, a
  localhost service, OpenBao, or the forge; reach the **Docker API** directly; symlink/hardlink-escape
  the shared checkout; and egress to **every destination allowed for any pod** - assert all fail.
  Assert `uname -r` shows the Kata guest kernel; assert privileged DinD cannot escape the microVM to
  the node. **Readback-exfil**: with the broker in place there is no durable token in the agent env
  to smuggle, AND an attempt to write a token-shaped string into the diff/comment/log is redacted
  before the forge write — assert both. **Admission-pinned pod**: the orchestrator cannot create a
  sandbox pod with a host mount, extra secret, foreign image, or broad egress (admission rejects).
  **Cross-tenant + cross-container negatives**: agent can't read forge creds; `test_cmd`
  can't read inference creds; worker SA can't list Secrets; one workspace bearer can't fetch another's
  config; non-tenant-zero can't resolve `claude_max`.
- **KEDA + lease**: seed K issues → scale to `min(K, maxReplicaCount)`, process (claims + the account
  lease prevent both collision and over-parallelism), return to warm/zero; assert the cap holds by
  counting **Running+Terminating** pods during rapid up/down, auth-canary delay, and pod crash;
  assert a **crashed lease-holder is stale-broken** (semaphore recovers) and a **late release
  cannot clear a newer epoch's lease** (reuse v1's ClaimService lease tests).
- **GitOps admission**: commit a malicious manifest (ClusterRole, privileged pod, source-redirect
  patch) to a tenant path → assert admission + Flux SA scoping reject it; seeing benign objects
  reconcile is not sufficient.
- **RLS (from P1)**: cross-org read blocked from the first migration, including background jobs,
  ingest endpoints, and admin/service code paths.
- **Platform**: OIDC login + org-membership enforced on every route; create workspace → git commit →
  (P1 plain manifests / P2 kro) materializes; analyzer recommends vs live allocatable with the
  corrected sandbox footprint; ingest rejects unauthorized/oversized/replayed events.
- **CP↔worker contract**: the versioned fixture suite passes in both repos' CI.

## Risks

1. **Same-pod isolation illusion (was the top design flaw; now resolved)** — untrusted exec is a
   **separate pod** with no Secret RBAC, no automounted token, model-only egress, and no orchestrator
   Docker-API reachability; the boundary tests above are the proof, gating the v1.1 flip.
2. **Multi-tenant secret isolation** - namespaced SecretStore is necessary but insufficient: also
   `automountServiceAccountToken:false` by default, projected tokens only where needed, **no worker
   RBAC to Secrets**, and admission preventing ClusterSecretStore/path escapes.
3. **GitOps write path** — the CP bot writes ONLY to `agentforge-tenants` (server-side repo
   permission; no write to `ailab` where root Flux + admission live) + generated-manifests-only +
   admission allowlist + per-tenant Flux SA. A compromised CP can neither redirect the reconciler
   nor escalate. Malicious-manifest + write-outside-tenant-repo rejection are required tests.
4. **External-cluster trust** — pull-based; hub can't attest spoke execution; every spoke payload is
   hostile input with replay protection, schema validation, per-spoke quotas, and reported-vs-trusted
   state separation; read-only deploy keys + rotatable per-workspace creds.
5. **Subscription-OAuth** — `claude_max` tenant-zero-only, enforced at API+config+worker+admission;
   external tenants get per-tenant LiteLLM virtual keys with budget caps; tenant-zero OAuth Secrets
   never referenced by shared/spoke templates.
6. **Cold-start economics** — warm sandbox pool + `minReplicaCount≥1` for interactive roles + pre-pull
   + Trivy-scanned split images; true zero only overnight/non-interactive; first-issue SLO defined.
7. **Scope** — multi-month; P1 is a coherent slice (CP+UI+DB/RLS+admission+plain manifests+SOPS
   token+one shadow orchestrator, no sandbox/v1.1 claim); P2 delivers the sandbox boundary + the
   unlock; P3 the full SaaS/fleet.

<!-- codex-review-status: complete -->
