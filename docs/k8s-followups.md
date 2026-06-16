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

## 6. Fast storage path for k8s PVCs — ✅ DONE: CSI on Thunderbolt (2026-06-15)
Both `nfs-csi` and `qnap-iscsi` now ride the TB fabric via **Approach A1: host-as-router + SNAT** (the VM
keeps its single NIC; each Proxmox host forwards + SNATs storage traffic over `thunderbolt0`). `server`/
`storageAddress` are `10.55.0.254`; the persistent `storage_router` (sysctl + idempotent SNAT + 30 s
self-heal timer) runs on all 3 hosts; the Talos route + `storage-tier` nodeLabels are on all CPs;
Prometheus is affinity-pinned to a TB node and attaches via `10.55.0.254:3260`. Measured ~**660 MB/s**
NFS write (vs ~280 on 2.5 GbE). Findings (see ADR 0011): Trident `storageAddress` is immutable → backend
delete+recreate; QuTS hero rejects NFSv4.1 → use `nfsvers=4.0`. node3 (USB, 2.5 GbE) reaches `.254` over
its slower link. A **per-node storage health-check** (blackbox DaemonSet probing `.254:2049/:3260` from
each node + `StorageFabricUnreachable` alert) catches a dropped host SNAT (node Ready but CSI hung).
`192.168.1.225` is the documented quick-revert.

## 7. Backup / DR — ✅ Layer A done, Layer B deferred (2026-06-15)
In-cluster VolumeSnapshots live (external-snapshotter v8 + a `qnap-iscsi` VolumeSnapshotClass;
round-trip validated). Off-NAS DR (Velero + CSI data-mover → Cloudflare R2) is the documented Layer B
follow-up — see ADR 0010. Until then a QNAP loss is an accepted residual risk.

## 8. Control-plane colocation hardening — ✅ DONE (2026-06-15)
Kubelet kube/systemReserved + evictionHard (all 3 CPs), PriorityClasses, and per-namespace LimitRanges —
see ADR 0009. Deferred: per-namespace ResourceQuotas (LimitRange-only for now; never quota the
`trident`/`local-path-storage` privileged DaemonSet namespaces).

## 9. SSO (Authelia OIDC hybrid) — ✅ deployed, ⏸️ user CF step pending (2026-06-15)
Self-hosted Authelia (ns `auth`, OIDC at sso.chifor.me) live + verified; Grafana + Open WebUI wired as
OIDC clients (group->role); LiteLLM stays master-key; Cloudflare Access kept as the edge gate. ADR 0012.
**User must:** `cloudflared tunnel route dns ailab sso.chifor.me` (+ no Access app on sso; relax Access on
grafana/chat for a single prompt), then browser-test. Gotchas captured in ADR 0012 (ntp/enableServiceLinks/
readOnlyRootFilesystem/command). Lab user login: chifor / <generated, in the authelia-secrets SOPS users db>.

## 10. metrics-server — ✅ DONE (2026-06-16)
Metrics API live (`kubectl top` / HPA / Homepage k8s widget). Helm chart 3.12.2 in `kube-system` with
`--kubelet-insecure-tls` (Talos kubelets serve self-signed certs). Wired under `infrastructure/monitoring`.

## 11. Gatus uptime + status page — ✅ DONE (2026-06-16)
Config-as-code uptime checks (ns `gatus`, memory storage, 11 endpoints across AI/Observability/Infra +
the QNAP storage-fabric TCP ports) — chosen over Uptime Kuma (UI/DB config) for GitOps fit. Surfaced on the
Homepage via the `gatus` widget. `status.chifor.me` CNAME now exists and resolves (HTTP 200), adopted into
the cloudflare tofu module (#13). It is currently **public** (no Access app — typical for a status page).

## 13. Cloudflare edge as IaC — ✅ DNS done (2026-06-16), Access opt-in
New OpenTofu module `kubernetes/infra/cloudflare/` (`cloudflare/cloudflare` v5.20, local state) manages the
edge. **DNS:** the six proxied tunnel CNAMEs (home/sso/status/chat/grafana/api) were **adopted via import**
(`imports.tf`; `terraform apply` = 6 to import, 0 changes — no live DNS touched). **Access:** apps/policies/
service-tokens scaffolded in `access.tf.example` (opt-in; per the per-host policy only `status` [Allow] and
`api` [service token] are sensible, `sso` must never be gated, `home/chat/grafana` stay with Authelia). The
tunnel **ingress** stays locally-managed in `cloudflared.yaml` — deliberately NOT owned by tofu (a tunnel is
locally- OR remotely-managed, not both). Token (DNS + Access scopes, no Tunnel) via `CLOUDFLARE_API_TOKEN`
env; tfvars/state gitignored. Closes the ADR 0012 "codify Access/DNS in IaC" follow-up for DNS. Gotcha: the
DNS-records permission is **Zone-scoped** (`Zone → DNS → Edit`); under Account scope you only see DNS
Settings/Firewall/Views, which don't manage records.

## 12. Homepage behind Authelia SSO (oauth2-proxy) — ✅ DONE + e2e verified (2026-06-16)
`home.chifor.me` now routes cloudflared -> **oauth2-proxy** (homepage ns, OIDC client `homepage` of Authelia,
PKCE/S256, `consent_mode: implicit`) -> Homepage. Full login flow verified headlessly (Playwright): login →
callback → dashboard renders, no consent prompt. This **locks the Homepage**, so **Cloudflare Access on
`home` is now redundant** (would be a 2nd login). CF Access remains relevant only for the other public
hostnames (chat/grafana/api/status) and still needs the user's CF credentials/token. Authelia is still
`one_factor` — 2FA (TOTP/WebAuthn) is the next hardening step.
