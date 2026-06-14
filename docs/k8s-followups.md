# K8s — known follow-ups / refinements

Tracked items deferred during the build (cluster + GitOps + storage + observability are working).

## 1. Loki/Alloy log-label enrichment
Loki + Alloy are deployed and Alloy is actively tailing every pod's `/var/log/pods/.../0.log`
on all nodes (file-based `loki.source.file` + `stage.cri`). Metrics are unaffected. But the
`namespace`/`pod`/`container` labels from `discovery.relabel` aren't yet surfacing as Loki stream
labels (only `service_name`/`source` appear). Needs a short pass on the Alloy pipeline
(`local.file_match` → `loki.source.file` label propagation, or promote via `stage.static_labels`/
`stage.label`). Logs flow; they're just under-labeled for filtering.

## 2. QNAP iSCSI CSI (RWO + ZFS snapshots)
Deferred in favor of NFS RWX (the working default). To add: enable QNAP iSCSI, Trident-based
`qnap-dev/QNAP-CSI-PlugIn` (Helm `./Helm/trident`, ns `trident`) via a Flux `GitRepository` source,
a SOPS-encrypted QNAP creds secret, a `TridentBackendConfig` (storageDriverName `qnap-nas`,
networkInterfaces `["Adapter1"]`, CHAP), and a `csi.trident.qnap.io` StorageClass (make it default).
Talos `iscsi-tools` extension is already baked into the image.

## 3. Docker Hub rate-limiting (recurring)
Anonymous pulls from `docker.io` hit HTTP 429 several times (kiwigrid sidecar, curl test image,
during the Alloy roll). quay.io/ghcr.io/registry.k8s.io were fine. Recommended fix: add a
`docker.io` pull-through mirror / auth in the Talos machine config
(`.machine.registries.mirrors` / `.config`) in `kubernetes/infra`, then re-apply. Prevents future
stalls (incl. AI images).

## 4. Control-plane component metrics on Talos
`kube-controller-manager` / `kube-scheduler` / `etcd` / `kube-proxy` scrape jobs are disabled in
kube-prometheus-stack (they bind locally / kube-proxy is off under Cilium). To get control-plane
metrics, expose them via Talos machine-config (bind addresses) and re-enable the serviceMonitors.

## 5. Fast storage path for k8s PVCs (optional)
k8s CSI currently uses the QNAP over the mgmt LAN (2.5 GbE) because the Talos VMs aren't on the
Thunderbolt fabric. To use TB for PVCs, bridge each host's storage link into a Proxmox bridge and
give each VM a NIC on `10.55.0.0/24`. VM *disks* already use TB (host NFS mount).
