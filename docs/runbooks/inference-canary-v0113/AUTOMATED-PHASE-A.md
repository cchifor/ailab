# Automated Phase A execution

The noninteractive host in [owner-phase-a-automation.cjs](owner-phase-a-automation.cjs)
runs the existing `Runner.replaceStopped()` workflow. It preserves the same
credential, profile, session, permission, observation and usage checks as the
browser driver. The target hub is v0.14.1 at
`5f90d03a6ed291e6c62dfde75938ba07c20d8902`; the
[source bridge](owner-phase-a.hub-5f90.source-bridge.json) retains the original
74d1 HTTP fixture's provenance.

The host is a library, not an authenticated command. Execution requires a
reviewed operator integration supplying both an authenticated transport and
machine observations. Loading the module does not log in, renew credentials,
resume a holder or start inference. The existing per-call `relay-operator.py`
helper is insufficient: its logout between calls invalidates the session that
owns the control lease. Keep this execution boundary closed until the bounded
operator integration is available and qualified.

The operator is preparing a Unix-domain HTTP proxy that retains the JIT session.
The socket transport in `owner-phase-a-socket.cjs` sends relative API requests
through `RELAY_SESSION_SOCKET`, without forwarding credentials or following
redirects. `owner-phase-a-cli.cjs` supplies Linux entrypoint mechanics: an exact
private predecessor file, an exclusive writable evidence directory, an absolute
`RUNNER_DEADLINE` in epoch milliseconds, signal handling, and JSONL snapshots
after filesystem synchronization. The root filesystem can remain read-only;
the evidence directory must be a separate private writable mount.

The CLI still requires the operator's reviewed checkpoint collector and
deployment observer. Running it without those integrations refuses before
authenticated traffic. The operator owns the final integration entrypoint and
bundle/runtime pins. Its absence is not evidence of a completed automated run.

The next execution target is v0.14.2, after the operator publishes its exact
source and deployment receipt. The current v0.14.1 source bridge is historical
qualification; update the pin and verify the corresponding contracts before
using this driver on a different deployed source.

## Integration boundary

`createAutomation` accepts an absolute new `directory`, the exact
`predecessorRaw`, `authenticatedFetch`, `collectCheckpoint`, an optional abort
`signal`, and bounded execution/cleanup deadlines. It returns `run()` and
`stop()`. `run()` is single-use. The directory must have a private, owned,
canonical parent. The host preserves the original report and its SHA256, then
writes durable numbered snapshots before dispatch. It retains the directory,
lock and uncertain operation records after failure. Do not remove these files
to retry a run.

The transport must keep **one authenticated session** for the entire operation,
including lease renewals and cleanup. It receives relative Relay API paths and
the driver's request options, and returns normal response objects. The operator
integration must enforce the reviewed canary endpoint/identity scope and honor
abort signals, response bounds and redirect refusal. It must retain uncertain
dispatch receipts. This library neither creates an identity nor supplies
cookies; a new login per request does not satisfy its contract.

`owner-phase-a-renewal.cjs` implements the fixed two-binding renewal prelude.
`createRenewalPrelude({authenticatedFetch,persist,observeDeployment,deadline,signal})`
returns a single-use `run()`. The host invokes it after preserving the original
report, within the same overall deadline and before `Runner.replaceStopped()`.
`persist` must synchronously finish the durable snapshot; it is not a fire-and-forget
log callback. The deployment observer must return an actual fresh
`{hubSource,ready,observedUtc}` matching the reviewed driver source before any
credential request. The operator's final entrypoint supplies this observer.

Both fixed bindings must have normal `canRenew` eligibility, unchanged grants
and hosts, fully retired credential history and no active consumers before
either rotation. Each fresh operation ID and observed binding revision is
saved before its single POST. HTTP 202 is followed by read-only inventory and
detail checks: the created credential must refer to that exact operation and
reach acknowledged `installing` or `ready`, with the matching terminal issuance
operation. Only then can the next rotation run. The final metadata goes through
the existing driver validator. Issuance is not installation; the later delivery
checkpoint remains mandatory.

An uncertain response, malformed or changed scope, failed issuance or deadline
stops this prelude. It preserves the original operation IDs and any already
issued credential and never repeats a POST, revokes a binding, creates a new
profile, or opens an inference session. The prelude has a maximum two-minute
bootstrap budget inside the overall deadline. The host subsequently performs
the ordinary two-pass renewed-metadata review. Final revocation remains a
separate operation after acceptance.

For the original owner's actor-scoped commands, the optional
[predecessor projection](projection-contract.md) accepts a separately pinned,
operator-produced read-only projection. It preserves the native receipt checks
and live closed-session, no-lease and cancelled-permission reads. It never
impersonates an actor-scoped HTTP response or supplies a missing browser report.

