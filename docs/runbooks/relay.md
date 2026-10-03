# Relay

The initial operator handoff is archived in [the deployment record](../../plans/2026-10-01-relay-operator-handoff.md).

Relay follows the existing llm-router topology: Cloudflare proxied CNAME → the locally managed ailab tunnel → `relay.relay.svc.cluster.local:80`, with the app handling browser/connector authentication. The public origin is `https://relay.chifor.me`; it is intentionally free of interactive Cloudflare Access challenges so connector WebSockets can authenticate using their own credentials.

The authoritative manifests live in `cchifor/ailab`, directory `kubernetes/apps/apps/relay`. The tunnel route is in `apps/edge/cloudflared.yaml`; DNS is declared in `kubernetes/infra/cloudflare/variables.tf`. Both require the normal protected-main GitOps review flow. Do not patch the shared tunnel or suspend Flux to bypass that flow.

## Release payload

`relay-0.1.2-20261001` is a versioned, immutable release containing compiled server/UI code, the production dependencies, and public connector downloads extracted from the tested `relay-platform:0.1.2` image. It is mounted read-only under a digest-pinned Node 22.23 runtime. There is no package installation at startup.

- `relay-releases`: 2 GiB RWX `nfs-csi`, code only.
- `relay-data`: 2 GiB RWO `qnap-iscsi`, plugin artifacts.
- `relay-postgres-data`: 5 GiB RWO `qnap-iscsi`, PostgreSQL 17 data and WAL.
- `relay-postgres-dumps`: 5 GiB RWO `qnap-iscsi`, consistent logical backups, continuously mounted for Velero.
- Namespace and persistent volumes are protected from Flux pruning; an intentional deletion needs an explicit follow-up change.
- Single Relay replica with `Recreate`; single PostgreSQL instance. A credential-free holder sidecar keeps the backup volume mounted in the same pod as the plugin volume. This deployment is not HA.
- Restricted Pod Security, non-root containers, dropped capabilities, read-only roots, no service-account token, default ingress isolation, and explicit DNS/Postgres/public-HTTPS egress.
- Existing Relay administrator and GPT Luna router credentials are SOPS-encrypted for Flux; no estate credentials are given to the app.

## Initial deployment sequence

1. Use an explicitly authorized ailab deployment kubeconfig. The worker's test/observer kubeconfigs are insufficient.
2. Apply Relay's namespace (operator), individually encrypted Secret projections, PVCs, PostgreSQL bootstrap ConfigMap, PostgreSQL, network policies, and backup resources. Keep the backup CronJob suspended until restore completes. Wait for the database.
3. Create the operator-only `staging/relay-stage.yaml` pod, which is deliberately excluded from Flux's Kustomization. It mounts Relay's own release and artifact volumes.
4. Verify the release archive SHA-256, then extract its versioned directory under `/releases`. Never rewrite a currently mounted release.
5. Briefly stop the local Relay service to freeze writes. Back up PostgreSQL with `pg_dump -Fc`, excluding session/lease/enrollment-token data, and archive `.data/plugins`. Transfer and restore them only to Relay's new database and data volume. Never publish these backups as source/release assets.
6. Restore to the fresh database as `relay_migrator` with `pg_restore --no-owner --no-acl --exit-on-error`, then run the release's migration and bootstrap commands through the migration init container. Default privileges established during initdb grant runtime/backup access to restored tables. Preserve existing tenant IDs, agents, conversations, and plugin state. Never restore through the runtime account. The restricted application role must not bypass RLS.
7. Delete the staging pod before starting the application, so the RWO artifact volume cannot block scheduling. Start Relay and verify readiness, login, plugin state, Assistant, and WebSocket origin/authentication before publishing.
8. DNS was created by the operator: proxied `relay.chifor.me` CNAME → `f93d9a6a-5172-43d3-8bef-13460ea7607b.cfargotunnel.com`, record ID `d01f099abc5e5a85c6c5bef5791c8e41`. `relay-import.tf` adopts it into the existing operator state. Do not apply an empty Terraform state against the estate. Run and restore a logical backup successfully, then unsuspend the nightly CronJob before exposing the app.
9. Merge the reviewed AILab PR and reconcile its Flux source/apps Kustomization. The tunnel revision annotation triggers both tunnel connectors to reload the added route.
10. Verify public HTTPS, authenticated browser flows, unauthenticated 401 responses, and connector WebSocket transport. Keep the stopped local database and snapshot as rollback material; reconnect any agent hosts to the new HTTPS origin.

