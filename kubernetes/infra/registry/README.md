# ailab private registry (Zot) — `registry.chifor.me`

Out-of-cluster private OCI registry for the Strive platform images. Runs on the
QNAP (Container Station) or a small Proxmox LXC — **not** in the K8s cluster, so
it survives cluster rebuilds and avoids an in-cluster bootstrap chicken-and-egg.

**Model:** anonymous **pull** (Talos nodes + the cluster pull with no creds over
a publicly-trusted Let's Encrypt cert) + authenticated **push** (CI runners log
in). Result: images are private to the LAN, with **no Talos machine-config
change** and **no K8s imagePullSecrets**.

## Deployment: Proxmox LXC via IaC (the live path)

The registry runs on a dedicated **unprivileged Proxmox LXC** (`registry.chifor.me` →
`192.168.0.36`, vmid 5004 on ai-node1), provisioned + configured entirely from this repo:

- **tofu** `kubernetes/infra/registry-lxc/` — creates the LXC (2 vCPU / 2 GiB, 16 GiB root + a
  **resizable 64 GiB `mp0` data disk** at `/var/lib/registry`). `just registry-plan` / `registry-apply`.
- **ansible** `ansible/roles/registry_zot/` (`ansible/registry.yml`, `just registry`) — installs Zot
  **natively** (binary + systemd on :443 via `CAP_NET_BIND_SERVICE`), obtains the LE cert via
  **certbot DNS-01** (Cloudflare token, auto-renewing + deploy-hook), and renders `config.json` + the
  bcrypt `htpasswd`. Secrets: `ansible/secrets/registry.sops.yaml` (`registry_ci_password`,
  `cloudflare_dns_api_token`).
- **DNS** `kubernetes/infra/cloudflare/registry-dns.tf` — `registry.chifor.me` A → `192.168.0.36`,
  **DNS-only** (grey-cloud) so the Talos nodes + CI resolve it on the LAN.

Grow the image store later: `pct resize 5004 mp0 +NG` (online) or bump `data_gb` + `tofu apply`.

The `config.json` + `docker-compose.yml` below remain the **QNAP Container Station** alternative
(Docker, 443→5000 port-map); the LXC role templates the same policy to bind :443 natively.

## One-time setup (QNAP / docker-compose alternative)

1. **Storage:** create a data dir on the host (QNAP: a ZFS/iSCSI share, e.g.
   `/share/registry/data`); point the compose `registry-data` volume at it.
2. **TLS cert** for `registry.chifor.me` (publicly trusted so Talos/containerd
   accept it with no extra config). Easiest is Let's Encrypt **DNS-01** via your
   Cloudflare token:
   ```bash
   certbot certonly --dns-cloudflare \
     --dns-cloudflare-credentials ~/.secrets/cloudflare.ini \
     -d registry.chifor.me
   cp /etc/letsencrypt/live/registry.chifor.me/fullchain.pem ./certs/tls.crt
   cp /etc/letsencrypt/live/registry.chifor.me/privkey.pem   ./certs/tls.key
   ```
   (Automate renewal; Zot reloads on cert change or restart.)
3. **Push credentials** (bcrypt htpasswd for the `ci` user referenced in
   `config.json`):
   ```bash
   htpasswd -Bbn ci '<strong-password>' > ./htpasswd
   ```
4. **DNS:** add a Cloudflare **DNS-only (grey-cloud)** A record
   `registry.chifor.me` → the registry host's **LAN IP** (e.g. 192.168.0.x).
   This resolves on the Talos nodes (they use the same resolver) and keeps the
   registry on the LAN — do **not** route it through the Cloudflare tunnel.
5. **Run:** `docker compose up -d` (from this directory). Verify:
   `curl -fsSL https://registry.chifor.me/v2/ -u ci:<pass>` → `{}`.

## Wire the rest

- **CI (platform repo):** set repo secrets `REGISTRY_USERNAME=ci` +
  `REGISTRY_PASSWORD=<pass>`. `.github/workflows/build.yml` logs in to
  `registry.chifor.me` and `docker-bake.hcl` pushes `registry.chifor.me/strive/*`.
- **Cluster:** nothing — the ailab overlay already points
  `global.imageRegistry=registry.chifor.me`; nodes pull anonymously over TLS.
- **First images:** trigger the platform `build.yml` (push to main) or, from a
  LAN box: `docker login registry.chifor.me && docker buildx bake -f docker-bake.hcl --push`.

## Optional hardening: authenticated pull

To require auth on pulls too, remove `"anonymousPolicy": ["read"]` from
`config.json` and add node-level creds in Talos
(`kubernetes/infra/machine-config/controlplane.yaml.tftpl`):
```yaml
  registries:
    config:
      registry.chifor.me:
        auth: { username: ci, password: <pass> }   # thread via a Tofu var
```
then `talosctl apply-config` / `tofu apply` to roll it to the nodes.
