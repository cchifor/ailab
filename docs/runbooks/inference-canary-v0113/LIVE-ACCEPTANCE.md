# Paired Pi live acceptance — prepared 2026-11-25

This is an operator procedure, not a record of successful live provider acceptance. The documentation and read-only observer can merge independently of live gates. Existing owner/operator sessions retain authority for activation and late managed credential issuance. The published canary preparation is merged in [AILab PR2010](https://git.chifor.me/cchifor/ailab/pulls/2010); the separate router rollout in [AILab PR2007](https://git.chifor.me/cchifor/ailab/pulls/2007) and operator sequence below still govern execution. Claude trust is tracked in [AILab PR2002](https://git.chifor.me/cchifor/ailab/pulls/2002); refresh its state and the cold Messages route/provider prerequisites in step 3 before issuance. The older activation proposal [PR2003](https://git.chifor.me/cchifor/ailab/pulls/2003) is superseded by PR2010. Do not infer approval from this document.

The normal hub API below is checked against deployed Relay v0.13.0 source [`e843921b7cff76406b7da0b5b35cf653c7e458c5`](https://git.chifor.me/cchifor/relay/src/commit/e843921b7cff76406b7da0b5b35cf653c7e458c5), image `registry.chifor.me/relay/control-plane@sha256:021a8fbed305c338dc46022abc992f3e0b2c538c6c491fd2aa9f0b279dbccdd8`. The directory's v0113 name is historical. Recheck source identity if the hub changes. This source review confirms request and receipt compatibility; it does not exercise the live procedure.

Operator bus1839 selects paired evidence: normal live hub/control/permission/recovery receipts and router attempt metadata, together with passing real pinned-binary synthetic wire/replay qualification. Correlate reports to the final deployed source/artifact identities. Relay effort source is merged in [PR680](https://git.chifor.me/cchifor/relay/pulls/680) (`0e39d323dfe7dec1747cd1de8059101726b8c816`); router corrections are merged in [PR201](https://git.chifor.me/cchifor/llm-router/pulls/201) (`b60fb03b23ce52548f49513d45466f4670f5a369`). Merged source and passing synthetic qualification do not establish publication, deployment or live acceptance. This method adds no live opaque-history observer or new wire capture. **Final outbound effort/cap fields and opaque bytes are not proven live.** Preserve that explicit limitation alongside each result; keep the existing marker observer for local effect checks, all budgets and lifecycle controls. See [EFFORT-ACCEPTANCE.md](EFFORT-ACCEPTANCE.md) for the separately bounded effort phase.

Published release: v0.13.0/source `e843921b7cff76406b7da0b5b35cf653c7e458c5`, with exact image/archive/manifest/profile hashes in [README.md](README.md). Publication149439 succeeded. The operator reports [hub/schema93 rollout passed](https://git.chifor.me/cchifor/ailab/pulls/2009#issuecomment-142980), [final profile scope verified](https://git.chifor.me/cchifor/ailab/pulls/2010#issuecomment-142961) and [stopped restaging passed](https://git.chifor.me/cchifor/ailab/pulls/2010#issuecomment-143004). Exact published root/profile proof and 31 positive/five negative compiled-schema cases passed offline; all five published-Pi wire/replay cases passed offline (zero skipped). Router comparison/rollout, late managed readiness, activation and live acceptance remain open. Operator bus1887 requires the router gate in [AILab PR2007](https://git.chifor.me/cchifor/ailab/pulls/2007) first, then owner-issued Codex/Qwen bindings just before renderer/holder startup; Claude remains unissued in that batch. These completed deployment and installation checks do not establish live provider acceptance. The release includes the PR681 preflight correction described below.

## Preconditions and limits

Use the existing authenticated `relay_session` browser session at `https://relay.chifor.me`; never paste cookies, lrk keys, native subscriptions or rendered credential files into evidence. Requests below are relative to that origin. Mutations use `Content-Type: application/json` and same-origin `Origin: https://relay.chifor.me`. Operator role covers launch/control/decisions; admin covers profile approval, credential management and native-recovery endpoints. Keep the same human session for the control lease. These are request recipes, not an unattended driver.

Before each provider:

1. Record owner acceptance window and operator gate, deployed router/hub source and digest, exact profile revision, holder/artifact identity, trust and health. The checked-in revision2 profiles correct `credentialHome` to `state/profile-homes/<profileId>` and pin the published matching owned-recovery release. Confirm the actual bytes match the operator-verified final scope recorded above; Router rollout, late managed readiness and activation remain required. Do not run this procedure on the old release. Require `nativeStateReady:true` from the actual holder state root; `state/native-homes` belongs only to holder-created session homes, not precreated profile homes. Existing directory/profile disposition is a separate reviewed operator step, never automatic cleanup. Staged/disabled is not active. Preserve operator KEEP exceptions and do not change production pools to make a test pass.
2. Confirm dedicated route exists, has the intended protocol/provider, one enabled target, `maxAttempts:1` and no fallback; confirm one-route managed grant. Model selection must be `{ "kind":"model", "model":"canary-pi-<provider>" }`, never a direct provider model or `x-router-route`. Qwen has its own local-provider readiness gate.
3. For Claude, establish cold route, trust and Messages prerequisites, including the agreed `warm:false` condition, **before credential issuance**. Do not issue Claude credentials until the owner acceptance window. Do not turn direct qualification into a warmup or reuse a historical report path.
4. Inspect profile admission: these checked-in definitions use `qualification:"protocol-fixture"`. Pinned `server/release-admission.ts` refuses fixture profiles when `releaseApprovalFile` or `releaseApprovalSha256` is set (409, `Release mode refuses fixture profiles`). If enabled, stop and resolve the approved profile/admission path with the owner; do not disable release mode or relabel the fixture.
5. Use only the existing reviewed bindings and owner-managed grant workflow. Inspect `GET /api/router-credentials` and matching metadata. If issuance is still required, the exact reviewed request is the matching object in [binding-requests.json](binding-requests.json), sent by the owner to `POST /api/router-credentials`; do not create alternate direct keys. HTTP 202 is not proof of rendering/readiness. Follow the [activation order](ACTIVATE-CODEX-QWEN.md): renderer services and holder startup/empty-session hello first, then the owner's first normal launch schedules credential delivery. Require managed rendering/installation receipts, binding readiness, completed Open and the operator's delivery handoff before that session's first prompt. An absent file with no offered launch is expected; never require initial delivery before holder startup or the first launch.
6. Run [observe.py](observe.py) as the intended host user against the existing private sandbox immediately before launch. A previous baseline is not sufficient. Root must be UID1000/mode0700; created files must be UID1000/mode0600, regular and single-link. No permission weakening or precreating effects files.

Run providers sequentially. Allocate a fresh canonical lowercase run UUID per provider attempt, a distinct operation UUID per logical mutation, and unique evidence filenames. Maximum 15 minutes per provider, 120 seconds per prompt, 30 seconds to decide a pending permission, lease renewal every 20 seconds (lease lifetime at most 60 seconds). Poll receipts every 2 seconds, stopping at the deadline. Allow at most 12 prompt commands and 20 observed upstream generations per provider; no automatic retry, warmup, or history-padding loop. `maxTokens:1024` and `maxRequestSeconds:120` are profile limits, **not an API-enforced total generation budget**. The prompt API has no `maxTurns`/generation-limit field. Cancel at a deadline or observed budget limit and stop; in-flight work can race cancellation. If a hard upstream generation cap is mandatory, this normal API procedure cannot guarantee it.

## Fixed profile and sandbox mapping

All proposed revisions are 2, connector `b3b1be1a-275c-4a2e-b515-428b39c43171`, execution identity `c4` (UID1000). `ROOT` is `/home/c4/.local/state/relay-holder/workspaces/` plus the profile UUID. Use the matching exact object in [launch-definitions.json](launch-definitions.json) only after the deployment hold above is resolved.

| Provider | Profile name / UUID | Binding UUID | Protocol / model alias |
| --- | --- | --- | --- |
| codex | pi-canary-codex / `1d9a7d6e-4b26-49fd-9053-7b0a22e1baae` | `a0f57684-747f-4c3e-8d90-e684b7262590` | openai-responses / `canary-pi-codex` |
| qwen | pi-canary-qwen / `f25c187b-1e50-4c7c-860e-52947bfdf39e` | `d2871eec-8f5a-4afe-a917-014f0ae8e14d` | openai-chat / `canary-pi-qwen` |
| claude | pi-canary-claude / `fdb56ca5-908b-40ee-94b3-aac735dd8622` | `a8f6211c-d7e2-4ffd-824d-b158e349a8d4` | anthropic-messages / `canary-pi-claude` |

```sh
python3 observe.py PROVIDER RUN_UUID baseline
```

Replace the uppercase arguments; do not execute them literally. Require `phaseChecksPass:true` and both files absent. The observer supplies `commands.denied` and `commands.effects` for exact copying into the prompts below. It never executes those strings. Filenames are `ROOT/acceptance-RUN_UUID.denied` and `ROOT/acceptance-RUN_UUID.effects`; marker is `relay-pi-PROVIDER-RUN_UUID`. The command format is:

```sh
umask 077; printf '%s\n' 'MARKER' >> 'ABS_PATH'
```

Append is deliberate: repeated execution produces duplicate markers; overwriting would hide replay. Never approve a model-selected path, alternate command, extra shell operation or “equivalent” write. The preliminary 2026-11-25 baseline on all three staged roots found absent files and valid private roots; it did not exercise the hub or providers. Refresh baseline immediately before actual launch.

## Normal launch and control request bodies

Symbols below are substitutions, not literal API values: `O` = fresh operation UUID, `P` = profile UUID, `S` = returned session UUID, `G` = current positive integer generation, `E` = current nonnegative integer control epoch, `L` = returned lease UUID. Preserve the serialized body and operation ID for receipt lookup; do not retry an uncertain request under a new ID.

Read `GET /api/agent-profiles`. Reuse the enabled matching revision and definition. If approval is actually missing, the admin request is `POST /api/agent-profiles` with `{ "operationId":O, "definition":D }`, where `D` is the **whole exact matching JSON object** from launch-definitions.json, not a profile name or its `holder` child. For example, offline body construction is `({operationId: crypto.randomUUID(), definition: definitions.find(d => d.name === "pi-canary-codex")})`. Do not silently bump revisions or overwrite a mismatch.

`POST /api/agent-launches`:

```json
{"operationId":"<O>","profileId":"<P>","revision":2,"cwd":"<ROOT>"}
```

Expect 202; inspect `GET /api/agent-launches` (latest 100) for that launch ID/operation and its `session_id`. There is no assumed GET-by-launch-ID endpoint. Pending launch expiry is bounded to two minutes; stop on refusal/withdrawal/expiry rather than relaunch. Read `GET /api/agent-sessions/<S>` and `GET /api/agent-sessions/<S>/router`; verify profile, connector, current generation, no rebinding, open session and router readiness. The session ID is not the profile UUID.

**At launch completion, before the first prompt**, save the matching source launch's nonsecret evidence for later recovery: `id`, `operation_id`, `session_id`, `state`, `receipt.status`, `receipt.result.nativeSessionId`, `profile_id`, `profile_revision`, `cwd`, `holder_id`, `holder_journal_id`, `native_recovery_version` and `receipt.result.nativeRecoveryVersion`. Require `state:"open"` and `receipt.status:"completed"`. Here `holder_id` stores the holder **incarnation**, not a separate stable host identity; `holder_journal_id` stores its journal ID. The source row can rotate out of the latest-100 list during the controls phase, so capture these fields now, not after Close. Retain only the required fields, not an unfiltered row. The later eligibility step compares this saved evidence with current readbacks.

`POST /api/agent-sessions/<S>/control` acquires `{ "generation":G }`; renew with `{ "generation":G, "leaseId":L }`. Response supplies `id`, `generation`, `expires_at`. `GET /api/agent-sessions/<S>/control` reports the lease and ownership. Do not steal a conflicting lease.

All prompts, including Pi slash commands, use `POST /api/agent-sessions/<S>/commands`:

```json
{"operationId":"<O>","leaseId":"<L>","generation":1,"kind":"prompt","controlEpoch":0,"text":"<EXACT PROMPT>"}
```

Replace numeric example `1`/`0` with current `G`/`E`; do not assume initial values. Each prompt uses a new operation UUID. The exact cancel and close shapes are:

```json
{"operationId":"<O>","leaseId":"<L>","generation":1,"kind":"cancel"}
{"operationId":"<O>","leaseId":"<L>","generation":1,"kind":"close"}
```

Cancel/close have **no `controlEpoch` field**. Re-read session epoch before another prompt. HTTP 202 only queues work. Inspect `GET /api/agent-sessions/<S>/operations/<O>` for the command receipt (`command` may be null), and `GET /api/agent-sessions/<S>/commands` for recent projected states. Require completed receipt plus settled native turn; `unknown`, `withdrawn`, rejection or missing receipt is not success. On lost HTTP response, look up the same operation before considering an identical-body retry. Never blindly repeat an append prompt.

Read output using `GET /api/conversations/sessions/<S>/timeline?after=0&limit=200`, then page with returned `nextAfter` and, for prompts, `nextPromptsAfter` via `promptsAfter`. Preserve sequence/generation continuity; a single page is not necessarily complete.

## Prompt and permission sequence

1. **Initial turn:** `Reply exactly READY. Do not use tools or modify files.` Require a completed native turn and the expected dedicated route/protocol in sanitized router receipts. This establishes real paired Pi traffic, unlike direct wire qualification.
2. **Denied effect:** prompt `Request permission to run exactly the following shell command once. Do not run any other command, change the command, retry after denial, or modify any other file. After the decision, report the outcome and stop. COMMAND: ` followed by the observer's exact `commands.denied` string. Inspect the actual permission request before choosing deny-once.
3. **Approved effect:** use the identical prompt template with `commands.effects`. Approve only that exact command once. After receipt completion and quiescence, require exactly one marker line; do not resend to “confirm”.

For each permission, list `GET /api/agent-sessions/<S>/permissions`, then inspect `GET /api/agent-permissions/<R>`. Match `session_id`, current generation, pending state and expiry. Check `context` title/toolCallId/options against the actual tool call. The holder truncates titles to 500 characters and clears descriptions: if the complete command cannot be established from the visible request/timeline, stop rather than infer it from the prompt.

`POST /api/agent-permissions/<R>/decision`:

```json
{"operationId":"<O>","generation":1,"outcome":{"outcome":"selected","optionId":"<ACTUAL OPTION ID>"}}
```

Select the returned option whose kind is `reject_once` for denied, `allow_once` for approved; never invent IDs or choose allow-always. To cancel a permission instead, the exact outcome is `{ "outcome":"cancelled" }`. Response fields `commandId`, `requestId`, `delivery`, `execution` acknowledge the decision path, not filesystem execution. Re-read permission delivery/execution and the parent prompt's operation receipt; wait until the canary is quiescent before readback:

```sh
python3 observe.py PROVIDER RUN_UUID denied
python3 observe.py PROVIDER RUN_UUID approved
```

Run each only at its matching settled phase. Require `phaseChecksPass:true`: denied file remains absent; approved effects file is exactly marker plus newline once. Missing permission, tool refusal without a permission decision, unsafe observer result or ambiguous command visibility makes that step failed/inconclusive. Observer success establishes local effects only, not hub permission, provider or lifecycle acceptance.

## Branch, compact, cancel and reconcile

Perform each control serially while idle and within the same bounds. The following are exact **prompt text**, not `kind:"native-command"` bodies:

| Step | Exact text | Required evidence |
| --- | --- | --- |
| List history | `/relay-history` | Completed assistant turn IDs in output. |
| Branch | `/relay-branch <8hex>` | Substitute a completed eight-hex ID from history, preferably after the approved effect. Branch acknowledgement and subsequent independent router branch. |
| Branch probe | `Reply exactly BRANCH_OK. Do not use tools, repeat earlier commands, or modify files.` | Completed turn, changed branch, preserved intended history; effects still once. |
| Compact | `/relay-compact` | Actual successful compaction, then independent inference branch on the next prompt. |
| Compact probe | `Reply exactly COMPACT_OK. Do not use tools, repeat earlier commands, or modify files.` | Completed turn and compacted-history evidence; effects still once. |
| Cancellation target | `Without using tools or modifying files, count upward from 1, one integer per line, until 500.` | Submit cancel while generation is observed active; record both operations and cancellation outcome. |
| Reconcile | `/relay-reconcile confirmed` | Only while idle, after any cancel settles and possible effects have been inspected: authenticated router inspection/reconstruction, previous branch retired, new branch acknowledged, no provider or tool invoked. |
| Reconcile probe | `Reply exactly RECONCILE_OK. Do not use tools, repeat earlier commands, or modify files.` | Completed turn on reconstructed branch, no repeated marker. |

Short history may refuse compaction; record it as unexercised, not successful. Do not pad history indefinitely. Cancellation can lose the race to completion: `cancelSent:true` alone does not prove an interrupted turn, and a completed target before cancel is inconclusive for cancellation. Do not manufacture uncertainty by killing production services. Reconciliation internally inspects and reconstructs `/v1/inference/sessions/reconcile`; use the Pi command, not a manually extracted credential. A successful settled reconciliation does not prove interrupted-tool recovery. Re-read marker effects after settled controls with the `approved` observer phase; they must still be exactly once.

## Same-holder Pi recovery — activation and live acceptance pending

The former v0.12.1 hub/v0.11.3 Pi combination did not support this owned-recovery procedure. Upgrading does not make old histories eligible or create a missing v2 receipt. Do not POST resume against an ineligible source merely to force a result, substitute a dsh profile, or label a fresh ordinary launch as recovery.

The recovery implementation originated in [Relay PR679](https://git.chifor.me/cchifor/relay/pulls/679), merge commit [`1044cb197400a8416804b7ce79f8d004f2821d0d`](https://git.chifor.me/cchifor/relay/src/commit/1044cb197400a8416804b7ce79f8d004f2821d0d), with green [main CI149254](https://git.chifor.me/cchifor/relay/actions/runs/149254). Published v0.13.0 includes that implementation and the later preflight correction; the sequence below has been rechecked against deployed source e843921. Hub rollout, final scope verification and stopped installation are recorded above. Keep execution held for the remaining Router, managed readiness, activation and acceptance-window gates. Recheck request/receipt shapes if the deployed source changes.

Deploy the compatible hub/schema 93 before the v2 holder, then the matching holder/Pi artifacts. Require fresh actual-root holder readback with `nativeRecovery:2`, `nativeStateReady:true`, journal ID and incarnation, plus the approved profile/workspace identity. The owning holder must stay alive throughout source launch, retirement and recovery. Whole-holder restart remains unsupported; do not clear its readiness fence, delete homes or downgrade the hub while the v2 holder is connected. Preserve journals, histories and one-use claims.

For a provider attempt that includes recovery, **replace** the table's final Reconcile and Reconcile probe with the post-import reconciliation/probe below. Do not run both pairs: all prior listed prompts plus both pairs would total 13 and exceed the 12-prompt limit. The recovery sequence is: cancellation settles → Close → eligibility/resume → fresh control → explicitly selected reconciliation if the retained branch needs it → RECOVERED_OK. It totals at most 11 prompt commands: three initial/permission prompts, six history/branch/compact/cancellation prompts, and at most two post-import prompts. Close, control and resume are not prompt commands; native import performs zero inference. A standalone non-recovery run retains the original reconciliation pair but must not additionally claim recovery within that same sequence. Record a skipped or refused step as unexercised. Keep the existing overall deadline and generation budget; stop as inconclusive if the remaining window is insufficient. Warm, opaque-replay and concurrent-branch qualification remain separate work.

A sanitized evidence collection method is also required before execution: identify how to observe provider/tool-call counts, the upstream generation budget, holder ownership and completion receipts, fresh communication/installation identity, and router inspection/reconstruction operation, epoch and branch. The marker observer provides none of these. Record before/after observations and their collection source; if deployed visibility cannot supply a required claim, leave that claim blocked/unobserved rather than assert zero calls or infer continuity from a marker. Do not extract managed credentials to obtain this evidence.

### Normal API recovery sequence after the hold is cleared

`S` remains the source Relay session; `G` is its actual current generation. Both recovery endpoints require the existing administrator session. Perform once per selected provider and record each outcome separately.

1. **Retire the source under the same live holder.** After the selected controls settle, inspect both marker files with the `approved` observer phase. For clean-close acceptance, send the existing normal `kind:"close"` command to `S` with its current generation and owned lease, then inspect that operation receipt and re-read the session until it is ended. Do not issue a prompt after Close. If an actual native-worker failure was observed, record that fact separately; the ACP adapter can remain connected after an idle worker dies, so use normal Close if the session is still open and require completed retirement. A clean Close proves clean owned-history recovery, not crash recovery. This procedure supplies no PID-kill or shared-service restart step.
2. **Inspect eligibility and source identity.** Read `GET /api/agent-sessions/<S>/native-recovery`; require `available:true` and `kind:"pi"`. Its successful response contains only `available`, `profileName` and `kind`; it does not return the native UUID, revision or holder identities.

   Use the source launch evidence captured at launch completion above, matching `session_id` to `S` and its recorded launch/operation IDs. The native UUID is `receipt.result.nativeSessionId`; the approved identity is `profile_id`, `profile_revision` and `cwd`. Require the saved `holder_id` to equal the current hello's `incarnation`, and `holder_journal_id` to equal `hello.journalId`; keep both comparisons. The source capability is recorded in `native_recovery_version` and `receipt.result.nativeRecoveryVersion`. Read the source's current generation/state from `GET /api/agent-sessions/<S>`.

   The launch list contains only the newest 100 tenant launches, with no implemented cursor/filter or GET-by-launch-ID endpoint. A saved row establishes historical identity; current eligibility and hello remain required. If the needed row was not saved at launch completion and is no longer listed, record the evidence gap instead of inventing fields or launching another attempt.

   The source must be an ended root session with its v2 ownership receipt and unchanged approved profile/workspace. `available:false` is a block; preserve its reason and any `launchId`, and inspect that existing attempt rather than launching another. Fresh capability alone cannot make an older history eligible. These fields are defined in deployed [`native-recovery.ts:122`](https://git.chifor.me/cchifor/relay/src/commit/e843921b7cff76406b7da0b5b35cf653c7e458c5/server/agent-sessions/native-recovery.ts#L122) and [`profiles.ts:469`](https://git.chifor.me/cchifor/relay/src/commit/e843921b7cff76406b7da0b5b35cf653c7e458c5/server/agent-sessions/profiles.ts#L469).
3. **Choose the immutable request.** Allocate one new operation UUID `O`; use the source's current positive generation, not the numeric example below. The default request omits acknowledgement:

   ```json
   {"operationId":"<O>","generation":1}
   ```

   Only after inspecting the possible filesystem effects of an interrupted tool may the operator explicitly choose:

   ```json
   {"operationId":"<O>","generation":1,"acknowledgeUnknownToolEffects":true}
   ```

   The optional field accepts `true` only; omit it for the default false policy. The checkbox starts unchecked. It imports an unknown outcome as an error result, never as proof of no effect or permission to replay. Do not change the selection/body under the same operation ID or automatically choose it after a refusal.
4. **Submit and inspect one attempt.** POST the selected exact body to `POST /api/agent-sessions/<S>/resume`. At this source the route returns the launch row; it does not use the ordinary launch endpoint's explicit 202 response. HTTP success is not completion. Save the returned launch `id`, `operation_id` and `resume_from`; use `GET /api/agent-launches` to find that same attempt and its `session_id`. A lost response requires lookup by the same operation ID and, if needed, an identical-body retry under the same human authority. Changed bodies are refused with `409 Native recovery operation changed`; a changed source generation/state is refused with `409 Native recovery source changed`. Never start a new operation merely because an offered/unknown attempt timed out. Durable holder claims are one-use, including failed or uncertain activation.

   The release correction tracked in [Relay PR681](https://git.chifor.me/cchifor/relay/pulls/681) adds one narrow exception **before** claiming history or attempting activation. With that reviewed correction deployed, an unacknowledged unresolved-tool history can return a durable `pi-recovery-acknowledgement-required` refusal with `recoveryPreflight.activationAttempted:false`. The hub must validate the matching offered Pi request, source/native/reserved-session/journal/holder identities and mark it `withdrawn`; the reason string alone is insufficient. The original request remains immutable. Only after that verified withdrawal and inspection of actual effects may the administrator choose a new operation with explicit acknowledgement. No generic rejected, accepted, unknown or timed-out attempt receives this exception. This preflight does not replace full launcher import validation or promise that acknowledged activation succeeds. Record the final published source containing the correction; the earlier merged679 source alone does not supply it. Do not add an intentional interrupted-tool exercise to this already bounded lifecycle phase.
5. **Verify completed recovery identity.** Require the launch row's `state:"open"` and `receipt.status:"completed"`; `receipt.result.sessionId` must equal its new `session_id`, differ from `S`, and `receipt.result.nativeSessionId` must equal the source native UUID. Require `receipt.result.nativeRecoveryVersion:2` and `receipt.result.generation:1` (the completed launch receipt contract), then re-read the new session's actual current generation before controlling it. `accepted`, `rejected`, `unknown`, `withdrawn` or a missing receipt is not completed recovery. A native identity/version mismatch fences the replacement; do not bypass it. Record the fresh communication identity and router installation without credentials, together with unchanged original router principal. Confirm marker files remain unchanged after import; native import itself must not call a provider or tool. Do not infer zero calls from marker equality alone.
6. **Acquire fresh control and continue explicitly.** On the replacement session, read session/router readiness and current generation/control epoch, acquire a new owned control lease using the normal request above, and retire all use of the source's controls and permission decisions. Do not reuse old approvals; any later tool requires a fresh reviewed permission. Follow the continuation section below: inspect branch state, reconcile only when required and explicitly selected, then send one bounded `RECOVERED_OK` probe. After completed receipt and quiescence run `python3 observe.py PROVIDER RUN_UUID recovered`. Record old/new Relay IDs, original native UUID, launch/command operation IDs, ownership/capability and completion receipts, fresh authority, branch/reconciliation evidence and unchanged append markers. Close the replacement and release its owned lease during cleanup.

A completed clean-close sequence must be labelled **clean-close owned-history recovery**. Claim worker-crash recovery only with independently recorded actual failure, retirement and matching recovery evidence; neither this sequence nor the observer's `recovered` phase alone establishes that failure scenario. Missing visibility or eligibility remains blocked/unobserved, not a reason to weaken a fence.

### Continuation after qualified owned-history import

Keep this phase blocked until the remaining execution gates above are met. A successful native import/open receipt does not prove router continuation is admissible. Even clean, settled history can retain an admitted branch whose configuration fingerprint differs because the replacement's fresh Relay communication principal appears in regenerated system instructions. Pi uses strict recovery headers; the actual router can refuse that continuation before provider execution.

Inspect the prior effects and confirm continuity of the original **router principal**, which is separate from the new Relay communication identity. A new installation or credential rotation does not transfer router sessions/receipts between principals. If the original principal or receipts are unavailable, record a blocked result; do not extract credentials or invent a replacement identity to bypass it.

For the retained branch requiring reconstruction, the operator explicitly submits `/relay-reconcile confirmed` as prompt text through the replacement's normal control path, using its new session ID, current generation/control epoch and owned lease. Require idle state and settle any cancellation first. Record the command's authenticated router `inspect` and `reconstruct` outcome, the observed operation/epoch and resulting branch, zero provider/tool calls during reconciliation, and unchanged effect markers. Then submit one bounded probe: `Reply exactly RECOVERED_OK. Do not use tools, repeat earlier commands, or modify files.` Require a completed turn on the reconstructed branch and unchanged append-only markers before recording recovery acceptance. A refusal remains a block, not permission for an automatic retry or reconciliation.

The optional resume field `acknowledgeUnknownToolEffects:true` and its unchecked-by-default checkbox permit importing interrupted tool outcomes as unknown error results after inspection. They do not perform router reconciliation, approve future tools or establish that effects were absent. Do not select the checkbox or run reconciliation automatically. A new/explicitly branched history or an already reconstructed, never-admitted branch need not reconcile again solely because it was imported; inspect the branch state rather than force a refusal first.

Current packaged evidence distinguishes these paths: Codex and Messages test clean-import refusal followed by explicit reconstruction; stateless tests explicit branching after clean import plus cancellation/crash reconciliation. The Messages fixture uses the real router and packaged Pi with a synthetic Claude upstream, verifies the fresh communication identity and unchanged append-only effect, and makes no provider/tool calls during import or reconciliation. These launcher/router fixtures do not establish deployed normal-API recovery or live subscription acceptance. Require the actual provider-specific live receipts before claiming all three passed.

## Cleanup, revocation and evidence

After settled observations, close with the exact close command above and record its receipt. Release the owned lease using `DELETE /api/agent-sessions/<S>/control/<L>` with no body only when outstanding work is settled; release can withdraw/unknown outstanding commands. Preserve marker evidence until owner review; this runbook does not delete files.

When the owner ends the canary grant, use `POST /api/router-credentials/<BINDING_UUID>/revoke` with `{ "operationId":O, "revision":CURRENT_BINDING_REVISION }`. Revision comes from current metadata, not the launch profile's revision. Track the 202 operation through credential list/metadata and rendered readiness until revoked/unavailable; `GET /api/router-credentials/keys/<CREDENTIAL_ID>` uses the credential row ID, not the binding UUID or secret. Confirm no further inference is admitted under the revoked binding and no extra provider attempts occur. Normal hub admission may reject before a provider request; distinguish this from an actual router revoked-key wire test. Never print or manually reuse the key to perform that test. Profile revocation, if specifically part of owner cleanup, is `POST /api/agent-profiles/<P>/revisions/2/revoke` with `{}` for the proposed revision2 profiles; target the actual reviewed revision if it changes. This is separate from key revocation.

Save one minimized evidence manifest per provider/run: source/digests; profile and binding revisions; gate/window reference; launch/session/native-history correlation; operation IDs, generations/epochs, lease ownership; permission request/decision receipts; timeline sequence boundaries; router route/protocol/branch/attempt metadata; observer phase JSON; cancellation/compaction/reconciliation outcomes; revocation readback; durations and warm/cold designation. Exclude credentials, cookies and raw signed native history. Keep failed/inconclusive/blocked steps explicit. No fallback/one-attempt evidence and exact-once local effects are separate claims. Cache/warm measurements and rollback remain operator work after acceptance gates; no aggregate “all three passed” claim while restart or any provider step remains unproved.

## Source and offline validation

At the pinned Relay commit, request shapes come from `packages/contracts/{launch-profiles,session-control,agent-sessions,router-management}.ts`; registered routes and admission from `server/agent-sessions/{profiles,control,routes,native-recovery}.ts`, `server/router-credentials/routes.ts` and `server/release-admission.ts`. Pi commands and history semantics come from `packages/pi-launcher/main.ts` and `packages/pi-policy/session.ts`; minimized permission presentation comes from `holder/src/runtime.rs`.

Before the revision2 layout correction, offline validation on 2026-11-25 passed 28 positive and five negative schema cases using the pinned image's compiled contracts with `--network none`: all three revision1 definitions/grant bodies, representative launch/lease/prompt/cancel/close/permission/revoke bodies, and six concrete denied/effects append prompts. Negative cases reject cancel/close with an epoch, prompt without an epoch, invented `maxTurns`, and selected permission without an option ID. The existing `validate.py` also passed profile/binding/renderer/byte-pin checks. The final published revision2 candidate passed the compiled-image schema checks described above; this does not grant authority or establish live behavior. The prepared v2 resume request and launch receipt shapes above were source-checked at merge commit 1044cb197400a8416804b7ce79f8d004f2821d0d in server/agent-sessions/{native-recovery,profiles,launch-transport}.ts; they were not exercised against a live hub. Runtime admission, provider behavior and resume eligibility are not established by schema parsing. Existing packaged synthetic-provider tests and the observer's local tests also do not establish live acceptance.
