# dsh Kubernetes access: `cluster-admin`

The dsh pod runs as ServiceAccount `dsh-k8s-admin` in namespace `dsh`, and that ServiceAccount is
bound to the built-in **`cluster-admin`** ClusterRole by one `ClusterRoleBinding`
(`kubernetes/apps/apps/dsh/k8s-admin.yaml`). Operator decision, 2026-09-18, recorded in
[ADR 0025](../decisions/0025-dsh-cluster-admin.md) as an exception to ADR 0021's credential tiers.

`kubectl` v1.31.4 is on the agent's PATH at `/dsh-home/.local/bin/kubectl`, installed by the
`install-kubectl` init container in `deployment.yaml`. It uses the in-cluster configuration (the
`KUBERNETES_SERVICE_HOST`/`_PORT` variables plus the projected token and CA under
`/var/run/secrets/kubernetes.io/serviceaccount/`); there is no kubeconfig and none should be created.

Every host-side command below carries `--context admin@ai` — the default context is a different
cluster.

## What the grant means

- **Everything.** Every namespace, every resource, every verb: every Secret in the cluster
  (SOPS-materialised Secrets, the OpenBao break-glass token, Gitea admin, Cloudflare tunnel
  credentials), Flux itself, `exec` and `port-forward` into any pod, workloads anywhere admission
  allows. From a pod that executes model-authored tool calls.
- **Identity-wide.** Every container of the dsh pod holds the token (it is automounted — `dsh`,
  `relay`, and the init containers), and so does any other workload that names the ServiceAccount.
  The operator-applied `dsh-native-*` acceptance Jobs do not (`automountServiceAccountToken:
  false`, no `serviceAccountName`); a future Job that names it would.
- **The network policies are no longer a boundary.** `dsh-allow`'s private-range exclusions and
  `dsh-operator-ssh`'s host list still block the pod's *direct* connections, which catches a
  mistake or an injection over a raw socket. They do not bound the agent: the API is the wider
  inward door, and the identity can rewrite those policies. The rules stay because they cost
  nothing; their comments say this.
- **RBAC is not admission.** The `dsh` namespace enforces the `baseline` Pod Security Standard
  (`namespace.yaml`), so a privileged, hostPath or host-namespace Pod *in `dsh`* is rejected
  whatever the identity — but the same Pod in a namespace without PSA labels is admitted. The
  `agentforge-*-guard` ValidatingAdmissionPolicies are keyed on other usernames, or on the
  `agentforge-ci` / `agentforge-sandbox` namespaces; none names this identity, and in those two
  namespaces they apply to it exactly as to anyone else.

## Verify

Authorization, from the workstation:

```bash
kubectl --context admin@ai get clusterrolebinding dsh-k8s-admin -o yaml     # roleRef cluster-admin, one SA subject
kubectl --context admin@ai auth can-i --list --as=system:serviceaccount:dsh:dsh-k8s-admin | head -3   # *.* [] [] [*]
kubectl --context admin@ai auth can-i get secrets -A --as=system:serviceaccount:dsh:dsh-k8s-admin    # yes
```

The binary and the identity, inside the pod (this is what the agent gets — its shell keeps the
`KUBERNETES_*` variables; the subprocess scrub drops only names matching `KEY|PASSWORD|SECRET|TOKEN`
and `DSH_*`):

```bash
kubectl --context admin@ai -n dsh logs deploy/dsh -c install-kubectl      # "installed to ..." or "already present ... sha256 matches" + the real client version
kubectl --context admin@ai -n dsh exec deploy/dsh -c dsh -- sh -c 'command -v kubectl; kubectl version --client; kubectl auth whoami'
kubectl --context admin@ai -n dsh exec deploy/dsh -c dsh -- kubectl get nodes
```

`Running 2/2` alone proves nothing about kubectl: the degraded boot below looks identical from
`kubectl get pods`. Read the init container's log.

## The degraded boot, and the curl fallback

