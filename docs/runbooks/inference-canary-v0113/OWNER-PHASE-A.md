# Owner-run phase A

This driver is under qualification. **Do not run it against the deployed e843
holder:** that holder drops Pi's tool-input message from the permission context.
The full-command review fix and a distinct configuration-refusal notice must be
reviewed, packaged, qualified and staged before this script is released for use.
Its holder identity and runtime/profile pins must then match the operator's new
handoff. An empty permission description is a stop, never an approval shortcut.
The checked-in draft has `QUALIFIED_INPUTS = false`: Start is disabled, and the
runner independently refuses before any request. Do not flip that flag locally.
The reviewed successor packet must replace the historical pins, profile revisions
and definition hashes and carry its published-artifact qualification first.

The owner can paste the entire reviewed [owner-phase-a.js](owner-phase-a.js) into
the browser console at `https://relay.chifor.me`. It installs a panel without
making requests. Start runs Codex lifecycle, Qwen lifecycle, then fresh Codex
effort, in that order. This is an operator-assisted driver: it constructs and
polls requests, renews the lease every20s and checks receipts. It does not pretend
that the browser can observe private host markers or Router trajectories.

Use the owner's existing authenticated session. No token, cookie, key file,
remote script or browser-console security override is needed. Keep the tab
visible and do not run other controls for these sessions. A second invocation or
saved run cannot silently restart work. The panel deliberately does not offer a
"clear and retry" button.

## Before Start

The operator must provide the current deployment, holder and route handoff from
[activation](ACTIVATE-CODEX-QWEN.md). The holder starts with no sessions; its hello
precedes the first real launch. Credential rendering and installation occur
after the launch is offered and must complete before the first prompt. There is
no probe launch. Missing or drifted profile definitions stop the driver; it does
not approve or replace a profile.

The first enabled-driver checkpoint separately records the current holder
incarnation and journal ID. A stopped upgrade changes the incarnation while the
journal can survive; never copy the historical incarnation into both fields.
The operator must verify `nativeRecovery: 2`, `nativeStateReady: true`, the
qualified v0.13.1 release and explicit maintenance-seal release before Start.

The current two grants expire **2026-11-26 21:27:37 UTC**. The driver never renews
or reissues them. Its assigned run UUIDs are fixed, so an existing marker cannot
be hidden by choosing a new run. Its source assertions are for the hub/API
contract, not a claim that reading API metadata authenticates a remote binary.

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
snapshot establishes the exact scope/window; every later snapshot must use the
same lower bound, namespace, principal and route. Confirm isolated scope,
continuous recording, retained history and stable process/database separately.
The driver rejects unsettled/truncated snapshots and changed baseline rows.

After Open, the separate delivery checkpoint accepts:

```json
{ "marker": {}, "deliveryConfirmed": true }
```

For subsequent observations use `{"trajectory":{},"marker":{},"coverageConfirmed":true}`;
omit `marker` only when the panel says none is needed. A successful known READY
request must appear in the recorder before zero-attempt claims are used.

## Decisions and limits

The lifecycle budgets remain15min,12 prompt commands,20 observed generations,
120s per prompt, and30s per permission decision. Effort remains10min,4 prompts,
6 observed generations and120s per prompt. Output cap1024 stays in the approved
profile. Operator evidence collection consumes the same wall-clock budget.
Recorded-attempt limits are observation gates, not a new server-enforced hard
generation cap; in-flight work and recording latency remain limitations.

Only the exact fixed marker command is eligible for an owner once-only decision.
The driver displays the whole reviewable input and refuses changed commands,
unexpected tool arguments, missing descriptions, expired requests and ambiguous
options. It never selects an always-allow option. Permission delivery is checked
separately from the marker effect.

The branch chosen from `/relay-history` is a completed turn in the same serial
session. Recovery is a clean close and one resume under the same living holder,
not a whole-holder restart. The source launch receipt is saved before prompting;
native UUID, holder incarnation/journal and router principal must be retained.
An explicit panel decision is required before reconciliation. Unknown tool-effect
acknowledgement is never inferred or sent.

## Failure, evidence and cleanup

Stop prevents further prompts. Cleanup cancels pending permissions and an active
turn, waits for the original operation outcome, then requests Close and verifies
session closure. It releases only its own lease; a lease already retired by a
verified Close is recorded correctly. Unknown dispatches retain their original
operation ID and body. Cleanup does not relaunch, retry a prompt, reconstruct a
branch, rotate a credential or take over another lease.

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
is outside this action. The checked-in draft remains disabled until qualified
successor inputs are reviewed.

The panel inventories both bindings and every credential generation, refuses
truncated inventories, gaps, active consumers or changed current credentials,
and shows the exact binding IDs, revisions and generations for confirmation.
Paste the displayed `targets` array unchanged with
`confirmRevokeAllGenerations:true` and `noFurtherUse:true`. The normal admin API
revokes **all generations of each binding**, not just its current key. Accepted
inference can finish; revocation is not proof that earlier work stopped.

Each operation ID and body is saved before its POST. If the response is lost,
the driver reads the same operation from the oldest credential generation; it
does not resend or create a replacement operation. It waits at most120s per
binding, within the existing credential window. It requires all generations to
be revoked, escrow cleanup complete, the binding disabled at the expected
revision, and the exact operation confirmed. An unresolved outcome stops this
action and leaves its original IDs for owner inspection. A second click/reload
cannot restart revocation. Use [live acceptance](LIVE-ACCEPTANCE.md) for a
reviewed manual continuation when needed.

`cleanupPending:false` describes secure escrow cleanup, **not host renderer
cleanup**. A separate final checkpoint requires operator evidence for all five
recorded launches: matching launch/session/binding/credential/generation IDs,
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
      "generation": 1,
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

`launches` must include every recorded launch; each marker entry is the actual
JSON report object, not a string. Collect the file and marker observations after
the displayed boundary, which includes a conservative5s clock-skew allowance.
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
