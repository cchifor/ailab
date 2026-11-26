# Native Messages output replay release

Status: published image; deployment candidate held for the owner. This change
records no rollout or live acceptance. The deployment candidate starts from
AILab `f5fad154f05e5becd9d4fb44804e7ece6edc1093`.

## Change and source binding

The deployed predecessor is Router source
`4af66b8c7c3cc83ddd10e326c554ad728caad72e`, image
`registry.chifor.me/llm-router/router@sha256:e98a8e189ce9d6fe2d6c2bd206594bb47922de0a49acafb82b3ff77fde471d69`
([AILab PR2019](https://git.chifor.me/cchifor/ailab/pulls/2019)).

[Router PR204](https://git.chifor.me/cchifor/llm-router/pulls/204) merged as
`4e4bb34e2f968f9450aabe9cf228d3e70c8bab71` from reviewed head
`9426855f02550a75e47a2f4cb9f40061ada333e5`. Both have Git tree
`5c9e310ef194cce681436ee7bf6f1e733b43de2b`; both reviews and
[CI151337](https://git.chifor.me/cchifor/llm-router/actions/runs/151337) passed.
The seven-file diff changes one production file,
[`packages/adapters/protocol/anthropic.ts`](https://git.chifor.me/cchifor/llm-router/src/commit/4e4bb34e2f968f9450aabe9cf228d3e70c8bab71/packages/adapters/protocol/anthropic.ts).
The strict native Messages input schema now accepts and preserves empty text
citations (`null` or `[]`) and the exact direct caller object
`{type:"direct"}` when clients echo assistant tool calls. Text in tool results
accepts the same empty citations. Unknown fields, nonempty citations and
server-side callers remain unsupported. No passthrough or metadata stripping is
introduced. The remaining six files are behavior documentation and tests.

The reported cold Messages qualification failed on a continuation after a
successful first generation and one in-memory tool effect. The rejected response
content was not captured; these legitimate metadata shapes are proven regressions,
not a claim about the exact unrecorded field from that live failure. A fresh
bounded cold qualification is still required after rollout.

## Archive and qualification

- Archive `router-0.1.0-20261126-messages-native-output.tar.gz`: 28,623,927 bytes,
  SHA256 `f667b242a70325336ee4f2aed6f505137e2140aa8b82817fd5633d12643bd04c`.
  Authenticated readback of release asset1279 matched it. The embedded
  `SOURCE_COMMIT` is the merged source above.
- Packaging commit `88ecb970085f0141aea02793b326f05438652a67` changes only the
  archive name, directory, source and checksum in `deploy/ci/releases.json`.
  Runtime base remains
  `docker.io/library/node@sha256:a723b54c35a76e947095a20a67d39585bb09c862e6b1adeb8a9f518f95e34fb0`.
  [Publication151374](https://git.chifor.me/cchifor/relay/actions/runs/151374),
  job790983, passed the production and backup smokes inside the built Node26
  image. Its authenticated [deployment receipt](https://git.chifor.me/cchifor/relay/releases/download/v0.2.0/deployment-images-151374.json)
  is asset1281, 789 bytes, SHA256
  `c3e5ab92a64973cebc373100868eeb5af9e26d15efe6c3521d108617e74b5886`.
  It binds the packaging commit, archive, source and runtime to image
  `registry.chifor.me/llm-router/router@sha256:f180cf7f1f960782a46dff6bb73cb955ce34dcb6e083daed0b1202f58753b457`.
- The archive now includes `scripts/qualify-inference-sessions.mjs` and
  `operational-scripts-manifest.json`; these are the only added archive paths.
  The manifest SHA256 is
  `0c52e1d9e38cf8a26db44f719b6a34d798f3803ca9d0261cc0f81f222b21540c`.
  Exported, staged and extracted script bytes match the raw merged Git blobs.
  Qualifier SHA256 is
  `0d3e2e7cbd3e271acc49f3b8d6a2e2df762b3a67c71d3840b933add0e4c5a9ac`;
  smoke-script SHA256 is
  `a8d9949a3da09103fcbf9f1362833464dcf0d7061080fa9b447c028dd900e925`.
  Both scripts are unchanged from source4af. This repairs the missing packaged
  qualifier; packaging does not execute its provider requests.
- The offline Node24.22.2/pnpm10.32.1 frozen build and both staged/extracted
  production smokes passed. The archive's sole native addon remains the exact
  Node-API v9 prebuilt `node-addon-require-builtin-linux-x64-gnu@0.1.6`, SHA256
  `ec78b442caffcce311c6a6cbbc4fead6d59a4b8cae5b5206c6c84994d68d5667`.
  The publisher copies frozen dependencies without a rebuild. Its production
  SPA/authenticated Responses/SQLite-worker/MCP smoke and backup verification
  passed under the built Node26 image, separately from this addon inventory.
  SQLite uses built-in
  `node:sqlite`. The backup helper comes from unchanged Relay packaging.
- Independent file-identity readback under that exact image, with networking
  disabled, confirmed source4e4, Node26.10.0, the operational manifest and both
  script hashes above. Backup-helper SHA256 is
  `63be6a39b3ce7e58a0206aff443ee8bd3a042fb254989730974d127588bfbd58`,
  matching the exact LF Git blob from packaging. This metadata check made no
  provider requests and did not repeat the publication smokes.
- Five packaged Pi wire/replay cases passed with zero skips in 63.02 seconds:
  published Pi/ACP/policy/tools from Relay
  `2604c41b69c5d9c50a3324545a51f0bfa38435a0`, Pi manifest
  `d0bf7f9d44a2ae992bb1fbd87f7f2043c7d582ad563078bd13d713ebcd211a0d`,
  and exact merged Router TypeScript source4e4. The runtime was not rebuilt.
  This used synthetic providers with networking disabled; it is not a published
  Router image test, real subscription request, holder/hub lifecycle test or live
  opaque-wire observation. PR204 separately covers native HTTP round trips and
  the pinned real-CLI fixture tier.

Local candidate validation used pinned Kustomize5.4.3 and kubeconform0.6.7:
15 resources rendered, 14 valid, zero errors, one encrypted Secret skipped under
the repository policy. Source and rendered comparisons found exactly four
changed scalars: two image pins and two provenance annotations. All 17 existing
router-rollout tests passed. The live/warm guide changes only add the successor
notes; their prior procedure bodies remain unchanged.

## Successor acceptance boundary

Exact Git comparisons show unchanged warm adapter/provider/generation handling,
account admission and queue logic, inference engine, session fingerprints and
reconcile handling, native error-code endpoint, settings/metadata/trajectory APIs,
operational scripts and dependency lock. There is no database or session-receipt
migration in this patch. This source comparison does not prove current settings,
live warm reuse, capacity, or successful cold qualification.

**Complete all active owner Phase A inference and trajectory collection on
source4af before replacing the router.** Its active driver is pinned to that
source. Do not modify its code, saved state or evidence to accommodate a rollout.
The operator must confirm the quiet boundary and a drained window before the
owner merges the deployment PR; a passing CI run is not that confirmation.

For subsequent Claude B/C execution, the source4af prerequisites in
[LIVE-ACCEPTANCE.md](inference-canary-v0113/LIVE-ACCEPTANCE.md) and
[WARM-ACCEPTANCE.md](inference-canary-v0113/WARM-ACCEPTANCE.md) gain this explicit
successor only after the new image/source and qualifier bytes are read back.
Use fresh helpers pinned to source4e4, the final published image, and fresh
evidence paths. Preserve older results as evidence for their original sources;
do not relabel them. Keep the existing owner window, seat check, cold prerequisite,
late Claude issuance, delivery/Open receipts, profile/prompt budgets and separate
exclusive warm/capacity-window decisions. Shipping the qualifier grants no new
provider execution or configuration authority.

## Owner rollout and rollback

The PR must retain `no-automerge` (248), both current-head reviewer
approvals, green required CI and the owner-controlled protected-path merge.
Merge triggers Flux; the PR hold is not a claim that Flux is suspended.
The manifest change is limited to router/backup-init image pins and source/release
annotations, with provenance comments. Trust, routes/accounts, credentials,
storage, resources, worker-budget mounts and the completed backup label remain.

Use the existing [router rollout procedure](llm-router.md), including its QNAP
maintenance constraints. Record the old image/source, active work and current
configuration, trust and backup state before merge. After convergence, use the
existing rollout checker and source-pinned metadata readback: one Ready router,
successful backup init, exact image/source, expected plugins and preserved
policies/mounts. Verify the image's operational-script manifest and qualifier
bytes before a separately authorized cold run. Explain independent health/config
events rather than assuming the configuration revision cannot advance.

Rollback is an owner-reviewed GitOps revert to source4af/imagee98 above. Preserve
the current database and admission history; do not restore a snapshot to erase
admitted work. Rolling back removes the expanded replay contract, so Claude
continuations using the new metadata must remain stopped until resolved. Do not
retry an uncertain turn or tool effect under a new operation identity.
