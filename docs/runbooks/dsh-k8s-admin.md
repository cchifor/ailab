# DSH Kubernetes access

The dsh Web pod runs as service account `dsh-k8s-admin` in namespace `dsh`. Its Role is
namespace-scoped and covers acceptance Jobs, ConfigMaps, NetworkPolicies, PVCs, Pod status/logs,
and Events. It has no Secret permissions, no access to other namespaces, and no ClusterRoleBinding.
The Kubernetes API egress is limited to the in-cluster Kubernetes Service (`10.96.0.1:443`) and the
three Talos control-plane addresses on TCP 6443.

The projected service-account token is available to `kubectl` in the runtime. Check the boundary
without reading credentials:

```bash
kubectl --context admin@ai -n dsh auth can-i --as=system:serviceaccount:dsh:dsh-k8s-admin create jobs
kubectl --context admin@ai -n dsh auth can-i --as=system:serviceaccount:dsh:dsh-k8s-admin get secrets
kubectl --context admin@ai -n dsh auth can-i --as=system:serviceaccount:dsh:dsh-k8s-admin get pods/log
```

Expected answers are `yes`, `no`, and `yes`. Model, Gitea, and SSH credentials continue to arrive
through their existing OpenBao/External Secrets references; the Kubernetes identity cannot read those
Secret objects. Independent reviewer approval and external publication remain outside this identity.
