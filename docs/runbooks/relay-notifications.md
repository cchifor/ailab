# Relay notification provisioning and rollout

The optional components in `kubernetes/components/relay-notifications/` provision
Relay's metadata-only ntfy delivery. They are **not included in either live
Kustomization**. The existing Relay v0.3.5 deployment lacks the notification
worker; including a component is not a release upgrade or production acceptance.

## Authority and configuration

The ntfy component keeps the pinned 2.29.0 server and its existing `ailab` and
`qnap` postStart reconciliation. Native declarative provisioning adds two regular
accounts, never administrators:

| Account | Topic | Authority |
|---|---|---|
| `relay-publisher` | `relay-actions` | Publish only |
| `relay-subscriber` | `relay-actions` | Subscribe only |

Each account also has a wildcard deny grant, so an unrelated public topic cannot
expand its authority. Anonymous users remain denied. The existing `ailab` admin
retains administrator access, including subscription to this topic. Give devices
the subscriber credential rather than the publisher or administrator credential.
All provisioned tokens have their account's authority; they are not inference,
connector, browser-login or agent messaging tokens.

This first deployment is **one approved tenant, one topic**. Do not point multiple
tenants at the same topic. Additional tenants require separate publisher and
subscriber accounts, topics, Secrets and reviewed configuration.

The operator provisions these Secrets through the existing OpenBao/SOPS process:

| Namespace / Secret | Required keys |
|---|---|
| `monitoring/ntfy-relay-auth` | `publisher-hash`, `subscriber-hash`, `publisher-token`, `subscriber-token` |
| `relay/relay-notifications` | `tenant-id`, `publisher-token` |

Use `ntfy user hash` with unrelated strong random passwords for each verifier;
bcrypt costs 10–12 are accepted. Use `ntfy token generate` for each distinct
32-character `tk_` token. The publisher token must match in both Secrets. The
tenant UUID must be the actual tenant authorized for these alerts, not a copied
example. Keep values within the secret-management route; never paste them into
PRs, command arguments, reports or logs. Do not generate a subscriber token from
an administrator account. No credential or encrypted placeholder is committed by
these components.

The ntfy initializer accepts only verifiers and opaque tokens. It fixes the
account names, roles, topic and permissions in code and rejects malformed values
without printing them. It requires the base `deny-all` configuration and refuses
to override existing `auth-users`, `auth-access` or `auth-tokens` lists. The
generated server configuration is a regular 0600 file in memory-backed storage;
it has the same root ownership as the current ntfy execution user. Existing
templates and the auth/cache volume locations are preserved.

The Relay initializer runs as UID/GID 1000. Kubernetes Secret projections use
root-owned symlinks; Relay correctly rejects those as credential files. The init
container reads the projection, validates its bounded contents and creates its
own 0700 directory and regular, single-link 0600 files in a memory-backed volume.
The application receives that volume read-only and only the destinations-file
path in its environment. It has no mount of the source Secret and no subscriber
credential. A failed setup prevents application startup. Interrupted temporary
writes are replaced on the next init attempt.

## Rollout gates and order

1. Complete Relay's release/restore/OIDC gates and select a reviewed release
   containing the notification worker. Preserve the coordinated database/artifact
   backup before migration. These components neither change the image nor set
   `RELAY_AGENT_CONTROL_PLANE=1`.
2. Inspect the existing ntfy configuration and account inventory through the
   approved operator route. This component owns the complete **declaratively
   provisioned** account set. Confirm there are no other provisioned accounts,
   reserved-name collisions or existing environment/CLI overrides of `auth-*`.
   Adopt nothing implicitly: a pre-existing account named `relay-publisher` could
   carry unmanaged ACLs or tokens. Remove that collision through a separate
   reviewed operation first. Retain a recoverable auth-volume backup.
3. Provision both Secrets and record only their names, owner and synchronization
   time in the operator handoff. Validate the publisher token equality in the
   secret-management process without displaying either value.
