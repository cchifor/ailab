# Implementation review — p3-tenant-quota

<!-- codex-impl-review-status: pending -->

## Scope
The LAST preflight-independent buildable item of the AgentForge v2 plan (P3 §"per-org quotas"): a
per-tenant **ResourceQuota + LimitRange** bounding tenant-zero/playground's compute. Landed on ailab
`feat/p2-unlock` (dormant umbrella).

- `agentforge-sandbox/tenant-resourcequota.yaml` — operator-owned ResourceQuota (requests/limits
  cpu+mem+ephemeral-storage, pods, PVCs, secrets/configmaps, deployment/service/job counts) + LimitRange
  (default/defaultRequest/max per container) in `af-tenant-tenant-zero-playground`.
- `agentforge-sandbox/tenant-namespace.yaml` (round-1 fix) — the tenant-zero namespace made OPERATOR-owned.
- `agentforge-sandbox/kustomization.yaml` — both listed (ns before the ns-scoped objects).
- `clusters/ai/agentforge-workers.yaml` — `dependsOn: agentforge-sandbox` (ns+quota before the workloads).

## Governance design (why operator-owned, not CP-rendered)
A ResourceQuota bounds a tenant's compute, so the CP must not be able to set its own. It cannot: `ResourceQuota`
/`LimitRange` are ABSENT from the tenant-guard GVK allowlist → the tenants-reconciler SA is rejected if it
tries to create either. The operator sets the bound (this manifest); the KEDA `maxReplicaCount` is the
per-account concurrency cap; this ResourceQuota is the namespace-total backstop.

## Codex Phase B trail
### Round 1 (focused, task bjfu4h1ut) — 1 defect
- **DEFECT (closed):** operator-owned quota in a CP-owned namespace is not fully sound — the
  tenant-reconciler SA (reconciler-rbac.yaml, cluster-wide `namespaces: delete`) could delete+recreate the
  tenant Namespace, transiently dropping the operator ResourceQuota/LimitRange.
  **FIX (bf42fee):** `tenant-namespace.yaml` makes af-tenant-tenant-zero-playground OPERATOR-owned (main
  Flux). tenant-zero is the operator's SHADOW tenant (all its objects are operator-owned) and is absent from
  the CP tenant repo the reconciler manages → that SA never touches the ns → can't delete the quota. Invariant
  documented in the ns header: tenant-zero MUST NOT appear in the CP tenant repo (else dual ownership). Plus
  `agentforge-workers dependsOn agentforge-sandbox` (ordering); PSA mirrors the renderer (baseline/restricted).
  The escape is reframed as a P3-LIVE concern only for CP-managed (self-service) tenants — whose robust fix
  is per-tenant operator-owned ns lifecycle (as here) OR a per-pod-resource VAP on af-tenant-* pods.

### Round 2 (task brcfc8j2r) — VERDICT PENDING
<!-- filled on read -->

## Verification
- `kubectl kustomize agentforge-sandbox` builds (28 docs); exactly one `af-tenant-tenant-zero-playground`
  Namespace object; ResourceQuota + LimitRange present; `agentforge-workers` still builds.
- Dormant: the ns + quota + workloads all activate together at un-gate; landing now is inert (the ns is
  new/empty, the worker Deployment stays gated/unlisted).
