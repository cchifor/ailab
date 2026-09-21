# LLM Router — router.chifor.me

## Topology and authentication

Cloudflare proxied CNAME → existing locally managed `ailab` tunnel → `llm-router.llm-router.svc.cluster.local:80` → Node :8787.

The router owns mandatory bearer authentication on `/admin/v1/*` and `/v1/*`. The public SPA displays its sign-in screen; only `/health`, `/ready` and static assets are anonymous. This hostname is intentionally Access-free so standard OpenAI-compatible clients can authenticate with their API token, consistent with AILab's own-auth API convention. It is NOT a public inference relay. Trusted extension installation remains disabled. Only the offline mock account is enabled at initial deployment; no existing provider credentials or subscriptions were copied.

The administrator token is independently generated (48 random bytes) and stored in Kubernetes Secret `llm-router/llm-router-auth`, key `admin-token`. It is not in this repository, logs or the release archive. An authorized operator can retrieve it locally with:

```sh
kubectl -n llm-router get secret llm-router-auth -o jsonpath='{.data.admin-token}' | base64 -d
```

Enter it at the SPA; do not put it in a URL. It is privileged for both management and inference. Back up this Secret securely or rotate it deliberately; there is not yet an ESO/SOPS owner for it.

## Deployment

Manifests: `kubernetes/apps/apps/llm-router/` (wired into the apps Kustomization).

- One replica, `Recreate`; non-root UID/GID 1000, restricted Pod Security, dropped capabilities, no service-account token, read-only root filesystem, `/tmp` emptyDir.
- Node runtime pinned to `docker.io/library/node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6` (Node 24.21.0 at deployment).
- Requests 100m CPU/256Mi memory; limits 2 CPU/768Mi memory. May schedule on the existing `dedicated=agent` worker pool; no existing taints, cordons or workloads were changed.
- `llm-router-data`: 5Gi RWO `qnap-iscsi`, mounted at `/data`, SQLite `/data/router.sqlite`. **Never move WAL onto NFS.**
- `router-releases`: 2Gi RWX `nfs-csi`, **application code only**, versioned directory `router-0.1.0-20260921` mounted read-only at `/app`. All production dependencies were installed from the frozen pnpm lockfile before deployment. There is no dependency install at startup.
- NetworkPolicy admits :8787 only from `edge` cloudflared pods; egress permits cluster DNS and public HTTPS, excluding RFC1918. No live provider is enabled.

### Why a staged release rather than a new OCI image?

The current deployment identity could not read the registry push credential (OpenBao 403), and its own Gitea package upload was denied. Neither permission was bypassed. The initial deployment uses a digest-pinned public Node image plus the already-built release on read-only storage. A multi-stage Dockerfile is supplied in the application source for migration to a dedicated immutable image once an authorized build identity is available.

The exact deployed release, including production `node_modules`, was exported as `/workspace/llm-router-runtime-0.1.0.tar.gz` in the deployment session. Preserve that artifact and the source archive outside the session workspace. Restoring a fresh cluster requires restoring/staging the versioned release directory and the authentication Secret **before** applying the Deployment; Git alone does not contain the runtime payload. Never modify a mounted release directory in place: stage a new version, verify it, then change the `subPath` and roll the singleton.

## Cloudflare and GitOps ownership

- Tunnel ingress belongs to `kubernetes/apps/apps/edge/cloudflared.yaml`, not Terraform's remote-tunnel configuration. The `chifor.me/config-revision` annotation is bumped so both connectors roll after the route changes.
- DNS is a proxied CNAME `router.chifor.me` → `f93d9a6a-5172-43d3-8bef-13460ea7607b.cfargotunnel.com`, created with the existing scoped DNS token. No other DNS records were changed.
- `kubernetes/infra/cloudflare/variables.tf` includes `router`; `router-import.tf` adopts record `c54e2ae4bd38fa150c2f5928967478ae` into the operator's existing local state. Do not apply a new empty Terraform state against the whole estate.
- AILab `main` is protected. The deployment agent cannot push/merge main. Merge the deployment PR through the existing review gate; Flux then adopts the application resources and rolls the tunnel. Do not suspend shared reconciliation or patch the live tunnel to evade that gate.
- Before the PR merges, the application can be healthy internally while the public hostname returns the tunnel's catch-all 404. DNS creation alone does not expose the origin.

## Verification and operations

```sh
kubectl -n llm-router rollout status deploy/llm-router
kubectl -n llm-router get pods,pvc
kubectl -n llm-router logs deploy/llm-router --tail=30
curl -fsS https://router.chifor.me/health
curl -fsS https://router.chifor.me/ready
# Must return 401 without credentials:
curl -s -o /dev/null -w '%{http_code}\n' https://router.chifor.me/admin/v1/config
```

Pre-exposure checks passed inside the running pod: `/health`, `/ready` and SPA return 200; unauthenticated config/models return 401; authenticated Responses works with the offline `demo` model. Backend tests: 103; browser tests: 13; compiled production smoke and dependency audit passed before staging.

For backup, use SQLite's online backup API or stop the singleton and copy a consistent database together with any WAL; do not copy only a live `.sqlite` file. Retain the release archive and authentication Secret separately. The data PVC's storage class uses Retain, but neither that nor this manifest proves an offsite restore drill. Single-authority availability and live subscription integration remain explicit limitations of the first implementation.
