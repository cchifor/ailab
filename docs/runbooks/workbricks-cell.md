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
| OR-7 | Envoy Gateway CRDs v1.9.2 (upstream tag, sparse checkout, server-side apply, prune disabled) | `clusters/ai/envoy-gateway-crds.yaml` |
| OR-7 | The controller's cluster RBAC, GatewayClass `workbricks-c1` | `infrastructure/workbricks-c1-gateway/` |
| OR-10 | OpenBao: `transit/` + keys `workbricks-c1-{jwks-root,release,audit}`, policies and k8s-auth roles `workbricks-c1-{gatekeeper,signing,secrets}`, KV prefix `af/workbricks/c1/*` | `infrastructure/security/openbao/workbricks-c1-provision-job.yaml` |
| OR-10 | ConfigMap `openbao-ca` (the public ailab root CA) in `workbricks-c1` and `-data`, for the cell's SecretStores | `infrastructure/workbricks-c1-operator/` |
| OR-11 | The cell's own Flux unit `workbricks-c1` (cchifor/workbricks, `deploy/cells/c1`, `main`), **suspended** until that path exists; read-only deploy key (Gitea key id 72) | `clusters/ai/workbricks-c1.yaml`, `clusters/ai/workbricks-c1-git-auth.yaml`, `workbricks-c1-bootstrap/` |
| OR-11 | Its identity `flux-system/workbricks-c1-reconciler`: ClusterRole `workbricks-c1-deployer` bound by RoleBindings in the 3 namespaces only (no `escalate`/`bind`) | `infrastructure/workbricks-c1-operator/reconciler-rbac.yaml` |
| OR-8/11 | VAP `workbricks-c1-certificate-issuer`: Certificates/CertificateRequests in the cell only from ClusterIssuer `letsencrypt-workbricks-c1` (not created yet, so none at all today) | `infrastructure/workbricks-c1-operator/certificate-issuer-guard.yaml` |
| OR-11 | VAPs `workbricks-c1-{gateway-attachment,service-exposure,no-sa-token-secrets,scrape-scope,secret-store-scope,operator-owned}`: the paths other controllers would carry outside the cell | `infrastructure/workbricks-c1-operator/cell-admission-guards.yaml` |
| OR-12 | zot push user `workbricks-ci` (`workbricks/**`, no delete); Actions secrets on `cchifor/workbricks` | `ansible/roles/registry_zot/defaults/main.yml`, `docs/runbooks/registry-cache.md` |
| OR-13 | Cilium socket LB pinned off | `kubernetes/infra/bootstrap/cilium-values.yaml` |

Paths under `infrastructure/` and `clusters/` are relative to `kubernetes/apps/`. The CRDs cannot be a
HelmRelease: Helm's release Secret would exceed the 1 MiB object limit (about 2.5 MB of CRDs). Flux units:
`workbricks-c1-operator`, `workbricks-c1-gateway`, `envoy-gateway-crds-upstream`, `workbricks-c1-git-auth` and
the cell's own `workbricks-c1` (`kubernetes/apps/clusters/ai/`).

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
$K -n flux-system get kustomization workbricks-c1                   # suspended until deploy/cells/c1 exists
R=system:serviceaccount:flux-system:workbricks-c1-reconciler
$K auth can-i create deployments -n workbricks-c1 --as=$R             # yes
$K auth can-i create deployments -n default --as=$R                   # no
$K auth can-i update resourcequotas -n workbricks-c1 --as=$R          # no
$K auth can-i escalate roles -n workbricks-c1 --as=$R                 # no
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
- **The cell's own unit** (`workbricks-c1`) impersonates `workbricks-c1-reconciler`. Its ClusterRole
  is the cell's declared kinds plus exactly what Envoy Gateway's chart writes into its namespaced
  Roles, so those Roles can be created without `escalate`; nothing it creates can grant ResourceQuota,
  LimitRange or Namespace writes. Measured 2026-10-22 with a probe of this ClusterRole: the chart's
  4 Roles and 4 RoleBindings were admitted; a ResourceQuota Role, a RoleBinding to `admin` and a
  Deployment in `default` were refused. A kind the cell needs later is requested in its
  `operator-requests.md` and added here. Resuming the unit (dropping `suspend: true`) is a reviewed
  change, made once `deploy/cells/c1` exists in cchifor/workbricks.
- **What RBAC does not bound.** The unit can run pods as any ServiceAccount in the cell, so its
  reach includes those accounts' bindings (gatekeeper's `system:auth-delegator`,
  `workbricks-c1-envoy-gateway`, the cell's OpenBao roles). Kept minimal by design. Pod specs are
  bounded by Pod Security `restricted`, enforced on the three Namespaces, which the unit cannot write
  (a privileged hostNetwork pod is refused). Writes that make
  ANOTHER controller act outside the cell are refused by `cell-admission-guards.yaml`, for every creator
  in a `workbricks/cell=c1` namespace: a Gateway must use class `workbricks-c1`, routes may attach
  only to cell Gateways, and ReferenceGrants may grant only the cell's own namespaces; Services are ClusterIP only with no `externalIPs` (so the cell's Envoy proxy
  Service must be ClusterIP: public traffic arrives through the tunnel); no legacy ServiceAccount-token
  Secrets; ServiceMonitors/PodMonitors may select only cell namespaces (the platform Prometheus does
  not ignore namespace selectors); ExternalSecrets may read only through a namespaced SecretStore
  (no ClusterSecretStore, no ClusterGenerator); and the cell's unit may not change or delete anything labelled
  `workbricks/operator-owned=true` (the `openbao-ca` ConfigMaps, its own RoleBindings). Measured
  2026-10-22 with probe copies: every admit/deny case as intended.
- **Certificates.** `workbricks-c1-certificate-issuer` refuses any Certificate or CertificateRequest
  in a namespace labelled `workbricks/cell=c1` unless it names ClusterIssuer
  `letsencrypt-workbricks-c1` (any creator, cert-manager's shims included). That issuer does not
  exist yet; public TLS for the cell is planned at Cloudflare's edge, like strive.place.
- **Retiring the cell** is explicit: delete its Flux unit, remove its deploy key from
  cchifor/workbricks, export its data, then
  `kubectl delete ns workbricks-c1 workbricks-c1-data workbricks-c1-sandbox`. Removing
  `workbricks-c1-operator` from git leaves the namespaces (prune disabled) and removing
  `envoy-gateway-crds-upstream` leaves the CRDs (`kustomize.toolkit.fluxcd.io/prune: disabled`). That unit
  runs as `envoy-gateway-crds-applier`, which may only write CustomResourceDefinitions.
