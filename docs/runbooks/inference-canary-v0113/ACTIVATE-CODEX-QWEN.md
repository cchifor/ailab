# Held Codex/Qwen service activation

Successor preparation from AILab main `b1c98cccc222db4e6d35d5cd9356871c40c9029a`.
This is a proposed operator procedure, not approval or evidence of activation.
Keep the activation PR held with `no-automerge` until the operator confirms
the gates below. Merging these opt-in inputs does not run either playbook; no
inventory imports them. This change starts no service and creates no key itself.

**Successor hold:** reviewed holder/Pi v0.13.1 source
`2604c41b69c5d9c50a3324545a51f0bfa38435a0` replaces the historical v0.13.0 runtime.
Published run150672 pins and revision3 profile bytes are recorded in
[README.md](README.md); exact-artifact root/profile and deployed-hub schema
qualification passed offline, as did all five published-Pi wire/replay cases
against Router4af66b8 with synthetic upstreams. Actual upgrade/activation
readbacks and live acceptance remain pending. Historical revision2/publication149439 proofs
do not qualify that replacement. The hub stays at
e843/schema93; the required Router correction source is 4af66b8. Verify actual
Router deployment separately. Complete qualification and verify the approved final diff before
execution. The effort capability exceeded the earlier home-layout/artifact-only
preapproval; operator bus1861 now records the owner's 15:30 **Covered** decision
for home layout, pins and Claude/Codex `off/low/high` only. Verify the full
original-c1c937fc-to-final diff under [that decision](README.md); no new request
for the same covered scope is needed. Follow
[the sealed idle upgrade](UPGRADE-IDLE.md) for this already-started,
never-launched holder. Keep both holder services stopped during replacement;
preserve the existing renderer selections, journal, homes and credentials.

