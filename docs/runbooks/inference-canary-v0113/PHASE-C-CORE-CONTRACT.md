# Selected warm C core contract

`owner-phase-c.cjs` is the fixed four-row core built on the reviewed A Runner
and B credential validator. It does not create an authenticated transport,
signer, account, profile, credential, execution window or controller process.
The shared host/CLI integration and final Router adapters are separate work.

**The live entry is disabled.** `QUALIFIED_ROUTER_RELEASE` is deliberately null.
`start()` refuses before any API, process, settings or fence call. A future
reviewed source commit must pin the actually published combined timing/fence
release and its qualification SHA. Input data cannot override this constant.
The current core is not proof that the proposed fence API exists or is deployed.

## Fixed plan and prerequisites

The public factory is `createClaudePhaseCRunner(env, inputs, ports)`. It returns
the same `start()/stop()/renew()/state` shape used by the shared host. It accepts
no caller-selected Runner, plan, prompt, tool or URL. `replaceStopped()` and
`revokeFinal()` refuse. A completed C still has `completed:false`: capacity,
active-generation drain and final grant cleanup remain separate.

Inputs: version1; distinct runId/phaseId UUIDs; actual account/model; a currently
open window of at most fifteen minutes; SHA256 references for the actual A,
cold, paired and exclusive-window records; bounded raw completed B report.
The raw B report must show both Claude lifecycle and effort completed for the
selected account/model, with the producer's exact
`passed-with-admission-fence-live-wire-unobserved` status for both runs. The old
unfenced completion label is not a current B prerequisite. Both retained fence
phases must be closed, with their settled 8/2 rows, fixed 20/6 phase caps,
continuous raw/admitted counters, no open/stale/unattributed attempts and matching
committed normal-close operations. C joins their raw-body digests, receipt IDs,
key/profile/source/selection, phase/run IDs and final current fence. It reuses
B's drain validator for the retained terminal shape and absence of pending work;
those historical observations are not presented as fresh C readiness. Settled
token-usage uncertainty is preserved and is not mistaken for an unknown attempt.
C retains the raw report's hash rather than importing its mutable
state. The trusted bundle must bind the report to its actual execution; schema
validation alone does not prove origin. A's original one prompt/two attempts
remain in A's preserved report and cannot fund C.

The component and disabled-host tests use the unchanged output of the prior
synthetic B host qualification (`pi-phase-b-fence-full-host-20261128-r2`, final
snapshot337, SHA256 `d0532ad01ec6aea711d1aefae6b46329d88cb2170a8d6a1d4201c8cf5a848fd5`).
`owner-phase-c-b-report-fixture.cjs` changes only the isolated B dependency's
Router pin from 4e4 to that report's qualified historical 02d source. It retains
before/after hashes and leaves C/host/CLI bytes and public NULL gates unchanged.
The raw report is never relabelled. This is producer/consumer compatibility,
not a new B run or a claim of live fence/Pi authority. Earlier C stack results
using a synthetic unfenced B prerequisite remain historical evidence.

| Row | Exact reviewed prompt                      | Admission ceiling |
| --- | ------------------------------------------ | ----------------- |
| P1  | WARM_FIRST_OK, frozen warm index0          | 2                 |
| P2  | WARM_SECOND_OK, frozen warm index1         | 2                 |
| P3  | exact `pwd` permission, frozen warm index3 | 3                 |
| P4  | COLD_ROLLBACK_OK, frozen warm index5       | 2                 |

There are four prompt commands and nine total nonrefundable attempt admissions;
unused row allowance cannot transfer. Each prompt keeps A's120-second deadline,
full operation/permission/native-tool checks and one scoped Relay read maximum.
The installed Pi/profile still supplies its existing1024-token output ceiling.
The host renews the existing control lease every20s; this core adds no renewal
timer. No B0 rerun, extra probe, capacity prompt, compaction or branch repair.

The controller persists two fixed subplans and their distinct phase/fence UUIDs
before any effect. Warm P1/P2/P3 has ceiling7 and rows2/2/3; cold P4 has ceiling2.
Both bind the same parent C plan, run, key and deadline. Warm settings are enabled
before the warm fence is armed with the **observed actual** provider generation.
After settled P3, close that fence, set warm:false **and original idle in one
CAS**, then observe the actual new generation. Only then may cold arm name the
closed warm predecessor and its exact revision, attempts and token-unknown count.
Unused warm allowance is retired. The original lifetime C baseline remains;
the additional aggregate bound is nine, never a reset at cold arm. There is no
third settings refresh after P4 and no cross-plan P3-to-P4 advance operation.

## Host-owned ports and lifetime

`env` is the existing guarded A environment: origin, crypto, load/save, fetch,
ask, cancelCheckpoint, and optional now/sleep. It remains Relay-only. In
particular the existing Unix socket is **not** widened to Router PATCH/admin.

`ports` has `signal`, `cleanupSignal` (the SAME shared host execution/cleanup
signals) and a trusted synchronous `monotonicNs():bigint`. It also has exactly
these named service groups:

