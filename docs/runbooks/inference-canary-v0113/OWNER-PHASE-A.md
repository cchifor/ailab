# Owner-run phase A

This packet retains published **v0.13.1** holder/Pi source
`2604c41b69c5d9c50a3324545a51f0bfa38435a0` and first-registration profile revision
1. The profile file SHA256 is
`928d41f0b24c0d1d2493458e7fac23cfd686c045b1aa9daaee480684a0aea9a4`.
Fresh published-holder actual-root readiness, compiled e843 schema checks
(31 positive/5 negative) and normalization passed for these exact revision-1
bytes. Nine offline compiled registration-boundary cases passed: each profile
accepts revision 1 with empty history and refuses revision 3 with empty history
or revision 1 over existing revision 3. These use strict fake database fixtures,
not live API authority or real database transactions. The runtime artifacts
are unchanged; the earlier five synthetic wire cases remain runtime evidence,
not live subscription acceptance. Evidence is recorded in [README](README.md).

This post-expiry successor targets renewal-capable hub source
`74d1d9f4ab2250fe6a6abe5a2e0bebce2ccc4613`, the reviewed release merge in
[Relay PR717](https://git.chifor.me/cchifor/relay/pulls/717). Its enabled paths are
qualified offline using this actual source pin; `QUALIFIED_INPUTS` does not
establish publication, deployment, owner authority or a live acceptance result.
Before use, require successful release publication, the reviewed hub rollout and
the operator's fresh deployment readback for that exact source. The bootstrap
then verifies both renewed credential references through the normal API before
any replacement launch. Do not edit the source pin or qualification flag locally.
The earlier merged 7dff6049 driver was qualified only for the original credentials
and their November 26 deadline; it cannot be reused after renewal. Qualification
does not perform profile approval or authorize live work by itself. The previously
installed revision-3 profile hash remains rejected.
The empty-history requirement and the 14:26 absence readback belonged to the
historical first-registration stage. Codex and Qwen were subsequently approved
at revision 1. The current replacement must verify those existing exact enabled
revision-1 definitions and normalized digests in [README](README.md), in the same
hub tenant. It must not demand empty history, reapprove the profiles, downgrade
revisions or invent an approval chain. Missing, disabled or drifted definitions
stop the driver; approval remains a separate owner/API action outside it.
Start still requires the operator's fresh installed-hash
handoff and router rollout below. Do not run it against the old e843 holder,
which drops Pi's tool-input description.
The historical report names the e843 hub. The successor requires the separately
deployed renewal-capable hub named by `RUN_SOURCE`; schema 93 and the installed
holder/Pi artifacts remain unchanged. The operator must verify that deployment
before the credential metadata review. An API response is not an independent
measurement of the deployed binary.
An empty permission description is a stop, never an approval shortcut. Do not
edit pins, revisions, qualification flags or run UUIDs locally.

The owner can paste the entire reviewed [owner-phase-a.js](owner-phase-a.js) into
the browser console at `https://relay.chifor.me`. It installs a panel without
making requests. This successor enables only the reviewed stopped-run replacement
described below; the initial **Start phase A** button stays disabled even if
browser storage is empty. After its explicit review and checkpoints, the driver
runs Codex lifecycle, Qwen lifecycle, then fresh Codex effort, in that order.
This is an operator-assisted driver: it constructs and
polls requests, renews the lease every 20 s and checks receipts. It does not pretend
that the browser can observe private host markers or Router trajectories.

Use the owner's existing authenticated session. No token, cookie, key file,
remote script or browser-console security override is needed. Keep the tab
visible and do not run other controls for these sessions. A second invocation or
saved run cannot silently restart work. The panel deliberately does not offer a
"clear and retry" button.

The communication-permission correction does not automatically reopen a saved
failed or stopped run. Keep its local storage, exported report, operation IDs
and fixed marker UUIDs. The replacement below covers only the known first Codex
prompt stopped at the Relay read permission, with all six recorded operations
received, its permission cancelled and session closed with control released.
Other saved runs, uncertain operations or additional work require separate
inspection; do not clear storage, edit a report or change identities to qualify.
The stopped 4af report remains historical evidence. Its one prompt and two
attempts stay charged to the Codex budget; its trajectory rows never become the
new 4e4 baseline or a cross-source delta.

## Before Start

The [stopped-idle upgrade](UPGRADE-IDLE.md) and revision-1 staging were completed
before the original run. That helper requires a journal with no command or
session ever; this holder has now run commands and is ineligible. Do not repeat
the helper, restage profiles, restart the holder or erase its journal for the
replacement. After normal packet review and merge, verify the current holder
and existing exact enabled Codex/Qwen definitions. Router
[PR2029](https://git.chifor.me/cchifor/ailab/pulls/2029) must be deployed and read back at source
`4e4bb34e2f968f9450aabe9cf228d3e70c8bab71` before any reviewed replacement Start;
its source merge and published image alone do not prove deployment. The
operator-controlled rollout comes first, after prior Phase A work has settled.
It does not restart that run or assign the replacement window.
The operator must provide the
current deployment, holder and route handoff using the readback requirements in
[activation](ACTIVATE-CODEX-QWEN.md), with the previous session closed and no work
in flight. The original empty-holder startup preceded that first launch; it is
not a request to run activation again. Credential rendering and installation for
the replacement occur after its launch is offered and must complete before its
first prompt. There is no probe launch. Missing or drifted profile definitions
stop the driver; it does not approve or replace a profile.

After replacement review, the patched-holder checkpoint separately records the
current holder incarnation and journal ID. A stopped upgrade changes the incarnation while the
journal can survive; never copy the historical incarnation into both fields.
The operator must verify `nativeRecovery: 2`, `nativeStateReady: true`, the
qualified v0.13.1 release and current resumed admission with the maintenance seal
already released before Start. This is a readback, not a new maintenance cycle.
The handoff must also include `profilesSha256` and `holderBinarySha256`, measured
from the installed profile file and holder binary. The driver compares both to
the exact values in [holder-inputs.json](holder-inputs.json) before any new launch;
a release label alone is insufficient. Paste the operator's actual readback,
not expected values copied from this guide. The saved report attributes these
hashes to the operator; the browser does not measure host files.

The original two credentials expire **2026-11-26 21:27:37 UTC**. Their identities
and deadline remain immutable historical inputs. The owner is unavailable until
after that deadline, so this successor must use a separately authorized new
generation behind each of the same two bindings. The driver never renews,
reissues or extends credentials. It reads only nonsecret metadata after the
owner explicitly begins review, checks the fixed binding/host/grant selection,
and asks the owner to confirm the actual replacement identities. Its effective
deadline is bounded by the earlier of the two new expiries. Re-reading metadata
must still identify the same generations before a new launch.

Expiry alone does not make the deployed e843 rotate API usable: normal expiry
cleanup can leave an enabled binding failed with both credential pointers empty.
The reviewed server correction is
[Relay PR716](https://git.chifor.me/cchifor/relay/pulls/716). After its release is
deployed, a fresh `canRenew === true` observation permits the owner to review the
ordinary rotation operation; the server rechecks eligibility and authority.
An installing credential must first complete normal expiry revocation and escrow
cleanup. Retry resumes its existing operation and cannot extend its lifetime.
Disabled, explicitly revoked, uncertain, worker-managed or otherwise ineligible
bindings still refuse. Do not race expiry reconciliation or change state to make
renewal possible. Publication, deployment and renewal are distinct steps.

Both renewals must use fresh retained operation UUIDs and the observed binding
revisions through the owner's normal authenticated API workflow. Preserve their
receipts and new credential metadata. A 202 response is not installation proof:
the fresh normal launch offers renderer delivery, and render/install/Open must
complete before a prompt. No probe launch is needed. The fixed profile revisions,
paths, holder journal, marker UUIDs and prior one-prompt/two-attempt accounting
remain unchanged; the old principal is used only for historical accounting.
Do not edit `EXPIRES`, old credential IDs, the saved report or local storage.

The supported successor still runs Codex lifecycle, Qwen lifecycle and Codex
effort together. It does not offer a Codex-only completion or reset the budget
while Qwen is reconciled. If either replacement is not ready for this workflow,
the metadata review refuses before a new launch. There is no partial Phase A
completion claim.

Obtain the operator's observations immediately around each checkpoint. The
marker tool prints JSON on stdout; preserve distinct pre-launch and post-open
files without overwriting either. Use the same UUID and marker filenames. Only
the timestamp may differ between successful baseline reports.

The panel accepts these nonsecret JSON envelopes, assembled from the existing
observer and collector outputs:

```json
{
  "trajectory": {},
  "marker": {},
  "coverageConfirmed": true,
  "holderReadyConfirmed": true,
  "routeConfirmed": true
}
```

Replace the empty objects with the actual fresh reports. The initial trajectory
snapshot establishes the exact scope/window; later snapshots for that run must
use the same lower bound, namespace, principal and route. The replacement's
historical accounting and new baseline are separate observations as described
below. Confirm isolated scope,
continuous recording, retained history and stable process/database separately.
The driver rejects unsettled/truncated snapshots and changed baseline rows.

After Open, the separate delivery checkpoint accepts:

```json
{ "marker": {}, "deliveryConfirmed": true }
```

For subsequent observations use `{"trajectory":{},"marker":{},"coverageConfirmed":true}`;
omit `marker` only when the panel says none is needed. A successful known READY
request must appear in the recorder before zero-attempt claims are used.

## Review the known stopped-run replacement

These are preparation instructions, not evidence that the replacement is ready
or authorized. Normal packet review/merge, the renewal-capable hub deployment,
actual Router 4e4 rollout readback, separate owner renewals of both bindings and
an explicit owner-assigned window remain required. No window is assigned by
loading this script or accepting the source change.

1. In the original authenticated owner browser, download and preserve the stopped
   report with **Download sanitized report**. Once the original action and cleanup
   have settled, reload the page without clearing site data or local storage and
   paste the complete newly reviewed script. Keep the same browser profile and
   fixed marker UUIDs. An absent or different saved report leaves both launch
   choices disabled; do not import edited data or bypass that check.
2. Choose **Review stopped-run replacement**. The driver retains the original
   raw report inside the successor record under the same storage key and binds it
   with a SHA256 of its canonical JSON. The two renewed-credential review steps
   come first: confirm the operator's actual deployed hub source and an observation
   collected during this review, then confirm the exact nonsecret references shown
   by the panel. The driver reads the inventory and both credential details twice,
   requiring matching references. Both decisions and all six GETs share a 120-second
   deadline. This reads metadata only; it never issues or rotates a credential.
   It then reads the original session and control, all three original
   prompt/cancel/Close operation receipts and the
   cancelled permission. These checks must confirm settled native outcomes, a
   closed session and no control lease before a new launch can proceed. Download
   the successor report too, including if preflight stops; the original remains
   nested and must not be removed to retry.
3. At **Assign one bounded replacement for the stopped Codex run**, supply the
   displayed nonsecret evidence envelope with `confirmReplacement:true`,
   `reportDownloaded:true`, `operatorVerified:true` and the displayed
   `priorReportCanonicalSha256`. Use the owner's actual `windowStartUtc` and
   `windowEndUtc`: the start must be at most two minutes old, the window at most
   15 minutes and its end no later than the earlier renewed credential expiry. This is
   a new explicitly assigned window, not an extension of the old deadline.
4. Supply the freshly collected 4e4 `trajectory` with the **original** scope and
   `since`, plus a fresh `marker` baseline after the displayed collection
   boundary. The historical snapshot must contain exactly the two known settled
   original requests and two attempts, with their expected complete/cancelled
   outcomes. Both marker files must remain absent with unchanged identities and
   commands. Extra history, missing visibility or changed markers stops preflight.
   Reading those old rows on 4e4 does not relabel the preserved 4af report.
5. Continue through the patched-holder handoff and normal lifecycle checkpoints.
   Before the new Codex launch, collect a **separate, later, empty 4e4 observation
   window for the newly selected Codex principal** and baseline as requested by the panel.
   The original principal remains confined to historical accounting. Repeated
   metadata checks before every launch must match the selected references;
   normal installation may move an unchanged generation from installing to ready.
   Keep the new evidence separate
   from the historical accounting snapshot. The new Codex run starts with one
   prompt and two attempts already charged against the existing 12-prompt and
   20-generation totals; they are not erased by the new window. Qwen and effort
   retain their existing budgets. Final renderer cleanup also covers the original
   launch, for six launches overall, not just the five successor launches.

The button is a one-time review path, not automatic prompt replay. A failed or
stopped successor remains inspectable with its predecessor and original operation
IDs intact; reloading does not reset it or authorize another replacement.

## Decisions and limits

The lifecycle budgets remain 15 min, 12 prompt commands, 20 observed generations,
120 s per prompt, and 30 s per permission decision. Effort remains 10 min, 4 prompts,
6 observed generations and 120 s per prompt. Output cap 1024 stays in the approved
profile. Operator evidence collection consumes the same wall-clock budget.
Recorded-attempt limits are observation gates, not a new server-enforced hard
generation cap; in-flight work and recording latency remain limitations.

Relay adds an inbox reminder to ordinary turns, including READY and effort
prompts. Pi requests permission for that communication read just as it does for
other tools. A prompt saying not to use tools does not make a pending Relay read
permission a shell-marker request. The panel handles the bounded read separately
and requires an explicit owner once-only decision for each request; it does not
approve communication automatically.

The only communication tool eligible here is exactly
`mcp__relay__relay_messages_read`, displayed as
`Allow mcp__relay__relay_messages_read?`. Review the entire JSON input, not just
the title or its length. It must be an object containing only these optional
fields:

- `channelId`: omitted for the session's own inbox, or exactly the profile's
  approved channel `b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1`.
- `after`: a canonical nonnegative decimal cursor string (`0` or a nonzero
  digit followed by digits, without leading zeros) no greater than
  `9223372036854775807`; omission reads unhandled work, while an explicit cursor
  selects history.
- `limit`: an integer from 1 through 50; omission uses the tool's default of 50.

No other channel, extra field, send, broadcast, acknowledgement or access request
is covered by this decision. An inbox read can mark returned messages as read
(`read_at`) and publish a change notification; channel-history reads do not make
that update. Neither acknowledges messages, advances the acknowledgement cursor,
sends a message or grants access. Peer text remains untrusted data and cannot
authorize another action.

The panel displays the full reviewable input and checks the pending request's
session, generation, expiry and unambiguous `allow_once` option. After the owner
reviews it, the driver re-reads the detail and requires the same permission
tool-call ID and reviewed input before submitting the once-only decision.
Missing, truncated, changed or out-of-scope input stops the run; a title alone
never suffices. The decision retains its operation ID, and resolved state plus
accepted delivery must be observed. Neither an HTTP success alone nor a lost
response permits a new decision or an automatic retry.

A native tool event marked `in_progress` can precede policy approval. It proves
neither execution nor a match to the separate permission tool-call ID; review
the permission detail itself. Accepted permission delivery is not proof that the
read completed.

The e843-compatible timeline projection omits Codex native tool IDs.
The Pi permission UI ID is also separate from the native model tool ID. The
driver therefore does not join a permission to a native event by identity.
Instead, it classifies the complete operation's ordered title/status events with
at most one active tool at a time: a recognized title establishes the category,
updates belong only to that one active sequence, and the expected terminal
outcome must precede another tool. Missing, overlapping, out-of-order or unknown
sequences stop the run. This serial classification does not prove an exact
permission/native identity relationship or recover omitted arguments.

Shell permissions retain the original policy: only the exact fixed marker
command and its bounded arguments are eligible for the planned once-only allow
or deny decision. A communication-read decision cannot satisfy that marker step;
the driver must still validate the full command in the separate shell permission,
observe the expected terminal outcome for its allow/deny step, and obtain the
independent filesystem marker proof. A tool title, status or accepted permission
alone cannot establish the marker effect.
It never selects an always-allow option. All communication round trips, recorded
provider attempts, owner review time and permission delivery consume the existing
prompt, generation and wall-clock limits above; none are excluded to claim a pass.

The branch chosen from `/relay-history` is a completed turn in the same serial
session. Recovery is a clean close and one resume under the same living holder,
not a whole-holder restart. The source launch receipt is saved before prompting;
native UUID, holder incarnation/journal and router principal must be retained.
An explicit panel decision is required before reconciliation. Unknown tool-effect
acknowledgement is never inferred or sent.

## Failure, evidence and cleanup

Stop rejects the active checkpoint and prevents further prompts. It waits for
the one active start or revocation action to settle before cleanup, so a late
launch response or prompt POST cannot race a stale cleanup snapshot. An open
session without an owned lease, or an uncertain launch, is explicitly recorded
for operator inspection and Close; cleanup never acquires or adopts a lease.
Cleanup cancels pending permissions and an active
turn, waits for the original operation outcome, then requests Close and verifies
session closure. It releases only its own lease; a lease already retired by a
verified Close is recorded correctly. Unknown dispatches retain their original
operation ID and body. Cleanup does not relaunch, retry a prompt, reconstruct a
branch, rotate a credential or take over another lease. Before each cleanup
command that carries a lease, it checks the existing lease expiry; it does not
renew or reacquire control. If settlement consumed the lease window, Close is
not dispatched and the report explicitly records the expiry. Cleanup records
the failing step, a sanitized reason and the original operation ID where one
was dispatched. POST records distinguish received responses from uncertain
responses; uncertain cancel/Close dispatches inspect only the same operation,
without resending.

A browser cannot guarantee cleanup after tab termination, connectivity loss or
expired authority. The report lists unresolved operations for the owner/operator.
Download it before leaving. Local storage retains only the fixed acceptance
requests, nonsecret identifiers and projected evidence; it does not store tokens,
raw provider bodies, thinking text or arbitrary tool output. Input observations
are projected before export. An existing saved run opens in inspection mode.

After all three phase-A runs pass and their sessions close, the separate **Review
final grant revocation** button becomes available. It never runs automatically
on success, Stop, failure or reload. Keep both grants if later authorized work
still needs them; coordinate that dependency before confirming. Claude's binding
is outside this action. Packet qualification does not authorize revocation or
replace that explicit owner decision.

The panel inventories both bindings and every credential generation, refuses
truncated inventories, gaps, active consumers or changed current credentials,
and shows the exact binding IDs, revisions and generations for confirmation.
Paste the displayed `targets` array unchanged with
`confirmRevokeAllGenerations:true` and `noFurtherUse:true`. The normal admin API
revokes **all generations of each binding**, not just its current key. Accepted
inference can finish; revocation is not proof that earlier work stopped.

Each operation ID and body is saved before its POST. If the response is lost,
the driver reads the same operation from the oldest credential generation; it
does not resend or create a replacement operation. It waits at most 120 s per
binding, within the existing credential window. It requires all generations to
be revoked, escrow cleanup complete, the binding disabled at the expected
revision, and the exact operation confirmed. An unresolved outcome stops this
action and leaves its original IDs for owner inspection. A second click/reload
cannot restart revocation after any intent has been saved. A declined confirmation
or inventory refusal before the first intent is recorded as
`declined-before-dispatch` and permits a fresh inventory and owner review; no
POST was sent. Stop still prevents another action. Use [live acceptance](LIVE-ACCEPTANCE.md) for a
reviewed manual continuation when needed.

`cleanupPending:false` describes secure escrow cleanup, **not host renderer
cleanup**. A separate final checkpoint requires operator evidence for all six
recorded launches, including the original stopped launch: matching
launch/session/binding/credential/generation IDs,
the cleanup command ID and timestamp, receipt status `absent`, and a metadata-only
check that the managed file is absent. `not-current` alone is insufficient. The
browser cannot read private renderer receipts or host files; the exported record
explicitly attributes these observations to the operator.

A launch-bound cleanup receipt can predate final revocation: cleanup may already
have completed. It must be no earlier than that launch, allowing the verified
clock skew. The file-absence observation must still be fresh after the final
checkpoint; a historical receipt alone is insufficient. Final marker hashes and
byte counts must exactly match the last saved recovered-phase observation for
each provider and run, not merely have a plausible one-marker shape.

The checkpoint accepts this shape; copy exact recorded identities from the
panel and supply real observations, never placeholder values:

```json
{
  "operatorVerified": true,
  "observedUtc": "<fresh ISO timestamp>",
  "launches": [
    {
      "launchId": "<UUID>",
      "sessionId": "<UUID>",
      "bindingId": "<UUID>",
      "credentialId": "<UUID>",
      "generation": 2,
      "commandId": "<UUID>",
      "cleanedAt": "<receipt ISO timestamp>",
      "status": "absent",
      "fileAbsent": true
    }
  ],
  "markers": [
    "<Codex recovered observer report>",
    "<Qwen recovered observer report>"
  ]
}
```

The example generation 2 must be replaced by each actual recorded generation:
the five successor launches use the selected renewed generations, while the
original stopped launch retains generation 1 and its original credential ID.
`launches` must include every recorded launch; each marker entry is the actual
JSON report object, not a string. Collect the file and marker observations after
the displayed boundary, which includes a conservative 5 s clock-skew allowance.
The current two marker files must still show exactly one approved effect and no
denied effect. Preserve histories, workspaces and markers; this action does not
delete them. Only after both revocation and separate renderer evidence pass does
the report say `phase-a-and-grants-closed`. Its overall `completed` flag remains
false: Claude, warm and the remaining plan evidence have separate windows.

The report distinguishes live hub/permission/marker/recorder evidence from the
published-Pi synthetic wire qualification. Live outbound effort/cap fields and
opaque replay bytes remain unobserved. Generic backend errors are not proof of a
configuration fence. Any failed, blocked or unexercised step prevents a phase-A
pass and the overall plan remains incomplete.

Offline checks run through `python -m unittest scripts.tests.test_pi_owner_phase_a`
or `node --test docs/runbooks/inference-canary-v0113/owner-phase-a.test.cjs`.
They use injected fetch/time and never contact Relay, a provider or the host.
