# R-3 — worker autoscaling (tenant-zero orchestrator + af-dispatcher + KEDA scale-to-zero + Cilium egress)

## Context

The plan's deferred P2 compute layer. R-1/R-2/Wave-B built the sandbox boundary + broker + provisioner +
the tenant-zero orchestrator RBAC/PVC/tenant-map/broker for EXACTLY ONE identity: SA
`af-orch-playground-planner` in ns `af-tenant-tenant-zero-playground`, pool `planner`. This tranche adds
the always-on scale ORACLE + KEDA scale-to-zero over that ONE dormant orchestrator, proving the
autoscaling mechanism without inventing per-account provisioning. All on ailab `feat/p2-unlock`, DORMANT/
gated (placeholder `@sha256:0000…` images, unlisted Deployments). The R-1 redesign superseded per-pod
DinD — a worker Deployment is a PURE orchestrator (`agentforge serve`, `AF_EXECUTOR=sandbox`) that creates
ephemeral sandbox Jobs; NO DinD.

**Scope decision (codex Phase A):** the FULL per-OAuth-account fleet (af-claude-max1/max2/codex/tester,
each a distinct orchestrator identity) requires a NEW per-account SA + cross-ns RoleBinding + staging PVC +
tenant-map entry + broker ingress/kid policy — none of which exist. That per-account provisioning is a P3
concern (or a provisioner extension). This tranche is scoped to the SINGLE existing tenant-zero orchestrator
identity + the dispatcher + one ScaledObject; the per-account fan-out is documented as the P3 expansion.

The scale oracle exists in agentforge code: `agentforge dispatcher` exports `forge_pending{account,pool,
role,repo}` on `metrics_port`. KEDA is already installed on `feat/p2-unlock` (a prereq CHECK, not an action).

## Approach — new dir `kubernetes/apps/infrastructure/agentforge-workers/`

### 1. af-dispatcher (the scale oracle) — trusted `agentforge` ns
- `dispatcher-deployment.yaml` (GATED, placeholder digest): `agentforge dispatcher`, replicas 1, baseline
  PSA, exposes prometheus `metrics_port` 9464. Mounts a READ-ONLY forge-PAT ESO Secret ONLY — explicitly
  EXCLUDES `AF_CAPABILITY_*`, sandbox, and broker material (it never creates Jobs or mints capabilities).
  Include a config-source credential ONLY if `AF_CONFIG_SOURCE=control_plane` is actually used (tenant-zero
  is forge-backed → none).
- `dispatcher-service.yaml`: ClusterIP on the metrics port (named port).
- `dispatcher-servicemonitor.yaml`: MIRROR the existing `monitoring/agentforge.yaml` details exactly —
  `namespace: monitoring`, `release: kube-prometheus-stack` label, explicit `namespaceSelector`, the named
  metrics port, and the scrape interval — so kube-prometheus actually scrapes `forge_pending`.
- `dispatcher-netpol.yaml`: egress = the FORGE (Gitea) + DNS only. If Gitea is an external hostname, use
  Cilium FQDN egress; add ingress-allow from kube-prometheus/monitoring for the `/metrics` scrape.

### 2. tenant-zero worker Deployment (the scale target) — ns `af-tenant-tenant-zero-playground`
`worker-deployment.yaml` (GATED, placeholder digest) — runs under the EXISTING orchestrator SA
`af-orch-playground-planner` in the tenant ns (so it inherits the already-bound cross-ns Job/pod/log/lease
RBAC [`orchestrator-rbac.yaml`], the tenant staging PVC, the tenant-map entry, and the broker ingress/kid
policy — NO new RBAC/PVC/SA needed):
- `agentforge serve`, `AF_EXECUTOR=sandbox`; **fixed** `AF_WORKER_NAME` (a configured Deployment name, NOT
  the pod name — a per-pod name would break `roles_for(worker_name)`; shared across KEDA replicas is safe,
  claims are epoch-elected per issue). The full sandbox env matching the Wave B-ii VAP-pinned values:
  `AF_SANDBOX_NAMESPACE=agentforge-sandbox`, `AF_SANDBOX_IMAGE`=<gated digest>, `AF_SANDBOX_SERVICE_ACCOUNT`,
  `AF_SANDBOX_WORKSPACE_PVC=af-sbx-ws-tenant-zero-playground`, `AF_SANDBOX_ORG=tenant-zero`,
  `AF_SANDBOX_WORKSPACE=playground`, `AF_SANDBOX_POOL=planner`, `AF_SANDBOX_BROKER_URLS`, `AF_LEASE_DURATION_S`.
- ESO `af-creds-playground-planner` (the ACTUAL Wave-B-ii-rendered orchestrator credential Secret for this ns — `creds_secret_name`, NOT the stale `af-forge-creds`): the forge PATs/HMAC/git-push token/litellm key from OpenBao
  `af/data/tenants/tenant-zero/playground/orchestrator`, PLUS templating the ACTIVE private
  `AF_CAPABILITY_SIGNING_KEY` **and** `AF_CAPABILITY_KID` (both required by the Settings/broker capability
  contract; the private signing key stays orchestrator-only — NEVER a sandbox Job). Restricted-ish runc
  (trusted tier; it creates Jobs).
