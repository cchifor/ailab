# ADR 0007 — K8s storage (iSCSI+NFS) and internet exposure (Cloudflare Tunnel)

**Status:** accepted · **Date:** 2026-06-14

## Storage
- **Default RWO = QNAP iSCSI** via the official `qnap-dev/QNAP-CSI-PlugIn` (Trident-based; real ZFS
  snapshots/clones/expansion). Requires the Talos **`iscsi-tools`** (+`util-linux-tools`) system
  extension and a QNAP iSCSI target/LUN scripted via `qcli_iscsi`.
- **RWX = `csi-driver-nfs`** pointed at the already-live `pve-nfs` export (zero new QNAP work). Do not
  rely on its tar-based snapshots — snapshot the QNAP ZFS dataset instead.
- democratic-csi rejected (no first-class QNAP backend).
- Storage data path stays on `10.55.0.0/24` where possible (resolve VM-vs-fabric networking at the CSI
  sub-phase — see `docs/k8s-architecture.md`).

## Internet exposure
- **Cloudflare Tunnel** (`cloudflared`, ≥2 replicas) as the public path — egress-only, no open ports,
  hides the home IP, free WAF; fronts **Traefik** which does per-app routing in-cluster.
- **cert-manager** with the Cloudflare **DNS-01** solver issues a wildcard `*.chifor.me` (works behind
  the tunnel, no inbound). Single cert authority (not Traefik's built-in ACME).
- **Cloudflare Access** (Zero Trust, free ≤50 users) gates public hostnames at the edge.
- **Tailscale operator** for private/admin access (Grafana, Flux, Traefik dashboard, kube-api). Tailscale
  Funnel rejected for public (HTTPS-only, relay-limited); port-forward+DDNS is break-glass only.
- Free-tier caveat: HTTP/HTTPS only, ~100 MB body limit, ambiguous large-media TOS → route big-upload/
  media apps over Tailscale, LLM streaming text is fine on the public path.

## Observability (related)
kube-prometheus-stack + Loki (singleBinary/filesystem) + **Grafana Alloy** (Promtail EOL). GPU metrics
via `node_exporter` sysfs/DRM/hwmon — AMD's device-metrics-exporter reports all-N/A on gfx1151.

## Secrets
**SOPS + age** (one key encrypts in-cluster Secrets + bootstrap material). age private key backed up
offline, never committed.