The existing administrator token remains valid. The Assistant uses the already approved `Relay Workspace Assistant` router identity and `gpt-6-luna`; it never receives the router administrator credential.

## Database identities and secrets

The image bootstraps with `POSTGRES_USER=postgres`, whose credential is available only in the PostgreSQL container. An initdb script creates four separate roles. TCP authentication as `postgres` is rejected; operator recovery uses the local socket through authorized `kubectl exec`.

| Role | Purpose | Privileges |
| --- | --- | --- |
| `relay_migrator` | Migration init container | Owns database/schema/tables; no superuser, CREATEROLE, CREATEDB, or RLS bypass |
| `relay_owner` | Runtime control-plane pool | DML and RLS bypass; no DDL ownership, superuser, CREATEROLE, or CREATEDB |
| `relay_app` | Tenant-scoped runtime pool | Explicit table grants with forced RLS; no bypass |
| `relay_backup` | Logical dump job | Read-only table/sequence grants and RLS bypass; no mutation or role administration |

These roles are provisioned **before restoring data**. The immutable release's migrator accepts the already-created `relay_app` role. Runtime secrets contain neither the migrator nor the bootstrap DSN. Default privileges for objects created by `relay_migrator` preserve runtime and backup grants on future migrations.

Each of the four Secret resources is encrypted as a separate SOPS document/file. Never combine multiple resources under one SOPS MAC: Flux decrypts individual Kubernetes resources. Per-resource decryption was verified using a temporary verification recipient; its wrapping key was removed while retaining the validated ciphertext/MAC. Only the estate recipient is committed.

## Logical backups

`relay-postgres-dump` runs at **00:15 UTC daily**, before Velero's 02:00 daily. The database init container writes a `pg_dump -Fc` archive and table of contents. A second, credential-free Node container extracts current and rollback plugin references from that exact archive (not a later live query), copies those source files from the read-only artifact volume, verifies each content hash against the archived manifest, and writes reconstructed manifests, `recovery.json`, checksums, and a `COMPLETE` marker. Only then is the whole generation atomically published. Missing, corrupt, or incompatible plugin files fail the generation. Only successful publication allows pruning; the newest seven completed generations remain. Failed writes keep previous backups. The job has a 30-minute deadline and is colocated with Relay for both RWO claims. Artifact validation covers current and previous plugin versions; future artifact garbage collection must respect running backup readers, or backups will fail safely until retried.

Relay's `backup-holder` sidecar continuously mounts the dump volume read-only, without credentials, for Velero filesystem backup. Keeping the holder in Relay's pod ensures both RWO volumes relocate together after rescheduling. The live PostgreSQL `database` volume is explicitly excluded from filesystem copying; it is not a consistent backup. Restore uses one completed generation's logical dump and its own `plugins/` tree, the immutable release archive, and the SOPS secrets. A later independent filesystem snapshot of `relay-data` is not required to recover that generation. The roles/bootstrap configuration are recreated from Git; role passwords are not published in dumps.

To test a fresh backup, create a Job from the CronJob and wait for completion. Check `SHA256SUMS`, then restore the archive into a **separate disposable database** as the migration role with `--no-owner --no-acl --exit-on-error`. Compare row counts and verify runtime grants and RLS before considering it restorable. Restore the generation's `plugins/` directory to a fresh data volume and confirm all archived hash references can load. A successful `pg_restore --list` alone is insufficient. Clean up the disposable database after the comparison.

This is daily logical recovery, not PITR. Both the local role-isolation drill and the first live dump/restore passed with the real workspace snapshot. The initial dump restored into a disposable database with matching row counts, all nine forced-RLS tables, and `relay_migrator` ownership. After artifact coordination was added, generation `20261001T191324Z-aab52654-4658-4f07-af64-34cfbf01a255` captured the database and five verified artifact versions together. Its checksums passed and it was restored into an isolated PostgreSQL/runtime environment; login and all five plugins passed using only that generation's files. Disposable restore databases were removed. A subsequent Velero capture is still to be verified; a mounted PVC alone is not proof of an offsite backup.

### Backup-gated GitOps rollouts

Every new Relay Pod now captures a coordinated recovery generation before its
migration init container can start. The single-replica `Recreate` strategy stops
the old control plane before the replacement Pod runs these steps:

