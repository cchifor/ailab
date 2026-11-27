# Preserve used Pi state while replacing the holder

This is a stopped, one-time state handoff for the three c4/UID1000 canary
profiles. It prepares a fresh journal/native-state/IPC directory while retaining
the approved profiles, credential homes, workspace markers and bindings. It
does not recover an old native session, transfer an admission grant, issue a key,
approve a profile, install a release, or start any service.

The old [idle preparation](UPGRADE-IDLE.md) remains ineligible after use. The
retained holder2604 creates a readiness fence when it starts with nonempty
`native-homes`; releasing admission cannot remove that fence. Do not erase,
rename or archive native homes to make that old state appear empty.

## Scope and prerequisites

The operator must have a qualified successor holder, published archive/image
receipts and independent evidence that it supports the unchanged published Pi
runtime and profiles. This document assigns no release version or artifact pin.
Use the existing exact installer inputs with only the separately reviewed
holder/image changes and the state-path overrides below. Retain the connector,
Pi and renderer pins unless a separate compatibility review requires a change.

The split is:

| Item | Path after preparation |
| --- | --- |
| Old journal, locks, socket and retained native homes | Unchanged under `/home/c4/.local/state/relay-holder` |
| New holder journal, locks, native homes and socket | `/home/c4/.local/state/relay-holder-handoff-PREPARATION_UUID` |
| Approved profile file | Unchanged `/home/c4/.config/relay-holder/profiles.json` |
| Profile homes and workspaces | Unchanged under the old `profile-homes` and `workspaces` directories |
| Renderer credential files | Unchanged `/home/c4/.local/state/relay/router-managed/BINDING_UUID/credential.json` |
| New holder unit | Same unit name, new binary pin and `--state-dir` |
| Reconciliation unit | Same unit name and connector, new `--holder-dir` |

The renderer talks to the hub and writes the profile's unchanged credential-file
path; it does not use the holder socket. Do not alter renderer configurations,
connector enrollment, grants, routes, profile revisions or workspace contents
as part of this handoff. Profile definitions do not contain the holder binary
or journal directory, so a byte-identical profile needs no new approval revision.
Retain the original acceptance run IDs, marker paths and carried prompt/attempt
counts. A new holder journal does not reset the owner's acceptance accounting.

Keep launch admission stopped externally. Require no outstanding hub launch
offer, active session, running turn or unresolved permission for this canary.
Finish the existing owner cleanup and preserve its evidence. A terminal
`unknown` command outcome remains unknown; a maintenance seal does not prove
that its effects succeeded or did not occur.

## Seal the old process, then stop it

While the old holder is still running, use the existing verified maintenance
client as c4 to inspect its current incarnation/admission revision, pause it,
then seal at the resulting revision and incarnation. The normal interfaces are:

```text
holder:maintenance OLD_STATE status
holder:maintenance OLD_STATE pause REVISION PAUSE_OPERATION_UUID
holder:maintenance OLD_STATE seal REVISION SEAL_OPERATION_UUID INCARNATION
```

Require `nativeStateReady:true`, zero active sessions and pending commands,
paused admission and the returned seal ID. Use the current exact revision for
each operation. Do not compact the journal. If the existing holder has already
restarted and cannot produce this drained seal, stop here; this procedure does
not clear its fence or forge a seal.

Stop and disable **only** `relay-holder-control-plane.service` and
`relay-holder.service` in c4's user manager. Verify each is inactive/dead,
disabled, MainPID0, empty ControlGroup, canonical local FragmentPath and no
DropInPaths. Keep the tmux connector and selected renderer services unchanged.
The preparation helper only reads service status; it never stops or starts them.

## Prepare the separate empty state directory

Record the stopped journal SHA256, journal ID, last incarnation and seal ID.
Record the SHA256 of `profiles.json`, `service.json`, and both unit files.
Do not print journal, history or credential contents. Write a UID1000-owned,
single-link, mode0600 request at an absolute canonical private path:

```json
{
  "version": 1,
  "operationId": "PREPARATION_UUID",
  "expectedFiles": {
    "profiles": "OBSERVED_PROFILES_SHA256",
    "service": "OBSERVED_SERVICE_JSON_SHA256",
    "relay-holder.service": "OBSERVED_HOLDER_UNIT_SHA256",
    "relay-holder-control-plane.service": "OBSERVED_CONTROL_UNIT_SHA256"
  },
  "journal": {
    "sha256": "OBSERVED_STOPPED_JOURNAL_SHA256",
    "journalId": "OBSERVED_JOURNAL_UUID",
    "incarnation": "OBSERVED_LAST_INCARNATION_UUID",
    "sealId": "OBSERVED_MAINTENANCE_SEAL_UUID"
  }
}
```

Use a new preparation UUID distinct from maintenance operations. As c4, execute
the reviewed helper once:

```sh
python3 scripts/prepare-used-holder-handoff.py --request /absolute/private/request.json
```

