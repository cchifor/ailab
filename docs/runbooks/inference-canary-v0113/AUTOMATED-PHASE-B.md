# Automated Claude Phase B

[owner-phase-b.js](owner-phase-b.js) runs the fixed Claude lifecycle and effort
sequence through the existing [Phase A Runner](owner-phase-a.js). It preserves
the normal launch, control-lease, permission, delivery, recovery and cleanup
checks. Phase B has a separate report and accounting. It references the completed
Phase A report by hash; it never imports that report as mutable state or resets
its carried one prompt / two attempts.

The public entry is held by `QUALIFIED_FENCE_ROUTER = null`. The new fixed
managed-key fence port is mandatory before launch; the historical Router pin
below does not implement that contract. A final combined Router release and
its compatibility/artifact/host qualification must precede a reviewed pin update.
Tests compile isolated copies with only exact source-pin substitutions and
record before/after hashes. A passing synthetic copy does not qualify the
unchanged held public bundle for execution. Loading or merging these files
supplies no credentials, execution window or authority.

## Fixed scope and inputs

The retained pre-fence core pins the following sources and artifacts. A different deployed source
requires an explicit reviewed compatibility update; it is not accepted by
changing an observer's reported source.

| Component            | Pin                                                                |
| -------------------- | ------------------------------------------------------------------ |
| Hub                  | `5f90d03a6ed291e6c62dfde75938ba07c20d8902`                         |
| Router               | `4e4bb34e2f968f9450aabe9cf228d3e70c8bab71`                         |
| Holder source        | `64dc50f47a4867a6d9a7afc2e7d5ae24068c59af`                         |
| Holder binary SHA256 | `d406b1f363f590b386ab3eaea4a8cb350c5ac9c49a3dcb26acc34034dfcf6926` |
| Pi source            | `2604c41b69c5d9c50a3324545a51f0bfa38435a0`                         |
| Pi manifest SHA256   | `d0bf7f9d44a2ae992bb1fbd87f7f2043c7d582ad563078bd13d713ebcd211a0d` |
| Profiles SHA256      | `928d41f0b24c0d1d2493458e7fac23cfd686c045b1aa9daaee480684a0aea9a4` |

The Claude profile is `fdb56ca5-908b-40ee-94b3-aac735dd8622`, revision 1,
definition SHA256
`b1fdad80cf111a6aa1123139ee9dbcb8bd060a8b423ea427d1d43205915ac01d`, bound to
`a8f6211c-d7e2-4ffd-824d-b158e349a8d4`. It uses `anthropic-messages`, route
`canary-pi-claude`, output cap 1024, and the approved off/low/high effort choices.
The account, model and execution window come from the actual assigned inputs:

```text
{version:1,
 lifecycleRunId:UUID, effortRunId:distinctUUID,
 account:label, model:label,
 window:{startsAt:epochMs, expiresAt:epochMs}}
```

Labels contain 1–200 characters from `A-Z`, `a-z`, digits, `.`, `_`, `:`, `-`.
Times are nonnegative safe integers. The window must already have begun and
must not have expired. No historical account choice is substituted. Normal
metadata reads verify the approved profile, full bounded credential history,
no active consumers, exact principal/generation, expiry and fresh delivery.

The sequence makes three launches: the lifecycle source, its owned recovery
with the same native UUID, and a fresh effort session. It performs no B0 probe,
credential issuance/rotation, profile approval, holder handoff or final grant
revocation. Retain the actual cold qualification for the selected account.
Phase C and final grant cleanup remain separate.

## Host and CLI boundary

`createClaudePhaseBRunner(env, inputs)` returns the fixed core's `start()`,
`stop()`, `renew()` and `state`. Its `env.load()` must return null for the new
exclusive B store. Start is single-use, including failure. Phase A replacement
and final-revocation entrypoints refuse on this core.

The core additionally requires `env.executionDeadline` (absolute milliseconds)
and the five fixed `env.attemptFence` callbacks documented below. Missing methods,
an expired deadline or the unassigned release pin refuses before launch.

