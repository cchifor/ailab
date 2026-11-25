# Paired Pi live acceptance — prepared 2026-11-25

This is an operator procedure, not a record of successful live provider acceptance. The documentation and read-only observer can merge independently of live gates. Existing owner/operator sessions retain authority for activation and late managed credential issuance; Claude trust/Messages and holder activation remain tracked in AILab PRs 2002/2003. Refresh their state before executing. Do not infer approval from this document.

The normal hub API below is pinned to Relay v0.12.1 source [`dfc739a492217ea5a8dfaf2471aead43b2ea646c`](https://git.chifor.me/cchifor/relay/src/commit/dfc739a492217ea5a8dfaf2471aead43b2ea646c), image `registry.chifor.me/relay/control-plane@sha256:177c1af20e5708af2ceb01e4e6360bd0b08a4859f50edad3784257ef74ea350f`. The directory's v0113 name identifies the prepared Pi artifacts, not the live hub version. Recheck source identity if the hub changes.

## Preconditions and limits

Use the existing authenticated `relay_session` browser session at `https://relay.chifor.me`; never paste cookies, lrk keys, native subscriptions or rendered credential files into evidence. Requests below are relative to that origin. Mutations use `Content-Type: application/json` and same-origin `Origin: https://relay.chifor.me`. Operator role covers launch/control/decisions; admin covers profile approval, credential management and native-recovery endpoints. Keep the same human session for the control lease. These are request recipes, not an unattended driver.

Before each provider:

1. Record owner acceptance window and operator gate, deployed router/hub source and digest, exact profile revision, holder/artifact identity, trust and health. The checked-in revision2 profiles correct `credentialHome` to `state/profile-homes/<profileId>` and remain held pending final matching owned-recovery release pins and renewed exact-byte approval. Do not run this procedure on the old release while waiting for that upgrade. Require `nativeStateReady:true` from the actual holder state root; `state/native-homes` belongs only to holder-created session homes, not precreated profile homes. Existing directory/profile disposition is a separate reviewed operator step, never automatic cleanup. Staged/disabled is not active. Preserve operator KEEP exceptions and do not change production pools to make a test pass.
2. Confirm dedicated route exists, has the intended protocol/provider, one enabled target, `maxAttempts:1` and no fallback; confirm one-route managed grant. Model selection must be `{ "kind":"model", "model":"canary-pi-<provider>" }`, never a direct provider model or `x-router-route`. Qwen has its own local-provider readiness gate.
3. For Claude, establish cold route, trust and Messages prerequisites, including the agreed `warm:false` condition, **before credential issuance**. Do not issue Claude credentials until the owner acceptance window. Do not turn direct qualification into a warmup or reuse a historical report path.
4. Inspect profile admission: these checked-in definitions use `qualification:"protocol-fixture"`. Pinned `server/release-admission.ts` refuses fixture profiles when `releaseApprovalFile` or `releaseApprovalSha256` is set (409, `Release mode refuses fixture profiles`). If enabled, stop and resolve the approved profile/admission path with the owner; do not disable release mode or relabel the fixture.
5. Use only the existing reviewed bindings and owner-managed grant workflow. Inspect `GET /api/router-credentials` and matching metadata. If issuance is still required, the exact reviewed request is the matching object in [binding-requests.json](binding-requests.json), sent by the owner to `POST /api/router-credentials`; do not create alternate direct keys. HTTP 202 is not proof of rendering/readiness. Wait for current managed delivery and holder readiness, then inspect the session router status after launch.
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
| Reconcile | `/relay-reconcile confirmed` | Only after cancel settles and possible effects have been inspected: previous branch retired, reconstructed branch acknowledged, no tool repeated. |
| Reconcile probe | `Reply exactly RECONCILE_OK. Do not use tools, repeat earlier commands, or modify files.` | Completed turn on reconstructed branch, no repeated marker. |

Short history may refuse compaction; record it as unexercised, not successful. Do not pad history indefinitely. Cancellation can lose the race to completion: `cancelSent:true` alone does not prove an interrupted turn, and a completed target before cancel is inconclusive for cancellation. Do not manufacture uncertainty by killing production services. Reconciliation internally inspects and reconstructs `/v1/inference/sessions/reconcile`; use the Pi command, not a manually extracted credential. A successful settled reconciliation does not prove interrupted-tool recovery. Re-read marker effects after settled controls with the `approved` observer phase; they must still be exactly once.

## Restart/resume limitation at this pinned hub

`GET /api/agent-sessions/<S>/native-recovery` and `POST /api/agent-sessions/<S>/resume` are admin endpoints. The resume body is:

```json
{"operationId":"<O>","generation":1}
```

This shape is valid, but **native Pi restart/resume is unsupported through this pinned normal hub path**. `server/agent-sessions/native-recovery.ts` requires an ended root session, approved `holder.persistent.kind === "dsh"`, recovery version 1, journal ownership and a fresh matching holder. An open session reports `Only an ended root session can be resumed`; these Pi profiles, once ended, fail the supported ownership/profile check. Do not POST resume merely to force it, substitute a dsh profile, launch a fresh session and label it recovery, or restart the shared holder/hub as part of this recipe.

Record paired Pi restart acceptance as **blocked for the deployed v0.11.3 Pi artifacts with the pinned v0.12.1 hub**. The launch, permission, branch, compact, cancel and reconcile checks above remain independently usable subject to their gates; report their individual outcomes without claiming recovery passed.

As of 2026-11-25, the replacement owned-history recovery path is being implemented in Relay worktree `relay-pi-owned-recovery-20261125`: durable Rust holder/launcher history ownership, hub recovery capability v2, persisted acknowledgements and API tests. This is implementation in progress, not a qualified release or deployed capability. Before adding a live recovery phase, require:

1. Reviewed new source commits and passing holder/launcher and hub API recovery tests, including ownership, durable acknowledgement, stale-generation and replay fencing.
2. Release qualification of the matching hub, holder and Pi artifacts; record exact source commits, image/artifact digests and approved profile revisions. Existing v0.11.3 artifact qualification does not qualify the new recovery path.
3. Owner/operator-approved deployment and fresh capability/ownership readback from the actual paired components. Re-pin this runbook and validate the final request/response schemas against that release; do not assume the in-progress capability or the old resume body establishes eligibility.
4. An agreed bounded recovery exercise using the normal API, with continuity of the intended native history, valid generation/lease, stale-generation rejection, no replay of either append command, completed recovery receipts and quiescence before `python3 observe.py PROVIDER RUN_UUID recovered`.

The observer's `recovered` phase alone cannot establish restart or hub recovery. Until these prerequisites are met, retain the explicit blocked result; do not manufacture a `/resume` pass.

## Cleanup, revocation and evidence

After settled observations, close with the exact close command above and record its receipt. Release the owned lease using `DELETE /api/agent-sessions/<S>/control/<L>` with no body only when outstanding work is settled; release can withdraw/unknown outstanding commands. Preserve marker evidence until owner review; this runbook does not delete files.

When the owner ends the canary grant, use `POST /api/router-credentials/<BINDING_UUID>/revoke` with `{ "operationId":O, "revision":CURRENT_BINDING_REVISION }`. Revision comes from current metadata, not the launch profile's revision. Track the 202 operation through credential list/metadata and rendered readiness until revoked/unavailable; `GET /api/router-credentials/keys/<CREDENTIAL_ID>` uses the credential row ID, not the binding UUID or secret. Confirm no further inference is admitted under the revoked binding and no extra provider attempts occur. Normal hub admission may reject before a provider request; distinguish this from an actual router revoked-key wire test. Never print or manually reuse the key to perform that test. Profile revocation, if specifically part of owner cleanup, is `POST /api/agent-profiles/<P>/revisions/2/revoke` with `{}` for the proposed revision2 profiles; target the actual reviewed revision if it changes. This is separate from key revocation.

Save one minimized evidence manifest per provider/run: source/digests; profile and binding revisions; gate/window reference; launch/session/native-history correlation; operation IDs, generations/epochs, lease ownership; permission request/decision receipts; timeline sequence boundaries; router route/protocol/branch/attempt metadata; observer phase JSON; cancellation/compaction/reconciliation outcomes; revocation readback; durations and warm/cold designation. Exclude credentials, cookies and raw signed native history. Keep failed/inconclusive/blocked steps explicit. No fallback/one-attempt evidence and exact-once local effects are separate claims. Cache/warm measurements and rollback remain operator work after acceptance gates; no aggregate “all three passed” claim while restart or any provider step remains unproved.

## Source and offline validation

At the pinned Relay commit, request shapes come from `packages/contracts/{launch-profiles,session-control,agent-sessions,router-management}.ts`; registered routes and admission from `server/agent-sessions/{profiles,control,routes,native-recovery}.ts`, `server/router-credentials/routes.ts` and `server/release-admission.ts`. Pi commands and history semantics come from `packages/pi-launcher/main.ts` and `packages/pi-policy/session.ts`; minimized permission presentation comes from `holder/src/runtime.rs`.

Before the revision2 layout correction, offline validation on 2026-11-25 passed 28 positive and five negative schema cases using the pinned image's compiled contracts with `--network none`: all three revision1 definitions/grant bodies, representative launch/lease/prompt/cancel/close/permission/revoke bodies, and six concrete denied/effects append prompts. Negative cases reject cancel/close with an epoch, prompt without an epoch, invented `maxTurns`, and selected permission without an option ID. The existing `validate.py` also passed profile/binding/renderer/byte-pin checks. Revalidate the final revision2 bytes after the matching release pins are filled. Resume's local request schema was source-inspected, not exercised against a hub. Runtime admission, provider behavior and resume eligibility are not established by schema parsing. Existing packaged synthetic-provider tests and the observer's local tests also do not establish live acceptance.
