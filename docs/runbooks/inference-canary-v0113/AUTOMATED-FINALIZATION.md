# Fixed canary finalization

This entry is held: `QUALIFIED_FINALIZATION = null`. It refuses before API calls
or creating evidence storage until the exact source/observer bundle is qualified.
It is separate from A/B/C and performs no inference, launch, renewal, rotation,
permission decision, settings change, profile revocation or history deletion.
The existing operator broker excludes REVOKE; its separately reviewed integration
must permit only the exact normal requests below. This code does not widen it.

`canary-finalization-passed` means the scoped grants and renderer cleanup passed.
It does not complete the entire plan or promote warm retention. Capacity and
active drain keep their original C dispositions. Finalization may start only
after all needed work on these bindings has ended; preserving a grant for a
later selected check is incompatible with the required no-further-use decision.

## Scope and time

The targets are fixed in source: Codex `a0f57684-747f-4c3e-8d90-e684b7262590`,
Qwen `d2871eec-8f5a-4afe-a917-014f0ae8e14d`, then Claude
`a8f6211c-d7e2-4ffd-824d-b158e349a8d4`. Claude is an explicit extension of the
normal managed-binding revoke workflow; the original A final action covers only
the first two. No arbitrary target or generic mutation API is exposed.

A's existing `Runner.revokeFinal()` enforces its inference window in `check()`,
`ask()` and polling; its ready/current-credential inventory is also unsuitable
after retirement. This entry leaves those guards unchanged. It preserves all
historical inference windows and carries no inference authority. Current admin
authority comes from the same authenticated session broker, independently of
credential expiration. A supplied cleanup-only deadline is required and cannot
exceed 15 minutes from entry preparation. No execution window is assigned by
this limit. Each binding readback and observation is bounded by 120 seconds,
polls every 2 seconds, and is shortened by the cleanup deadline; HTTP requests
are bounded by 15 seconds. There is no heartbeat or lease acquisition.

The source contract is Relay5f90 `server/router-credentials/service.ts` revoke():
current admin role, binding revision, immutable operation ID/digest, all
generations; no key-expiry precondition. Revision increases only when the binding
was enabled. Migration008 has no state named `expired`. A recognized retired
binding is enabled/`failed`, null current/pending pointers, and all recorded
credentials revoked with cleanup complete. Ready bindings retain the exact last
credential; disabled/revoked bindings are verified without another POST. Unknown,
in-flight or partial revocation, pending issuance/rotation, extra generations,
changed grants and incomplete inventories refuse. The original operation must be
resolved separately; a new run cannot manufacture a replacement operation ID.

The inspected Hub5f90 has a blocking retired-binding defect: revoking an enabled
`failed` binding whose credentials are already revoked leaves the binding and
its new operation pending. The already-revoked reconciliation path returns
without completing the binding. This path requires a reviewed Hub fix, a real
PostgreSQL regression, and qualified deployment before finalization can run.
The 6.075-second Linux Python qualification passed with synthetic Hub responses;
its fixture directly supplies terminal states and does not prove compatibility
with this real transition. `QUALIFIED_FINALIZATION` remains `null`; the actual
source and observer pins are unassigned. No retry or state repair substitutes
for the missing Hub transition.

## Inputs and linkage

The library captures `createFinalization({reports, manifest, authenticatedFetch,
observer, save})`. `observer` must provide the compiled qualified digest and its
single `observe(context)` callback. It is an operator trust input, not a digest
provided by an untrusted observation. Private raw artifacts are retained byte
for byte:

- `a`: completed A report, including the original stopped report and carried
  one-prompt/two-attempt accounting.
- `b`: completed fenced Claude lifecycle/effort report.
- `bInputs`: the original host's `phase-b-inputs.json`. B's report alone does not
  contain the A hash; this artifact supplies the actual retained linkage.
- `c`: completed selected four-prompt C report with its A/B hashes.

The external manifest is `{version:1, pins:{a,b,bInputs,c}, cleanupDeadline,
decisionSha256}`. Each pin hashes exact UTF-8 bytes, not parsed/canonical JSON.
The decision hash identifies the existing operator's exact no-further-use
decision. B-input fields must match B's report and pinned A; C must reference
both pinned reports and the same account/model. All run/status/source checks
remain strict; an arbitrary success flag is insufficient. Exact final compiled
source pins are still unassigned. Synthetic test substitutions are qualification
fixtures, not executable live inputs.

The qualified bundle separates immutable report provenance from the cleanup
deployment. `reportHubSources:{a,b,c}` and `reportRouterSources:{a,b,c}` each pin
the exact source for every historical report. `hubSource` and `routerSource`
instead pin the current deployment required in both observer stages. Every
source is a full commit SHA; missing or additional artifact entries refuse.
There is no source allowlist fallback. Deploying the required Hub fix never
rewrites report bytes, changes their retained hashes or relabels their sources.
The bundle and observer still require qualification against the actual cleanup
deployment before the public NULL gate can be replaced.

The CLI uses six fixed path pairs:

