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
```

These checks use synthetic services and reports. They do not prove operator
authentication, original browser-state recovery, a live provider outcome or a
completed acceptance window.
