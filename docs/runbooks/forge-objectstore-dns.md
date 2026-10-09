# Exact split DNS for private Forge object storage

This draft adopts the existing `kube-system/coredns` **Corefile scalar** into
Flux and adds one exact rewrite:

```text
forge-objects.taild43998.ts.net
  → objectstore-forge-tls.strive-ailab.svc.cluster.local
```

Cluster SDK clients and tailnet browsers keep the same HTTPS URL and signed
Host. Cluster DNS returns the internal Service address; outside the cluster,
Tailscale resolves the name to the dedicated proxy. The platform owner PR must
provide that Service and TLS endpoint before this change is activated. No fixed
IP, LAN reservation, public DNS, Tailscale DNSConfig or wildcard rewrite is added.
No Kubernetes write or rollout was performed during preparation.

## Ownership decision

This is not a supported custom-fragment import: none exists in the deployed
Corefile. The authoritative machine configuration has no CoreDNS override, and
Talos v1.14.2 `KubeCoreDNSConfig` supports only enablement and image selection.

The baseline in [the test fixture](../../scripts/tests/fixtures/talos-v1.14.2.Corefile)
was constructed from the exact [Talos v1.14.2 bootstrap template](https://github.com/siderolabs/talos/blob/v1.14.2/internal/app/machined/pkg/controllers/k8s/internal/k8stemplates/coredns.go)
with `ClusterDomain=cluster.local`, and matched the live Corefile byte-for-byte
on 2026-10-10. The live ConfigMap carries
`config.k8s.io/owning-inventory: talos-bootstrap-manifests-inventory`.
Talos's pinned [ManifestApplyController](https://github.com/siderolabs/talos/blob/v1.14.2/internal/app/machined/pkg/controllers/k8s/manifest_apply.go)
skips resources already in inventory and backfills existing resources; it does
not continuously overwrite this scalar. That observation is specific to this
Talos version, not a promise about future bootstrap behavior.

**Owner approval of full-Corefile adoption is required before merge.** Flux's
server-side apply will manage `data.Corefile`. Existing field-manager ownership
may conflict on the first apply; inspect it and choose the ownership transition
explicitly. Do not force-apply, remove Talos inventory metadata or deploy another
controller as an unreviewed workaround. The manifest omits that existing
inventory annotation, so Flux does not intentionally claim its value. This PR
adds no Talos machine patch and does not disable Talos CoreDNS bootstrap.

For every Talos upgrade, compare the new version's bootstrap template and
ManifestApplyController, the committed baseline and the live Corefile. Reconcile
intended upstream changes into the baseline and adopted file together. Otherwise
Flux can mask new DNS defaults after upgrade. A future supported import mechanism
should replace this full-scalar adoption through a separate reviewed migration.

## Behavior and safe activation

The sole new block uses `rewrite stop`, `name exact` and `answer auto`. It changes
only this hostname and maps answers back to the queried name. Kubernetes service
discovery, reverse lookups, upstream forwarding, error logging, caching, readiness,
metrics, loop detection and reload/load-balancing retain the exact pinned baseline.
Names adjacent to the selected name and unrelated tailnet names are not rewritten.

1. Review the ownership boundary above and the platform companion PR. Confirm
   the internal 443 Service and certificate are ready before merging this DNS
   change. Cross-repository Flux ordering is not enforced by these manifests;
   owner rollout order is required. Activating DNS first produces resolution or
   connection failures for Forge storage.
2. Re-read the live Corefile and Talos version. Compare the full baseline, not
   just the rewrite. Stop on unreviewed live customization or an ownership
   conflict. Snapshot only the public ConfigMap for rollback.
3. Verify `forge-objects` is available in the tailnet. Preparation found no
   matching live Service/Ingress and a worker query returned NXDOMAIN; the
   worker was not a Tailscale client. At rollout confirm the operator claims
   `forge-objects.taild43998.ts.net` without an automatic suffix.
4. Merge/apply only after owner approval. CoreDNS's existing `reload` plugin
   observes the mounted ConfigMap; a rollout is not deliberately triggered by
   this change. Watch both CoreDNS replicas and DNS error metrics. Confirm
   existing cluster services, reverse DNS and ordinary external names still
   resolve. Test A, AAAA and CNAME answers for the exact name, as well as a
   neighboring tailnet name; verify the intended Service's current address.
5. Qualify HTTPS/SNI/CA trust and signed SDK operations from admitted Forge pods,
   plus browser uploads/downloads on the tailnet. DNS resolves names only: it
   grants neither network access nor bucket privileges. Trust the existing lab
   CA through the owner distribution process; never bypass TLS verification.

Only static baseline tests and local kustomize rendering were performed during
preparation. Live plugin behavior, initial SSA ownership adoption and production
end-to-end S3 remain rollout gates.

## Rollback without deleting cluster DNS

The existing critical ConfigMap is annotated
`kustomize.toolkit.fluxcd.io/prune: disabled`. Removing this directory from Flux
**does not undo its Corefile** and must not delete the ConfigMap. To roll back,
first change only `data.Corefile` to the reviewed baseline through Flux and wait
for reload and DNS regression checks. Then, if desired, remove the resource from
the infrastructure kustomization while retaining the live ConfigMap and its
Talos inventory annotation. Confirm field ownership explicitly; Talos does not
automatically resume updates just because this Flux input was removed.

```sh
python3 -m unittest discover -s scripts/tests -p test_forge_objectstore_dns.py -v
kubectl kustomize kubernetes/apps/infrastructure/core-dns
kubectl kustomize kubernetes/apps/infrastructure
```

The fixture-preservation, exact rewrite and prune guards are collected by the
existing broker-inventory workflow's unittest discovery. The manifest workflow
also renders the existing infrastructure root.