- Mounts the tenant staging PVC at the staging root (same as the reaper).

### 3. KEDA ScaledObject (scale-to-zero) — same ns as its target
`scaledobject.yaml`: `scaleTargetRef` = the worker Deployment (SAME namespace as the ScaledObject),
`minReplicaCount: 0`, `maxReplicaCount: <account.max_parallel>` (the hard cap — the dispatcher reports
PENDING work, not in-flight-subtracted, so maxReplicas bounds concurrency; the claim epoch-lock dedups
duplicate claims). Triggers:
- **prometheus**: `serverAddress: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090`;
  `query: sum(forge_pending{account="anthropic/default", pool="planner", role=~"planner|reviewer"})` — MUST
  filter account+pool (the gauge is `{account,pool,role,repo}`; role-only would let another account's
  backlog scale this one), with a role MATCHER for a multi-role worker; `threshold: "1"`;
  `activationThreshold`; `ignoreNullValues: true` + an explicit scrape-missing posture (don't flap to 0 on
  a missing sample). 
- **cron** warm-floor (interactive planner/reviewer, business hours): `desiredReplicas: "1"` (with the
  ScaledObject `minReplicaCount: 0`) — NOT a min-replica.
- cooldownPeriod so a finished burst returns to 0.

### 4. per-worker Cilium egress — `cilium-egress.yaml`
The ORCHESTRATOR process's runtime egress ONLY: the FORGE (Gitea) + the kube-apiserver (entity
`kube-apiserver`, port 6443 — REQUIRED to create/watch Jobs, read pod logs, and CAS the Lease; safe under
the narrow cross-ns RBAC) + DNS. **NO OpenBao/ESO** (ESO sync is controller-side, not the app). **NO direct
broker/litellm** from the orchestrator — the SANDBOX JOBS are the broker/litellm clients (their egress is
already locked by the Wave B-ii sandbox CNP), the orchestrator only stages the capability + creates the Job.
Deny world/metadata/IPv6.

## Critical files
- NEW `kubernetes/apps/infrastructure/agentforge-workers/**` (dispatcher deploy[gated]/svc/servicemonitor/
  netpol; worker deploy[gated] in the tenant ns [reusing the existing af-creds-playground-planner ESO/Secret];
  scaledobject; cilium-egress; kustomization — Deployments UNLISTED/gated).
- NEW `kubernetes/apps/clusters/ai/agentforge-workers.yaml` (Flux Kustomization, `wait:false`, dependsOn
  the sandbox/broker/tenant-map/ESO+KEDA CRDs — mirrors `agentforge-broker.yaml`).
- Verify the existing `af-creds-playground-planner` orchestrator ESO already templates `AF_CAPABILITY_SIGNING_KEY` + `AF_CAPABILITY_KID` (orchestrator-only); if not, add it to that ESO.

## Verification
- `kubectl kustomize` builds; all Deployments UNLISTED/gated with placeholder digests; no
  `privilege_hardening` flip.
- worker: runs as `af-orch-playground-planner` in `af-tenant-tenant-zero-playground` (inherits RBAC/PVC/
  tenant-map/broker); `AF_EXECUTOR=sandbox`, fixed `AF_WORKER_NAME`, the sandbox env == the VAP-pinned
  values; `AF_CAPABILITY_SIGNING_KEY`+`AF_CAPABILITY_KID` orchestrator-only via ESO; staging PVC mounted.
- ScaledObject: `scaleTargetRef` same-ns; `minReplicaCount:0`; `maxReplicaCount==max_parallel`; the query
  filters account+pool+role against the kube-prometheus serverAddress; `ignoreNullValues`+activation set;
  cron warm-floor is `desiredReplicas:"1"`.
- dispatcher: read-only (NO capability/sandbox/broker creds); ServiceMonitor mirrors the existing SM
  (monitoring ns, release label, namespaceSelector, port, interval); netpol egress = forge + DNS.
- Cilium: worker egress = forge + apiserver:6443 + DNS ONLY (no OpenBao/ESO/broker/litellm); metrics
  ingress from monitoring allowed; deny world/metadata/IPv6.

## Notes
- On `feat/p2-unlock` (dormant umbrella), NOT a PR to main. **P3 expansion (documented, not built):** the
  full per-OAuth-account fleet (max1/max2/codex/tester) = one PROVISIONED orchestrator identity per account
  (SA + cross-ns RoleBinding subject + staging PVC + tenant-map entry + broker ingress/kid policy + a
  per-account ScaledObject) — a provisioner/CP concern, not hand-authored here.
- Activation (operator): build+pin the SAME agentforge image (`serve`/`dispatcher`), list the Deployments,
  ensure the KEDA CRDs + kube-prometheus + the tenant OpenBao keys are present, then the orchestrator scales
  0→N on forge work.
- codex Phase A on this plan, then Phase B on the rendered manifests (cap 3 each).

<!-- codex-review-status: finalized -->

<!-- Phase A: codex round 1 (crux CONFIRMED sound; 16 wiring findings) + round 2 (2 residuals: use the ACTUAL provisioned names af-creds-playground-planner + account anthropic/default, not the deferred-fleet af-forge-creds/anthropic-max1) — all accepted; no pushback. Finalized. -->
