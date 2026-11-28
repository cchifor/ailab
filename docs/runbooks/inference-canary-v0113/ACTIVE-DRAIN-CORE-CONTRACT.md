# Fixed Claude active-drain entry

This entry qualifies disabling warm sessions while one native generation is
still producing output. It is separate from the selected four-row C flow.
`QUALIFIED_ROUTER_RELEASE` remains `null`: the checked-in public entry refuses
before service I/O. Component checks, synthetic prerequisite reports and local
Router candidates do not authorize a live window or warm promotion.

The future compiled release object pins current D `source` and
`qualificationSha256` separately from `prerequisiteCSource` and
`prerequisiteCQualificationSha256`, with required timing/fence/clock-witness
capabilities. C's exact report source must match that fixed historical pin;
readiness and current observations must match D's current source. Both sources
and their reviewed qualification join are explicit. Callers cannot supply a
fallback source, relabel C's historical bytes or infer approval from a callback.
Adding the clock-witness implementation does not require repeating C merely to
make its source string equal the new commit.

`createClaudeActiveDrainRunner(env, rawInputs, ports)` exposes the existing
owned Runner lifecycle through `start()`. The private class reuses A's actual
command, permission, prompt, control-lease and Close paths, B's exact credential
validator, and C's process/attempt/settings validators. It neither calls C's
four-row plan nor modifies its source gate. The named host entry owns durable
storage, 20-second lease renewal, callback tracking, abort and bounded cleanup.

## Fixed plan and lifetime

| Row | Immutable admission allowance | Exact prompt                                                                                  |
| --- | ----------------------------: | --------------------------------------------------------------------------------------------- |
| D1  |                             2 | Without using tools or modifying files, count upward from 1, one integer per line, until 500. |
| D2  |                             2 | Reply exactly COLD_ROLLBACK_OK. Do not use tools or modify files.                             |

The total is at most four admitted generations and exactly two prompts. One
scoped Relay read per prompt may use its second admission through the existing
permission path. Raw extra attempts, including denied admissions, remain a
failure. Unused allowance is never transferred or refunded.

The caller supplies the actual `window` and a safe integer
`env.executionDeadline` no later than its expiry. The core further clamps to
the current credential expiry. The host requires an explicit deadline and
retains its existing 45-minute maximum; this does not create or renew a window.
The inherited 120-second prompt bound and the qualified 1024-token route remain
unchanged. There is no retry, warm-up prompt, arbitrary plan or capacity test.

Two fresh phase/fence identities, their fixed plans and original deadline are
persisted before effects. Warm enable preserves the original idle value. Its
actual observed generation receives a two-admission fence. D1 runs concurrently
with the process observer. Once one specific fetch is active, the core persists
one full-values `warmSessions: false` CAS. The original fence remains active
through that CAS, native completion and client settlement. Closing it earlier
would cancel its issued tickets and cannot prove successful drain.

After successful D1 overlap, native/client settlement, exact terminal counters
and observed child disappearance, the warm fence closes. The old observer is
joined and archived. A new observer uses the actual cold generation; a linked
two-admission fence carries the exact closed predecessor ID/revision/attempts
and unknown-token counter. D2 must return `COLD_ROLLBACK_OK`, settle and leave no
retained child. Both fences close and the owned session/lease closes/releases.
No third settings write occurs when cold and original idle already match.

If a permission arrives after D1's disable CAS begins, the run stops without
approving it or moving its continuation to another generation. Cleanup can
cancel that permission. This boundary can leave a legitimate run unqualified;
it does not justify changing the fence's generation or increasing its cap.

## Inputs and predecessor evidence

Only these RAW keys are accepted:

- `version: 1`, distinct UUID `runId`/`phaseId`, `account`, `model`.
- `window: {startsAt, expiresAt}`, currently open.
- Independent `phaseAReportSha256`, `phaseBReportSha256`,
  `phaseCReportSha256`, `coldReportSha256`, `pairedReportSha256`,
  `exclusiveWindowSha256`.
- `phaseCReportRaw`: exact bounded UTF-8 JSON, at most 1 MiB, matching the
  separately supplied C hash. The CLI reads it from its separate private file.

`inputs(raw, now)` validates the report and returns normalized immutable inputs
without raw C bytes, adding `phaseCRouterSource`. The factory still receives
RAW inputs. The host must retain the original private bytes and supplied hash;
a collector cannot create a trusted pin from its own response.

C must be a completed `claude-phase-c` report with the exact Hub/source/profile,
account/model and A/B/cold/paired links, four passed rows, two closed fences,
zero open/stale/unattributed requests, no live processes, joined final observer,
normal launch/native-session and matching Close evidence, no control lease or
unresolved cleanup. D preparation requires the exact current Claude credential
tuple to equal C's `claudeCredential`, including key, generation, revision and
expiry. It also requires C's original idle value. A new provider registration
generation does not permit credential rotation or reinterpretation of C bytes.

C's prior run and warm phase must be distinct UUIDs. Its final scope joins that
same run and the cold fence's phase; the warm fence joins the original input
phase. Both fences join the prior run. The cold scope phase deliberately differs
from the warm input phase, and neither may be silently retagged as D's identity.

