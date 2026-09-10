# Runbook: `helmtest` — deploying a chart from a dev-worker

Six per-worker namespaces (`helmtest-dw1..6`) where an agent may actually `helm install`, and the
credential that reaches them. Design + rejected alternatives: **ADR 0021**. The lease pool this is
*not*: `kubernetes/apps/infrastructure/testpool/README.md`. The credential plane it rides on:
`docs/runbooks/openbao-dev-workers.md`.

**Context for every command:** `kubectl --context admin@ai` (the default context flip-flops — always
pass it explicitly), repo root as CWD.

## Which kubeconfig, and when

| File | Namespace | For |
|---|---|---|
| `~/.tep/kubeconfig` | `testpool` | **Leasing** a Kata sandbox: `tep lease` / `tep run` / `tep release`. Can claim sandboxes and exec into them. **Cannot deploy** — no create on anything. |
| `~/.helmtest/kubeconfig` | `helmtest-dw<N>` | **Deploying**: `helm install/upgrade/test/uninstall`. Yours alone; no other worker can reach it. |

Both are rendered by the local `bao agent` from `af/dev-workers/<hostname>` and refresh themselves.
**Never edit, copy, or hand one to anything else** — the next render overwrites it, and a copy is a
bearer token with a month of life on it.

If a file is missing, the host has not cut over yet (ADR 0021 Phase 3); see
`openbao-dev-workers.md` § "kubeconfig cutover".

## Installing a chart

```bash
KC=~/.helmtest/kubeconfig
VALUES=kubernetes/apps/infrastructure/helmtest/hack/values-restricted.yaml

helm --kubeconfig $KC upgrade --install myrel ./chart -f $VALUES --history-max 3 --wait --timeout 5m
helm --kubeconfig $KC test myrel --logs
helm --kubeconfig $KC uninstall myrel
```

**`--history-max` is an `upgrade` flag.** `helm install` (3.16.3, the pinned worker version) does not
accept it and exits before deploying anything. Always `upgrade --install`. Use `--history-max 3`: each
revision is a Secret, `count/secrets` is quota'd, and the namespace budget assumes ≤10 concurrent
releases × 3 revisions.

## The four things that will bite you

### 1. PSA `restricted` rejects ordinary charts, not just privileged ones

Every container — **including initContainers and `helm test` hook pods** — must set all four of:

```yaml
runAsNonRoot: true
allowPrivilegeEscalation: false
capabilities: { drop: ["ALL"] }
seccompProfile: { type: RuntimeDefault }
```

Most upstream charts do not by default. `hack/values-restricted.yaml` sets them for the two common
chart conventions; for anything else, read the chart's own `values.yaml` — there is no universal
shape. The reliable check is to render and grep rather than to hope:

```bash
helm template myrel ./chart -f $VALUES | grep -A5 'securityContext' | head -40
```

A **Deployment** whose pod template violates `restricted` is *accepted as an object* and then produces
no pods — the rejection surfaces on the ReplicaSet. So "the Deployment was created" proves nothing:

```bash
kubectl --kubeconfig $KC describe rs -l app.kubernetes.io/instance=myrel | grep -i 'FailedCreate\|violates'
```

The standard `nginx` image cannot run here at all (root, binds :80). `nginxinc/nginx-unprivileged` or
a busybox `httpd -p 8080` can — see `hack/smoke-chart/` for a worked, working example.

### 2. Your Role is explicit, and the escalation check cuts both ways

You can CRUD: secrets, configmaps, services, serviceaccounts, PVCs, pods, deployments, statefulsets,
replicasets, jobs, cronjobs, PDBs, HPAs, and **namespaced** roles/rolebindings. `pods/log` and
`events` are read-only; `pods/exec` and `pods/portforward` are create+get for debugging.

You **cannot**: create NetworkPolicies, Ingresses, DaemonSets, Endpoints, CRDs, ClusterRoles,
ClusterRoleBindings, Namespaces, or any custom resource.

And a chart whose **own Role** grants something you lack is **rejected** by the API server's RBAC
escalation check — you cannot grant what you do not hold. In practice that means charts asking for
`events` create/patch, `endpoints` get, or `coordination.k8s.io` `leases` (leader election) fail to
install even when they are otherwise `restricted`-compatible. Pre-flight it:

```bash
helm template myrel ./chart -f $VALUES \
  | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if not d: continue
    k = d.get("kind")
    if k in ("ClusterRole","ClusterRoleBinding","CustomResourceDefinition","Namespace","Ingress","DaemonSet","NetworkPolicy"):
        print(f"UNSUPPORTED KIND: {k}/{d[\"metadata\"][\"name\"]}")
    if k == "Role":
        for r in d.get("rules", []):
            print(f"chart Role wants: {r.get(\"apiGroups\")} {r.get(\"resources\")} {r.get(\"verbs\")}")
'
```

