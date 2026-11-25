# Bounded Pi effort acceptance — prepared, not executed

This is a separate operator procedure for **one fresh Claude session and one fresh Codex session**, run sequentially. It does not authorize activation, credential issuance or inference. It is not part of the eleven-prompt lifecycle/recovery sequence in [LIVE-ACCEPTANCE.md](LIVE-ACCEPTANCE.md), and passing that sequence does not establish effort qualification. Qwen/OpenAI Chat is excluded from this capability.

The prepared contract is source-checked against Relay [`74c2ec3500dc3b3b927f655b4dca0b04dbf508c6`](https://git.chifor.me/cchifor/relay/src/commit/74c2ec3500dc3b3b927f655b4dca0b04dbf508c6) ([PR680](https://git.chifor.me/cchifor/relay/pulls/680)) and router candidate [`3402c4fc9b39fb695317bf68d4006f2b0d45473b`](https://git.chifor.me/cchifor/llm-router/src/commit/3402c4fc9b39fb695317bf68d4006f2b0d45473b). The router change makes native Responses effort part of its configuration fingerprint. The old `2ad086a` router does not satisfy this Codex check. Source, packaged synthetic-upstream tests and local builds are not proof of published or deployed capability. Require reviewed, qualified release artifacts containing both changes, record their final source/image/manifest pins and recheck request shapes if the final source differs.

## Gates and fixed budget

- The owner must approve the final concrete opt-in profile definitions and exact bytes. Adding `holder.settings.config.thought_level` is new capability **outside the prior home-layout/pins-only preapproval**. These instructions neither modify the checked-in profile JSON nor assign a new revision. Use the actual newly approved revision, never silently overwrite an older definition.
- The supported profile policy has `settings.modes:[]`, no native commands (absent or `[]`), and exactly the config key `thought_level`. Its unique allowlist starts with `off` and includes the approved values needed here, `low` and `high`; the minimal selection is `["off","low","high"]`. The implementation can qualify subsets of `low`, `medium`, `high`, `xhigh`; this procedure does not test or authorize other levels. `max` and `minimal` are unsupported. Messages and Responses only; no new `native.effort` field.
- Preserve the reviewed route/model alias, principal, one-attempt/no-fallback policy, output limit, workspace, trust and managed credential boundary. Require profile `maxTokens:1024` for the wire expectations below and the existing 120-second request limit. A different approved limit needs a matching reviewed procedure, not a silent increase. For Claude require the approved Sonnet-capable dedicated Messages route and cold `sdk-resume`, with warm retention disabled. Do not repoint a route or mint keys to make the test pass. Retain the separate Claude/Qwen/provider gates and release-admission checks from the lifecycle runbook.
- Reconfirm owner acceptance window, operator activation/readiness and exact deployed hub/holder/Pi/router identity. Use existing managed grants and delivery; no raw key extraction. `nativeStateReady:true`, current holder identity and matching approved profile are required. Staged or service-active alone is insufficient.
- Allocate a fresh run UUID and new normal Pi session for each provider. Each provider has **ten minutes, four prompt commands total, at most six observed upstream generations**, and at most 120 seconds per prompt. The expected successful path has two provider completions; the refused high probe and reconciliation each require zero recorded attempts. Settings commands, lease operations and Close are not prompts. There is no warmup, retry loop, history padding, tool execution or marker write. Do not borrow prompts or time from the lifecycle run, or silently extend either budget.
- Renew the same human session's owned lease every 20 seconds; poll operation receipts every two seconds. Stop and cancel/Close at the first deadline or observed budget limit. As with the lifecycle API, no hard aggregate generation cap is enforced by the prompt request itself; cancellation can race in-flight work. If the required counters are unavailable, this phase is blocked, not permission to assume the budget was respected.

`off` is labelled **Provider default**. It omits the explicit effort control and does not promise disabled provider reasoning, zero thought tokens or a particular provider default. Fresh and recovered sessions start at `off`; this procedure uses fresh sessions and explicitly selects each tested value.

## Required evidence collection before starting

Arrange a sanitized collection method tied to the exact deployed source and this run's namespace, original router principal (`key:<ID>` where applicable), dedicated route and time window. Do not put credentials, cookies, prompts, raw histories or signed blocks in the evidence packet.

1. Capture normal hub launch, settings, command and control receipts, source/current generations and epochs, and timeline boundaries. A settings acknowledgement proves selection, not the wire payload or provider behavior.
2. Take paired settled trajectory snapshots around each claimed zero-attempt interval (the high-setting/refusal phase and reconciliation separately). The router list endpoint supports `GET /admin/v1/trajectories?namespace=<N>&principal=<KEY_ID>&route=<ALIAS>&limit=500`; use existing authorized metadata access, never the canary key as an admin credential. `principal` is the exact `key:<ID>` string, URL-encoded as necessary. Preserve only identity, scope, timestamps, status, completeness, configuration revision and attempts. Reject/inconclusive a full 500-row page, open/incomplete records, changed/missing baseline rows or unclear time boundaries. First correlate the completed low request with its recorded trajectory to establish coverage. Preserve process/database identity and recording/retention continuity; isolate unrelated traffic in this exact scope.
3. Record allowlisted **actual request** metadata for each accepted completion: protocol, selected alias/resolved target, explicit effort and output limit. For Claude require `output_config.effort` equal to the selected value, no top-level `thinking`, and `max_tokens:1024`; for Codex require `reasoning.effort` equal to the selected value and `max_output_tokens:1024`. Record only these approved fields with the corresponding request/trajectory ID, not a full request body. The trajectory list alone does **not** expose these fields. If safe deployed wire observability is absent, mark the wire claim blocked; source code, settings snapshots and synthetic tests are not substitutes.
4. Identify how to observe the explicit router inspection/reconstruction outcome, operation/epoch and successor branch. Trajectory attempt metadata is not a reconciliation audit. A zero recorded-attempt delta is conditional on coverage and does not expose failures before trajectory creation or prove zero tool calls. Pair it with hub outcomes and sanitized request evidence. An old collector hard-pinned to `2ad086a` must not be used unchanged against the new router or have its identity check bypassed.

Do not begin the four prompts until required observability is available. Missing evidence remains blocked/unobserved, never an inferred pass. This text-only phase does not qualify opaque-block replay, worker-crash recovery, compaction, concurrent branches, warm performance or rollback.

## Launch, lease and exact settings requests

Use the normal authenticated, same-origin hub workflow in [LIVE-ACCEPTANCE.md](LIVE-ACCEPTANCE.md#normal-launch-and-control-request-bodies), including release admission and managed-router readiness. Launch the selected approved profile with `POST /api/agent-launches`, its actual approved revision and exact workspace; locate the returned launch in `GET /api/agent-launches` and record its new `session_id`. Require completed launch/open session and ready router metadata. Do not reuse a lifecycle session.

Symbols: `S` = new session UUID; `G` = actual current generation; `E` = actual control epoch; `L` = owned lease ID; `R` = latest `snapshot.revision`; `O` = fresh UUID for each logical mutation. Numeric examples below are placeholders, not assumed initial values. Acquire `POST /api/agent-sessions/<S>/control` with `{"generation":G}` and renew with `{"generation":G,"leaseId":L}`.

Read `GET /api/agent-sessions/<S>/settings`. Require a non-null `snapshot` with the single advertised `configOptions` entry `id:"thought_level"`, `currentValue:"off"`, and choices whose **IDs** include `off`, `low` and `high`. Hub choices use `options[].id`, not the native ACP `value` shape. Require no model control or extra choices outside the approved policy. Read the latest settings revision, current generation/epoch and idle state before each setting change.

Select low through `POST /api/agent-sessions/<S>/commands`:

```json
{"operationId":"<O>","leaseId":"<L>","generation":1,"controlEpoch":0,"settingsRevision":1,"kind":"set-config","configId":"thought_level","value":"low"}
```

HTTP 202 only queues the command. Poll `GET /api/agent-sessions/<S>/operations/<O>` and require `command.state:"completed"` with its completed receipt `result.settingsApplied:true`. Then re-read `/settings`: require the latest snapshot's `thought_level.currentValue` to be `low`. Do not infer or increment `R`; use the returned revision. Apply the identical sequence later for `high`, with a **new** operation UUID and fresh `G`, `E` and `R`.

Preserve the exact serialized body for retries. On a lost response, look up the same operation before considering an identical-body retry. A changed body under the same operation ID is refused; stale generation/control/settings revision, missing lease, unadvertised choice, active prompt or uncertain prior setting is a stop condition. Do not bypass it or automatically resubmit under a new ID. No speculative invalid-setting probes are part of this bounded live procedure; those are covered by offline regressions.

## Four prompts, serially while settled

Every row uses `POST /api/agent-sessions/<S>/commands` with a fresh operation UUID and current owned lease/generation/control epoch:

```json
{"operationId":"<O>","leaseId":"<L>","generation":1,"kind":"prompt","controlEpoch":0,"text":"<EXACT TEXT BELOW>"}
```

| Prompt | Preparation and exact text | Required outcome |
| --- | --- | --- |
| 1: low | After confirmed low selection: `Reply exactly EFFORT_LOW_OK. Do not use tools, access files or modify anything.` | One bounded native completion, low wire metadata and unchanged 1024-token limit. Record its completed router trajectory and branch. |
| 2: high refusal | While idle, snapshot attempts, select high using the fresh settings revision, confirm its acknowledgement/current value, then: `Reply exactly EFFORT_HIGH_PROBE. Do not use tools, access files or modify anything.` | The already-admitted branch refuses changed configuration; require the sanitized configuration-change/reconstruction-required error and zero recorded-attempt delta. A successful provider completion here is a failure, not a pass or an invitation to continue. |
| 3: explicit reconcile | After refusal settles, inspect effects/absence of tools, confirm the same router principal and explicitly choose: `/relay-reconcile confirmed` | Authenticated inspect and reconstruct succeed, previous branch retired/successor recorded, zero recorded-attempt delta. No automatic selection, hidden retry or silent new branch. |
| 4: high | After successful explicit reconstruction: `Reply exactly EFFORT_HIGH_OK. Do not use tools, access files or modify anything.` | One bounded native completion on the successor branch, high wire metadata and unchanged 1024-token limit. |

Read operation receipts and paginated timeline as in the lifecycle runbook. A prompt transport receipt alone does not establish inference success; the high refusal can be reported through native output even when command delivery completed. Keep delivery, native outcome, router refusal and attempts as separate fields. `unknown`, `withdrawn`, missing receipt or nonsettled work is inconclusive. Expected refusal is not an automatic reconciliation trigger: the operator must inspect and explicitly submit prompt 3. A new router installation or rotated credential does not transfer principal-owned history or reconciliation authority.

Do not approve any tool or permission request. If one appears, cancel the pending request/turn through the normal reviewed controls and stop this phase; do not manufacture an effect or use the lifecycle marker commands. Reconcile only idle, settled work. If a deadline, refusal or observability gap prevents a row, preserve its outcome and leave later rows unexercised. Do not repeat prompts to fill a report.

## Close and evidence packet

Close the fresh session with the ordinary command, using a new operation UUID and actual current generation/owned lease:

```json
{"operationId":"<O>","leaseId":"<L>","generation":1,"kind":"close"}
```

Close has no `controlEpoch` or `settingsRevision`. Record completion/ended state, then release the owned lease using `DELETE /api/agent-sessions/<S>/control/<L>` with no body after outstanding work settles. Grant revocation is the owner's normal separate cleanup workflow; do not create, rotate or revoke credentials merely to produce an effort result. Preserve the provider's existing policy and warm setting.

Save one minimized manifest per provider/run: approved-profile bytes/hash/revision and approval reference; acceptance window; final source/image/runtime/holder pins; namespace/principal/route and no-fallback configuration revision; new launch/session/native UUID; holder/readiness identity; lease owner; before/after settings snapshots and revisions; every operation UUID/body reference, generation/epoch and receipt; timeline boundaries; paired trajectory snapshots and aggregate attempts with coverage limitations; request-correlated low/high effort and output-limit metadata; explicit inspection/reconstruction receipt and branch mapping; prompt/generation counts, timestamps/durations, Close outcome and separate passed/failed/blocked/unexercised status for every claim. No aggregate Claude+Codex pass while either provider or wire/fence claim is unproved.

Source references: `packages/contracts/{session-control,session-settings,launch-profiles}.ts`, `server/agent-sessions/{control,settings}.ts`, normal HTTP regression `tests/session-control.test.ts`, and packaged `tests/pi-router-{messages,codex}.test.ts`. The local packaged five-case qualification used synthetic upstreams and a patched router, not live subscriptions. This prepared document records no live acceptance.