The named `createClaudePhaseBAutomation` export from
[owner-phase-a-automation.cjs](owner-phase-a-automation.cjs) accepts
`directory`, `phaseInputs`, `phaseAReportSha256`, `authenticatedFetch`,
`collectCheckpoint`, and the existing bounded deadline, cancellation and
snapshot options. It selects the fixed B factory internally; callers cannot
inject a different Runner. Phase A predecessor/projection/renewal-prelude options
are rejected.

The host creates a new private directory beneath a canonical private owned
parent. It preserves `phase-b-inputs.json`, its SHA256, `run.lock`, and numbered
state snapshots with file/directory synchronization before dispatch. Existing
directories are refused. Failure retains the directory, original operation IDs
and uncertain receipts; removing files is not a supported retry. The immutable
input contains only the B inputs and the Phase A report hash, not Phase A's raw
report or credentials.

The host uses the same authenticated session throughout execution, 20-second
control-lease renewal, bounded checkpoint/HTTP response collection, and bounded
abort/cleanup draining as Phase A. The execution ceiling is 30 minutes, shortened
by an earlier absolute deadline or assigned window. The core also stops at
credential expiry and each phase deadline. These host bounds do not enlarge
the phase or prompt budgets. Unresolved cleanup is recorded as unresolved,
never as a successful closed session.

[owner-phase-b-cli.cjs](owner-phase-b-cli.cjs) exposes this integration signature:

```js
runPhaseBCli({
  argv: [
    "--inputs",
    ABS_INPUTS,
    "--phase-a-report",
    ABS_REPORT,
    "--output",
    NEW_ABS_DIRECTORY,
  ],
  env: { RELAY_SESSION_SOCKET, RUNNER_DEADLINE },
  collectCheckpoint,
  writeLine, // optional synchronous JSONL sink
  signals, // optional signal emitter
});
```

The CLI requires Linux, private bounded canonical input files, a new output
directory, and an absolute future `RUNNER_DEADLINE` as an epoch-millisecond
string no more than 30 minutes away. It uses the existing Unix-domain session
transport; authentication remains in the operator's session broker. The supplied
Phase A report must have `phaseACompleted:true`, the three passed
Codex/Qwen/Codex-effort runs, preserved original raw report and carried counters,
and status `phase-a-passed` or `phase-a-and-grants-closed`. The CLI hashes the
exact file bytes. This validates report structure and attribution, not its
authenticity or live truth; the operator must bind it to the completed run.

`collectCheckpoint({name,instructions,deadline,signal,context})` returns actual
observations. Dispatch on the typed context, not instruction text. The host
applies the existing validator and stops on absent or invalid observations.
The CLI refuses before authenticated traffic if no reviewed collector is linked.
Direct execution without that integration is not an automatic observation path.

## Readiness projection

The existing pre-launch, post-open, permission and observation contexts retain
their validators. The new readiness context is:

```text
{version:1,kind:'claude-readiness',notBefore,account,model,pins,profile,window}
```

Its result is:

```text
{version:1,observedUtc,
 hubSource,routerSource,holderSource,holderBinarySha256,piManifestSha256,profilesSha256,
 holderId:UUID,journalId:UUID,nativeRecovery:2,nativeStateReady:true,
 routerDeploymentId:UUID,
 activeSessions:0,pendingCommands:0,admissionPaused:false,
 route:{alias:'canary-pi-claude',account,model,protocol:'anthropic-messages',
        maxAttempts:1,fallbacks:[],transport:'sdk-resume',cliVersion:'2.1.283',
        warmSessions:false,trustVerified:true,configRevision:integer,
        deploymentId:actualDeploymentId,providerGeneration:actualRegistrationGeneration},
 cold:{reportSha256:hex64,account,source:ROUTER_SOURCE,passed:true},
 projectionSha256:hex64}
```

Observation must be at or after `notBefore`, less than 120 seconds old and at
most five seconds in the future. Route/account/model and runtime pins must
match the fixed scope and actual inputs. The cold report must qualify that
selected account; an unrelated historical B0 cannot satisfy it. These are
operator-attributed observations. Their hashes do not independently authenticate
the deployment, history or database. Normal Relay metadata checks still run.

## Native and Router branch projection

