# Relay router credential workers

The opt-in `relay_router_renderer` role stages the independent renderer and a dedicated
OpenBao AppRole agent. It is not part of `site.yml` or `dev-workers.yml`; no host enables it
by default. It neither enrolls a Relay connector nor grants a router key. The hub and holder
must still validate the exact credential generation before a routed session opens.

This role depends on Relay's renderer through [PR #44](https://git.chifor.me/cchifor/relay/pulls/44),
including private CA support. Native harnesses, worker rollout and estate OpenBao trust remain
release gates. The existing holder still admits only protocol fixtures. Do not enable a real
Codex/Claude profile on the basis of this provisioning test.

## Inputs and scope

Set the following in reviewed per-host inventory or a separate staging vars file:

```yaml
relay_router_enabled: true
relay_router_manage_services: false # stage first; use only while these managed units are stopped
relay_router_user: c4
relay_router_tenant_id: TENANT_UUID
relay_router_host_id: EXISTING_APPROVED_CONNECTOR_UUID
relay_router_connector_state_file: /home/c4/.local/state/relay/INSTANCE/connector.json
# Controller-local artifacts from the reviewed release receipt. No global installs.
relay_router_renderer_source: /workspace/releases/RELEASE/relay-credential-renderer
relay_router_renderer_sha256: RENDERER_SHA256
relay_router_bao_source: /workspace/releases/RELEASE/bao
relay_router_bao_sha256: BAO_BINARY_SHA256
relay_router_bindings:
  - tenantId: TENANT_UUID
    hostId: EXISTING_APPROVED_CONNECTOR_UUID
    executionIdentity: c4
    connectionId: CONNECTION_UUID
    bindingId: BINDING_UUID
    origin: https://llm-router.chifor.me
    credentialFile: /home/c4/.local/state/relay/router-managed/BINDING_UUID/credential.json
    protocol: codex-responses
    selection: {kind: model, model: QUALIFIED_REAL_MODEL}
```

Use actual reviewed IDs and catalog model names. The complete `router` policy must match the
administrator-approved hub/holder profile, including the credential filename. The role rejects
cross-host/tenant/user policies, duplicate bindings, native route aliases and arbitrary file
destinations. Up to 32 bindings share one host-scoped reader identity; each gets its own renderer.
The role requires an already approved private connector state file, checks its owner and origin,
and never changes it or copies its token into a configuration file. Connector/holder/Node/bridge
artifact packaging is a separate milestone; these two binaries alone cannot start agent sessions.

Both executable sources are regular controller files and must match the supplied SHA-256 before
worker changes. Installed files are verified again under content-addressed directories in
`/opt/relay/router-worker`. The worker does not compile code or download mutable releases. Obtain
the renderer from a verified Relay release. For OpenBao, extract `bao` from the reviewed 2.5.5
upstream archive (archive SHA `2c5577707e97fc95c2086950f39880ead5e45b356c94388e5cb606f5a5c2b697`);
the Linux x86-64 binary used in qualification has SHA
`4e7e04cc5d13043b9d67850d875242a42fae5c5f01aaff226a6ebba77ccc27bb`.
Do not replace a missing release receipt with an arbitrary hash of an unknown download.

The default estate CA comes from the existing reviewed `openbao_agent/files/ailab-root-ca.crt`.
The role copies it as a 0600 file owned by the execution user; renderer trust is scoped to that
bundle. OpenBao's LAN name must already resolve through the worker's existing managed DNS/hosts
configuration. No global trust store, legacy `openbao-agent.service`, `cred` sink, host login or
last-good router-key cache is modified or used as a fallback.

## Provision the independent reader identity

The OpenBao operator must review/apply `reader-policy.hcl.j2` with the intended mount, prefix,
tenant and host. It permits only `read` under the exact KV v2 data prefix:

```hcl
path "secret/data/relay/router/TENANT_UUID/HOST_UUID/*" {
  capabilities = ["read"]
}
```

Create one dedicated AppRole per tenant/host/execution identity with only this policy plus the
normal default self-token lifecycle policy. Use a renewable periodic token, bounded SecretID
lifetime and a documented rotation schedule. The automated fixture exercises a 10-second period;
production periods/lifetimes must follow the estate operator's reviewed availability policy.
The role uses the existing `auth/approle` mount. Never supply Relay's escrow writer, the router
management identity or the legacy host-wide reader AppRole. Bootstrap access remains outside the
model-facing tool interface. Scope/capabilities must be checked using the resulting reader token,
including negative reads across tenant/host, KV list/write/delete/destroy denials.

