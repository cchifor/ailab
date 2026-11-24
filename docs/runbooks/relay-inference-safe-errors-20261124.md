# Combined inference and safe-error router release

Status: prepared, not deployed. Owner approval at the exact production-pin head,
green CI and reviewer approvals are required before merge. Coordinate with the
owner of AILab #1970: its `dd020272` release excludes the inference implementation
already deployed by #1967. This release retains both changes; it does not modify
the other release branch or PR.

## Immutable provenance

- Reviewed router main: `a16a6846e89d6971d9549bac5ff3a7dde99b361b`, merging
  inference #196, safe-error classification #197 and timeout classification #199.
- Archive: `router-0.1.0-20261124-inference-safe-errors-r2.tar.gz`, Relay v0.2.0
  asset 1102; SHA-256
  `187ee75d73b75bd87ece82ef164fb9aea43c0551e8538bf3d5ec77f03ea031d6`.
- [Publication run 147751](https://git.chifor.me/cchifor/relay/actions/runs/147751),
  packaging source `d152e35`, receipt `deployment-images-147751.json` on Relay
  v0.2.0. Only its router image is intended for deployment.
- Image: `registry.chifor.me/llm-router/router@sha256:1d3eaf5671cb85be3720eab7a65a87acc1aa3a33f283ef88a21580ea78fb8167`.
- Runtime: pinned Node 26.10.0. Frozen production pnpm 10.32.1 installation with
  install scripts disabled. Production smoke and publication backup tests pass.

Full verification on the exact merged source passed 3,257 tests (49 gated and
3,208 general), with 20 skipped; typecheck, build and production smoke passed.
The initial combined release exposed a timeout classification race. Investigation
reproduced a router-owned response close reaching the client-abort listener before
iterator cleanup, incorrectly recording CLIENT_CLOSED. Reviewed router #199
fixes this by detaching the handler's abort listeners before deadline destruction.
Its deterministic regression checks TIMEOUT/MAX_DURATION, released engine capacity,
an aborted upstream signal and generator cleanup. Issue #198 is resolved by #199;
this r2 archive replaces the intermediate artifact that lacked the fix.

Relative to deployed source `300856ff`, this includes reviewed #197 and #199.
No schema, account, route or
credential migration is introduced. The original backup init container retains
its label and mount restrictions; the new image verifies the historical COMPLETE
snapshot without overwriting it. Preserve that snapshot and all live receipts.
The backup implementation, backup tests and publisher are unchanged between
packaging commits `b0336729fbd9915379246df3e64b616197736642` (deployed image) and
`d152e35` (this image); an exact Git diff of those three files is empty.
The backup script's Git blob is `021ead1128c3d0fe4688c893c810a9794f5216fb`
in both. Both images already contain inference sessions; this is not a change
from a pre-inference backup implementation to a post-inference one. Publication
147751 again tested the same script at `/app/deploy/backup-router.mjs` inside
the newly built image, including repeat verification of the historical snapshot.

## Rollout and acceptance

1. Preserve the existing route baseline and refresh readiness/configuration
   readback. Record the saved and active `subscription-login` CLI settings.
   Native Messages requires Claude 2.1.283. If staging that version through the
   normal administrator settings API, use its current revision and preserve all
   other values; confirm saved 2.1.283, active 2.1.281 and `pending: true` before
   the rollout. This setting takes effect on restart, not through plugin toggles.
   Staging completed on 2026-11-24 at 10:15 UTC: saved 2.1.283, active 2.1.281, pending true,
   revision `974be1b47ad7507dd2128e786b778907bb2cc9936aea0e6a68114a6eca02bc62`.
   All other settings were preserved. Re-read this state before rollout; do not
   repeat the staging write blindly. Messages and warm retention remain disabled.
2. Owner-approved merge triggers Flux. Require the exact image/source above,
   successful backup init, one ready pod, and convergence after reconciliation.
   Confirm saved and active CLI versions agree after restart. Do not restart a
   second writer or clone subscription credentials for qualification.
3. Repeat existing Claude streaming Chat, native Codex Responses and Qwen Chat
   baselines; compare `/v1/routes` to the saved baseline. Verify budget file links
   and the canonical authority-available log line. Preserve inactive hub budgets;
   File B remains unqualified for future worker-budget use.
4. Require `/app/dist/packages/kernel/safe-error.js` in the running image. Read the
   whole new pod log successfully and apply the [log checks below](#log-checks);
   report only pass/fail, never matching secret text.
5. Enable native Messages only with active CLI 2.1.283 and warm retention off.
   Use a temporary fixed-model Claude route with one deployment, maxAttempts 1,
   no fallback and a disposable scoped key. Run the published bounded session
   canary; revoke the key, require 401/403 and remove temporary routes/key files.
6. Complete the dedicated live Relay/Pi profile acceptance and warm-to-cold
   rollback in [the inference rollout](relay-inference-sessions-20261124.md).
   Router-only acceptance does not replace the paired Relay release or its tests.

## Log checks

Use the complete new router container log from pod start. A failed log read fails
this gate; an empty result from a failed command never counts as clean. Require
one `Worker budget authority available` line and no noncanonical budget-authority
message. Fail on any match of this secret-shape expression, without printing the
matching line:

```text
postgres(ql)?://[^ ]*:[^ ]*@|Bearer [A-Za-z0-9._-]{8,}|sk-[A-Za-z0-9_-]{8,}
```

This checklist is retained here because the separate
[safe-errors release runbook](https://git.chifor.me/cchifor/ailab/src/commit/204bdf309024fcdd08df99c328f962fab8382caa/docs/runbooks/llm-router.md#safe-error-classifications-release-2026-11-24-llm-router-194)
is on the unmerged #1970 branch. Do not use that branch's older image or rollback
pins. A clean log alone does not exercise the error paths: also verify this
release's exact image/source and the installed safe-error module in step 4.

## Rollback

Stop canary launches and drain active inference; disable warm retention first.
Preserve native history and all admitted receipts. The immediately previous image
is `registry.chifor.me/llm-router/router@sha256:370bf5bf7f03da92b728e92fcc62b06439f886730a8a670d13be0d84924f1909`,
source `300856ff7881eba67d2081650fca9f66a7f26b66`, release
`router-0.1.0-20261124-inference-sessions`. It retains the inference schema but
loses #197's logging hardening and #199's timeout fix, so prefer recovery forward when practical.

Revert image/source/release pins (including the init image) through the reviewed
owner-approved GitOps path. Both binaries support CLI 2.1.283; retaining that pin
preserves Messages capability. Returning to CLI 2.1.281 requires first stopping
native Messages launches and saving the prior settings before the controlled
restart. Never restore the pre-canary database over admitted operations merely
to roll back an image or CLI pin.
