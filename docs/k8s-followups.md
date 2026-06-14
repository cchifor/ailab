# K8s — known follow-ups / refinements

Tracked items deferred during the build (cluster + GitOps + storage + observability are working).

## 1. Loki/Alloy log-label enrichment — ✅ RESOLVED (2026-06-14)
The Alloy `discovery.relabel` rules now surface the full label set on Loki streams — verified via
`/loki/api/v1/labels`: `namespace`, `pod`, `container`, `node`, `app` (+ `instance`, `filename`).
The earlier "only service_name/source" note was stale.

## 2. Durable block storage — local-path now; QNAP iSCSI optional (RWO + ZFS snapshots)
**Done:** `local-path-provisioner` (StorageClass `local-path`, node-local NVMe under Talos `/var`)
provides durable RWO block storage; Prometheus TSDB now uses it (was ephemeral emptyDir). NFS stays
the RWX default. Good enough for single-replica stateful workloads (node-pinned).

**Optional upgrade — QNAP iSCSI (network block, uses the QNAP's large ZFS pool, migratable, snapshots).**
Researched + validated for QuTS hero h5.2.9 (2026-06-14). Use the official Trident-based
`qnap-dev/QNAP-CSI-PlugIn` v1.6.0 (driver `csi.trident.qnap.io`, `storageDriverName: qnap-nas`) —
**not** democratic-csi (no QNAP/qcli driver; QuTS hero uses proprietary SCST, not LIO/targetcli).
Steps: (a) **MANUAL, on the NAS GUI**: enable the iSCSI service (the driver can't; the qcli is
undocumented) + confirm a ZFS pool with free space + a QNAP API user with iSCSI-admin rights;
(b) vendor the repo's `./Helm/trident` chart + `./VolumeSnapshot/` CRDs into Flux (no public Helm repo);
(c) `TridentBackendConfig` — **apiVersion `trident.qnap.io/v1`** (NOT trident.netapp.io), `qnap-nas`,
`storageAddress: 192.168.1.225` (mgmt — used for BOTH the HTTP API and the iSCSI portal; the VMs are
NOT on the 10.55 TB fabric so 10.55.0.254 is unreachable), `userCHAP: true` (NOT useCHAP), CHAP secret
12–16 chars; (d) StorageClass `csi.trident.qnap.io`. **Pin Talos at v1.11.2** — v1.11.3 has an iSCSI
regression (talos #12119). Talos `iscsi-tools` extension already baked in.

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

## 5. AI heavyweight models + router (sub-phase 3 follow-on)
The daily driver (Qwen3-30B-A3B) is live on all 3 `ai-llm` LXCs. Status:
- ✅ **Heavyweights downloaded + validated (2026-06-14)** on the shared NFS, on-demand per
  `docs/runbooks/ai-host-setup.md`: gpt-oss-120B **53 tok/s** (59 GiB VRAM, fits the carve), Qwen3.5-122B
  **23 tok/s** (64 GiB VRAM + 8 GiB GTT spill). Both run on the current 64 GiB carve; the 122B is RAM-tight
  with a 32 GiB CP VM — downsize that node's CP VM (or shrink the carve) only for large 122B contexts.
- **Model router (open)** — to serve multiple models behind one endpoint, add a router (e.g. LiteLLM)
  in front of the per-node `llama-server`s; today the `llm` Service advertises only the daily driver, so
  heavyweights are addressed directly by node IP. Plus an optional Open WebUI in the `ai` namespace.
- **Grafana dashboard** for `amdgpu_*` + `llamacpp:*` (panels: iGPU busy %, VRAM/GTT used, decode tok/s,
  KV usage, queue depth).

## 6. Fast storage path for k8s PVCs (optional)
k8s CSI currently uses the QNAP over the mgmt LAN (2.5 GbE) because the Talos VMs aren't on the
Thunderbolt fabric. To use TB for PVCs, bridge each host's storage link into a Proxmox bridge and
give each VM a NIC on `10.55.0.0/24`. VM *disks* already use TB (host NFS mount).
