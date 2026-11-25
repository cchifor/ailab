# Prepare the never-used stopped canary for restaging

This procedure prepares files only. It does not install a release, start/enable a
service, issue credentials, perform inference or qualify recovery. Keep PR2003
activation held until the home-layout and coordinated release prerequisites are
resolved. Use normal owner/operator approval for this exact change and the later
role rerun; the existence of this helper is not a deployment approval.

The old role precreated three profile credential homes under the holder-reserved
`/home/c4/.local/state/relay-holder/native-homes`. Even empty preexisting children
fence native readiness. The corrected role/profiles must use `profile-homes` for
credential homes, leaving `native-homes` to the holder. Do not weaken that fence.

## Required release and state checks

For recovery v2, deploy and verify the qualified newer hub and schema migration
093 before connecting the upgraded holder. The current v0.12.1 hub accepts only
hello `nativeRecovery:1`; the new holder advertises v2. Require actual reviewed
source/image/archive/manifest/binary digests, matching holder/Pi artifacts and
exact approved new profile revisions. Staged v0.11.3 artifacts cannot qualify the
new path. Preserve the current hub until that coordinated release is ready.

The parent read-only 2026-11-25 snapshots
`pi-stopped-holder-metadata-20261125.json` and
`pi-native-home-metadata-20261125.json` in the operator-local
`.data/inference-release-evidence` report stopped services, no journal/socket,
and three empty private profile placeholders. They are historical preflight
evidence, not a substitute for execution-time checks.

Run only for the never-used c4 installation. The helper fixes UID1000 and
`/home/c4`; its CLI has no path/user override. It requires:

- Both `relay-holder.service` and `relay-holder-control-plane.service` disabled,
  inactive/dead, MainPID0, empty ControlGroup, exact user FragmentPath and no
  DropInPaths. Obtain exclusive operator change ownership so no concurrent
  deployment, enablement or manual startup races the transaction.
- Holder state contains **only** `native-homes` and `workspaces`; no journal,
  socket, prior lock or unknown state. Both roots contain exactly the three
  reviewed profile UUID directories, all empty, UID1000/mode0700 and canonical
  nonsymlink directories. Nonempty workspaces also refuse this narrow procedure.
- Four existing regular, single-link files with reviewed old SHA256 values:
  `.config/relay-holder/profiles.json` and `service.json` (0600), and
  `.config/systemd/user/relay-holder.service` and
  `relay-holder-control-plane.service` (0644), all owned by UID1000.
- Safe directory ancestry and a private backup destination on the same filesystem.

Any actual/unknown session state requires a separate maintenance plan. Do not
delete or relabel it to make these checks pass. The active-holder sealed upgrade
helper is unsuitable here: it requires an active maintenance endpoint and
unchanged profiles; never start the old canary merely to satisfy it.

## Reviewed preparation command

Prepare a controller-reviewed JSON file with exactly these keys. These are the
**old installed** SHA256 values reported by the parent's nonsecret hash-only
snapshot `pi-staged-config-hashes-20261125.json`, not new release hashes. The staged
v0.11.3 inputs pin source `aaad724607e2f9a97f64a40951142cf39815e7d4`.
Review that evidence against the original approved artifacts before use; the
helper checks the four hashes and UID/mode/link metadata again at execution and
refuses drift. Do not replace these expectations with whatever a changed host
happens to contain just to pass validation.

```json
{
  "profiles": "c1c937fca19a95e46fd0178a37ab973237b1af48549bec18959b3200f5c93809",
  "service": "903674ffe15726b68b4efe559898a192bbc197009c8917b3a8c7dbc8ed8ba367",
  "relay-holder.service": "d271f544871dd56db6f1f4ac57e6a7c9cbd8130213246e1183083a9ae83fb799",
  "relay-holder-control-plane.service": "832bc4afb2bba53e91a28d1c18e647d256b76c4fc23144cb69ad63aa9a7559f2"
}
```

After approval, run the reviewed helper as c4 on dev-worker-1, with a fresh
canonical lowercase operation UUID and the reviewed hash file:

```sh
python3 scripts/restage-inference-canary.py \
  --operation OPERATION_UUID --expected-sha256 REVIEWED_OLD_HASHES.json
```

This command **moves files**; it is not a dry run. It first rechecks the stopped
layout and hashes, exclusively creates and flocks the holder's actual
`holder.lock`, then rechecks service/state. Under that startup lock it creates
`.config/relay-holder/restaging/OPERATION_UUID` (0700), writes a private prepared
receipt, and atomically moves the four files and three empty placeholders into
that backup. Linux `renameat2(RENAME_NOREPLACE)` enforces no clobber; there is no
non-atomic fallback. File bytes/modes and placeholder directories are preserved;
each completed move has a flushed receipt. No credential/enrollment file,
immutable release closure, workspace or native history is moved or read.

Success writes `complete.json` and prints only operation/state/backup metadata.
Profiles and both unit files are then absent, `native-homes` is empty, workspaces
are preserved and services remain stopped. The flock is released on process exit;
the empty private `holder.lock` inode deliberately remains. Never unlink it to
reset a run: removing a flock inode can create competing locks. The helper refuses
any repeat/partial operation or preexisting lock, even with a different UUID.

## Failure and separate restaging

On any failure, stop. Failure before the backup receipt or first move can still
leave the private lock inode; this also forbids blind retry. Earlier successful moves remain in the operation backup;
unmoved files remain at their original paths. There is no deletion, automatic
rollback or retry. `00-prepared.json` records every intended mapping; per-move
receipts record completed steps. A crash between a move and its receipt requires
checking both locations and the recorded old hash/metadata. Absence of
`complete.json` means incomplete preparation. Preserve all bytes and the lock
inode; have the owner review an explicit stopped-state recovery plan. Do not
manually move files over unknown replacements or start either service.

After a complete receipt, the operator separately reruns the corrected role with
the reviewed new inputs, `relay_holder_enabled:true`, `relay_holder_start:false`.
The role can now populate the absent exact profile/unit paths without disabling
its drift checks. It creates approved `profile-homes`; it must not recreate
children under `native-homes`. Preserve old release closures, backups, workspace
markers and connector identity. `start:false` skips the role's manager reload and
effective-unit checks: separately perform the reviewed user daemon-reload and
verify exact FragmentPath/no drop-ins plus disabled/inactive/MainPID0, without
starting or enabling services. Do not treat this helper's old-profile absence as
approval for new profiles.

Activation and late managed issuance are separate gated steps. Claude cold
route/trust/Messages prerequisites precede Claude issuance. Require current hub
compatibility, new holder readiness/ownership receipts and the bounded acceptance
procedure. Recovery v2 is same-live-holder owned-history recovery; whole-holder
restart remains fenced. A stopped filesystem preparation is not a recovery pass.

## Local validation

Run `python3 -B -m unittest scripts.tests.test_restage_inference_canary -v`
in Linux. Fixtures inject a private temporary home/current UID and fake systemd
readback; these are internal test seams, not CLI options. Tests execute only
temporary-file moves and verify byte preservation after partial failure, active/
enabled/PID/drop-in/hash/link refusals, no-clobber behavior and the actual flock
held during every move. No live host or provider is required.
