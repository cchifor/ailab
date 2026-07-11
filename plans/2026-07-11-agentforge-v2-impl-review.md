# Implementation review — agentforge-v2 — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Org selection, membership checks, RLS scoping, workspace-bearer authorization, and OAuth-token scrubbing are generally well designed.
- The public session configuration fails open: a known signing key can be used, allowing forged principals and cross-org access.
- Provisioning neither restricts compute to tenant-zero nor pins the workload image, and its generated Deployment lacks the configuration and credentials required to start a worker.
- Workspace configuration accepts policy-bearing documents without binding repositories, engines, or merge behavior to server-owned workspace policy.
- Monitoring hardening is process-local despite a two-replica deployment; the SPA and repository bootstrap are also not deployable P1 implementations.
- Test execution was unavailable under the read-only sandbox; static inspection also found no integration proof of the required Gitea and admission boundaries.

## Findings

### Public default permits forged authenticated sessions
**Location:** agentforge-platform/src/agentforge_platform/settings.py:49, agentforge-platform/src/agentforge_platform/main.py:114, agentforge-platform/src/agentforge_platform/main.py:120, agentforge-platform/deploy/deployment.yaml:38, agentforge-platform/src/agentforge_platform/api/auth.py:107
**Severity:** blocker
<!-- codex: The app accepts a publicly known session-signing key, the Secret reference is optional, the cookie lacks `Secure`, and SessionAuthenticator trusts the signed principal and memberships; an omitted AFP_SESSION_SECRET therefore lets an attacker mint an owner session for any known org. Make the secret required, reject weak/placeholding values at startup, make the Secret reference non-optional, and set `https_only=True` outside explicit local development. -->

### Any operator can provision an arbitrary image on the hub
**Location:** agentforge-platform/src/agentforge_platform/api/workspaces.py:46, agentforge-platform/src/agentforge_platform/api/workspaces.py:78, agentforge-platform/src/agentforge_platform/api/workspaces.py:98, agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:152, agentforge-platform/src/agentforge_platform/settings.py:63
**Severity:** blocker
<!-- codex: Workspace creation accepts an unvalidated caller-selected image and up to 50 replicas for every org, while P1 compute must be tenant-zero-only and use a server-owned digest-pinned image. Reject provisioning unless `ctx.org_slug` is the configured bootstrap org and remove `image` from the request, selecting and validating the immutable orchestrator digest server-side. -->

### Rendered workers cannot start or fetch control-plane configuration
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:53, agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:116, agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:149, agentforge/src/agentforge/infra/settings.py:31, agentforge/src/agentforge/infra/settings.py:34, agentforge/src/agentforge/infra/settings.py:50, agentforge/src/agentforge/main.py:128, agentforge/deploy/orchestrator.Dockerfile:4
**Severity:** blocker
<!-- codex: The manifest supplies only unused AF_WORKER_POOL/AF_ROLES values: it omits required AF_WORKER_NAME and AF_GITEA_URL, bot credentials, AF_CONFIG_SOURCE, the full config endpoint, and the workspace bearer, while `config_bearer_secret` is dead; the selected orchestrator image also deliberately contains no agent CLI although P1 still uses LocalExecutor. Render the complete environment and Secret references and provide a constrained P1 execution arrangement capable of the required shadow 1→2 transition. -->

### Workspace policy is accepted as caller-supplied configuration
**Location:** agentforge-platform/src/agentforge_platform/api/workspaces.py:154, agentforge-platform/src/agentforge_platform/api/workspaces.py:164, agentforge-platform/src/agentforge_platform/domain/assembly.py:26, agentforge-platform/src/agentforge_platform/domain/config_schema.py:42
**Severity:** blocker
<!-- codex: `PUT /config` performs schema validation only, so an operator can configure repositories unrelated to the workspace, enable `auto_merge`, and select unrestricted engines and worker identities; this violates the server-side playground/merge-disabled boundary. Assemble configuration from server-owned workspace and pool policy, enforcing the registered repository, tenant-zero engine policy, merge-disabled shadow settings, and worker/pool identity before persistence. -->

