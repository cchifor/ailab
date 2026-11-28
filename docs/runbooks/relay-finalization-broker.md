# Cleanup-only canary broker

The reviewed `canary-finalization` manifest selects a separate session purpose.
It validates the original reports with the actual finalizer before acquiring a
Lease or granting authority. The proxy permits inventory and credential-detail
reads, GETs for the reports' exact historical sessions and controls, and at most
one revoke for each of the three fixed canary bindings. It has no issue, rotate,
launch, resume, command, permission-decision or control-write route.

Run the existing operator session command with a reviewed version-2 manifest,
the pinned bundle, and these additional arguments:

```text
--finalization-inputs /absolute/private/inputs
--finalization-plan /absolute/private/operator-plan.json
--finalization-plan-sha256 <independently-retained-raw-plan-sha256>
```

The input directory contains exactly `a.json`, `b.json`, `bInputs.json`,
`c.json`, `decision.json` and `manifest.json`. Add `d.json` only when the
finalizer manifest explicitly selects `activeDrain: true`. Preserve every
report's original UTF-8 bytes. The operator plan is a separate file; its digest
must come from the retained, authorized plan rather than from the Runner.

The operator plan has exactly `version: 1`, `purpose: "canary-finalization"`,
`runId` (a fresh retained UUID), `reportPins`, `decisionSha256`, `cleanupDeadline`
(epoch milliseconds), and `qualificationSha256`. The latter is the SHA-256 of
the canonical, recursively key-sorted JSON for the pure validator's `sources`.
The decision has the same version, purpose, runId, reportPins, deadline and
qualification digest, plus the sorted three `bindingIds` and
`noFurtherUse: true`; it has no `decisionSha256` field. The finalizer manifest
contains `version: 1`, `pins`, `cleanupDeadline` and `decisionSha256`, plus
`activeDrain: true` when D is selected. These digests bind the records; the
operator must independently retain the actual no-further-use authorization.

The cleanup deadline T must be fresh and at most fifteen minutes away. It is
independent of the expired inference window. Both the Runner and proxy stop
canary calls at T, including reads and revokes. The existing T+600-second
authority-teardown allowance does not extend those routes. An uncertain or
non-successful revoke keeps its operation and binding reservation; the Runner
may inspect original operation evidence but cannot resend the mutation.

Before authority, the broker keeps an exclusive, fsynced authorization claim in
`relay-operator/finalization-ledger` under LOCALAPPDATA, or `~/.local/state`.
Any later attempt to consume that run UUID refuses. Keep the claim and all
reports after failure; reconciliation must precede a separately authorized
future run. Newly created ledger ancestors are synced on Linux. Windows ACL
and power-loss durability remain a separate qualification requirement; the
observer rejects a staging directory that does not meet its private-ACL check.

The bundle pins the fixed `owner-finalization-entry.cjs`, its complete module
dependencies and `scripts/relay-finalization/client.cjs`. The observer manifest
pins that identical client, the new finalization observer, the existing framing
module and Gate, the fixed hub/Router/host readers, and their exact configuration
and referenced host-key files. Only those verified copies are staged. A cached
Phase A module cannot replace the pinned framing module. Finalization never
calls Phase A preparation, credential renewal or holder activation.

`inference-canary-v0113/finalization-bundle.example.json` is deliberately
non-executable: null pins must not be treated as a release. Both compiled source
qualification gates remain closed until the actual deployment, including the
retired-binding Hub fix, and the complete broker/observer/CLI composition are
qualified. Synthetic unit tests and local Hub PostgreSQL integration establish
their stated boundaries; they do not establish live authorization or readiness.
