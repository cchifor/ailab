# Relay

The initial operator handoff is archived in [the deployment record](../../plans/2026-10-01-relay-operator-handoff.md).

Relay follows the existing llm-router topology: Cloudflare proxied CNAME → the locally managed ailab tunnel → `relay.relay.svc.cluster.local:80`, with the app handling browser/connector authentication. The public origin is `https://relay.chifor.me`; it is intentionally free of interactive Cloudflare Access challenges so connector WebSockets can authenticate using their own credentials.

The authoritative manifests live in `cchifor/ailab`, directory `kubernetes/apps/apps/relay`. The tunnel route is in `apps/edge/cloudflared.yaml`; DNS is declared in `kubernetes/infra/cloudflare/variables.tf`. Both require the normal protected-main GitOps review flow. Do not patch the shared tunnel or suspend Flux to bypass that flow.

### Temporary R0 observation (remove by 2026-10-17)

The purpose-scoped `relay-r0-observer-dw4` Role and RoleBinding in
`kubernetes/apps/infrastructure/platform-access/rbac.yaml` let only `platform-dw4`
inspect Relay workload/init-container status, backup Job status, release annotations
and volume metadata using `platform kubectl -n relay ...`. They grant no ConfigMap,
pod log, Secret, exec/attach, port-forward, credential minting or Kubernetes write access. Use them
to compare live release identity and backup-gate outcomes with the control-plane
plan; successful Job status alone does not prove a restore or artifact inventory.
Any repair or restore still needs its normal reviewed GitOps change or authorized
operator route. Remove both resources after R0 observation, no later than
2026-10-17, tracked by [issue #1216](https://git.chifor.me/cchifor/ailab/issues/1216).
Their annotations record the deadline and issue; Kubernetes does not enforce it.
Inspecting a specific backup failure's log requires a separate reviewed grant
after the relevant Pod identity and log source have been checked.

The dedicated platform-access Flux Kustomization intentionally has no dependsOn
on the apps/platform/vault stacks: their outages must not freeze observer RBAC
changes. It retries missing namespaces every minute during a rebuild, as it
already does for the platform namespaces. Relay's Namespace is protected from
Flux pruning. This temporary grant follows that existing policy; it does not
guarantee first-reconcile success on an empty cluster. Its Kustomize output keeps
the `relay` namespace unchanged (there is no top-level namespace transformer).

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
8. DNS was created by the operator: proxied `relay.chifor.me` CNAME → `f93d9a6a-5172-43d3-8bef-13460ea7607b.cfargotunnel.com`, record ID `d01f099abc5e5a85c6c5bef5791c8e41`. `relay-import.tf` adopted it into the existing operator state (with P4 of ADR 0040 on 2026-10-27, as `cloudflare_dns_record.edge["relay"]`; the import block is gone since). Do not apply an empty Terraform state against the estate. Run and restore a logical backup successfully, then unsuspend the nightly CronJob before exposing the app.
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

### Release asset retention (Gitea attachments)

Owner-approved 2026-11-23. Every Relay release uploads about 306 MB of assets to Gitea's `gitea-shared-storage`
volume. Most of it is three per-commit bundles, `relay-codex-*` (three ~84 MB parts), `relay-pi-*` (~75 MB) and
`relay-holder-*` (~13 MB), rebuilt and re-uploaded even by hub-only releases. Releasing every console feature as it
lands added about 2 GB in one night and took the volume to 90.5% (expanded to 50 Gi in ailab #1953).

**The rule.** Only those three bundle families are ever deleted. Every release object, tag and other asset
(manifests, `SHA256SUMS`, connector binaries, skill files, receipts) stays. A release keeps its bundles when it is
one of the newest five, or its version is named anywhere in this repo's Relay pins
(`scripts/relay_release_retention.py`, `PIN_PATHS`), or it is passed with `--keep`. `v0.2.0` is never touched:
llm-router's `deploy/publish.py` uses it as its artifact store for router image tarballs and their deployment
receipts, which the llm-router runbooks link. A deleted bundle can be rebuilt from its tag by re-running that tag's
release workflow.

**Run it.** From a checkout of main, in Git Bash:

```sh
scripts/relay-release-retention.sh                 # dry run: prints KEEP, DELETE per release, TOTAL and PLAN-SHA256
scripts/relay-release-retention.sh --execute <sha> # deletes exactly that plan, then re-reads each release
```

`--execute` deletes only when the freshly computed plan has exactly the sha256 the owner approved. A new release
shifts the newest five and so changes the sha; re-run the dry run and get the new list approved. The script reads
and deletes through `scripts/gitea-api.sh` (workstation-bot). Deleted files free ext4 space inside the volume; the
QNAP LUN does not shrink (no UNMAP), which is fine.

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

## Managed router access — pending public approval integration

Relay requests keys through [llm-router's public agent contract](https://router.chifor.me/agent/skill.md):
request, router-administrator approval, and one-time redeem into the credential
store. Rotation requires a new approval; revocation occurs in Router Settings.
Relay shows a persistent pending notification with the human code and approval
link. Poll tokens and issued keys must never appear in browser responses, logs,
plaintext database columns, or this runbook.

The dedicated trust bridge is deferred. Its implementation and deployment
material are preserved on the `todo/relay-router-trust-bridge` branches in
[Relay](https://git.chifor.me/cchifor/relay/src/branch/todo/relay-router-trust-bridge)
and [AILab](https://git.chifor.me/cchifor/ailab/src/branch/todo/relay-router-trust-bridge).
These branches are TODO references, not activation candidates. The old
`relay-router-control-plane` and `llm-router-relay-keys` components have been
removed from the deployment tree. Do not provision signing keys, a router trust
Secret, or a Relay trust mapping. This retirement changes no live llm-router
image, configuration, or deployment.

`RELAY_AGENT_CONTROL_PLANE`, qualified profiles, and pools stay disabled until
the public approval integration and its constrained acceptance are reviewed.
The nonsecret record remains at `relay-v060-activation-evidence.yaml` for
continuity; it now describes the public contract rather than the retired bridge.
The activation gate prevents enabling the retired components or the managed flag
while that record is pending. Default communication may be deployed independently
using `RELAY_AGENT_COMMUNICATION`; it does not authorize managed activation.

### Required integration and acceptance evidence

- A reviewed Relay release persists pending request state and notifications,
  enforces at most one pending request per profile, and handles denial, expiry,
  delivery loss, revocation, rate limits, and restart recovery.
- Router approval determines routes and lifetime. Keys are redeemed exactly
  once into a confirmed secret-store write. No Relay-specific router changes
  are required.
- OpenBao escrow and worker delivery remain separate requirements. Use the
  provisioned `af` mount and `relay/router` prefix. A private origin or estate CA
  needs a reviewed narrow egress/CA change before activation.
- One named acceptance worker has the matching connector, holder, renderer,
  private-file permissions, and approved profile. Record exact source, image,
  artifact, and deployment identities with a rollback target.
- Record request → approve → redeem → route → rotate → revoke against the
  supported runtime, plus loss/restart handling and secret confinement. Codex
  live acceptance awaits provider capacity; fixture tests are not live evidence.

Ship activation only through a reviewed AILab PR and Flux, pinning the same
immutable image in Relay and its migration container. Remove any obsolete
`admin-key.pem` and `reconciler-key.pem` inputs through the normal secret-management
route if a previously staged connection Secret contains them; this repository
contains no such live Secret and this change does not mutate one.

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


## Workspace UI release 0.3.1

Source: `1a94684350342a41c5a95c8e742a86a49261af3e`, [Relay PR #9](https://git.chifor.me/cchifor/relay/pulls/9).
Image: `registry.chifor.me/relay/control-plane@sha256:d089449d0185dfa86b751f21017d6810f3d16afabb978689f78a0c942ab1c2df`.
Provenance: [deployment-images-63408.json](https://git.chifor.me/cchifor/relay/releases/download/v0.3.1/deployment-images-63408.json),
produced by [release CI 63408](https://git.chifor.me/cchifor/relay/actions/runs/63408).
Both the application and migration containers use this digest. The image keeps
all previously served pinned connector downloads, including v0.3.0.

This release gives every workspace page browser scrolling and a shared Back
button with linked breadcrumbs. Status cards open agent terminals directly;
the old agent detail modal and Latest Activity panel are removed. Matrix,
focused pane and native tmux views have compact terminal headers. Agents and
Activity use search and removable filter chips while preserving their detailed
rows and actions; Agents sort through column headers. Scrollbar thumbs hide
while idle. Browser history restores filters and native tmux selections.

There are no new database migrations or connector behavior changes. Existing
host processes require no upgrade. The existing Recreate and coordinated
backup gate still run before migration and application startup; no Secret,
RBAC, tunnel, PVC or backup policy changes are part of this release.

Local validation passed typecheck, formatting, build and seven suites (55 tests
passed; optional user-service and ARM64 checks skipped locally). Real API,
PostgreSQL, Rust connector and tmux Playwright coverage includes breadcrumbs,
Back, matrix/focused/native views, control, host review, table filters/sorting,
Activity pagination and live snapshots, and document scrolling at 390, 768
and 1440 pixels. Tag CI additionally builds and checks both portable connector
architectures and clean installation.

After Flux reconciliation, verify health/readiness together with the v0.3.1
public manifest, expected source, new UI asset hashes, and retained downloads.
Run authenticated Playwright checks for shared breadcrumbs and Back, Status
terminal links, matrix/focused views, table filters/sorting, Activity details,
and responsive scrolling. Readiness of the expected release demonstrates the
ordered backup/migration gate completed; detailed generation logs, a fresh
live restore drill and offsite backup capture need separate observation.

Rollback restores `registry.chifor.me/relay/control-plane@sha256:1ce1e70eadaccf0f07c92de408f623c2492cc507651004ed3dccf512bf3bf420`
in both containers, `relay-0.3.0-ci-20261003` and source
`dafe4de8ccbcdfa4f4a11fdc99972b9481c26ea3` through a reviewed GitOps PR.
No database restore or connector restart is required to roll back this UI release.


## Agent deletion and sorting release 0.3.3

Source: `05a07dc4d373f028388309adc7b635af4a7d4d8d` ([Relay PR #10](https://git.chifor.me/cchifor/relay/pulls/10)).
Image: `registry.chifor.me/relay/control-plane@sha256:5ad2ab3aada9784875c652fda59fcfdb89994bf14ad1a0b987b8a8804b50a3f3` in both the application and migration containers.
Provenance: [deployment-images-63803.json](https://git.chifor.me/cchifor/relay/releases/download/v0.3.3/deployment-images-63803.json),
from successful [release CI 63803](https://git.chifor.me/cchifor/relay/actions/runs/63803).
All previous pinned connector downloads, including v0.3.1, are retained.

Administrators can confirm **Agents → ⋯ → Delete agent…** to remove the Relay
record and saved-layout references. Viewers and control leases are closed;
the host, tmux process, other agents, and historical Activity events remain.
Migration 004 adds the tenant-isolated `agent_tombstones` table with forced RLS
and grants the existing runtime role access. Deleted pane identities are
suppressed during both automatic discovery and explicit registration; new panes
can register normally. The existing coordinated backup gate runs before the
additive migration. Connector behavior is unchanged and hosts need no upgrade.

Column headers now cycle ascending, descending, then unsorted. The third click
restores default attention-first ordering, removes explicit sort parameters,
and displays the two-arrow icon. Menus fit short/mobile viewports.

Validation includes permissions and tenant boundaries, lease/layout cleanup,
retained audit history, suppression across app/host restarts, stale broker-work
fencing, and migration under a restricted non-bypass owner. Real API,
PostgreSQL, Rust connector and tmux Playwright coverage verifies deletion,
cancellation, viewer restrictions, another browser's immediate update,
desktop/mobile layouts, preserved worker processes and all five sort cycles.
Production acceptance opens and cancels Delete on existing records; it must
not delete users' agents merely to verify the UI. Check exact release/assets,
health/readiness, retained downloads and browser errors after reconciliation.

Rollback pins `registry.chifor.me/relay/control-plane@sha256:d089449d0185dfa86b751f21017d6810f3d16afabb978689f78a0c942ab1c2df`
in both containers and restores `relay-0.3.1-ci-20261004` with source
`1a94684350342a41c5a95c8e742a86a49261af3e` through a reviewed PR. Keep migration
004's table in place; do not restore an older database just to roll back the
application. Older code does not honor deletion suppression, so a previously
deleted pane may be rediscovered while running the old release.


## Host deletion release 0.3.4

Source: `2e02ba4c281de754065a77ac221b3352474b2536` ([Relay PR #11](https://git.chifor.me/cchifor/relay/pulls/11)).
Image: `registry.chifor.me/relay/control-plane@sha256:6469aa151ecc13ec7f3fedaeb8de9c103fd41f9ad37f3878fad6a81a51dacb23` in both the application and migration containers.
Provenance: [deployment-images-64003.json](https://git.chifor.me/cchifor/relay/releases/download/v0.3.4/deployment-images-64003.json),
from successful [release CI 64003](https://git.chifor.me/cchifor/relay/actions/runs/64003).
All previous published connector downloads through v0.3.3 are retained.

Administrators can confirm **Agents → ⋯ → Delete host…** on a host or one of
its agent rows. Deletion removes the registration, associated agents, leases
and saved-layout entries, closes viewers and revokes connector access.
Worker processes and tmux sessions keep running; historical Activity remains
filterable by the removed host. Empty approved/blocked hosts are labeled
**Registered host** instead of **Host access request**.

Migration 005 adds `connector_tombstones` with forced tenant RLS and grants
the existing runtime role access. It stores only deleted installation IDs,
credential hashes and deletion times. Enrollment retries serialize with host
deletion and reject either a retired ID or credential. To reconnect intentionally,
use a fresh connector `--state` path and request new administrator approval.
Hosts require no connector upgrade. The existing coordinated database/artifact
backup and verification gate runs before this additive migration.

Validation covers roles and tenant boundaries, reviewed revisions, all host
states, lease/layout cleanup, retained audit history, concurrent retries,
restart suppression and restricted-role migration. Real API/PostgreSQL/Rust/tmux
Playwright tests delete empty and live hosts on desktop/mobile, verify
cancellation and viewer restrictions, immediate cross-browser removal,
unchanged tmux process IDs, and a rejected access request from the restarted
connector. Production acceptance cancels existing-host confirmations and checks
health/readiness, exact release/UI/download hashes, labels and sort cycles.
It must not delete user hosts merely to validate the release.

Rollback pins `registry.chifor.me/relay/control-plane@sha256:5ad2ab3aada9784875c652fda59fcfdb89994bf14ad1a0b987b8a8804b50a3f3`
in both containers and restores `relay-0.3.3-ci-20261004` with source
`05a07dc4d373f028388309adc7b635af4a7d4d8d` through a reviewed PR. Leave migration
005 in place; do not restore an old database merely to roll back code. Older
code ignores connector tombstones, so a deleted installation could reappear
as a pending request during rollback; approval is still required.


## Account menu release 0.3.5

Source: `a78d6f33dcc14c7ecd34b12b1adca23975dea874` ([Relay PR #12](https://git.chifor.me/cchifor/relay/pulls/12)).
Image: `registry.chifor.me/relay/control-plane@sha256:25640aafcdf4484ebe87c47ebfa9be1a3dc69233dca6b9b6a0df5a97416cb82d` in both the application and migration containers.
Provenance: [deployment-images-69146.json](https://git.chifor.me/cchifor/relay/releases/download/v0.3.5/deployment-images-69146.json),
from successful [release CI 69146](https://git.chifor.me/cchifor/relay/actions/runs/69146).
Previous published connector downloads through v0.3.4 are retained.

The sidebar account button opens Settings, Account and Sign out. Outside clicks
and Escape dismiss the menu; Escape restores trigger focus. Keyboard navigation,
collapsed rails, mobile same-route navigation, theme palettes and long names
are supported. The popup renders outside the scrolling rail to avoid clipping.
Logout prevents duplicate requests, preserves error reporting and permits retry.

There are no schema or connector behavior changes. Existing workers require no
upgrade. Preserve the existing single-replica Recreate strategy and coordinated
backup/migration startup gate. No new secrets, routes, storage or RBAC are needed.

Local verification includes typechecking, production build, repository formatting
and five real-session browser tests, covering failed and successful logout and
existing appearance controls. Source and tag CI run the full application and
connector verification. Production acceptance checks health/readiness, the exact
source manifest and image UI asset hashes, desktop/mobile navigation, outside
click and Escape dismissal, and logout of its own newly created test session.
It must not change existing hosts, agents, conversations or settings.

Rollback restores `registry.chifor.me/relay/control-plane@sha256:6469aa151ecc13ec7f3fedaeb8de9c103fd41f9ad37f3878fad6a81a51dacb23`
in both containers, release `relay-0.3.4-ci-20261004` and source
`2e02ba4c281de754065a77ac221b3352474b2536` through a reviewed GitOps change.
No database restore or worker downgrade is needed for this UI-only rollback.


## Control-plane and authentication release 0.5.2 — stage 1, local sign-in

Source: `39767c6329524a36fce9875a14f37a0eef942c32` (Relay main after [PR #140](https://git.chifor.me/cchifor/relay/pulls/140); tag `v0.5.2`, release 2124).
Image: `registry.chifor.me/relay/control-plane@sha256:3571fb473a1657c6ee85405e08b0505204139449be9dd91da7b6fc717119b6cc` in both the application and migration containers.
Provenance: `deployment-images-84067.json` on the [v0.5.2 release](https://git.chifor.me/cchifor/relay/releases/tag/v0.5.2),
from the successful [release CI 84067](https://git.chifor.me/cchifor/relay/actions/runs/84067). The
registry index for that digest was re-read and hashes to the same value.

This is the first deployment since 0.3.5. It carries the v0.4.0 control-plane milestones and the
v0.5.x OIDC relying-party code, and it applies migrations `006`–`049` (forward-only, none drops a
table or column). Every new control-plane feature stays disabled by default: no pool, profile,
approval file, notification, monitoring or Gitea-issuer input is set here.

**Authentication is unchanged in this stage.** `RELAY_AUTH_MODE=local` is pinned on the `relay`
container rather than left to the release default, so Relay keeps the administrator-token sign-in.
Stage 2 flips that one value to `oidc` against the ailab Keycloak (`id.chifor.me`, realm `relay`,
ADR 0038) in a separate reviewed change. That change's rollback is this state.

**No old pod runs against the new schema.** The Deployment strategy is `Recreate`: the 0.3.5 pod
is terminated before the new pod's init chain starts. The `backup-database` and `backup-artifacts`
init containers must publish `/tmp/relay-pre-migration-backup.json`, or `migrate` does not start.
Then `migrate` applies 006–049 and re-applies every `relay_app` grant, and only then does the
0.5.2 `relay` container start.

**Evidence to record for this rollout**, nonsecret, with UTC times:

- the manual pre-merge backup run (Job created from `relay-postgres-dump`) and its published
  generation;
- the pre-migration generation named in the `backup-database` log of the **first** init attempt
  (see the rollback note on retries), plus any later attempts' generations;
- the `migrate` and `bootstrap` log lines and exit 0, and `SELECT max(version) FROM
  schema_migrations` = 49;
- Relay 1/1 Ready on the new digest;
- `/health` and `/ready` returning 200, and `/api/auth/config` reporting local;
- one administrator-token sign-in returning 200.

### Restore rehearsal (2026-10-13, before the merge)

Generation `20261013T073237Z-8a79e0e2-34ca-4619-90f4-9840ab4a4638` was produced by a manual Job from
`relay-postgres-dump`.

- **Checks.** Every `SHA256SUMS` entry was OK. The copied `relay.dump` re-hashed to the same sha256
  (`535c5695…`).
- **Restore.** It was restored into a **disposable** database on `relay-postgres`, as
  `relay_migrator` (`pg_restore -U postgres --role=relay_migrator --no-owner --no-acl
  --exit-on-error`), after replaying the bootstrap's per-database grants. The restore took about
  2 s.
- **Result.** 22/22 tables, 167/167 rows, every table owned by `relay_migrator`, and 16/16 tables
  with forced RLS. The disposable database was dropped.
- **Finding.** After `--no-acl`, `relay_app` had **no** privilege on its 16 tables. Its grants come
  from `migrate` (both 0.3.5's and 0.5.2's `scripts/migrate.ts` re-grant `relay_app` on every run),
  not from the bootstrap's default privileges. A restore is therefore only usable **after** the
  release's `migrate` has run, which is exactly what the init chain does.

This rehearsed the database restore, not the full rollback below.

### Rollback to 0.3.5

Auth needs no rollback in this stage. Running the 0.3.5 image against the migrated schema-49
database is **untested**. Every Relay DSN selects its database by the name `relay`, so a rollback
swaps that database and needs no Secret change. Do not start 0.3.5 against schema 49.

1. **Freeze.** Run `flux suspend kustomization apps -n flux-system`, then
   `kubectl --context admin@ai -n relay scale deploy/relay --replicas=0`, and wait until no `relay`
   pod remains.
2. **Swap the database.** As `postgres`, by local socket inside `relay-postgres-0`:
   1. `ALTER DATABASE relay RENAME TO relay_failed_v052;`
   2. `CREATE DATABASE relay OWNER relay_migrator;`
   3. In the new `relay` database, replay the per-database statements from
      `postgres-bootstrap.yaml`: the `REVOKE CREATE ON SCHEMA public`, the `GRANT USAGE`, and the
      four `ALTER DEFAULT PRIVILEGES`.
3. **Restore.** Pick the generation first:
   - **Use the EARLIEST generation of this rollout**, the one named in the `backup-database` log of
     the first init attempt. `migrate` commits each migration file in its own transaction. If it
     fails partway, the init chain retries, and every retry's `backup-database` publishes a new
     generation of an already **partially migrated** database. Never restore the latest
     generation by default.
   - If the first attempt's generation is unknown, use the manual pre-merge generation
     `20261013T073237Z-8a79e0e2-34ca-4619-90f4-9840ab4a4638`. It is known pure 0.3.5 (schema 5),
     but it loses writes made between 07:32Z and the rollout.
   - Either way, confirm on the restored database that `max(version) FROM schema_migrations` = 5.

   Copy that generation's `relay.dump` into the pod and check it against the generation's
   `SHA256SUMS`. Then run `pg_restore -U postgres --role=relay_migrator --no-owner --no-acl
   --exit-on-error -d relay`.
4. **Verify the restored database before anything starts**:
   - table and row counts match the generation;
   - every table is owned by `relay_migrator`;
   - 16 tables have forced RLS.

   `relay_app` grants are expected to be missing at this point.
5. **Restore artifacts if needed.** If plugin artifacts changed during the failed release, restore
   the generation's `plugins/` tree to `relay-data`.
6. **Pin 0.3.5.** In a reviewed PR, pin
   `registry.chifor.me/relay/control-plane@sha256:25640aafcdf4484ebe87c47ebfa9be1a3dc69233dca6b9b6a0df5a97416cb82d`,
   release `relay-0.3.5-ci-20261006` and source `a78d6f33dcc14c7ecd34b12b1adca23975dea874`, and
   remove the `RELAY_AUTH_MODE` line. Merge it, then run `flux resume kustomization apps -n
   flux-system`.
7. **Check the first start.** 0.3.5's `migrate` finds its schema current and re-grants `relay_app`.
   Check `/ready`, an administrator-token sign-in, and the counts.
8. **Clean up.** Drop `relay_failed_v052` only after acceptance.

## Authentication release 0.5.2 — stage 2, sign-in through the ailab Keycloak

Same image as stage 1 (`sha256:3571fb47…`). This stage only switches the browser sign-in:

- `RELAY_AUTH_MODE=oidc`;
- `OIDC_ISSUER=https://id.chifor.me/realms/relay`, `OIDC_CLIENT_ID=relay`;
- `OIDC_PROVIDER_NAME=Relay ID`, `OIDC_REQUIRE_EMAIL_VERIFIED=1`;
- `OIDC_CLIENT_SECRET` in `relay-secrets`, which must equal Keycloak's `relay` client secret
  (compare sha256 prefixes, never values).

The identity provider is ADR 0038, with its runbook in `docs/runbooks/keycloak.md`. There is no
`OIDC_IDP_HINT` until the Google broker exists.

**Merge only after** Keycloak is Ready, the realm import is logged, discovery returns exactly
`https://id.chifor.me/realms/relay`, the client checks pass, and the client secret's hash equals
this Secret's (keycloak runbook, "First deployment").

**Who gets in.** Signing up at `id.chifor.me` grants nothing. A person reaches Relay only after an
operator has done both of these (keycloak runbook, "Members"):

- confirmed them out of band and set `emailVerified=true` on their exact Keycloak user ID;
- bound `https://id.chifor.me/realms/relay|<that id>` with `node dist/scripts/member.js`.

`POST /api/auth/login` (the administrator token) answers 404 while this mode is on. A Relay session
lasts up to 8 h, and disabling the Keycloak user does not end it. `member.js` only upserts a role,
and revokes that user's sessions whenever it runs. To remove someone, disable their Keycloak user,
then delete their Relay membership and sessions as `postgres` inside `relay-postgres-0`
(`DELETE FROM sessions …; DELETE FROM memberships …` for that `user_id` and tenant).

**Acceptance**, recorded with UTC times and nonsecret evidence:

- `/api/auth/config` reports OIDC.
- The first member signs in. The `users.subject` row equals the recorded `issuer|id` exactly, and
  the ID token carries `email_verified=true`.
- A throwaway sign-up is denied with no session.
- The same throwaway, bound while `emailVerified=false`, is rejected.
- Logout removes the Relay session.
- A tampered `state` creates no session.
- The admin-token login returns 404.

Afterwards, remove the throwaway's Relay membership and delete its Keycloak user.

**Rollback**: one reviewed change setting `RELAY_AUTH_MODE` back to `local` (stage 1; its
admin-token sign-in was verified). Then revoke the sessions OIDC minted. They stay valid for up to
8 h, and the two modes must not overlap. As `postgres` inside `relay-postgres-0`, against `relay`:

```sql
DELETE FROM sessions s USING users u
 WHERE s.user_id = u.id AND u.subject LIKE 'https://id.chifor.me/realms/relay|%';
```

The `OIDC_*` values may stay. Memberships keyed on the Keycloak issuer simply stop matching
anything.

**Callback URL**: Relay builds `redirect_uri` from `RELAY_URL` (`https://relay.chifor.me` +
`/api/auth/callback`), not from request or proxy headers. In OIDC mode Relay refuses to start
unless `RELAY_URL` is an exact HTTPS origin. Keycloak's `relay` client allows exactly that one
redirect URI, so changing the public origin means changing both.

## Operator automation identity (just-in-time admin, 2026-11-16)

Relay's admin API (`authorize(req, "admin")`) accepts only a `relay_session` from the OIDC login
(`RELAY_AUTH_MODE=oidc`; Bearer tokens are connector-only and `POST /api/auth/login` answers 404).
Instead of a person handing the operator a browser cookie, the operator uses a dedicated Keycloak
account with **no Relay membership at rest**:

| What | Where |
| --- | --- |
| Keycloak user | `relay-operator@ailab.invalid`, id `72e99c0a-fce0-42f1-8e2a-af1363b5aa4f`, realm `relay` |
| Relay subject | `https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f`, user `a2d7daaa-27e1-422b-9909-d8ba5ae9c9c8`, display name `relay-operator (automation)` |
| Password | OpenBao `af/relay/operator-oidc` (`username`, `password`, `subject`, `keycloak_user_id`), read with the breakglass token over stdin |
| Tools | `scripts/relay-operator.py` (host: kubectl + docker), `scripts/relay-admin-call.py` (runs in `python:3.13-bookworm@sha256:073ffebb…`, stdlib only) |

`python scripts/relay-operator.py call OP [ARGS]` does, in order:

1. `member.js --role admin` for that subject (cluster admin is the real gate, as for every operator
   step);
2. in the container: Relay `/api/auth/oidc` -> the Keycloak login form -> `/api/auth/callback`. Every
   redirect is followed by hand and its `Location` checked (`auth_error=…` fails closed), with an
   explicit User-Agent (Cloudflare refuses Python's default). The session cookie stays in memory;
   `/api/auth/me` must say `relay-operator (automation)` and `admin`;
3. ONE call from the allowlist: `me`, `list-installations`, `get-installation ID`,
   `enrollment-token ID [--ttl S]`. There is no approve, no grant, no policy and no key reveal;
4. `POST /api/auth/logout` with `Origin` (without it the 403 would leave the session alive for 8 h),
   then `/api/auth/me` must answer 401;
5. in a `finally` that already covers the grant (`member.js` can commit and still report a transport
   error): delete the identity's memberships and sessions (the subject travels as a psql variable, never
   interpolated into the SQL), read back `0 memberships, 0 sessions`, and end its Keycloak SSO sessions.
   A failure there is printed loudly and becomes exit code 3 (revoke not confirmed) or 5 (Keycloak
   logout). A Relay logout that did not close the session exits 4.

**Never let it approve anything durable.** Relay re-checks the approver's CURRENT admin membership at
every use of a worker grant, creation policy, management authority, delegation or repository grant
(`workers/grants/authority.ts`, `management/authority.ts`, `delegation/service.ts`, …). Approved by this
identity, they would fail `approver_invalid` the moment its membership is removed. Installation
approval (I3) stays a human admin's step too.

**Enrollment tokens.** The reissue route answers 200 (approve answers 201). The token is in the first
response only. A replay of the same operationId returns the stored receipt with a null token, so the
tool sends a new operationId every time and writes ONLY the raw token to stdout, for piping straight
into the Secret (a redacted receipt goes to stderr):

```bash
python scripts/relay-operator.py call enrollment-token <installation-id> --ttl 900 \
  | kubectl --context admin@ai -n relay-infra-control create secret generic relay-infrastructure-agent-enrollment --from-file=token=/dev/stdin
```

(On Windows, `kubectl.exe` has no `/dev/stdin`. Build the Secret JSON in memory and pipe it to
`kubectl create -f -` instead.)

**Audit.** Keycloak records `LOGIN`, `CODE_TO_TOKEN` and errors for the account (realm events,
90 days: `kc get events -r relay -q user=72e99c0a-fce0-42f1-8e2a-af1363b5aa4f`). Relay's logout does not
end the Keycloak SSO session, so the cleanup ends it with an admin logout of the user
(`users/<id>/logout`); that is an admin action, not a user `LOGOUT` event. Relay's own events name
user `a2d7daaa-…` (for example `enrollment-reissued`).

**Operations.**
- `python scripts/relay-operator.py status`: Relay membership/session counts (expect 0/0) and the
  Keycloak user state.
- `revoke`: delete its membership and sessions and end its Keycloak SSO sessions again (idempotent).
  Use it after any interrupted `call`, or whenever `call` exits 3 or 5.
- `provision --rotate`: a new random password, set in Keycloak and OpenBao, verified by a sha256 prefix.
  Never retry a failed login: the realm has brute-force protection, so repeated failures lock the account.
- **Retire it:** `revoke`, then `kc update users/72e99c0a-… -r relay -s enabled=false` (or delete it),
  then `bao kv metadata delete -mount=af relay/operator-oidc`.

### `session`: one bounded authenticated run (automated Pi canary phase A, 2026-11-27)

`call` logs in and out per call, so it cannot hold a Relay control lease, which binds `actor.userId` AND the
login session. The automated phase A (the inference session's `owner-phase-a-automation.cjs`) needs ONE session for
its whole run. `session` provides exactly that, and nothing general-purpose
(plan: `plans/2026-11-27-relay-operator-session-plan.md`):

```bash
python scripts/relay-operator.py session --manifest-ref <FULL_COMMIT>:<path/to/manifest.json> \
  --bundle <dir> --runner-minutes <1-45> --evidence-dir <NEW dir>
```

- **The bundle.** The manifest (`{version: 1, image: name@sha256:…, entrypoint: [...], files: {relpath: sha256}}`)
  is read from a commit on `gitea/main`, so only reviewed code runs. The bundle is copied into a private directory,
  refusing symlinks, special files and files outside the manifest, and the COPIED bytes are hashed.
- **The proxy** (`scripts/relay-session-proxy.py`, stdlib, pinned python image, hardened container) logs in once,
  keeps the cookie in memory and serves a fixed allowlist on a Unix socket. Scope is pinned in its source: the two
  canary bindings, profiles `1d9a7d6e` / `f25c187b` at revision 1 and their workspaces, the read-only predecessor
  session and permission. Each request is rebuilt from validated fields (Relay v0.14.1 schemas); client headers are
  never forwarded.
  - Allowed: binding rotation (at most once per binding), launch, control lease, `prompt` / `set-config`
    (`thought_level` low or high) / `cancel` / `close` commands, permission decisions (Relay's `outcome` union, once
    per permission), native recovery and resume (never with `acknowledgeUnknownToolEffects`), and the reads.
  - Not allowed: binding revoke, profile or grant approvals, `set-mode`, `native-command`, question replies.
  - Dynamic IDs are learned only from this run's own responses. A launch must be this run's operation, by this
    actor, created after login, matching the dispatched intent (a resume also keeps the source's profile, revision
    and cwd). A lost POST response is reconciled read-only through the inventory, never resent. Operation IDs that
    existed at login are refused, and once-only routes reserve the operation and the binding or permission
    atomically.
  - **Two sockets.** `proxy.sock` serves every allowed call except the control lease. `control.sock` serves ONLY
    control-lease acquire, renew and release, and each socket refuses the other's routes. Each has its own reader
    pool (24 and 4) with an absolute 5 s request deadline, then its own handler slots (6 and 2), so nothing on the
    general socket can starve renewal.
  - Bounds:
    - one deadline thread for every request and upstream call;
    - a listen backlog of 64;
    - DNS resolved off the request path;
    - connect, TLS, send and the whole response under one absolute deadline, capped by the phase's own deadline,
      with nothing sent once it has expired;
    - prompts of at most 16 KiB of UTF-8 (the server's own byte limit).

    The evidence logs fixed reason codes and route shapes from a fixed vocabulary only, never a raw target, query
    value, client key or client-chosen path word.
- **The Runner** runs with `--network none` and is hardened. It shares only the proxy's two sockets
  (`RELAY_SESSION_SOCKET` and `RELAY_SESSION_CONTROL_SOCKET`). Its durable store is a named volume
  `relay-state-<run>` (`AUTOMATION_DIR`), kept after the run and exported to `state.tar`.
- **Deadlines.** `RUNNER_DEADLINE`, then `CLEANUP_DEADLINE` (10 min later).
  - After the runner deadline the Runner gets SIGTERM. The proxy then passes only cleanup-shaped calls: reads,
    renewal of an existing lease and its release, `cancel` / `close`, and cancelled permission decisions.
  - At the cleanup deadline the proxy refuses everything, logs out (`/api/auth/me` must answer 401) and exits.
  - A dispatched mutation that was interrupted is logged `uncertain`, with its operationId.
- **The Lease** `relay-operator-jit` (namespace `relay`) serializes `session`, `call` and the reaper.
  - It is taken by create, or by a `resourceVersion`-conditioned replace when expired. Orphan authority is cleared
    before every grant.
  - Destructive cleanup (the revoke, the account-wide Keycloak logout, the release) runs ONLY after the run
    positively re-confirms ownership: a `resourceVersion`-conditioned extension of its own Lease, or a conditional
    takeover of an absent or expired one. If a successor holds a live Lease, or ownership cannot be established (an
    API error), the run does NOT revoke and exits 7. The reaper then clears the identity, or run
    `relay-operator.py revoke` by hand once the Lease is free. `call` follows the same rule.
- **Cleanup stages**, each bounded and independent: drain the logs, stop the proxy (logout), remove the containers and
  the socket volume, export the state, revoke plus readback `0 memberships, 0 sessions`, Keycloak logout, release the
  Lease.
- **Exit codes** (precedence order): 3 revoke not confirmed, 4 proxy logout, 5 Keycloak, 6 evidence capture, 7 Lease,
  8 Runner or deadline, 2 refused before any authority.
- **Evidence** (`--evidence-dir`): `run.json` (manifest commit, image, file hashes, deadlines), `proxy.jsonl`
  (one line per call: route, IDs, operationId, status; never bodies or cookies), `runner.jsonl` (the library's
  snapshots and report), `state.tar` and `result.json`.

**Router credential rotation is safe to run under this identity, with a qualification** (Relay v0.14.1). `rotate()`
records the actor only in the `router_credential_operations` journal, and later reconciliation and revocation do not
recheck its membership. But `issue()` re-enters an admin check after recording intent and swallows failures, so a 202
does not prove issuance: the Runner must read the new credential back to a terminal state before the session ends.

**Live read-only smoke** (after merge, before any real run): `--manifest-ref <main commit>:scripts/relay-session-smoke/manifest.json
--bundle scripts/relay-session-smoke/bundle --runner-minutes 2`. Expect `{"smoke": "pass"}` in `runner.jsonl`
(profiles and credentials 200; local refusals for `/api/auth/me` and an unlearned session; the predecessor's control route
refused on the control socket with reason `predecessor-read-only` and on the general socket with `use-the-control-socket`;
no network from the Runner), then rc 0 and `status` 0/0.

**Gates before a real phase A run:**
- this change and the reaper are deployed, and the reaper has been checked once;
- the inference session's adapter and its manifest are merged and pinned to the deployed hub source;
- the observer (`collectCheckpoint`) is merged;
- the predecessor command-receipt export is accepted by the inference session;
- the original browser report has been recovered byte-exact.

#### The phase A observer (automated phase A)

Plan: `plans/2026-11-27-relay-phase-a-observer-plan.md`. A `session` whose manifest has an `observer` block
(`{files, config, predecessorSha256}`) also needs `--predecessor-report FILE`, the preserved original report. Before
any import, the broker checks every observer file and the report against the manifest's hashes.

**How it works:**
- **Host collectors.** The observer's collectors run in the broker process, with the operator's own host credentials:
  `kubectl --context admin@ai` (Router pod reads, read-only `psql`) and `ssh c4@192.168.0.8` (pinned host key). Remote
  scripts and SQL are piped over stdin from the verified bytes.
- **Runner side.** The Runner holds no credentials. It reaches the observer only through
  `relay-observer-gate-<run>`, a `--network none --log-driver none` sidecar serving
  `/run/relay-session/observer.sock`. The gate frames one request per connection on its stdout. A cancel is
  terminal.
- **Files** (`scripts/`):
  - `relay_phase_a_observer.py`: protocol, threads, transition table, evidence;
  - `relay_phase_a_collectors.py`: the collectors and the C8 policy;
  - `relay_phase_a_host.py`: ledger, pre-run, C4 and the probe;
  - `relay-observer-gate.py`;
  - `relay-phase-a/`: the client, the entry, `remote/`, `sql/`, `observer-config.json` and `known_hosts`.

**What it changes on dev-worker-1.** These are the observer's only mutations:
1. **Pre-run** (after the Lease, before any Relay authority): verify the pinned handoff receipt, the units and a
   journal that ends paused, then start **only** `relay-holder.service`. If the holder comes up unpaused, it is stopped
   at once.
2. **C4:** resume admission, then enable both units and start reconciliation. Each step is inside the C3 window and
   re-checks the Lease first.

Revoking the JIT session does **not** undo the resume. Afterwards the holder and reconciliation stay running and
enabled, and the renewed credentials stay live.

**The handoff ledger.** It lives at `%LOCALAPPDATA%\relay-operator\handoff-ledger\<preparationId>.jsonl`, outside any
run directory. Every mutation writes an intent record first and a receipt record after. Each record carries a
checksum.
- **Any existing record makes the next observer run refuse (rc 2). There is no automatic re-entry.**
- **Recovery:** classify the previous attempt read-only, using its receipts, `observer-probe` and the holder journal
  (`journal.jsonl` names the operationId of every admission change). Then move the ledger aside, by hand, with the
  classification recorded beside it.

**Exit code 9** means an observer internal fault: gate or thread death, a protocol violation, or a pre-run failure.
Its cause is in `result.json` and `observer.jsonl`. A failed evidence check is a phase A outcome, not code 9: the
Runner stops, and the broker returns 8.

**Read-only probe:** `python scripts/relay-operator.py observer-probe`. It needs no session, no Lease and no holder
start. It checks every source with its latency: hub deployment, the Router pod and capture config, the trajectory
envelopes, the marker baselines, the routes, the clock bounds, every SQL shape, the pre-run facts, the native
transcript format and the predecessor staging. The 2026-11-27 run: 19/19 checks passed, each call 0.2–1.7 s.

**Open gates** (the collectors answer `unsupported`, and the run stops there):
- **C10 (`reconcile-choice`):** the inference session must name the authoritative reconstruction-required field.
- **The predecessor projection:** it needs the driver change to the pinned-hash contract.
- **The phase A bundle manifest:** it needs #2060 on main.