### Degraded configuration does not stop new claims
**Location:** agentforge/src/agentforge/adapters/config/control_plane.py:101, agentforge/src/agentforge/infra/api.py:113, agentforge/src/agentforge/infra/api.py:200, agentforge/src/agentforge/app/orchestrator.py:142, agentforge/src/agentforge/app/orchestrator.py:173
**Severity:** important
<!-- codex: A failed refresh sets `degraded`, but that state affects only `/readyz`; the reconcile and webhook queues continue reaching `ClaimService.acquire`, contrary to the required “drain but make no new claims” semantics. Pass an admission predicate into the orchestrator and check config-source degradation immediately before acquiring a claim while allowing already-held work to drain. -->

### Ingest hardening is process-local and loses retryable events
**Location:** agentforge-platform/deploy/deployment.yaml:9, agentforge-platform/src/agentforge_platform/api/readmodel.py:86, agentforge-platform/src/agentforge_platform/api/readmodel.py:133, agentforge-platform/src/agentforge_platform/api/ingest.py:108, agentforge-platform/src/agentforge_platform/api/ingest.py:113
**Severity:** important
<!-- codex: Two replicas maintain independent rate limits, idempotency sets, dashboards, and SSE hubs, so duplicate events can be accepted twice, limits can be bypassed, and clients see only one replica’s state; additionally, an idempotency key is recorded before rate limiting and application, permanently suppressing retries after rejection or failure. Put idempotency/rate/read-model state in a shared transactional store with a uniqueness constraint, or run one replica until that exists, and record the key atomically with successful application. -->

### Repository bootstrap is only a success-reporting stub
**Location:** agentforge-platform/src/agentforge_platform/api/workspaces.py:174
**Severity:** important
<!-- codex: The P1 endpoint merely flips `bootstrapped=True` and explicitly defers labels, branch protection, and webhooks to P2, although the finalized P1 slice requires reuse of v1 bootstrap behavior for the shadow workflow. Invoke a real scoped bootstrap adapter and set the flag only after successful completion. -->

### The Vue application is not present in the deployed control plane
**Location:** agentforge-platform/Dockerfile:7, agentforge-platform/Dockerfile:13, agentforge-platform/src/agentforge_platform/main.py:123, agentforge-platform/deploy/service.yaml:1
**Severity:** important
<!-- codex: The image neither builds nor copies `webapp`, FastAPI mounts no static assets, and the only Service targets the API, leaving the required three-section SPA unreachable in this deployment. Add a webapp build stage and SPA/static routing or a separately deployed frontend with explicit ingress routing. -->

### Required GitOps boundary negatives are not exercised
**Location:** agentforge-platform/tests/conftest.py:136, agentforge-platform/tests/unit/test_gitops_renderer.py:60, agentforge-platform/src/agentforge_platform/adapters/gitops/gitea.py:26
**Severity:** important
<!-- codex: Workspace tests replace Gitea with a recorder and renderer tests cover only in-process GVK checks; nothing proves the production bot token is denied against `cchifor/ailab` or that admission rejects malicious tenant commits. Add integration tests using the deployed bot identity and admission path for outside-repository writes, ClusterRole/privileged manifests, and source-redirection attempts. -->

### Codex’s executor seam assumes a shared orchestrator `/tmp`
**Location:** agentforge/src/agentforge/adapters/runners/codex.py:51, agentforge/src/agentforge/adapters/runners/codex.py:68, agentforge/src/agentforge/adapters/runners/codex.py:108
**Severity:** important
<!-- codex: Codex writes its final response to an orchestrator-local temporary path passed through ExecSpec and then reads that local file, so a future SandboxExecutor cannot produce the file from a separate pod. Place the output beneath the shared job checkout with validated ownership/path handling, or return the output artifact through the Executor result contract. -->

## Verdict

The P1 slice has blockers to fix before it is sound to proceed.