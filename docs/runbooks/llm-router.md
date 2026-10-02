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
- The application OCI image is pinned by digest in `router.yaml`; its CI receipt records the unchanged Node 26.10.0 base digest used for this release.
- Requests 100m CPU/384Mi memory; limits 2 CPU/1280Mi memory (Assistant image jobs: see the comment in router.yaml). May schedule on the existing `dedicated=agent` worker pool; no existing taints, cordons or workloads were changed.
- `llm-router-data`: 5Gi RWO `qnap-iscsi`, mounted at `/data`, SQLite `/data/router.sqlite`. **Never move WAL onto NFS.**
- `router-releases`: 2Gi RWX `nfs-csi`, retained for legacy release archives and rollback only. It is no longer mounted into the live router. The CI-built OCI image carries application code and frozen production dependencies; there is no dependency install at startup.
- NetworkPolicy admits :8787 only from `edge` cloudflared pods. Egress permits cluster DNS, public HTTPS (RFC1918 excluded), seat hosts in the namespace on :8791, and the mgmt LAN `192.168.0.0/24` on the model-server ports only (8080, 8081, 8082, 18020, 11434, 1234, 8000).

### Legacy staged-release procedure

For the current Gitea CI image flow, see the Relay Assistant API image release below. The earlier PVC-based release procedure remains available for recovery: a staged release is a new directory on `router-releases`, never a change to a mounted one.

