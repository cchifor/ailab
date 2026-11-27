# Relay hub rollout for explicit expired-binding renewal

Status: **published v0.13.2 candidate; deployment remains owner-held**. This pin
adds the normal API for explicit renewal after an ordinary binding has expired
and completed revocation and cleanup. Publication does not authorize credential
issuance or a live Pi run. Keep the pin PR's `no-automerge` label until the
operator records the isolated production-copy boot rehearsal, required
cross-review and quiet rollout decision; the earlier request for that rehearsal
is not its result.

| Publication identity | Verified value |
| --- | --- |
| Source change | [Relay #716](https://git.chifor.me/cchifor/relay/pulls/716), merge `c25105694bb3e560d734e6ae73b2ef1064882df2` |
| Release source | [Relay #717](https://git.chifor.me/cchifor/relay/pulls/717), merge `74d1d9f4ab2250fe6a6abe5a2e0bebce2ccc4613` |
| Release / workflow | [v0.13.2](https://git.chifor.me/cchifor/relay/releases/tag/v0.13.2), [successful run 152253](https://git.chifor.me/cchifor/relay/actions/runs/152253), job 795616 |
| Image | `registry.chifor.me/relay/control-plane@sha256:ce79dec456861f3d70cf9c27be42b6fffd31ddcaf762b83974f42299d99e3b83` |
| [Image receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.13.2/deployment-images-152253.json) SHA-256 | `2e8c46b45e7c551666c4b098a0f17f72ceac30b257158a19c1906ac62f0141aa` |
| [Control-plane manifest](https://git.chifor.me/cchifor/relay/releases/download/v0.13.2/control-plane-manifest.json) SHA-256 | `9c9a824aa461d82caa0a274e3a07287738ef2da670a05ceeb335c7bf6a75883d` |

The authenticated readback matched release 2275, receipt asset 1296 and manifest
asset 1288 before and after download. Both source PRs had current approvals and
all five required checks before normal reviewer merge. The four renewal files
in the source merge and release merge are byte-identical. The release workflow
passed packaged holder/Codex/Pi qualification, Rust checks, real holder/renderer
integration, application checks and 6,016 tests (60 skipped; 439 files passed,
9 skipped), connector container tests and image publication. Packaged native
qualification separately had 27 passing tests and 2 skips; this is not a claim
that every test or live owner acceptance phase ran.

An independent pull of that immutable image and a non-root, read-only,
network-disabled Node metadata probe matched `SOURCE_COMMIT` above, package
version `0.13.2`, Node `22.23.3`, the exact control-plane manifest hash and all
93 SQL migrations through `093_pi_native_recovery`. The probe did not start the
application, connect to a database or perform inference. It establishes image
content, not production-copy boot or live deployment success.

Pin validation rendered 26 resources with the repository's pinned Kustomize
5.4.3 and validated them with pinned kubeconform 0.6.7: 22 valid, no invalid
resources or errors, and 4 skipped under the repository's encrypted-Secret and
missing-CRD-schema policy. Source and rendered comparisons each change exactly
two image fields and two provenance annotations; all other parsed fields are
identical. All 14 existing hub-upgrade checks passed. CI and owner rollout
evidence remain separate requirements.

## Baseline and retained state

The hub baseline is source `e843921b7cff76406b7da0b5b35cf653c7e458c5`, schema **93**,
and image:

```text
registry.chifor.me/relay/control-plane@sha256:021a8fbed305c338dc46022abc992f3e0b2c538c6c491fd2aa9f0b279dbccdd8
```

This was the pin before this change in
[`relay.yaml`](../../kubernetes/apps/apps/relay/relay.yaml), whose original
schema-92-to-93 rollout is recorded in
[`relay-pi-v0130-20261125.md`](relay-pi-v0130-20261125.md). Refresh the actual
deployment identity and schema before the new rollout; a different baseline
requires reviewing that difference rather than assuming this comparison applies.

Preserve the existing Recreate strategy, coordinated database/artifact backups,
guarded migration/bootstrap command, migration lock timeout, environment, trust
mounts, volumes, service accounts, resources and admission settings. This hub
change does not replace the Router, change routes/accounts or enable a new
credential grant. It also preserves the installed holder/Pi source
`2604c41b69c5d9c50a3324545a51f0bfa38435a0`, the approved profile bytes with SHA-256
`928d41f0b24c0d1d2493458e7fac23cfd686c045b1aa9daaee480684a0aea9a4`, and the existing
connector, renderer, journals, native homes and workspaces. No holder restart or
profile restaging is part of this rollout.

## Final source comparison and deployment prerequisites

The final source comparison used merged release
`74d1d9f4ab2250fe6a6abe5a2e0bebce2ccc4613`, whose tree is identical to reviewed
head `ae8f6b54f50733770e239c7d460bb899708cf42d`. It found no source-contract blocker:

- Twenty-two compared objects were unchanged from the deployed hub, including
  session/control/permission/recovery APIs, conversations, contracts, auth,
  database access, migration code, renderer code and Helm templates.
- The new hub behavior is the credential manager's list/inspect `canRenew`
  observation and explicit rotation eligibility, plus the release version.
  Thirteen issuance, reconciliation, revocation, cleanup and consumer methods
  were unchanged. The full deployed-to-candidate diff also includes the earlier
  holder permission and Pi refusal corrections already present in retained
  holder/Pi source 2604; it is not described as only PR #716.
- The migration tree remains exactly **1..93**. `canRenew` is computed response
  metadata, not a database column or new persistent credential state.
- Holder/Pi runtime trees match the retained 2604 release. Existing profiles
  select the binding; a fresh launch still reserves a particular credential
  generation and reference. Old consumers do not inherit a renewed key.

The final evidence packet is `pi-v0132-hub-driver-compatibility-74d1d9f.json`,
SHA-256 `add20433a1e23b859e77d5ede29b738717397eb51300d355e31abf757167a683`.
Its historical publication-pending fields describe the comparison time; the
separate release receipt above establishes publication. Reproducible source
anchors are the [deployed-to-release diff](https://git.chifor.me/cchifor/relay/compare/e843921b7cff76406b7da0b5b35cf653c7e458c5...74d1d9f4ab2250fe6a6abe5a2e0bebce2ccc4613),
[schema tree](https://git.chifor.me/cchifor/relay/src/commit/74d1d9f4ab2250fe6a6abe5a2e0bebce2ccc4613/migrations),
[credential routes](https://git.chifor.me/cchifor/relay/src/commit/74d1d9f4ab2250fe6a6abe5a2e0bebce2ccc4613/server/router-credentials/routes.ts)
and [renewal eligibility](https://git.chifor.me/cchifor/relay/src/commit/74d1d9f4ab2250fe6a6abe5a2e0bebce2ccc4613/server/router-credentials/expired-renewal.ts).
Source compatibility is distinct from image qualification, live eligibility and
rollout evidence; a changed source requires reviewing the changed comparison.

Before releasing the owner hold, retain all of the following in its review evidence:

1. The normally merged source PR, final release commit and assigned version;
   exact source equality for the reviewed renewal change and the retained
   session/runtime contracts.
2. Successful release qualification and the authenticated publication receipt
   binding that source to the immutable application image. Read the published
   image's `SOURCE_COMMIT`, version and migration inventory; do not substitute a
   local build or assume a tag proves image content.
3. The actual hub/driver compatibility result for the final source. Any owner
   driver that pins a hub source remains held until its source and input checks
   are reviewed and qualified for that release.
4. A rendered manifest comparison limited to the application and migration image
   pins, their source/release annotations and explanatory comments. Preserve all
   other runtime settings listed above. Run the repository's normal render,
   schema and relevant rollout checks, CI and both reviewer gates.
5. The operator's isolated production-copy boot rehearsal under this exact image:
   backup identity and starting schema 93, normal migrate/bootstrap success,
   final contiguous schema 1..93, readiness, and observations under the normal
   non-root/read-only configuration. Do not connect that copy to production
   renderers or issue credentials as a rehearsal. Record any limitations rather
   than reusing the earlier 92-to-93 rehearsal as proof of this image.

## Owner-controlled quiet rollout

The operator must choose a quiet window for the affected hub clients, with no
active owner acceptance run, unsettled tool effect or concurrent credential
renewal crossing the replacement. Coordinate it outside the existing QNAP
maintenance windows. A previous canary stop is evidence of that stop, not proof
that every current client is quiet.

Use the normal protected-file owner merge and `no-automerge` hold for this
pin PR. **Merge deploys through Flux** after the Gitea-to-GitHub mirror update;
the label is not an in-cluster Flux suspension. Record the owner's release of
the hold and window before merging. Do not manually apply around that process.

Recreate stops the old application before the existing coordinated backup and
guarded migrate/bootstrap sequence. Preserve the first backup generation and its
verification receipt for this rollout, plus any later retry generations. The
starting and expected ending schema are both 93; the normal migration/bootstrap
command must still complete successfully. Do not bypass it because no migration
was added, and do not reuse the historical schema-92 backup as this rollout's
current backup.

## Readback before any owner renewal

Retain an attributed, sanitized readback containing:

- Deployment/observed generation, Pod UID, application and migration image
  references, running imageID and `SOURCE_COMMIT`, all matching the final
  publication receipt and pin annotations.
- Coordinated backup completion and its generation/verification evidence, every
  required init-container exit, contiguous schema **1..93**, readiness and the
  established [H12 health observations](relay-researcher-hub-upgrade.md#h12-v0114-health-tiles-with-history-schema-89).
  An init exit code alone does not independently authenticate backup contents.
- Unchanged environment, trust mounts, storage, resources and admission settings;
  retained holder/Pi/profile identities and their expected reconnect state.
  Preserve any actual restart or readiness failure in the receipt.
- A normal authenticated, tenant-scoped credential inventory/inspection showing
  the new boolean `canRenew`, existing nonsecret fields and the actual binding
  state. Select known approved identities; do not dump tokens or escrow material.

Observe ordinary expiry and cleanup without issuing a key as a deployment test.
`canRenew: true` requires an enabled ordinary binding whose latest generation
naturally expired, all generations have confirmed revocation and completed
cleanup, and no unresolved consumer remains. Explicit revocation, disabled
authority, machine/preparation credentials or uncertain cleanup do not qualify.
False is therefore not automatically a deployment failure. Missing `canRenew`,
unexpected state or an unverified source stops the successor owner procedure.

Only after this readback may an authorized administrator separately review and
submit the existing `POST /api/router-credentials/{bindingId}/rotate` with a
retained operation UUID and the current revision. Follow the reviewed
`docs/runbooks/router-expired-binding-renewal.md` in the published Relay source.
Keep the same binding and grant. A timeout is reconciled as the same operation;
there is no automatic renewal, expiry extension or new inference allowance.

For fresh holder use, preserve the launch-driven order: issuance metadata and
valid holder hello, normal offered launch, renderer delivery, completed
`install-router`, completed `Open`, then the first prompt after all receipt and
owner-window checks. Credential readiness before any offered launch is not a
new prerequisite. Old references and retained sessions never inherit the new
key. Deployment/readback and credential renewal are distinct from live Pi
lifecycle, effort, warm and capacity acceptance.

## Rollback within schema 93

The source comparison supports understanding the new ordinary generation records
with the baseline e843/schema-93 hub: issuance, reconciliation, cleanup and
consumer contracts are unchanged. An actual image rollback still requires a
reviewed operator plan, quiet admission and current durable-state/configuration
checks; this procedure neither performs nor automatically triggers one.

Returning to the baseline image does not undo a renewal, decrement generations,
revoke a replacement, revive an expired key or rebind an old consumer. Baseline
e843 can reconcile already-created ordinary generations, but lacks terminal
expired-binding renewal and the new `canRenew` projection. The successor owner
procedure must halt when its expected source or metadata disappears.

No database restore or state rewind follows automatically from this same-schema
comparison. The earlier
[schema-93-to-92 restore and fresh recovery-fence boundary](relay-pi-v0130-20261125.md#rollback-restore-fence-reviewed-re-pin)
remains a separate historical downgrade procedure, with its own backup identity,
authority loss and write-loss consequences. Do not apply it to a schema-93 image
rollback without an independently reviewed need and restore plan.