Store the resulting identity in a separate SOPS file selected by `relay_router_secrets_file`:

```yaml
relay_router_credentials:
  dev-worker-N:
    role_id: DEDICATED_ROLE_ID
    secret_id: DEDICATED_SECRET_ID
```

The role writes owner-only identity files and redacts Ansible output/diffs. Do not pass secrets
in `-e` command-line arguments. The agent runs in the execution user's trust domain and renews a
0600 token in `/run/user/UID/relay-router-auth/token`, under a systemd-created 0700 directory.
R1–R3 retain the documented same-UID, passwordless-sudo trust boundary; this is not a sandbox
against another malicious process with that UID.

## Staging, activation and rollback

Use `ansible/relay-router-workers.yml --limit <reviewed-host>` with the SOPS and inventory inputs.
Stage with `relay_router_manage_services: false` on a host where the managed services are stopped;
this writes files and units but never contacts the user manager. Rerunning unchanged staging is
idempotent. Inspect the reference-only configuration and host-scoped policy, verify unit syntax,
verify real AppRole scope/TLS and then enable service management in a reviewed inventory change.
The role verifies that user linger is already enabled; it never changes linger automatically.

The unit pair is `relay-router-auth.service` and one
`relay-router-render-BINDING_UUID.service` per binding. Renderers *want* the auth service but do
not require its health: absent/retired-file cleanup must still run during an OpenBao outage.
Each renderer may write only its binding directory. Auth can write only its runtime sink.
User-manager filesystem sandboxing requires working unprivileged user namespaces, including
the worker's AppArmor policy; offline syntax checks do not establish that runtime capability.

After activation, verify the exact-version render receipt, independent holder installation
receipt and first default-communication tools check. Exercise rotation, lost receipt retry,
remote revocation and cleanup. Do not equate an active systemd unit or a rendered file with a
ready holder binding. No services or real worker credentials were changed in this milestone.

When replacing artifacts/configuration, keep reconciliation running and disable new launches
until the change passes qualification. A managed apply restarts only these authentication and
renderer units; holders and unrelated worker credentials are untouched. Roll back by repinning
the previously verified artifact hashes/configuration and applying the role. Never restore an
old token file or force an old credential reference. Keep the previous artifact directory until
rollback validation is complete; remove obsolete directories afterward rather than archiving them.

Inventory removal fails closed if a previously managed binding disappears. Revoke/drain it in
Relay, confirm the durable cleanup receipt and absence of its local credential, stop and disable
that binding's unit, remove its unit/configuration, reload the user manager, and remove only its
UUID from the private `bindings.json` ownership manifest. Then remove the inventory entry. Keep
the auth service while any binding remains. Full decommission also revokes its dedicated
AppRole/SecretIDs, stops auth and removes the now-unused identity files. Do not remove the
connector's identity or unrelated login material.

## Validation

`scripts/tests/test_relay_router_workers.py` runs the real Ansible role against disposable local
paths. It checks disabled mode, wrong SHA rejection before installation, private ownership,
secret-free output/diffs, a zero-change rerun, offline systemd parsing, matching configuration,
invalid/cross-identity policies, shared/unapproved connector identities and orphan prevention.
`.gitea/workflows/relay-router-workers.yaml` runs that test without credentials, sudo or services.

`scripts/tests/test_relay_router_openbao.py` runs real OpenBao 2.5.5 in TLS dev mode with ephemeral
AppRole credentials and the rendered policy/configuration. It verifies periodic renewal past
the initial TTL, cross-tenant/host and write/list/destroy denials, exact-version rendering even
with a newer KV version, rejection of deleted versions and cleanup without auth/CA availability.
Only the agent sink path is moved from systemd's runtime directory into the disposable fixture.

Run that integration with `BAO_BIN=/absolute/bao`, `RELAY_RENDERER_BIN=/absolute/relay-credential-renderer`
and `TMPDIR` under `/workspace`. It requires Jinja2 and never reads operator tokens, contacts the
estate endpoint or persists a dev root token in the user's token helper. All fixture processes
are terminated and files removed on success/failure. Its cross-repository renderer artifact is
not yet available in AILab CI; a release-pinned combined gate remains part of packaging work.

These checks qualify isolated configuration and protocol behavior, not live worker boot/restart,
AppArmor, deployment trust, operator-provisioned policies, native model traffic or fleet rollout.
