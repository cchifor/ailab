# AILab release

Relay follows the existing llm-router topology: Cloudflare proxied CNAME → the locally managed ailab tunnel → `relay.relay.svc.cluster.local:80`, with the app handling browser/connector authentication. The public origin is `https://relay.chifor.me`; it is intentionally free of interactive Cloudflare Access challenges so connector WebSockets can authenticate using their own credentials.

The authoritative manifests live in `cchifor/ailab`, directory `kubernetes/apps/apps/relay`. The tunnel route is in `apps/edge/cloudflared.yaml`; DNS is declared in `kubernetes/infra/cloudflare/variables.tf`. Both require the normal protected-main GitOps review flow. Do not patch the shared tunnel or suspend Flux to bypass that flow.

## Release payload

`relay-0.1.0-20261001` is a versioned, immutable release containing compiled server/UI code and the production dependencies extracted from the tested `relay-platform:0.1.0` image. It is mounted read-only under a digest-pinned Node 22.23 runtime. There is no package installation at startup.

- `relay-releases`: 2 GiB RWX `nfs-csi`, code only.
- `relay-data`: 2 GiB RWO `qnap-iscsi`, plugin artifacts.
- `relay-postgres-data`: 5 GiB RWO `qnap-iscsi`, PostgreSQL 17 data and WAL.
- Single Relay replica with `Recreate`; single PostgreSQL instance. This deployment is not HA.
- Restricted Pod Security, non-root containers, dropped capabilities, read-only roots, no service-account token, default ingress isolation, and explicit DNS/Postgres/public-HTTPS egress.
- Existing Relay administrator and GPT Luna router credentials are SOPS-encrypted for Flux; no estate credentials are given to the app.

## Initial deployment sequence

1. Use an explicitly authorized ailab deployment kubeconfig. The worker's test/observer kubeconfigs are insufficient.
2. Apply only Relay's namespace, encrypted Secret projection, PVCs, PostgreSQL, and network policies. Wait for the database.
3. Create the operator-only `staging/relay-stage.yaml` pod, which is deliberately excluded from Flux's Kustomization. It mounts Relay's own release and artifact volumes.
4. Verify the release archive SHA-256, then extract its versioned directory under `/releases`. Never rewrite a currently mounted release.
5. Briefly stop the local Relay service to freeze writes. Back up PostgreSQL with `pg_dump -Fc`, excluding session/lease/enrollment-token data, and archive `.data/plugins`. Transfer and restore them only to Relay's new database and data volume. Never publish these backups as source/release assets.
6. Run the release's migration and bootstrap commands against the destination. Preserve existing tenant IDs, agents, conversations, and plugin state. The restricted application role must not bypass RLS.
7. Delete the staging pod before starting the application, so the RWO artifact volume cannot block scheduling. Start Relay and verify readiness, login, plugin state, Assistant, and WebSocket origin/authentication before publishing.
8. Create just the proxied `relay.chifor.me` CNAME to `f93d9a6a-5172-43d3-8bef-13460ea7607b.cfargotunnel.com` with an authorized DNS Edit token. Add the resulting record ID in a Terraform import block. Do not apply an empty Terraform state against the estate.
9. Merge the reviewed AILab PR and reconcile its Flux source/apps Kustomization. The tunnel revision annotation triggers both tunnel connectors to reload the added route.
10. Verify public HTTPS, authenticated browser flows, unauthenticated 401 responses, and connector WebSocket transport. Keep the stopped local database and snapshot as rollback material; reconnect any agent hosts to the new HTTPS origin.

The existing administrator token remains valid. The Assistant uses the already approved `Relay Workspace Assistant` router identity and `gpt-6-luna`; it never receives the router administrator credential.

## Recovery

For a failed first rollout, restore the local service and keep the public route unmerged until fixed. For subsequent rollouts, preserve the previous release directory and change the Deployment subPath back only when its schema is compatible. Restore PostgreSQL and plugin artifacts from the same backup point when a schema rollback is required. Do not run two control planes against the same database.

## Prepared release

Source: `cchifor/relay-platform@ed1631f88a3501a65915ac7608dc55a25d5ceb21`

Archive: `relay-0.1.0-20261001.tar.gz`, attached with its checksum to the private [Relay v0.1.0 candidate release](https://git.chifor.me/cchifor/relay-platform/releases/tag/v0.1.0).

SHA-256: `35f9759c1dd9eb1c8787404008f9db4e0b30a9a21b33e9b2beb4f25d2326f56d`

Publication status must be verified from live cluster/DNS checks; preparing these manifests alone does not publish the hostname.

## Validation before cluster staging

- [Application CI](https://git.chifor.me/cchifor/relay-platform/actions/runs/56308) passed: type checks, formatting, production build, Rust tests, and integration/browser tests.
- AILab manifest lint passed: 33 Kustomization paths, 723 rendered resources, zero invalid resources or errors; 81 resources skipped by the repository's existing schema exclusions. Inline-hash and Flux Job annotation checks passed.
- The exact release archive passed a Docker smoke test with the pinned runtime images, PostgreSQL UID 70, Node UID 1000, read-only roots, dropped capabilities, and no privilege escalation. Migration, bootstrap, readiness, unauthenticated rejection, login, secure cookies, and all five builtin plugins succeeded.
- These checks do not establish cluster storage, tunnel reachability, or public DNS readiness. Keep the deployment PR in draft until the release and existing data are staged and the destination passes its internal checks.
