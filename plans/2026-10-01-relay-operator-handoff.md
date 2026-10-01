# Relay operator handoff — 2026-10-01

The requested endpoint is `https://relay.chifor.me`. The application and release are ready; cluster staging, DNS publication, and protected-main approval are pending. The deployment uses the existing llm-router pattern.

- Deployment PR: <https://git.chifor.me/cchifor/ailab/pulls/1010>
- Application: <https://git.chifor.me/cchifor/relay-platform>
- Release archive and checksum: <https://git.chifor.me/cchifor/relay-platform/releases/tag/v0.1.0>
- Deployment and migration sequence: [Relay runbook](../docs/runbooks/relay.md)

## Request 1: namespace-scoped deployment access

On ailab, create the `relay` namespace from the PR's `kubernetes/apps/apps/relay/namespace.yaml`, preserving its restricted Pod Security labels. Create a temporary deployment identity scoped to that namespace and place its kubeconfig on **dev-worker-2** at:

`/workspace/c4/relay-platform/.data/ailab-kubeconfig`

Make the file readable only by the workspace user `c4` (mode `0600`). Use a short-lived credential sufficient for rollout, preferably 24 hours, and include its expiry in the handoff response. Do not put credential values in the PR, chat, or logs.

Required permissions within `relay`:

| API group | Resources | Permissions |
| --- | --- | --- |
| core | pods, services, persistentvolumeclaims, secrets | get, list, watch, create, update, patch, delete |
| core | pods/exec | get, create |
| core | pods/log, events | get, list, watch |
| apps | deployments, statefulsets | get, list, watch, create, update, patch, delete |
| apps | replicasets | get, list, watch |
| apps | deployments/scale, statefulsets/scale | get, update, patch |
| networking.k8s.io | networkpolicies | get, list, watch, create, update, patch, delete |

The deployment needs the existing `nfs-csi` and `qnap-iscsi` StorageClasses. Confirm both are usable by this namespace. No cluster-admin credential, access to other application secrets, or write access to `edge`/`flux-system` is needed. Shared resources will change only through the reviewed PR.

Once access is available, the implementation agent will stage the immutable release, migrate the local PostgreSQL data and plugin artifacts, and verify the destination before requesting merge. The original local service remains available until the migration window. Existing administrator and Assistant credentials must be preserved.

## Request 2: create the Cloudflare DNS record

Using the operator's authorized Cloudflare credentials, create this one record in the `chifor.me` zone if absent:

| Field | Value |
| --- | --- |
| Zone ID | `c967ce7dbbf43b1d7599eb4d213efa57` |
| Type | `CNAME` |
| Name | `relay.chifor.me` |
| Target | `f93d9a6a-5172-43d3-8bef-13460ea7607b.cfargotunnel.com` |
| Proxied | `true` |
| TTL | `1` (Auto) |

If the exact record already exists, reuse it and return its ID. If a conflicting record exists, report it before replacing it. Return the DNS record ID so the implementation agent can add the Terraform import block to PR #1010. Do not run Terraform against an empty state or change unrelated records.

Use the existing ailab tunnel. Do not add an interactive Cloudflare Access challenge: Relay handles browser login and connector authentication, and connectors need WebSocket access. DNS creation alone does not make the application available; the route remains pending until the reviewed deployment is merged. No Cloudflare token needs to be shared if the operator creates the record.

## Request 3: protected-main review and rollout

Review PR #1010 under the existing AILab main-branch policy (one approval). **Keep it in draft and do not merge until the release and database are staged, internal application checks pass, and the DNS import ID is recorded.** The release volume is initially empty, so an early merge would deploy an application that cannot start.

When the implementation agent reports staging complete, approve and merge through the normal review flow. Allow Flux to adopt `kubernetes/apps/apps/relay` and roll the shared tunnel using its revision annotation. Do not suspend Flux or patch the live shared tunnel to bypass review. If an explicit Flux reconciliation is needed, perform it using the operator's normal infrastructure access.

The desired tunnel route is `relay.chifor.me` → `http://relay.relay.svc.cluster.local:80`. Public acceptance checks cover HTTPS, readiness, authentication, Assistant responses, and connector WebSockets. Deployment remains incomplete until those checks pass.

## Release identity and existing verification

- Source commit: `ed1631f88a3501a65915ac7608dc55a25d5ceb21`.
- Release directory: `relay-0.1.0-20261001`.
- Archive SHA-256: `35f9759c1dd9eb1c8787404008f9db4e0b30a9a21b33e9b2beb4f25d2326f56d`.
- Local archive: `/workspace/c4/relay-platform/.data/relay-0.1.0-20261001.tar.gz` on dev-worker-2.
- Application CI, AILab manifest validation, and the restricted-container release smoke test passed. Live storage, tunnel, and domain verification remain pending.

## Expected operator response

Return only the deployment kubeconfig path and expiry, namespace/storage readiness, Cloudflare record ID, and PR review status. Never return secret contents.

## Resolution during rollout

The operator created the namespace and scoped identity (ServiceAccount in separate `relay-deploy` namespace), supplied a kubeconfig expiring 2026-10-02T18:20:00Z, and created DNS record `d01f099abc5e5a85c6c5bef5791c8e41`. The temporary Role was extended for ConfigMaps and batch Jobs/CronJobs to stage database bootstrap and backup resources. These grants remain confined to `relay`.

Review findings were addressed: each Secret has its own encryption/MAC; DNS has an import block; the bootstrap superuser is separate from runtime/migration identities; nightly logical backups have a continuously mounted dump volume and a successful live restore drill; the runbook documents post-merge recovery. Release/data staging and internal checks are complete. Public verification follows merge.

After public verification, the operator must delete the out-of-band `relay-deployer` Role and RoleBinding in `relay`, and the `relay-deploy` namespace. The scoped deployer cannot remove these resources itself. Remove the local kubeconfig afterward. Do not delete the application namespace or its PVCs.
