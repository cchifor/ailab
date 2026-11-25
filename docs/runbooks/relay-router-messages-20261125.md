# Managed Relay Messages capability release

Status: published; production pin prepared for owner review. Merging the pin PR
triggers the Flux rollout. This document records preparation evidence; post-roll
and activation acceptance remain pending.
The isolated preparation starts from AILab `181b6d2bc806619c4344413b3631af219b024ec0`.

## Immutable provenance

- Router [PR #200](https://git.chifor.me/cchifor/llm-router/pulls/200) merged as
  `2ad086a9002311ff4eaa61ff26dacc56d121cbeb` (confirmed through read-only repository API).
- Release: `router-0.1.0-20261124-inference-messages`.
- Archive: `router-0.1.0-20261124-inference-messages.tar.gz`; SHA-256
  `68aa4bd2736583b0e2e600a9108fd36d636cfd9b7d6544f4b6991eacd3bd2049`.
- Successful [publication 148962](https://git.chifor.me/cchifor/relay/actions/runs/148962),
  packaging source `fff67dda59e0cfac94d92b1051ac7a75be5be7d6`; verified receipt
  `deployment-images-148962.json`, asset `1227`. Its local copy was checked against
  these source, archive and image pins.
- Image: `registry.chifor.me/llm-router/router@sha256:c35fc2225b787a041799cd2c638d129c349d2718d6a090040aca1f84c9181516`.
- Runtime: Node 26.10.0, base
  `docker.io/library/node@sha256:a723b54c35a76e947095a20a67d39585bb09c862e6b1adeb8a9f518f95e34fb0`.
- Offline verification of the compiled capability module reports
  `nativeAnthropic` transitions `false/true/false` and `routeSelectors:false`, using
  synthetic dependencies with no router or provider requests. This is not live
  inference or end-to-end managed credential qualification.

PR #200 makes managed-key capability discovery report `nativeAnthropic` from the
active `api-anthropic` plugin state. It retains `routeSelectors:false`; callers
must separately qualify their scoped route catalog and use `model:<allowed alias>`.
Capability discovery does not itself enable Messages or approve a canary profile.

The deployment diff is limited to `kubernetes/apps/apps/llm-router/router.yaml`:
the router and `backup-before-inference-sessions` image digests, source/release
annotations, and adjacent provenance comments. It preserves the backup label
`pre-inference-sessions-20261124`, mounts, security settings and completed snapshot.
Publication passed production smoke and backup-helper tests against the installed
helper, including repeat verification, before producing the immutable receipt.

The preceding Git pin is source `a16a6846e89d6971d9549bac5ff3a7dde99b361b`, release
`router-0.1.0-20261124-inference-safe-errors-r2`, image
`registry.chifor.me/llm-router/router@sha256:1d3eaf5671cb85be3720eab7a65a87acc1aa3a33f283ef88a21580ea78fb8167`.
The operator reports [AILab PR #1987](https://git.chifor.me/cchifor/ailab/pulls/1987)
deployed the same image at 02:05Z on 2026-11-25.
Retain that trust configuration; do not replay or replace its changes.
No hub pin or migration belongs in this router update: current main already has
Relay v0.12.1 and subsequent backup-holder shutdown changes. Historical v0.11.3
deployment instructions are not a request to roll the hub back.

## Read-only preroll baseline

The read-only snapshot `router-messages-preroll-20261125.json`, observed
2026-11-25 at 09:03:02.630Z, reports the preceding source above. No inference ran.

- Configuration revision `2510`, SHA-256
  `df056b1941de21bc4cb418b3c63607ee52fc50a2781e4d4d3fd580b31e8d657f`.
- Route catalog SHA-256
  `58254eeab6f9d626fda4939ca406318156b9e111e3dabd0d227a29dbd02a6b50`.
- `canary-pi-codex`: one enabled `codex-2 / gpt-6-astra` target;
  `canary-pi-qwen`: one enabled `lan-192.168.0.28-18020 / qwen3.8-27b` target.
  Both have no fallback and `maxAttempts: 1`. No Claude canary route exists yet.
- Claude CLI `2.1.283` saved and active, no pending change; provider saved/active
  settings are `sdk-resume`, warm retention false, `maxRunning: 8`, idle 300000 ms,
  with no pending change.
- `relay-keys` and `inference-sessions` are active; `api-anthropic` is disabled.
  Anonymous route catalog and admin configuration requests both returned 401.

Preserve these settings and routes during the image rollout; refresh their readings
before/after the authorized roll. With `api-anthropic` still disabled, a false
native Messages capability is expected on the new image. Enabling it, creating a
Claude route, issuing credentials and running inference remain separately authorized
acceptance work; neither this pin nor the offline verification establishes them.

## Owner and rollout requirements

The router manifest is an owner-protected production path under
`ansible/roles/pr_reviewer/defaults/main.yml`. Its pin PR needs `chifor`'s approval
at the exact head (or the owner's own merge), green CI and both reviewer personas.
`no-automerge` remains an absolute hold while present.
[AILab PR #1986](https://git.chifor.me/cchifor/ailab/pulls/1986)'s merge and its
reviews do not grant owner approval of these production pins or the exact profiles.
Owner profile approval, credential issuance and renderer delivery remain separate
prerequisites in [the canary inputs](inference-canary-v0113/README.md).

Before an authorized rollout, refresh the actual image/source, route baseline,
plugin and CLI state, witness readings and existing backup evidence. A GitOps
merge is the rollout; do not add a manual deployment restart or second writer.
Require one ready pod on the exact digest/source, successful backup verification,
Flux convergence, unchanged routes, and the existing safe-error/log checks in
[the preceding release runbook](relay-inference-safe-errors-20261124.md).
Refresh managed-key capability discovery through the existing authenticated Relay
router connection flow and record `nativeAnthropic:false` and `routeSelectors:false`
while `api-anthropic` remains disabled. After the separately authorized cold
Messages activation, repeat that check and require `nativeAnthropic:true` with
`routeSelectors:false`, plus `anthropicMessages:true` for the exact scoped Claude
alias in `/v1/routes`. Do not enable Messages solely for the image-roll check.
Coordinate any live acceptance with the operator; this preparation authorizes no
live configuration changes, router-ab run or inference.

## Worker-budget File B

Preserve disabled/unqualified worker-budget use. **File B reissue is not a
prerequisite for this image rollout while that use remains disabled.** This follows
the router runbook's [File B exception](llm-router.md#budget-status-on-stdout-release-2026-11-23-llm-router-195)
and [inference deployment order](relay-inference-sessions-20261124.md#deployment-and-acceptance-order).
The new binary must not inherit a prior release's qualification.

Before any future worker-budget use, take fresh readings after the final rollout,
requalify and reissue File B for the executing `routerReleaseDigest` and
`providerGeneration`, with owner confirmation of the old/new qualification digests
when superseding, then pass `file_b_router_check`. Follow
[Generating/Reissuing File B](relay-worker-budget-authority.md#generating-file-b-file-bjson-and-ledger-step7bsql).
No router change may follow that qualification before its canary. Do not infer
current hub budget readiness from historical H6 metrics or alter hub profiles.

## Local manifest validation

The repository-pinned Kustomize v5.4.3 build rendered 15 router resources. Strict
kubeconform v0.6.7 validation of the rendered Deployment passed (one valid resource,
no errors or skips). A structural comparison against the base manifest confirmed
only the two image fields and source/release annotations changed; the rendered
Relay trust and worker-budget mounts remain present. The existing rollout checker's
offline expectation matches the new digest, existing backup init and no canary Job.
No cluster, router configuration or inference requests were made by these checks.

## Recovery

Stop affected canary launches, drain active turns, disable warm retention if enabled,
and preserve histories and admission receipts. Prefer recovery forward if rollback
compatibility is unproven. Any rollback to the Git pin recorded above needs the
same owner-approved path, coherent image/source/release pins and a verified backup
helper; do not restore a snapshot over admitted operations. A changed release or
provider generation must be qualified again before future worker-budget use.
