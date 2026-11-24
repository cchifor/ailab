# Relay/Pi inference sessions rollout

Status: router deployed and baseline checks passed; Relay/Pi activation pending. Deployment completion
requires the post-roll readbacks below, not merely the Git merge.

## Router rollout readback — 2026-11-24

Owner merged AILab #1967 at 09:14 UTC, commit
`eb63210529ffc95849419acc9778cfd34b2909d7`. Flux `apps` reports this applied
revision. The router Deployment runs the `370bf5bf` image digest recorded below;
pod `llm-router-67bf4df5b7-brk5d` is ready (1/1), with zero restarts at readback.
Readbacks and bounded baseline requests were collected at 09:18–09:23 UTC.
Its source/release annotations match `300856ff` and
`router-0.1.0-20261124-inference-sessions`.

- Init-container log: `Router SQLite backup and associated files verified:
  pre-inference-sessions-20261124`.
- Authenticated `/v1/routes` is byte-identical to the saved pre-roll baseline:
  SHA-256 `cb9904a07f3a4627cbf849a60ad9396f544a4db9cb886c83ccae03ea6fa85afa`.
  Baseline location: `/data/inference-rollout-20261124/routes-before.json` on
  the router PVC (mode 0600). Compare the authenticated response inside the pod;
  do not extract or log the administrator credential. This file is the pre-roll
  listing and must not be overwritten by later canary route changes.
- Anonymous `/v1/routes` and `/admin/v1/config` both return 401.
- Existing native Codex Responses: 200, completed, 2,535 ms.
- Existing Qwen Chat: 200, completed, 416 ms.
- Existing Claude streaming Chat: 200, completed, 966 ms.

These bounded checks qualify existing-route behavior after the image roll, not
the new Messages/session path. No live plugin configuration was changed by these
checks. Native canary activation, packaged Relay/Pi acceptance and warm-to-cold
rollback remain pending. Relay v0.11.3 release PR #648 also waits for recovery
fix #650: first-turn tool interruption with only a pending admission receipt.

## Live Codex and Qwen session acceptance — 2026-11-24

At 09:34:36–09:34:48 UTC, the published image's
`/app/scripts/qualify-inference-sessions.mjs` passed against public HTTPS
`https://router.chifor.me` using temporary single-deployment, fixed-model routes
with `maxAttempts: 1`, no fallback, and disposable route-scoped keys.

| Protocol | Fixed target | Result |
| --- | --- | --- |
| Responses | `codex-2`, `gpt-6-astra` | Seven calls, three generations, one marker effect; passed |
| Chat | `lan-192.168.0.28-18020`, `qwen3.8-27b` | Seven calls, three generations, one marker effect; passed |

Both paths passed native tool-result continuation, duplicate-operation refusal
(409), durable completed receipt inspection, explicit branch retirement,
retired-worker refusal (409), and reconstructed continuation (200). Both revoked
keys then returned 401. Temporary routes `inference-canary-20261124-codex` and
`inference-canary-20261124-qwen` were removed using the latest configuration
revision; key files were removed. Durable receipts remain in the router database.
The runner's only effect was recording its fixed marker in memory.

Reports are retained on the router PVC under `/data/inference-rollout-20261124/`
as `native-codex-qwen-report.json` and `native-codex-qwen-revocation.json`, mode
0600. They contain controlled status/usage/receipt metadata, not credentials or
provider output. Their respective SHA-256 values are
`ba50615eed55c33b7b4f1403166af546ce2b4d2c4c439bd1c40557d5d1342737` and
`c46a3b7cb7c51a67de39f56ed83817991f0ffed7c7844671525fa87ebe649baf`.
This qualifies the router wire lifecycle for these two providers;
it does not qualify the live Relay/Pi profile, which remains unassigned.

Claude Messages remains pending. The configured CLI is still 2.1.281; native
Messages requires 2.1.283. Subscription-login version changes apply on restart
to retain a single sign-in/renewal owner, so the shared CLI change must accompany
the coordinated router release. Messages stays disabled and warm retention off.
The separate safe-errors release must retain the now-deployed inference source.

## Published router and backup qualification

