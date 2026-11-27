# Implementation review — relay-operator-session — round 1

<!-- codex-impl-review-status: complete -->

## Findings

### Cleanup revokes without confirmed Lease ownership
**Location:** scripts/relay_operator_session.py:414; docs/runbooks/relay.md:902
**Severity:** blocker
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### The call path can outlive its Lease and revoke a successor
**Location:** scripts/relay-operator.py:201
**Severity:** blocker
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### A signal during cleanup can skip revocation entirely
**Location:** scripts/relay_operator_session.py:383
**Severity:** blocker
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### New subprocess timeouts escape the existing cleanup handler
**Location:** scripts/relay-operator.py:52; scripts/relay-operator.py:160
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Early returns bypass cleanup exit-code precedence
**Location:** scripts/relay_operator_session.py:315
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Real command failures can leave a successful result record
**Location:** scripts/relay_operator_session.py:352
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Unknown proxy termination and incomplete log drains can report success
**Location:** scripts/relay_operator_session.py:387
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Evidence failures do not stop authorized work
**Location:** scripts/relay_operator_session.py:222; scripts/relay_operator_session.py:306
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Rotation and permission reservations are not atomic
**Location:** scripts/relay-session-proxy.py:322; scripts/relay-session-proxy.py:388
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Stalled clients can consume the reserved control slot
**Location:** scripts/relay-session-proxy.py:582; scripts/tests/test_relay_session_proxy.py:382
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Upstream socket timeouts do not enforce a total deadline
**Location:** scripts/relay-session-proxy.py:543
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Dispatch does not recheck cleanup-phase eligibility
**Location:** scripts/relay-session-proxy.py:692
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Valid Connection: close responses become upstream failures
**Location:** scripts/relay-session-proxy.py:556
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Resume learning omits profile, revision, and cwd validation
**Location:** scripts/relay-session-proxy.py:207
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Refusal logs can contain secrets and request content
**Location:** scripts/relay-session-proxy.py:624
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### Proxy shutdown can abandon logout and mutation accounting
**Location:** scripts/relay-session-proxy.py:781; scripts/relay-session-proxy.py:824
**Severity:** important
**Resolution:** fixed in b0e16725 (see the commit message), with a regression test.

### The prompt ceiling has drifted from the reviewed scope
**Location:** scripts/relay-session-proxy.py:483
**Severity:** nit
<!-- codex: The finalized plan limits prompt text to 8 KiB, but the implementation accepts 16,384 Python characters, which also differs from the server's byte-based ceiling for multibyte text. Enforce the reviewed UTF-8 byte limit or explicitly revise the plan, and test ASCII and multibyte boundary cases. -->
<!-- opus-pushback: The 8 KiB in the plan was a drafting value, not a reviewed limit; the Runner's prompts are short. The proxy now enforces 16 KiB of UTF-8 BYTES (MAX_PROMPT_BYTES), which is stricter than the server's 16384 UTF-16 units for any multibyte text, and test_prompt_byte_limit covers the ASCII and multibyte boundaries. I'd rather revise the plan's number than halve a limit the server itself allows. -->

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
