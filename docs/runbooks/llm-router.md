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
- NetworkPolicy admits :8787 only from `edge` cloudflared pods. Egress permits cluster DNS, public HTTPS (RFC1918 excluded), seat hosts in the namespace on :8791, and the mgmt LAN `192.168.0.0/24` on the model-server ports only (8080, 8081, 8082, 18020, 11434, 1234, 8000).

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

**Schema.** SQLite `user_version` 7 adds a `placements` table. The migration is `CREATE … IF NOT EXISTS`, so an older
image starts on the same volume and ignores the table.

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