## Fixed ports and clock evidence

Adapters supply exactly the existing four groups:

| Group       | Methods                              |
| ----------- | ------------------------------------ |
| `readiness` | `read`                               |
| `process`   | `start`, `next`, `stop`              |
| `settings`  | `read`, `replace`                    |
| `fence`     | `arm`, `inspect`, `advance`, `close` |

The host supplies `signal`, `cleanupSignal`, and its own `monotonicNs` to the
core. Adapters must not add a top-level clock property to the host's four-group
object. Each callback receives `{scope, signal, ...}`. All returned promises
must encompass their complete work and obey the supplied signal. `advance` is
part of the fixed shared contract but this two-segment plan never calls it.

Readiness adds `phaseCReportSha256` to the exact C source/key/deployment/holder/
journal/profile/route qualification. It requires the selected alias, SDK-resume,
CLI 2.1.283, one Router attempt, no fallback and initially cold settings. Actual
source approval and independently pinned prerequisite reports remain required.

Settings use the full plugin values/revision CAS. Their normalized
`providerRuntime` adds required decimal-string `observedMonotonicNs` to C's
`runtimeInstanceId`, `providerGeneration`, and epoch-ms `observedAt`. The
extended validator checks this shape first, removes only the clock field from
a cloned copy, and delegates to unchanged C validation. `null`, absent,
noncanonical or regressing clock values refuse; wall time is never substituted.

The clock must come from the same trusted Router engine clock and recorder
domain as `provider.process`. The reviewed admin provider projection supplies
this field, with `null` when unavailable. It is not a provider probe or a
reservation of future generation identity. Settings reads bracket actual
full-values CAS/readback under one runtime and confirmed old/new generations.
Historical warm observations remain bound to their original generation.

D1 accepts overlap only when:

```text
fetchNs <= beforeCASWitnessNs <= afterCASWitnessNs < nativeCompleteNs
```

It also rechecks the selected attempt immediately before settings dispatch.
Completion observed during the pre-CAS read prevents dispatch. A late post-CAS
witness leaves the row settled but not passed, even if native generation may
have overlapped the actual commit. Missing evidence is never a timing pass.

Process callbacks retain C's strict scope, runtime, generation, original
request/attempt identities, sequence, PID/start-ticks, cumulative history and
sample freshness. Absence must be observed for the actual producing child;
terminal events alone do not establish disappearance. The core joins three
epochs: initial cold, active warm, and final cold. No historical event is
retagged into a later epoch.

Fence callbacks retain the existing normalized signed-receipt contract and
compare-and-swap counters. Each mutation has a saved operation ID and exact
intent; lost replies are not resent. Unknown token usage remains charged and
distinct from open/stale/unattributed attempts. A failed close or unresolved arm
cannot create another allowance. Failure cleanup may close the current fence
first, then cancel/Close, restore owned settings and join observers; that path
does not qualify active drain. Foreign settings changes remain unresolved.

## Report and finalizer join

Success has `kind: "claude-active-drain"`, `status: "active-drain-passed"`,
`activeDrainCompleted: true`, `activeDrain: "observed"`, `completed: false`
and `capacity: "unexercised"`. It includes:

- Normalized inputs with exact independent A/B/C raw hashes; `relay`, `router`,
  `routerRelease` with both fixed source/qualification pins, `scope`,
  `claudeCredential` and the single `runs` entry.
- Inherited actual `launch`, delivery/Open and `closed` evidence. The
  `warm-launch-baselines` evidence carries `pre` and `post` markers for
  `inputs.runId`; this is a distinct consumer for finalizer inventory.
- Two passed rows D1/D2 with cap/generation count, exact terminal evidence and
  D1's `overlap`. `activeAttempt` and `activeSettings` preserve the selected
  request and settings readback witnesses.
- Immutable `subplans`, two closed `fences`, `drainBaseline`, `drainUsage`, and
  complete settings/fence operation histories.
- `originalSettings` and `finalSettings` readbacks proving cold/original idle;
  three archived `processEpochs`, ending `final-drained-and-joined`, with no
  live process or control lease.

Grant revocation remains the separate finalizer's job. Its original A/B/C-only
inventory must not silently accept this additional session. Supporting D
requires an explicit independently pinned D report and exact consumer/marker
join, while preserving historical C's active-drain-unexercised report.

## Qualification scope

The dedicated tests exercise actual fixed row methods, inherited HTTP command
and permission paths, saved intent ordering, two exact generations/plans and
cleanup. The component fixture primes an admitted session and uses synthetic
services, process samples and a deterministic clock; it does not prove the
public positive entry, published holder/Pi, actual Router dispatch, live process
census, credential authority, capacity, or an operator window.

Before live use, separately qualify the named host and actual published
holder/Pi plus Router signed meter/settings and pinned CLI with a held synthetic
upstream. Require the old request not to be aborted by the settings CAS,
successful native/client drain, real child disappearance, exact linked cold
fence/history and the same-domain witness inequality. Preserve all failures and
cleanup evidence. A source pin substitution in an isolated qualification copy
must record both hashes; it must not remove the checked-in NULL gate.
