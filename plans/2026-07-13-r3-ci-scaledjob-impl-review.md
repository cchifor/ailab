# Implementation review — r3-ci-scaledjob — round 1
<!-- codex-impl-review-status: finalized -->

**FINALIZED — codex Phase B round 2 = READY TO MERGE onto feat/p2-unlock (dormant scaffolding).** R1 1 blocker+6 important+1 nit -> all fixed -> R2 all CLOSED, no regressions. The 3 activation preflights (forgejo<->gitea compat, docker://<->:host workflows, reg-token single-use) are activation gates, NOT merge blockers.
## Summary
- Dormancy is mostly intact: the ScaledJob is unlisted and paused with placeholder digests, and the canary is unlisted with a sandbox-guard-admissible Job shape.
- The CI VAP is not yet strong enough for the advertised security boundary; it permits projected Secret/serviceAccountToken volume bypasses and does not actually require runner `allowPrivilegeEscalation: false`.
- The KEDA Forgejo trigger is close to the 2.20 documented shape, but activation still has contract gaps: the scaler Secret has no ESO producer, and the runner registers a bare label rather than an explicit docker execution schema.
- CI egress mostly follows the intended destination model, but the policy omits explicit OpenBao/ESO denies and leaves wildcard DNS as an unacknowledged exfil path.

## Findings

### Projected volumes bypass the runner-only token boundary
**Location:** kubernetes/apps/infrastructure/agentforge-ci-runners/ci-guard.yaml:88
**Severity:** blocker
<!-- codex: Validation 5 only restricts direct `secret:` volumes, and validation 6 only blocks mounts named `reg-token` outside `runner`. A pod with exactly `{runner,dind}` can add a `projected` volume containing `serviceAccountToken` or `secret` sources and mount it into `dind` under any other name; it passes the current CEL, bypassing both `automountServiceAccountToken: false` and the “Secret mounted ONLY into runner” invariant. Fix by pinning the exact allowed volume set/types, or at minimum rejecting all `projected.sources[].serviceAccountToken`, `projected.sources[].secret`, direct Secret volumes except `reg-token`, and any dind mount except the intended non-secret volumes. -->

### Runner may still get privilege escalation
**Location:** kubernetes/apps/infrastructure/agentforge-ci-runners/ci-guard.yaml:109
**Severity:** important
<!-- codex: The runner check accepts an omitted `securityContext.allowPrivilegeEscalation`; in Kubernetes that defaults effectively to allowed, and PSA is `privileged` in this namespace. The policy therefore does not enforce the claimed “runner no-allowPrivilegeEscalation” property. Require `has(c.securityContext.allowPrivilegeEscalation) && c.securityContext.allowPrivilegeEscalation == false`, and consider pinning the full runner securityContext (`drop: ["ALL"]`, no added caps, RuntimeDefault seccomp) like the sandbox guard does. -->

### CI pod storage is not bounded by admission
**Location:** kubernetes/apps/infrastructure/agentforge-ci-runners/ci-guard.yaml:121
**Severity:** important
<!-- codex: Validation 9 bounds only CPU and memory. Combined with validation 5 allowing arbitrary non-Secret volumes, a conforming two-container CI pod can add unbounded `emptyDir`, PVC, CSI, configMap, or downwardAPI volumes and omit ephemeral-storage requests/limits. That weakens the node-DoS backstop the comments claim. Fix by requiring ephemeral-storage requests/limits and pinning the exact `emptyDir` volumes with explicit `sizeLimit`/medium constraints. -->

### Scaler token Secret has no ESO producer
**Location:** kubernetes/apps/infrastructure/agentforge-ci-runners/scaledjob.yaml:225
**Severity:** important
<!-- codex: `TriggerAuthentication` references `agentforge-ci-scaler-token`, and comments say it is ESO-synced from `af/data/operator/ci/scaler-token`, but the only ExternalSecret in this directory creates `ci-runner-registration`. Once the ScaledJob is ungated, KEDA auth will fail unless a manual Secret exists outside GitOps. Add a separate operator ExternalSecret for `operator/ci/scaler-token`, keeping it disjoint from the registration token. -->

### Runner label does not encode the docker execution contract
**Location:** kubernetes/apps/infrastructure/agentforge-ci-runners/scaledjob.yaml:119
**Severity:** important
<!-- codex: The existing ansible runner contract registers `self-hosted-hv:host`, and KEDA 2.20 Forgejo docs model runner registration labels as label-plus-execution-schema while the scaler matches label names: https://keda.sh/docs/2.20/scalers/forgejo/. This manifest registers only `self-hosted-hv`, so the dormant shape does not actually encode the reviewed docker/DinD execution contract. Before activation, register an explicit `self-hosted-hv:docker://<pinned image>` or generated runner config/one-job equivalent, with the scaler metadata matching the label name. -->

### Forgejo threshold knobs appear undocumented/no-op
**Location:** kubernetes/apps/infrastructure/agentforge-ci-runners/scaledjob.yaml:190
**Severity:** nit
<!-- codex: KEDA 2.20 Forgejo trigger docs list `name`, `address`, `labels`, one scope selector, and auth `token`; the scaler target defaults to one pending job. `threshold` and `activationThreshold` are not part of the documented Forgejo trigger metadata, so the comments overstate that these fields control behavior. Remove them or replace with supported generic target metadata only after verifying the exact KEDA 2.20 scaler parser. -->

### OpenBao and ESO are not explicitly denied for CI
**Location:** kubernetes/apps/infrastructure/agentforge-ci-runners/cilium.yaml:87
**Severity:** important
<!-- codex: The header and plan claim explicit OpenBao/ESO deny, but `egressDeny` only covers kube-apiserver/world/host/remote-node and CIDRs. Current default-deny plus lack of allow keeps those endpoints unreachable, but the explicit deny invariant is missing and would not survive a future broader endpoint allow. Add `egressDeny.toEndpoints` entries for `openbao` and `external-secrets`, matching the broker policy pattern. -->

### Wildcard DNS is an unacknowledged exfil path
**Location:** kubernetes/apps/infrastructure/agentforge-ci-runners/cilium.yaml:49
**Severity:** important
<!-- codex: The policy allows `matchPattern: "*"` DNS to kube-dns. Even with direct `world` egress denied, arbitrary CI can encode data into external DNS query names and let CoreDNS recurse upstream. The residual section names the package proxy as the exfil path but not DNS. Restrict DNS L7 to the required cluster service names, or explicitly name and monitor DNS as an accepted residual. -->

## Diff stat
 .../apps/clusters/ai/agentforge-ci-runners.yaml    |  38 ++++
 .../agentforge-broker/ciliumnetworkpolicy.yaml     |  19 +-
 .../agentforge-ci-runners/ci-guard.yaml            | 153 ++++++++++++++
 .../agentforge-ci-runners/cilium.yaml              | 106 ++++++++++
 .../externalsecret-regtoken.yaml                   |  84 ++++++++
 .../agentforge-ci-runners/kustomization.yaml       |  23 +++
 .../agentforge-ci-runners/namespace.yaml           |  50 +++++
 .../agentforge-ci-runners/scaledjob.yaml           | 228 +++++++++++++++++++++
 .../agentforge-ci-runners/serviceaccount.yaml      |  16 ++
 .../agentforge-sandbox/egress-canary.yaml          | 121 +++++++++++
 10 files changed, 834 insertions(+), 4 deletions(-)
---
## Round-1 addressed (all 8 — commits 8bd4378..8616409)
- [FIXED blocker] projected/secret volume bypass: ci-guard now an explicit VOLUME allowlist (!has(projected)
  + exactly {reg-token Secret, data/sentinel/docker-storage emptyDir w/ sizeLimit}) + per-container MOUNT
  allowlist (runner⊆{reg-token,data,sentinel} must-mount-reg-token; dind⊆{sentinel,docker-storage}) — no
  secret/projected volume can reach dind.
- [FIXED] runner full restricted securityContext (runAsNonRoot, allowPrivilegeEscalation present&&false,
  drop ALL, seccomp RuntimeDefault); manifest conformed (runAsUser 1000, reg-token 0440).
- [FIXED] ephemeral-storage requests+limits on both containers (≤64Gi) + every emptyDir sizeLimit-bounded.
- [FIXED] scaler-token operator ExternalSecret producer (disjoint operator/ci/scaler-token).
- [FIXED] runner label encodes self-hosted-hv:docker://<pinned image> (PREFLIGHT #2).
- [FIXED nit] dropped no-op threshold/activationThreshold from the forgejo trigger (kept in prometheus fallback).
- [FIXED] egressDeny.toEndpoints for openbao + external-secrets.
- [FIXED] DNS L7 restricted to exact service matchName (no wildcard); external-DNS residual documented.
Dormancy preserved: ScaledJob unlisted + paused, placeholder digests, CI VAP/PSA scoped to agentforge-ci.
