# ADR 0025 — The dsh harness holds `cluster-admin`

**Status:** ACCEPTED (2026-09-18), operator decision. Implemented in `kubernetes/apps/apps/dsh/`
(`k8s-admin.yaml`, the `install-kubectl` init container in `deployment.yaml`).
**Relates to:** ADR 0021 (credential tiering — this is a recorded exception to Tier A), 0018 (the
agents this estate runs), 0007 (k8s exposure).

## Context

dsh (https://dsh.chifor.me) is the operator's interactive agent harness: a single-user pod that
executes model-authored tool calls. The operator wants it to operate the ailab cluster directly,
and asked for the implementation to be simple.

Five PRs on 2026-09-17/18 (#770–#772 merged, #773/#774 open) tried to give it a *bounded*
Kubernetes identity (#770 a namespace Role
plus a `ValidatingAdmissionPolicy` that admitted only acceptance-shaped Jobs and NetworkPolicies;
#771/#772 the network path; #773 a kubectl binary; #774 a name-based hole in the policy). The result
did not work: the agent's real acceptance workloads failed two of the policy's three expressions,
the identity could not read the cluster-scoped policy that denied it, the kubectl image reference
in #773 no longer resolved, and #774 was a bypass by construction. Every further bounding step was
another guard to get wrong.

ADR 0021 §5 defines the agent-readable credential tier as one that never gains "anything
cluster-scoped", and the dev-worker role deliberately REMOVED a cluster-wide escalation helper for
the stated reason that an interactive agent can talk its operator into a sudo prompt. This decision
is the opposite call for one workload, made knowingly.

## Decision

- `ServiceAccount dsh/dsh-k8s-admin` is bound to the built-in `cluster-admin` ClusterRole by ONE
  `ClusterRoleBinding`. No custom ClusterRole, no Role, no admission policy of our own.
- The `dsh-k8s-admin-guard` ValidatingAdmissionPolicy and its binding are deleted.
- `kubectl` (pinned version + sha256) is installed into the pod's home volume by an init container
  that is never fatal; the network rules that make the API reachable are unchanged.
- The network policies stay as they are, but their comments now say what they are: a bound on the
  agent's DIRECT connections, not a security boundary — the API is the wider door.

## Consequences

- **Everything the cluster holds is reachable from a pod that runs model-authored code**: every
  Secret in every namespace (SOPS-materialised Secrets, the OpenBao break-glass token, Gitea admin,
  Cloudflare tunnel credentials), Flux itself, `exec` and `port-forward` into any pod, and the
  ability to create workloads anywhere admission allows. The grant is identity-wide: every
  container of the dsh pod (the token is automounted) and any workload that names the
  ServiceAccount.
- **RBAC is not admission.** The `dsh` namespace keeps the `baseline` Pod Security Standard; the
  `agentforge-*` guards keep matching what they match today. cluster-admin makes every verb
  *authorized*; it does not make a privileged Pod in `dsh` *admissible*.
- **Revocation is one object, and revokes future use only.** Delete the ClusterRoleBinding from git,
  let Flux prune it, confirm `kubectl auth can-i --list --as=system:serviceaccount:dsh:dsh-k8s-admin`
  no longer shows `*.* [*]`. It does not undo bindings, tokens or other access created while the
  grant was held; that is an incident-recovery question, not this ADR's.
- **Dev-workers are unchanged.** ADR 0021 Tier A still holds for them; this ADR is the one
  recorded exception, cross-referenced from ADR 0021 §5.

Operations, verification and the degraded-boot fallback: `docs/runbooks/dsh-k8s-admin.md`.
