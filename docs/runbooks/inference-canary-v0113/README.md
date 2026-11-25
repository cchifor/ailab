# Exact dev-worker-1 Pi canary inputs

These are proposed, nonsecret inputs for owner review. Committing them does not
approve profiles, issue credentials or enable an inventory host. The operator
owns route creation, trust configuration, dedicated OpenBao reader provisioning
and the host converge. The owner issues credentials in their own authorized
Relay session and approves the profiles. The inference session owns these pinned
definitions and subsequent paired acceptance. Do not overlap the operator's
router-ab gate with route changes or new inference qualification workloads.

## Fixed scope

Host `dev-worker-1`, connector `b3b1be1a-275c-4a2e-b515-428b39c43171`, execution
user `c4`/UID 1000. Tenant `00000000-0000-4000-8000-000000000001`, router connection
`babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`, workspace communication channel
`b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1` were verified through read-only discovery.

| Profile | Protocol | Dedicated route | Fixed target |
| --- | --- | --- | --- |
| pi-canary-codex | openai-responses | canary-pi-codex | codex-2 / gpt-6-astra |
| pi-canary-qwen | openai-chat | canary-pi-qwen | lan-192.168.0.28-18020 / qwen3.8-27b |
| pi-canary-claude | anthropic-messages | canary-pi-claude | claude / sonnet |

Every route must have exactly one enabled target, no fallback and maxAttempts 1.
Profiles select that alias as `{"kind":"model","model":"<route alias>"}`:
the managed Router advertises `routeSelectors:false`. The same selection appears
in the holder, hub and renderer definitions; no explicit route selector is sent.
The issued key's one-route grant confines the alias at Router admission.
The Qwen target is the one that passed the earlier native session canary.
Each profile has a separate UUID, private home/workspace, 120-second request
deadline, 1024 output-token limit and mandatory Relay communication. Context
limits are 200000 for Codex/Claude and 32768 for Qwen. The grant requests each
permit one route and one access class for 86400 seconds. Credentials are issued
only by Relay's existing router-credential flow; no token is present here.

## Files and review

- `binding-requests.json`: three exact `CreateCredential` request bodies. Relay
  accepts caller-selected binding/operation UUIDs. The owner submits each unchanged
  to `/api/router-credentials` through their own admin `relay_session` cookie and
  matching Origin after its route exists and activation is imminent. The operator's
  JIT identity must not issue credentials. Preserve operation IDs on
  retries; inspect existing state before retrying. Do not substitute direct router
  keys or database inserts. If the operator has already created other bindings,
  revise all affected files and obtain renewed exact-profile approval.
- `profiles.json`: the exact holder profile array for owner approval. Its
  authoritative byte SHA256 is `relay_holder_profiles_sha256` in `holder-inputs.json`.
  It was computed from the UTF-8, LF-only working-tree bytes, which match Git's
  blob bytes. The directory's explicit `.gitattributes` rule preserves LF even
  when a Windows checkout uses autocrlf. Do not reformat the approved file.
- `launch-definitions.json`: matching hub launch definitions, including mandatory
  workspace-channel policy. Approve/register them only through the normal hub flow.
- `renderer-bindings.json`: the same router policies as Ansible extra-vars for
  `relay_router_bindings`. The renderer needs its own approved OpenBao reader;
  these policies do not provision that identity or provide a credential.
- `holder-inputs.json`: exact v0.11.3 image/source, holder/connector hashes and
  independently pinned holder/Pi archives. `relay_holder_enabled: true` selects
  this explicit staging run; `relay_holder_start: false` prevents service startup.
  No fleet inventory includes this file.

