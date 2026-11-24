# Relay/Pi inference sessions rollout

Status: source review and release preparation. No production deployment claimed.

Source changes are [Relay #644](https://git.chifor.me/cchifor/relay/pulls/644)
and [llm-router #196](https://git.chifor.me/cchifor/llm-router/pulls/196).
The implementation and acceptance plan is Relay
`docs/plans/2026-11-23-inference-sessions.md`; the bounded acceptance procedure is
router `docs/runbooks/inference-session-canary.md`.

## Release prerequisites

Both source PRs must pass CI and receive the required reviewer approvals before
the reviewer agent merges them. Build immutable artifacts from the merged source,
using the existing Relay release and router image publication workflows. Record
their source commits, image digests, archive checksums and Pi manifest hash here
before submitting the production pin change for owner approval.

Sequence the Relay rollout after the v0.11.1 migration rehearsal and the v0.11.2
Activity release. This rollout must retain the migrations and already-shipped
fixes in those releases. Do not modify their release branches or image pins.
Production pin paths require the owner's approval at the exact PR head under
`ansible/roles/pr_reviewer/defaults/main.yml`.

## Observed baseline (2026-11-24)

- Cluster: `admin@ai`; deployments `relay/relay` and `llm-router/llm-router`.
- Relay image:
  `registry.chifor.me/relay/control-plane@sha256:d98c6ce7c5578f40057ac6873f07276dbcb4a7c4ca4d9b604d0a407337a2e22e`.
- Router image:
  `registry.chifor.me/llm-router/router@sha256:eb2243e96a84c4ba85ef64f4051c88fe7fa21d1dec9f3ee1aaf24fda24f61be8`;
  source `50d879ca96c2b62ada9e37a4119bdcda786dec66`, release
  `router-0.1.0-20261123-budget-stdout`, Node 26.10.0.
- Claude settings: `transport: sdk-resume`, `maxRunning: 8`, CLI 2.1.281.
  Native Messages requires the qualified CLI 2.1.283. The CLI selection is shared
  across Claude accounts, so its change requires existing Claude-route regression
  checks as well as canary acceptance. Start with warm retention disabled.
- Claude streaming baseline passed. Native Codex Responses on `pool-codex`, model
  `gpt-6-astra`, returned 200 in 4,043 ms; Qwen Chat on `qwen3.8-27b` returned 200
  in 349 ms. The legacy Codex Chat baseline returns 400 because its legacy route
  has no usable deployments; it is not the qualified native Codex path.

Refresh these observations immediately before rollout. They do not qualify a new
image, and configuration revisions may change while releases are reviewed.

## Deployment and acceptance order

1. Record current image/configuration, route listing, budget readings, and a
   consistent SQLite backup using the existing router backup procedure. Preserve
   credentials in their existing stores; never copy a live subscription seat into
   a second router. The router remains one Recreate writer on its persistent PVC.
2. Publish the paired images and prepare the exact digest/source/release changes
   in `kubernetes/apps/apps/relay/relay.yaml` and
   `kubernetes/apps/apps/llm-router/router.yaml`. Include migration rehearsal
   evidence for the actual Relay source. The owner-approved merge triggers Flux;
   do not separately roll the deployments.
3. Check deployment readiness, image/source readback, anonymous auth refusal,
   existing route baseline, and Flux convergence after its next reconcile.
4. Requalify worker-budget evidence and reissue File B for the new router release
   digest according to [the router runbook](llm-router.md). Do not reuse File B
   from the prior binary. Verify witness readback and the budget gate.
5. Enable `api-anthropic` and `inference-sessions`, pin Claude 2.1.283, and retain
   `sdk-resume` with warm retention disabled. Compare existing routes before and
   after this shared CLI change. Use dedicated no-fallback canary routes for
   Claude Messages, Codex Responses and Qwen Chat; issue disposable route-scoped
   keys through the established flow. Keep keys out of logs and this repository.
6. Run `scripts/qualify-inference-sessions.mjs` from the published router archive:
   maximum eight HTTP calls/three generations per target, four-minute deadline,
   fixed in-memory marker only. Require all three reports to pass. Revoke each
   disposable key and require its denial check to return 401/403.
7. Launch the paired packaged Pi runtime with the dedicated Relay profile and
   scoped binding. Qualify denial/approval, exactly one effect, signed/encrypted
   history, branching, compaction, cancellation, reconciliation and restart.
   Do not inject process crashes into shared production workers; the isolated
   local matrix covers 24 router and two Pi crash boundaries.
8. Enable bounded warm retention only after cold acceptance passes. Record cold
   and warm latency, process RSS, idle expiry and capacity. A tool boundary must
   reap the process; a long approval must retain no waiting Claude process.
9. Exercise warm-to-cold rollback during an active canary generation. The old
   provider generation must drain and its next continuation reconstruct cold.
   Record the receipts and effect count. Promote only after all evidence passes.

## Rollback

Stop new canary launches and drain active inference. Disable warm retention first,
leaving native Messages and `sdk-resume` available for the retained history. If
native inference itself fails, stop the profile rather than normalizing away its
signed or encrypted blocks. Revoke disposable keys and preserve transcripts and
receipts for reconciliation.

Revert image/source/release pins through the owner-approved GitOps path only when
the prior binary is compatible with the current database. Do not restore an old
SQLite backup over admitted operations: that erases duplicate-execution fences.
Any required restore must quarantine affected conversations and reconcile effects.
Reissue File B again if the rollback changes the router release digest. Relay
migration rollback follows the rehearsal's recovery procedure; an old image alone
does not undo database migrations.
