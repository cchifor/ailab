# Native Messages configuration refusal code release

Status: published image; deployment candidate held for the owner. No rollout or
live acceptance is recorded by this change. The candidate starts from AILab
`525ba01580b0c0224b243bb24c931d6f98c1151f` and changes only the router and
backup-init image pins, source/release annotations and provenance comments.

## Source and artifact binding

The previous source is `b60fb03b23ce52548f49513d45466f4670f5a369`, image
`registry.chifor.me/llm-router/router@sha256:0d4fa1318f2c03d192a28f6d5eee8fca6b8e62b5ce3d9c3e770d21f42d8050a6`.
Its rollout receipt is [AILab #2007, comment143268](https://git.chifor.me/cchifor/ailab/pulls/2007#issuecomment-143268).

- Router [PR203](https://git.chifor.me/cchifor/llm-router/pulls/203) merged as
  `4af66b8c7c3cc83ddd10e326c554ad728caad72e`; reviewed head
  `08ba9fc14524bdf595a09b7d74c9930986aa32c6`. Both reviewers approved and
  [CI150562](https://git.chifor.me/cchifor/llm-router/actions/runs/150562) passed,
  including PostgreSQL integration, pinned Claude CLI loopback fixtures,
  production smoke and browser checks. Both commits have Git tree
  `439be7b08f47f6131d7da94cf0a9f11747f5ddb0`.
- Archive `router-0.1.0-20261126-messages-code.tar.gz`, 28,586,036 bytes,
  SHA-256 `0c525f3c6064b41755298937bf93aa3a68b847b02c75488d5f58dd3da454ce06`;
  embedded `SOURCE_COMMIT` is the merged source above. Archive asset1261 was
  downloaded through authenticated Gitea and matched these pins. The Node24.22.2
  frozen production build and both staged/extracted archive smoke tests passed.
- Packaging commit `2881055eb789243a039f2c1f2f5be38af31674aa`,
  [publication150599](https://git.chifor.me/cchifor/relay/actions/runs/150599).
  Its [deployment receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-150599.json)
  is asset1263, 771 bytes, SHA-256
  `b13564048e078e13c03cbdc2de4c0ce24fcdeea0d37044b4b9d40bae59868540`.
  Authenticated readback binds the packaging commit, run, source, archive,
  runtime and image to the reviewed spec; the publisher passed production and
  backup smoke checks on the built image.
- New image:
  `registry.chifor.me/llm-router/router@sha256:e98a8e189ce9d6fe2d6c2bd206594bb47922de0a49acafb82b3ff77fde471d69`.
  The unchanged runtime base is
  `docker.io/library/node@sha256:a723b54c35a76e947095a20a67d39585bb09c862e6b1adeb8a9f518f95e34fb0`.
  Independent read-only metadata execution of that digest, with networking
  disabled, confirmed `/app/SOURCE_COMMIT` equals `4af66b8c7c3cc83ddd10e326c554ad728caad72e`,
  Node `v26.10.0`, ABI `147`, and backup-helper SHA-256
  `63be6a39b3ce7e58a0206aff443ee8bd3a042fb254989730974d127588bfbd58`.
  That metadata check did not repeat production smoke or perform deployment.

The source diff from the previous release contains exactly three files:
`packages/plugins/api-anthropic/index.ts`, `tests/anthropic-messages.test.ts`
and `docs/guides/relay-messages.md`. Only a router-owned HTTP409 with exact code
`SESSION_CONFIGURATION_CHANGED` gains native `error.code`; its existing
`invalid_request_error` type and message remain. Other error envelopes and
successful forwarding are unchanged. There is no configuration, database,
session-fingerprint or receipt-format migration in this patch. Existing
uncertainty and explicit-reconciliation rules still apply across a process stop.

The unchanged backup helper comes from Relay packaging, not the router source.
Packaging bases `0524cb5b` and `2881055e` have identical helper Git blob
`021ead1128c3d0fe4688c893c810a9794f5216fb`; its test blob remains
`884d21f5b48674be90a93fe8918dcb7889367698`. Keep the existing completed
`pre-inference-sessions-20261124` snapshot: the init verifies it without replacing
it. Trust, routes, accounts, credentials, storage, resources, service topology,
backup label and worker-budget configuration are outside this manifest change.

Local validation used the repository-pinned Kustomize `5.4.3` and kubeconform
`0.6.7`: 15 rendered resources, 14 valid, no errors, and one encrypted Secret
skipped under the existing lint policy. Source and rendered semantic comparisons
found exactly four changed scalars: two image pins and two annotations. All 17
router-rollout tests passed; the existing checker still expects only the router
and its backup init, with no inference canary Job.

## Owner rollout and readback

Keep the PR's `no-automerge` label (248) until the operator coordinates the normal
owner-controlled release. The manifest is an owner-protected path. Both reviewers
must approve the current head and required CI must pass before owner release of
the hold. The hold is on the PR, not a claim that Flux is suspended. Merge triggers
GitOps deployment; do not apply around the hold, manually restart, or add another
SQLite writer.

Use the existing [router rollout procedure](llm-router.md) and a drained window
selected by the operator, respecting the existing QNAP maintenance constraints.
Record current image/source, active work, configuration and route/account hashes,
plugin state, trust hash and backup verification before merge. After Flux
converges, verify one ready router on the new digest and `/app/SOURCE_COMMIT`,
successful backup init, preserved trust mounts and route/account policies, and
existing health/log checks. Classify independent health/configuration events
instead of assuming a global revision number cannot advance. The existing
`scripts/check-router-rollout.py` observes the pinned router and backup init;
this candidate adds no inference canary Job or provider call.

Rollback is an owner-reviewed GitOps revert of the image/provenance pins to the
previous release. Keep the current database and admission evidence; do not
restore an older snapshot to erase admitted work. A binary rollback removes the
new structured code, so clients must treat its absence as an unclassified
refusal rather than infer the reason from text.

The matching Relay Pi notice and holder permission changes require their own
reviewed release and approved host upgrade. Router publication, its five passing
packaged-Pi synthetic wire/replay cases and this pin do not establish live Pi
acceptance. Continue the separately bounded owner/operator lifecycle, effort and
warm procedures only after their normal delivery and readiness gates.
