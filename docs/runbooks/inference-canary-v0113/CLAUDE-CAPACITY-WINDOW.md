# Optional Claude account-capacity window

Operator scope decision **Relay bus 2086 (2026-11-26)** retains two fresh sessions
and four planned prompts, with **at most six minutes / five observed upstream
generations** and at most one scoped Relay read per prompt. The 120-second prompt
limit, 1024-token cap, no-retry rule and existing cleanup remain unchanged. This
scope decision does not itself start the exclusive window or mutate the account.
Use current Router source `4e4bb34e2f968f9450aabe9cf228d3e70c8bab71` and image
`registry.chifor.me/llm-router/router@sha256:f180cf7f1f960782a46dff6bb73cb955ce34dcb6e083daed0b1202f58753b457`;
[the release bridge](../relay-router-messages-native-output-20261126.md) preserves
the account/configuration semantics cited below. The following readback remains
historical and must not supply a write precondition.

The saved read-only observation at **2026-11-26 14:56:06.878 UTC** shows
`canary-pi-claude` targeting account `claude`: configured concurrency **4**,
effective shared-pool capacity **4**, one enabled pool member, queue disabled.
The Router source is `4af66b8c7c3cc83ddd10e326c554ad728caad72e`, on published image
`e98a8e189ce9d6fe2d6c2bd206594bb47922de0a49acafb82b3ff77fde471d69`.
Config revision 2882/hash `b1d75f1d92f106149524dfea7495baa07b9f568e714e6f935d7ca2d0f84e6afd`
is historical input, never a future write precondition. This observation proves
configuration only, not occupancy or an exclusive window.

