# Native effort and streaming retirement release

Status: published; production pin prepared for exact-head owner review. Merging
the pin triggers Flux deployment. No rollout or live acceptance is recorded here.
The preparation starts at AILab `f02a3953` and preserves the existing router
configuration, trust, credentials, storage, backup label and service topology.

## Published provenance

- Router [PR201](https://git.chifor.me/cchifor/llm-router/pulls/201) was merged by
  the reviewer as `b60fb03b23ce52548f49513d45466f4670f5a369`. Its reviewed head
  `264424352e97d7e71658ca4669bdcd086adaacaa` passed
  [CI149389](https://git.chifor.me/cchifor/llm-router/actions/runs/149389), including
  PostgreSQL auth integration, pinned real-CLI fixtures and browser checks.
- Archive `router-0.1.0-20261125-native-effort.tar.gz`, 28,588,506 bytes, SHA-256
  `6e2198f4627568bf0f66b1690b647d2ef2b7aea35b9adf1ffc075eee63b1d80e`.
  A fresh Node24.22.2/pnpm10.32.1 frozen production install passed smoke checks
  both before packing and after extracting the archive. The authenticated
  published download independently matched the byte count and SHA-256.
- [Image publication149400](https://git.chifor.me/cchifor/relay/actions/runs/149400)
  passed production and backup-helper checks. Packaging commit
  `0524cb5b3b0a1d27da2c5381eddf8182479e9ce5`; receipt
  `deployment-images-149400.json`, asset1245, verified against these pins.
- Image
  `registry.chifor.me/llm-router/router@sha256:0d4fa1318f2c03d192a28f6d5eee8fca6b8e62b5ce3d9c3e770d21f42d8050a6`.
  Runtime base remains
  `docker.io/library/node@sha256:a723b54c35a76e947095a20a67d39585bb09c862e6b1adeb8a9f518f95e34fb0`.
  Build-time Node24 qualification is separate from the unchanged image runtime.

The published image was pulled by digest and checked offline as UID1000 with
networking disabled: `/app/SOURCE_COMMIT` matches the merged source and the image
runtime is Node26.10.0. Pinned Kustomize5.4.3 renders 15 resources; strict
kubeconform0.6.7 validates 14 with one encrypted-Secret skip under repository
policy. Structural comparison permits only the two image fields and two
annotations; trust/budget mounts and the backup-init expectation are unchanged.
The rollout checker's 17 offline tests pass. These checks made no cluster or
inference requests.

The deployment diff changes only the router and backup-init image pins,
source/release annotations and provenance comments. The prior image is
`registry.chifor.me/llm-router/router@sha256:c35fc2225b787a041799cd2c638d129c349d2718d6a090040aca1f84c9181516`,
source `2ad086a9002311ff4eaa61ff26dacc56d121cbeb`. Keep the completed
`pre-inference-sessions-20261124` backup; the idempotent helper verifies it and
must not overwrite it.

## Behavior and existing conversations

Native Responses admission now fingerprints the entire `reasoning` object and
`max_output_tokens`. Previously these native fields were omitted. A coordinated
branch admitted by the old router with either field present can therefore refuse
its next strict continuation after upgrade, even when the caller did not change
its settings. Inspect effects and use the existing explicit reconstruction flow
under the original router principal. Do not automatically retry, silently start a
new branch, rotate credentials to transfer authority or delete receipts. This
upgrade boundary applies to existing native Codex conversations as well as the
canary. Chat, Messages and native requests without those fields retain their
previous fingerprints, verified by literal hashes from the old implementation.

When a client stops reading, retirement now aborts unfinished upstream work before
closing its iterator. This covers the race where the transport observes the
request deadline before the engine's timer callback. Deadline failures remain
`TIMEOUT/MAX_DURATION`; ordinary early retirement remains cancellation; a
completed provider stream is not aborted. The failure was reproduced on the old
source and covered with deterministic ordering tests.

The image preserves native Messages capability and does not enable effort in a
Relay profile. Normal Pi effort controls require the separately reviewed Relay
release containing PR680 and exact owner-approved profile definitions. Preserve
the three dedicated canary route aliases, one-target/no-fallback/one-attempt
policies, Claude2.1.283, `sdk-resume`, and warm retention disabled. Refresh the
actual configuration before rollout; configuration2522 and the prior image are
historical observations, not current-state proof.

## Owner-controlled rollout and readback

This manifest is an owner-protected production path under
`ansible/roles/pr_reviewer/defaults/main.yml`. Require owner approval at the exact
PR head, both reviewer approvals, green CI and removal of any explicit hold.
The operator selects a drained rollout window and handles existing coordinated
conversations affected by the fingerprint boundary. Merge is the GitOps rollout;
do not add a manual restart or a second SQLite writer.

Before merge, record the actual image/source, configuration/route hashes, plugin
and CLI state, active turns and completed backup. After Flux convergence, require
one ready router on the exact digest/source, successful backup verification and
unchanged configuration/route policies. Use the existing rollout checker and
safe-error/log checks. Read back `nativeAnthropic:true` and `routeSelectors:false`
through Relay's authenticated router connection while Messages remains enabled;
also confirm the dedicated Claude alias's Messages capability. No key issuance,
route changes or inference is part of the image-pin validation.

Worker-budget use remains disabled/unqualified. File B reissue is not a rollout
prerequisite in that state; any later use requires qualification against the new
executing release digest/provider generation through the existing owner workflow.
Do not inherit a previous binary's qualification.

## Recovery and acceptance

Preserve all post-admission receipts, histories and backup evidence. Prefer forward
repair. Rolling back the binary does not make newly fingerprinted native branches
compatible with the older code: pause affected launches, drain work and assess
explicit reconstruction under the original principal before continuation. Never
restore an older database over newer admitted operations to remove the fence.

The paired Pi lifecycle/recovery, effort and warm procedures remain separate
bounded acceptance phases after the matching Relay release and profile approval.
Source tests and published-image smoke checks do not establish live subscription
acceptance. Existing trajectory metadata proves recorded attempts and outcomes;
it does not expose final Claude/Codex wire effort or opaque-block replay. Keep
that observation limitation explicit in the operator's evidence plan.