Historical [AILab #1999 staging evidence](https://git.chifor.me/cchifor/ailab/pulls/1999#issuecomment-142077)
records holder and renderer installation on dev-worker-1 with all managed services
disabled/inactive and no credential files. The existing tmux connector remains
active. The operator subsequently reported #1998's owner-approved router rollout
to `c35fc2225b787a041799cd2c638d129c349d2718d6a090040aca1f84c9181516`.
Later operator bus1945/1967 reports the holder started and idle with no launches.
Refresh readiness before the successor upgrade; this preparation made no host requests.

## First delivery is scheduled by a launch

Operator bus1928 reports that the owner issued both Codex/Qwen bindings at
2026-11-25 21:27:37 UTC and the selected renderer services converged, but activation
stopped because this procedure required delivered credentials before holder
startup. That ordering was circular. This is an operator report, not evidence
that the holder started or any inference ran.

At published Relay source `e843921b7cff76406b7da0b5b35cf653c7e458c5`,
[`offerRouterRendering`](https://git.chifor.me/cchifor/relay/src/commit/e843921b7cff76406b7da0b5b35cf653c7e458c5/server/agent-sessions/router-rendering.ts)
offers a render task only for an **offered launch** with a pending, unrendered
installation. Issuing a binding and starting its renderer do not create that
launch. With no eligible launch, `task:null` and an absent credential file are
expected; they do not prove a delivery failure. Cleanup of a retired credential
is a separate task and cannot deliver an initial credential.

The normal order is renderer services, holder startup and an empty-session hello,
then the owner's normal launch. That launch waits for rendering, installs the
exact managed reference and only then opens the native session. Verify the file
metadata, rendering/install receipts and binding readiness **before the first
prompt**. Do not pre-render a token manually, forge a receipt, force a binding to
`ready`, or use a substitute key to satisfy the former gate.

## Gates before either activation converge

1. Record the existing Covered decision and operator verification of the exact `profiles.json` diff:
   The successor revision3 SHA-256 is `58120839759fdff7c8bcd4ef483c75711558613c1487366d8406de4bceaa3b39`;
   the historical revision2 baseline is `665e380e584bd2a1886291d82adfed667419e34d4d8ded0e912b9c3f4b79c42e`. The operator must verify
   the final diff against the owner's approved scope; source PR reviews and
   staging do not substitute for that exact-byte verification.
2. The owner creates only the Codex and Qwen bindings from the existing
   `binding-requests.json`, in their own authorized Relay session, just before
   activation. Required binding IDs are `a0f57684-747f-4c3e-8d90-e684b7262590`
   and `d2871eec-8f5a-4afe-a917-014f0ae8e14d`. For the reported 21:27:37 UTC
   issuance, inspect the existing operation/binding and expiry rather than
   issuing again. Do not issue a substitute key, change IDs, or issue the Claude
   request in this batch.
3. The operator approves these concrete service selections and the activation
   window. Confirm scoped Codex/Qwen grants, existing routes, dedicated reader
   validity and trust. Use the final qualified artifacts and the existing SOPS
   controller setup, with fresh readiness readbacks. Do not expose credential
   contents in output or command arguments.
   Preserve the owner decision governing startup and same-UID exposure on
   dev-worker-1; the subsequently reported idle startup is not approval of the
   successor inputs. Private modes protect against other UIDs, not other
   processes running as `c4`/UID1000.

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
ownership/mode and managed delivery/ready receipts at the first launch, as
described below, without reading tokens into logs. Before any launch, require
renderer authentication/polling and the approved binding/reference scope;
an absent credential file and `installing` binding alone do not block holder
startup. A failed, revoked, expired or mismatched binding still blocks acceptance.
The existing retirement procedure still governs revocation; disabling a service
does not revoke a key or erase credentials/history.

## Holder activation before the first launch

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
all three profiles but does not launch inference or require any of the three
managed credential files; their already-staged private parent directories must
exist. [`inference.rs::validate`](https://git.chifor.me/cchifor/relay/src/commit/2604c41b69c5d9c50a3324545a51f0bfa38435a0/holder/src/inference.rs)
explicitly permits a file not yet produced by the renderer at profile-admission
time. File validation still occurs during the later managed installation. This overlay is not
a two-profile holder filter. It starts the holder and its independent reconciliation
client; only the Codex/Qwen launch definitions may subsequently be enabled through
the separately approved hub flow. The coordinated hub/schema upgrade must already
be verified before connecting the upgraded holder; these activation commands do
not change the hub image/configuration. Preserve the existing tmux connector.

Allow at most 120 seconds for execution-user IPC `hello` on the holder socket.
Require the same three profile IDs, `nativeRecovery: 2`, `nativeStateReady: true`,
zero sessions and the exact installed profile hash. Check both holder services
and reconciliation without exposing secrets. This hello precedes the first
launch; it does not assert that either managed credential has been delivered.
The successor 2604c41 holder's
[`hello` builder](https://git.chifor.me/cchifor/relay/src/commit/2604c41b69c5d9c50a3324545a51f0bfa38435a0/holder/src/runtime.rs)
advertises `nativeRecovery: 2`. This capability is required for this canary's v2
owned-recovery checks; `nativeStateReady` separately reports current state safety.
For the sealed idle upgrade, verify the preserved pause/seal and original journal,
then explicitly release the seal and resume as [UPGRADE-IDLE.md](UPGRADE-IDLE.md)
requires. A fresh holder incarnation is expected; a readiness failure must not
be cleared by deleting state. The operator can then hand off readiness for the
first launch. Delivery evidence follows that launch, so a combined pre-launch handoff
requiring both credential files would recreate the circular gate.
If any gate fails, stop acceptance,
retain state and receipts, and coordinate recovery with the operator. Do not
overwrite immutable profiles/units, rotate credentials blindly or start Claude.

Service readiness is not inference acceptance. Any Codex/Qwen inference, tool
effects, history, cancellation or rollback exercises need their separate reviewed
acceptance window. No inference is performed by these inputs or this preparation.

## First launch, delivery and the first-prompt gate

After the owner decision and successful holder hello, the owner registers/enables
only the already approved Codex/Qwen launch definitions and requests the first
normal launch through [LIVE-ACCEPTANCE.md](LIVE-ACCEPTANCE.md). Use one operation
ID per logical launch; an uncertain response requires inspecting that operation,
not creating a replacement launch. Pending launch expiry is bounded to two
minutes, so have the holder, connector and renderer polling ready first. Do not
extend that deadline or send a prompt yet.

Confirm that the selected renderer units use the approved
[`--watch` service template](../../../ansible/roles/relay_router_renderer/templates/renderer.service.j2)
and are polling successfully before launching. The qualified renderer's
[`watch` loop](https://git.chifor.me/cchifor/relay/src/commit/e843921b7cff76406b7da0b5b35cf653c7e458c5/holder/src/bin/relay-credential-renderer.rs)
polls immediately, completes the poll, then sleeps two seconds; the retained
connector's [agent-control loop](https://git.chifor.me/cchifor/relay/src/commit/e843921b7cff76406b7da0b5b35cf653c7e458c5/connector/src/agent_control.rs)
also sleeps two seconds after a completed cycle. The source/input equivalence
was checked when retaining their existing pins; this does not request an upgrade.
These are delays between cycles, not delivery deadlines. HTTP/IPC operations
have their own ten-second bounds, and sequential work and scheduling add time.
`RestartSec=2s` in the unit is a process-restart delay, not its poll interval.
Require render/install/Open completion before the **actual** launch expiry,
which can be shorter than two minutes when admission authority expires sooner.
Slow or failing polling is a reason to stop and inspect, not to widen expiry or
start a retry loop.

For each selected binding, the normal
[`launch-transport`](https://git.chifor.me/cchifor/relay/src/commit/e843921b7cff76406b7da0b5b35cf653c7e458c5/server/agent-sessions/launch-transport.ts)
sequence is `await-router-render` → `install-router` → `open`. The connector sends
no holder command for `await-router-render`. A successful renderer receipt alone
does not establish holder installation: the validated install receipt calls
`manager.installed`, which establishes managed binding readiness, before Open.

Within the existing launch and acceptance deadlines, require all of the following
before that session's first prompt:

- The exact credential generation/reference matches the approved binding,
  connection, route, host and execution identity; it is unexpired and not revoked.
- The renderer's successful receipt and the corresponding holder installation
  receipt are recorded; the binding is ready. Check the delivered file's UID,
  private mode and normal path metadata without printing its token.
- The normal launch completes with the expected profile revision, native UUID
  and v2 receipt. Save the launch evidence immediately, as the live procedure
  requires, and acquire the new session's normal control lease.
- The operator supplies that provider's delivery/readiness and nonsecret
  credential-scope handoff before its first acceptance prompt. Obtain the fresh
  marker baseline immediately before the launch as the live procedure requires
  and retain that report. After Open, run the `baseline` observation again with
  the **same run UUID and observed marker filenames**; both marker files must
  still be absent, matching the retained pre-launch report. The observer prints
  JSON to stdout and does not choose or write an evidence file. Capture its two
  outputs separately as `PROVIDER-RUN_UUID-pre-launch.json` and
  `PROVIDER-RUN_UUID-post-open.json` in the operator's evidence directory, refusing
  an existing destination; never redirect both runs to the same report path.
  Require exit0 and `phaseChecksPass:true` for both, with the same provider,
  profile, run UUID and marker commands; only observation timestamps may differ.
  Follow the same check in [live precondition6](LIVE-ACCEPTANCE.md#preconditions-and-limits).
  A marker appearing
  during launch is a failure: do not absorb it into a replacement baseline,
  delete it or select a new run UUID to hide it. No launch step is allowed to
  write either marker.

Keep the existing Codex-then-Qwen acceptance sequence and check each provider at
its own first launch. Do not require Qwen delivery before the first Codex launch
or consume an extra probe launch just to manufacture a ready state. The first
handoff proves holder startup; each later delivery handoff permits only the
corresponding provider's first prompt. If rendering, installation or Open fails
or expires, stop acceptance,
retain the operation/receipts and use the normal reconciliation/cleanup flow.
Do not bypass a failed gate, rotate blindly or infer readiness from an active
service or HTTP202. These launch steps do not authorize inference before the
operator's handoff and do not change the reviewed prompt/generation budgets.

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