4. In a rollout PR, add the ntfy component to
   `kubernetes/apps/infrastructure/monitoring/kustomization.yaml`:

   ```yaml
   components:
   - ../../../components/relay-notifications/ntfy
   ```

   Let Flux reconcile. This restarts the shared ntfy service. Confirm init,
   readiness, existing Alertmanager/QNAP delivery and the access checks below.
   A new script ConfigMap hash rolls the pod automatically. Base-config changes
   still require the existing template-revision rollout annotation.
5. After the new Relay release and ntfy checks pass, add the Relay component to
   `kubernetes/apps/apps/relay/kustomization.yaml` in a rollout PR:

   ```yaml
   components:
   - ../../../components/relay-notifications/relay
   ```

   Delivery additionally requires the independently reviewed control-plane flag.
   The HTTPS destination is fixed to `https://ntfy.chifor.me/relay-actions`, using
   the existing public HTTPS egress rule. No private-network egress is added.
   Confirm certificate trust, DNS, edge routing and authenticated publication in
   the actual namespace; local Docker tests cannot prove those network gates.
6. Subscribe a device with the restricted subscriber credential. Create one real
   pending permission, question and agent access request. Confirm generic alerts
   with links to Relay's authenticated Sessions/Messages pages, and verify source
   text, tool arguments, answers and secrets never appear in the alert. Confirm
   `GET /api/agent-notifications` reports delivery for the correct tenant.

The access check must prove anonymous subscribe/publish denial, publisher
subscribe denial, subscriber publish denial and unrelated-topic denial. A 200
publish means ntfy accepted the message; it does not prove device delivery. Record
both acceptance and a device observation. Alerts cannot resolve requests. Users
must open Relay, authenticate and act under current authorization.

## Rotation, rollback and restore

Secret projections update in place, but **these private copies are taken only at
pod initialization**. Secret changes therefore require a reviewed rollout of the
affected deployments, using an explicit nonsecret credential-revision annotation
or the normal operator restart process. Changing a Secret alone is insufficient.

For publisher rotation, pause external delivery first by removing the Relay
component and reconciling Relay (the inbox and source requests remain available).
Rotate the publisher token in both Secrets, roll ntfy, and prove the old token is
rejected and the new token has only the intended authority. Re-enable the Relay
component and roll Relay, then perform a fresh canary. Avoid rotating while the
worker is publishing: 401/403 results are terminal for those outbox rows. Relay
does not blindly resend failed rows, and expired requests must not be revived.

Subscriber rotation needs only the ntfy Secret, ntfy restart and device update.
Native ntfy reconciliation removes the previous provisioned token. It also
restores the configured role and provisioned ACLs after drift, and recreates
accounts/tokens after an empty-volume rebuild. An old auth-database restore is
reconciled against current Secrets when the server starts. Restoring an obsolete
Secret would restore its authority too; restore the current credential generation
and prove obsolete tokens fail. Previously unmanaged ACLs/tokens are outside
native provisioning's removal contract, hence the collision gate above.

For rollback, disable Relay delivery first. Removing the ntfy component and
restarting on the base configuration removes the declaratively provisioned Relay
accounts/tokens; the existing manually reconciled `ailab` and `qnap` accounts
remain. Remove unused Secrets through the normal secret lifecycle. Do not roll
ntfy back to a version without the verified provisioning semantics.

Watch queued age, failed rows and overflow through Relay's administrator status
endpoint. A stopped publisher does not disable the actual Relay inbox. Keep the
notification service optional during incident response.

## Reproducible qualification

Run as an unprivileged user with Docker and PyYAML:

```sh
python3 scripts/tests/integration/test_relay_notifications.py
```

The CI workflow builds both opt-in components with pinned Kustomize, executes
their rendered initialization scripts under the pinned Node image, and runs real
ntfy 2.29.0 with the existing postStart hook. It covers private ownership/mode and
Secret symlinks, malformed-input redaction, interrupted init, authority boundaries
(including an unrelated public topic), role repair, token rotation, auth-volume
rebuild and component withdrawal. It also checks that Relay's release and backup
gates remain intact. Containers and scratch data are removed after each run.
These tests do not establish live credentials, network routing or device receipt.