Router source merged at `300856ff7881eba67d2081650fca9f66a7f26b66`.
[Publication 147499](https://git.chifor.me/cchifor/relay/actions/runs/147499)
published image `registry.chifor.me/llm-router/router@sha256:370bf5bf7f03da92b728e92fcc62b06439f886730a8a670d13be0d84924f1909`.
Archive `router-0.1.0-20261124-inference-sessions.tar.gz` has SHA-256
`79df76afc52dedc7f80381b31d9f3744e134fde8e3e0454ea83d7de63c25f391`;
the publication receipt is attached to Relay v0.2.0 as
`deployment-images-147499.json`. This is the router image only; the legacy Relay
image also emitted by that packaging workflow must not be used for this rollout.
The router pin adds an init-container backup, after the Recreate writer stops,
under `/data/backups/pre-inference-sessions-20261124`. Failure blocks startup.
The [pinned backup script](https://git.chifor.me/cchifor/relay/src/commit/b0336729fbd9915379246df3e64b616197736642/deploy/ci/backup-router.mjs)
verifies the existing `COMPLETE` manifest, file checksums and SQLite integrity on
subsequent starts, then exits successfully without overwriting anything. Its
[publication test](https://git.chifor.me/cchifor/relay/src/commit/b0336729fbd9915379246df3e64b616197736642/deploy/ci/test-backup.mjs)
qualifies repeated runs, preservation of historical snapshots, and corruption
refusal. Keep the completed snapshot while this init container is configured;
remove the init block through a reviewed change before retiring that snapshot.
This backup shares the live PVC. It protects against application-level corruption,
not PVC loss; independent off-volume recovery remains the estate backup procedure.
The [pinned publisher](https://git.chifor.me/cchifor/relay/src/commit/b0336729fbd9915379246df3e64b616197736642/deploy/ci/publish.py)
copies `deploy/ci/backup-router.mjs` into the image's `/app/deploy/` directory and
runs the backup test against that exact installed path. This was also verified
after pulling the published digest, with no network, UID 1000, a read-only root,
all capabilities dropped and writable `/tmp`: running
`node /checks/test-backup.mjs /app/deploy/backup-router.mjs` passed live WAL backup,
default/custom labels, repeat verification, invalid-label/corruption rejection
and unchanged-source checks. `/checks` was the read-only pinned Relay CI directory.

Storage rehearsal against an isolated consistent copy of the live database also
passed: new store, current production store, then new store again. Schema version
7, integrity `ok`, configuration hash and all 23,875 trajectories were unchanged.
No server/provider process was started, no credential key was copied, and the
temporary snapshot was removed. This establishes storage compatibility, not
provider or end-to-end acceptance.
This first pin stages the router with native Messages and warm retention still
disabled. It may roll before the paired Relay release; it does not activate the
new profile or constitute end-to-end acceptance. Relay's pin follows separately
after its source, version and migration rehearsal are qualified.

Pre-roll route baseline is preserved inside the existing router PVC at
`/data/inference-rollout-20261124/routes-before.json` (0600), SHA-256
`cb9904a07f3a4627cbf849a60ad9396f544a4db9cb886c83ccae03ea6fa85afa`.
Validation: router full verification on Node 26.10.0, production-only smoke,
image publication smoke/backup tests, 17 rollout-check tests, Kustomize render,
and cluster server-side dry run. Source PR #196's final CI includes the split
UTF-8 regression. Real-Claude active warm-to-cold rollback also passed locally.

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
4. Verify witness readback and preserve the existing disabled worker-budget use.
   The 2026-11-23 router release explicitly left File B unqualified because no
   worker-budget path is configured (router runbook, "Worker budgets: File B is
   not reissued after this roll"). This rollout does not reopen that campaign.
   Before any future worker-budget use, requalify evidence, reissue File B for
   this binary, and pass `file_b_router_check`. Never reuse the prior release row.
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

The router rollback must also remove `backup-before-inference-sessions` from
`initContainers`, so no new-image backup process runs ahead of the old binary.
Before starting that old binary, verify its schema compatibility on an isolated
copy of the current database. If compatibility is unproven, keep launches stopped
and recover forward; do not infer compatibility from the prior image being healthy
before the upgrade. Preserve the completed backup when removing the init block.
