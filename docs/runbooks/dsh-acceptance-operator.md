# DSH acceptance operator identity

`dsh-acceptance-operator` is the service account for disposable native acceptance Jobs. Its Role is
namespace-scoped and contains no Secret, Deployment, StatefulSet, Service, or RBAC permissions. It
can create and inspect Jobs, ConfigMaps, NetworkPolicies, the dedicated
`dsh-acceptance-workspace` PVC, and Pod status/logs in namespace `dsh`.

Acceptance pods labelled `dsh.chifor.me/acceptance=true` receive a narrow egress exception to the
Kubernetes API VIP/control-plane addresses on TCP 6443. They do not inherit the Web pod's tokenless
identity; the acceptance manifest must opt in to `dsh-acceptance-operator` explicitly.

The ValidatingAdmissionPolicy is part of the boundary: resources created or changed by this identity
must match `dsh-*-acceptance-*`; Jobs must run as this identity, have no host namespaces, privileged
containers, init/ephemeral containers, Secret/hostPath/PVC volumes, and may reference only
`dsh-litellm/api_key` or `dsh-credentials/DSH_CODEX_ACCESS_TOKEN`. NetworkPolicies must select pods
labelled `dsh.chifor.me/acceptance=true`. No Secret values are readable through Kubernetes RBAC.

Acceptance manifests should set `serviceAccountName: dsh-acceptance-operator` and request the API
endpoint through the pod's projected service-account token. The dsh Web Deployment remains tokenless
and unchanged. The identity is intended for the bounded acceptance process; external reviewer approval
and publication/merge remain outside this account.

Check authorization without reading credentials:

```bash
kubectl --context admin@ai -n dsh auth can-i --as=system:serviceaccount:dsh:dsh-acceptance-operator create jobs
kubectl --context admin@ai -n dsh auth can-i --as=system:serviceaccount:dsh:dsh-acceptance-operator get secrets
kubectl --context admin@ai -n dsh auth can-i --as=system:serviceaccount:dsh:dsh-acceptance-operator get pods/log
```

The expected answers are `yes`, `no`, and `yes`. Use the existing acceptance Job manifests to apply,
inspect, and rerun work; do not create a long-lived token Secret. For an operator-side kubectl
session, mint a short-lived token with `kubectl create token` and keep it in the local kubeconfig only.
