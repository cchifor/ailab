# Combined inference and safe-error router release

Status: prepared, not deployed. Owner approval at the exact production-pin head,
green CI and reviewer approvals are required before merge. Coordinate with the
owner of AILab #1970: its `dd020272` release excludes the inference implementation
already deployed by #1967. This release retains both changes; it does not modify
the other release branch or PR.

## Immutable provenance

- Reviewed router main: `700c51c160213edd8486a0ca1ecdf11e5b8ebbe2`, merging
  inference #196 and safe-error classification #197.
- Archive: `router-0.1.0-20261124-inference-safe-errors.tar.gz`, Relay v0.2.0
  asset 1099; SHA-256
  `24fdd42885d2f409f9b7d5a2178339f53e2d19dafe73060b25dbe7198a790af4`.
- [Publication run 147708](https://git.chifor.me/cchifor/relay/actions/runs/147708),
  packaging source `b29fe37`, receipt `deployment-images-147708.json` on Relay
  v0.2.0. Only its router image is intended for deployment.
- Image: `registry.chifor.me/llm-router/router@sha256:eeb06e101aefc9202e76b46d9fc210071535ae08bc6633349511c0373259ed84`.
- Runtime: pinned Node 26.10.0. Frozen production pnpm 10.32.1 installation with
  install scripts disabled. Production smoke and publication backup tests pass.

Full verification passed 3,256 tests (49 gated and 3,207 general), with 20 skipped;
typecheck and production build passed. An initial full run failed one existing
stream-timeout status assertion (`cancelled` instead of `failed`). Its entire
52-test file passed separately, then the full verification passed without source
changes or waived tests. Retain both runs as evidence; this is not a claim that
the initial failure was fixed by the logging change.
The unresolved classification race is tracked in
[llm-router #198](https://git.chifor.me/cchifor/llm-router/issues/198), including
the failed assertion, runtime, reproduction evidence and required investigation.
Passing reruns do not establish its cause. The release owner must assess that
record alongside the passing qualification before approving this pin.

Relative to the deployed source `300856ff`, the only source delta is reviewed
#197 (fixed error classifications and their tests). No schema, account, route or
credential migration is introduced. The original backup init container retains
its label and mount restrictions; the new image verifies the historical COMPLETE
snapshot without overwriting it. Preserve that snapshot and all live receipts.
The backup implementation, backup tests and publisher are unchanged between
packaging commits `b0336729fbd9915379246df3e64b616197736642` (deployed image) and
`b29fe37` (this image); an exact Git diff of those three files is empty.
The backup script's Git blob is `021ead1128c3d0fe4688c893c810a9794f5216fb`
in both. Both images already contain inference sessions; this is not a change
from a pre-inference backup implementation to a post-inference one. Publication
147708 again tested the same script at `/app/deploy/backup-router.mjs` inside
the newly built image, including repeat verification of the historical snapshot.

## Rollout and acceptance

1. Preserve the existing route baseline and refresh readiness/configuration
   readback. Record the saved and active `subscription-login` CLI settings.
   Native Messages requires Claude 2.1.283. If staging that version through the
   normal administrator settings API, use its current revision and preserve all
   other values; confirm saved 2.1.283, active 2.1.281 and `pending: true` before
   the rollout. This setting takes effect on restart, not through plugin toggles.
2. Owner-approved merge triggers Flux. Require the exact image/source above,
   successful backup init, one ready pod, and convergence after reconciliation.
   Confirm saved and active CLI versions agree after restart. Do not restart a
   second writer or clone subscription credentials for qualification.
3. Repeat existing Claude streaming Chat, native Codex Responses and Qwen Chat
   baselines; compare `/v1/routes` to the saved baseline. Verify budget file links
   and the canonical authority-available log line. Preserve inactive hub budgets;
   File B remains unqualified for future worker-budget use.
4. Require `/app/dist/packages/kernel/safe-error.js` in the running image. Read the
   whole new pod log successfully and apply the secret-pattern checks from the
   safe-errors release runbook; report only pass/fail, never matching secret text.
5. Enable native Messages only with active CLI 2.1.283 and warm retention off.
   Use a temporary fixed-model Claude route with one deployment, maxAttempts 1,
   no fallback and a disposable scoped key. Run the published bounded session
   canary; revoke the key, require 401/403 and remove temporary routes/key files.
6. Complete the dedicated live Relay/Pi profile acceptance and warm-to-cold
   rollback in [the inference rollout](relay-inference-sessions-20261124.md).
   Router-only acceptance does not replace the paired Relay release or its tests.

## Rollback

Stop canary launches and drain active inference; disable warm retention first.
Preserve native history and all admitted receipts. The immediately previous image
is `registry.chifor.me/llm-router/router@sha256:370bf5bf7f03da92b728e92fcc62b06439f886730a8a670d13be0d84924f1909`,
source `300856ff7881eba67d2081650fca9f66a7f26b66`, release
`router-0.1.0-20261124-inference-sessions`. It retains the inference schema but
loses #197's logging hardening, so prefer recovery forward when practical.

Revert image/source/release pins (including the init image) through the reviewed
owner-approved GitOps path. Both binaries support CLI 2.1.283; retaining that pin
preserves Messages capability. Returning to CLI 2.1.281 requires first stopping
native Messages launches and saving the prior settings before the controlled
restart. Never restore the pre-canary database over admitted operations merely
to roll back an image or CLI pin.