Anything it flags either gets disabled through chart values, or becomes a reviewed addition to
`kubernetes/apps/infrastructure/helmtest/rbac.yaml` — never a wildcard.

### 3. Service type and network are constrained

`services.nodeports` and `services.loadbalancers` are quota'd to **0**, so a chart defaulting to
`LoadBalancer` (Bitnami's nginx does) is refused at admission with a quota error. Set
`service.type: ClusterIP`.

Network: same-namespace traffic is allowed (so `helm test` hooks can reach their Service). Everything
else is denied — other `helmtest` namespaces, `testpool`, the OpenBao ClusterIP and NodePort, the LAN,
the node IPs, and the internet. DNS resolves cluster-local names only. If your chart needs to pull
something at runtime, it will not work here; bake it into the image or use a leased sandbox.

Image *pulls* are unaffected — containerd does those on the node, outside pod policy.

### 4. `helm test` hooks and their logs

Do **not** put `helm.sh/hook-delete-policy: hook-succeeded` on a test hook: in 3.16.3 that deletes the
pod before `helm test --logs` reads it, so the logs come back empty and the check silently proves
nothing. Use `before-hook-creation` (cleans up the *previous* run) and delete explicitly afterwards.

Asserting cleanup with `-l 'helm.sh/hook'` does not work either — `helm.sh/hook` is an **annotation**,
not a label, so that selector matches nothing and reports "clean" regardless. Assert by name:

```bash
kubectl --kubeconfig $KC delete pod myrel-test --ignore-not-found
kubectl --kubeconfig $KC get pod myrel-test 2>&1 | grep -q NotFound && echo cleaned
```

## Charts this namespace cannot run

Anything needing privileged pods, CRDs, cluster-scoped objects, or real internet egress. The intended
fallback is a nested cluster (k3d) inside a leased Kata sandbox — **that workflow does not exist
yet** (ADR 0021 follow-up). Until it does, such a chart is out of scope here: say so rather than
looking for a wider credential.

## Reserved objects you cannot touch

A Kyverno ClusterPolicy denies DELETE/UPDATE on the `helmtest-dw<N>` ServiceAccount, the
`helmtest-deployer` Role and RoleBinding, the ResourceQuota, the LimitRange and the NetworkPolicies.
That is not paranoia about you — the Role *must* allow deleting ServiceAccounts for `helm uninstall`
to work, and RBAC cannot exclude one by name, so a chart that happens to use the name `helmtest-dw3`
would otherwise delete your own identity and invalidate every token minted for it.

If a chart collides with one of those names, rename the chart's object.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `unknown flag: --history-max` | Used it on `install`. | `upgrade --install`. |
| `violates PodSecurity "restricted"` | A container missing one of the four settings. | Apply `values-restricted.yaml`; check initContainers and hook pods too. |
| Deployment created, no pods | Pod template violates `restricted`. | `describe rs`, look for `FailedCreate`. |
| `exceeded quota: services.nodeports` | Chart defaults to NodePort/LoadBalancer. | `service.type: ClusterIP`. |
| `attempt to grant extra privileges` | The chart's own Role exceeds `helmtest-deployer`. | Disable that component, or get the rule added (ADR 0021). |
| `forbidden: ... is not allowed` on a CRD/ClusterRole | Cluster-scoped, never granted here. | Out of scope for `helmtest`. |
| `helm test` passes but `--logs` is empty | `hook-succeeded` deleted the pod first. | Use `before-hook-creation`. |
| Test hook cannot reach its Service | Usually a wrong Service name, not policy — same-namespace traffic IS allowed. | `kubectl --kubeconfig $KC get svc`. |
| `~/.helmtest/kubeconfig` missing | Host has not cut over. | `openbao-dev-workers.md` § kubeconfig cutover. |
| `Unauthorized` from a kubeconfig | Token expired (the sync is failing) or the SA was deleted. | `kubectl --context admin@ai -n openbao get job -l app.kubernetes.io/name=openbao-k8stoken-sync`. |

## Operator: adding a permission or a namespace

The worker list is five copies of one fact — `helmtest/namespaces.yaml`, `helmtest/rbac.yaml`,
`helmtest/networkpolicy.yaml`, `openbao/k8stoken-sync.yaml` (the per-namespace Role/RoleBinding and
the script's `TARGETS`), and `inventory/hosts.yml`. Change them together.

A new permission is a reviewed edit to the `helmtest-deployer` rules in `rbac.yaml` plus a line here
saying why. Never a wildcard: `["*"]` includes `escalate` and `bind`.
