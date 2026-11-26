# Exact dev-worker-1 Pi canary inputs

These are proposed, nonsecret inputs for owner review. Committing them does not
approve profiles, issue credentials or enable an inventory host. The operator
owns route creation, trust configuration, dedicated OpenBao reader provisioning
and the host converge. The owner issues credentials in their own authorized
Relay session and approves the profiles. The inference session owns these pinned
definitions and subsequent paired acceptance. Do not overlap the operator's
router-ab gate with route changes or new inference qualification workloads.

## Fixed scope

**First-registration hold (2026-11-26):** holder/Pi v0.13.1 source
`2604c41b69c5d9c50a3324545a51f0bfa38435a0` is reviewed and merged in
[Relay PR705](https://git.chifor.me/cchifor/relay/pulls/705). It contains the
permission-review and structured configuration-refusal corrections. Publication
completed in [run150672](https://git.chifor.me/cchifor/relay/actions/runs/150672).
The checked-in profiles now use **first hub approval revision1**, SHA256
`928d41f0b24c0d1d2493458e7fac23cfd686c045b1aa9daaee480684a0aea9a4`. Published runtime artifacts are unchanged.
The [operator's PR2022 upgrade readback at14:20:17 UTC](https://git.chifor.me/cchifor/ailab/pulls/2022#issuecomment-144744)
and a separate independent SSH readback at14:22:37 UTC confirmed installed
revision3 bytes `58120839759fdff7c8bcd4ef483c75711558613c1487366d8406de4bceaa3b39`,
the published holder, recovery2/readiness, resumed admission and zero sessions.
That is an installation result, not a hub profile approval. A tenant-scoped
14:26 UTC read found no approval rows for any of the three exact profile IDs.

The deployed hub [requires the first revision to be1](https://git.chifor.me/cchifor/relay/src/commit/e843921b7cff76406b7da0b5b35cf653c7e458c5/server/agent-sessions/profiles.ts#L240);
the holder and session revisions must match, and revision participates in the
installed holder digest. These prepared revision numbers never created approval
history. This correction changes nine revision values from3 to1 and the two
matching file-hash references, across four JSON files. It does not replace an
approved revision or create placeholder approvals. Runtime pins, IDs, bindings,
scopes, settings and budgets stay unchanged.

Before the new configuration replacement, refresh the existing authorized
tenant-scoped readback for these same three profile IDs, including revoked
revisions. The 14:26 snapshot is not continuing proof of absence. The prepared
operator helper is `.data/observe-pi-profile-approvals-e843-20261126.mjs` in the
Relay workspace; it uses the existing read-only tenant context, not a new API.
`GET /api/agent-profiles` is capped at200: absence from a full list cannot prove
that an ID has no history. If any revision now exists, stop and derive the next
revision from that actual history; never downgrade or invent approval history.
Also recheck the never-launched journal and stopped-idle eligibility below.

Exact revision1 root/profile and deployed-hub schema qualification passed,
along with nine compiled approval-boundary cases described below. The second
live sealed-idle configuration replacement, first approval readbacks and live
acceptance remain pending. Prior revision3 root/schema proof and the unchanged
runtime's five synthetic wire cases remain evidence for their stated scope;
none is live acceptance or authority to launch.

The deployed hub remains v0.13.0/source
`e843921b7cff76406b7da0b5b35cf653c7e458c5`, with the
[schema93 rollout recorded](https://git.chifor.me/cchifor/ailab/pulls/2009#issuecomment-142980).
The required Router correction is merged source
`4af66b8c7c3cc83ddd10e326c554ad728caad72e`
([Router PR203](https://git.chifor.me/cchifor/llm-router/pulls/203)); record its
actual deployment/readiness separately. Updating the holder installer image
does not imply another hub rollout.

The canary remains started but never launched in the reported readback. Before
replacing the installed revision3 configuration, prove eligibility through the
complete journal and use [the sealed idle upgrade](UPGRADE-IDLE.md) with a fresh
preparation operation and current pause/seal. Preserve the first upgrade backup. The
never-started restaging helper is no longer applicable. Publication, offline
qualification, exact successor approvals, preserved-seal activation, managed
readiness and live acceptance remain distinct steps. No inference is authorized
by this documentation refresh.

Historical v0.13.0 publication succeeded in
[run149439](https://git.chifor.me/cchifor/relay/actions/runs/149439), attempt2,
job781668; see the [deployment receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.13.0/deployment-images-149439.json).
The historical revision2 profile SHA256 is `665e380e584bd2a1886291d82adfed667419e34d4d8ded0e912b9c3f4b79c42e`.
Its published-runtime root/profile proof and five published-Pi wire/replay cases
passed offline. Those results do not qualify the replacement artifacts.
The earlier prepared v0.11.3 revision1 and
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

Published v0.13.1 successor pins (JSON inputs remain authoritative):

| Item | Exact value |
| --- | --- |
| Holder installer image; hub unchanged | `registry.chifor.me/relay/control-plane@sha256:f2eacdda4614f4cadf51faa68291b4af2249f9231fcdf38afe16a53bee064dbe` |
| Control-plane manifest SHA256 | `d4d353a8b409b28593339b9de2773939328b49b1aa2fd307d65acb0af463ea9f` |
| Holder archive SHA256 | `e502c526c5dc0cfa9d033831e5e6e4fcf9b2f9692ff7db5c5c098831f80e4f2c` |
| Holder manifest SHA256 | `b970fba0e4eaa4efdf5bf9d548137ccde020bec06e1c542271aaed4b4c64ea39` |
| Holder binary SHA256 | `0b1a7907f6da2ef6b5794621d87c55c2e0ee6d5aeb1c4e0fdf35c9031a127003` |
| Pi archive SHA256 | `096ea187814a4332898751ae4e5dbfaac2455369624d30d6e9a2190c8f7df4cd` |
| Pi manifest SHA256 | `d0bf7f9d44a2ae992bb1fbd87f7f2043c7d582ad563078bd13d713ebcd211a0d` |

The [v0.13.1 deployment receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.13.1/deployment-images-150672.json)
has SHA256 `96efee03a390918917ecd5d88c008e3df6c96c6276870e93e1ed7d6c3ff6d318`.
It binds source2604c41, the installer image and control-plane manifest above.
Archive bytes, inner manifests and selected runtime entries were independently
checked against those published hashes. The historical revision3 candidate report SHA256 is
`2901990c87d782de1f7e5583a97944505c3aec4037e8048a6e8f36776c287b2f`.
Comparison with the eight frozen revision2 Git blobs found exactly four changed
files and 50 reported JSON-pointer changes: artifact/runtime pins and matching
revision2-to-3 metadata only. The four renderer/binding files remain byte-identical;
connector/renderer pins, identities, routes, grants, limits and effort choices
remain unchanged. Source digests are authenticated through the receipt-bound
manifest, not independently recomputed from the build closure. This comparison
does not itself grant profile approval.

The corrected revision1 profile SHA256 `928d41f0b24c0d1d2493458e7fac23cfd686c045b1aa9daaee480684a0aea9a4`
passed a fresh offline actual-root qualification using the unchanged published
v0.13.1 holder/Pi artifacts. Hello admitted all three profiles with
`nativeRecovery:2`, `nativeStateReady:true` and zero sessions. The deployed e843
image's compiled schemas passed **31 positive and five negative cases**, and
normalized definition hashes were computed with its actual approval defaults.
Evidence is `.data/pi-v0131-first-registration-root-20261126/` in the Relay
workspace: `qualification-summary.json`, `actual-root/hello-corrected.json`,
`hub-schema.log` and `normalized-definition-hashes.json`.

The deployed image's compiled approval function also passed **nine boundary
cases**: each profile's fixtures accept revision1 without prior history, refuse
revision3 without prior history, and refuse revision1 when revision3 exists. Evidence is
`.data/pi-v0131-first-registration-boundary-20261126/result.json`. These use a
strict fake database and synthetic caller authority/state, not HTTP requests or
a real database transaction. They do not establish current approval history,
live authority or an actual approval. Keep the fresh scoped history readback,
second configuration replacement and normal owner approval steps above.

The earlier v0.13.1 root qualification used these exact archives and revision3 profiles,
UID1000 and the actual `/home/c4/.local/state/relay-holder` paths in an isolated
offline container. Installation and repeat verification passed using packaged
Node22.23.2. Holder hello reported `nativeRecovery:2`, `nativeStateReady:true`,
all three profiles, zero sessions/children and empty native homes; shutdown
exited0. The unchanged e843 hub image's compiled schemas passed **31 positive
and five negative cases** against those revision3 inputs. Approval-definition
hashes were computed after the deployed `ApproveLaunchProfile` defaults, not
from unnormalized input JSON. Evidence in the Relay workspace is
`.data/pi-v0131-root-qualification-20261126/`: `qualification-summary.json`,
`actual-root/hello-corrected.json`, `hub-schema.log` and
`normalized-definition-hashes.json`. The retained connector was hash-checked,
not executed. This proves offline installation/profile admission and schema
compatibility, not a real systemd upgrade, live readiness or provider acceptance.
Published v0.13.1 Pi wire/replay qualification passed **5/5, zero skipped, in
74.05s** on 2026-11-26. It used the exact Pi manifest/archive above, packaged
Node22.23.2 (SHA256 `3517c2df0b2f8cd7f422b4b8450ef81c6889f08eb03e281d6de9079b15e6a327`),
and Router TypeScript source `4af66b8c7c3cc83ddd10e326c554ad728caad72e` under
Node24.22.2 with synthetic upstreams and external networking disabled. Source
and runtime mounts were read-only; runtime and test source were not rebuilt or
modified. Evidence is `.data/published-pi-wire-v0131-20261126/qualification-evidence.json`
and its `r1/` summary, provenance and Vitest results. Installation/repeat evidence
is `.data/published-pi-install-v0131-20261126/published-pi-installation.json`.
This qualifies published Pi/ACP/policy/tools against that Router source;
it does **not** exercise the published Router image, real subscription CLI,
holder/hub lifecycle or live upstream payloads. Preserve those limits alongside
the still-required live receipts and attempt metadata.

The historical v0.13.0 offline proof used its e843/021a8fbed installer,
holder/Pi archives and revision2 profile bytes at the actual state/home paths as UID1000. Both installer modes
passed; hello reported `nativeRecovery:2`, `nativeStateReady:true`, all three
profiles, zero sessions and zero children, with no credentials. The private lock
inode was retained. The old connector hash was verified without executing it.
Compiled-image profile/control schema checks passed 31 positive and five negative
cases. These results establish offline installation/profile readiness and schema
validation, not owner authority, managed delivery, live inference or recovery.

Historical published-Pi wire/replay qualification passed **5/5, zero skipped, in 63.10s**
on 2026-11-25. It used v0.13.0 Pi manifest
`7eafd53ce7a67e38432d8163f17dfe586ad7c6a571d1f18a0b893a087e523d6f` and packaged Node22.23.2
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
The [stopped-stage restaging procedure](RESTAGE-RECOVERY.md) applies only before
any holder startup and requires no journal. The current started, never-launched
canary instead requires [UPGRADE-IDLE.md](UPGRADE-IDLE.md): normal pause/seal,
both holder services stopped, exact old-file/journal proof, four-file backup,
normal role with `start:false`, then explicit activation and seal release.

Host `dev-worker-1`, connector `b3b1be1a-275c-4a2e-b515-428b39c43171`, execution
user `c4`/UID 1000. Tenant `00000000-0000-4000-8000-000000000001`, router connection
`babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`, workspace communication channel
`b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1` were verified through read-only discovery.

| Profile | Protocol | Dedicated route | Fixed target |
| --- | --- | --- | --- |
| pi-canary-codex | openai-responses | canary-pi-codex | codex-3 / gpt-6-astra |
| pi-canary-qwen | openai-chat | canary-pi-qwen | lan-192.168.0.28-18020 / qwen3.8-27b |
| pi-canary-claude | anthropic-messages | canary-pi-claude | claude / sonnet |

[Operator handoff 1945](https://git.chifor.me/cchifor/ailab/pulls/2016#issuecomment-143858)
records the owner's approval to move the Codex target from
`codex-2` to `codex-3` at Router configuration revision 2803, with profiles,
bindings and keys unchanged. An independent
[read-only metadata check](https://git.chifor.me/cchifor/ailab/pulls/2016#issuecomment-143840) at
**2026-11-26 10:45:38 UTC** observed configuration **2882** and confirmed the table's Codex target was the
only enabled target, with `maxAttempts:1` and no fallback. The running Router
reported source `b60fb03b23ce52548f49513d45466f4670f5a369` and image
`registry.chifor.me/llm-router/router@sha256:0d4fa1318f2c03d192a28f6d5eee8fca6b8e62b5ce3d9c3e770d21f42d8050a6`;
Deployment/Pod identity remained stable across the check. A subsequent
[comparison of historical snapshots](https://git.chifor.me/cchifor/ailab/pulls/2016#issuecomment-143858)
found the complete `canary-pi-codex` route policy identical at revisions 2803 and
2882. Intervening revisions were not inspected; Relay profile/binding history is
outside Router configuration, so their unchanged status above remains the
operator's report. This metadata readback
made no provider request and does not establish current quota availability,
credential delivery or live Pi acceptance. Refresh the scoped route readback
before the owner launch as required by [live acceptance](LIVE-ACCEPTANCE.md).

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
- `holder-inputs.json`: the staging installer image/source and holder/Pi hashes,
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
Include the versioned holder/Pi archive paths in `holder-inputs.json`, `/evidence/v0.11.3`
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
Before startup, confirm the selected renderer services authenticate/poll and the
owner-approved bindings have valid scope/expiry. Initial credential delivery is
scheduled by the first normal launch after holder startup, not by issuance alone.
Confirm file ownership/mode and managed rendering/installation receipts before
that session's first prompt, using the [corrected activation order](ACTIVATE-CODEX-QWEN.md).
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
The first-delivery order is **renderers → holder startup/empty-session hello →
owner launch → rendering/installation/Open → verified delivery/readiness → first
prompt**. An `installing` binding with no launch has no renderer task yet; waiting
for its credential file before starting the holder is circular. Operator bus1928
reported this pause after owner issuance at 21:27:37 UTC on 2026-11-25. The
operator subsequently reported the holder started and idle (bus1945/1967).
That historical startup does not authorize the successor upgrade or acceptance;
preserve the operator's owner-approved window and shared-UID constraints.
Its original revision1 hash and v0.11.3 staging references are historical inputs,
not a hub approval or permission to launch the corrected first-registration definitions. The deployment hold above
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
it rotatable. Issue late, start the reviewed renderers and holder under the
operator's decision, verify the empty-session hello, then request the first
approved normal launch. That launch schedules rendering and managed installation;
the binding becomes ready after the validated install receipt. Require delivery
and readiness before its first prompt. Readiness remains a prerequisite for
rotation; it is not a prerequisite that can precede the initial launch. No direct
token placement, synthetic receipt or extra probe launch is part of this flow.
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
`nativeRecovery: 2`, `nativeStateReady: true` and exactly the three IDs in `profiles.json`; require zero
sessions before the first acceptance launch. Service-active status alone does not
prove profile admission has finished. If readiness times out, stop acceptance and
inspect the holder journal without replacing its state. This startup allowance is
separate from each profile's inference request deadline.
The pinned holder advertises recovery capability version2 for this canary's
owned-history recovery, separately from its current native-state readiness;
the [activation procedure](ACTIVATE-CODEX-QWEN.md) links the exact source contract.
