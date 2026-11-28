# Fixed finalization observer

This sidecar observes cleanup for the fixed Relay/Pi subscription plan. It has
exactly two stages, `preflight` and `renderer`. It never starts a service,
prepares a holder, reads credential contents, changes settings, acquires or
renews a lease, grants authority, revokes a key, or invokes inference.

`QUALIFIED_FINALIZATION_OBSERVER` remains `None`. The public factory refuses
before constructing a remote reader or opening evidence. The finalizer's own
public source gate must also remain NULL until the complete merged adapter,
broker, source releases and deployment identities are qualified. Hub 5f90 does
not complete the retired/all-revoked binding case; fix733 must be included in
the actual deployed Hub. An open release or a local test is not that evidence.

## Broker and driver interface

The broker calls:

```python
create_finalization_host(
    gate=gate_process, plan=validated_plan, decision_raw=retained_decision_bytes,
    authorization_sha256=independently_authorized_decision_sha256,
    config=qualified_config, evidence=private_new_evidence_path,
    on_fault=broker_fault_callback, lease_ok=broker_lease_readback,
)
```

`gate=None` is permitted during preparation; assign the broker-owned gate
process before `start()`. The host exposes `start`, `alive`, `stop`, `drain`,
`fault_reason`, and `ev.close`. `drain` cancels and joins active reader work;
the broker then terminates the gate and joins its framing process. No Phase A
`prerun`, collector, admission policy or handoff state is used.

The import closure is stdlib, `relay_finalization_observer.py`, and the existing
`relay_phase_a_observer.py` framing/evidence module. Bundle the exact reviewed
`relay-observer-gate.py` as well, including its independently reviewed bounded
refusal-worker fix. This branch is based on 48ff; its offline Gate integration
proves the single accepted-request protocol, not that the older Gate's refusal
availability finding is fixed. Parent integration owns that newer Gate.

The fixed Runner entry calls
`createFinalizationObserver({socketPath, qualificationSha256})` from
`relay-finalization/client.cjs`. It returns `{qualificationSha256, observe}`.
The digest comes from the compiled finalizer qualification, never a Runner
override. Its credential-less Unix frame is exactly
`{v:1,call:'checkpoint',kind:'finalization',deadline,context}`. The context's
AbortSignal is handled locally; closing the socket triggers the existing
Gate's terminal cancellation frame. Replies are bounded to 512 KiB, requests
to 64 KiB, and each stage to 120 seconds within the fixed cleanup deadline.

`validated_plan` is the result of the actual finalizer's pure
`validateFinalizationInputs({reports,manifest})`, run before authority. It has
exactly `version`, `purpose`, `scope`, `reportPins`, `decisionSha256`,
`cleanupDeadline`, `launches`, `markers`, `originalIdle`, and `sources`.
The observer does not reimplement report validation. It checks fixed scope,
ten launches/four markers for A/B/B-inputs/C or eleven/five with the explicitly
selected D report, and exact equality with every callback context. There is no
arbitrary extra-report, session, process, SQL, path, or remote-command list.

The separately retained decision bytes must be exact UTF-8 JSON with
`{version:1,purpose:'canary-finalization',runId,reportPins,cleanupDeadline,qualificationSha256,bindingIds,noFurtherUse:true}`.
`runId` is the broker's fixed cleanup-run UUID; `qualificationSha256` hashes the
canonical pure plan's compiled `sources`, and `bindingIds` is the exact three
fixed bindings in plan order. The pure plan supplies the fixed full scope.
Its hash must match both the validated plan and the broker's independently
authorized ledger value. A matching hash is not authorization by itself. The
broker must never obtain that ledger value from a callback or self-authored
Runner manifest. `Reader` is the offline composition seam; it is not an
alternative public authority entry.

## Qualified deployment configuration

The future compiled observer qualification contains `configSha256` (the SHA256
of sorted compact UTF-8 JSON of the complete trusted configuration) and exact
`files` hashes for `hub.sql`, `router.cjs`, and `host.py`. The reviewed broker
manifest additionally pins the complete Python/client/import closure.

Configuration has exactly these top-level keys:

| Key | Fixed fields |
| --- | --- |
| `version`, `kubeContext` | `1`, `admin@ai` |
| `hub` | namespace, deployment, selector, container, source, podUid, restarts, imageId |
| `router` | the same identity fields, plus storeIdentity, runtimeInstanceId, root `{pid,startTicks,executableSha256}` |
| `hubDb` | namespace, pod, container, user, database, podUid |
| `devWorker` | host `192.168.0.8`, user `c4`, knownHosts absolute path and knownHostsSha256 |

These values must come from independent authorized deployment observations.
They are not inferred from the callback or replaced with current values after
a mismatch. Restart, inode replacement, image/source drift or a changed root
requires a new qualification. No deploy-specific configuration is invented or
enabled in this change.

