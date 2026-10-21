# Runbook — the Workbricks cell `c1`: what the operator owns

Workbricks deploys one pooled **cell** (`c1`) on this cluster beside the platform, additively. Its
charts contain no cluster-scoped objects. Everything cluster-scoped, and every node, vault, identity,
DNS and GitOps change, is requested by the Workbricks team in their `operator-requests.md` (ids
`OR-n`) and lives in this repo. This page lists what exists and how to check it.

| Request | What | Where |
| --- | --- | --- |
| OR-1, OR-2 | Sandbox runtime labels + user namespaces on the kata/gVisor nodes | `kubernetes/infra/agent-nodes/talos.tf`, `docs/runbooks/agent-nodes.md` § Sandbox runtime nodes |
| OR-1 | RuntimeClasses `kata`/`gvisor` select `ailab.io/runtime-*` | `kubernetes/apps/infrastructure/agentforge-runtimeclasses/` |
| OR-4 | `system:auth-delegator` for `workbricks-c1/gatekeeper` (TokenReview) | `infrastructure/workbricks-c1-operator/gatekeeper-auth-delegator.yaml` |
| OR-5 | Namespaces `workbricks-c1`, `-data`, `-sandbox` (PSA `restricted`, prune disabled) + `compute` quotas | `infrastructure/workbricks-c1-operator/` |
| OR-6 | PriorityClass `workbricks-low` (500, `Never`) + a `priority-guard` quota refusing any other class | same |
| OR-7 | Envoy Gateway CRDs v1.9.2 (CRDs-only chart, `keep` policy) | `infrastructure/envoy-gateway-crds/` |
| OR-7 | The controller's cluster RBAC, GatewayClass `workbricks-c1` | `infrastructure/workbricks-c1-gateway/` |
| OR-10 | OpenBao: `transit/` + keys `workbricks-c1-{jwks-root,release,audit}`, policies and k8s-auth roles `workbricks-c1-{gatekeeper,signing,secrets}`, KV prefix `af/workbricks/c1/*` | `infrastructure/security/openbao/workbricks-c1-provision-job.yaml` |
| OR-10 | ConfigMap `openbao-ca` (the public ailab root CA) in `workbricks-c1` and `-data`, for the cell's SecretStores | `infrastructure/workbricks-c1-operator/` |
| OR-12 | zot push user `workbricks-ci` (`workbricks/**`, no delete); Actions secrets on `cchifor/workbricks` | `ansible/roles/registry_zot/defaults/main.yml`, `docs/runbooks/registry-cache.md` |
| OR-13 | Cilium socket LB pinned off | `kubernetes/infra/bootstrap/cilium-values.yaml` |

Paths under `infrastructure/` are relative to `kubernetes/apps/`. Flux units:
`workbricks-c1-operator`, `workbricks-c1-gateway` and `envoy-gateway-crds` (`kubernetes/apps/clusters/ai/`).

## Verify

```bash
K="kubectl --context admin@ai"
$K get ns -l workbricks/cell=c1                               # 3 namespaces
$K get resourcequota -A -l workbricks/cell=c1                 # compute + priority-guard per namespace
$K get priorityclass workbricks-low
$K get crd | grep gateway.envoyproxy.io                       # 8 CRDs
$K get gatewayclass workbricks-c1                             # Accepted once the cell's controller runs
$K auth can-i create tokenreviews --as=system:serviceaccount:workbricks-c1:gatekeeper   # yes
$K auth can-i update gatewayclasses/traefik --as=system:serviceaccount:workbricks-c1:envoy-gateway  # no
```

## Rules that protect the platform

- **Priority.** A pod that names no PriorityClass gets the global default `platform-normal`
  (100000, preempting). The `priority-guard` quota (`pods: 0` for every class except
  `workbricks-low`) refuses such a pod in the cell's namespaces, including pods an operator creates
  for the cell (set `priorityClassName` on CloudNativePG `Cluster`s and similar).
- **Gateway API CRDs** (v1.2.1 standard) belong to the platform's Traefik HelmRelease. The cell's
  Envoy Gateway chart must run with `crds.enabled=false`: with CRDs on it also installs a
  ValidatingAdmissionPolicy that refuses Gateway API CRDs older than v1.5, which would block
  Traefik's own CRD management.
- **No webhooks.** The cell's Envoy Gateway runs with the topology injector off
  (`topologyInjector.enabled=false` and
  `config.envoyGateway.provider.kubernetes.proxyTopologyInjector.disabled=true`); its chart's
  ClusterRole/ClusterRoleBinding are dropped from the cell's render in favour of
  `workbricks-c1-envoy-gateway`, which can write only the `workbricks-c1` GatewayClass.
- **Retiring the cell** is explicit: delete its Flux unit, export its data, then
  `kubectl delete ns workbricks-c1 workbricks-c1-data workbricks-c1-sandbox`. Removing
  `workbricks-c1-operator` from git leaves the namespaces (prune disabled) and removing
  `envoy-gateway-crds` leaves the CRDs (`helm.sh/resource-policy: keep`).