1. **Build** `dist` from the merged `dsh/llm-router` main after **deleting `dist/` first**. The web build does not empty it, and old bundles pile up.
2. **Package** it: tar a directory named `router-0.1.0-<date>-<name>` containing `dist/`, `package.json`, `pnpm-lock.yaml` and `scripts/` (step 8 runs `scripts/validate-live.mjs` from the release; a release without it needs the script copied into the pod's `/tmp`).
3. **Stage** it:
   - Start the staging pod: `kubectl --context admin@ai apply -f kubernetes/apps/apps/llm-router/staging/router-stage.yaml`. It is not in the Flux kustomization.
   - Its `fsGroupChangePolicy: OnRootMismatch` keeps start-up to seconds. Without it the kubelet re-applied group ownership to every file on the NFS volume, which took up to 22 min.
   - Copy the tarball in, extract it under `/releases`, and run `npx -y pnpm@10.32.1 install --prod --frozen-lockfile --ignore-scripts --store-dir /releases/.pnpm-store`.
4. **Smoke-test against a copy of the live database.**
   - Back it up first with `VACUUM INTO /releases/.backups/<date>-pre-<name>/router.sqlite`, copying `router.secrets.key` and `plugins.yml` alongside.
   - Start the release on a spare port against a copy of that backup, with the production `ROUTER_PLUGIN_CONFIG`.
   - Check health, plugins, limits and anything new.
5. **Prune:** copy `staging/prune-releases.sh` into the pod and run it with the new release's name and the current live one (which becomes the rollback). It is a dry run until `--apply`. It keeps those two releases and the newest five backups, deletes other release directories, and prunes the pnpm store.
6. **Delete** the staging pod, **before** rolling. It mounts the RWO volumes, and the Deployment is `strategy: Recreate`: if the staging pod is still up, the new router pod waits on a Multi-Attach error while the old one is already gone. That caused a 7-minute outage on 2026-09-27. The pod has required affinity to the pod labelled `app: llm-router`, so it is scheduled onto the router's node. Affinity applies only at scheduling: if the router moves afterwards, the staging pod stays where it is. With no router pod running (for example a restore), the staging pod stays Pending; replace the affinity with a `nodeSelector` for that node by hand.
7. **Roll** it: a PR here bumping `subPath` (and the source-commit comment beside it) in `router.yaml`. After merge, annotate `gitrepository/flux-system` and `kustomization/apps` with `reconcile.fluxcd.io/requestedAt`, then `kubectl rollout status`.
   - **Before merging**, take a baseline on the release that is still live: `kubectl --context admin@ai -n llm-router exec deploy/llm-router -- sh -c 'cd /app && node scripts/validate-live.mjs --baseline'` (see step 8). If the live release predates the script, copy it in with `kubectl cp` first.
   - Record the result so step 8 can be compared against it.
8. **Validate end to end on the live pod** (from `router-0.1.0-20260929-subs` on): `kubectl --context admin@ai -n llm-router exec deploy/llm-router -- sh -c 'cd /app && node scripts/validate-live.mjs'`.
   - It sends real requests through the router's own API as the administrator, who is also the Claude owner: Codex text, streaming, a tool call (never executed), images to a vision model and refused for a non-vision one, Claude streaming, and (from `router-0.1.0-20260929-images` on) one 512x512 image from `qwen-image-2.1-fast-cloud`, checked to be a PNG of that size served by the `qwen-image` account. The step-7 `--baseline` run never includes it (it reports the check as skipped: the older release has no images API). On the first roll that adds it, configure the `qwen-image` account and its routes (admin API) before this step; until then run step 8 with `--skip-images "image routes not configured yet"`.
   - Each check names the account and model that actually answered, taken from the request's trajectory.
   - At most 9 small requests and 3 minutes. It restores the one setting it changes (`visionModels` on `codex-2`).
   - Exit code 0 means all checks passed. Compare the output with the baseline from step 7.
   - **If the run is interrupted** (the exec is killed, or the pod restarts) before it prints its summary, the temporary `visionModels` setting may still be in place. Check `codex-2`'s `visionModels` in `GET /admin/v1/config`. If it differs from before the run, put it back with `update_account` in the chat, or revert that config change from the history (`config_history`, then `revert_config`).
   - **Never run it on the staging pod:** Codex logins must be used by one router only.
   - **cloud2 is off at night** (Qwen-Image is day-only, ADR 0033). An unreachable image server FAILS the image check: a firewall, route or deployment fault looks the same. When the server is off on purpose, say so: `--skip-images "cloud2 powered off (night)"` reports the check as skipped, with that reason.

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


### One-shot network preflight for the admin bridge (done, removed)

AILab#901 ran `router-network-preflight-20260927` in production under the router's own
NetworkPolicy. It completed on 2026-09-28 at 00:24Z with
`{"cluster_dns":true,"registry_dns":true,"registry_https":true,"pnpm_archive_integrity":true,"result":"passed"}`.
That was the recorded release-gate evidence #900 asked for. The Job, its kustomization entry and the
extra `llm-router-preflight` value in the NetworkPolicy selector were removed on 2026-09-29, once the
result was recorded here. For another run, re-add it from #901 under a new versioned Job name.


## Relay Assistant API image release (2026-10-02)

Gitea CI in `cchifor/relay` now publishes the runtime through the existing
organization registry identity. The input archive contains merged router source
`0013627e5c6511b21066ea8dea803ab4fde9f96a` (PR #65), a clean build and frozen
production dependencies. CI verifies archive SHA-256
`de1067351cc7bf5af27226de45fcc27b3e816700d0d07e6078f7fd530771bd49`, the source
marker, the offline production smoke, and the backup helper on the exact runtime
image. The runtime stays on the current Node 26.10.0 digest; this rollout does
not change its Node version. Image digests and source provenance are attached to
[Relay v0.2.0](https://git.chifor.me/cchifor/relay/releases/tag/v0.2.0) in the
`deployment-images-<run>.json` CI receipt.

The reviewed Flux Deployment runs the published image directly, with no code PVC
mount or package installation. `router-releases` and its previous
`router-0.1.0-20261002-clear` directory remain available for rollback. Persistent
SQLite, the existing authentication Secret, provider credentials and network
policy retain their current locations and ownership.

The Recreate strategy stops the old process before the `backup-before-relay`
init container runs. It uses SQLite VACUUM INTO to create
`/data/backups/pre-relay-api-20261002/router.sqlite`, copies `router.secrets.key`
and any `plugins.yml`, checks SQLite integrity and SHA-256, and atomically
publishes a COMPLETE marker. Retries verify the completed generation; corruption
fails startup. It does not contact providers or need a Kubernetes API token.
The backup remains on the data PVC and is local rollback material, not proof of
an offsite backup. Preserve the external authentication Secret separately.

This release preserves `/clear` and adds eligible model/route capabilities and
bounded authenticated PNG edits at `/v1/images/edits`. There is no router schema
or credential change. After normal protected-main review and Flux rollout,
verify public readiness, the new SPA assets and authenticated capability
discovery with Relay's existing scoped key (`x-agent-id: relay-assistant`), then
roll Relay. A failed init backup keeps the new router stopped: inspect storage
and restore only through the established recovery procedure. Application rollback
uses the previous Node image and code subPath via a reviewed PR; never rewrite
a live directory or run a second control plane against the same database.

Published by [Gitea CI run 57525](https://git.chifor.me/cchifor/relay/actions/runs/57525):
`registry.chifor.me/llm-router/router@sha256:12697b84cc68e097b7dc11e570ad8a192cd1220881fb324e2a5b8e468c3aa30e`.

The registry permits anonymous OCI pulls (`ansible/roles/registry_zot` read
policy). The exact router digest was exported with regctl using empty registry
and Docker credential configurations, verifying the manifest, config and all
layers; no imagePullSecret is required.

The dated backup init container is temporary. After public acceptance, remove
`backup-before-relay` through a follow-up reviewed PR, preserving its completed
backup on the data PVC. That cleanup prevents future restarts from depending on
an old backup's retention or integrity. The cleanup causes one more Recreate
rollout; verify readiness again. Retain the recovery generation until a newer
consistent backup is validated, then prune deliberately during maintenance.
