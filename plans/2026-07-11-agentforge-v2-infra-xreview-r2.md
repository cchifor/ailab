# Cross-review — agentforge-v2 P1 — round 2

<!-- codex-xreview-status: pending -->

## Summary

- The host-escape blocker is resolved: create/update admission pins tenant namespaces to baseline/restricted, forbids the sandbox label and `nodeName`, and fails closed on CEL errors (`ailab/kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml:18`, `:73`, `:82`, `:89`). The rendered worker complies with baseline PSA; container root remains permitted but the reported host-access mechanisms are prohibited ([Kubernetes Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/)).
- Worker/CP networking is resolved: rendered namespaces carry the ingress selector label, and Service, container, cloudflared, internal URL, config endpoint, and worker egress consistently use TCP 8080 (`agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:97`, `:172`, `:189`; `ailab/kubernetes/apps/apps/agentforge/serviceaccount-service.yaml:22`; `ailab/kubernetes/apps/apps/agentforge/networkpolicy.yaml:33`; `ailab/kubernetes/apps/apps/edge/cloudflared.yaml:88`). DNS and HTTPS egress remain allowed.
- Agent-pool placement is resolved: the rendered selector and toleration exactly match `ailab.io/agent-pool=true` and `dedicated=agent:NoSchedule` (`agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:213`; `ailab/kubernetes/infra/agent-nodes/machine-config/worker.yaml.tftpl:5`).
- The worker-image gate rejects the placeholder, but its regex does not require complete-string consumption and still admits one malformed image form.

## Findings

### Digest validation accepts a trailing newline
**Location:** agentforge-platform/src/agentforge_platform/adapters/gitops/renderer.py:27; agentforge-platform/src/agentforge_platform/api/workspaces.py:116
**Severity:** important
<!-- codex: Because `$` can match immediately before a final newline and `is_digest_pinned()` uses `match()`, `repo@sha256:<64hex>\n` passes the gate and is committed as an invalid worker image. Use `fullmatch()` or `\Z`, and add a trailing-newline regression case that must return 503. -->

## Verdict

Not safe to merge until worker-image validation consumes the entire setting value.

---
**Round-2 resolution:** the single round-2 finding (digest regex trailing-newline) was fixed via re.fullmatch + a regression test (platform). Cross-review ALIGNED.