```text
--manifest ABS --phase-a ABS --phase-b ABS --phase-b-inputs ABS --phase-c ABS --output NEW_ABS_DIRECTORY
```

`RUNNER_DEADLINE` is normalized by the existing epoch-seconds/milliseconds parser
and must equal the manifest cleanup deadline. `RELAY_SESSION_SOCKET` supplies the
existing authenticated transport; the optional separate control socket retains
its existing routing. Input reads reuse the private, single-link, non-symlink,
same-UID UTF-8 reader (1 MiB per CLI artifact). The library bounds each raw
artifact to 8 MiB. No cookies, key material or environment contents are exported.
The executable requires the reviewed observer to be linked explicitly; there is
no arbitrary module path or script evaluator option.

## Required observations

The callback receives a fixed context with `stage` (`preflight` or `renderer`),
report pins, decision hash, sources, scope, every expected launch/marker,
original idle setting, `notBefore`, deadline and AbortSignal. Scope fixes tenant
`00000000-0000-4000-8000-000000000001`, host, connection, c4 identity, three
profiles and three bindings. The response must join those exact values:

| Field                                                          | Required evidence                                                                                                                                                          |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `observedUtc`, sources, report pins, decision                  | Fresh within 120 s, after the supplied boundary, at most 5 s future skew; exact deployed Hub/Router, pinned reports and decision; `noFurtherUse:true`.                     |
| `unlistedLaunches`, `unlistedSessions`, `unresolvedOperations` | All zero from the complete fixed tenant/profile/binding scope, including current work beyond the supplied reports; never inferred from a truncated list.                   |
| `sessions`                                                     | Exact recorded session set, each closed with zero pending commands/permissions. Normal session/control GETs also require closed state and lease:null.                      |
| `bindings`                                                     | Exact three bindings, zero active consumers/open requests/stale requests/unattributed attempts. Claude fence closed; A bindings closed or explicitly not applicable.       |
| `router`                                                       | warm:false, original idle value, current settings revision and empty scoped process census.                                                                                |
| `provenance`                                                   | Query SHA256, projection SHA256 and bounded database identity. These attribute the operator observation; the finalizer does not independently query private DB/host state. |

Normal credential GETs independently require complete inventories (<500
bindings, <1000 credentials/consumers; detail <32 operations/<128 consumers),
every known generation, current revision, exact host/connection/identity/grants,
no active consumers or unknown operations. Expired inference timestamps are not
modified. Extra generations or changed credentials refuse.

After all grants are confirmed, `renderer` additionally requires every recorded
launch, including the original stopped A generation1: exact session/binding/
credential/generation, cleanup command UUID/time, status `absent`, and fresh
managed-file absence. `not-current` is insufficient. Existing launch cleanup
receipts may predate revocation but cannot predate launch beyond clock skew.
File observations and the four marker reports must follow the final collection
boundary. Codex/Qwen/Claude recovered effect hashes and byte counts must match
their historical qualified observations; C's no-effect baseline remains absent.
Escrow `cleanupPending:false` alone never proves renderer removal or halted work.

## Durability, stop and qualification

An exclusive private directory retains the manifest, raw artifacts/hashes, lock,
and numbered fsynced snapshots; its parent directory is fsynced before dispatch.
An existing/partial directory is never cleared or resumed automatically. Each
revoke body, SHA256, operation ID and revision is saved before the one POST.
Lost responses are inspected through the original oldest-generation operation;
there is no resend. Confirmed readback retains sanitized per-generation terminal
state. Already revoked bindings get read-only verification.

Failure snapshots retain a fixed internal code/reason, stage, intent index and
binding identity. Dispatch evidence records a received HTTP status and distinguishes
a typed local refusal from an uncertain transport outcome. A server refusal is
not proof that no work committed: one available original-operation readback is
collected, then an unconfirmed operation stops without polling or resending.
Bounded last-readback projections are saved before validation can refuse and on
settlement timeout. They contain only identifiers, enumerated states, revisions,
cleanup flags and failure presence; raw response bodies and external exception
messages are never retained. A second evidence-write failure does not replace
the original safe reason, and the reported diagnostic marks that failure.

The CLI distinguishes preparation refusal from an unresolved execution using
the last durable snapshot status, stage and intent counts. It emits only safe
diagnostics to stderr. These fields do not grant permission to resend or reuse
an existing evidence directory. Observer context and returned observations are
copied, so callbacks cannot change the retained report pins, launch expectations
or marker baseline used by later checks.

Stop forbids another mutation and allows only bounded settlement of an already
dispatched operation. Fetch bodies, observers and the whole action are tracked.
An uncooperative callback remains unresolved; no late response can turn that
into success. Failures retain partial revocation and original IDs. This finalizer
does not try to recover unresolved runtime work or acquire someone else's lease.

The tests use actual finalizer/host/CLI modules, a real local Unix HTTP socket and
durable files, but synthetic Hub metadata, reports and operator observations.
Only the exact NULL qualification literal is replaced in isolated test bytes,
with before/after hashes; no Runner/prototype override. Existing published
runtime and live acceptance proofs remain separate. No tests or live finalization
are claimed solely by the presence of this source.