## Evidence and completeness

`hub.sql` uses a repeatable-read, read-only transaction and five-second
statement timeout. It selects only metadata from schema93 tables. It counts
the complete fixed-profile/binding inventory before applying bounded arrays;
overflow, missing rows, foreign identity, unlisted sessions/launches or
installations, pending commands/permissions/control leases, nonterminal
credential operations, missing generations and active/blocked consumers all
refuse. Session and renderer identities are joined to the immutable report
launches. No newest-N API list is treated as completeness evidence.

The Router reader opens the existing SQLite store read-only with extensions
disabled and a read transaction. SQL projects only managed-key metadata,
aggregate/fence records and process-event metadata; it never selects bearer
material, key hashes, secret rows or captured request bodies. Missing aggregate
or deployment rows refuse. All open meter rows refuse regardless of epoch;
only an actually empty complete scan permits zero open/stale rows. Unknown
token usage remains distinct from unattributed attempts.

Closed fence identity, revision, original key/deployment, counters and terminal
evidence must agree with the aggregate. Retained trajectory attempt totals
must equal lifetime meter totals; eviction/retention gaps refuse. Process
identities remain joined to the exact current Router runtime and parent root.
The census reads real `/proc` PID/start identities, validates the root's actual
executable hash, rejects unreadable/disappearing entries and changed membership,
and includes both all current descendants and still-live journal-linked PIDs
even if reparented. It does not infer absence from a terminal trajectory.
The settings API's saved and active cold values, pending flag, revision and
runtime metadata bracket the read. Any descendant, ambiguous census or restart
blocks completion. This conservative census can refuse during unrelated process
churn; it does not silently weaken coverage to obtain a pass.

At `renderer`, every generation must be revoked with cleanup complete and no
active/blocked consumers. Exact `command_id`, launch/session/binding/credential,
generation and full immutable local reference must match the stored cleanup
receipt. Raw `absent` or `not-current` is preserved. A `not-current` receipt
means the renderer found a different generation at the time; it is never
relabeled as historical absence. The finalizer separately applies its reviewed
acceptance policy to this full reference and the fresh file evidence.

The dev-worker reader walks fixed directories with `openat`/`O_NOFOLLOW` and
requires private ownership. It uses `lstat` only for the shared per-binding
`credential.json` and `.credential.json.next` paths; even a FIFO or symlink is
present, and no credential content is opened. Missing parent directories do
not prove absence. Acceptance marker files alone are read (maximum4096 bytes),
hashed, counted, and compared with the retained marker metadata and exact
command text. Inode/path replacement, link count, mode or content changes refuse.

Each successful projection and bounded raw metadata evidence is fsynced before
reply. Evidence paths are new/exclusive under an already-private broker
directory; POSIX ownership/mode or Windows current-user ACL is checked. Output
and stderr from child readers are bounded, and failures emit fixed codes rather
than external text. Cancellation kills and joins owned local children. Remote
reads also carry their own deadline; killing SSH/kubectl alone is never claimed
as proof of remote termination. The remote operation is read-only regardless.

## Offline qualification

Use already-cached images; no downloads, installations or live endpoint calls:

```text
python3 -m unittest -v scripts.tests.test_relay_finalization_observer
node --test scripts/relay-finalization/reader.test.cjs
python scripts/tests/test_relay_finalization_observer.py --postgres NEW_EVIDENCE_DIRECTORY
```

Run the first two in pinned Node24 image
`sha256:8fa96264f302f58bbb13aa7339c541547b56975766ce956c875da149452a3e40`,
network none, UID1000, read-only source/root and private writable `/tmp`. The
explicit PostgreSQL orchestrator uses cached PostgreSQL17 image
`postgres@sha256:f9cf3e1f0066f0eb0c48707d864f0ac5941e9ff454a072077d97a4d278d07d73`,
an owned UUID container, no published ports/network, private tmpfs, and removes
only that container in `finally`. It retains all command output. Its synthetic
schema has the actual queried columns; this proves fixed SQL and validators,
not the Hub revoke state machine or a live database migration.

The tests use real SQLite, private files, `/proc`, child processes and Unix
transport with synthetic scoped metadata. They include incomplete/foreign/
overflow/retention-gap evidence, marker and credential-path attacks, exact
renderer references, abort/child joining, both fixed stages and explicit D
inventory. The first Node attempt's ambiguous SQLite column and fixture Gate
constructor failure are preserved in the qualification directory; subsequent
focused results do not overwrite them.

Remaining release gates are the actual finalizer/broker/Hub-PG end-to-end
composition, newer Gate integration, independent authorization staging, exact
merged source/image/store/runtime/root configuration, Windows production ACL
qualification where used, and deployed Hub733. The sidecar supplies no live
approval, cleanup authority, deployment claim or inference window.