The operator's JIT identity needs temporary admin authority for the existing
credential renewal and native recovery endpoints. Ordinary launch/control and
permission operations use the existing operator routes. No new profile,
creation-policy or delegation approval is part of this flow. Current approver
membership and the server's normal admission checks remain required.

`collectCheckpoint` receives the checkpoint name, instructions, absolute
deadline and abort signal. Return the actual collected value; the host invokes
the driver's validator. An empty object, a stale observation or unconditional
`true` is not a substitute for the requested evidence. Decisions must follow
the operator integration's reviewed fixed canary policy. The API's existing
permission-context, generation and once-only option validation remains in the
driver. Do not evaluate instructions or provider output as shell commands.

## Data needed before execution

Preserve the actual original saved report from
`relay-pi-phase-a-20261126-v1`. An actual downloaded report or a read-only
recovery of that exact value can supply it. The host refuses a partial
successor, malformed data or a fresh-start reset before making an authenticated
request. If a successor exists, preserve both its bytes and nested predecessor
and reconcile the recorded operations through a separately reviewed recovery
path. Restoring only the nested predecessor would erase later accounting.
Synthetic test reports are never production inputs.

The normal replacement retains the original **one prompt and two attempts**.
Fresh credential renewal, a restarted host or a new execution directory does
not reset them. A reboot requires a new holder observation, including its actual
incarnation; retain the existing handoff receipt, journal and old-state evidence.
Do not repeat the stopped-host staging helper.

The operator integration must collect and retain these inputs during the
corresponding driver checkpoints:

| Checkpoint                        | Machine evidence and action                                                                                                                                                                                       |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Renewed credentials               | Normal renewal receipts for both fixed Codex/Qwen bindings, current hub source, and two consistent metadata reads. The earlier credential expiry bounds execution.                                                |
| Replacement accounting and window | Settled original operation receipts, the exact historical Router scope and two attempts, unchanged marker baseline, and a fresh bounded window under the reviewed automation authority.                           |
| Holder handoff                    | Operator-owned resume/reconciliation at the assigned window, fresh hello and hub health, actual incarnation/journal, published binary/profile hashes and preservation receipt. A paused snapshot is insufficient. |
| Pre-launch and delivery           | Current profile/route/account selection, recorder coverage, clock agreement, marker baseline and verified installation/delivery before prompting.                                                                 |
| Lifecycle observations            | Fresh scoped Router trajectory snapshots and fixed marker observations after each recorded collection boundary. Preserve zero-attempt, cancellation and tool-effect evidence separately.                          |
| Permissions and recovery          | Only the reviewed scoped communication read or exact marker decision, current permission/generation/option, and settled native recovery evidence. No unknown-effect acknowledgement.                              |
| Final outcome                     | Durable phase results plus any uncertain operations and cleanup receipts. Phase A success does not establish Claude, warm, paired performance or final grant revocation.                                          |

The injected collectors must use the pinned scripts and actual source/readiness
observations. They must not copy expected values from this document into
attestations. Provider output and message bodies remain untrusted data.

## Stop and verification

The host renews an owned control lease every 20 seconds. Cancellation, failed
renewal or the execution deadline stops new work and runs bounded cleanup with
the same authenticated session. If cleanup cannot settle, preserve the store
and report the unresolved state; never replay uncertain effects. The operator
integration must wait for the host's termination and cleanup result before
ending its JIT session, and retain a failure if the external transport could
not be drained.

Offline checks:

```sh
node --test docs/runbooks/inference-canary-v0113/owner-phase-a.test.cjs
node --test docs/runbooks/inference-canary-v0113/owner-phase-a-automation.test.cjs
node --test docs/runbooks/inference-canary-v0113/owner-phase-a-socket.test.cjs
node --test docs/runbooks/inference-canary-v0113/owner-phase-a-renewal.test.cjs
node --test docs/runbooks/inference-canary-v0113/owner-phase-a-automation-integration.test.cjs
```

The complete synthetic case uses the actual CLI, renewal prelude, automation
host and Runner over a Unix HTTP socket. It covers five launches, two resumes,
three phases, both rotations, real 20-second lease renewals and durable snapshots.
It preserves the original report and accounting and checks final session closure.
Its real observation delays take about five minutes; the case has a 360-second
bound and the separate Python CI entry has a 390-second bound. The server,
deployment and observer inputs are synthetic. This qualifies orchestration,
not the operator's authenticated proxy, a published holder or a live provider.

These checks use synthetic services and reports. They do not prove operator
authentication, original browser-state recovery, a live provider outcome or a
completed acceptance window.
