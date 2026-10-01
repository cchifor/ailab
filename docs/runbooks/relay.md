# Relay

Pending access and publication prerequisites are listed in the [operator handoff](relay-operator-handoff.md).

Relay follows the existing llm-router topology: Cloudflare proxied CNAME → the locally managed ailab tunnel → `relay.relay.svc.cluster.local:80`, with the app handling browser/connector authentication. The public origin is `https://relay.chifor.me`; it is intentionally free of interactive Cloudflare Access challenges so connector WebSockets can authenticate using their own credentials.

The authoritative manifests live in `cchifor/ailab`, directory `kubernetes/apps/apps/relay`. The tunnel route is in `apps/edge/cloudflared.yaml`; DNS is declared in `kubernetes/infra/cloudflare/variables.tf`. Both require the normal protected-main GitOps review flow. Do not patch the shared tunnel or suspend Flux to bypass that flow.

## Release payload

`relay-0.1.0-20261001` is a versioned, immutable release containing compiled server/UI code and the production dependencies extracted from the tested `relay-platform:0.1.0` image. It is mounted read-only under a digest-pinned Node 22.23 runtime. There is no package installation at startup.

- `relay-releases`: 2 GiB RWX `nfs-csi`, code only.
- `relay-data`: 2 GiB RWO `qnap-iscsi`, plugin artifacts.
- `relay-postgres-data`: 5 GiB RWO `qnap-iscsi`, PostgreSQL 17 data and WAL.
- `relay-postgres-dumps`: 5 GiB RWO `qnap-iscsi`, consistent logical backups, continuously mounted for Velero.
- Namespace and persistent volumes are protected from Flux pruning; an intentional deletion needs an explicit follow-up change.
- Single Relay replica with `Recreate`; single PostgreSQL instance. This deployment is not HA.
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

`relay-postgres-dump` runs at **00:15 UTC daily**, before Velero's 02:00 daily. It writes a `pg_dump -Fc` archive, table of contents, SHA-256, and `COMPLETE` marker to a temporary directory, then atomically publishes it. Only successful publication allows pruning; the newest seven completed generations remain. Failed writes keep previous backups. The job has a 30-minute deadline and is colocated with the holder pod for the RWO claim.

`relay-dumps-holder` continuously mounts the dump volume read-only, without credentials, for Velero filesystem backup. The live PostgreSQL `database` volume is explicitly excluded from filesystem copying; it is not a consistent backup. Restore uses the complete logical dump plus the matching plugin artifacts on `relay-data`, the immutable release archive, and the SOPS secrets. The roles/bootstrap configuration are recreated from Git; role passwords are not published in dumps.

To test a fresh backup, create a Job from the CronJob and wait for completion. Check `SHA256SUMS`, then restore the archive into a **separate disposable database** as the migration role with `--no-owner --no-acl --exit-on-error`. Compare row counts and verify runtime grants and RLS before considering it restorable. A successful `pg_restore --list` alone is insufficient. Clean up the disposable database after the comparison.

This is daily logical recovery, not PITR. A first local restore drill passed with the real workspace snapshot and reduced privileges. The first live dump/restore and Velero capture must be verified separately; a mounted PVC alone is not proof of an offsite backup.

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
- AILab manifest lint passed: 33 Kustomization paths, 732 rendered resources, zero invalid resources or errors; 83 resources skipped by the repository's existing schema exclusions. Inline-hash and Flux Job annotation checks passed.
- The exact release archive passed a Docker smoke test with the pinned runtime images, PostgreSQL UID 70, Node UID 1000, read-only roots, dropped capabilities, and no privilege escalation. Migration, bootstrap, readiness, unauthenticated rejection, login, secure cookies, and all five builtin plugins succeeded.
- These checks do not establish cluster storage, tunnel reachability, or public DNS readiness. Keep the deployment PR in draft until the release and existing data are staged and the destination passes its internal checks.
