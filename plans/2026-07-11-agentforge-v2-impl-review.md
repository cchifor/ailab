# Implementation review — agentforge-v2 — round 2

<!-- codex-impl-review-status: pending -->

## Summary

- Tenant-zero commit gating, server-owned image/replica selection, and the RLS record/compute split are sound.
- Repo and auto-merge policy binding, degraded claim admission, single-replica ingest deployment, SPA route ordering, and bootstrap flag timing are resolved.
- Session validation still permits forgeable secrets, and rendered workers remain unable to fetch config or resolve roles.
- Ingest and GitOps commits retain atomicity/fail-closed gaps; bootstrap uses the wrong credential scope.
- Codex output is not consistently placed beneath the shared job root.

## Findings

### Weak or development-mode session secrets remain servable
**Location:** agentforge-platform/src/agentforge_platform/main.py:119
**Severity:** blocker
<!-- codex: Any repeated or known value of 16 characters passes, and AFP_DEV_MODE=true permits even an empty key without proving the app is local, so a publicly served deployment can still accept forgeable sessions. Require a generated secret with at least 32 bytes of validated key material and remove or strictly localize the weak-secret development escape. -->

### NetworkPolicy blocks the control-plane config endpoint
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:151
**Severity:** blocker
<!-- codex: The ConfigMap points workers at HTTP port 8080, but egress permits only TCP 443 and does not permit DNS, so a fresh worker cannot resolve or fetch config and has no last-good fallback. Allow DNS plus the control-plane Service on port 8080 while retaining the intended forge/model egress. -->

### Pod identity cannot match the fetched worker configuration
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:175
**Severity:** blocker
<!-- codex: AF_WORKER_NAME is the generated pod name, while the worker selects roles with cfg.roles_for(settings.worker_name) and PUT /config cannot predict ReplicaSet pod suffixes, so provisioned pods start with no roles. Introduce a stable pool/config identity for role lookup while retaining a separate unique worker identity for claims, or generate stable pod identities and matching config entries. -->

### Ingest idempotency remains racy and partially applied
**Location:** agentforge-platform/src/agentforge_platform/api/ingest.py:108
**Severity:** important
<!-- codex: For transition/escalation events, apply occurs before an awaited audit write and mark_applied, allowing concurrent requests with the same key to both apply; an audit failure also leaves an emitted event unmarked and retryable, duplicating feed/SSE effects. Serialize or reserve each workspace/key before effects, roll the reservation back on failure, and commit the applied state only after all effects succeed. -->

### Bootstrap uses the tenant-repository commit token
**Location:** agentforge-platform/src/agentforge_platform/main.py:224
**Severity:** important
<!-- codex: GiteaLabelBootstrapper is constructed with tenants_bot_token, whose required scope is only cchifor/agentforge-tenants, so a correctly restricted CP bot cannot create labels on workspace repositories; broadening it would violate the GitOps boundary. Use a distinct workspace-scoped bootstrap credential/adapter and leave bootstrap unconfigured with 501 when that credential is absent. -->

### Committer validation is neither fully normalized nor preflighted
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/gitea.py:26
**Severity:** important
<!-- codex: safe_content_path rejects only literal `..` components, accepting encoded traversal such as `%2e%2e`, and commit validates files lazily so an earlier safe file can trigger HTTP calls before a later unsafe name fails. Normalize and reject encoded/backslash/dot components, validate the complete batch and fixed repository target first, then perform any HTTP request. -->

### Codex gate output still misses the shared job root
**Location:** agentforge/src/agentforge/app/gates.py:174
**Severity:** important
<!-- codex: The primary Codex alignment-gate path supplies cwd=Path("."), so the new scratch directory lands under the orchestrator working directory rather than AF_JOBS_ROOT and will not be visible to a separate executor pod. Allocate a validated per-job directory beneath the shared jobs root and pass it to every Codex AgentJob; retain the existing credential-directory separation. -->

## Verdict

The P1 slice is not yet sound to proceed; the three blockers and four important residuals above must be fixed.