| Group     | Methods                      | Purpose                                                              |
| --------- | ---------------------------- | -------------------------------------------------------------------- |
| readiness | read                         | Fresh source/selection/holder/prerequisite/meter identity projection |
| process   | start, next, stop            | One concurrent bounded metadata observer per joined scope epoch      |
| settings  | read, replace                | Full-settings CAS on the one Claude plugin                           |
| fence     | arm, inspect, advance, close | Existing signed issuer-management surface, once implemented          |

Every asynchronous call receives `{scope,signal,...methodFields}`. The host must
wrap and track these callbacks in its existing transport/collector drain before
constructing the core. It must include response-body consumption and bound
uncooperative callbacks by the same absolute execution/cleanup deadlines. No
work may start after the host returns. Do not hand the core an untracked generic
HTTP client. Service identity/credentials are supplied by the reviewed host
context; no header, secret, proxy or arbitrary command enters these inputs.

Normal calls use `signal`. Cleanup fence inspect/close, settings read/replace
and process stop use `cleanupSignal`. `process.next` receives a combination of
the host execution signal and the core's one local monitor-stop signal. That
local controller only stops the observer; it does not create another execution
window. `stop` must release a pending `next`; the core joins the monitor task.
The host must preserve the cleanup signal long enough for the bounded join.

The initial cold baseline/Open observer, warm observer and final cold observer
have separate immutable scopes. Each old observer is drained, stopped, joined
and archived before the next starts with the same runtime/root. Old events keep
their historical generation after a settings refresh; they are never retagged.
The transition explicitly has an observation gap after complete drainage, with
no prompt permitted in that gap. New epochs require empty scoped events and no
live children or open requests. Final evidence preserves every epoch.

On failure the core first attempts to close the fence, then uses inherited
session/permission cancellation, Close and lease cleanup; it next restores
warm:false/original idle when still owned and stops the observer. Each cleanup
failure is retained independently and does not authorize retries or erase
other outcomes. An uncertain original command is reconciled under its original
operation ID by A's existing cleanup.
Fence cleanup retains a separate context from the current observer scope. Before
a cold arm intent exists, the closed warm fence remains the target. After a cold
arm intent, a mismatching current marker is unresolved; it never licenses a new
arm or identity. Cleanup cannot create a successor plan.

## Projection contracts (normalizations, not invented HTTP responses)

Readiness includes exact hub/holder/Pi/profile pins from B, the final qualified
Router source, observed living holder identities, no active sessions/commands,
actual selected account/model, native Messages/sdk-resume/CLI2.1.283/trust,
single/maxAttempts1/no-fallback, warm:false, matching prerequisite hashes,
Router runtime and durable-state deployment IDs, target deployment/provider
generation, key creation time, original admitted count/fence revision and known
observer root PID/startTicks. Credential/binding identity comes separately from
actual Relay list/detail through `b.credential`; no projection can replace that
validation. The source-bound adapter is responsible for proving freshness and
coverage of its underlying queries, never fabricating missing fields.

The immutable scope binds these to the run/phase/window, key/principal/namespace,
binding, credential generation, profile and observer root. Its canonical SHA256
is `scopeDigest`. Router's durable-state deployment ID is distinct from its
selected route-target deployment ID.

Process `start/next` return complete cumulative scoped projections:
`{version,source,scopeDigest,runtimeInstanceId,observedUtc,projectionSha256,cursor,
root,coverageComplete,gaps,events,processes,absent,settledRequestIds,openRequests}`.
The increasing cursor is a normalized observer snapshot cursor, not a claimed
Router API field. Each event retains requestId/attemptId/journal sequence plus
the exact allowed process phase/mode/turn/reuse/identity/outcome/monotonic ticks,
accountId/providerGeneration. No model text, tool args, argv, environment,
credentials, executable name or arbitrary extra fields are accepted. At most128
events/samples/absence records and16 settled requests are permitted.

Process samples contain only pid/ppid/startTicks/rssBytes. Absence contains only
pid/startTicks/status=`absent-at-read`; a previously observed matching identity
is required. Missing events, PID reuse without explicit original absence,
changed runtime/root, gaps, truncated history, conflicting duplicates or missing
native/terminal phases stop advancement. Sampling cannot prove the absence of
between-sample processes or identify remote provider execution. `projectionSha256`
names preserved nonsecret source evidence; it is not a cryptographic substitute
for the trusted collector or actual records.

Settings `read` returns
`{source,revision,values,active,pending,projectionSha256,providerRuntime}`.
It must reflect the actual plugin projection (pending:false, values===active).
`providerRuntime` is `{runtimeInstanceId,providerGeneration,observedAt}`, with a
numeric observation time no more than five seconds from the controller's clock.
It must come from the actual registry/admin metadata in the same Router runtime.
The adapter brackets that read with equal settings revision/value reads. This
joined normalization is not an assertion that the settings HTTP response has
these extra fields. Null/missing identity, runtime replacement, stale evidence,
or an unchanged generation after changed settings refuses. Never infer a
generation by incrementing an earlier value or from a plugin revision.
`replace` takes `{intentId,expectedRevision,values}` and returns a normalized
`{intentId,projectionSha256}` only after the one actual PATCH has replied. The
intentId is LOCAL evidence, not a nonexistent Router idempotency field. The
adapter uses the actual settings If-Match and full `{values}` replacement.
The core rereads active state, preserves all unrelated fields, and does not
replay a lost response. Cleanup can inspect that last intent and issue a distinct
narrowing CAS only while the values/revision still prove ownership. Foreign
changes are unresolved, never overwritten.

