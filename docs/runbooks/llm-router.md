# LLM Router — router.chifor.me

Current deployments use Gitea CI and reviewed GitOps changes; no llm-router
kubeconfig is required or issued. The source is merged in Gitea, mirrored to
GitHub, then reconciled by Flux. The initial manual deployment notes below are
historical. See the provider-naming release section for the current release and CI acceptance.

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
- NetworkPolicy admits :8787 only from the `llm-router-edge` pods (cloudflared reaches the router through them). Egress permits cluster DNS, public HTTPS (RFC1918 excluded), seat hosts in the namespace on :8791, and the mgmt LAN `192.168.0.0/24` on the model-server ports only (8080, 8081, 8082, 18020, 11434, 1234, 8000).

### Legacy staged-release procedure

For the current Gitea CI image flow, see the Relay Assistant API image release below. The earlier PVC-based release procedure remains available for recovery: a staged release is a new directory on `router-releases`, never a change to a mounted one.

1. **Build** `dist` from the merged `cchifor/llm-router` main after **deleting `dist/` first**. The web build does not empty it, and old bundles pile up.
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
   - It sends real requests through the router's own API as the administrator: Codex text, streaming, a tool call (never executed), images to a vision model and refused for a non-vision one, Claude streaming, and (from `router-0.1.0-20260929-images` on) one 512x512 image from `qwen-image-2.1-fast-cloud`, checked to be a PNG of that size served by the `qwen-image` account. The step-7 `--baseline` run never includes it (it reports the check as skipped: the older release has no images API). On the first roll that adds it, configure the `qwen-image` account and its routes (admin API) before this step; until then run step 8 with `--skip-images "image routes not configured yet"`.
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

## Upgrades: edge hold and drain

The router is a single replica on an RWO volume (`Recreate`), so every rollout has a gap with no router pod. The
`llm-router-edge` Caddy proxy hides that gap from clients. Manifests: `kubernetes/apps/apps/llm-router/edge.yaml`.
Design: llm-router `docs/design/live-status-and-upgrades.md`, section 5a.

**Status: the edge is on the tunnel.** The `router.chifor.me` rule in `edge/cloudflared.yaml` points at
`llm-router-edge`, so all public traffic passes through the hold. If the router looks unreachable, check the edge
pods first (`kubectl --context admin@ai -n llm-router get deploy,pdb,svc llm-router-edge`).

**The path:** cloudflared (namespace `edge`) -> `llm-router-edge:80` (Caddy, 2 replicas, port 8080) ->
`llm-router:80` (container port 8787).

**What is held, and for how long:** Caddy retries dial failures and "no healthy upstream" every 250 ms, for 85 s,
or for 10 s on `/v1/images/*`. The 85 s cover the longest measured gap: 76–79 s on 2026-10-10/11, when the old
shutdown waited its full 60 s grace on open streams. The first rollout behind the edge still replaces such a pod.
With the router's drain the gap is about 50 s at most: 20 s drain, up to 12 s cleanup, up to 5 s for the journal's flush and the runtime's close, about 13 s to ready (35 s measured on 2026-10-12, with streams still running at the deadline). 85 s
also leaves room under Cloudflare's roughly 100 s time-to-first-byte for a held stream, which gets its first byte
(`: ok`) as soon as the router selects an account (the router already gives images a 90 s execution budget, and a longer hold would
let Cloudflare time out first). Active health checks poll the router's `/ready` every 3 s, with a 3 s timeout; two failures in a row mark it down, so a draining router (503 at once) is marked down within about 6 s, a hung one within about 9 s, and a recovered one is marked up within 3 s. (Until the follow-ups release of 2026-10-12, `/ready` waited behind the router's SQLite worker, and a heavy admin query such as the week usage took 2.7 s on 2026-10-11; with a 2 s timeout every Status page load marked the router down for about 2 s, which is why the timeout is 3 s. Since that release `/ready` asks the worker whether it is alive and moving (opened, not failed, no call waiting past 10 s) without a round trip, so admin queries no longer delay it; a hung worker turns it 503 after 10 s, so the edge marks the router down about 16 s after the first call the worker holds. The check cannot tell a hang from a long query, so an admin query that itself runs 10 s or more also turns `/ready` 503 until it ends; the edge then holds new requests (up to 85 s) while requests in flight continue. The slowest measured is 5.2 s (the Status page's usage under load); before that release any query over 3 s did this. The bound is `stallMs` in llm-router's `SqliteStore.responsive()`: raising it trades slower hang detection for longer admin queries. The interval is not shorter than the timeout, so probes never pile up; a single slow probe never marks the router down, as that takes two in a row.) A request that was never sent
(dial failure) is safe to hold and retry. After the router has accepted a request, Caddy may replay a GET within
`lb_try_duration`, but never a POST (its default retry match is GET-only), so a held POST was never sent.
Caddy's own readiness is a TCP probe, so it does not flap with the router.

**The router's drain:** on SIGTERM the router stops taking new work and drains in-flight requests for
`ROUTER_DRAIN_MS`. Streams still open when it ends are cut and recorded as `cancelled` with reason `SHUTDOWN`;
the router writes those journal entries before it exits (for at most 3 s; then `runtime closed in … ms`, `drained in … ms`).
Work found unfinished at the next start is marked "Interrupted by restart". A watchdog exits the process at
`ROUTER_DRAIN_MS` + 17 s (37 s), which must stay below `terminationGracePeriodSeconds` (60); the router refuses a
drain above 40000 ms (a 57 s watchdog).

**What clients see:**
- A request that arrives during the gap waits (up to the hold above) and is then served by the new pod. If the gap is
  longer than the hold (a router that takes more than 85 s to become ready, for example after a volume moves node),
  the request gets a 502 when the hold ends; Codex's request retries then wait again behind the edge.
- A stream cut by the drain makes Codex print `Reconnecting… n/5` and continue.
- A POST the router accepted and then lost to a reset (accepted-then-reset) reaches the client as a 502, because a
  sent POST is never replayed; Codex's request retries cover it. A GET in the same situation may be replayed by Caddy
  within `lb_try_duration`.
- A long non-streaming request held near the limit can still hit Cloudflare's edge timeout (about 100-125 s).
  That is the same as before the hold.

**How to check after a rollout:** read the router journal for the rollout window. There must be no `abandoned`
entries; only `cancelled` / `SHUTDOWN` for streams the drain cut.

**To restart the router** (for example to verify a drain), send its process SIGTERM inside the container:

```sh
# 1. PID 1 must be the router itself: this prints `node dist/apps/router/main.js`.
kubectl --context admin@ai -n llm-router exec deploy/llm-router -c router -- sh -c 'tr "\0" " " < /proc/1/cmdline; echo'
# 2. Watch the drain, then signal it.
kubectl --context admin@ai -n llm-router logs -f deploy/llm-router -c router &
kubectl --context admin@ai -n llm-router exec deploy/llm-router -c router -- sh -c 'kill -TERM 1'
```

- **If PID 1 is anything else** (a shell or an init wrapper in a future image), signal the node process's own PID
  instead. Find it with its own exec, `kubectl ... exec deploy/llm-router -c router -- pgrep -f 'dist/apps/router/main[.]js'`;
  the `[.]` keeps the pattern from matching a wrapping `sh -c` command line. Then run
  `kubectl ... exec ... -- kill -TERM <pid>`. A wrapper that does not forward SIGTERM gives no drain and leaves the
  container running, and nothing reports it. The log must show `SIGTERM, draining`. Checked 2026-10-12: PID 1 is node,
  the image has `sh`, `kill` and `pgrep`, and that lookup prints `1`.
- **What happens:** the router drains exactly as on any SIGTERM. Its watchdog bounds that at `ROUTER_DRAIN_MS` + 17 s.
  Then it exits, and the kubelet restarts the container in the same pod (`restartPolicy: Always`, after a back-off of
  about 10 s), with the same volume.
- **What traffic sees:** the router closes its listener at SIGTERM, so new connections to the pod are refused from the
  start, and the edge holds them (dial failures are retried for up to 85 s), as in a rollout.
  - The pod stays in the Service until its readiness probe fails (`/ready`, 5 s × 3, so within about 15 s), while a
    rollout removes it soon after SIGTERM. The endpoints therefore change later, but clients are held all the same.
  - The edge's own health check marks the router down within about 6 s.
  - Once the new process is ready, the probe passes and the endpoint returns.
- **Probe events:** the liveness probe (`/health`, 10 s × 3) can fail during a long drain and log "will be restarted".
  The kubelet's kill is then a second SIGTERM, which the drain ignores (`SIGTERM while draining: ignored`). Its SIGKILL
  comes only after the 60 s grace, after the watchdog.
- **What to check:** the pod's RESTARTS count goes up by one, and `kubectl logs --previous` shows the drained process.
  The backgrounded `logs -f` ends when the drained container exits: run it again to follow the new process.
  The Deployment does not change, so Flux has nothing to undo, and no second router can start.
- **Not `kubectl rollout restart`:** it adds a `restartedAt` annotation to the pod template. Flux removes the
  annotation at its next apply of `apps`, which restarts the router a second time. This was seen on 2026-10-12 at 07:42
  and 08:51, about 10 minutes after each deliberate restart.
- **Not `kubectl delete pod`:** the ReplicaSet starts a replacement at once, outside the `Recreate` strategy. On the
  same node, that would run two routers on one SQLite volume while the old one drains.
- **Not scaling to 0 and back:** if Flux re-applies `replicas: 1` while the old pod still drains, the same second
  router starts.
- This procedure had not been exercised yet when it was written (2026-10-12).

**An edge rollout** lets open streams finish (Caddy `grace_period 90s`, `preStop` sleep 5, `terminationGracePeriodSeconds: 100`);
new connections go to the other replica (`maxUnavailable: 0`, PDB `minAvailable: 1`). If you change the Caddyfile
in ConfigMap `llm-router-edge-caddy`, bump the `chifor.me/caddyfile-revision` pod annotation in the same change, or the
pods keep the old config.

**Rollback:** in `kubernetes/apps/apps/edge/cloudflared.yaml`, change the `router.chifor.me` rule's `service:` back to
`http://llm-router.llm-router.svc.cluster.local:80` and change the pod-template annotation
`chifor.me/config-revision` (currently `"2026-10-11-llm-router-edge"`) to a new value, so both connectors roll.
The router NetworkPolicy no longer admits cloudflared on 8787, so in the same change add this rule back to the
`llm-router` NetworkPolicy's `ingress:` list in `kubernetes/apps/apps/llm-router/router.yaml`:

```yaml
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: edge
          podSelector:
            matchLabels:
              app.kubernetes.io/name: cloudflared
      ports:
        - protocol: TCP
          port: 8787
```

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

The dated `backup-before-relay` init container is removed after public acceptance.
The completed recovery generation remains on the data PVC, while future
restarts no longer depend on its retention or integrity. The cleanup causes
one more Recreate rollout; verify readiness again. Retain the recovery
generation until a newer consistent backup is validated, then prune deliberately
during maintenance.

### Router image `router-0.1.0-20261003-ui` (2026-10-03)

