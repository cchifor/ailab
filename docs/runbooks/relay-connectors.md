# Pinned Relay connector service adoption

`ansible/relay-connectors.yml` reconciles an **already-enrolled** worker's existing
tmux connector identity and user service. It is opt-in and is not included by the
worker/site playbooks or the daily fleet converge. It does not enroll a host,
rewrite its identity/token/service settings, approve access, enable ACP launches
or start an agent. Relay's R0 rollout remains incomplete until the actual worker
inventory, logout/reboot and fresh hub heartbeat checks pass.

## Release and inventory

The default release is the public v0.6.2 connector manifest observed on
2026-10-14 at
`https://relay.chifor.me/downloads/connector/v0.6.2/manifest.json`:

| Field | Pin |
|---|---|
| Source | `f52693cef05985e7ba8164b26362ac40c8aa39e4` |
| Linux amd64 SHA-256 | `8cf35551be1390d8ec1c834f36166d5ace623bb7bc83cbd1530b570581ebea81` |
| Linux arm64 SHA-256 | `3a43de6b91a536e179bb40ec0227bb5cdd1f6560a786be89b136d4d1488e5d51` |

The pins above were checked against the published manifest at
`https://relay.chifor.me/downloads/connector/v0.6.2/manifest.json` on
2026-10-14. The manifest reports source commit `f52693cef05985e7ba8164b26362ac40c8aa39e4`
and the same architecture-specific digests. The connector role only changes the
content-addressed binary pin; its service unit, identity checks and activation
gates are unchanged.

Obtain the versioned binary on the controller from that reviewed release and
compare the manifest/source/digest. Pass its local absolute path; workers never
build, fetch `latest` or replace a global binary. SHA-256 binds the reviewed
bytes, not an independent publisher signature. Future upgrades change the
release object through review, including both architecture hashes and source.

Reconcile the current `dev_workers` inventory first. Do not assume the historical
count of four. For each selected worker, record its execution user, architecture,
connector host UUID, state path, service name, selected sockets and linger
status. Keep tokens in their existing private identity file. Use nonsecret
host-scoped variables with this shape:

```yaml
relay_connector_enabled: true
relay_connector_manage_service: false
relay_connector_user: reviewed_execution_user
relay_connector_arch: amd64
relay_connector_source: /absolute/controller/release/relay-connector-linux-amd64
relay_connector_host_id: reviewed-existing-host-uuid
relay_connector_state_file: /absolute/worker/private/connector.json
relay_connector_name: Existing reviewed service name
relay_connector_sockets: ['']  # Must exactly match existing service configuration.
```

The example identity/path values must be replaced, not copied into inventory.
Home and primary group default to the existing passwd/group records, including
nonstandard account homes and group names; explicit overrides are for reviewed
custom layouts. The role resolves these facts before validating paths.
The service filename is `relay-connector-<host UUID>.service`; it matches Relay's
installer convention. The existing state must be a regular single-link 0600
file owned by that execution user, with the exact origin, explicit approved phase, account,
name and sockets. A missing or mismatched identity fails; use the normal reviewed
enrollment flow separately for a new installation. Phase-less legacy state is
unsupported by this role; establish approval through the existing connector/hub
workflow before adoption rather than inferring it from a missing field. Do not copy a worker token
into Ansible variables, logs or operator handoff files.

## Current inventory (2026-10-14)

All four dev-workers run the account-scope service on the default tmux socket, and each
has its pin in `ansible/host_vars/dev-worker-N.yml`:

| Worker | Installation UUID | State file | Origin of the installation |
|---|---|---|---|
| dev-worker-1 | `b3b1be1a-…` | `~/.config/relay-connector/connector.json` | migrated from the v0.1.0 pane runner `relay-connector.service` (disabled) |
| dev-worker-2 | `14123362-…` | `~/.local/state/relay/f765a05a957da5b4/connector.json` | new 2026-10-14 |
| dev-worker-3 | `47405392-…` | `~/.local/state/relay/relay.chifor.me/connector.json` | migrated from the v0.2.2 `join` runner in tmux session `relay-chifor` (removed) |
| dev-worker-4 | `a7366b7a-…` | `~/.local/state/relay/f765a05a957da5b4/connector.json` | service mode since 2026-10-04 |

dev-worker-1 and dev-worker-3 kept their original state paths: `service install --state` on the
existing identity keeps the UUID and credential, and only the scope change needed a fresh approval.
A detached tmux session that runs the connector belongs to the pre-v0.3.0 workflow. tmux-continuum
restores such a session after every reboot as an empty shell, so remove it after a migration.

The controller binary lives in the WSL converge clone's sibling directory,
`~/.ailab-converge/artifacts/relay-connector/v<version>/relay-connector-linux-amd64`, fetched on
Windows (WSL has no internet) and checked against the pinned SHA-256. Pass it with
`-e relay_connector_source=…`; it is not committed.

## Converge one worker

On the existing Ansible control node, use its normal reviewed inventory:

```sh
cd ansible
ansible-playbook relay-connectors.yml --limit <reviewed-worker> -e @<host-vars-file> --check --diff
ansible-playbook relay-connectors.yml --limit <reviewed-worker> -e @<host-vars-file> --diff
```

Staging requires a reachable user manager and the selected connector service
inactive/dead with no pending job. It never stops a running service for you.
For an intentional online upgrade, review the reconnect window, set
`relay_connector_manage_service: true` and rerun. Linger must already be enabled
by the worker account bootstrap; the role checks it and never silently grants it.
Only the selected service is enabled/restarted. Tmux sessions, other connectors,
router renderers and credentials are preserved.

The role verifies controller and installed hashes, native architecture and
`--version`; installs root-owned content-addressed artifacts; and uses the
current connector's unit hardening and `serve` command. Network readiness uses
the connector's reconnection loop and service restart policy, not a system-only
network target in the user manager. The effective unit must
be the managed file without drop-in overrides. An applied nonsecret unit revision
is written only after successful service activation. It makes a later activation
of staged files and a retry after interrupted restart converge correctly.
Unchanged managed reruns do not restart the connector. `--check` never reloads,
enables or starts services and does not record an applied revision.

Confirm `systemctl --user is-enabled/status` as the execution user, the installed
hash/version, `loginctl show-user <uid> --property=Linger`, and a fresh Relay hub
heartbeat under the same host UUID with the expected account, sockets, version
and linger metadata. Check actual terminal reconnect and logout/reboot
persistence in the rollout window. The applied revision is a convergence receipt,
not proof of a live authenticated heartbeat or production acceptance.

Rollback repins a previously reviewed compatible binary/source and reapplies;
never restore an old identity file or clear hub blocks. Keep the previous binary
until rollback qualification completes, then delete obsolete artifacts. Full
decommission uses the existing host revoke/drain and service uninstall workflow;
turning this role off only disables reconciliation.

## Qualification

The dedicated workflow runs
`scripts/tests/integration/test_relay_connectors.py` with Ansible 2.20.0 and
PyYAML 6.0.3. It uses disposable files and strict local `systemctl`/`loginctl`
fixtures, never host service commands, sudo or production credentials. Cases
cover disabled mode, checksum/architecture rejection, identity preservation,
private state, unapproved/cross-account inputs, staging safety, linger, drop-in
denial, interrupted activation, idempotence, check mode and offline unit parsing.
Set `TMPDIR` under `/workspace` for local runs. Real estate convergence and
logout/reboot evidence are operator acceptance work.
