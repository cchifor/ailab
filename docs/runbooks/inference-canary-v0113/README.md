# Exact dev-worker-1 Pi canary inputs

These are proposed, nonsecret inputs for owner review. Committing them does not
approve profiles, issue credentials or enable an inventory host. The operator
owns route creation, trust configuration, dedicated OpenBao reader provisioning
and the host converge. The owner issues credentials in their own authorized
Relay session and approves the profiles. The inference session owns these pinned
definitions and subsequent paired acceptance. Do not overlap the operator's
router-ab gate with route changes or new inference qualification workloads.

## Fixed scope

**Deployment hold:** the revision2 definitions now pin published v0.13.0 holder/Pi
artifacts from source `e843921b7cff76406b7da0b5b35cf653c7e458c5` and propose the
bounded Claude/Codex effort control. Publication succeeded in
[run149439](https://git.chifor.me/cchifor/relay/actions/runs/149439), attempt2,
job781668; see the [deployment receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.13.0/deployment-images-149439.json).
The candidate profile SHA256 is `665e380e584bd2a1886291d82adfed667419e34d4d8ded0e912b9c3f4b79c42e`.
Exact published-runtime root/profile proof passed offline; all five published-Pi wire/replay cases passed offline (zero skipped). Keep execution held for operator hub/schema93
rollout and stopped restaging, operator verification of the covered final diff, late managed
bindings and readiness. Publication is not deployment or live acceptance.
The current hub remains v0.12.1/source `dfc739a492217ea5a8dfaf2471aead43b2ea646c`;
keep canary services off through the coordinated upgrade. Earlier revision1 and
layout/pins-only preapproval did not cover effort. Operator bus1861 relays the
owner's 15:30 decision **Covered**, extending that approval to Claude/Codex
`off/low/high`. Before apply, the operator must verify the complete diff from
original profile SHA256 `c1c937fca19a95e46fd0178a37ab973237b1af48549bec18959b3200f5c93809`
to the final exact bytes contains **only home-layout changes, artifact pins and
those Claude/Codex choices** (with matching administrative revision metadata).
Record that verification and readbacks; this is existing conditional approval,
not a requirement to ask again for the same covered scope. Other changes are
not covered. No routes, grants, identities, output/time budgets or Qwen effort
capability may be added under this decision.

Published candidate pins (the JSON inputs remain authoritative):

| Item | Exact value |
| --- | --- |
| Application/installer image | `registry.chifor.me/relay/control-plane@sha256:021a8fbed305c338dc46022abc992f3e0b2c538c6c491fd2aa9f0b279dbccdd8` |
| Control-plane manifest SHA256 | `4a6f09dd297cf2ee3c1ad249348886ff09ed7e0fbc0ef279d08d22b8c0d6df95` |
| Holder archive SHA256 | `3ee727c19dcaf5221510e3b22c88dcde50d1b917d7ccba19fd1296d7041c4a70` |
| Holder manifest SHA256 | `ec9ee0fc8a525e758408c5398997180d5d1379eb771b31487de1030653935369` |
| Holder binary SHA256 | `f70a471bb00b3f61675768666802e9b3ac9ea02b59dbf554191c198bd88d7909` |
| Pi archive SHA256 | `b12de9f54aeab0704b71d1fdde97c663faedc96e4791ac12151c8d9d00a7669d` |
| Pi manifest SHA256 | `7eafd53ce7a67e38432d8163f17dfe586ad7c6a571d1f18a0b893a087e523d6f` |

Offline proof used this exact published image, holder/Pi archives and candidate
profile bytes at the actual state/home paths as UID1000. Both installer modes
passed; hello reported `nativeRecovery:2`, `nativeStateReady:true`, all three
profiles, zero sessions and zero children, with no credentials. The private lock
inode was retained. The old connector hash was verified without executing it.
Compiled-image profile/control schema checks passed 31 positive and five negative
cases. These results establish offline installation/profile readiness and schema
validation, not owner authority, managed delivery, live inference or recovery.

Published-Pi wire/replay qualification passed **5/5, zero skipped, in 63.10s**
on 2026-11-25. It used the published Pi manifest above and packaged Node22.23.2
(SHA256 `3517c2df0b2f8cd7f422b4b8450ef81c6889f08eb03e281d6de9079b15e6a327`),
against exact router TypeScript source `b60fb03b23ce52548f49513d45466f4670f5a369`
under Node24.22.2 with synthetic upstreams and external networking disabled.
Source/runtime mounts were read-only; neither runtime nor test source was rebuilt
or modified. Controller evidence in the Relay workspace is
`.data/inference-release-evidence/published-pi-wire-e843-20261125-r4/`:
`qualification-summary.json`, `wire-provenance.json` and `vitest-results.json`.
Installer/repeat evidence remains in the sibling `published-pi-wire-e843-20261125-r2/`;
earlier driver/bootstrap failures are preserved and distinguished in the summary.
This proves published Pi/ACP/policy/tools behavior against that router source,
**not the published router OCI/runtime, real subscription CLI, live wire, or
holder/hub lifecycle**. Final outbound effort/cap fields and opaque bytes remain
unproven live; retain the operator-selected paired evidence limitation.

Claude and Codex now propose exactly
`settings:{modes:[],config:{thought_level:["off","low","high"]}}`.
`off` means provider default; it does not disable provider reasoning. Qwen has no
effort capability. This addition exceeded the earlier layout/pins-only
preapproval; bus1861's Covered decision includes it subject to the exact-diff
verification above.
It requires the release containing PR680 and the router containing PR201; the
historical v0.11.3 qualification below did not exercise these settings. Keep
the existing request/output limits, grants and route policies unchanged.

Profile `credentialHome` is now `state/profile-homes/<profileId>`; workspaces stay
at `state/workspaces/<profileId>`. Reserve `state/native-homes` exclusively for
holder-created session UUID homes. The holder intentionally reports
`nativeStateReady:false` whenever that directory is nonempty at startup. The old
revision1 layout precreated three profile directories there and triggered that
fence even without a session. The role now rejects that layout before creating
profile directories; it never removes or migrates existing native state.

An existing staged host needs a separate operator-reviewed disposition of its
old directories and exact installed profile bytes, with services stopped and
ownership/emptiness evidence preserved. The role's immutable-file check refuses
an old installed profile hash; it does not silently replace it. Do not clear a
native-state fence or discard histories to make startup pass. Directory emptiness
alone is not proof of process containment. Verify final artifact pins and the
layout plan against the existing covered scope before execution.
The bounded [stopped-stage restaging procedure](RESTAGE-RECOVERY.md) preserves
the old profile directories as evidence and does not start services; it requires
its own final review before operator execution.

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
- `holder-inputs.json`: published v0.13.0 image/source and holder/Pi hashes,
  the unchanged v0.11.3 connector pin, and
  independently pinned holder/Pi archives. `relay_holder_enabled: true` selects
  this explicit staging run; `relay_holder_start: false` prevents service startup.
  No fleet inventory includes this file.

The earlier revision1 profile and launch-definition schemas, credential requests and renderer
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
Earlier independent offline validation installed and reverified the complete published
holder/Pi archives and loaded all three exact profiles in the packaged Rust holder
as UID 1000, with networking disabled, but used a separate qualification state
directory rather than the role's actual state root. IPC `hello` reported `nativeStateReady: true`,
the three expected profile IDs and zero sessions. This proves artifact integrity
and profile admission, not correctness of the old staged home layout or live inference.
A subsequent offline A/B check used the exact v0.11.3 image and verified archives,
the actual `/home/c4/.local/state/relay-holder` state root, and UID1000. With the
revision1 profile directories under `native-homes`, IPC hello reported
`nativeStateReady:false`; the revision2 `profile-homes` layout reported
`nativeStateReady:true` with an empty native root. Both loaded all three profiles,
with zero sessions, no holder children and no credentials. This is a layout
regression check, not evidence for the published v0.13.0 artifacts or a live rollout.
The Pi installer must execute using
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
assets under a common read-only `/evidence` mount when using a container.
Include `/evidence/v0.13.0` for the new holder/Pi archives, `/evidence/v0.11.3`
for the unchanged connector and credential renderer, and `/evidence/openbao-2.5.5`
for Bao. Mounting only the new release directory hides required old paths.
Do not repin the connector or renderer merely to make controller paths resolve.
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

## Renderer staging (operator)

The dedicated reader is live: policy and AppRole `relay-router-reader-dev-worker-1` (ailab #1988, created by
the provision Job 2026-11-24 17:23Z). Its SecretID was issued 2026-11-25 09:17:28Z inside `openbao-0` (the
breakglass token on stdin, nothing in argv) and piped straight into `ansible/secrets/relay-router.sops.yaml`
(`relay_router_credentials.dev-worker-1`, both leaves `ENC[`, decrypt round-trip checked). Accessor
`2600514a-4ab8-b2d7-f3e0-c5d0eb73ae6f`, TTL 720h: it **expires 2026-12-25 09:17Z**. Issue a replacement
before then. A reboot or auth-service restart after expiry cannot log in. Verified at issue with a token from
that login, `bao token capabilities` only: `read` on its own host's escrow data; `deny` on another host,
another tenant, metadata, list, destroy, the signer and trust documents and the escrow token role. The token
was periodic 72h with `default` + the reader policy, and was revoked afterwards.

`renderer-inputs.json` holds the non-secret staging vars, with `relay_router_manage_services: false`.
Evidence layout on the controller (all verified before use):
- `/evidence/v0.11.3/holder/relay-credential-renderer-linux-amd64`: extracted from the release's holder
  archive (`670f510d…`, recorded in `control-plane-manifest.json`), checked against the archive's own
  `SHA256SUMS`, SHA-256 `ff328ccc456cd8bf92443b5f0548c74d1c32b1d70d7c02a7669e2ba22754c04d`;
- `/evidence/openbao-2.5.5/bao`: from the upstream `bao_2.5.5_Linux_x86_64.tar.gz` (archive `2c557770…`),
  binary `4e7e04cc…`, both the pins in relay-router-workers.md.

Run from `/repo/ansible`, after `--list-hosts` shows exactly `dev-worker-1`:

```sh
ansible-playbook relay-router-workers.yml --limit dev-worker-1 \
  -e @/repo/docs/runbooks/inference-canary-v0113/renderer-inputs.json \
  -e @/repo/docs/runbooks/inference-canary-v0113/renderer-bindings.json
```

## Activation and acceptance

The held [Codex/Qwen-only activation procedure](ACTIVATE-CODEX-QWEN.md) supplies
separate service-start overlays while retaining this exact three-profile array
and all three renderer policies. Use its explicit two-binding service selection;
the renderer role's compatible default starts every defined binding.
Its original revision1 hash and v0.11.3 staging references are historical inputs,
not approval to activate the revision2 definitions. The deployment hold above
also applies to that workflow. Before publishing the final activation change,
verify the integrated published pins and activation procedure's exact-profile
references against the final reviewed bytes and record the completed offline qualification and remaining live gates.

Use [the paired live acceptance procedure](LIVE-ACCEPTANCE.md) for exact normal-hub
request bodies, bounded prompts, permission decisions and marker evidence. It
records the deployed Pi recovery limitation separately from the runnable checks;
staging or a passing marker observation does not establish full acceptance.

Use the separate [bounded effort acceptance procedure](EFFORT-ACCEPTANCE.md) for
fresh Claude/Codex sessions after qualified releases and operator verification
of the conditionally covered settings/profile diff. Its four-prompt budget is separate from lifecycle
acceptance; it changes no profile pins here and excludes Qwen.

Use [bounded warm acceptance and rollback](WARM-ACCEPTANCE.md) only after cold
native acceptance, in an operator-selected exclusive Claude window. It separates
process reuse, idle expiry, permission-boundary release, capacity and active
warm-to-cold rollback evidence; the settings affect all router-managed Claude
accounts and are not enabled by these inputs.

Operator bus1839 selects normal live receipts/attempt metadata paired with
passing real pinned-binary synthetic wire/replay qualification, as specified in
[LIVE-ACCEPTANCE.md](LIVE-ACCEPTANCE.md) and
[EFFORT-ACCEPTANCE.md](EFFORT-ACCEPTANCE.md). No new live capture or live opaque
observer run is required by this method. Final outbound effort/cap fields and
opaque bytes remain **unproven live**; record synthetic and live results separately.
This choice does not claim acceptance or relax any budget, control or release gate.

The read-only [opaque history observer](opaque-history.py) remains an offline
utility, not a step in the selected live evidence method. It compares selected
opaque-field hashes between a retained source and its exact imported prefix.
Supply source/replacement Relay IDs and the native UUID from completed hub
receipts, run as UID1000 while quiescent, and keep sanitized output outside holder
state. It refuses unsafe or inconsistent files and reports `NOT_EXERCISED` when
the required opaque blocks are absent. `PRESERVED` proves local import equality;
it explicitly does not prove that a provider received those blocks. Its fourteen
offline tests use only private synthetic fixtures.

The operator must verify these exact definitions against the owner's Covered
decision before any profile is enabled. Staging all three does not authorize
launching all three immediately.
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
