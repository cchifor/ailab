# Implementation review — relay-operator-session — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- Substantial plan coverage: pinned scope, strict request reconstruction, manifest verification, container isolation, and the principal Relay v0.14.1 response contracts are present. The smoke bundle matches its manifest.
- Blocking concerns remain in Lease fencing and interruption handling: cleanup can revoke a successor's authority or terminate before revocation.
- Proxy concurrency, deadline enforcement, provenance validation, and evidence handling have gaps, several reproduced with in-memory checks.
- Thirteen planner tests passed using a Windows import shim; targeted fault checks reproduced additional failures. Full Linux socket/container tests were not run, and several planned fault scenarios lack coverage.
- The deliberately deferred reaper implementation is excluded from these findings.

## Findings

### Cleanup revokes without confirmed Lease ownership
**Location:** scripts/relay_operator_session.py:414; docs/runbooks/relay.md:902
**Severity:** blocker
<!-- codex: A failed Lease read, an absent Lease, or a different expired holder all permit revocation and account-wide SSO logout, so a late run can destroy a successor's authority; the documented "unknown answer still revokes" policy explicitly weakens the finalized plan. Require positively confirmed ownership with sufficient remaining validity before each destructive stage, use a resourceVersion-conditioned acquisition or extension when necessary, and report unknown ownership or failed release as a Lease failure. -->

### The call path can outlive its Lease and revoke a successor
**Location:** scripts/relay-operator.py:201
**Severity:** blocker
<!-- codex: call acquires a ten-minute Lease but runs Docker without a timeout and unconditionally invokes cleanup afterward, allowing an expired call to revoke a successor's memberships and sessions. Give call a bounded, signal-aware lifecycle with ownership checks before destructive operations, and include credential retrieval inside the Lease-release cleanup scope. -->

### A signal during cleanup can skip revocation entirely
**Location:** scripts/relay_operator_session.py:383
**Severity:** blocker
<!-- codex: The proxy wait and evidence-thread joins execute outside _stage while SIGINT/SIGTERM still raise Interrupted, so an interruption there escapes _cleanup and skips revocation, SSO logout, and Lease release; an in-memory reproduction skipped both revocation and SSO logout. Defer further signals during cleanup and guard every stage, including waits and joins, so an interrupted stage cannot prevent the remaining stages from running. -->

### New subprocess timeouts escape the existing cleanup handler
**Location:** scripts/relay-operator.py:52; scripts/relay-operator.py:160
**Severity:** important
<!-- codex: run now raises subprocess.TimeoutExpired, but cleanup catches only SystemExit, so a timed-out revoke escapes before SSO logout and prevents call from reaching Lease release. Normalize subprocess failures or handle them independently in each cleanup stage, with tests asserting rc 3/5 and continued cleanup after timeouts. -->

### Early returns bypass cleanup exit-code precedence
**Location:** scripts/relay_operator_session.py:315
**Severity:** important
<!-- codex: return self.rc inside the protected try evaluates the return value before finally changes it, so a READY timeout can return 8 even when cleanup discovers a higher-priority logout or revocation failure; the reproduced return was 8 while the final recorded rc was 4. Defer returning until cleanup completes and test combined startup, logout, and revocation failures against the documented precedence. -->

### Real command failures can leave a successful result record
**Location:** scripts/relay_operator_session.py:352
**Severity:** important
<!-- codex: The real Ops wrappers raise SystemExit for unsuccessful commands, which bypasses except Exception, so a failed grant or container setup can leave result.json with rc 0 after successful cleanup even though the process exits unsuccessfully. Catch and classify these operational failures before entering cleanup, and exercise the actual wrapper exception types in the fault tests. -->

### Unknown proxy termination and incomplete log drains can report success
**Location:** scripts/relay_operator_session.py:387
**Severity:** important
<!-- codex: A failed stop followed by proxy.poll() returning None is accepted without rc 4, timed-out log-thread joins are not reported, and there is no inspect/log recovery path before removal; a mocked still-running proxy produced rc 0. Retain containers without --rm until exit status and evidence have been collected, classify unconfirmed logout and incomplete capture as failures, and verify the result of termination and removal operations. -->

### Evidence failures do not stop authorized work
**Location:** scripts/relay_operator_session.py:222; scripts/relay_operator_session.py:306
**Severity:** important
<!-- codex: Proxy and Runner evidence files are first opened after authority is granted, and a stream write failure merely sets rc 6 while abandoning pipe consumption and allowing the run to continue without its audit trail. Open the evidence sinks before granting, propagate streaming failures into bounded run cancellation, and keep draining or explicitly terminate producers so blocked logging cannot stall the proxy or its logout. -->

