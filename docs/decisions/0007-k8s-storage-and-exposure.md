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

### Amendment 2026-06-18 — admin UIs moved to the WAN (PR #24)
The original split kept **Proxmox, QNAP, Prometheus, and Alertmanager Tailscale-only**. They are now
**published via the tunnel + Cloudflare Access** (`kubernetes/infra/cloudflare/access.tf` `admin_uis`;
ingress in `cloudflared.yaml`), gated default-deny to `allow_email`. Tailscale still provides private
L3 reach for SSH / kubectl / API.

**Accepted risks + threat model:**
- **Proxmox / QNAP** have their own logins → Access is **defense-in-depth**; 8h Access session.
- **Prometheus / Alertmanager** have **no native auth** → Access is the **sole** gate. An Access
  identity compromise can read all metrics and **silence every alert** (all alerts route to ntfy, so it
  also blinds detection). Mitigations in place: ClusterIP-only origins (no direct-origin bypass),
  Prometheus admin/lifecycle APIs disabled, and a tight **30m** Access session. The gate is
  **single-factor** (email OTP) — accepted for a solo operator, for now.

**Hardening roadmap (tracked here; partially codified):**
1. **MFA** for the no-native-auth UIs — wire an IdP that enforces MFA (e.g. Authelia-as-Access-IdP, see
   `docs/runbooks/cloudflare-access-apps.md`) and add a `require` rule, or move Alertmanager back onto
   the Tailscale admin mesh. *Not codified yet:* email OTP is the only login method until an IdP exists,
   so a `require` rule now would lock out the sole identity.
2. **Origin TLS** — replace `noTLSVerify: true` on the Proxmox/QNAP tunnel origins with the pinned LAN
   CA via `originRequest.caPool` + `originServerName`, so cloudflared verifies the origin before
   forwarding admin credentials. *Needs the origins' CA material extracted + mounted into cloudflared.*

*Codified now:* per-app Access session durations (8h own-auth / 30m no-native-auth) and the doc/threat-model
sync (this ADR + `internet-exposure.md` + `cloudflare-access-apps.md`).

## Observability (related)
kube-prometheus-stack + Loki (singleBinary/filesystem) + **Grafana Alloy** (Promtail EOL). GPU metrics
via `node_exporter` sysfs/DRM/hwmon — AMD's device-metrics-exporter reports all-N/A on gfx1151.

## Secrets
**SOPS + age** (one key encrypts in-cluster Secrets + bootstrap material). age private key backed up
offline, never committed.
