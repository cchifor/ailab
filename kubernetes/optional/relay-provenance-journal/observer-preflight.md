# Runtime observer preflight (read-only, incomplete by design)

`observer-preflight.py` runs on an operator workstation, outside Talos. It
executes only `kubectl get node`, `kubectl get pod`, `kubectl auth whoami`, and,
when a binary is supplied, `talosctl version`. The tool paths and exact Node,
namespace, and Pod names are explicit arguments. The collector emits a fixed
JSON allowlist: credential identity, Pod and Node UIDs, Pod placement and
Running state, and public runtime versions. It discards all other API fields,
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