Web UI fixes only: scrollbars without arrow buttons (llm-router #69); History closes
on a press outside it, and a long page no longer slides the app up (#75). Source:
llm-router `release/router-20261003-ui` at `867272e581aa5d6357a7abadf6220ede922918ae`,
which is the live `0013627` plus those PRs' commits only: the later unreleased `main`
work (#66-#68, #70-#72, #76-#78) is not in it. The runtime archive
`router-0.1.0-20261003-ui.tar.gz` (SHA-256
`f6d748ec2c11f79ba8b0316f196622a0f3789ec75eb8cb9aa2b4f84723166010`, the same layout as
`router-0.1.0-20261002-relay`) is a v0.2.0 release asset; `releases.json` on the relay
branch `router-image-20261003-ui` names it (router entry only). Published by
[Gitea CI run 58874](https://git.chifor.me/cchifor/relay/actions/runs/58874):
`registry.chifor.me/llm-router/router@sha256:083ef7c7f931358b53e404b68b49c14a16d33eb6ecbcd55dd7e0968512cdb74b`
(receipt `deployment-images-58874.json`). Before the roll: a verified backup in
`/data/backups/pre-ui-20261003` (VACUUM INTO, integrity ok, key and plugins.yml) and a
`validate-live --baseline` (4/4). Rollback: the previous digest
`registry.chifor.me/llm-router/router@sha256:12697b84cc68e097b7dc11e570ad8a192cd1220881fb324e2a5b8e468c3aa30e`
(run 57525) with source-commit `0013627e5c6511b21066ea8dea803ab4fde9f96a`.


### Shared subscription routing release (2026-10-07)

Release `router-0.1.0-20261007-shared-routing` uses source
`0057394cf9d606c9648e885fa2f895d0f4362bd4` from
[llm-router #83](https://git.chifor.me/cchifor/llm-router/pulls/83).
It is based on the preceding production release and removes the caller ownership
restriction from subscription routing, setup, catalogs, administration and UI.
The same implementation is merged to main in
[llm-router #84](https://git.chifor.me/cchifor/llm-router/pulls/84).
Namespace, access-kind and route-scoped key authorization still apply. Claude's
existing single-turn/no-tools adapter capabilities are unchanged.

The runtime archive is `router-0.1.0-20261007-shared-routing.tar.gz`, SHA-256
`016202a5d9b6efb8cc1d61f41bf5be4139324889dc50bbf1928263f0d73d1108`, attached to
[Relay v0.2.0](https://git.chifor.me/cchifor/relay/releases/tag/v0.2.0).
The image publisher verifies the source marker and archive checksum, then runs
the compiled production smoke, backup tests and isolated migration test under
the same Node 26.10.0 base digest and restricted container settings.
[CI run 70582](https://git.chifor.me/cchifor/relay/actions/runs/70582) passed and
published image
`registry.chifor.me/llm-router/router@sha256:40478a235364dbf930c4b3d8d2e3d8defeaf271fb7eacedf813c3a6d7addc353`.
[The immutable receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-70582.json)
records source and packaging commit `d4a2844e5684228c53dbfdb5789c2fdbc072d78f`.
Application validation: hotfix 941/941 tests; main 1,232 passed and one skipped;
typecheck, compiled production smoke and browser coverage passed. The known
architecture exception is unchanged (#79).

After the old singleton stops, `backup-before-shared-routing` runs the packaged
backup helper with `ROUTER_BACKUP_LABEL=pre-shared-routing-20261007`.
It creates and verifies `/data/backups/pre-shared-routing-20261007`, including
SQLite, the encryption key and any plugin composition file. A second init
container runs `deploy/check-router-upgrade.mjs` against a temporary copy of that
backup. It boots only storage and configuration: no provider plugins, saved
extensions, network listeners or token refresh. It checks that only obsolete
account owner fields and token bindings are removed, the configuration revision
advances once, routes and encrypted credentials are preserved, and a second
startup is idempotent. Either init failure prevents application startup. The
application then applies the same migration to its normal database.

Deployment and acceptance use CI/GitOps:

1. The image publishing workflow produces an immutable digest and receipt.
2. A reviewed AILab PR pins it; CI and the protected merge gate run normally.
   Gitea mirrors main to GitHub and Flux reconciles the change.
3. `.gitea/workflows/router-acceptance.yaml` runs on main changes to the router
   manifests (or workflow dispatch). `scripts/check-router-rollout.py` reads the
   existing LAN Prometheus API, rejects scrape samples older than 90 seconds,
   and requires exactly one Ready router on the expected image, successful init
   checks while configured, and successful completion of the configured canary.
   It also checks public health/readiness and the unauthenticated 401. CI has no
   Kubernetes credential and no router administrator token.
4. The temporary GitOps Job `router-shared-routing-20261007` runs the packaged
   `validate-shared-routes.mjs` against the live service. It waits for the exact
   release's web document and readiness before testing. The existing
   `llm-router-auth` Secret is referenced only inside the cluster. The Job has no
   service-account token, persistent volumes or external egress. A temporary
   NetworkPolicy admits only its connection to the router and DNS.

The canary creates a temporary key scoped to the `claude` and `codex` routes,
checks visibility, requests streaming Chat Completions and nonstreaming
Responses, and verifies the serving provider in each trajectory. It fails if a
fallback masks a broken route and revokes its temporary key in cleanup. It runs
once, with no Job retries and a 420-second deadline; no TTL is set because Flux
would recreate the Job and repeat the calls. The script has its own 240-second
request deadline and 15-second cleanup timeout. Its success/failure is recorded
in Prometheus and sanitized request/cleanup receipts in Loki under
`{namespace="llm-router", container="canary"}`. Existing LAN endpoints are
Prometheus `http://192.168.0.41:30090` and Loki `http://192.168.0.41:30310`.
A process killed without cleanup can leave a key active for at most one day;
revoke its `shared-route-canary-` name prefix through the admin UI if needed.

The backup helper never overwrites a completed generation. Pod recreation with
the same backup label rechecks its COMPLETE marker, SQLite integrity and file
hashes, and reuses it; the migration check uses a fresh temporary copy. The
original pre-upgrade recovery set remains intact. A corrupt backup prevents
startup. Remove the dated init containers after acceptance so later Pod starts
do not depend on that generation's retention.

Production rollout evidence on 2026-10-07: pod `llm-router-6c644f6fbd-hk2lg`
was Ready on the pinned image; both init containers completed. Loki recorded
`Router SQLite backup and associated files verified: pre-shared-routing-20261007`
and the successful isolated migration/idempotence check. Public web asset names
matched the release, `/health` and `/ready` returned 200, and unauthenticated
admin config returned 401. Live acceptance also passed in GitOps Job
`router-shared-routing-20261007` (pod `router-shared-routing-20261007-mvh7k`),
with the CI observer at [run 70799](https://git.chifor.me/cchifor/ailab/actions/runs/70799).
The temporary client key was revoked. Each response's trajectory confirmed the
expected serving provider:

| Route | Protocol | Serving account | Trajectory |
| --- | --- | --- | --- |
| claude | streaming Chat Completions | claude-3 | dd2d8e53-4f24-4f40-8676-df45844aab36 |
| claude | Responses | claude-4 | d5f1246f-f124-4259-be18-294c6321ee5c |
| codex | streaming Chat Completions | codex | 01257e95-8577-4083-8ca9-bf91d3e6fd6b |
| codex | Responses | codex-2 | dee8e508-365f-4633-8142-c31d9aef30a2 |

The acceptance Job, its temporary network rules and dated init containers are
removed after this result. The verified backup remains on the data PVC. The
reusable CI workflow remains, and verifies the new Ready Pod has the expected
image and the current init-container composition; the old pre-cleanup Pod cannot
satisfy that check.

Rollback requires the matched pre-upgrade state as well as the old image, since
the old release enforces the removed fields. Through the existing GitOps review
and recovery procedure, stop the singleton, retain a consistent post-upgrade
snapshot, and restore the verified pre-upgrade SQLite/key/plugins set while no
router process is running (remove stale WAL/SHM sidecars only after shutdown).
Restore image
`registry.chifor.me/llm-router/router@sha256:083ef7c7f931358b53e404b68b49c14a16d33eb6ecbcd55dd7e0968512cdb74b`,
source annotation `867272e581aa5d6357a7abadf6220ede922918ae`, release annotation
`router-0.1.0-20261003-ui`, and remove both new init containers before restarting.
This restores application state to the backup timestamp; check any credentials
that rotated after that timestamp and reauthenticate if necessary.

For a future release, add a new uniquely named canary Job and remove it through
a CI-reviewed cleanup after recording its result. Retain the verified recovery
generation until a newer consistent backup has been validated. The existing data PVC, authentication Secret and network
policy remain in place.


## Plugin-settings release (2026-10-07)

`router-0.1.0-20261007-plugin-settings` deploys merged llm-router main
`6ebe437da928efcb50b6bbcfbb187802fb3fa00b` ([PR #85](https://git.chifor.me/cchifor/llm-router/pulls/85)).
Plugin rows expand into schema-backed settings; Claude has a live Print/SDK-resume
selector. Print remains the default. SDK-resume requires Claude CLI 2.1.281 or
2.1.283; existing accounts must explicitly enable tools. Deployment-owned settings
remain locked. No provider account, credential or transport selection is changed
by this rollout. This release includes the agent and web refactors already merged
on main after the previous focused release; it does not add a SQLite schema migration.

[Source CI 71407](https://git.chifor.me/cchifor/llm-router/actions/runs/71407) passed
unit/integration, browser, production build and real pinned Claude CLI loopback
checks. [Image CI 71420](https://git.chifor.me/cchifor/relay/actions/runs/71420) verified
the frozen production archive, production smoke, the read-only settings canary
against an offline router, backup integrity and isolated migration fixtures under
the exact image's non-root/read-only container constraints.

- Image: `registry.chifor.me/llm-router/router@sha256:0c1f9af1d3e0ee8bccc34a87ddd204d6500864c124e13844cd8346a81a127d44`.
- Runtime: unchanged Node 26.10.0 digest `sha256:a723b54c35a76e947095a20a67d39585bb09c862e6b1adeb8a9f518f95e34fb0`.
- Archive SHA-256: `6f904242d1e22c0ccfe80a3c91fdf4b856e012fea63222be4c8f86713d3f74fe`.
- [Immutable image receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-71420.json).

After the old singleton stops, `backup-before-plugin-settings` captures SQLite,
`router.secrets.key` and `plugins.yml` in `/data/backups/pre-plugin-settings-20261007`.
The next init container checks an isolated copy twice, asserting preserved router
configuration and encrypted credentials. Neither check starts provider plugins.
A failure prevents the new process from starting. The external admin Secret is
retained separately; the local backup is not an offsite recovery guarantee.

The one-shot `router-plugin-settings-20261007` Job waits for this exact release's
SPA and readiness, then verifies all installed plugins' configuration views,
ETags, no-store responses, authentication refusal, both Claude transport choices,
active Print, restart modes, router-owned settings and deployment locks. It makes
only GET requests and logs no credentials or configuration bodies. Its token
stays in the existing in-cluster Secret reference. The temporary NetworkPolicies
allow only DNS and router traffic, with no Kubernetes API access or data mount.

The existing **Router rollout acceptance** CI workflow observes the pinned image,
successful init checks and canary Job through fresh monitoring samples, plus
public readiness and unauthenticated management refusal. It needs no kubeconfig.
After acceptance, remove the dated init containers, canary and temporary ingress
through a reviewed cleanup PR; preserve the completed backup. Verify the cleanup
rollout again. Record both CI results here.

Rollback uses a reviewed GitOps change to previous image
`registry.chifor.me/llm-router/router@sha256:40478a235364dbf930c4b3d8d2e3d8defeaf271fb7eacedf813c3a6d7addc353`
and its source/release annotations. Before rolling back after any settings edits,
review the saved `plugins.yml` against the previous version; restore the captured
composition if necessary, retaining the newer copy for recovery. Stop the singleton
before restoring persistent files and retain subsequent data deliberately.


Production acceptance passed in [CI 71438](https://git.chifor.me/cchifor/ailab/actions/runs/71438)
after [deployment PR #1128](https://git.chifor.me/cchifor/ailab/pulls/1128), merge
`245285233b98b97060be199c717a21cc8d44e2ca`. The new pod
`llm-router-54d8d687dc-fh58k` was Ready on the expected image; both init checks
completed. Loki recorded the verified `pre-plugin-settings-20261007` backup,
preserved accounts/routes/encrypted credentials and idempotent restart. The
one-shot Job passed all 41 plugin settings views at 2026-10-07 13:56:35 UTC,
with active Print, required authentication and deployment locks. Public readiness
and management refusal passed too. No production inference or settings writes
were used by this canary; SDK tools were tested with the real pinned CLIs against
loopback fixtures in source CI.

The dated init containers, completed canary Job and temporary network allowance
are removed after that acceptance. The immutable image, persistent backup and
reusable CI observer remain. The resulting Recreate rollout must pass the same
Ready/image/public checks with no init containers or canary expected.


## Provider-naming release (2026-10-07)

`router-0.1.0-20261007-provider-names` deploys merged llm-router source
`0974a7a00160ba1961fe4722af5c758f88610ea8` ([PR #86](https://git.chifor.me/cchifor/llm-router/pulls/86)).
Provider rows now identify API-key and subscription access. The external Claude
connector appears as `provider-claude-bridge`, described as connecting to an
external Claude Code service that manages the login and runs requests. Claude's
conversation choices appear as **Single-turn** and **Multi-turn**.

This is a presentation change: stable plugin IDs, provider kinds, saved transport
values, routes, credentials and the SQLite schema remain unchanged. The deployment
preserves the selected transport. There is no data migration or one-shot init/Job;
the existing verified backup remains available. The usual single-replica Recreate
rollout briefly interrupts service while replacing the pod.

[PR CI 71588](https://git.chifor.me/cchifor/llm-router/actions/runs/71588) passed
before merge. A clean merged-source build, frozen production dependency install,
production smoke and offline authenticated settings acceptance also passed locally.
[Image CI 71933](https://git.chifor.me/cchifor/relay/actions/runs/71933) validates the
exact image with all six provider display names, the bridge description, both
conversation labels and all 41 plugin settings views before publishing it. These
image checks use offline fixtures, without production configuration writes or
provider inference.

- Source: `0974a7a00160ba1961fe4722af5c758f88610ea8`.
- Archive SHA-256: `c5d26c6bd310b45b2b5cdef8e52fc957c132ef89d63d6a123c0d490424cf8acc`.
- Image: `registry.chifor.me/llm-router/router@sha256:598cd7e1b634bc652024aa7041e2ea7584df88caf6cb2e15fae0f044375d44ef`.
- Runtime: unchanged Node 26.10.0 digest `sha256:a723b54c35a76e947095a20a67d39585bb09c862e6b1adeb8a9f518f95e34fb0`.
- [Immutable image receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-71933.json).

The **Router rollout acceptance** CI workflow checks one Ready router on this
exact image, no init containers, public health/readiness and unauthenticated
management refusal. Public SPA and bundle hashes are compared to the packaged
release. No kubeconfig is used. The deployment PR records the resulting CI run
and acceptance receipt.

Rollback is an image/annotation revert through a reviewed GitOps PR to
`registry.chifor.me/llm-router/router@sha256:0c1f9af1d3e0ee8bccc34a87ddd204d6500864c124e13844cd8346a81a127d44`
(source `6ebe437da928efcb50b6bbcfbb187802fb3fa00b`, release
`router-0.1.0-20261007-plugin-settings`). Saved settings remain compatible with
that release; no database or composition restore is needed for this naming change.


## Codex-native routes release (2026-10-09)

`router-0.1.0-20261009-codex-native` deploys merged llm-router main
`d7c798db2f1e3c4785a69cf9b9970d72c8fce5f3`
([PR #87](https://git.chifor.me/cchifor/llm-router/pulls/87)). It lets the **Codex CLI** use the
router. Today it is the ailab dev workers (`dev-workers.md` § "Codex through the router").

**Why.** The CLI speaks the subscription backend's own Responses protocol: tools as
`additional_tools` input items, encrypted reasoning items, `include`, `prompt_cache_key`,
`reasoning.context`, `text.verbosity`. The router's Responses subset refuses it
(`400 UNSUPPORTED_PARAMETER: include`, measured 2026-10-09), and codex 0.160 has no Chat Completions
mode any more (`wire_api = "chat" is no longer supported`).

**What.** The api-openai setting `codexNative.routes` (in `ROUTER_PLUGIN_CONFIG`, `router.yaml`)
lists routes that serve `POST /v1/responses` as a pass-through:

- **Request:** the client's body goes to the route's Codex account as sent. The router sets the
  deployment's model, `store: false` and `stream: true`, forwards an allowlist of the CLI's
  session and client headers, and uses the account's own credentials.
- **Response:** every upstream SSE event comes back as it came, the backend's own failure events
  included. The router reads only the terminal event, for usage, the finish and the error code.
- **Engine features apply unchanged:** routing, admission, timeouts, cooldowns, plan limits,
  usage and the journal (`request.received` carries `native: codex-responses`).
- **Unlisted routes:** nothing changes for them.
- **Model names:** a route-scoped key that names a model (the CLI's `gpt-6-astra`) is served by
  the first of its listed routes whose every target serves exactly that model. A route of the
  requested name always wins over that mapping.
- **Body size:** bodies over 2 MiB, up to 16 MiB (the CLI resends the whole conversation each
  turn), are admitted before they are read, and only for keys that may use a listed route. At
  most 8 are received at once.

**The routes and the account.** Six routes, one per model `codex-5` offers: `dw-gpt-6-astra`,
`dw-gpt-6-sol`, `dw-gpt-6-luna`, `dw-gpt-5.6-sol`, `dw-gpt-5.6-terra` and `dw-gpt-5.6-luna`. Each has:

- one target, `codex-5/<model>`;
- `maxAttempts: 1`, no fallback (encrypted reasoning only decrypts on the account that produced
  it);
- `timeoutMs` 300 s and `idleTimeoutMs` 600 s, and no `maxDurationMs`.

**What bounds a long Codex turn.** In the router, `timeoutMs` is only the wait **before any
output** (the first upstream byte), not a whole-request deadline. `idleTimeoutMs` bounds the
**silence between upstream bytes** once output has begun. With no `maxDurationMs`, nothing caps
the whole request, so a turn that keeps streaming runs as long as it streams.

On a stalled upstream the **CLI's own stream idle timeout (300 s) fires first**: the router's
`: keepalive` comments do not reset it. This was measured on 2026-10-09 with codex 0.154.0 and
0.160.0 through a local router whose fake backend sent `response.created` and then stalled, with
`stream_idle_timeout_ms` at 30 s. Both CLIs ended with `idle timeout waiting for SSE` after
32-33 s while the router kept sending keepalives every 15 s. The CLI's idle timer counts SSE
events, not bytes. The route's 600 s `idleTimeoutMs` therefore matters only for clients that do
not time out themselves.

The Cloudflare edge does not cut these streams. Its ~100 s limits are on the origin's first byte
and on silence between bytes. The router sends the SSE headers and `: ok` as soon as the attempt
is selected, then a `: keepalive` comment after every 15 s without a byte (README, "Streams start
at once"). The api-openai `imageProxy` 90 s deadline (requests carrying `cf-ray`) is applied only
by the image endpoints. Inference on `/v1/responses` passes no caller deadline (llm-router
`packages/plugins/api-openai/index.ts`: `proxied` is computed in `images()` only).

**State and order.**

- **Done before this rollout:** the six routes, created through `PUT /admin/v1/config`
  (revision 314), and the four workers' keys (`dev-worker-N codex (ailab dev worker, <ip>)`,
  expiring 2027-10-09, limited to the six routes).
- **Cutover, after this rollout's acceptance:**
  - account `codex-5` comes out of the shared `codex` route, back to the four accounts it had
    on 2026-10-07;
  - its `concurrency` goes from 1 to 6, so it serves only the dev workers.
- **Then, in the dev-worker PR:** the keys are seeded into OpenBao (`devworker-seeds.sops.yaml`)
  and the workers are switched.

Check the live state with `GET /admin/v1/config`: account `codex-5` and the `codex` route.

**Image.** Built from the merged source in the pinned Node 26.10.0 runtime image: frozen install,
build, production install, production smoke. Published by relay
`release/router-codex-native-image`.

- Archive `router-0.1.0-20261009-codex-native.tar.gz`, SHA-256
  `6cc278b18f80c31312f52d92bfbfcc1d62ebe89958051e6c6cf1a4914756bf50` (relay v0.2.0 asset).
- Image: `registry.chifor.me/llm-router/router@sha256:9f9661c8458479d9ed3b2915bd0be7bef73e6fa706f783d9410bad0fe8c0b49b`.
- [Image CI 75225](https://git.chifor.me/cchifor/relay/actions/runs/75225) (production smoke, plugin settings, backup and upgrade checks under the non-root/read-only constraints); [receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-75225.json).
- Before release, the same build ran the real Codex CLI (0.153.4 and 0.160.0) on dev-worker-1 against the real subscription backend, through a local router on that host's own login (an access token only, never refreshed): a shell tool call, an `apply_patch` edit and multi-turn reasoning all completed.

There is no schema or data change, and no init container or canary. The usual single-replica
Recreate rollout briefly interrupts service. The **Router rollout acceptance** workflow checks the
pod and the public endpoints. End to end: a real `codex exec` on each dev worker
(`scripts/validate-codex-fleet.sh`). Locally, before merge: the real CLI through the router to a
fake backend replaying a captured stream.

**Rollback** is an image/annotation revert through a reviewed GitOps PR to
`registry.chifor.me/llm-router/router@sha256:598cd7e1b634bc652024aa7041e2ea7584df88caf6cb2e15fae0f044375d44ef`
(source `0974a7a00160ba1961fe4722af5c758f88610ea8`, release
`router-0.1.0-20261007-provider-names`). That release's settings schema is not strict, so it ignores
the `codexNative` key, but drop the key in the same PR anyway. The order:

1. **First** turn the dev workers back to their own logins: `dev_worker_codex_router_enabled:
   false`, converge, then `codex app-server daemon restart` per user.
2. Then revert the image.
3. Then delete the six `dw-*` routes, or at least keep the workers off the router until a native
   release is back. The previous release serves those routes with the Responses subset and
   answers the CLI with `400 UNSUPPORTED_PARAMETER: include`, so never re-enable
   `dev_worker_codex_router_enabled` against it.
4. Put `codex-5` back into the `codex` route if its capacity is wanted there.

## Per-key routing release: model mode (2026-10-09)

`router-0.1.0-20261009-model-mode` deploys merged llm-router main
`473c9e0f3bbf92659326a0122e1cb872665083b3`
([PR #89](https://git.chifor.me/cchifor/llm-router/pulls/89)). It is PR A of the design
`docs/design/per-key-routing.md` in llm-router. It lets each API key be routed on its own, with
routes that serve whatever model the agent names.

**What it adds.** **Model mode**: a request names the model it wants in `model`, and a route is
selected for it, either by the `X-Router-Route` header or by the key's new **`defaultRoute`**.

- **Any-model routes.** A route target may say `model: "*"`. It then serves the requested model on
  any account whose catalog lists it.
- **`models`** on a route is an allowlist. It bounds the route and its fallbacks.
- **`native: ["codex-responses"]`** on a route makes `POST /v1/responses` a Codex pass-through.
  - It replaces the `codexNative.routes` list in `router.yaml`, which still applies to routes
    without the field.
  - A config rule refuses native routes that would reach non-Codex accounts
    (`422 INVALID_NATIVE_ROUTE`).
- **New responses.**
  - `x-router-route` names the route that served.
  - `400 MODEL_NOT_SUPPORTED` comes before any upstream call, for a model the route chain does not
    serve.
  - `400 MODEL_REQUIRED` when an any-model route is named in `model`.
  - `404 ROUTE_NOT_FOUND`.
- **Route mode is unchanged.** Every request that names a route or a model by name behaves as
  before, including the dev workers' current keys and the six `dw-gpt-*` routes.

**State and order** (the per-key cutover for the dev workers, design §7). None of this is done by
this rollout.

1. After acceptance, through `PUT /admin/v1/config`, add:
   - route `sub-codex-5`: one target `codex-5` with model `*`, `native: ["codex-responses"]`, the
     same timeouts as the `dw-gpt-*` routes, `maxAttempts: 1`, and `models` set to the six models
     the `dw-gpt-*` routes serve. This keeps the per-key model set explicit. Done 2026-10-09:
     revision 655 for the route, revision 676 for `models`;
   - pointers `dw-1` … `dw-4` → `sub-codex-5`.
2. Issue four keys: `{routes: [dw-N], defaultRoute: dw-N, expiresInDays: 365}`.
3. Re-seed `dev-worker-N.json` in `devworker-seeds.sops.yaml`. The workers read the key live, so
   their Codex config does not change.
   - The workers' Codex sends the **model name**, not a route: the role pins
     `model = "gpt-6-astra"` (`dev_worker_codex_model`), and the CLI puts it in `model`.
   - With the new key, `gpt-6-astra` is not one of the key's routes and the CLI sends no header, so
     the request takes the key's `defaultRoute` `dw-N` in model mode.
   - Never set the pin to a route name: a route name in `model` is route mode, and a route outside
     `[dw-N]` would be refused with 403.
4. Verify that each worker's request resolved through its pointer:
   - in the journal, the trajectory has `model: gpt-6-astra` and `route: dw-N`;
   - its `routing.evaluated` route is `sub-codex-5`, served by `codex-5`;
   - the response carries `x-router-route: sub-codex-5`;
   - then run `scripts/validate-codex-fleet.sh dev_workers`.
5. Keep the old keys until after a soak, then revoke them.
   - Keep the six `dw-gpt-*` routes, and their `codexNative.routes` entries in `router.yaml`, for
     as long as a rollback to an image older than this release is possible. The rollback below
     needs them.

After that, repointing a worker is one change to its pointer (`dw-N`), for example to a pool. Pools
should wait for conversation placement (PR C).

**Image.** Built from the merged source in the pinned Node 26.10.0 runtime image: frozen install,
build, production install, production smoke. Published by relay `release/router-model-mode-image`.

- Archive `router-0.1.0-20261009-model-mode.tar.gz`, SHA-256
  `4912d861143192a23bcbf37a5ce14c57d4aa5b659c7b6801e9171fc1913ee00a` (a relay v0.2.0 asset).
- Image: `registry.chifor.me/llm-router/router@sha256:3a106b220aa2cbb6911f90edff698b93fec5628534a4ac4c7f57b8b081df8dd7`.
- [Image CI 76282](https://git.chifor.me/cchifor/relay/actions/runs/76282);
  [receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-76282.json).
- Before merge: 1324 tests passed. The only failures were the known environmental ones (the image
  store's free-space guard, `claude-sdk` timing). Each of the eight tasks was reviewed, plus a final
  whole-branch review.

There is no schema change: new route and key fields are optional, and trajectories keep their
columns. The usual single-replica Recreate rollout briefly interrupts service. The **Router rollout
acceptance** workflow checks the pod and the public endpoints.

> **Superseded on 2026-10-11.** The old-keys rollback below no longer exists. The four pre-per-key dev-worker keys are
> revoked and the six `dw-gpt-*` routes are deleted (see "Claude tools and route cleanup (2026-10-11)"). To roll back
> the dev workers, repoint `dw-1` … `dw-4` to `fallback-codex` (see "Codex fallback route (2026-10-11)"). Every image
> since `router-0.1.0-20261010-placement` serves that route. It replaced `sub-codex-5` (`codex-5:*`), removed on
> 2026-10-11.
>
> **An image rollback older than `router-0.1.0-20261010-placement`, such as this section's own Rollback below, cannot
> serve `fallback-codex`.** The route needs conversation placement, so `dw-N` would get 503. Before such a rollback:
> 1. create a `priority` route with one Codex account, model `*`, `native: ["codex-responses"]` and the six-model
>    `models` allowlist (the old `sub-codex-5` shape: one account, so it does not depend on placement);
> 2. repoint `dw-1` … `dw-4` to it;
> 3. only then revert the image.

**Rollback.**
- **Before the cutover above:** an image/annotation revert through a reviewed GitOps PR to
  `registry.chifor.me/llm-router/router@sha256:9f9661c8458479d9ed3b2915bd0be7bef73e6fa706f783d9410bad0fe8c0b49b`
  (source `d7c798db2f1e3c4785a69cf9b9970d72c8fce5f3`, release `router-0.1.0-20261009-codex-native`).
- **After the cutover**, the older image ignores `defaultRoute` and the per-route `native`, and
  serves `*` targets to no one, so the new keys stop working. Its next config write also drops
  `models` and `native`.
  - **Precondition:** the older image passes the Codex protocol through only on routes listed in
    the manifest's `codexNative.routes`. The workers must therefore go back to routes in that
    list, the six `dw-gpt-*` routes, which must still exist.
  - **The Codex model pin never changes in either direction.** It stays `model = "gpt-6-astra"`
    (`dev_worker_codex_model`), and the CLI always sends that model name.
    - **What changes is the key.** A key limited to the `dw-gpt-*` routes, with no `defaultRoute`,
      is served by the **legacy Codex-native exact-model mapping** (#87, kept in every release since).
    - The mapping takes the first of the key's native routes whose every enabled target serves
      exactly the requested model: `gpt-6-astra` becomes `dw-gpt-6-astra`.
    - The router then journals that route name as the trajectory's `model`. That is why old-key
      traffic shows `model: dw-gpt-6-astra` even though the CLI sent `gpt-6-astra`.
  - **Before the old keys are revoked** (during the soak):
    1. Re-seed the old key values into `dev-worker-N.json` and let the provision Job write them.
    2. Check with `scripts/validate-codex-fleet.sh dev_workers` that each worker answers through a
       `dw-gpt-*` route. In the journal: `route` absent and `model: dw-gpt-6-astra`, from the
       mapping above.
    3. Then revert the image.
    4. Then remove `sub-codex-5` and the `dw-N` pointers.
  - **After the old keys are revoked:**
    1. Issue four replacement route-mode keys first, in the old shape:
       `{routes: [the six dw-gpt-* routes], expiresInDays: 365}`, with no `defaultRoute`.
    2. Seed and verify them as above.
    3. Then revert the image.
    4. Then remove `sub-codex-5` and the `dw-N` pointers.
    5. Then revoke the per-key `dw-N` keys.

## Per-key routing release: quota-aware routing and conversation placement (2026-10-10)

`router-0.1.0-20261010-placement` deploys merged llm-router main
`076dff0cc0e487c81fd6266d4ed75824fc3e00a6`. It contains PR B
([#92](https://git.chifor.me/cchifor/llm-router/pulls/92)) and PR C
([#93](https://git.chifor.me/cchifor/llm-router/pulls/93)) of the design `docs/design/per-key-routing.md` in
llm-router.

**PR B: routing follows each subscription's real limits.**
- The plan-limit readings the router already records become routing input on **every** route. They come from Codex
  reply headers, the Claude provider and the usage check (Status → "Check now", `/usage`).
- A full window rules its account out until the window resets.
- **Credits exception:** a full window whose reading shows usable credits keeps the account eligible. The `usage`
  strategy ranks it last. A `priority` route keeps sending to it while it is the first target.
- Claude per-family weekly windows bound only that family's model id. This includes `seven_day_overage_included`,
  which therefore never rules a whole account out.
- A reading counts only for the signed-in seat that recorded it.
  - Readings saved by an older image carry no seat stamp, so they **do not route** until the account's next reply or
    check.
  - The rollout therefore changes nothing at first; each account joins as it is next used or checked.
- Expected at rollout, from the 2026-10-10 readings:
  - `codex` and `codex-4` are at 100 % with no credits until 10-14 / 10-15. They leave the `codex`, `gpt-6-luna` and
    `default` rotations after their next reading, where today the upstream refuses them.
  - `codex-5`, the dev workers' `sub-codex-5`, is at 100 % **on credits**. It keeps serving, as today.

**PR C: conversation placement**, opt-in per route (`placement: "conversation"`, `placementIdleHours`).
- It keeps each Codex conversation (`thread-id`, else `prompt_cache_key`) on one subscription of a pool, so its prompt
  cache stays warm.
- If the bound account is busy, the turn is served elsewhere for that turn only. If it is gone, the conversation
  moves.
- On a `usage` route, an account spending credits while a sibling has quota counts as gone.
- Responses carry `x-router-placement: new|bound|inherited|spilled|moved`. The journal and
  `GET/DELETE /admin/v1/bindings` hold only a hash of the conversation id.
- **No route has `placement` at rollout**, so nothing changes until one opts in. Routes without it behave as before,
  including the `x-router-*` disclosure headers.

**Schema.** SQLite `user_version` 7 adds a `placements` table (`CREATE … IF NOT EXISTS`). The older image never reads
`user_version`: it only runs its own idempotent migrations and then sets the version to 6. So it starts on a v7 volume,
writes 6, and leaves `placements` untouched. Re-upgrading sets 7 again, with the rows intact.

Verified on 2026-10-10 by running the two workers in turn on one database file:
1. the new worker (`076dff0`): v7, one placement written;
2. the old worker (`473c9e0`): it starts, and the version is 6 with the table kept;
3. the new worker again: v7, the placement loads.

**The dev-worker pool is held.** The design's first pool was `pool-codex` over `codex-2`/`codex-3`/`codex-4`. It is
not created, for these reasons:
- Each of those accounts has concurrency 1 and serves the shared `codex`, `gpt-6-luna` and `default` routes. A pool
  would take those slots from other agents.
- On 2026-10-10, `codex-4` was exhausted until 10-15.
- `codex-5` now has concurrency 16, enough for the four workers.

Create a pool only from subscriptions dedicated to the dev workers. Then repoint ONE `dw-N` and watch it:
- `x-router-placement` mostly `bound`;
- `usage.cachedInputTokens` staying high across turns;
- the spill rate.

Only then move the others.

**Image.** Built from the merged source in the pinned Node 26.10.0 runtime image: frozen install, build, production
install, production smoke. Published by relay `release/router-placement-image`.
- Archive `router-0.1.0-20261010-placement.tar.gz`, SHA-256
  `e52b52e6b2d14ce240285f919081e644343d9d1cd3caa44f977fa08141b31c34` (a relay v0.2.0 asset).
- Image: `registry.chifor.me/llm-router/router@sha256:39517806cf64c29efb4d5b526720491dbed78d2ed684787d0be53bb49cabfce1`.
- [Image CI 77889](https://git.chifor.me/cchifor/relay/actions/runs/77889);
  [receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-77889.json).
- Before merge: 1455 tests passed. The only failures were the known environmental ones (the image store's free-space
  guard, `claude-sdk` timing). Each task was reviewed, and each PR had a final whole-branch review with one fix wave.
- The usual single-replica Recreate rollout briefly interrupts service. **Router rollout acceptance** checks the pod
  and the public endpoints.

**Rollback.** Revert the image and annotations through a reviewed GitOps PR, to
`registry.chifor.me/llm-router/router@sha256:3a106b220aa2cbb6911f90edff698b93fec5628534a4ac4c7f57b8b081df8dd7`
(source `473c9e0f3bbf92659326a0122e1cb872665083b3`, release `router-0.1.0-20261009-model-mode`).
- The per-key keys, `sub-codex-5` and the `dw-N` pointers keep working: PR A's features are in both images.
- The older image ignores quota readings for routing, as before this release.
- Its next config write drops `placement` and `placementIdleHours` from routes. Remove those first if a pool exists,
  so that nothing depends on placement after the revert.

## Smart pools release (2026-10-10)

`router-0.1.0-20261010-smart-pools` deploys merged llm-router main `8786a3dcb2a44403a0730569abeb811b729cc563`
([#95](https://git.chifor.me/cchifor/llm-router/pulls/95)). The design is `docs/design/smart-pools.md` in llm-router.

**What it adds.** **Smart pools**: routes that spread their clients over every subscription that can serve them.
- **The `smart-pools` plugin** keeps one managed route per enabled pool (`managedBy: smart-pools`) in step with the
  accounts. The pools themselves are runtime plugin settings, editable in:
  - the settings editor;
  - `PATCH /admin/v1/plugins/smart-pools/config`;
  - the chat tools `list_pools`, `configure_pool` and `remove_pool`.
- **The `smart` strategy** chooses an account like this:
  - quota that would be lost at the next reset goes first;
  - accounts spending credits go last;
  - near-limit accounts (`reservePercent`) are held back;
  - busy accounts are discounted by load;
  - near-ties rotate.
- **`GET /admin/v1/pools`** shows each member's tier, score, load and reset.
- **The model-refresh fix:** the Codex model refresh no longer rewrites `*` targets or pools' routes. That bug had
  turned `sub-codex-5`'s `*` into `gpt-6-astra`.

**At start** the plugin writes one revision as `plugin:smart-pools` (`pools.synced`):
- `pool-codex` gets every Codex account, `*` targets limited to the six `dw` models, Codex-native and conversation
  placement.
- `pool-claude` gets every Claude account on `sonnet`.
- `pool-mixed` (Codex `gpt-6-astra` + Claude `sonnet`) ships **disabled**.

Nothing else changes, and nothing uses the pools until a pointer or key is moved to one.

**After acceptance** (each step through the admin API):
1. **Restore `sub-codex-5`'s target to `codex-5:*`.** It is the rollback route for the per-key keys.
2. **Repoint `dw-1` … `dw-4` from the stopgap `dw-pool` to `pool-codex`.**
   - Validate with `scripts/validate-codex-host.sh` on each worker.
   - In the journal, check `route: dw-N`, routing `pool-codex` with strategy `smart`, and `x-router-placement` mostly
     `bound`.
3. **Delete `dw-pool`**, the hand-built 2026-10-10 stopgap: `usage` and placement over codex-3, codex-2 and codex-5.
4. **`pool-mixed`.**
   - Run `scripts/smoke-pool.mjs` from an llm-router checkout, with `ROUTER_BASE_URL=https://router.chifor.me`,
     `ROUTER_ADMIN_TOKEN` read from `llm-router-auth`, and `SMOKE_POOL=pool-mixed`.
   - It smokes through a temporary `pool-mixed-smoke` route and key, and removes both even when interrupted.
   - Enable the pool only if it passes.

**Cutover done (2026-10-10, about 22:25 UTC):**
- **Release.** The image rolled out at 22:20. The start sync wrote one revision, 1259 (`pools.synced`, added
  `pool-codex` and `pool-claude`).
- **Revision 1260:** `sub-codex-5` restored to `codex-5:*`.
- **Revision 1261:** `dw-1` … `dw-4` repointed to `pool-codex`. All four workers pass `validate-codex-host.sh`. The
  journal shows `route: pool-codex`, strategy `smart`, codex-3 selected, and placement `new` then `bound`.
- **Revision 1262:** `dw-pool` deleted.
- **The `pool-mixed` smoke FAILED, so the pool stays off.** All 24 tool-carrying requests went to codex-3. Every Claude
  account is `tools_unsupported`, because the router's Claude provider runs `transport: print` (single-turn text, no
  tools).
- **What Claude clients get today, independently of pools.** A request that carries tools, on the existing `claude`
  route or on `pool-claude`, gets **400 `UNSUPPORTED_CAPABILITY`** ("No target of route … can serve this request:
  tools_unsupported"), with no fallback. Plain chat is served as before. Neither route can serve tool calls until the
  transport changes.
- **To enable Claude tools:** set the `provider-claude` plugin setting `transport` to `sdk-resume`. Use the router's
  plugin settings editor, or `GET`/`PATCH /admin/v1/plugins/provider-claude/config` (the `transport` value).
  - This is a global change for every Claude client. It is the owner's decision, recorded as pending on 2026-10-11.
  - After the change, re-run the smoke (`SMOKE_POOL=pool-mixed`) before setting `pool-mixed` to `enabled: true`.
- **Smoke cleanup.** The smoke creates a temporary pool `pool-mixed-smoke` (with its managed route of the same name) and
  a temporary key limited to that route. It removed both: "temporary key revoked" and "temporary pool pool-mixed-smoke
  removed".

**Rollback.** Revert the image through a reviewed GitOps PR to
`registry.chifor.me/llm-router/router@sha256:39517806cf64c29efb4d5b526720491dbed78d2ed684787d0be53bb49cabfce1`
(`router-0.1.0-20261010-placement`, source `076dff0`). **First**:
1. repoint anything that uses a pool. For the dev workers: `dw-1` … `dw-4` → `fallback-codex` (see "Codex fallback
   route (2026-10-11)"); the old image serves that route. Do not use `dw-pool`: it was deleted in revision 1262;
2. then turn every pool off (`configure_pool … enabled: false`, or `remove_pool`), so their routes go.

> **Changed on 2026-10-11.** Step 1 used to name `sub-codex-5` (`codex-5:*`, restored in revision 1260). That route
> was removed on 2026-10-11 and replaced by `fallback-codex`.

If you skip this, the old image:
- answers pool requests with 503 `STRATEGY_UNAVAILABLE`;
- drops the pool settings (its loader does not know the plugin);
- strips the pool routes' `managedBy` mark at its first **internal** write. Its own trusted writes (the Codex model
  refresh about 20 s after start, token renewals) save the whole configuration through the old schema, which drops the
  unknown field, and they skip the strategy check.
- refuses **administrator and chat-tool** writes until then: dropping the mark counts as changing a route whose
  strategy (`smart`) the old image does not know, so `STRATEGY_UNAVAILABLE`. After that internal write, writes that
  change a pool route itself (removing an account in it, for example) stay refused for the whole rollback.

So when the procedure was skipped, the pool routes reach the roll-forward unmarked. Rolling forward then adopts them
(strategy `smart`, every target `<pool>:<account>`) instead of reporting a collision, but restores the shipped pool
settings: re-apply any custom ones. With the procedure followed, there is nothing to adopt; the pools are re-created.

**The old image still carries the Codex model-refresh bug.** After a rollback, its refresh again rewrites `*` targets
on router-owned Codex logins to a fixed model, within about 40 minutes. That includes `fallback-codex`'s five
targets. The bug turned `sub-codex-5`'s `*` into `gpt-6-astra`, the model the workers pin. After rolling forward
again, re-pin `fallback-codex`'s targets to `*`.

**Image.**
- Archive `router-0.1.0-20261010-smart-pools.tar.gz`, SHA-256
  `b3a392a3ad4d4121c5be2dfa00e60fe022086a7338372fe5a20720425e5132a6` (relay v0.2.0 asset); relay branch
  `release/router-smart-pools-image`.
- Image: `registry.chifor.me/llm-router/router@sha256:6ff09174b0a57f9983fcd566bfb47ca61217d862d8c3cba43fbb73631ca816b6`.
- [Image CI 79636](https://git.chifor.me/cchifor/relay/actions/runs/79636);
  [receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-79636.json).
- **Before merge:**
  - 1588 tests passed and 0 failed; typecheck and build passed; `check:architecture` reported only its known finding
    (llm-router issue #79, `packages/modules/inference/index.ts`).
  - Nine tasks were each reviewed, then a whole-branch review with one fix wave.
  - The reviewers proved a production-like first start writes only the two pool routes, and that a rollback cycle
    heals.

## Claude tools and route cleanup (2026-10-11)

**Claude multi-turn with tools.** On the owner's go:
- The `provider-claude` plugin setting `transport` is now `sdk-resume` ("Multi-turn"). Claude Code 2.1.281 is on the
  validated SDK list.
- `capabilities.tools` is `true` on all five Claude accounts (config revision 1273).
- Claude accounts now serve tool calls on the `claude` route, `pool-claude` and `pool-mixed`.

**`pool-mixed` passed the live smoke and is enabled** (revision 1278).
- Codex and Claude each served streaming and non-streaming tool calls.
- The smoke ran with `pool-mixed`'s `spread` temporarily at 1. At the default 0.2, Claude's fresher quota outscores
  Codex, so a sequential smoke never reaches Codex. `spread` was restored to 0.2 before the pool was enabled.

**Unused routes removed** (revision 1280; the evidence is 7 days of `GET /admin/v1/stats?hours=168`, plus every
reference from pointers, fallbacks and active keys):
- `claude-5-test`: a test route. 1 request, on 10-07; nothing references it.
- The six `dw-gpt-*` routes: legacy Codex-native routes for the pre-per-key dev-worker keys. `dw-gpt-6-astra` was last
  used on 10-09 18:00, just before the per-key cutover; the other five were never used.
- Their four keys (`key_aa2543746716`, `key_43132b508eaa`, `key_65e07ec0f75a`, `key_b06fc4e10a3c`) were revoked first.
  They could reach only those routes.

The rollback described in "Per-key routing release: model mode" (going back to the old keys and the `dw-gpt-*` routes)
**no longer exists**. The supported rollback is `dw-N → fallback-codex`, which every image since
`router-0.1.0-20261010-placement` serves (see "Codex fallback route (2026-10-11)").

**Kept, though lightly used:**
- `claude` (6 requests in 7 days; the router agent's binding uses it);
- `qwen-image-2.1-cloud` and `qwen-image-2.1-fast-cloud`, which the TrueSwarm key scopes;
- `default`, the router's default pointer;
- `sub-codex-5`, then the `dw-N` rollback target. It was removed later that day (revision 1299) and replaced by
  `fallback-codex`.

**Leftover, now gone:** `router.yaml`'s `api-openai.codexNative.routes` listed the six deleted `dw-gpt-*` names (a
harmless membership check). It was dropped with the next digest bump, in "Activity source release (2026-10-11)"
below, so the router restarted only once.

## Activity source release (2026-10-11)

`router-0.1.0-20261011-activity-source` deploys merged llm-router main `cf6e0a23d23d12902ebbc3a2fe9677d3f24bafbe`
([#96](https://git.chifor.me/cchifor/llm-router/pulls/96)).

**What it adds.** The Activity page shows who made each request:
- the **Source** column carries the API key's name (e.g. `dev-worker-3 codex per-key`, with `(revoked)` or `(expired)`
  when that applies), resolved within the row's namespace;
- **Route** is the route the caller entered (`dw-3`);
- **Model** is the model asked for (`gpt-6-astra`);
- **Source** and **Route** filters run on the server (`GET /admin/v1/trajectories?principal=…&route=…`);
- the detail header reads `gpt-6-astra · from dev-worker-3 codex per-key via dw-3 → pool-codex`.

The responses only gain `source: {id, name, kind}`; no key hash or prefix is exposed. There is no schema change: the
filters use the existing `principal_id` column and the stats' `COALESCE($.route, model)` grouping.

**Also in this rollout:** `router.yaml` drops `api-openai.codexNative.routes`, the six `dw-gpt-*` names deleted on
2026-10-11. Codex-native pass-through is a per-route `native` setting (`fallback-codex`, `pool-codex`).

**Image.**
- Archive `router-0.1.0-20261011-activity-source.tar.gz`, SHA-256
  `b312f7fdfe7a47aac8beb72f695a5a43eaba5955af1a3e3fed96dba742aff8f1`; relay branch
  `release/router-activity-source-image`, [run 80376](https://git.chifor.me/cchifor/relay/actions/runs/80376).
- Image `registry.chifor.me/llm-router/router@sha256:33b4298abd96062003cf8cd30fac023eccc4ae82032d3c7b49fc91a62db37e2e`.
- Before merge, 1600 tests passed and 0 failed; typecheck and build passed; `check:architecture` reported only its
  known finding (#79).

**Rollback.** Revert, in `router.yaml`, the image digest and both pod-template annotations together, so the rolled-back
pod advertises the right release:
- `image: registry.chifor.me/llm-router/router@sha256:6ff09174b0a57f9983fcd566bfb47ca61217d862d8c3cba43fbb73631ca816b6`
- `chifor.me/source-commit: 8786a3dcb2a44403a0730569abeb811b729cc563`
- `chifor.me/release: router-0.1.0-20261010-smart-pools`

Nothing else is needed: the pools are unchanged, and the dropped `codexNative` list named only deleted routes, so it
need not come back.

## Codex fallback route (2026-10-11)

**Why.** The dev workers' pointers `dw-1` … `dw-4` target `pool-codex`, a smart pool that depends on the `smart-pools`
plugin. `fallback-codex` is where to repoint them if that plugin is turned off or misbehaves, or if the image is rolled
back to one older than `router-0.1.0-20261010-smart-pools`.
- It needs no plugin. The `usage` strategy, `*` targets and conversation placement are served by every image since
  `router-0.1.0-20261010-placement` (the PR B+C release, image `39517806`).
- Unlike the old `sub-codex-5`, it follows quota across all five Codex accounts instead of spending codex-5's credits.
  - All five targets stay eligible. `access: ["subscription"]` is the account class (subscription, not API or
    local), not credit state.
  - On a `usage` route, an account serving on credits (a full window with usable credits) ranks last. It is used only
    when no other target has included quota left (`packages/modules/placement` QUOTA_RANKED).
  - So codex-5, on credits until 10-14, is the last resort, not excluded.

**Revision 1298** added route `fallback-codex`:
- strategy `usage`, `access: ["subscription"]`, `maxAttempts: 2`;
- five any-model targets, `fallback-codex:<account>` for `codex`, `codex-2`, `codex-3`, `codex-4` and `codex-5`, each
  with model `*`;
- `native: ["codex-responses"]` and `placement: "conversation"`;
- `models`: the same six-model allowlist as `pool-codex`;
- `timeoutMs` 300000, `idleTimeoutMs` 600000.

**Smoke test.** Through a temporary key bound to the route (`defaultRoute: fallback-codex`), revoked afterwards:
- chat completions were served by `codex`;
- a native `/v1/responses` stream was served by `codex-2` and completed.

**Revision 1299** deleted route `sub-codex-5`, the previous rollback target. It was a `priority` route over `codex-5:*`
only, and `codex-5` is on credits until 10-14. No key or pointer referenced it.

**To use it,** repoint `dw-1` … `dw-4` → `fallback-codex` (`PUT /admin/v1/config`, or the chat's `/point dw-N`).
Nothing changes on the workers. Before an image rollback to `39517806`, do this first: see "Smart pools release
(2026-10-10)", **Rollback**.

**Caveat.** The pre-smart-pools image (`39517806`) still carries the Codex model-refresh bug. Within about 40 minutes it
rewrites `*` targets on router-owned Codex logins to a fixed model, `fallback-codex`'s five included. After rolling
forward again, re-pin `fallback-codex`'s targets to `*`. The same caveat applied to `sub-codex-5`.

## Live status and upgrades release (2026-10-11)

`router-0.1.0-20261011-live-status` deploys merged llm-router main `1a7bb7e85d4053daef8771df18fc10d047c791e6`
([#97](https://git.chifor.me/cchifor/llm-router/pulls/97)). Design: llm-router
`docs/design/live-status-and-upgrades.md`.

**What it adds.**
- **Status, time window:** the stats cards take a window of 30 min, 1 h, 3 h, 6 h, 12 h or 24 h
  (`GET /admin/v1/stats?window=…`, 24 buckets aligned to the clock). The week usage card stays on 7 days.
- **Status, live serving:** each route shows the account serving it right now in bold green with "N running", then
  "served N s ago", from `GET /admin/v1/live`. The page polls it every 5 s while it is visible.
- **Running** is an accent pill that pulses, in the Activity list and the request detail.
- **Cancellation reasons:** a cancelled request is labelled **Superseded** (the same conversation sent its next request
  right after, e.g. a Codex multi-agent follow-up), **Interrupted by restart**, or **Cancelled** (the client left).
  `explain_request` returns the reason.
- **Activity layout:** a divider between the list and the detail can be dragged (or moved with the arrow keys,
  double-click resets it). The list scrolls sideways on its own.
- **Drain on SIGTERM:** `/ready` turns 503 and the listener closes, so the edge holds new requests. In-flight requests
  finish for `ROUTER_DRAIN_MS`; every response sent meanwhile says `Connection: close`, so the edge opens a fresh
  connection (to the next pod) for its next request. At the deadline the engine shuts down: streams still open are cut
  with no terminal event and journaled as `cancelled` with cause `SHUTDOWN` (Codex reconnects through the edge), and a
  request still arriving is refused with a reset. A stream already sending its own final event ends normally. Streams
  send a keepalive every 5 s. A watchdog exits the process at `ROUTER_DRAIN_MS` + 15 s.

**Configuration.** `router.yaml` sets `ROUTER_DRAIN_MS: "20000"`, the default, written out so it is visible next to the
grace period. There is no `preStop` delay, on purpose: the router closes its listener at SIGTERM while Kubernetes is
still removing its endpoint, so for a moment the edge's connections to it are refused. With one replica (`Recreate`)
there is no other pod to send them to either way, and Caddy retries a refused connection (a dial failure) every 250 ms
within its hold, so such a request waits for the next pod instead of failing. The router's watchdog exits at `ROUTER_DRAIN_MS` + 15 s (35 s), which must stay below
`terminationGracePeriodSeconds` (60). The router refuses to start with a value above 40000.

**First start.** The schema step that runs at every start creates index `trajectory_started` on
`trajectories(started_at)` once. On 2026-10-11 the journal held 41,195 rows (335 MB). The same build over a synthetic
journal of that size (41,195 rows, 361 MB) took 204 ms on a local disk. On the volume the build is bound by reading
the journal: 405 MB on 2026-10-12, read from the page cache in 0.06 s, but a rollout detaches and re-attaches the
volume, so the first start reads it cold, about 2-4 s at an iSCSI rate of 100-200 MB/s. `user_version` stays 7.

**The first rollout still uses the old shutdown.** Kubernetes stops the old pod, which has no drain: it waits up to
60 s on open streams and is then killed, as in earlier releases (a gap of about 76–79 s), and the new pod's first start
adds the index build (a few seconds cold, above): about 80–83 s, close to the edge's 85 s hold. The edge holds new
requests meanwhile, but the streams the old pod held are lost: expect those rows as **Interrupted by restart** after the
new pod starts. If the gap does pass the hold, a request held for the full 85 s gets a 502 from the edge; Codex retries
it (request retries, with backoff) and the retry reaches the new pod, so a turn shows a retry, not a failure. Later
rollouts drain, so their gap is about 45 s at most. To verify the drain itself, restart the router once the new pod is up (see "To restart the router" above; the
`rollout restart` used on 2026-10-12 caused a second restart when Flux next applied). Expect the following:
- the pod log shows `SIGTERM, draining`, then `drained in … ms` (or `… request(s) still running: ending them (SHUTDOWN)`);
- the journal for that window has no new **Interrupted by restart** rows, only `cancelled` / `SHUTDOWN` for streams
  that outlived the 20 s;
- the dev workers' turns continue; a cut stream shows as Codex's `Reconnecting… n/5` and recovers.

**Known residual (llm-router ruling R11).** When the drain starts, Node's `server.close()` closes idle kept-alive
connections once, and it counts a response as idle as soon as the response has ended, even while a slow reader still
has its tail queued in the router. A response that ends at that instant can lose its tail; the client sees a cut stream
or a short body and retries. Behind the edge this needs a reader that is applying backpressure at that exact moment.

**Image.**
- Archive `router-0.1.0-20261011-live-status.tar.gz`, SHA-256 `e6115e21a5aaae30ef78f0b665149d2536b7d236ef2d7741016ccd81ca4b77d6`; relay branch
  `release/router-live-status-image`, [run 82431](https://git.chifor.me/cchifor/relay/actions/runs/82431).
- Image `registry.chifor.me/llm-router/router@sha256:10c0828a04a12ae64205184aea0c74892f5d62a0c00513eb5c574962fe22117a`.
- Before merge: 1721 tests passed, 0 failed, on Linux CI (Node 24), and on the production Node 26 image; typecheck,
  build, the production smoke and the browser tests passed. Both reviewer bots approved it once ailab #1276 let them
  read `trajectory-recorder/index.ts`, which git had marked binary for two raw NULs (now escapes).

**Rollback.** In `router.yaml`, revert the image digest and both pod-template annotations together:
- `image: registry.chifor.me/llm-router/router@sha256:33b4298abd96062003cf8cd30fac023eccc4ae82032d3c7b49fc91a62db37e2e`
- `chifor.me/source-commit: cf6e0a23d23d12902ebbc3a2fe9677d3f24bafbe`
- `chifor.me/release: router-0.1.0-20261011-activity-source`

`ROUTER_DRAIN_MS` can stay: older images ignore it. The new index stays too; older images do not use it, and it costs
one more index update per journal write. A rollback is itself a rollout of the NEW image's pod, so it drains.

## Follow-ups release (2026-10-12)

`router-0.1.0-20261012-followups` deploys merged llm-router main `9163faa9c5390a0dd1c31b7e52c8950ffa1e82d6`
([#98](https://git.chifor.me/cchifor/llm-router/pulls/98)). It fixes what the live-status release surfaced.

**What it changes.**
- **The drain writes the journal before it exits.** On the 2026-10-12 verification restart, 2 of the 3 streams the
  deadline cut were found `abandoned`: their SHUTDOWN was still queued in the recorder when the runtime closed. The drain
  now flushes the journal first (for at most 3 s; a flush that does not finish is logged) and logs
  `runtime closed in N ms`.
- **Watchdog:** `ROUTER_DRAIN_MS` + 17 s (was + 15 s): the 12 s cleanup, then 5 s for the journal's flush and the
  runtime's close. At the 40 s maximum that is 57 s, inside the 60 s grace.
- **`/ready` no longer waits behind admin queries.** It asks the storage worker whether it is alive and moving (opened,
  not failed, no call stuck past 10 s), without a round trip. An admin query that runs 10 s or more still turns it 503
  until it ends (above: before, any query over 3 s did). The edge's health checks (3 s / 3 s / 2 failures) stay as they
  are.
- **Codex conversations stuck on an overloaded backend recover.**
  - An overload (`server_is_overloaded`, `server_error`) that arrives before any output now fails over to another
    account, as does a stream the backend ends before any output. The router holds back the stream's opening
    `response.created`/`in_progress` until real output, so it can.
  - After 3 overloads in a row, a conversation's upstream `prompt_cache_key` gets a suffix (`-rk1`, …). The backend
    routes by that key, so the conversation lands on another backend, at the cost of its prompt cache once. It is
    journaled as `conversation.rekeyed`.
  - This is the case dev-worker-2 hit on 2026-10-12: one conversation failed about 83% of the time, on every account.
- **Activity:** cancellations of older rows classify as Superseded again; the principal comes from the backfilled column.
- **Status page:** route rows no longer repeat "Healthy" and "· N running". A subscription that is not healthy shows its
  reason in red.

**How to check after the rollout.** The old pod (the live-status image) already drains, so expect a gap of about 35 s,
with requests held by the edge. Its drain does not flush the journal yet, so streams it cuts at the deadline may still be
found `abandoned`, as on 2026-10-12. To verify the fix, restart the router once the new pod is up (see "To restart the router" above); that drain runs
the new code:
- its log ends with `runtime closed in … ms`, then `drained in … ms`;
- the journal has no `abandoned` rows for the restart: streams the deadline cut are `cancelled` / `SHUTDOWN` (Activity:
  "Interrupted by restart").

**Image.**
- Archive `router-0.1.0-20261012-followups.tar.gz`, SHA-256
  `a40176589b3e625702fff9b06a8117f714181552b538c925757900ce54f2fdec`; relay branch
  `release/router-followups-image`, [run 82520](https://git.chifor.me/cchifor/relay/actions/runs/82520).
- Image `registry.chifor.me/llm-router/router@sha256:562f48f323a94be8dbe3d983cf2838892bf954dd30de503f8f10cefad9e01f16`.
- Before merge: 1743 tests passed, 0 failed, on Linux CI
  ([run 82515](https://git.chifor.me/cchifor/llm-router/actions/runs/82515)); typecheck, build, the production smoke and
  the browser tests passed there. The archive's production smoke passed again in the pinned Node 26 image. Both reviewer
  bots approved.

**Rollback.** In `router.yaml`, revert the image digest and both pod-template annotations together:
- `image: registry.chifor.me/llm-router/router@sha256:10c0828a04a12ae64205184aea0c74892f5d62a0c00513eb5c574962fe22117a`
- `chifor.me/source-commit: 1a7bb7e85d4053daef8771df18fc10d047c791e6`
- `chifor.me/release: router-0.1.0-20261011-live-status`

Nothing else changes: no schema change, no new setting. The journal's `conversation.rekeyed` events stay, and the
live-status image's Activity skips event types it does not know. The re-key lives in the router's memory only and
changes just the copy of the body sent upstream, so after a rollback every conversation sends its own key again (and its
overload count starts over).

## Codex overload retries release (2026-10-12)

`router-0.1.0-20261012-codex-retries` deploys merged llm-router main `4f1fb4f6a79fe3df2a42712448309dc8568d1613`
([#99](https://git.chifor.me/cchifor/llm-router/pulls/99)).

**Why.** After the follow-ups release, Codex turns on the dev workers still ended with "Selected model is at
capacity. Please try a different model." That is the Codex CLI's text for an error code of `server_is_overloaded`, and
it ends the turn without a retry. Between 08:41 and 09:32 UTC about 23 requests ended this way (about 4% of all
requests). It happened on all four dev workers, and on the reviewer bots' model-mode keys, whose journal rows have no
`route` (reviewer-codex, model `codex`). It happened in two ways:
- **Before output (about 6):** every attempt met the overload. The router failed over to another account, but the
  backend routes by the conversation's `prompt_cache_key`, so the second account met the same overload. The router's
  final `response.failed` then carried the upstream code.
- **After output (about 17):** the backend's own `response.failed` arrived after output had started and went out
  unchanged.

Because each turn ended after at most 2 overloaded attempts, the conversation never reached the 3 in a row that re-key
it.

**What it changes.**
- An overload reaches the Codex CLI as `server_error`, the code it retries, before or after output. The upstream code
  goes beside it as `upstream_code`, and the rest of the event is unchanged.
- Measured with the Codex CLI 0.162.1 against a local mock: `server_is_overloaded` ends the turn at once, while
  `server_error` is retried (5 reconnects by default, 8 on the dev workers).
- Each retry fails over again, and the 3rd overloaded attempt in a row re-keys the conversation onto another backend
  (`conversation.rekeyed`).
- Every other upstream code still goes out as before. The CLI acts on some of them, e.g. `context_length_exceeded`.
- **Counting overloads:** a client now sees `server_error`, so count overloads from the journal's `upstreamCode` (or
  the event's `upstream_code`), never from the code the client saw. No alert keyed on the client-facing code when this
  shipped (checked 2026-10-12).

**How to check after the rollout.** In the journal, failures with `upstreamCode: server_is_overloaded` should now be
followed by a retry of the same conversation within seconds. After 3 overloaded attempts in a row there should be a
`conversation.rekeyed` event. On a dev worker, an overload shows as `Reconnecting... n/8`, not as "Selected model is at
capacity".

**Image.**
- Archive `router-0.1.0-20261012-codex-retries.tar.gz`, SHA-256
  `39673ce276e8ad9fd7a34d870f27252f944589aa7b745375b8cf7c691e3d8c34`; relay branch `release/router-codex-retries-image`,
  [run 82585](https://git.chifor.me/cchifor/relay/actions/runs/82585).
- Image `registry.chifor.me/llm-router/router@sha256:ce27f28df7900daf807892684939a204041611e9843e3e65c8e23ce5f7c3555e`.
- Before merge: 1745 tests passed, 0 failed, on Linux CI ([run 82571](https://git.chifor.me/cchifor/llm-router/actions/runs/82571)). Both reviewer
  bots approved.

**Rollback.** In `router.yaml`, revert the image digest and both pod-template annotations together:
- `image: registry.chifor.me/llm-router/router@sha256:562f48f323a94be8dbe3d983cf2838892bf954dd30de503f8f10cefad9e01f16`
- `chifor.me/source-commit: 9163faa9c5390a0dd1c31b7e52c8950ffa1e82d6`
- `chifor.me/release: router-0.1.0-20261012-followups`

Nothing else changes: no schema change, no new setting.

## Activity directory table release (2026-10-12)

`router-0.1.0-20261012-activity-table` deploys merged llm-router main `0b0b2514eab20a2257782444ec058508dc16c241`
([#100](https://git.chifor.me/cchifor/llm-router/pulls/100)). It is a web console change only: no server, API, schema
or setting change.

**What it changes.** The Activity page uses the directory table of the TrueSwarm admin's Agents page (trueswarm-admin
`RecordTable` / `DirectoryToolbar` / `TableFooter`, ported to React as `apps/web/table/`):
- **Toolbar:** a search box (request id, model, account; the endpoint's `search`) and a **+ Filter** menu with removable
  chips. Its fields are Result, Source, Route, Served by and Model (`status`, `principal`, `route`, `accountId`, `model`
  on `/admin/v1/trajectories`). Then a Comfortable/Compact toggle and a Columns menu.
- **Rows and headers:** headers sort the loaded rows. Each row shows the source name with the request id under it,
  Result (status pill), Route, Model, Served by, Latency and Time.
- **Loading:** the newest 100 requests. Load more, or scrolling to the end, adds 100 at a time up to the endpoint's 500.
  One query refreshes every loaded row each 10 s.
- **Detail:** it opens only when a row is clicked (× closes it). At 900 px or narrower the open request replaces the
  list.
- **Browser storage:** density and hidden columns are kept per browser (`router.activity.density`,
  `router.activity.hidden`).

**How to check after the rollout.** Open router.chifor.me → Activity:
- The table and its **+ Filter** menu should render.
- Filtering by Source should send `principal=` (visible in the browser's network tab).
- Clicking a row should open the request beside the list.

**Image.**
- Archive `router-0.1.0-20261012-activity-table.tar.gz`, SHA-256
  `1c8a4ed293c58004817cf51ab64d819ddbe57757963e2529be2cab26b62ad8f8`; relay branch
  `release/router-activity-table-image`, [run 82819](https://git.chifor.me/cchifor/relay/actions/runs/82819).
- Image `registry.chifor.me/llm-router/router@sha256:2127e3943e48d5262c41058f314446c1801ff33530f1217dfe353763c12c387b`.
- The run's receipt `deployment-images-82819.json` (relay release v0.2.0) ties the two together:
  `images.router.pinned` is the image above, built from `source 0b0b2514…`, archive `sha256 1c8a4ed2…`,
  `packagingCommit 1088e062…`.
- Before merge: 1768 tests passed, 0 failed, and Playwright passed 22 plus the drag test, on Linux CI
  ([run 82814](https://git.chifor.me/cchifor/llm-router/actions/runs/82814)). Both reviewer bots approved. One red
  run during the review was a CI host fault, not this change: [ci-runners.md, Troubleshooting, `dpkg was
  interrupted`](ci-runners.md#troubleshooting).

**Rollout (2026-10-12, ailab [#1286](https://git.chifor.me/cchifor/ailab/pulls/1286), `96185e0b`).**
- **Drain:** the Deployment changed at 12:31:16 UTC. The old pod drained in 20.2 s, ending 4 long streams as
  `cancelled` / `shutdown`, and its runtime closed in 85 ms.
- **New pod:** ready at 12:31:47, 31 s after SIGTERM. No `abandoned` rows.
- **Checks:** [router acceptance](https://git.chifor.me/cchifor/ailab/actions/runs/82859) passed on the new pod. A
  headless browser on the live Activity page showed the new table with no page errors. A Source filter sent
  `principal=`, a row opened its detail, and the check session signed out.
- **Endpoint baseline** (`GET /admin/v1/trajectories`, admin token), measured 2026-10-12 12:37 UTC:

  | Query | Samples | Min | Median | Max | Size |
  |---|---|---|---|---|---|
  | `limit=100` | 5 | 8 ms | 9 ms | 16 ms | 68.5 KB |
  | `limit=500` | 5 | 25 ms | 29 ms | 33 ms | 348.6 KB |

  - **Path:** `kubectl port-forward svc/llm-router` from the workstation on the mgmt LAN. That skips the edge and
    Cloudflare, so a reading through router.chifor.me adds their latency on top.
  - **Store at the time:** `GET /admin/v1/status` reported `storage.bytes` 268,271,509 (the SQLite store, journal
    included) and `evicted` 5,700. Compare a later reading with that size in mind.
  - **What the console asks for**, as of image `2127e394`: in llm-router `apps/web/pages/Activity.tsx`,
    `refetchInterval: 10_000`, `PAGE = 100` and `MOST = 500`. An open Activity tab asks for 100 rows every 10 s,
    as the previous image did. It asks for up to 500 only after Load more or scrolling to the end.

**Rollback.** In `router.yaml`, revert the image digest and both pod-template annotations together:
- `image: registry.chifor.me/llm-router/router@sha256:ce27f28df7900daf807892684939a204041611e9843e3e65c8e23ce5f7c3555e`
- `chifor.me/source-commit: 4f1fb4f6a79fe3df2a42712448309dc8568d1613`
- `chifor.me/release: router-0.1.0-20261012-codex-retries`

Nothing else changes: no schema change, no new setting.

## Warm spills release and `codex` concurrency (2026-10-12)

`router-0.1.0-20261012-sticky-spills` deploys merged llm-router main `ccfbd1dbe1310ce11b2d9b659ddf6a3d72aba597`
([#101](https://git.chifor.me/cchifor/llm-router/pulls/101)). Earlier the same day, a live config change raised the
concurrency of account `codex` from 1 to 4.

**Why.** The dev workers' Codex conversations live on `pool-codex` with `placement: "conversation"`. The subscription
backend caches prompts per account, so a turn on an account that has not seen the conversation is sent uncached.

Measured 08:10-12:56 UTC on 3,909 dev-worker requests (median input 118k tokens):
- 10 % of turns spilled.
- 27 of 39 conversations touched several accounts, with 339 account changes.
- A turn on an account new to the conversation had a median 33 % of its input cached; one on the same account had 99 %.
- Account changes were 8 % of turns but 25 % of all uncached input.

There were two causes:
- **`codex` concurrency 1.** Every second request in flight on a conversation bound to `codex` spilled. That was 317 of
  the 412 spills in those 5 hours.
- **Scattered spills.** Each spill went to whichever account the strategy picked.

**What changes.**
- **Live config revision 1400 (13:09 UTC), at the owner's request:** `codex` concurrency 1 → 4, like codex-2/3/4.
  - In the first 8 minutes (159 requests) spills fell from 9.9 % to 2.5 %. Conversations bound to `codex` spilled once.
  - Rollback: set it back to 1 (a full `PUT /admin/v1/config` with `If-Match`; change only that field).
- **The image:** a spilled turn goes to the conversation's spill account, while that account answered one of its
  spills within 15 minutes and can take the turn.
  - Passed over: an account serving on credits while another candidate has included quota. In that case, or with no
    warm spill account, the strategy places the spill as before.
  - The spill account is noted when an account answers (first content), never by an attempt that failed first. Each
    answered spill renews the 15 minutes.
  - The record is the router's memory only; a restart forgets it. The binding is unchanged.
- **README fix:** the router's README no longer claims that encrypted reasoning only decrypts on the account that produced
  it. That was measured portable between Pro subscriptions. The Pro ↔ Pro Lite check is still open: codex-3 and
  codex-4 are Pro Lite.

**How to check after the rollout.**
- **Wait first.** The spill record lives in memory and the rollout is Recreate, so the new pod starts with none. Each
  conversation's first spill after a restart is placed by the strategy, so measure from about 15 minutes after the
  rollout, over at least 30 minutes of dev-worker traffic.
- **Query.** Port-forward `svc/llm-router` and page `GET /admin/v1/trajectories?limit=500&before=<oldest startedAt>`
  over the window. Keep rows whose `source.name` starts with `dev-worker`, then group by `principalId` +
  `conversation` in `startedAt` order. Per request:
  - `placement`;
  - whether `accountId` differs from the conversation's previous request (an account change);
  - whether that account served the conversation in the previous 15 minutes (a warm change) or not (a cold change);
  - `usage.cachedInputTokens / usage.inputTokens`.
  For a spilled request, `GET /admin/v1/trajectories/:id` shows `routing.evaluated` with `placement.warmSpill` when
  the warm spill account was used.
- **Pass:**
  - account changes under 3 % of turns;
  - cold changes under 1 % of turns;
  - same-account turns at a median of at least 95 % cached;
  - every spill of a conversation that spilled in the previous 15 minutes either names `warmSpill`, or its
    `routing.evaluated` decisions show that account not eligible (busy, offline, credits beside quota).
- **Measured** (dev workers, `pool-codex`; the same query in three windows). Percentages are of the window's turns:

  | Window (UTC) | Requests | Spilled | Account changes | Cold changes | Uncached input from changes |
  |---|---|---|---|---|---|
  | 08:10-13:09, `codex` concurrency 1, old image | 4,129 | 412 (10.0 %) | 351 (8.5 %) | 74 (1.8 %) | 25 % |
  | 13:09-14:06, concurrency 4 (rev 1400), old image | 794 | 20 (2.5 %) | 37 (4.7 %) | 3 (0.4 %) | 23 % |
  | 14:21-14:58, concurrency 4, warm-spills image | 562 | 0 | 0 | 0 | n/a (no changes) |

  - **Window 2:** it starts at a config change, not a restart. The old image keeps no spill record, so there was nothing
    to wait for.
  - **Window 3:** it starts 15 minutes after the 14:06 rollout, per "Wait first". In that window every request stayed
    on its conversation's account, at a median of 99 % cached.
  - **Counting:** an account change is any request on a different account from the conversation's previous request.
    - Window 2 (20 spills, 37 changes): 18 home → spill, 18 spill → home, 1 spill → another spill account; 1 spill
      followed a spill on the same account, which is no change.
    - Window 1 (412 spills, 351 changes): 130 home → spill, 124 spill → home, 93 spill → another spill account and 4
      moves. 188 spills followed a spill on the same account (runs while `codex`, at concurrency 1, stayed busy) and
      are no change. The 412th spill was its conversation's first request in the window, with no previous request to
      compare.
    - The 93 spill-to-spill changes on another account are the scattering warm spills prevent.
  - **Which threshold matters:** the old image at concurrency 4 already met the cold-change bar (0.4 %). The account-change
    bar (3 %) is the one that told the windows apart.
  - **The concurrency raise removed the hops.** With no spill since the rollout, the warm-spill path itself is **not
    yet exercised in production**, and the `warmSpill` criterion is unverified. The engine tests cover it.
  - **To exercise it on purpose:** lower one account's concurrency for about 30 minutes (live config) and rerun the
    query. That costs the dev workers cache while it lasts, so it is an owner decision. Otherwise check it the next
    time spills appear, for example when a subscription reaches its limit.
- **Pro and Pro Lite.** codex-3 and codex-4 are Pro Lite; codex, codex-2 and codex-5 are Pro. Encrypted reasoning was
  measured portable between Pro subscriptions only (llm-router `docs/design/per-key-routing.md` §2.2).
  - An account becomes a spill account only once it answers (first content), so one that refuses foreign reasoning
    with an error never becomes one.
  - A silent loss would not fail the request: the answer succeeds, but without the earlier reasoning. Sticky spills
    would then repeat it for that conversation for as long as its spills stay on that account.
  - **Interim signal:** dropped reasoning items should make the prompt smaller than the previous turn. So compare
    `usage.inputTokens` of a turn after a plan-crossing account change with the previous turn's, against same-plan
    changes. Only changes where both turns completed count: a failed, cancelled or open turn has no comparable usage.
    On 2026-10-12 (08:10-14:58) that was 268 of the 388 account changes; the other 120 had a turn that did not
    complete. The median next/previous ratio was:
    - Pro → Pro Lite: 1.018 (n = 43);
    - Pro Lite → Pro: 1.000 (n = 42);
    - same plan: 1.007 (n = 183);
    - no change: 1.005.
    That is no systematic drop. It is weak evidence only: the backend may count input it then ignores, and a local
    compaction also shrinks the prompt.
  - Until the Pro ↔ Pro Lite check is done, that is the open risk. If the check fails, prefer spills within the home
    account's plan type, or split the pool by plan type.

**Image.**
- Archive `router-0.1.0-20261012-sticky-spills.tar.gz`, SHA-256
  `e7cabb9f43567680e1d5fa62782e1405114215444642249c633e95ac120f2b48`; relay branch `release/router-sticky-spills-image`,
  [run 83008](https://git.chifor.me/cchifor/relay/actions/runs/83008).
- Image `registry.chifor.me/llm-router/router@sha256:f8ddc9ce9353f6e3a5f7bcc75b8ff16d2cfa29cf0a5e801358b57d9dbc4bec54`.
  The receipt `deployment-images-83008.json` (relay release v0.2.0) pins it from `source ccfbd1db…`, archive
  `sha256 e7cabb9f…` and `packagingCommit c12bc466…`.
- Before merge: 1775 tests passed, 0 failed, and Playwright passed, on Linux CI
  ([run 82979](https://git.chifor.me/cchifor/llm-router/actions/runs/82979)). Both reviewer bots approved.

**Rollback.** In `router.yaml`, revert the image digest and both pod-template annotations together:
- `image: registry.chifor.me/llm-router/router@sha256:2127e3943e48d5262c41058f314446c1801ff33530f1217dfe353763c12c387b`
- `chifor.me/source-commit: 0b0b2514eab20a2257782444ec058508dc16c241`
- `chifor.me/release: router-0.1.0-20261012-activity-table`

Nothing else changes: no schema change, no new setting. The `codex` concurrency is separate config and stays.

## Reviewer routes (2026-10-13)

**Why.** At the owner's request, the PR reviewer bots get routes of their own: `codex-reviewer` on the Codex smart
pool, and `claude-reviewer` on claude-5, falling back to the Claude smart pool when claude-5 fails.

**Revision 1477** added two routes:
- `codex-reviewer`: a pointer to `pool-codex`, like `dw-1` … `dw-4`.
- `claude-reviewer`: one target, `claude-reviewer:claude-5`.
  - Strategy `priority`, affinity `none`, `access: ["subscription"]`, `maxAttempts: 3`,
    `fallbackAliases: ["pool-claude"]`.
  - It has no timeouts of its own, like the direct `fable` route reviewer-claude uses now.

**Revision 1479** changed that target's model from `fable` to `opus`, and it stays there.
- **Opus 5.5.** `opus` is the router's Claude Code alias for `claude-opus-5-5` (2.1.281, `latest_per_family`; `fable`
  is `claude-fable-5-1`). The owner ruled on 2026-10-13 that the Gitea reviewer need not use `fable`:
  - claude-5 serves the reviewer on Opus 5.5;
  - the route falls back only when claude-5 cannot take the request.
- **Why not `fable`:** claude-5 refuses it until 2026-11-01 00:00 UTC (429 `RATE_LIMITED`; the reply named no
  window). With `fable`, every request failed over to the pool.
  - **Where the date comes from:** the router's reading of that refusal. `GET /admin/v1/usage` showed claude-5 at
    `limits.status: "limited"`, `source: "reply"`, `limitedUntil: 1793491200000`.
  - That is the `resetsAt` Claude Code reported with its rejection (`mapClaudeLimits` in `provider-claude`). No
    dashboard or support reply confirms it.
- **The account itself serves.** A `haiku` probe a minute later read 5-hour 0 % and weekly 27 %
  (`POST /admin/v1/usage/refresh`; the probe always asks for `haiku`).
- Revision 1478, between the two, was the router's own cooldown write after that 429.

**How the fallback behaves.** Verified 15:07-15:10 UTC with administrator requests:
- **claude-5 not eligible** (busy at its concurrency, cooling down, or offline): the same attempt goes to
  `pool-claude`. At concurrency 1, of two simultaneous requests, claude-5 served one on `opus` and `pool-claude`
  (claude-3) the other. Revision 1480 raised it to 4 (below).
  - reviewer-claude sends one request at a time: 200 requests from 2026-10-12 07:19 to 2026-10-13 14:57 UTC, at most 1
    in flight.
  - So "busy" means another router client (`pool-claude`, `pool-mixed`, the `claude` route) holds claude-5's slot. In
    the 24 hours before these routes, claude-5 served no router request.
- **A retryable failure on claude-5:** the next attempt goes to `pool-claude`. With the 429 on `fable`, claude-5
  failed in 0.9 s and cooled down for 60 s, and claude-3 answered.
- **The model changes on fallback.** `pool-claude` answers on its pool model, `sonnet`, so a review that falls back is a
  `sonnet` review.
- **Telling which target served a review:**
  - **Response header:** `x-router-route: claude-reviewer` means claude-5 served it; `pool-claude` means the
    fallback did.
  - **Journal:** `GET /admin/v1/trajectories?route=claude-reviewer` gives each request's `accountId`. Its detail
    (`/admin/v1/trajectories/:id`) has `attempt.started` events with the `deploymentId` and `model` of each attempt.
- **Quota readings are ignored.** A `priority` route ignores them: while claude-5 is limited, every request after its
  60 s cooldown tries it first. Each such try costs about 1 s and one of the 3 attempts.
- **If claude-5 becomes limited on `opus` too:** stop that waste with a full `PUT` and `If-Match`.
  - Set the target's model to one that claude-5 still serves.
  - Or set its `enabled` to `false`. A disabled target is not eligible (`disabled`), so each request goes straight to
    `pool-claude` in its first attempt.
  - Re-enable it once claude-5 serves again.

**Revision 1480: Claude concurrency 1 → 4.** At the owner's request, like the Codex accounts, all five Claude accounts
(`claude`, `claude-2` … `claude-5`) now have `concurrency: 4`.
- **Verified:** two simultaneous `claude-reviewer` requests were both served by claude-5 (until then the second went
  to `pool-claude`).
- **The pod still runs at most 2 Claude requests at once.** `provider-claude` caps the Claude Code processes per pod
  (`MAX_RUNNING = 2`, hard-coded: each run is a full runtime in a pod limited to 1280Mi).
  - A third request waits for a free run for up to 60 s.
  - It then fails with 429 `CLAUDE_BUSY` (retryable), and the route's next attempt goes elsewhere.
  - So the concurrency raise stops an account from looking busy, but more Claude work at once needs an llm-router change
    (a configurable cap) and a higher memory limit. That followed the same day: see "Claude run cap release
    (2026-10-13)" below.
- **Rollback:** set the five back to 1, in one full `PUT` with `If-Match` that changes only those fields.

**How the bots call them.** Wired on 2026-10-13 (`ansible/host_vars/reviewer-{1,2}.yml`; dev-workers.md § "Router
seats").
- **`claude-reviewer`: route mode**, `model: "claude-reviewer"`.
  - reviewer-claude's key (`key_f856495c9b0c`) may use every route, so it did not change.
  - Its router seat carries `model: claude-reviewer`, so every ladder tier (`fable`, `opus`, `sonnet`) asks for the
    route.
- **`codex-reviewer`: model mode only.**
  - `pool-codex`'s targets are `*`, so `model: "codex-reviewer"` gets 400 `MODEL_REQUIRED`.
  - The request names a model of `pool-codex`'s allowlist (`gpt-6-astra`) and selects the route with
    `X-Router-Route: codex-reviewer`, or through a key whose `defaultRoute` it is.
  - The reviewbot sends no header, and the old key (`key_d7adf31fb93e`) may use only `codex`, with no default route.
    Keys cannot be edited.
  - So reviewer-codex has a new key, `key_4fdbe2b833fe`: `{routes: ["codex-reviewer"], defaultRoute: "codex-reviewer"}`,
    365 days, escrowed as `reviewbot_codex_router_key`.
  - Its seat has no `models` map and sends the tier name, `gpt-6-astra`.
  - Verified with that key before the switch: `gpt-6-astra` gave 200 via `pool-codex`; `codex` gave 400
    `MODEL_NOT_SUPPORTED`. So the key and the seat change must deploy together, which one `reviewers.yml` run does.
  - The old key stays valid until the new route has run for a while: rolling back is then a seat change.

**Check** without spending quota: `POST /admin/v1/preview` as the administrator.
- `{"model": "claude-reviewer", …}` selects `claude-reviewer:claude-5`.
- `{"model": "gpt-6-astra", …}` with `X-Router-Route: codex-reviewer` selects a `pool-codex` target.

**Rollback.** Move every client off the routes first, then delete them. A key whose only route is deleted serves
nothing.
1. **reviewer-claude:** drop `model` from its router seat, so the ladder's own names go back to the router. Its key may
   use every route, so it does not change.
2. **reviewer-codex,** if it was switched:
   - **While the old key still exists** (`key_d7adf31fb93e`, `codex` only): restore it as `reviewbot_codex_router_key`
     (revert that SOPS change), restore the seat's `models: { gpt-6-astra: codex }`, and deploy both in one run.
   - **Once it has been revoked:** keys cannot be edited, so first issue a replacement in the old shape:
     `{routes: ["codex"]}`, with no `defaultRoute`. Seed it into the seat and restore the mapping to `codex`. Check that
     reviews are served (journal rows of that key with `model: codex`).
   - Then revoke the `codex-reviewer` key.
3. Delete both routes in one `PUT /admin/v1/config` with `If-Match`. No other route, pointer or fallback references
   them.

## Claude run cap release (2026-10-13)

`router-0.1.0-20261013-claude-run-cap` deploys merged llm-router main `6ef7eee235f7e5ea5d0c118249659ed99a63434b`
([#102](https://git.chifor.me/cchifor/llm-router/pulls/102)), on the owner's request ("raise the pod MAX_RUNNING cap
too").

**What changes.**
- **The cap is a setting.** `provider-claude` capped the pod at 2 Claude Code runs at once (`MAX_RUNNING`,
  hard-coded). It is now the plugin setting `maxRunning`, "Parallel runs", 1-32.
  - Edit it under Settings → Plugins → provider-claude-subscription, or with
    `PATCH /admin/v1/plugins/provider-claude/config`.
  - A change applies at once: a raise starts waiting requests, a cut lets running ones finish.
  - The code default stays 2, so the image alone changes nothing.
- **The slots.** The FIFO wait, the 60 s timeout before 429 `CLAUDE_BUSY` (Retry-After 5 s) and the once-only release
  are unchanged. A wait that times out no longer leaves its abort listener on the request signal.
- **The pod (`router.yaml`).**
  - Memory limit 1280Mi → 3584Mi, memory request 384Mi → 2560Mi, CPU limit 2 → 4.
  - Measured on the live pod: the router process ~310 MiB, one Claude Code run (2.1.281, `sdk-resume`) ~210 MiB.
  - Worst case at 8 runs: 310 + 512 (Assistant image jobs) + 8 × 210 ≈ 2.5 GiB, about 70 % of the limit.
  - **The old 1280Mi was already too small.** At the old cap of 2, the worst case (310 + 512 + 2 × 210 ≈ 1.2 GiB)
    filled about 97 % of it. Never go back below 2Gi, whatever the cap.
  - The request is 2560Mi, the 8-run worst case, reserved on the node.
    - One replica with `Recreate` does not stop other pods from overcommitting the node; a request does.
    - Keep the request in step with `maxRunning`: `822 + N × 210` MiB.
  - The agent nodes have 16 GiB at about 20 % used.

**After the rollout** (live setting): `maxRunning` 2 → 8.
- The value lives in `/data/plugins.yml` (admin API), not in `router.yaml`, and reverting the manifest does not reset
  it.
- Set it only once the pod with the 3584Mi limit is running.
- Before ever lowering the limit, lower the setting first.
- **The ceiling is the memory, not the schema.** The setting accepts 1-32, but 3584Mi holds 8 runs at about 70 % and
  about 13 at 100 % ((3584 − 310 − 512) / 210).
  - Keep `maxRunning` at 8 or below with this limit. An OOM kill drops every request in flight on the single
    `Recreate` replica.
  - For more runs, raise the limit first, to at least `(822 + N × 210) / 0.7` MiB for N runs, then the setting.

**Rolled out 2026-10-13.** ailab PR #1311 merged as `2237b6dd`.
- **Rollout:** Flux applied it at 16:35 UTC.
  - The old pod drained in 18.4 s (`runtime closed in 62 ms`). The new pod `llm-router-886d985ff-wxpsv` (talos-agent-node-2,
    limits 4 CPU / 3584Mi, request 2560Mi) was ready about 25 s after the old one stopped being ready.
  - In the journal, 16:24:30-16:37: 142 requests, 140 complete, none `abandoned` and none `SHUTDOWN`. The one failure
    (16:32) and the one cancel (16:24) came before the rollout.
  - Router rollout acceptance passed:
    [run 85092](https://git.chifor.me/cchifor/ailab/actions/runs/85092).
- **Setting (about 16:39):** `PATCH /admin/v1/plugins/provider-claude/config` set
  `{"transport": "sdk-resume", "maxRunning": 8}` with `If-Match`. Active at once (`pending: false`); `/data/plugins.yml`
  now holds the same.
- **Verified at 4 (16:40).** Four simultaneous `claude-reviewer` requests ran four Claude Code processes at once, where
  the old cap allowed two.
  - About 210 MiB RSS each, as measured before.
  - Pod memory peaked at about 1.2 GiB of 3584Mi (cgroup `memory.current`).
  - All four were served by the router account `claude-5` (a Claude subscription; concurrency 4 since revision 1480),
    on `opus`, in 9-12 s.
- **Verified at the full cap of 8 (16:43).** Eight simultaneous `claude-reviewer` requests ran eight Claude Code
  processes at once.
  - Four were served by `claude-5` (its concurrency of 4) and four by the fallback `pool-claude`, all with 200, in
    9.5-14.8 s.
  - The pod cgroup's `memory.peak` was **1719 MiB, 48 % of 3584Mi**: anon about 1070 MiB plus 563 MiB file cache. The
    Claude processes' summed RSS was about 1.75 GiB (about 218 MiB each) and the router's about 260 MiB.
  - The cgroup counts the pages the processes share (the Claude Code binary) once, which is why it is lower than the
    summed RSS.
- **Reading these numbers against the sizing rule.** `822 + N × 210` MiB is a deliberately conservative upper bound,
  so the samples do not argue for a lower limit.
  - It counts the Assistant's 512 MiB of image-job headroom (no image job ran during these tests) and adds RSS without
    the sharing above.
  - At 8 runs that bound is ~2.5 GiB, about 70 % of 3584Mi. The 8-run sample plus 512 MiB of image jobs is about
    2.2 GiB, 62 %.

**Image.**
- Archive `router-0.1.0-20261013-claude-run-cap.tar.gz`, SHA-256
  `ae35f96c3a205d3ce4ce90086ae9941b710179481cf089a3af5f826d8cdcfd26`.
  - Built in the pinned Node 26.10.0 image on dev-worker-1, and the production smoke passed.
  - Relay v0.2.0 asset 214.
- Relay branch `release/router-claude-run-cap-image` (`e7f75394`),
  [run 85014](https://git.chifor.me/cchifor/relay/actions/runs/85014); receipt `deployment-images-85014.json`.
- Image `registry.chifor.me/llm-router/router@sha256:6cbb671d1e2f773cb8407d58ec99b4cb55741eb1f2c351518bcac88137b8ac91`.
- Before merge: CI on Linux passed after one rerun.
  - The first run failed on an unhandled rejection from `subscription-login/emails.ts`: a log call after a test
    runtime closed, while `tests/smoke-pool.test.ts` ran. That is a teardown race this change does not touch; all 1784
    tests had passed.
  - Both reviewer bots approved.

**Rollback.**
1. **First remove the setting.** `PATCH /admin/v1/plugins/provider-claude/config` with
   `{"values": {"transport": "sdk-resume"}}` (no `maxRunning`), with `If-Match` set to the view's etag.
   - The cap returns to the default 2, the old image's hard-coded cap.
   - The save stores exactly the submitted values in `/data/plugins.yml` (`provider-claude` held only
     `{"transport":"sdk-resume"}` before this release), so the file no longer carries `maxRunning`. The old image's
     schema is `.strict()`, so it would refuse the extra key.
2. **Then revert `router.yaml`:** only the image digest and both pod-template annotations, together.
   - `image: registry.chifor.me/llm-router/router@sha256:f8ddc9ce9353f6e3a5f7bcc75b8ff16d2cfa29cf0a5e801358b57d9dbc4bec54`
   - `chifor.me/source-commit: ccfbd1dbe1310ce11b2d9b659ddf6a3d72aba597`
   - `chifor.me/release: router-0.1.0-20261012-sticky-spills`
   - **Keep the resources block.** It does not depend on the image, and the old 1280Mi was undersized even at 2 runs
     (above).

## Activity search release (2026-10-13)

`router-0.1.0-20261013-activity-search` deploys merged llm-router main `2baf22df220a7ce0811e98aeee55b87593528e3d`
([#103](https://git.chifor.me/cchifor/llm-router/pulls/103)), on the owner's request: the PR reviewer bots did not show
on the Activity page.

**Why they did not show.** The page lists the newest 100 requests, and "Load more" stops at 500.
- On 2026-10-13, 100 requests covered only 2.4 minutes, 79 of them from dev-worker-1. The bots make about 20 requests an
  hour each.
- The Source and Route filters offered only the loaded rows' values, so the bots were not in the filters either.
- The search matched only the request id, model, account and agent.

**What changes.**
- **Filters.** Source also offers every active API key, and Route every configured route (`GET /admin/v1/keys`, the
  configuration; the same cache entries as the API keys and settings pages). If those calls are refused, the options
  are the loaded rows' values, as before.
- **Search.** `GET /admin/v1/trajectories?search=` also matches the entered route (model mode), the principal id and the
  source's name.
  - A key's name matches only rows of the key's own namespace, as the displayed source does, so a search never reveals
    a name the row would not show.
  - The matching (namespace, principal) pairs reach SQLite as one JSON parameter, so every matching key counts.
  - **Cost:** the route term reads each row's JSON. A search that matches nothing scans the whole journal: ~0.48 s on
    41k rows (2026-10-13), once per 10 s refresh while a search is active. That is about what the `route` filter
    already cost.
- **So, for the reviewers:** search `reviewer`, `claude-reviewer` or `codex-reviewer`, or pick them under Source or
  Route.

**Image.**
- Archive `router-0.1.0-20261013-activity-search.tar.gz`, SHA-256
  `71e8c095cbbacd03fc20dc0951af5611997dde46458c25bceb075cc1fa59480f`.
  - Built in the pinned Node 26.10.0 image on dev-worker-1, and the production smoke passed.
  - Relay v0.2.0 asset 216.
- Relay branch `release/router-activity-search-image` (`1788f0c6`),
  [run 85184](https://git.chifor.me/cchifor/relay/actions/runs/85184); receipt `deployment-images-85184.json`.
- Image `registry.chifor.me/llm-router/router@sha256:62d4885cd0954687ed128bf9e0ac76c0366356991f0cf1f8467839ca6b33ddef`.
- Before merge: CI on Linux passed, and both reviewer bots approved (after the 500-key cap was removed).

**Rollback.** Revert the image digest and both pod-template annotations together, and keep the resources:
- `image: registry.chifor.me/llm-router/router@sha256:6cbb671d1e2f773cb8407d58ec99b4cb55741eb1f2c351518bcac88137b8ac91`
- `chifor.me/source-commit: 6ef7eee235f7e5ea5d0c118249659ed99a63434b`
- `chifor.me/release: router-0.1.0-20261013-claude-run-cap`

Nothing else is needed: there is no schema change and no new setting, and `maxRunning` stays (that image has it).

**Rolled out 2026-10-13.** ailab PR #1313 merged as `aea2b77c`.
- **Rollout:** Flux applied it at 17:38 UTC.
  - The old pod used its whole drain budget (`drained in 20151 ms`, `runtime closed in 98 ms`). The new pod
    `llm-router-7c87f55f78-wdtmw` (resources unchanged, 0 restarts) was ready about 20 s after the old one stopped.
  - In the journal, 17:33-17:39:30: 207 requests, none `abandoned`.
  - **Six long streams were cut by the restart** (`cancelled`, cause `shutdown`): five from dev-worker-1 and one from
    `covenant-demo-luna`. Codex clients retry a cut stream (the dev workers allow 8 stream retries).
  - The other cancels in the window were `superseded`, the normal Codex follow-up pattern.
  - Router rollout acceptance passed: [run 85227](https://git.chifor.me/cchifor/ailab/actions/runs/85227).
- **Setting:** `maxRunning` 8 is still active after the restart, read from `/data/plugins.yml`.
- **Verified on the live router:**
  - `?search=codex-reviewer` returns reviewer-codex's requests (29 rows; its model is `gpt-6-astra`, so only the entered
    route finds them).
  - `?search=claude-reviewer` returns reviewer-claude's (27).
  - `?search=reviewer` returns both bots' keys, including reviewer-codex's old one.
  - `REVIEWER-CODEX` matches the name regardless of case.
  - Each search took 0.09-0.6 s.
  - The page served at router.chifor.me carries the new search placeholder.

## Relay managed-key bridge (v0.6.0)

The `kubernetes/components/llm-router-relay-keys` component remains opt-in and
supplies the trust file that enables the `relay-keys` plugin. A follow-up
activation PR may include it only after the operator reviews the exact router
image, issuer/tenant mapping, key ceilings, OpenBao policies and staged worker
services described in [Relay's preflight](relay.md#preflight-before-merging-activation).
The activation gate test enforces this boundary in CI.
Live token and holder acceptance follows the constrained activation; production
profile/pool admission remains disabled until that report is reviewed.

Provision the external Secret `llm-router/llm-router-relay-keys-trust` through
the approved secret-management route with one key, `trust.json`. It contains
only the versioned public trust mapping consumed by the plugin: the Relay
issuer, Ed25519 public key IDs and roles, the approved tenant UUID, router
namespace, route aliases, access kinds, maximum lifetime and key-count ceiling.
It must not contain private keys or router bearer credentials. The init
container bounds the JSON, writes it atomically to a 0600 in-memory file, and
the router validates key types, duplicates and ceilings again at startup.
The component preserves the base router's existing UID/GID/fsGroup 1000
identity (the same identity that owns the SQLite data volume); it makes those
values explicit so the trust file cannot introduce a volume-ownership change.

Activation is a reviewed Flux PR that includes this component and pins the
router image containing the reviewed bridge. The rollout must prove that the
route is unreachable without a valid Relay assertion, tenant and grant ceilings
are enforced, and disabling the component removes the bridge without revoking
existing keys. Keep the prior router image and SQLite backup as rollback
material. A private OpenBao origin or custom CA requires a separate narrow
network/TLS change; never widen the router policy to arbitrary private egress.
