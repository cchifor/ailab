# Upgrade the started but never-launched canary

Use this procedure only for the dedicated c4/UID1000 canary whose complete holder
journal proves that no command or session has ever been accepted. It prepares
four configuration files for the normal role to replace with reviewed published
pins. It does not install artifacts, change a profile approval, start services,
release admission, issue credentials or make an inference request.

This differs from [RESTAGE-RECOVERY.md](RESTAGE-RECOVERY.md), which requires an
installation that has never started and has no journal. Do not use that older
helper after holder startup. This procedure is also not recovery of a previously
used holder: any command, session, preflight refusal, retained native home,
compacted journal or unknown state is a hard refusal. Preserve such state and use
a separately reviewed maintenance procedure; never delete it to become eligible.

The normal role deliberately preserves existing profiles and units. The upstream
[binary-only upgrade](https://git.chifor.me/cchifor/relay/src/commit/e843921b7cff76406b7da0b5b35cf653c7e458c5/docs/plans/2026-10-09-agent-control-plane/holder-upgrades.md)
preserves profile bytes, so it does not implement this combined holder/Pi repin.
Here a durable maintenance seal and a proof of no prior launches protect the
stopped configuration handoff. No role behavior is changed.

## Review the successor before stopping

The current successor source is reviewed v0.13.1
`2604c41b69c5d9c50a3324545a51f0bfa38435a0`
([Relay PR705](https://git.chifor.me/cchifor/relay/pulls/705)). Publication pins,
successor profile bytes and exact-artifact qualification are pending in
[README.md](README.md); do not stop the current idle holder merely because the
source merged. The hub stays at e843/schema93. The required Router correction
source is 4af66b8, with actual deployment/readiness verified separately.
Operator bus1967's idle/no-launch report selects this procedure; the helper must
still prove the complete journal and all filesystem/service conditions below.

Have the reviewed successor's published holder/Pi archives, control-plane
manifest, immutable installer image receipt and exact profile definitions ready.
Verify their source and hashes through the existing release process. Keep the
current connector, credential renderer, binding identities, route grants,
workspaces, credential homes, effort allowlists and request/output limits.
Runtime and artifact pins change together in `profiles.json` and
`launch-definitions.json`; their holder/session revisions must be the next
approved revision, never replacement bytes at an already approved revision.
Derive the exact profile file SHA256 in `holder-inputs.json` and
`holder-activate-codex-qwen.json` from the new bytes.

The helper has no successor-pin arguments and cannot approve that release or
profile. The independently reviewed successor inputs govern the later role run.
The hub can remain on its verified compatible source; a new installer image pin
does not imply a hub deployment. Existing evidence for older runtime bytes is
historical evidence, not qualification of their replacements.

## Pause, seal and stop

Keep new launch requests stopped externally throughout this handoff. Using the
existing verified release's `holder:maintenance` client as c4, inspect status and
retain its current incarnation and admission revision. Require no active
sessions or pending commands and `nativeStateReady:true`. Apply the normal pause,
inspect the resulting revision, then seal at that current incarnation/revision
using a fresh operation UUID. The seal ID equals that seal operation UUID.

The existing client interfaces are:

```text
holder:maintenance STATE status
holder:maintenance STATE pause REVISION OPERATION_UUID
holder:maintenance STATE seal REVISION OPERATION_UUID INCARNATION
```

`STATE` is `/home/c4/.local/state/relay-holder`. These are client argument forms,
not a request to install a package or execute an unpinned tool. Use the operator's
already verified release execution environment. Retain the returned seal and
status; after a lost response inspect the same operation rather than inventing
another. Do not compact the journal. Require the final status to be paused,
drained and sealed at the recorded incarnation.

Then stop and disable only `relay-holder-control-plane.service` and
`relay-holder.service` in c4's user manager. Keep the existing tmux connector and
selected renderer services unchanged. Both stopped units must report inactive,
dead, disabled, MainPID0, an empty ControlGroup, the expected local fragment and
no DropInPaths. The helper verifies those properties again; it never stops a
service itself.

## Record exact old identities and prepare

After stopping, obtain SHA256 values for these four nonsecret files and for
`/home/c4/.local/state/relay-holder/journal.jsonl`. Record the existing journal ID,
last holder incarnation and maintenance seal ID from the retained status/hello.
Do not print journal contents or credential files.

| Request key | Existing file |
| --- | --- |
| `profiles` | `/home/c4/.config/relay-holder/profiles.json` |
| `service` | `/home/c4/.config/relay-holder/service.json` |
| `relay-holder.service` | `/home/c4/.config/systemd/user/relay-holder.service` |
| `relay-holder-control-plane.service` | `/home/c4/.config/systemd/user/relay-holder-control-plane.service` |

Create a private regular single-link request file owned by UID1000, mode0600.
The following is a shape template; replace every placeholder with the observed
value, and use a fresh preparation operation UUID distinct from the seal UUID:

```json
{
  "version": 1,
  "operationId": "PREPARATION_UUID",
  "expectedFiles": {
    "profiles": "OLD_PROFILES_SHA256",
    "service": "OLD_SERVICE_JSON_SHA256",
    "relay-holder.service": "OLD_HOLDER_UNIT_SHA256",
    "relay-holder-control-plane.service": "OLD_CONTROL_UNIT_SHA256"
  },
  "journal": {
    "sha256": "STOPPED_JOURNAL_SHA256",
    "journalId": "ORIGINAL_JOURNAL_UUID",
    "incarnation": "LAST_HOLDER_INCARNATION_UUID",
    "sealId": "CURRENT_MAINTENANCE_SEAL_UUID"
  }
}
```

As c4, from the reviewed checkout, run:

```sh
python3 scripts/prepare-idle-holder-upgrade.py --request /absolute/private/request.json
```

This is a mutation, not a dry run. The helper acquires exclusive flocks on the
existing `holder.lock` and `connector-control.lock` inodes without creating,
truncating or removing them. It accepts only a complete, uncompacted v1 journal
with contiguous sequence numbers from1 and the exact reviewed SHA256. Its only
allowed events are `holder-started`, `holder-admission` and
`holder-maintenance-seal`, with their exact fields and valid admission/operation/
incarnation transitions. Admission must end paused with the recorded seal.
All command and session events are refused, including failed or rejected ones.
The preparation UUID must also be absent from the journal's maintenance
operations. Records use the Rust reader's LF framing; permitted JSON whitespace
does not weaken the exact journal SHA256 comparison.

The helper also requires empty `native-homes` and `tool-bindings`, the exact
three profile-home/workspace directories, canonical owned paths and unchanged
file identities. It preserves workspace/profile-home contents without reading
them. It does not access host credentials, managed router files or renderer state.
A stale private holder socket is retained; only the normal holder startup can
replace its own socket after acquiring its lock.

It writes a flushed preparation receipt under
`/home/c4/.config/relay-holder/idle-upgrades/PREPARATION_UUID`, then atomically
moves only the four listed files there with Linux no-clobber rename. Each move
and parent directory is synced and receives its own receipt. Journal bytes and
inode, singleton locks, native directories and service status are rechecked
between moves. A final `complete.json` records `prepared-stopped-sealed`.
The backup directory is private and existing operations are never overwritten.
Systemd can keep removed units loaded with their old FragmentPath and
NeedDaemonReload=yes until the later explicit role/activation sequence reloads
the manager. The helper accepts this stale loaded metadata for its own moved
units, as well as not-found metadata, while still requiring inactive/disabled
services, MainPID0, empty cgroups and no overrides. It does not reload systemd.
This is same-UID maintenance, not protection from a hostile process sharing c4.

## Install separately and verify the preserved seal

Only a successful final receipt permits the next preparation step. Rerun the
normal holder role with the reviewed successor `holder-inputs.json` and
`relay_holder_start:false`. It installs the immutable archives, supplies the
absent profiles/service configuration/units and validates their exact pins.
Preserve the backup, the full holder state and all old immutable release
directories. This step must leave both services stopped.

Use the existing explicit activation procedure after the new profile approvals
and other operational prerequisites are satisfied. Verify the new binary,
runtime/profile hashes, fresh holder incarnation, unchanged original journal ID,
zero sessions and `nativeRecovery:2`. Require `nativeStateReady:true` and the
preserved pause/seal through maintenance status. A false readiness value is a
failure, never a reason to delete homes or rewrite the journal.

Release the recorded seal with a fresh operation UUID at the **new** current
incarnation/revision, inspect status, then explicitly resume at the resulting
revision. The existing client forms are:

```text
holder:maintenance STATE release REVISION OPERATION_UUID INCARNATION SEAL_UUID
holder:maintenance STATE resume REVISION OPERATION_UUID
```

Seal release alone leaves admission paused. Only after the reviewed release and
profile readbacks may the owner start the existing bounded acceptance sequence.
Service startup or configuration preparation does not demonstrate live Pi
acceptance, recovery, provider availability or successful credential delivery.

## Interrupted preparation

On any refusal or incomplete receipt, keep both services stopped and preserve
every file. A failure can happen after some moves or between a move and its
receipt. Inspect the request, preparation receipt, actual old/backup file
identities and journal hash; do not rerun with another operation UUID, overwrite
the backup, move files back automatically or use the normal role to fill a
partially prepared installation. Obtain a concrete reviewed recovery procedure.
The helper intentionally has no automatic partial retry or rollback. Locks are
released on process exit while their original inodes remain in place.

Focused Linux validation:

```sh
python3 -B -m unittest scripts.tests.test_prepare_idle_holder_upgrade -v
```

The tests exercise real flock/no-clobber filesystem behavior with synthetic
systemd status and exact journal-format fixtures. They do not establish a live
systemd upgrade or authorize production execution.

On 2026-11-26, a separate offline check executed the verified published v0.13.0
holder from source `e843921b7cff76406b7da0b5b35cf653c7e458c5`, binary SHA256
`f70a471bb00b3f61675768666802e9b3ac9ea02b59dbf554191c198bd88d7909`.
The published receipt149439, archive and inner manifest were checked before
execution. A fresh empty-profile holder accepted normal IPC pause/seal calls;
its live flock prevented preparation. After SIGTERM exit0, the helper accepted
its unmodified journal and moved all four fixture files, preserving journal
bytes and inode. That exact emitted journal is a permanent regression fixture,
SHA256 `033e05cf92d8a8aee7e7e9ad8b24065c3c6a3b9a143453829442530a5eae0556`.
This ran as UID1000 with networking disabled, no workers or credentials, and
synthetic stopped-systemd status; it proves the real journal/lock interface,
not a real user-manager upgrade or live canary outcome.
