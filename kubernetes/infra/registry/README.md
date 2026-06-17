# ailab private registry (Zot) — `registry.chifor.me`

Out-of-cluster private OCI registry for the Strive platform images. Runs on the
QNAP (Container Station) or a small Proxmox LXC — **not** in the K8s cluster, so
it survives cluster rebuilds and avoids an in-cluster bootstrap chicken-and-egg.

**Model:** anonymous **pull** (Talos nodes + the cluster pull with no creds over
a publicly-trusted Let's Encrypt cert) + authenticated **push** (CI runners log
in). Result: images are private to the LAN, with **no Talos machine-config
change** and **no K8s imagePullSecrets**.

## One-time setup

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