The three profile and launch-definition schemas, credential requests and renderer
policies were validated using the compiled contracts in the exact published
v0.11.3 application image, with network disabled and read-only inputs. Cross-file
identities, routes and the profile-file hash match. The Pi manifest bytes were
verified inside the SHA-pinned release archive before extracting entrypoint hashes.
This validates configuration, not live launch or credential delivery.
The same compiled image's `validateProfileReferences` also accepted all three
model-alias selections with one-route binding fixtures and matching host/user/
connection/channel references. This is a local validation of the hub code path;
actual grant confinement at inference remains the Router's key-admission check.
The renderer role now accepts `anthropic-messages` while retaining its exact
nine-key policy contract. Its 11 Linux/Ansible integration tests passed, including
private idempotent Claude staging and ten refusal cases that preserve staged
files and invoke no service commands. These tests use disposable local fixtures;
they do not establish live managed delivery.
Independent offline validation also installed and reverified the complete published
holder/Pi archives and loaded all three exact profiles in the packaged Rust holder
as UID 1000, with networking disabled. IPC `hello` reported `nativeStateReady: true`,
the three expected profile IDs and zero sessions. This proves artifact integrity
and profile admission, not live inference. The Pi installer must execute using
the verified Node 22.23.2 shipped in its archive, pinned as `nodeVersion` beside
`manifestSha256` in the Pi entry of `holder-inputs.json`. The role requires this
version to match the authenticated manifest's `node` field before execution;
future reviewed archives supply their own version pin without changing the role.
The image's default Node 22.23.3 does not satisfy this runtime's verifier.
Stage the renderer first so the private
credential parent directories exist even before credentials are issued.
Run `python3 docs/runbooks/inference-canary-v0113/validate.py` from the checkout
before using any file. CI also runs this check through
`scripts/tests/test_inference_canary_inputs.py`. It checks the exact byte hash,
deep equality of duplicated holder/router objects, and all binding identities,
names and route grants together. Any edit requires this check to pass again.

## Controller layout and staging

Use a Linux Ansible controller with this checkout at `/repo` and verified release
assets at `/evidence/v0.11.3`. Mount the operator's local verified-evidence
directory for v0.11.3 read-only at `/evidence/v0.11.3` when using a container.
The holder role validates its own archive hashes and upstream inventories again.

After the definitions are reviewed and the operator stages the dedicated renderer
with services stopped, run from `/repo/ansible` (holder services also stay stopped):

```sh
ansible-playbook relay-holders.yml --limit dev-worker-1 \
  -e @/repo/docs/runbooks/inference-canary-v0113/holder-inputs.json
```

`--limit dev-worker-1` is mandatory: Ansible extra-vars override inventory, and
this input deliberately sets `relay_holder_enabled: true`. Omitting the limit
would select other hosts in the playbook's `dev_workers` group. Review the target
with the same command plus `--list-hosts` and require exactly `dev-worker-1`
before executing it. Do not use this file with fleet-wide playbooks.

The renderer converge follows its existing playbook and approved secret delivery;
do not place its AppRole secret or a router token in these files or command arguments.
Confirm credential ownership/mode and managed delivery receipts before startup.
Keep the existing tmux connector running. Preserve installed immutable directories,
profiles, receipts and native history on any failure.

## Activation and acceptance

The owner must see and approve these exact definitions before any profile is
enabled. Staging all three does not authorize launching all three immediately.
Claude CLI 2.1.283 is already active in the deployed router, but native Messages
must be enabled with warm retention false and cold acceptance must pass before
issuing the Claude request or launching its profile. Do not submit all three
binding requests in a batch while Claude's prerequisites remain pending. Issue
each short-lived credential only when its acceptance window is ready.
No warm retention is enabled by these inputs.

If a credential expires before acceptance, stop new launches and inspect its
binding through the normal hub flow. A ready, enabled binding without a pending
credential supports `RotateCredential` with its current revision and a fresh
operation UUID; rotation retains the binding ID and paths while advancing the
credential generation. Reconcile renderer delivery and hub readiness before
resuming. Reusing the original CreateCredential operation only retries the original
issuance; it does not renew its expiry. A failed/revoked/pending binding must be
reconciled through its normal retry/revocation flow before rotation is allowed.
Initial issuance is `installing`, not `ready`; staged services alone do not make
it rotatable. The owner should issue late, then start the reviewed renderer for
delivery and confirm readiness before attempting any rotation or Pi launch.
Do not force state changes. If it cannot return to a rotatable state, replacement
requires new binding IDs, revised files/pins and renewed exact-profile approval.

Startup is a separate explicit operator action using the reviewed holder role
after prerequisites pass. Activate the Codex/Qwen profiles first if Claude is
still pending; keep the installed profile array unchanged. Follow the
[canary holder runbook](../relay-pi-canary-holder.md) for service readbacks and
[inference rollout](../relay-inference-sessions-20261124.md) for denial/approval,
one effect, native history, branch/compact, cancellation, reconciliation, restart,
warm measurement and active warm-to-cold rollback. Preserve all durable receipts.
After startup, allow at most 120 seconds for a successful holder IPC `hello` on
its state directory's `control.sock`, as the execution user. Require
`nativeStateReady: true` and exactly the three IDs in `profiles.json`; require zero
sessions before the first acceptance launch. Service-active status alone does not
prove profile admission has finished. If readiness times out, stop acceptance and
inspect the holder journal without replacing its state. This startup allowance is
separate from each profile's inference request deadline.
