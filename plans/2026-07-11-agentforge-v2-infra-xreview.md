# Cross-review — agentforge-v2 P1 pre-merge (ailab infra + platform RLS/OIDC/CI)

<!-- codex-xreview-status: pending -->

## Summary

- Not safe to merge: the tenant GitOps path still permits node-level escape through attacker-controlled Deployment pod fields and namespace labels.
- The CP Flux nudge is correctly limited to `agentforge-tenants`; the whole-spec CEL lock is valid and fails closed.
- RLS FORCE covers all ten policy-bearing tables, with `afp_app` NOBYPASSRLS and `afp_admin` BYPASSRLS. OIDC callback/groups handling and the CI DB tier are sound.
- The Talos workers correctly reuse the existing cluster secrets and carry the intended label, taint, VIP, and provider versions, but rendered workers do not target that pool.
- The CP Deployment wedge split and core AFP env contract are correct, but worker networking and image configuration prevent the P1 shadow worker from running.

## Findings

### Tenant manifests can still escape onto the host
**Location:** ailab/kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:52; ailab/kubernetes/apps/agentforge-tenants-bootstrap/reconciler-rbac.yaml:25
**Severity:** blocker
<!-- codex: The reconciler can create/update Namespaces and Deployments, but the Deployment check only rejects `securityContext.privileged: true`; it permits hostPath, hostNetwork/hostPID/hostIPC, host ports, root containers, and nodeName, while validation 5 leaves namespace labels unrestricted. A compromised CP can therefore mount a node filesystem directly or label its own namespace `ailab.io/agentforge-sandbox=true` to bypass even the privileged check. For P1, pin tenant namespaces to at least Pod Security baseline and prevent weakening/self-assigning the sandbox label, or add equivalent CEL prohibitions for all host-access fields. -->

### Worker-to-control-plane traffic is denied in both directions
**Location:** ailab/kubernetes/apps/apps/agentforge/networkpolicy.yaml:22; agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:168; ailab/kubernetes/apps/apps/agentforge/serviceaccount-service.yaml:22
**Severity:** important
<!-- codex: The CP ingress policy admits only cloudflared, excluding tenant workers, while rendered worker egress permits TCP 8080 although the configured in-cluster URL uses Service port 80. Consequently P1 workers cannot fetch `/api/v1/.../config` or submit ingest events. Admit authenticated tenant-worker traffic to the CP and change the rendered egress port to 80. -->

### Rendered workers do not target the dedicated agent pool
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:184; ailab/kubernetes/infra/agent-nodes/machine-config/worker.yaml.tftpl:5
**Severity:** important
<!-- codex: The worker pod template has neither `nodeSelector: ailab.io/agent-pool=true` nor a `dedicated=agent:NoSchedule` toleration. It therefore cannot schedule on the new pool and may instead run on the schedulable control-plane nodes. Add the selector and matching toleration to the server-owned renderer and regression-test both fields. -->

### Provisioning emits the placeholder worker digest
**Location:** agentforge-platform/src/agentforge_platform/settings.py:90; agentforge-platform/src/agentforge_platform/api/workspaces.py:113; ailab/kubernetes/apps/apps/agentforge/deployment.yaml:57
**Severity:** important
<!-- codex: `worker_image` remains `@sha256:REPLACE_ME`, provisioning copies it directly into the tenant Deployment, and the ailab CP environment never supplies `AFP_WORKER_IMAGE`; the resulting worker cannot pull. Make this setting mandatory with a real 64-hex digest validation and set it from the released P1 worker digest before enabling provisioning. -->

## Verdict

merge-after-fixes (tenant pod/namespace admission, worker-to-CP networking, agent-pool placement, and a real AFP_WORKER_IMAGE digest).