1. `backup-database` uses the existing read-only `relay_backup` identity and
   unchanged `dump.sh` to capture PostgreSQL and extract that archive's plugin
   references. Its attempt has a 30-minute timeout.
2. `backup-artifacts` uses the existing credential-free `publish.mjs`, with
   read-only access to the plugin volume. The rollout wrapper preserves the
   original dump inputs across init retries, verifies every published checksum
   and the complete file inventory, and writes
   `/tmp/relay-pre-migration-backup.json` with the generation name, Pod UID and
   archive SHA-256. A publishing attempt has a 30-minute timeout.
3. `migrate` requires that receipt before migration and bootstrap. Application
   containers start only after all three init containers succeed. The runtime
   has neither the backup credential nor a mount of the backup volume.

This permits a fresh backup and deployment entirely through the normal reviewed
GitOps change when an imperative deployment kubeconfig is unavailable. Pin the
receipt's application image digest in both `migrate` and `relay`, update the
release/source annotations, and merge only after the protected-main review and
CI gates pass. Flux continues its normal reconciliation; there is no manual
backup Job, Secret change, additional RBAC, or protection bypass. A backup error
holds the replacement Pod in init and leaves the service unavailable until the
cause is fixed through the appropriate reviewed change or authorized recovery.

The shared `apps` Kustomization can temporarily report NotReady when backup work
exceeds its five-minute health timeout. Flux retries after one minute; this does
not terminate the Pod or restart the backup. The Deployment's progress deadline
also reports status without cancelling init work. An already-running scheduled
backup can delay attachment of the RWO volumes on another node until that Job
finishes (its deadline is 30 minutes). Inspect actual init progress before
treating those transient health reports as a failed migration.

Do not accept an old Pod's health response as rollout evidence. Check the
expected new release's public connector manifest and UI assets along with
`/ready`. When cluster observation is available, inspect the new Pod's image ID,
all three successful init statuses, and `backup-artifacts` logs. They identify
the exact verified generation; the receipt is also readable from that Pod's
`/tmp` volume. Readiness of the expected replacement Pod proves the backup and
migration gate succeeded in order. It does **not** prove a fresh live restore
drill or a later offsite Velero capture. The existing restore procedure remains
the verification for those claims.

Retries for the same Pod reuse and rehash its original completed generation;
they cannot silently replace a corrupted publication or accept another Pod's
backup. A new Pod UID captures a new dump. Ordinary application-container
restarts do not repeat the init sequence. The seven-generation retention limit
is shared with scheduled backups, so seven retained generations may cover less
than seven days after frequent rollouts. Failed Pod retries retain
`.rollout-source-<pod-uid>` on the dump PVC to preserve their original snapshot;
an operator may remove an abandoned source only after confirming that its Pod
has terminated and the snapshot is no longer required. Low storage or missing
artifacts fail closed and require repair; never skip the gate to force rollout.

Run `python3 scripts/tests/test-relay-rollout-backup.py` as the unprivileged CI
runner or developer. It uses only disposable Docker fixtures and the exact
digest-pinned PostgreSQL and Node images: read-only roots, dropped capabilities,
read-only backup-role enforcement, real dump/restore with migrator ownership,
plugin hashes, publication retry, corruption rejection and retention. The
always-on `manifests` workflow runs it before a deployment change can merge.

## Recovery

For a failed first rollout, restore the local service and keep the public route unmerged until fixed. For subsequent rollouts, preserve the previous release directory and change the Deployment subPath back only when its schema is compatible. Restore PostgreSQL and plugin artifacts from the same backup point when a schema rollback is required. Do not run two control planes against the same database.

After merge, a failed Relay rollout can hold the shared `apps` Kustomization NotReady (`wait: true`). Revert the Relay deployment/tunnel change through a reviewed PR and reconcile normally; never suspend shared Flux to hide the failure. The namespace/PVC prune protections preserve recovery data. If writes have reached the cluster, freeze them and capture the latest consistent dump and plugin artifacts before restoring the local control plane. The pre-cutover local database alone is not a current rollback after public writes.

## Prepared release

Source: `cchifor/relay-platform@ed1631f88a3501a65915ac7608dc55a25d5ceb21`

