# Relay v0.11.3 inference deployment

Status at the 2026-11-24 pre-merge checkpoint: published and locally qualified;
production deployment and paired live Pi acceptance are pending. This pin does
not enable a holder or a canary profile. Record subsequent deployment evidence
below without treating this historical checkpoint as current rollout state.

## Immutable provenance

- Relay source: `aaad724607e2f9a97f64a40951142cf39815e7d4`, release v0.11.3,
  merged PR #648 including inference #644, recovery #650 and Pi output-cap #651.
- Successful publication: [run 147885](https://git.chifor.me/cchifor/relay/actions/runs/147885),
  job 776155.
- Application and migration image:
  `registry.chifor.me/relay/control-plane@sha256:2c350aeef49aaf0b7f596594cc4aa7b72d6d618c54ad5fb6317192bfcaca8a2b`.
- [Publication receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.11.3/deployment-images-147885.json).
- Control-plane release manifest SHA-256:
  `f0ca24bd096069c88f4d1729904e6454c32d86506b3372113c095e5f77fe04a6`.
- Holder archive SHA-256:
  `670f510d2de6ca904d3c674b64070cdda5717b8f2557b34167297f01072a6025`;
  inner manifest `5610a6775d5b94110bd46e6dd5397b595da2a4f010a3417d2720ec10cc50314c`.
- Pi Linux x64 archive SHA-256:
  `0f38af6d2a5a3343e58b07b75c6e5dc5f33acee8db59ac08cf5f523770d6cbf9`;
  inner manifest `a2f10547d5885989b399765dfde815088efea8406cbb9f6b2f5bd2643a3829f7`.
- Connector amd64 SHA-256:
  `bc2a0b69aa763ffcf9182d1d6ffc8fc25862cf325d7baa66982d989a4a1defe6`;
  arm64 `bfdbde7c759c1481f967def3884713cbb17bbfb10384c4e5750afca43695c64b`.

Both archives use the source commit above in their filenames. Downloaded archive
hashes and manifests were verified against the published release. The pulled
image's SOURCE_COMMIT and CONTROL_PLANE_RELEASE.json match that provenance.
Its compiled holder installer, Pi installer and service renderer load with
production dependencies.

## Qualification and deployed baseline

Publication passed 5,576 tests (58 skipped; 415 files passed, nine skipped),
packaged holder/native Pi/Codex tests (22 passed, two skipped), copied
holder/renderer integration (two passed, 18 skipped), Rust, typecheck, format,
and production build checks. Skipped tests do not constitute live acceptance.

The observed live baseline is v0.11.1, source
`3678b8ed882efc48c8ce8d75d043896d7d285eaa`, image
`registry.chifor.me/relay/control-plane@sha256:d0bbbc58392ac75db0c1d08ed4cfb90aade846ae415898bdf209e40f31c8fbbc`.
The candidate retains v0.11.2 Activity changes. Both images contain the same
88 SQL migration files, byte for byte. The sorted filename/file-hash aggregate
SHA-256 is `86755d033be9e7cb7a68c4641dcaf7fd3d61e3b2280358b6f0099a343e1652b0`.
No new migration is introduced. Preserve the existing schema-88 backup,
restore fencing and rollback procedures. v0.11.4 introduces migration 089 and
is a separate release; it is not interchangeable with this qualified pin.

## Deployment and acceptance

1. Require green CI, both reviewer approvals and `chifor` approval at the exact
   production-pin head, as enforced by `ansible/roles/pr_reviewer/defaults/main.yml`.
   Let the reviewer merge and Flux reconcile; do not manually apply the deployment.
2. Refresh the baseline before rollout. If another release has advanced it,
   stop and reassess schema and source compatibility rather than downgrade it.
3. Read back Flux revision, application and migration image/source, successful
   database/artifact backup init containers, migration completion, readiness,
   and schema 88. Retain the verified backup artifacts and logs.
4. Coordinate router AILab #1972, whose published candidate includes inference,
   safe errors and timeout cleanup. Verify its source/image, CLI 2.1.283 activation,
   existing-route baseline and anonymous authentication refusal after approval.
   Native Messages and warm retention remain disabled until cold qualification.
5. Use the merged holder IaC from AILab #1976 and
   [holder runbook](relay-pi-canary-holder.md) on assigned host `dev-worker-1`,
   connector `b3b1be1a-275c-4a2e-b515-428b39c43171`, UID 1000. Obtain the
   operator-approved exact profiles, managed router binding/channel identifiers
   and dedicated OpenBao reader through the existing renderer flow. No hand-minted
   Pi credentials or launcher-proxy profiles substitute for that provisioning.
6. Qualify packaged Pi with Claude Messages, Codex Responses and Qwen Chat:
   denial/approval, one effect, retained signed/encrypted history, branch,
   compaction, cancellation, reconciliation and restart. Preserve durable receipts.
7. After cold Claude acceptance, measure bounded warm latency/RSS, expiry and
   capacity. Require no retained process during tool approval. Exercise active
   warm-to-cold drain and reconstructed continuation without duplicate effects.
8. Record actual evidence in the inference plan and rollout runbook before
   declaring the entire feature complete. Existing live Codex/Qwen router tests
   do not replace paired packaged Pi acceptance.

## Deployment evidence

Read back on 2026-11-24 at 16:21 UTC after owner merge of AILab #1979:
Pod status and its init-container timeline were re-observed at
`2026-11-24T16:24:58Z` for the second-resolution evidence below.

- Merge and Flux `apps` applied revision:
  `8cfb09844043a8c5abf2524b937db21bb2f51223`; Flux Ready is true.
- Pod `relay-c75f846b9-sfvb4`: both containers ready, zero restarts; all four init
  containers exited zero. Both observed application and migration images were
  `registry.chifor.me/relay/control-plane@sha256:2c350aeef49aaf0b7f596594cc4aa7b72d6d618c54ad5fb6317192bfcaca8a2b`,
  equal to the expected published and Git-pinned digest
  `sha256:2c350aeef49aaf0b7f596594cc4aa7b72d6d618c54ad5fb6317192bfcaca8a2b`.
- Pod start: `16:20:44Z`; database backup finished `16:20:49Z`; artifact
  verification finished `16:20:50Z`; migration ran `16:20:53Z`–`16:20:54Z`;
  Ready transitioned true at `16:20:57Z` (all on 2026-11-24).
- `/app/SOURCE_COMMIT` is `aaad724607e2f9a97f64a40951142cf39815e7d4`.
- Read-only `schema_migrations` query: maximum version 88, row count 88.
- Database backup published coordinated recovery generation
  `20261124T162049Z-bfa30aad-dc8a-4f90-a6fd-f837a31b2216` with five verified
  artifacts. Artifact init verified that same pre-migration generation.
- Migration init reported the schema and restricted application role ready.

This establishes hub deployment only. Paired Pi and warm/rollback acceptance
remain pending. A read-only query using the forced-RLS application connection
found no managed credential bindings for the assigned connector. The operator
must provision those bindings and its dedicated renderer reader before activation.
The existing tenant is `00000000-0000-4000-8000-000000000001`, enabled router
connection `babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8`, and workspace communication
channel `b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1`. These nonsecret identifiers are
discovery evidence, not approval of an exact profile or credential policy.

## Rollback

Stop new canary launches, drain active turns, disable warm retention and preserve
native histories and admission receipts. If inference fails, stop the affected
profile. Do not rewrite signed/encrypted history or restore a database snapshot
that erases admitted-operation fences. Follow the existing schema-88 recovery
procedure and owner-approved GitOps process for any compatible image rollback.
Migration equality alone does not prove runtime/data compatibility after use;
verify the current database with the rollback candidate on an isolated copy.
Keep post-admission receipts and quarantine/reconcile affected conversations if
restoration is unavoidable. See [the full rollout](relay-inference-sessions-20261124.md).
