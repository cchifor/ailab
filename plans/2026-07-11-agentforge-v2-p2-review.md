# Implementation review — agentforge-v2 P2 (the unlock) — round 1

<!-- codex-p2-review-status: pending -->

## Summary

- The sandbox boundary is not sound: untrusted code can poison the shared checkout’s Git metadata and regain execution inside the credentialed orchestrator.
- `SandboxExecutor` does not create a separate sandbox pod; Kata isolates the combined orchestrator/DinD pod from the node, not untrusted execution from orchestrator credentials.
- Durable inference credentials are passed directly to the agent, with neither the required broker nor outbound redaction.
- P2 provisioning is dead on arrival: PSA, tenant admission, reconciler RBAC, ESO API versions, and rendered GVKs do not agree.
- Egress is either nonfunctional (`--network none` for agents) or overly broad; the retained Kubernetes NetworkPolicy defeats the Cilium FQDN allowlist.
- OpenBao tenant roles and policies are not provisioned, and the documented bootstrap identity does not match the rendered SecretStore identity.
- The operator/Kata-node scaffolding is otherwise coherent: OpenBao `wait:false`, ESO dependency ordering, current chart lines, kro OCI source, agent-only image, runtime handlers, and nested-virtualization setup are reasonable.

## Findings

### The shared checkout is a direct credential escape into the orchestrator
**Location:** agentforge/src/agentforge/app/handlers/roles.py:125, agentforge/src/agentforge/app/handlers/roles.py:156, agentforge/src/agentforge/app/workspace.py:136
**Severity:** blocker
<!-- codex: The agent can modify `.git` on the read-write shared checkout, after which the orchestrator executes `git status`, `commit`, and PAT-bearing `push` there; a malicious hook or repo-local Git helper therefore executes as the orchestrator and can read `/proc/1/environ` or the push credential from its parent argv. Export only a validated patch/content set, apply it to a fresh trusted checkout containing no untrusted Git metadata, validate paths/links/ownership, and perform commit/push exclusively from that checkout. -->

### SandboxExecutor does not establish a pod boundary
**Location:** agentforge/src/agentforge/adapters/exec/sandbox.py:63, agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:323
**Severity:** blocker
<!-- codex: The executor calls `docker run` against localhost DinD while the renderer places DinD and the credentialed orchestrator in the same Kata pod, so the orchestrator boundary is still nested-container isolation and any guest/container escape lands beside its credentials. Implement the planned separate ephemeral Kata pod with its own tokenless SA, model-only policy, no forge/OpenBao/CP access, and admission-pinned pod shape. -->

### Durable inference credentials remain readable and exfiltratable
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:416, agentforge/src/agentforge/adapters/exec/sandbox.py:137
**Severity:** blocker
<!-- codex: The ExternalSecret extracts forge and inference material into one orchestrator Secret, and SandboxExecutor embeds the durable OAuth value in the agent container’s Docker argv/environment; the agent can then copy it into the shared checkout, output, or a later `test_cmd`, with no broker or outbound redactor present. Split the secrets, implement the required short-lived credential broker over a Unix socket, and scan/redact every diff, comment, log, event, and error before forge publication. -->

### P2 has no secure, functional agent-egress configuration
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:240, agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:452, agentforge-platform/src/agentforge_platform/settings.py:117
**Severity:** blocker
<!-- codex: The retained Kubernetes NetworkPolicy permits TCP 443 and 8080 to any destination, and policy rules are additive, so it defeats the Cilium destination allowlist; meanwhile the default Docker network is `none`, preventing the agent from reaching its model at all, while switching it to `bridge` gives it the whole pod’s forge/OpenBao/CP egress. Remove the broad P1 policy in P2 and enforce distinct orchestrator and model-only policies on separate pods, including explicit DNS-proxy rules for FQDN enforcement. -->

### PSA and tenant admission both reject the privileged DinD deployment
**Location:** ailab/kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:52, ailab/kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:73, agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:173
**Severity:** blocker
<!-- codex: The renderer creates a baseline namespace without the trusted sandbox label, tenant-guard forbids the CP from adding that label, and no PSA runtimeClass exemption exists, so both validation 4 and baseline PSA reject `privileged:true`; tenant-guard also still lacks the promised digest/SA/runtimeClass/volume/host-field pod-shape pin. Add a trusted operator-created sandbox namespace or label, `PodSecurityConfiguration.exemptions.runtimeClasses: ["kata"]` in the API-server admission configuration, and a P2 admission policy that pins the complete sandbox shape before granting the exemption. -->

### Rendered P2 GVKs cannot pass discovery, RBAC, or admission
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:45, ailab/kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:36, ailab/kubernetes/apps/agentforge-tenants-bootstrap/reconciler-rbac.yaml:25, ailab/kubernetes/apps/infrastructure/security/external-secrets/helmrelease.yaml:20
**Severity:** blocker
<!-- codex: The CP emits ESO `v1beta1` resources and a KEDA ScaledObject, but tenant-guard and the reconciler ClusterRole allow neither API group; additionally ESO 2.7.0 does not serve `v1beta1` by default. Emit `external-secrets.io/v1`, extend both RBAC and admission for the exact ESO/KEDA resources, and add field validations pinning SecretStore provider/role, ExternalSecret path/target, and ScaledObject target/query. -->

### OpenBao tenant authentication is neither provisioned nor aligned
**Location:** ailab/kubernetes/apps/infrastructure/security/openbao/helmrelease.yaml:14, agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:391, agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:400
**Severity:** blocker
<!-- codex: No code or manifest creates the computed per-workspace role and KV-v2 policy, while the bootstrap instructions describe binding a generic role to the ESO controller SA rather than the tenant SA referenced by SecretStore; the renderer also selects HTTPS although the release supplies no TLS listener or CA configuration. Add a trusted provisioning path that creates a role bound to the exact namespace+SA and an `af/data/<org>/<workspace>/*` read policy, and either configure verified OpenBao TLS or use the actual internal HTTP endpoint. -->

### KEDA lacks its metric source and safe multi-replica identity
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:430, agentforge/src/agentforge/main.py:343, agentforge-platform/src/agentforge_platform/settings.py:95
**Severity:** blocker
<!-- codex: The branch implements dispatcher code but renders no always-on dispatcher Deployment, Service, or Prometheus scrape target, so a min-zero ScaledObject receives no metric and never starts workers; its query also omits account/pool labels. Render and scrape the dispatcher, then separate stable config identity from a downward-API pod claim ID and implement the epoch-safe account lease before lifting the current hard replica cap of one. -->

### The inner agent container has no writable or populated home
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:335, agentforge/src/agentforge/adapters/exec/sandbox.py:123
**Severity:** important
<!-- codex: HOME/CODEX_HOME is writable only in the orchestrator container, while the inner Docker container receives a read-only root filesystem, `/tmp`, and the jobs bind mount; it therefore has neither writable CLI state nor seeded Codex authentication. Give the agent an isolated writable home containing only broker configuration/socket material, never the orchestrator home or durable credentials. -->

## Verdict

P2 is not sound to proceed to PR/merge or rollout; the blocker findings must be fixed and proven by admission, credential-exfiltration, egress, and live Kata boundary tests before the v1.1 flip.