The `claude-branch` context is:

```text
{version:1,kind:'claude-branch',sessionId,generation,nativeSessionId,holderId,journalId,
 principal:'key:ID',namespace:'default',tenantId,bindingId,profileId,notBefore,
 retiredBranch?:label}
```

Return the same session/generation/native/holder/journal/principal/namespace/
tenant/binding/profile scope, plus:

```text
{version:1,observedUtc,hubSource:HUB_SOURCE,routerSource:ROUTER_SOURCE,piSource:PI_SOURCE,
 native:{conversation,branch,nativeSession,parent?,pending?,reconciledFrom?},
 router:{branch,epoch,operation,state:'complete'|'uncertain',updatedAt:epochMs,
         successor?:label},
 routerStoreNamespace:'inference-session-v1',routerStoreKey:hex64,
 provenance:{projectionSha256:hex64,querySha256:hex64,
             databaseIdentity:label,nativeHistorySha256:hex64}}
```

`HUB_SOURCE`, `ROUTER_SOURCE` and `PI_SOURCE` mean the exact full pins above.
Freshness uses the readiness bounds. The native source is the private
`relay-inference-session-v1` custom entry on the active ancestry in
[Pi's session policy at 2604c41](https://git.chifor.me/cchifor/relay/src/commit/2604c41b69c5d9c50a3324545a51f0bfa38435a0/packages/pi-policy/session.ts).
The Router source is the scoped KV record in
[ReplaySessions at 4e4bb34](https://git.chifor.me/cchifor/llm-router/src/commit/4e4bb34e2f968f9450aabe9cf228d3e70c8bab71/packages/plugins/inference-sessions/service.ts).

The operator reader must validate the owned live/retained native history
identity, parse only this metadata, and query only the corresponding KV key.
It must not export credentials, message text or tool arguments. The core
recomputes `routerStoreKey` as the SHA256 of the UTF-8 encoding of
`JSON.stringify([namespace,principal,native.conversation,router.branch])`.
There is no existing hub API exposing this pair. The fixed read-only operator
adapter is required and remains separate from this core. Query/history/projection
hashes retain provenance, not independent database authentication.

Before recovery, inspect the closed source. After import, its native receipt
must match exactly, and the retained Router branch, epoch, operation and state
must still match with a nonregressing update timestamp. A change refuses before
any continuation or reconciliation prompt. Without a pending receipt, continuation requires a matching
complete parent. With a pending receipt, the Router operation must match either
the pending operation or the complete parent, as required by Pi's reconciliation
guard. This selects explicit reconciliation; it never acknowledges unknown tool
effects. Active, changed, retired or unavailable branch state refuses further
inference.

After reconciliation, `retiredBranch` requests the old Router record. Its epoch,
operation and state must be unchanged, its timestamp must not regress, and its
successor must equal the new native branch. The conversation and native UUID
remain the same; `reconciledFrom` identifies the old branch, and parent/pending
are absent. A success flag alone cannot satisfy these checks.

## Budgets, permissions and evidence limits

Lifecycle retains 15 minutes, at most 12 prompts and 20 observed upstream
generations. Fresh effort retains 10 minutes, at most four prompts and six
observed generations. Prompt timeout remains 120 seconds and output cap 1024.
The successful fixed sequence uses 11 lifecycle prompts and four effort prompts.
The exact strings in `TEXT` preserve the manual packet; no extra probe or B0 is
added.

Before each prompt the core durably reserves two attempts for ordinary text/read,
three for marker/read, two for compaction, and zero for local controls and the
expected configuration refusal. Insufficient remaining allocation refuses
before dispatch; an observed excess stops subsequent work. The separate fixed
fence arms lifecycle rows `ready:2, denied:3, approved:3, branch:2, compaction:2,
compacted:2, cancel:2, recovered:2` under phase limit 20, then effort rows `low:2,
high:2` under phase limit 6. Unused row allowance is retired, never added to the
next row. The unallocated difference between row sums and phase ceilings is
not permission for another prompt or retry. A denied N+1 request can still
create a raw trajectory attempt before admission; that mismatch fails the
phase. Preserve both counters without subtracting the denied diagnostic.

Local history/branch/reconciliation commands and the expected configuration
refusal keep the existing fence and create no new row. Their source-qualified
local behavior and exact zero-attempt observations remain mandatory. This is
**not a server-enforced zero-admission stage**: an unexpected request could
consume unused current-row allowance and must stop the phase.

## Fixed managed-key fence port

[owner-phase-b-fence.cjs](owner-phase-b-fence.cjs) implements the controller for
the [Router candidate contract](https://git.chifor.me/cchifor/llm-router/src/commit/02d698236b6336daee0d5581547636f7cf2b36f3/packages/contracts/attempt-fence.ts),
whose raw SHA256 is
`79316b299abc9b94262de060dda408c2e4f9f419a63f40731b148aaf02af22ee`.
This source reference is not a published deployment pin.

`inspect`, `arm`, `advance`, `close` and `readOperation` each receive one frozen
object `{raw,bodySha256,requestDigest,notBefore,deadline,signal,cleanup}`. The
operator transport signs and sends the **exact UTF-8 raw bytes** through the
existing signed `POST /register/relay-keys` authority. The callback's
`requestDigest` is the JWT assertion binding,
`SHA256("POST\n/register/relay-keys\n" + SHA256(raw))`. The returned receipt's
`requestDigest` is instead `SHA256(raw)`, compared with the durable original
`bodySha256`, including after operation lookup. These two hashes are distinct.
No key/token or signer secret enters the report. `readOperation` uses a fresh
lookup operation ID and the original `targetOperationId`, and returns the
original immutable mutation receipt with its original action/ID/digest.

Every callback returns `{routerSource,observedAt,projectionSha256,response}`.
`response` is the actual strict inspect response or mutation receipt;
`projectionSha256` is SHA256 of `owner-phase-a.js` canonical JSON of that response.
The source must equal the eventual qualified pin. Outer observations begin after
dispatch and remain less than 120 seconds old, at most five seconds ahead.
Receipt commit clocks allow the existing five-second skew. The envelope is
trusted operator attribution, not independent database authentication.

The controller validates key creation/coverage, persistent Router deployment,
namespace/principal, complete immutable plan and selection (including actual
provider registration generation), exact counters/revision, and whole returned
state. Mutation IDs, exact raw body and both digests are saved before calling the
signer. A lost response permits one read of the original operation under the
same deadline, never a mutation resend. Missing/changed/late receipts stop work.
First arm requires no earlier fence and zero token-unknown baseline. Later
settled cancellations retain charged attempts and token uncertainty; every
mutation carries the exact `expectedUnknownUsage` CAS. Unattributed history,
stale opens or missing attempt coverage remain refusals.

Before advancing, the `claude-attempt-drain` checkpoint supplies:

```text
{version:1,observedAt:epochMs,scope:exactContextScope,
 completedOperationIds:exactContextIds,terminalPrompts:exactContextTerminals,
 pendingPermissions:[],pendingTools:[],pendingControls:[],
 nativeRequests:[],clientRequests:[],
 provenance:{nativeSha256:hex64,clientSha256:hex64,operationsSha256:hex64}}
```

The context binds source/deployment, key/principal, tenant/binding/profile,
holder/journal, Relay session/generation, original native UUID, run and fence.
It lists the issued command operation IDs and validated native terminal prompt
receipts. Read the actual native/client pending work and operation evidence;
zero Router opens alone is not proof that a delayed request cannot arrive.
The observation must be collected after the existing five-second boundary.
Unavailable or ambiguous native/client evidence stops advancement. Hashes retain
provenance; the Router does not authenticate the controller's terminal digest.

Normal phase completion closes the exact fence before the next phase can arm
with its closed predecessor ID/revision. On failure, the controller attempts a
bounded irreversible close before ordinary Relay cancel/Close/release cleanup.
A failed or unresolved fence close is retained and does not suppress Relay
cleanup or become a drained-success claim. No new phase follows failure.
The host must track raw port promises/body reads, combine stop/deadline signals,
and include these ports in its bounded cleanup drain. Only inspect, close and
original-operation read are allowed on the cleanup signal.

The existing exact marker commands, full-input permission rereads, independent
filesystem observations and scoped Relay message-read rule remain enforced.
Message reads consume time and generation budgets. Inbox reads can update
`read_at`; this does not authorize acknowledgment, sending or additional
channels. Effect counts must survive branch/compact/recovery without replay.
Changing low to high effort must first produce the exact configuration refusal
with no observed upstream attempt, followed by explicit branch reconciliation.
A generic failure cannot substitute for that refusal.

The historical pre-fence core qualification passed 117 tests: 104 unchanged Phase A cases and 13
Claude cases through the actual public entry, with no Runner/start overrides.
After correcting only the Claude handoff instructions, all 13 Claude cases
passed again. They cover three launches and native UUID retention, direct and
reconciled recovery, fresh effort, permissions, provenance/configuration drift,
durable intent and accounting failures. The final shared-host compatibility
suite also passed all 145 cases (104 Phase A driver, 24 host, seven socket and
ten renewal); the B host has eight separate negative cases. The complete named
B CLI/host/Runner flow passed over Unix HTTP in 209.7 seconds: three launches,
one native resume, eleven lifecycle and four effort prompts, one approved
marker effect, ten real lease heartbeats and 206 durable snapshots. Its completed
A input and HTTP/readiness/branch/marker/trajectory services are synthetic.
The full Phase A compatibility flow also passed in 297 seconds with the shared
host, preserving the original one prompt/two attempts and all 286 snapshots.

Both full flows retain real observation barriers. A focused regression proved
that a single duration timer can wake before a wall-clock boundary; fixture
collectors now recheck the boundary after each wake without changing observed
timestamps or production validators. Earlier failed full runs remain recorded;
their rejected observation was not retained, so the controlled regression does
not establish their precise cause. These fixtures do not
claim a live subscription run, published-Pi execution, a qualified operator
reader or hard budget enforcement. `phase-b-passed` leaves `completed:false`;
Phase C, paired performance evidence and final cleanup remain outstanding.

The initial fence-core follow-up passed 29 focused Linux cases with zero skips. It
includes the real public entry in isolated pin-only compiled modules, unchanged
held-bundle refusal before I/O, exact ordered rows and durable intents, native/
client drain refusals, cancellation token-unknown continuity, original-operation
readback after lost arm/advance/close replies, save/deadline/stop failures, and
ordinary cleanup even when fence closure is unresolved. The successful synthetic
transcript contains 44 management reads/writes and 12 committed mutations; every
request and receipt also passed the actual frozen Router `Input` and
`AttemptFenceReceipt` parsers. Those shape checks did not verify the HTTP route's
receipt-digest semantics: the initial synthetic transport incorrectly reused the
JWT binding as the receipt digest. The subsequent 301.938-second full synthetic
host pass has the same limitation. Both proofs remain historical; neither proves
compatibility with actual HTTP receipts. The corrected helper compares the
receipt with the original raw-body hash, and the fixture now models that exact
contract.

The correction passed 31 focused Linux cases with zero skips, including two
regressions that failed before the correction: recovering the original body
digest after a lost reply, and refusing a JWT binding substituted into the
receipt. A separate test against real signed Router HTTP, SQLite, KeyMeter and
the same controller passed two cases on integrated candidate
`0bc835ab72fb9ea03b2b3ea51bba7a9789a5e171`. It traversed all eight lifecycle and
two effort rows, committed twelve mutations, and recovered one lost committed
reply by its original operation ID without resending. Its issuer and terminal
evidence are synthetic, with zero provider opens.

The corrected full synthetic host then passed in 301.950 seconds with zero
skips, retaining real Unix HTTP, five-second observation barriers, twenty-second
heartbeats, fsynced snapshots and normal cleanup. It completed three launches,
eleven lifecycle plus four effort prompts, twelve fence mutations and thirteen
lease renewals. All thirteen qualified source files were unchanged during the
run; all 44 management requests and twelve receipts passed the actual Router
schemas and raw-body digest checks. This host test uses synthetic fence,
readiness, branch and terminal observations. The public release gate remains
NULL; no final published release, live authority or deployed native/client drain
qualification is claimed.
