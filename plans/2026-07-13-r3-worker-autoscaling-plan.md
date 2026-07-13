# R-3 — worker autoscaling topology (per-account orchestrator Deployments + af-dispatcher + KEDA + Cilium egress)

## Context

The plan's deferred P2 sub-tranche: the compute layer. R-1/R-2 built the sandbox boundary + broker +
provisioner; this adds the OPERATOR-owned tenant-zero worker fleet that actually claims forge work and
creates the ephemeral sandbox Jobs. All on ailab `feat/p2-unlock` (the P2-activation umbrella), DORMANT/
gated (placeholder `@sha256:0000…` images, unlisted Deployments) until the operator builds+pins the
worker image. NOTE: the R-1 redesign SUPERSEDED the per-pod DinD sidecars — untrusted exec now goes to a
separate ephemeral Kata Job (the SandboxExecutor), so a worker Deployment is a PURE orchestrator
(`agentforge serve`, `AF_EXECUTOR=sandbox`); NO DinD.

The scale oracle already exists in agentforge code: `agentforge dispatcher` (always-on, read-only) polls
the forge and exports `forge_pending{account,pool,role,repo}` on `metrics_port`. The KEDA operator is
already installed on `feat/p2-unlock`. This tranche wires them.

## Approach — new dir `kubernetes/apps/infrastructure/agentforge-workers/`

### 1. af-dispatcher (the scale oracle)
- `dispatcher-deployment.yaml` (GATED, placeholder digest): `agentforge dispatcher`, replicas 1,
  read-only (forge PAT via ESO `af-forge-creds`, no sandbox/broker creds), baseline PSA, exposes the
  prometheus `metrics_port` (9464). Trusted `agentforge` ns.
- `dispatcher-service.yaml`: ClusterIP exposing the metrics port.
- `dispatcher-servicemonitor.yaml`: scrape it into kube-prometheus (so KEDA's Prometheus scaler can
  query `forge_pending`). Mirror the existing `kubernetes/apps/infrastructure/monitoring/agentforge.yaml`.
- `dispatcher-netpol.yaml`: egress = forge (Gitea) + DNS only.

### 2. per-OAuth-account worker Deployments (the scale targets)
One Deployment per trusted OAuth account (the KEDA cap IS v1's per-account semaphore, so one Deployment
per account, roles via config): `af-claude-max1` (planner/reviewer), `af-claude-max2` (implementer),
`af-codex` (cross-reviewer), `af-tester` (litellm). Each (GATED, placeholder digest):
- `agentforge serve`, `AF_EXECUTOR=sandbox`, distinct `AF_WORKER_NAME` (the Deployment name — the role
  lookup + claim owner key; single-replica base × KEDA maxReplicas = the semaphore) via the downward API
  or a fixed env, the sandbox config env (`AF_SANDBOX_NAMESPACE`, `AF_SANDBOX_IMAGE` [gated digest],
  `AF_SANDBOX_SERVICE_ACCOUNT`, `AF_SANDBOX_WORKSPACE_PVC`, `AF_SANDBOX_ORG`/`WORKSPACE`/`POOL`,
  `AF_SANDBOX_BROKER_URLS`, `AF_CAPABILITY_SIGNING_KEY` via ESO, `AF_LEASE_DURATION_S`), `AF_CONFIG_SOURCE`.
- ESO-injected `af-forge-creds` (PATs/HMAC/git-push/litellm/capability signing key) — orchestrator only
  (the sandbox Job gets NONE of these — R-1/R-2). Restricted-ish securityContext (runc, trusted tier;
  it creates Jobs so it has the cross-ns RBAC from `agentforge-sandbox/orchestrator-rbac.yaml`).
- The orchestrator's `AF_SANDBOX_ORG/WORKSPACE/POOL` must match a `agentforge-tenant-map` entry + the VAP
  (Wave B-ii) — tenant-zero/playground/<pool>.

### 3. KEDA ScaledObjects (scale-to-zero)
- `scaledobjects.yaml`: one `ScaledObject` per worker Deployment. `minReplicaCount: 0`,
  `maxReplicaCount: <account.max_parallel>`, a **Prometheus trigger** on
  `sum(forge_pending{role="<the deployment's role(s)>"})` against the in-cluster kube-prometheus, plus a
  **cron trigger** warm-floor (minReplica 1) for interactive roles (planner/reviewer) during business
  hours. Cooldown so a finished burst returns to 0. The claim-lock dedups across replicas (no new code).
- (Optional, noted) a pre-pull DaemonSet to hide the fat-image cold start — deferred, cold-start
  mitigation is a P3 nicety.

### 4. per-Deployment Cilium egress
- `cilium-egress.yaml`: per worker Deployment, an egress CNP: `agentforge` orchestrator → the FORGE
  (Gitea) + OpenBao/ESO (for its ESO sync is the controller's job, so orchestrator egress = forge +
  broker + litellm + kube-apiserver [creates Jobs] + DNS). Each account's worker only needs the broker
  for ITS aud + the forge; the SANDBOX Job's egress is already locked by the Wave B-ii sandbox CNP. Deny
  world/metadata/IPv6.
- ServiceMonitor for the workers' own `/metrics` (the fleet board).

## Critical files
- NEW `kubernetes/apps/infrastructure/agentforge-workers/**` (dispatcher deploy[gated]/svc/servicemonitor/
  netpol; per-account worker deploy[gated] ×4; scaledobjects; cilium-egress; kustomization — Deployments
  UNLISTED/gated).
- NEW `kubernetes/apps/clusters/ai/agentforge-workers.yaml` (Flux Kustomization, dependsOn infra+the
  KEDA/ESO CRDs, wait:false — mirrors agentforge-broker).
- ESO `af-forge-creds` ExternalSecret (if not already present) sourcing the orchestrator creds from
  OpenBao `af/data/tenants/tenant-zero/playground/orchestrator` (the provisioner's key) — verify vs the
  existing agentforge apps ESO.

## Verification
- `kubectl kustomize` builds; all Deployments UNLISTED/gated with placeholder digests; no
  `privilege_hardening` flip.
- ScaledObject: `scaleTargetRef` names each real worker Deployment; `minReplicaCount:0`; `maxReplicaCount`
  == the account's `max_parallel`; the Prometheus query is `sum(forge_pending{role=…})` against the right
  server URL; the cron warm-floor is scoped to interactive roles.
- dispatcher: read-only (no sandbox/broker/capability creds), ServiceMonitor scrapes `forge_pending`,
  netpol egress = forge + DNS only.
- workers: `AF_EXECUTOR=sandbox`, distinct `AF_WORKER_NAME`, the full sandbox env matches the Wave B-ii
  VAP-pinned values + a `agentforge-tenant-map` entry; `af-forge-creds` orchestrator-only; the cross-ns
  Job-create RBAC is the existing `orchestrator-rbac.yaml`.
- Cilium: each worker egress = forge + its broker + litellm + apiserver + DNS; deny world/metadata/IPv6.

## Notes
- On `feat/p2-unlock` (dormant umbrella), NOT a PR to main. Activation (operator): build+pin the worker
  + dispatcher image (the SAME agentforge image, `serve`/`dispatcher` subcommands), list the Deployments,
  seed OpenBao, install KEDA CRDs — then the fleet scales 0→N on forge work.
- codex Phase A on this plan, then Phase B on the rendered manifests (cap 3 each).

<!-- codex-review-status: pending -->