With a drained pool, one active test session plus a second request cannot reach
capacity 4. The [default capacity procedure](WARM-ACCEPTANCE.md#separate-capacity-phase)
keeps existing limits. **This optional procedure requires its own explicit
operator decision for the exclusive window and the temporary account-limit
change before any write.** Until that decision, keep the capacity row unexercised.
Merging this documentation, publishing an artifact or reading capacity metadata
grants no execution authority.

The optional exception is exactly account `claude` concurrency **4 → 1 → 4** in
the already required exclusive, drained window. Keep global
`provider-claude.maxRunning: 8`, every other account field, pool identity, queue,
route, fallback, grant, model, capture setting and existing warm settings
unchanged. Use the authorized capacity budget: **≤6 minutes, ≤2 fresh sessions,
4 prompts, ≤5 generations**, 120 seconds per prompt, 1024 output cap, existing
permission/lease/cleanup controls. Do not compensate by creating extra sessions,
holding unrelated requests, changing queue settings, or consuming another seat.

## Supported source API and request shape

The supported HTTP write is **`PUT /admin/v1/config`**, administrator only. It
replaces the complete RouterConfig and requires `If-Match` containing the current
**integer global config revision**, as an unquoted decimal string. It is not the
64-hex plugin-settings revision. Missing/invalid revision produces 428
`REVISION_REQUIRED`; a concurrent committed change produces 409
`REVISION_CONFLICT` before applying this write.

There is no account-concurrency PATCH endpoint in this source. Do not invent
`PATCH /admin/v1/accounts/claude` or use the provider-claude settings endpoint:
the latter controls a different global process cap.

After authorization, use the operator's existing authenticated Router workflow:

1. Verify the exact source/image and selected window. Obtain a **fresh full admin
   GET `/admin/v1/config`** into the existing private evidence context, not chat.
   The sanitized capacity report cannot serve as a PUT body. Config bodies can
   contain identity and credential references; do not print/export them into
   shared reports or place credentials in argv.
2. Require one exact direct single-target `canary-pi-claude` route, model `sonnet`,
   `maxAttempts:1`, no fallback, target account `claude`, expected namespace,
   provider `claude`, enabled/ready state, concurrency 4, one enabled pool member,
   queue disabled, and no changed identity/credential ownership. If any drift is
   found, stop and have the operator re-evaluate; do not adapt this exception.
3. Verify the pool is drained using the existing operator observations and hold
   all other callers/probes of that account for the whole experiment and
   restoration. Lowering the limit is not a drain or cancellation mechanism.
   Calls already admitted can retain earlier configuration; their presence would
   invalidate this two-session comparison.
4. Prepare a private copy of that exact fresh config. Leave its `revision` equal
   to the fresh value; change only `accounts[indexOfExactClaude].concurrency`
   from 4 to 1. Compare the complete input objects: exactly this one scalar must
   differ. Do not reconstruct a partial accounts list, overwrite settings, or
   copy the historical revision 2882 config.

The HTTP request is this template, not a staged request:

```http
PUT /admin/v1/config
If-Match: CURRENT_FRESH_INTEGER_REVISION
Content-Type: application/json

COMPLETE_FRESH_ROUTER_CONFIG_WITH_ONLY_EXACT_CLAUDE_CONCURRENCY_CHANGED_TO_1
```

No authentication value, actual fresh revision, config body or execution UUID is
provided by this document. This endpoint has no client operationId parameter;
retain the private request, input revision/hash, time and response for readback.
Do not invent idempotency or retry automatically after a lost response.

The server validates, audits and commits the change as a new revision. Registered
config followers may commit further revisions before PUT returns. Therefore do
not assume the returned revision is exactly input+1. Re-read config and the
bounded capacity metadata. Require selected/effective capacity 1, one member and
queue disabled; compare the complete config against the prepared baseline,
allowing only concurrency and server-managed revision. Any additional follower
change requires review before prompts; do not silently accept it or overwrite it.
Verify global `maxRunning` remains 8 through the existing settings readback.

An alternative **existing Router Assistant tool**, not an HTTP endpoint, is
`update_account` with the exact proposed arguments below. It creates a change
plan whose `baseRevision` and diff must be inspected before the normal authorized
apply. Its apply uses the same compare-and-swap config update. Select either this
existing workflow or the HTTP workflow; do not execute both.

```json
{"account":"claude","concurrency":1}
```

## Execute only the existing bounded capacity row

With fresh capacity 1/no-queue proof and other callers held, start planned A and
observe it actually active. Send planned B while A still holds admission. Require
the genuine account-admission refusal, no added provider child and zero covered
attempt delta for B, using the existing receipt/counter/proc evidence limitations.
Provider process-slot or warm-pool failures are not equivalent. If A settled too
soon, another cause blocked B, or coverage is incomplete, mark the row
unexercised/inconclusive. No extra prompts or retries to manufacture a pass.

Use the [scoped Relay-read rule](LIVE-ACCEPTANCE.md#scoped-relay-communication-reads),
at most once per prompt, with full input review and a separate owner decision.
Count every read continuation, observed upstream attempt and owner-review delay.
Before prompts and permission continuations, reserve/check the remaining window
and generation allowance: two for A, one for B's refusal probe, zero for the
explicit local reconciliation command, and two for B's released continuation.
Do not double-count a reserved continuation. B should consume zero upstream
generations on account-admission refusal; any unexpected execution counts and
fails that claim. Unknown coverage, a second read or insufficient budget stops
advancement through normal cleanup; no extra prompt or automatic reconciliation.
Peer traffic remains excluded. A read permission wait does not hold an upstream
admission slot: tool boundaries reap workers, so submit B only during observed
active upstream work on A. Keep per-attempt latency and owner decision time
separate in the record.

Cancel A if active and wait for the existing settled cleanup. Only the already
planned B reconciliation/continuation is permitted. Session closure, lease
retirement, unexpected permissions and unknown-effect handling stay exactly as
the current procedure states. Stop on the existing deadline. The metadata helper
alone does not establish provider refusal, saturation or cleanup.

## Restore only the changed field, with a fresh revision

After the planned requests settle and the test's session/permission cleanup is
accounted for, keep the exclusive window held and obtain a **new full config
snapshot**. Match the same account/namespace/pool identity and the recorded
temporary change. Require current concurrency 1; prepare a private copy of the
**latest** config changing only that scalar back to the recorded original 4.
Use `PUT /admin/v1/config` with this latest integer `If-Match`. This preserves
intervening legitimate changes instead of restoring a stale whole-config image.
For the existing `update_account` tool alternative, create the restoration plan
only after this post-test fresh snapshot; do not pre-stage it before the window.
Require its `baseRevision` to equal that snapshot's revision and inspect its
field-only diff before the normal authorized apply:

```json
{"account":"claude","concurrency":4}
```

Do not use whole-config `revert_config`/historical restore: that can undo unrelated
intervening changes. A 409 stops this attempt; an operator may inspect the new
revision and prepare a newly reviewed field-only restoration, with no automatic
retry loop. A lost write response requires config/history readback before any
further write. If current concurrency is already 4, verify the corresponding
restoration and record it instead of writing again. Any other value, identity
change or ambiguous outcome requires operator resolution; do not guess.

Finally require readback of selected/effective capacity 4, one pool member, queue
disabled, and global maxRunning 8. Preserve all other current fields; retain the
private before/request/response/after snapshots and sanitized hashes/revisions.
Record whether each test effect and restoration was proven. Do not release the
exclusive window or claim the cleanup passed while configuration restoration or
running work is unresolved. The existing overall C cleanup still restores
`warmSessions:false` with its own separate fresh plugin-settings revision; this
proposal does not change that behavior.

## Recorded observation and source boundaries

The nonsecret metadata below was read from the published image, between matching
before/after deployment identities, using the existing Router admin context.
It is a historical configuration snapshot; refresh it before the proposed window.
The raw JSON report SHA256 is
`1b1aec5d2dd5a1438afddc2c60e7d52d770705840054b2d00316c783ef5dea37`.
Whitespace in this excerpt is not the raw report's byte representation.

```json
{
  "source": "4af66b8c7c3cc83ddd10e326c554ad728caad72e",
  "observedUtc": "2026-11-26T14:56:06.878Z",
  "configRevision": 2882,
  "configSha256": "b1d75f1d92f106149524dfea7495baa07b9f568e714e6f935d7ca2d0f84e6afd",
  "selectedClaudeConcurrency": 4,
  "effectiveCapacity": 4,
  "poolMemberCount": 1,
  "queue": { "enabled": false }
}
```

The read-only helper checked the exact source before/after one local config GET,
refused redirects and emitted no raw identity/pool/credential fields. Its SHA256
was `d4e4b30faa399701e6f0535b782f0078cc1c258762376952f2756b25524a08e8`.
The earlier [deployment readback](https://git.chifor.me/cchifor/ailab/pulls/2019#issuecomment-144742)
records the published image/source and the separate provider `maxRunning:8`.
These observations do not prove occupancy, an exclusive window, or live refusal.

Exact deployed-source references:

- [Config GET/PUT](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/plugins/api-admin/index.ts#L24):
  full config body, admin write, integer If-Match and 428 for an invalid revision.
- [Serialized update and followers](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/plugins/config-store/index.ts#L67),
  [revision conflict, validation and audit](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/plugins/config-store/index.ts#L92).
- [update_account arguments](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/plugins/config-store/tools.ts#L378),
  [field-only draft](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/plugins/config-store/tools.ts#L400)
  and [revision-bound plan apply](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/plugins/config-store/tools.ts#L43).
- [Shared-pool identity](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/modules/accounts/index.ts#L22)
  and [effective minimum concurrency](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/modules/routing/index.ts#L219).
- [Queue aggregation](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/modules/inference/index.ts#L75),
  [account reservation](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/modules/inference/index.ts#L695)
  and [admission check](https://git.chifor.me/cchifor/llm-router/src/commit/4af66b8c7c3cc83ddd10e326c554ad728caad72e/packages/modules/admission/index.ts#L48).

This document records a source-reviewed proposal and existing metadata. No limit
change, provider execution or capacity acceptance was performed in preparing it.
Reachable capacity 1 would be a controlled test configuration; it would not
demonstrate saturation of the normal capacity 4 configuration.
