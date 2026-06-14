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

## 3. Docker Hub rate-limiting — ✅ RESOLVED (2026-06-14)
Talos machine-config mirrors docker.io through Google's anonymous pull-through cache
(`machine.registries.mirrors.docker.io -> https://mirror.gcr.io`) on all nodes — no creds, dodges 429s.

## 4. Control-plane component metrics on Talos — ✅ partial (2026-06-14)
`kube-controller-manager` + `kube-scheduler` now bind `0.0.0.0` (Talos machine-config extraArgs) and are
scraped by kube-prometheus-stack (3+3 targets up). **etcd metrics deferred** — exposing them needs a
per-node etcd restart (a parallel tofu apply restarts all 3 etcd at once → quorum risk); do it rolling
when needed. `kube-proxy` stays off (Cilium).

## 5. AI router + UI + dashboard — ✅ DONE (2026-06-14)
- Heavyweights downloaded + validated; **5 models** served (general + coder on node1, gpt-oss-120B on
  node2, Qwen3.5-122B on node3, Qwen3-VL-8B vision on node1). See `docs/runbooks/ai-host-setup.md`.
- **LiteLLM** router (`litellm.ai.svc:4000/v1`) fronts all models; **Open WebUI** (`open-webui.ai.svc`)
  is the chat UI (public via Cloudflare at chat.chifor.me).
- **Grafana dashboard** "AI LLM — Strix Halo iGPU + llama.cpp" provisioned (amdgpu_* + llamacpp:* panels).

## 6. Fast storage path for k8s PVCs (optional)
k8s CSI currently uses the QNAP over the mgmt LAN (2.5 GbE) because the Talos VMs aren't on the
Thunderbolt fabric. To use TB for PVCs, bridge each host's storage link into a Proxmox bridge and
give each VM a NIC on `10.55.0.0/24`. VM *disks* already use TB (host NFS mount).
