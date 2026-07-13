# Implementation review — p3-tenant-quota

<!-- codex-impl-review-status: finalized -->

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

### Round 2 (task brcfc8j2r) — 1 residual
- Ordering + PSA confirmed correct (no cycle; `agentforge-workers dependsOn agentforge-sandbox`).
- **RESIDUAL (closed):** the closure rested on a COMMENT-ONLY invariant — tenant-guard still allowed the
  reconciler to create/update/delete any af-tenant-* object incl. tenant-zero. If the CP tenant repo ever
  rendered tenant-zero, dual ownership would return.
  **FIX (this commit):** enforce the reservation in admission, not a comment —
  (a) `tenant-guard.yaml` validation **(16)**: on CREATE/UPDATE the reconciler SA is denied when the object
      IS the tenant-zero Namespace or lives in it;
  (b) NEW `tenant-reserved-guard.yaml`: a companion VAP+binding matching **DELETE** (all kinds/scopes) for
      the same SA, denying `request.name`/`request.namespace == af-tenant-tenant-zero-playground` (the main
      VAP matches only CREATE/UPDATE — its validations deref `object`, null on DELETE — so DELETE needed a
      separate policy). Together they make tenant-zero a name the CP path cannot create, update, or delete.

### Round 3 (task byx95cmjm, the review cap) — CONFIRMED
- **CONFIRMED closed + safe.** CREATE/UPDATE/DELETE are structurally denied for the tenant reconciler SA;
  the DELETE-target CEL (`request.name`/`request.namespace`) is correct; other (real) tenants and the
  operator's main-Flux identity are unaffected. Sole note: subresources aren't matched by `resources: ["*"]`,
  but this SA has NO subresource RBAC → no effective gap. No further rounds.

**FINALIZED — the LAST preflight-independent buildable item of the AgentForge v2 plan is complete.**

## Verification
- `kubectl kustomize agentforge-sandbox` builds (28 docs); exactly one `af-tenant-tenant-zero-playground`
  Namespace object; ResourceQuota + LimitRange present; `agentforge-workers` still builds.
- Dormant: the ns + quota + workloads all activate together at un-gate; landing now is inert (the ns is
  new/empty, the worker Deployment stays gated/unlisted).