`install-kubectl` is deliberately **never fatal**. On a first boot (or after a version bump) it
downloads kubectl from `dl.k8s.io`, verifies the pinned sha256, and publishes it atomically; on
every later boot it hashes the cached file and exits without touching the network. If anything
fails — architecture, download (three attempts: a stalled server costs at most 3 × 120 s + 2 × 5 s
= 370 s, an unreachable one ~40 s), checksum, install — it prints a `WARNING:` block naming the
cause, removes whatever is at the destination, and exits 0, and dsh boots **without kubectl**. The
invariant: after this init container, `/dsh-home/.local/bin/kubectl` is either the pinned binary
(mode 0755) or absent; nothing unverified is left on PATH. (The one exception is a filesystem that
refuses the removal, which the WARNING names.)

A completed init container is not re-run by a container restart, so the retry is a pod roll:

```bash
kubectl --context admin@ai -n dsh logs deploy/dsh -c install-kubectl
kubectl --context admin@ai -n dsh rollout restart deploy/dsh
```

Until then the API is still reachable from the pod with curl and the projected token. Hand the
header to curl on **stdin** (`-H @-`), built by the shell's `printf` builtin, so the token is never
in any process's argv (`ps` in the container would show it), and never write it into a kubeconfig,
a log or `set -x` output:

```bash
kubectl --context admin@ai -n dsh exec deploy/dsh -c dsh -- sh -c '
  SA=/var/run/secrets/kubernetes.io/serviceaccount
  printf "Authorization: Bearer %s
" "$(cat $SA/token)" |
    curl -sS --cacert $SA/ca.crt -H @- https://kubernetes.default.svc/api/v1/namespaces | head -20'
```

(Tested 2026-09-18 in the live container, curl 7.88.1: `/version` answers 200 with the header read
from stdin and 401 without it; the certificate is validated against the projected CA. The same
endpoint answered on `10.96.0.1` and the control planes on `:6443`.)

## Bump kubectl

Edit BOTH values on the `install-kubectl` init container in `deployment.yaml`: `KUBECTL_VERSION`
and `KUBECTL_SHA256` (the amd64 checksum from
`https://dl.k8s.io/release/<version>/bin/linux/amd64/kubectl.sha256`). The template change rolls
the pod; the cache check fails on the new pin and re-downloads. The cache is keyed on the sha256
ALONE — a version-only edit is a no-op that keeps the old binary, which is why the cache-hit log
line prints the binary's real `version --client` rather than the pinned string. Keep the client
within one minor of the server (`kubectl --context admin@ai version`). Bump it in the same change
that upgrades the cluster.

## Revoke

Delete the `ClusterRoleBinding` from `k8s-admin.yaml`, merge, let Flux prune it, then confirm:

```bash
kubectl --context admin@ai get clusterrolebinding dsh-k8s-admin                                   # NotFound
kubectl --context admin@ai auth can-i --list --as=system:serviceaccount:dsh:dsh-k8s-admin | head   # no *.* [*]
```

That revokes **future** use only. It does not undo bindings, tokens, or other access created while
the grant was held; if that is the concern, this is an incident and the ServiceAccount's token
history (`authentication.kubernetes.io/credential-id` in the audit trail) is the starting point,
not this runbook.

## History: the bounded design this replaced

2026-09-17/18, PRs #770–#774: a namespace-scoped Role plus a `ValidatingAdmissionPolicy`
(`dsh-k8s-admin-guard`) that admitted only Jobs and NetworkPolicies named
`^dsh-[a-z0-9-]+-acceptance-[a-z0-9]+$` with a fixed pod shape and a fixed pair of Secret references.
The agent's real acceptance workloads failed two of its three expressions, the identity could not
read the cluster-scoped policy that denied it, the kubectl image reference in #773
(`mirror.gcr.io/bitnami/kubectl:1.31.1`) no longer resolved, and #774 punched a name-based hole
through the policy. The operator chose full access and simplicity over another guard. Do not
re-derive the bounded design; if a bound is ever wanted again, ADR 0025 is where to argue it.
