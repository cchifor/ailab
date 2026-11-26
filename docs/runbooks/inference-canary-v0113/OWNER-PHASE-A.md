# Owner-run phase A

This packet uses published **v0.13.1** holder/Pi source
`2604c41b69c5d9c50a3324545a51f0bfa38435a0`, revision-3 definitions and profile
file SHA256 `58120839759fdff7c8bcd4ef483c75711558613c1487366d8406de4bceaa3b39`.
Published installation/repeat verification, actual-root readiness, the deployed
e843 hub's compiled schema checks (31 positive/5 negative), and all five native
wire cases passed. The wire cases use Router TypeScript source with synthetic
providers; they do not establish live subscription acceptance. The exact pins
and qualification evidence are recorded in [README](README.md).

`QUALIFIED_INPUTS = true` enables this reviewed packet. **Start still requires
the operator's fresh post-upgrade handoff and the router rollout below.** Do not
run it against the old e843 holder, which drops Pi's tool-input description.
The e843 hub/API and schema 93 stay in place; the holder/Pi update is separate.
An empty permission description is a stop, never an approval shortcut. Do not
edit pins, revisions, qualification flags or run UUIDs locally.

The owner can paste the entire reviewed [owner-phase-a.js](owner-phase-a.js) into
the browser console at `https://relay.chifor.me`. It installs a panel without
making requests. Start runs Codex lifecycle, Qwen lifecycle, then fresh Codex
effort, in that order. This is an operator-assisted driver: it constructs and
polls requests, renews the lease every 20 s and checks receipts. It does not pretend
that the browser can observe private host markers or Router trajectories.

Use the owner's existing authenticated session. No token, cookie, key file,
remote script or browser-console security override is needed. Keep the tab
visible and do not run other controls for these sessions. A second invocation or
saved run cannot silently restart work. The panel deliberately does not offer a
"clear and retry" button.

## Before Start

After normal packet review and merge, the operator must complete
[the stopped-idle upgrade](UPGRADE-IDLE.md), stage the revision-3 profiles and
verify the new holder. Router [PR2019](https://git.chifor.me/cchifor/ailab/pulls/2019)
must be deployed and read back at source
`4af66b8c7c3cc83ddd10e326c554ad728caad72e` before Start; its source merge and
published image alone do not prove deployment. The operator must provide the
current deployment, holder and route handoff from
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
The handoff must also include `profilesSha256` and `holderBinarySha256`, measured
from the installed profile file and holder binary. The driver compares both to
the exact values in [holder-inputs.json](holder-inputs.json) before any request;
a release label alone is insufficient. Paste the operator's actual readback,
not expected values copied from this guide. The saved report attributes these
hashes to the operator; the browser does not measure host files.

The current two grants expire **2026-11-26 21:27:37 UTC**. The driver never renews
or reissues them. Its assigned run UUIDs are fixed, so an existing marker cannot
be hidden by choosing a new run. Its source assertions are for the hub/API
contract, not a claim that reading API metadata authenticates a remote binary.
If either grant expires, rotates or is reissued, stop and prepare a new reviewed
packet with the actual credential/key identities and authorized window before
Start. Do not edit `EXPIRES` or credential IDs locally. The existing delivery
checks also compare live credential, binding, consumer and router-key identities
before the first prompt; a successful source qualification cannot override drift.

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

The lifecycle budgets remain 15 min, 12 prompt commands, 20 observed generations,
120 s per prompt, and 30 s per permission decision. Effort remains 10 min, 4 prompts,
6 observed generations and 120 s per prompt. Output cap 1024 stays in the approved
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