The helper holds exclusive flocks on both existing lock inodes throughout.
It accepts only a complete uncompacted v1 journal, contiguous from sequence1,
with known events, valid admission transitions and zero live sessions/pending
commands at its final seal. It hashes the old journal, native homes,
communication bindings, profile homes and workspaces before and after each
configuration move. It records metadata/hashes, never content. Symlinks inside
retained trees are inventoried without following their targets; special files,
hardlinked regular files, unexpected owners or writable directory boundaries
are refused. Inventory bounds are 20,000 entries, depth64, 64MiB per regular
file and 256MiB per tree. These are handoff bounds, not a new runtime format.

The fresh sibling state root must not exist. The helper creates it mode0700,
empty, without copying a journal, lock, native session or authority. It moves
only `service.json` and the two unit files into
`/home/c4/.config/relay-holder/used-handoffs/PREPARATION_UUID/` using Linux atomic
no-clobber rename. It retains the profile file and every old state path in place.
Private numbered receipts are fsynced; `complete.json` is written last. Save its
path and SHA256 from the sanitized result.

Any failure leaves both services stopped and all successfully moved files and
receipts in place. There is no automatic retry, rollback, deletion or overwrite.
Refusals report fixed diagnostic labels; unexpected errors report only their
exception type, without paths, payloads or tracebacks.
Inspect the partial operation and source/backup locations before proposing any
recovery. A missing `complete.json` cannot authorize the next step.

## Stage through the normal role, without activation

Use the reviewed published holder installation inputs. Override only:

```yaml
relay_holder_start: false
relay_holder_state: /home/c4/.local/state/relay-holder-handoff-PREPARATION_UUID
relay_holder_profile_state: /home/c4/.local/state/relay-holder
relay_holder_handoff_receipt: /home/c4/.config/relay-holder/used-handoffs/PREPARATION_UUID/complete.json
relay_holder_handoff_receipt_sha256: OBSERVED_COMPLETE_RECEIPT_SHA256
```

The role first transfers the self-contained verifier and authenticates this
receipt, exact scope, old retained evidence, original profile bytes and empty
new-state identity. It requires `start:false` and stopped services before any
installation writes. Existing default same-base role behavior is unchanged.
The normal role then verifies release artifacts, preserves `profiles.json` with
`force:false`, regenerates service configuration and installs only absent units.
The new holder state and reconciliation `--holder-dir` must match exactly;
the holder still loads the original profile file. Existing differing units fail
instead of being replaced. Recompute both unit hashes and service configuration
from the reviewed release and inspect the effective units after daemon-reload.

This is one-shot stopped staging. A full role pass followed by a second pass has
not been qualified; any rerun requires a fresh review of the retained receipts
and actual configuration state. Once the holder has started, or profile/workspace
contents have legitimately changed, this gate refuses a role rerun before
installation writes. It is not general active-service convergence.
Do not change the profile base back, discard the receipt or clear the new state
to bypass that refusal. Future maintenance needs its own reviewed transition.

## Activate deliberately and establish new identities

After the operator's separate start authorization, reload the c4 user manager
and verify the exact new unit fragments, no overrides, binary SHA, new state
path and unchanged connector enrollment. Keep reconciliation stopped. Start
only the holder, then inspect hello/maintenance status over the **new** socket.
Require new journal ID/incarnation different from the old ones, exact profile
digests, `nativeRecovery:2`, `nativeStateReady:true` and zero sessions. Its new
admission state starts independently; immediately use the normal pause action
and retain its receipt while the owner's next launch window is prepared.
Do not release or copy the old seal, reuse old command IDs or adopt old sessions.

Only after the owner's fresh window and existing hub cleanup checks, resume the
new holder's admission using its new revision and start the reconciliation
service pointed at the new directory. Verify its effective path and a fresh
hello before the first owner launch. Use normal launch/render/install/Open gates
before the first prompt as in [ACTIVATE-CODEX-QWEN.md](ACTIVATE-CODEX-QWEN.md).
Old native-history resume requests must be refused across the journal/incarnation
boundary; recovery within the new holder remains a separate live acceptance.

Recheck the old journal/native-home snapshots and workspace markers after
staging and startup. Old journal/native data remains preserved indefinitely.
Later authorized workspace and credential updates are normal operations; they
are not a requirement to freeze the entire old root forever. Keep both state
roots and the preparation receipts on any failed hello, startup or acceptance.
Do not automatically reinstall old units: the old root still has retained Pi
homes and its restart fence. Restoring a binary is not restoring session authority.

## Offline validation boundary

`test_prepare_used_holder_handoff.py` exercises real private files, flock and
atomic rename with fixture systemd observations. The role integration test
executes the real Ansible script transfer in a disposable UID1000 home, including
default no-op, complete receipt, refusal and no-start paths. The separate
`test_used_holder_handoff_runtime.py` is an explicit artifact-qualified holder/Pi
test; its required environment and result must be recorded independently. None
of these tests proves live systemd containment, deployment, owner authority or
provider acceptance.
