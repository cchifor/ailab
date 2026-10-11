# Relay repository authority and independent cleanup

`kubernetes/components/relay-gitea-authority` is optional. It provides private
issuer files to Relay and a separate `relay-gitea-reconciler` Deployment, which
continues cleanup when the web service is unavailable. It includes the existing
restore-generation mount for the web application; both processes use the same
external generation. No live overlay is changed by this component.

Use a reviewed Relay release containing #106–108 and the independent worker's
reconciliation-only fix (`9734d5d` or a descendant). The database must be migrated
to schema 46 before starting either process. The worker may inspect existing
keys, revoke ended authority, cancel unoffered intents and resolve uncertain PR
results. It never creates keys or PRs, even with its feature flags enabled. Only
the web worker may advance newly requested operations. It has no web listener,
Service, Kubernetes API token, Incus certificate, model credential or admin token.
Its database roles remain the existing restricted Relay roles.

Provision an external Secret named `relay-gitea-issuers` in namespace `relay`
through OpenBao/External Secrets. Do not put the actual values in Git or logs.
It contains `deployments.json`, a JSON array of at most 20 deployment descriptors,
and one `issuer-<deployment UUID>` key per descriptor. The descriptor shape is:

```json
[{
  "id": "<deployment UUID>",
  "tenants": ["<Relay tenant UUID>"],
  "origin": "https://git.chifor.me",
  "repositories": ["cchifor/relay"],
  "enabled": false,
  "pullRequests": {"cchifor/relay": "main"},
  "ssh": {
    "host": "git.chifor.me",
    "port": 22,
    "user": "git",
    "hostKey": "ssh-ed25519 <independently verified canonical public key>"
  }
}]
```

The example contains descriptive placeholders and cannot be activated as-is.
Use a dedicated bot with repository administration only on explicitly approved
repositories and the needed repository API scope. Never supply a general worker
PAT or site-administrator token. `pullRequests` is optional and separately opts
in the named base branches. `ssh` is optional until initial checkout is enabled;
verify the server's public host key through an independent trusted route.
Descriptors cannot choose token paths. Missing `enabled` defaults to false.
The token input is a bounded single opaque value, not JSON or a bearer header.

The pinned Node init container validates all inputs before materializing them
into a private 0700 directory with regular 0600 files owned by UID 1000 on a
1 MiB memory volume. The app/worker mount only this output read-only; only their
init containers mount the projected Secret. Symlinked kubelet inputs are supported;
output paths are generated locally. Failures log a fixed message without values.
Secret changes require deliberate replacement of both pods; a running init
container does not watch updates. Keep the old narrow bot available until all
keys it issued have confirmed deletion. Removing a descriptor prematurely leaves
its pending cleanup unavailable.

## Activation sequence

1. Wait for the matching Relay release and this component's CI/reviews. Publish
   its immutable container image. Approve repository/protected-branch policy,
   token scope and SSH host key before enabling any descriptor or pool.
2. Stop Relay and every provisioning, bootstrap and reconciliation worker and
   prevent automatic restart. Migrate the database. Provision the nonsecret
   `relay-recovery-generation` ConfigMap and complete the matching release's
   offline `recovery:fence` with that same file and the normal schema-owner role.
   Follow [restore verification](relay-recovery-verification.md). This component
   does not run migrations or fencing, and a missing/mismatched receipt prevents
   worker startup. Initial activation also retires old authority; schedule it as
   a maintenance operation. Do not apply a new generation to a running estate.
3. Add this component in the activating overlay. Set the reviewed immutable
   image on the `relay` Deployment and add the following **at the top level of
   that overlay's kustomization**, alongside its patches and components:

   ```yaml
   replacements:
   - source:
       kind: Deployment
       name: relay
       fieldPath: spec.template.spec.containers.[name=relay].image
     targets:
     - select:
         kind: Deployment
         name: relay-gitea-reconciler
       fieldPaths:
       - spec.template.spec.containers.[name=reconciler].image
   ```

   A component-level replacement runs before parent overlay patches and can
   silently copy an older image. The final overlay replacement copies the actual
   selected release after patches. Without it the worker deliberately references
   `registry.invalid/relay-gitea-reconciler:release-required` and cannot start.
   Render the entire overlay and verify both images are the same immutable
   digest before review/apply; do not replace the sentinel independently.
4. Keep all descriptors disabled while checking generation refusal, pod-owned
   0600 files, startup, database access and Gitea reachability. Worker egress is
   limited to DNS, Relay Postgres and public TCP 443; no ingress is admitted. A
   private Gitea address or non-443 endpoint needs a reviewed policy change.
5. Enable the approved descriptor on both pods and qualify an isolated guest:
   repository scope, protected branches/tags, PR base and existing CI/reviews,
   copied-key rejection after expiry/withdrawal/host loss, and revocation while
   the web Deployment is stopped. Verify no new key or PR is created by the
   independent worker. Initial-workspace checkout and native profile admission
   remain separate acceptance gates; enabling this component does not admit a
   production Incus pool.

For rollback, disable `RELAY_AGENT_CONTROL_PLANE` and `RELAY_ENVIRONMENTS` in
**both** deployments (or disable the original issuer descriptor while retaining
its token/configuration) and replace both pods. Keep reconciliation running until
all known keys have confirmed absence. The independent worker's default `1`
flags preserve currently valid keys during a web outage; they do not authorize
new issuance. Merely stopping the web service does not withdraw that authority.
Gitea deploy keys have no native expiry; cleanup still needs a live worker,
healthy database, reachable Gitea and valid issuer credential. Record and accept
this outage limitation explicitly. Post-snapshot resources require independent
external inventory before recovery admission.

The optional `relay-recovery-monitoring-schema46` component follows the original
`relay-control-monitoring` and `relay-recovery-monitoring` components. Choose it
only with a matching schema-46 release and an actual successful isolated restore
receipt. It overrides only the schema policy and preserves the reviewed verifier
hash; schema-42 and schema-43 components keep their existing behavior. Earlier
receipts become unavailable with the schema-46 policy. No receipt or live
activation is supplied by this change.

Run `python3 -B scripts/tests/integration/test_relay_gitea_authority.py` locally.
It renders the combined optional components, validates Kubernetes schemas, tests
release propagation after an overlay patch, checks worker isolation and runs
private-file setup/invalid-input/restart scenarios in the pinned Node image with
synthetic tokens. It contacts no live issuer and cannot establish live bot scope,
cluster volume ownership, egress or outage-time revocation acceptance. Operator
request 9 tracks that evidence.
