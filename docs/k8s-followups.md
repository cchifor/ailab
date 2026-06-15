# K8s — known follow-ups / refinements

Tracked items deferred during the build (cluster + GitOps + storage + observability are working).

## 1. Loki/Alloy log-label enrichment — ✅ RESOLVED (2026-06-14)
The Alloy `discovery.relabel` rules now surface the full label set on Loki streams — verified via
`/loki/api/v1/labels`: `namespace`, `pod`, `container`, `node`, `app` (+ `instance`, `filename`).
The earlier "only service_name/source" note was stale.

## 2. Durable block storage — ✅ DONE: QNAP iSCSI network block (2026-06-15)
Three tiers now exist: **`nfs-csi`** (RWX default) · **`local-path`** (node-local NVMe, RWO, node-pinned) ·
**`qnap-iscsi`** (network block from the QNAP ZFS pool, RWO, migratable, expandable, ZFS snapshots).

**`qnap-iscsi` is live** via the official Trident-based `qnap-dev/QNAP-CSI-PlugIn` v1.6.0
(driver `csi.trident.qnap.io`, `storageDriverName: qnap-nas`) — **not** democratic-csi (no QNAP/qcli
driver; QuTS hero uses proprietary SCST, not LIO/targetcli). Layout: GitRepository + `qnap-trident`
HelmRelease (`./Helm/trident`, `trident` ns) under `infrastructure`; `TridentBackendConfig` + StorageClass
under the `qnap-storage` Flux Kustomization (`dependsOn infrastructure` so the CRD exists first).
Gotchas that bit us, now resolved:
- **apiVersion `trident.qnap.io/v1`** (NOT trident.netapp.io); `userCHAP` (NOT useCHAP).
- **`networkInterfaces: []` (empty)** → the iSCSI portal defaults to `storageAddress` (192.168.1.225,
  the mgmt NIC). Named QTS adapters like `"Adapter1"` fail with *"adapter not found"* — they don't match
  what the HTTP API reports, and the Talos VMs (192.168.0.0/23) can only route to 192.168.1.225 anyway
  (NOT on the 10.55 TB fabric, so 10.55.0.254 is unreachable). Backend then → **Bound/Success**.
- Secret keys (SOPS): `username`/`password`/`storageAddress`/`https`/`port`; single-quote values that
  start with `@`. **MANUAL prereq on the NAS GUI:** enable the iSCSI service (the driver can't) + a ZFS
  pool with free space + an API user with iSCSI-admin rights. **Pin Talos v1.11.2** (v1.11.3 has an iSCSI
  regression, talos #12119); `iscsi-tools` extension already baked in.
- **`reclaimPolicy: Retain`** (production-safe). To fully drop a released volume incl. the QNAP LUN, use
  `tridentctl delete volume <name> -n trident` inside the controller pod (k8s Retain leaves the LUN).

**Prometheus TSDB migrated to `qnap-iscsi`** (was local-path) — now durable AND migratable: the LUN
re-attaches wherever Prometheus reschedules, so node loss no longer strands the metrics history. The
StatefulSet volumeClaimTemplate is immutable, so a fresh build provisions iSCSI directly; an in-place
migration needs out-of-band STS surgery (scale the prometheus-operator to 0, delete the STS + old PVC,
scale back to 1 → operator recreates the STS with the new template and a fresh iSCSI PVC).

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