Archive: `relay-0.1.0-20261001.tar.gz`, attached with its checksum to the private [Relay v0.1.0 candidate release](https://git.chifor.me/cchifor/relay-platform/releases/tag/v0.1.0).

SHA-256: `35f9759c1dd9eb1c8787404008f9db4e0b30a9a21b33e9b2beb4f25d2326f56d`

Publication status must be verified from live cluster/DNS checks; preparing these manifests alone does not publish the hostname.

## Validation before cluster staging

- [Application CI](https://git.chifor.me/cchifor/relay-platform/actions/runs/56308) passed: type checks, formatting, production build, Rust tests, and integration/browser tests.
- AILab manifest lint passed: 33 Kustomization paths, 730 rendered resources, zero invalid resources or errors; 83 resources skipped by the repository's existing schema exclusions. Inline-hash and Flux Job annotation checks passed.
- The exact release archive passed a Docker smoke test with the pinned runtime images, PostgreSQL UID 70, Node UID 1000, read-only roots, dropped capabilities, and no privilege escalation. Migration, bootstrap, readiness, unauthenticated rejection, login, secure cookies, and all five builtin plugins succeeded.
- Cluster staging completed: all four PVCs bound, release SHA verified before extraction, migration completed, and Relay (including its backup holder)/PostgreSQL are Ready.
- Existing data matched after migration: zero agents/connectors, one conversation, two messages, five plugin records. The local service is stopped; private rollback snapshot is on dev-worker-2 at `.data/cutover/20261001T185511Z`.
- Live pre-exposure checks passed: health/readiness, SPA, public skill, unauthenticated 401, secure login cookies, all five plugins, browser WebSocket welcome, and a real `gpt-6-luna` Assistant response. A deployment-verification conversation was added after the preserved data was checked.
- A Job created from the final backup CronJob completed; its database and exact plugin files passed SHA verification and a full application restore drill. Nightly scheduling is enabled. The data PVC is mounted read-only in the artifact container; the PVC volume itself uses the normal writable CSI staging mode so an already-mounted iSCSI device can be shared on the same node.
- Public tunnel/HTTPS/browser/connector verification follows merge. The first Velero capture and deletion of the out-of-band deployer identity are operator follow-ups.

Backup publisher regression checks are reproducible with `RELAY_NODE_BIN=/path/to/node22 python3 scripts/tests/test-relay-backup.py`. They cover COPY escaping, both current/rollback artifact versions, independence from later source deletion, missing/corrupt source rejection, preservation of earlier complete generations, empty registries, and seven-generation retention.

## Connector downloads release 0.1.2

[Relay v0.1.2](https://git.chifor.me/cchifor/relay-platform/releases/tag/v0.1.2) adds public Linux x86-64 and ARM64 static connector binaries, a checksum-verifying per-user installer, and guided Install → Enroll → Share panes onboarding. Source commit: `41786bbb071ca3fd884d37dd5f8d135cc1d75c72`. Runtime archive: `relay-0.1.2-20261001.tar.gz`; SHA-256: `2c99aec856ac0b1a7643b991142482181bcc57a76f751addda8da8109e840f17`. The archive includes the exact connector assets published by the successful tag CI job.

Unauthenticated bootstrap routes are `/install.sh`, `/downloads/connector/manifest.json`, and `/downloads/connector/<version>/{relay-connector-linux-amd64,relay-connector-linux-arm64,SHA256SUMS,manifest.json,install.sh}`. They serve only packaged release files and remain independent of optional feature plugins. Missing artifacts return 404 rather than SPA HTML. Root metadata/installer use no-cache; versioned downloads are immutable. SHA-256 detects mismatch/corruption; HTTPS supplies origin trust, not an independent publisher signature. No forge credentials or Rust toolchain are needed on agent hosts.

CI verified 19 integration/browser checks, 2 Rust tests, actual x86-64 and emulated ARM64 enrollment/tmux output/control, and clean installation/reinstallation as UID 1000 on Ubuntu 22.04 and Alpine 3.21. CI databases now use isolated containers and dynamically assigned loopback ports. Public HTTPS acceptance runs after Flux rollout.

Release annotations and both application/migration subPaths advance together. Image references also restore the validated Node 22.23.0 and PostgreSQL 17.11 images, with explicit version/flavor tags plus digests. There are no schema, Secret, PVC, DNS, or tunnel changes. Roll back to `relay-0.1.0-20261001` with its original annotations if needed; schema compatibility is unchanged. Keep prior connector version directories inside subsequent archives when retaining pinned public download URLs. Stage new release directories before changing the Deployment; never replace a mounted directory in place.

### Runtime version guard

While this release was staged, automated digest-only PRs #1014 and #1015 resolved the tagless image references against latest PostgreSQL 18.6 and Node 26.10. PostgreSQL refused the existing major-17 data directory, leaving the migration container unable to reach the database. These manifests restore the last tested digests and add explicit `17.11-alpine` / `22.23.0-bookworm-slim` tags. Relay-scoped Renovate rules restrict updates to PostgreSQL 17.x and Node 22.x, grouping PostgreSQL server/dump-tool updates. Changing these major bounds requires runtime acceptance or a planned database upgrade/restore rehearsal. Do not reinitialize or delete the database PVC to resolve a version mismatch.

### Release storage capacity

The release PVC requests 2 GiB. After staging on 2026-10-01, `du -sk` reported 70,210 KiB for 0.1.0 and 79,519 KiB for 0.1.2: approximately 146 MiB total, or 7.2% of that budget. The NFS `df` result describes the shared export, so it is not used as the PVC budget. Before staging, compare existing `du -sk /releases/*` usage plus the new archive's extracted size against that budget. Retain the active release and the previous validated rollback release. Additional historical runtime directories can be removed by an operator only after their archive and checksum are verified in Gitea and no pod mounts them. Keep at most three connector versions in the next runtime archive; older pinned public URLs are retired explicitly. Long-term archive retention belongs in the release repository, rather than accumulating every runtime directory on the PVC.

## Worker approvals and Assistant parity: 0.2.0 CI image

Gitea CI in [cchifor/relay](https://git.chifor.me/cchifor/relay) publishes the
validated runtime as `registry.chifor.me/relay/control-plane`, then a reviewed
Flux change pins its digest in both the migration and application containers.
The image uses the existing Node 22.23.3 runtime digest, includes production
code/dependencies and public downloads, and needs no package installation or
code PVC mount at startup. Previous directories remain on `relay-releases`;
retain that PVC and the coordinated backup for recovery.

Application source is `e82a1222d4eab1d410ad67bcf017ada2f2856f12`, merged in PR #1.
CI verifies the candidate runtime archive SHA-256, then replaces its connector
files with the canonical artifacts from the successful v0.2.0 tag workflow.
The [release page](https://git.chifor.me/cchifor/relay/releases/tag/v0.2.0)
contains a `deployment-images-<run>.json` receipt with image digests, input
archive hashes, source commits and public connector hashes. The image digest
identifies the final payload; the older runtime archive alone does not contain
the canonical tag-job connector bytes.

Worker agents follow `/skills/platform-connect/SKILL.md`, install the connector,
and run its join flow. Pending hosts appear live in Agents → Connector hosts for
administrator authorization or blocking. Connector 0.2.0 waits for approval and
retains its private host identity; existing approved host credentials survive.
Legacy enrollment endpoints return 410. Version 0.1.2 downloads remain available.
The app opens Assistant, renames Overview to Status (`#/status`, with legacy
redirects), and moves Plugins into Settings.

Deploy [router API support, AILab #1032](https://git.chifor.me/cchifor/ailab/pulls/1032)
first and verify Relay's existing inference key can discover eligible tool
routes with `x-agent-id: relay-assistant`. No router administrator credential is
provided to Relay.

Migration 002 adds host access state, workspace revisions and persisted
Assistant turns/events/preferences/images. Its revoked-host update runs per
tenant under the non-bypass schema owner. Coordinated recovery generation
`20261002T161029Z-ed959ab0-da66-4ddc-bce3-f2337cf80b06` passed every checksum and
a complete restore/migration rehearsal as `relay_migrator`: 1 agent, 1 connector,
3 conversations, 6 messages and 5 plugins preserved, all 14 tenant tables with
forced RLS. Refresh the coordinated backup before rollout if data changed.

After the normal protected-main review and Flux rollout, verify migration init,
readiness, login, new SPA, public skill and canonical connector manifest, host
approval/blocking, and Assistant streaming. Recreate keeps one control plane.
Keep `relay-0.1.2-20261001` and its backup. Rolling only the old application back
does not restore legacy enrollment state; freeze writes and perform coordinated
database/plugin recovery when a complete rollback is needed. Existing Secrets,
data PVCs, PostgreSQL, backup scheduling, DNS and tunnel routes are unchanged.

Published by [Gitea CI run 57525](https://git.chifor.me/cchifor/relay/actions/runs/57525):
`registry.chifor.me/relay/control-plane@sha256:48b408402873b38c74665121e98739fccf7f6d6aa5e4443957fe051661ecad31`.


## Terminal geometry release 0.2.1

The tag CI builds and verifies the application and both portable connectors,
then publishes the application image and a `deployment-images-<run>.json`
receipt on the Relay release. Both application and migration containers use
that exact digest. Existing 0.1.2/0.2.0 connector downloads remain available.

Terminal rendering is isolated from legacy card CSS. A single terminal uses
one full-width grid column. Fit view changes the browser font size while
preserving the worker's grid; minimum-size overflow remains scrollable. Fit
pane uses measured cell dimensions and requires the existing control lease.
For a single-pane window it resizes the window; split windows use pane-local
resizing and report the actual accepted dimensions. The connector broadcasts
local tmux layout changes with a fresh snapshot; the browser applies geometry
and output in order, and subscriptions use the latest stored dimensions.

No schema, Secret, volume or router changes are required. Rollback pins the
previous verified Relay image in both containers through a reviewed PR. The
existing coordinated backups remain valid. Upgrade an existing worker using
the public installer and restart only its connector process, preserving its
state file and tmux session; app deployment does not replace worker processes.

Acceptance covers right-edge column rulers, Unicode, desktop/mobile rendering,
view-only zoom without worker resize, control leases, two viewers, external and
rapid local resizes, reconnect geometry, and keyboard focus after Fit pane.

Release CI: https://git.chifor.me/cchifor/relay/actions/runs/57676

Source: `0fb09ca14732e9addaeacda8e63c94bba46ef431`.

Image: `registry.chifor.me/relay/control-plane@sha256:ffd2fd664bc013ffe03a679cc5811c09011585ba03683f0dd3810c52a941a03c`.

Rollback image: `registry.chifor.me/relay/control-plane@sha256:48b408402873b38c74665121e98739fccf7f6d6aa5e4443957fe051661ecad31`.


## UI refinement release 0.2.2

Relay 0.2.2 refines typography, spacing, dashboard layout and mobile controls,
using self-hosted Inter. Existing Harness and Router colors and saved appearance
preferences are preserved. Clarity is an optional slate and soft blue palette
in Settings → Appearance, with light, dark and system modes.

Source: `28b204b87a4f75dc2085092a230bcd3cc2009dcb` (Relay PRs #4 and #5).
Release CI: https://git.chifor.me/cchifor/relay/actions/runs/58900

Image: `registry.chifor.me/relay/control-plane@sha256:d78f03bbb963ae1ba75425c00d0a81f6997cf4e49ea9ae82b9e7979b7cb707ec`.

Provenance: `deployment-images-58900.json` on the
[Relay v0.2.2 release](https://git.chifor.me/cchifor/relay/releases/tag/v0.2.2).

Rollback image: `registry.chifor.me/relay/control-plane@sha256:ffd2fd664bc013ffe03a679cc5811c09011585ba03683f0dd3810c52a941a03c`.

The release changes presentation and appearance selection, plus synchronized
release version metadata. Backend, terminal and connector behavior, database
schemas, Secrets, volumes and routes are unchanged. Existing workers need no
connector restart or upgrade. Both application and migration containers must
use the same receipt image digest. Rollback pins the verified 0.2.1 image in
both containers; no database rollback is required.

This image retains all three previously deployed connector versions (v0.1.2,
v0.2.0 and v0.2.1) alongside v0.2.2. This deliberately exceeds the old archive's
three-version retention target to keep every existing pinned download URL
working during the UI-only rollout. These files live in the immutable image,
not the release PVC; retiring old URLs requires a separate explicit change.

Validation before publication: typecheck, formatting, production build,
29 application/browser checks, two Rust tests, unchanged-palette regression
coverage, and 30 page/viewport visual checks. Tag CI additionally verifies
clean connector installation and the immutable image. After Flux rollout,
verify public health/readiness, exact UI/font assets, palette colors and saved
appearance, plus checksums for current and retained connector downloads.


## Browser tab icon release 0.2.3

Relay now declares a local SVG favicon using the same blue terminal symbol as
the navigation rail. It is served on the sign-in page and all app routes.

Source: `a389d2b031df1c8cc11b00539c6ce1c7bfd260db` (Relay PR #6).
Release CI: https://git.chifor.me/cchifor/relay/actions/runs/59190

Image: `registry.chifor.me/relay/control-plane@sha256:c82acd6168d96285dd565c59fefe584b7456c0d046880aa81d23fff3689433a9`.

Provenance: `deployment-images-59190.json` on the
[Relay v0.2.3 release](https://git.chifor.me/cchifor/relay/releases/tag/v0.2.3).

Rollback image: `registry.chifor.me/relay/control-plane@sha256:d78f03bbb963ae1ba75425c00d0a81f6997cf4e49ea9ae82b9e7979b7cb707ec`.

Both application and migration containers use the same verified digest.
This release adds only the favicon and release metadata; no schema, Secret,
volume, route or connector behavior changes are required. Existing workers
need no restart or upgrade. The image retains all existing pinned connector
downloads (v0.1.2, v0.2.0, v0.2.1 and v0.2.2) alongside v0.2.3.
Rollback restores the v0.2.2 digest in both containers through a reviewed PR.

Validation: build, typecheck, formatting, 29 application/browser checks and
clean connector installation passed in Gitea release CI. Chromium verified
the favicon declaration, SVG MIME type, exact bytes and image decoding.
After rollout, verify public health/readiness, `/favicon.svg` against the
source asset, the document favicon link, and retained connector manifests.


## Persistent host and terminal workspace release 0.3.0

Source: `dafe4de8ccbcdfa4f4a11fdc99972b9481c26ea3` ([Relay PR #8](https://git.chifor.me/cchifor/relay/pulls/8)).
Release CI: https://git.chifor.me/cchifor/relay/actions/runs/61038

Image: `registry.chifor.me/relay/control-plane@sha256:1ce1e70eadaccf0f07c92de408f623c2492cc507651004ed3dccf512bf3bf420`.

Provenance: `deployment-images-61038.json` on the
[Relay v0.3.0 release](https://git.chifor.me/cchifor/relay/releases/tag/v0.3.0).
Both application and migration containers use this exact digest. The image
retains all connector downloads from v0.1.2 through v0.2.3.

Agents and pending host approvals share one searchable table with host details,
label editing, overflow actions and a top-right connection skill link. Agent
rows open the focused terminal. Matrix remains available alongside pane and
full tmux views. Native tmux uses a private observer client and the existing
control lease. Image/file uploads are bounded, host-private and insert a quoted
path without submitting Enter. Activity gains filtering and event details;
Status remains the separate live-health view. Assistant, all existing themes
and Settings → Appearance are preserved. Workspace settings are removed.

The updated Connect skill installs a Linux systemd user service with verified
lingering, discovering recognized agents across current and future sessions.
Approval covers the account and configured tmux sockets; expanding the scope
requires fresh approval. Existing remote connector processes keep working and
are not replaced by application deployment. Upgrade a worker through the new
skill to enable persistent host connections and the new terminal/upload modes.

Migration 003 adds host approval and agent display metadata. A full baseline
restore/rehearsal preserved existing records, restricted migrator ownership
and all 14 forced-RLS tables. The rollout gate above captures and verifies a
fresh coordinated backup before this migration. Local validation passed 56
application/browser tests and seven Rust tests, real service restart checks,
ARM64 integration under QEMU and clean Ubuntu/Alpine installation; tag CI
repeated build, integration/browser and portable connector checks. The backup
gate passed 12 regression tests plus a real restricted dump/restore under the
exact pinned images.

After normal Flux reconciliation, verify the new manifest/source and UI assets,
health/readiness, isolated host approval, Matrix and focused/full tmux control,
exact uploaded bytes, service restart/reconnection, Activity and preserved
Appearance/Assistant pages. Remove only the acceptance host's service/socket
and block its generated host identity after verification.

Rollback image: `registry.chifor.me/relay/control-plane@sha256:c82acd6168d96285dd565c59fefe584b7456c0d046880aa81d23fff3689433a9`.
Restore that digest in both containers and the v0.2.3 source/release annotations
through a reviewed PR. The migration is additive; leave its columns in place
for an application rollback. Database restoration is reserved for a recovery
that actually requires it, using matching dump and plugin artifacts.
