# Runtime observer preflight (read-only, incomplete by design)

`observer-preflight.py` runs on an operator workstation, outside Talos. It
executes only `kubectl get node` and `kubectl get pod` with an explicit
kubeconfig, context, and request timeout. The tool path and exact Node,
namespace, and Pod names are explicit arguments. The collector emits a fixed
JSON allowlist: Pod and Node UIDs, Pod placement and running container state,
and public runtime versions from Node status. Credential identity remains
unknown. It discards all other API fields,
including annotations, container environment, command lines, and tokens.

The report always sets `readyForObserver: false`. Talos has no host shell. An
outside-node API read cannot prove the CRI socket path or instance, verbose
inspect PID schema, cgroup driver and namespace, host PID/proc and ptrace
access, init time namespace, kubelet checkpoint feature and `nodes/checkpoint`
RBAC, or signing key and off-node ledger ownership. Those fields remain
`unknown`. Do not probe checkpoint by POST or request `nodes/proxy` for this
preflight. An operator must supply a separately reviewed, nonsecret host
diagnostic vehicle before any observer backend or key mount is enabled.

This file and the collector are not referenced by the optional Kustomization
and perform no Kubernetes write. The presence source remains at zero replicas.