### Rotation and permission reservations are not atomic
**Location:** scripts/relay-session-proxy.py:322; scripts/relay-session-proxy.py:388
**Severity:** important
<!-- codex: Checking rotated/decided, reserving the operation ID, and adding the binding or permission use separate lock acquisitions, allowing two threads with different operation IDs to pass the same once-only restriction; a barrier-based reproduction accepted two rotation plans for one binding. Validate the complete request first, then atomically check and reserve both the operation ID and the binding or permission, with concurrent regression tests. -->

### Stalled clients can consume the reserved control slot
**Location:** scripts/relay-session-proxy.py:582; scripts/tests/test_relay_session_proxy.py:382
**Severity:** important
<!-- codex: All eight handler slots are allocated before headers and bodies are read, while the general-request semaphore is acquired afterward, so eight incomplete requests can exclude lease renewal; per-read socket timeouts also let a trickling client retain its slot beyond ten seconds. Protect control admission through bounded nonblocking ingress or a separate control channel, enforce an absolute request-read deadline, and replace the current stalled-client test whose immediate tiny responses release their slots before renewal is attempted. -->

### Upstream socket timeouts do not enforce a total deadline
**Location:** scripts/relay-session-proxy.py:543
**Severity:** important
<!-- codex: getresponse() and read(65536) can perform multiple successful socket reads under one unchanged inactivity timeout, allowing a slow stream to exceed the thirty-second call budget or twenty-second logout budget before the outer deadline check runs. Enforce an absolute deadline across connection setup, header parsing, and body reads, and test trickling headers and bodies against both ordinary-call and shared logout budgets. -->

### Dispatch does not recheck cleanup-phase eligibility
**Location:** scripts/relay-session-proxy.py:692
**Severity:** important
<!-- codex: _dispatch rechecks only cleanup_deadline, so a launch, rotation, prompt, or selected permission planned before runner_deadline can still be sent after the cleanup phase begins; a launch was reproduced reaching the fake upstream in that state. Carry cleanup eligibility in the plan and revalidate it immediately before dispatch, with the upstream budget capped to the applicable phase deadline. -->

### Valid Connection: close responses become upstream failures
**Location:** scripts/relay-session-proxy.py:556
**Severity:** important
<!-- codex: http.client transfers ownership of a Connection: close response to HTTPResponse and clears conn.sock during getresponse(), making the subsequent conn.sock.settimeout call raise AttributeError before the body is read; this was reproduced using a valid HTTP response in memory. Apply timeout handling to the response-owned transport and add contract tests for both persistent and closing connections, including logout responses. -->

### Resume learning omits profile, revision, and cwd validation
**Location:** scripts/relay-session-proxy.py:207
**Severity:** important
<!-- codex: The resume branch validates only resume_from and resume_generation, then substitutes the source intent's profile metadata without checking the returned row, so a response with a different profile, revision, and cwd still authorizes its session ID. Require all carried-over provenance fields to match the learned source for both POST responses and inventory reconciliation, and add negative tests for each mismatch. -->

### Refusal logs can contain secrets and request content
**Location:** scripts/relay-session-proxy.py:624
**Severity:** important
<!-- codex: Refusals log the raw target, including query values, and schema errors interpolate arbitrary JSON or query key names, so a rejected request such as /api/auth/me?token=SECRET persists that value in proxy.jsonl. Emit fixed reason codes, matched route templates, and validated identifiers only, with tests proving that sensitive paths, queries, and unknown JSON keys never enter evidence. -->

### Proxy shutdown can abandon logout and mutation accounting
**Location:** scripts/relay-session-proxy.py:781; scripts/relay-session-proxy.py:824
**Severity:** important
<!-- codex: Signal handlers are installed only after login and startup inventory, while normal shutdown closes a server with daemon handlers without waiting for their dispatched requests, so startup termination can skip logout and later termination can discard mutations without an uncertain record. Put the authenticated lifecycle under a bounded shutdown/finally path from session creation onward, track dispatched requests, and settle or explicitly mark outstanding mutations uncertain before exiting. -->

### The prompt ceiling has drifted from the reviewed scope
**Location:** scripts/relay-session-proxy.py:483
**Severity:** nit
<!-- codex: The finalized plan limits prompt text to 8 KiB, but the implementation accepts 16,384 Python characters, which also differs from the server's byte-based ceiling for multibyte text. Enforce the reviewed UTF-8 byte limit or explicitly revise the plan, and test ASCII and multibyte boundary cases. -->

## Diff stat

```text
 docs/runbooks/relay.md                       |  66 +++
 scripts/relay-operator.py                    |  55 +-
 scripts/relay-session-proxy.py               | 835 +++++++++++++++++++++++++++
 scripts/relay-session-smoke/bundle/smoke.py  |  60 ++
 scripts/relay-session-smoke/manifest.json    |  12 +
 scripts/relay_operator_session.py            | 429 ++++++++++++++
 scripts/tests/test_relay_operator_session.py | 313 ++++++++++
 scripts/tests/test_relay_session_proxy.py    | 409 +++++++++++++
 8 files changed, 2177 insertions(+), 2 deletions(-)
```
