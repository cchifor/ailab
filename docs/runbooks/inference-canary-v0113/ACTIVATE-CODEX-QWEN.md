# Held Codex/Qwen service activation

Prepared from AILab main `3c5590713666d277201aa0d26b2d1ad9bb60323e`.
This is a proposed operator procedure, not approval or evidence of activation.
Keep the activation PR held with `no-automerge` until the operator confirms
the gates below. Merging these opt-in inputs does not run either playbook; no
inventory imports them. This change starts no service and creates no key itself.

**Final-release hold:** the integrated revision2 profiles correct the home layout
and propose bounded Claude/Codex effort choices, but still carry v0.11.3 artifact
pins. The activation overlay now matches their
current byte hash; this is consistency preparation, not permission to start the
old artifacts. Merge the updated layout/release branch, repin the final qualified
artifacts and both staging/activation profile hashes, and verify the operator's
approved final diff before execution. The new effort capability exceeds the
earlier home-layout/artifact-only preapproval and requires explicit owner review
of the final profiles. Follow
[stopped restaging](RESTAGE-RECOVERY.md), including hub/schema-first ordering and
matching-holder readiness. Keep the canary services off through that window.

[AILab #1999 staging evidence](https://git.chifor.me/cchifor/ailab/pulls/1999#issuecomment-142077)
records holder and renderer installation on dev-worker-1 with all managed services
disabled/inactive and no credential files. The existing tmux connector remains
active. The operator subsequently reported #1998's owner-approved router rollout
to `c35fc2225b787a041799cd2c638d129c349d2718d6a090040aca1f84c9181516`.
Refresh those readbacks before activation; this preparation made no host requests.

## Gates before either activation converge

1. Record the owner's explicit approval of the exact `profiles.json` bytes:
   Current revision2 SHA-256 `c96c00bd7c84624d1059d052a4d5a5e011b5e70f82fa7950502f8b3b18281d21`.
   This must be repinned again with the final release. The operator must verify
   the final diff against the owner's approved scope; source PR reviews and
   staging do not substitute for that exact-byte verification.
2. The owner creates only the Codex and Qwen bindings from the existing
   `binding-requests.json`, in their own authorized Relay session, just before
   activation. Required binding IDs are `a0f57684-747f-4c3e-8d90-e684b7262590`
   and `d2871eec-8f5a-4afe-a917-014f0ae8e14d`. Issuance is pending. Do not issue a
   substitute key, change IDs, or issue the Claude request in this batch.
3. The operator approves these concrete service selections and the activation
   window. Confirm scoped Codex/Qwen grants, existing routes, dedicated reader
   validity and trust. Use the final qualified artifacts and the existing SOPS
   controller setup, with fresh readiness readbacks. Do not expose credential
   contents in output or command arguments.

Claude activation is separate from this two-binding procedure. Its route and
Messages capability were reported ready at configuration2522; refresh those
readbacks for its separately approved binding and activation. Do not
enable Messages, create a Claude route/binding, or register/enable its launch
definition as part of this procedure. Worker-budget use remains unqualified;
File B requalification is required before future worker-budget use, not here.

## Renderer activation first

From `/repo/ansible`, first run the following with `--list-hosts` and require
exactly `dev-worker-1`. Execute only after the gates above are recorded:

```sh
ansible-playbook relay-router-workers.yml --limit dev-worker-1 \
  -e @/repo/docs/runbooks/inference-canary-v0113/renderer-inputs.json \
  -e @/repo/docs/runbooks/inference-canary-v0113/renderer-bindings.json \
  -e @/repo/docs/runbooks/inference-canary-v0113/renderer-activate-codex-qwen.json
```

The final extra-vars file intentionally overrides the staging service flag.
Retain all three binding definitions, directories, units and `bindings.json`.
The role rejects removing a staged binding; the new `relay_router_active_binding_ids`
selects services without removing policies. With service management enabled,
unselected renderer units are stopped/disabled; handlers restart only selected
renderers, including after shared binary or CA changes. Without this explicit
selection the compatible default remains all defined bindings.
The role already requires at least one binding in its initial validation task
(`relay_router_bindings | length > 0`), including staging runs with service
management disabled; zero-binding/auth-only convergence was already refused.
An empty selection is refused before any service command; this activation input
is not a bulk-stop control. Managed convergence reloads the user manager once
after staging units, before stop/start operations and restart handlers.

Require `relay-router-auth.service` and only the Codex/Qwen renderer services
active/enabled. Claude unit
`relay-router-render-a8f6211c-d7e2-4ffd-824d-b158e349a8d4.service` must remain
inactive/disabled, with no credential file. Check the two delivered credentials'
ownership/mode and managed delivery/ready receipts without reading tokens into
logs. Stop before holder activation if delivery or readiness is incomplete.
The existing retirement procedure still governs revocation; disabling a service
does not revoke a key or erase credentials/history.

## Holder activation after delivery

Again require `--list-hosts` to show exactly `dev-worker-1`, then use:

```sh
ansible-playbook relay-holders.yml --limit dev-worker-1 \
  -e @/repo/docs/runbooks/inference-canary-v0113/holder-inputs.json \
  -e @/repo/docs/runbooks/inference-canary-v0113/holder-activate-codex-qwen.json
```

The complete three-profile array stays byte-identical. Before startup, the holder
role validates the installed byte pin before any service-start task: its
[configure.yml](../../../ansible/roles/relay_holder/tasks/configure.yml) task
`Check installed profile and connector pins` computes SHA-256, then
`Refuse drift rather than replace profiles or executables` requires
`item.stat.checksum | default('') == item.item.sha`, where the profile's expected SHA is
`relay_holder_profiles_sha256`. This happens before importing `services.yml`;
the overlay is an enforced pin, not only a documentation value. The role also
checks owner, single-link regular-file status and mode `0600`.

Holder startup validates
all three profiles but does not launch inference or require Claude's credential
file; its already-staged private parent directory must exist. This overlay is not
a two-profile holder filter. It starts the holder and its independent reconciliation
client; only the Codex/Qwen launch definitions may subsequently be enabled through
the separately approved hub flow. The coordinated hub/schema upgrade must already
be verified before connecting the upgraded holder; these activation commands do
not change the hub image/configuration. Preserve the existing tmux connector.

Allow at most 120 seconds for execution-user IPC `hello` on the holder socket.
Require the same three profile IDs, `nativeStateReady: true`, zero sessions and
the exact installed profile hash. Check both holder services, managed delivery
and reconciliation without exposing secrets. If any gate fails, stop acceptance,
retain state and receipts, and coordinate recovery with the operator. Do not
overwrite immutable profiles/units, rotate credentials blindly or start Claude.

Service readiness is not inference acceptance. Any Codex/Qwen inference, tool
effects, history, cancellation or rollback exercises need their separate reviewed
acceptance window. No inference is performed by these inputs or this preparation.

## Historical offline validation of revision1 preparation

Before the revision2 integration, the real Ansible renderer-role suite passed 13 tests in a disposable container
with networking disabled, capabilities dropped and a read-only checkout. Service
commands used a strict local fixture, never a host user manager. Coverage includes
two selected renderers becoming active/enabled, the third stopped/disabled with
all policies retained, repeat idempotence, shared-change handlers excluding the
third, and invalid selections rejected before service commands. Eight input and
staging tests also passed, including the then-current exact three-profile byte hash.
These results do not establish owner approval, credential delivery or live readiness.

The combined revision2 integration subsequently passed 12 canary-input,
renderer-staging and profile-home checks plus all 14 renderer-role integration
tests in the cached `ailab-relay-tests:local` image, with network disabled and
source mounted read-only. The temporary fixture mount explicitly allowed execution
of local fake service commands. The activation overlay now matches the staging
profile pin and actual bytes; the final-release hold still applies.