Fence methods map to Faraday's separately reviewed strict contract at
`POST /register/relay-keys`: arm/inspect/advance/close. The core supplies the
fixed plan and original deadline, scope digest, key creation/deployment/target
identity, expectedFenceRevision, expectedAttempts and expectedUnknownUsage.
expectedAttempts is the durable
ADMITTED counter, not raw trajectory attempts. Mutation operationId is durable
before dispatch; unknown outcomes are not blindly repeated.

The normalized fence result is
`{version,source,scopeDigest,projectionSha256,operationId?,fence}`. Fence contains
the immutable identity/plan/selection/deadline, revision/state/activeRowId,
baselineAttempts/rowStartAttempts/attempts, baselineTokenUsageUnknown/
rowStartTokenUsageUnknown/tokenUsageUnknown, openRequests/staleOpenRequests,
unattributed and coveredFrom. This is an adapter
contract awaiting binding to the real API, not proof of implementation. Advance
requires actual native prompt completion, validated tool delivery/outcomes, zero
pending client prompt, complete settled process requests and zero meter opens.
Before either advance or close, the inspected admitted and token-unknown totals
must exactly match the previous row's recorded terminal evidence, bound to its
fence, phase, scope and active row. An extra settled admission within that row's
remaining allowance is still refused before any advance or next prompt; a new
row's baseline cannot hide it.
The terminalEvidenceSha256 attributes that controller evidence; Router cannot
verify the digest as native completion. Close blocks and preserves charges;
open/stale/unattributed reservations can remain unresolved. No new phase follows cleanup.

Unknown TOKEN usage is distinct from an unknown admission: a known charged
cancelled/incomplete generation can lack final tokens. Preserve that monotonic
counter and use exact expectedUnknownUsage CAS; never refund its attempt. First
arm requires zero unknown tokens; later phases carry the closed predecessor's
counter. Require zero openRequests/staleOpenRequests/unattributed and continuous
coverage from keyCreatedAt for advancement. A successful mutation increments
the control revision exactly once and reports the exact compared attempt and
token-unknown totals. The core never translates tokenUsageUnknown to an
unattributed-attempt failure or discards it from evidence.

## Native timing and permission checks

The process monitor runs concurrently with actual A prompt/permission handling.
C does not reuse A's five-second post-action skew wait as its tight-row clock.
P2's FIRST generation must reuse P1's FINAL retained identity and next turn;
P2's final process may differ after a read. Native-complete→first-fetch-invoked
uses qualified645bbd semantics in the same Router runtime clock domain, with
unchanged10s/20s limits. Cold has no invented native-complete phase.

There is no current-Router-clock API in645bbd. Before P2/P3 the core therefore
requires the conservative interval from the prior LOCAL prompt-entry time to
current local monotonic time to fit10s/20s. That start precedes native completion.
It rechecks immediately before A's durable prompt POST, after any awaited
timeline/session reads, so a now-known missed interval dispatches no prompt.
Slow turns or delayed evidence can make the interval unprovable: stop without a
new prompt or retry. Awaited work after that check still counts, and the actual
Router monotonic pair is checked after any admitted prompt. A late observed
pair is a charged failure, never a pass or a refund. This conservative policy
does not promise that every otherwise legal120-second turn can qualify C.

P3 also waits at least10s after the prior final settlement was observed and for
that exact retained worker's disappearance. This is conservative about native
idle time; it does not infer the cause of a process exit. Its exact pwd permission
requires producing attempts reaped plus matching observed absence, then a15s
local monotonic hold, then only reject_once within the original30s deadline.
The inherited validator rereads the exact permission and checks accepted
delivery plus a separate failed native-tool outcome. P4 starts only after
effective warm:false and old child absence; no process-retention PASS is inferred
from a successful settings response alone.

## Qualification boundary

The current tests execute actual C row methods and inherited A HTTP command,
permission, native projection and cleanup validation with a primed synthetic
session, synthetic process/settings/fence ports and a deterministic clock. Both
no-read and one-read-per-row flows are covered, along with timing, identity,
uncertainty and rollback failures. They do not qualify the actual named host
entry, published Pi/holder, live Router meter, or real /proc observation.

Required next integration proof: fixed C host entry; actual new Router settings
and fence transaction; published holder/Pi; native fake Claude subprocesses;
real bounded /proc observer; only remote generation/Hub projection seams
synthetic. Preserve real timing bounds and prove extra continuation refusal,
one-read2/2/3/2, no-borrowing/drain, fifteen-second denial, rollback and no calls
after host completion. Only then may a reviewed final release pin enable the
entry. An actual owner-selected account and execution window are still required.
