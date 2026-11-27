# Implementation review — relay-operator-session — round 2

<!-- codex-impl-review-status: finalized -->

## Findings

### The call cleanup Lease can expire during destructive cleanup

**Location:** scripts/relay-operator.py:246; scripts/relay_operator_session.py:202  
**Severity:** blocker  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### The call lifecycle still lacks signal-safe cleanup

**Location:** scripts/relay-operator.py:209; scripts/relay-operator.py:244; scripts/relay-operator.py:164  
**Severity:** blocker  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### Timing out the Docker client does not terminate the call container

**Location:** scripts/relay-operator.py:230  
**Severity:** important  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### The shared reader pool can still exclude lease renewal

**Location:** scripts/relay-session-proxy.py:639; scripts/relay-session-proxy.py:763  
**Severity:** important  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### Reader timers exceed the container thread limit and can leak admission capacity

**Location:** scripts/relay-session-proxy.py:653; scripts/relay_operator_session.py:362  
**Severity:** important  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### The upstream timer cannot interrupt connection setup

**Location:** scripts/relay-session-proxy.py:580; scripts/relay-session-proxy.py:593  
**Severity:** important  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### Evidence failures during draining can still report success

**Location:** scripts/relay_operator_session.py:252; scripts/relay_operator_session.py:376; scripts/relay_operator_session.py:455  
**Severity:** important  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### Refusal logs still preserve arbitrary lowercase path content

**Location:** scripts/relay-session-proxy.py:100; scripts/relay-session-proxy.py:729  
**Severity:** important  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### Startup signals do not interrupt authenticated network work

**Location:** scripts/relay-session-proxy.py:890; scripts/relay-session-proxy.py:910; scripts/relay-session-proxy.py:927  
**Severity:** important  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### The new drain consumes the Docker logout grace

**Location:** scripts/relay-session-proxy.py:69; scripts/relay-session-proxy.py:951; scripts/relay_operator_session.py:447  
**Severity:** important  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

### The finalized prompt ceiling remains inconsistent

**Location:** plans/2026-11-27-relay-operator-session-plan.md:77; scripts/relay-session-proxy.py:64  
**Severity:** nit  
**Resolution:** fixed in the round-2 fix commit (see its message), with regression tests.

## Round-1 verification

- **Cleanup revokes without confirmed Lease ownership — fixed:** `scripts/relay_operator_session.py:477` calls `lease_secure(ops, self.run_id, CLEANUP_LEASE_S)` and line 478 gates destructive cleanup with `if owned is True:`; line 488 reports unconfirmed ownership instead of revoking, and lines 206–207 condition replacement on `resourceVersion`.
- **The call path can outlive its Lease and revoke a successor — not fixed:** `scripts/relay-operator.py:233` now supplies `timeout=CALL_DOCKER_TIMEOUT_S`, but line 246 uses `lease_secure(o, holder, 300)` and `scripts/relay_operator_session.py:202` sets `want = now + extend_s`; the resulting shorter Lease can expire during destructive cleanup, as reproduced above.
- **A signal during cleanup can skip revocation entirely — fixed for the session path:** `scripts/relay_operator_session.py:304` checks `if self.in_cleanup:`, records the signal and returns; line 324 sets `self.in_cleanup = True` before cleanup, and line 456 guards the evidence join through `_stage`.
- **New subprocess timeouts escape the existing cleanup handler — fixed:** `scripts/relay-operator.py:56` catches `subprocess.TimeoutExpired`, line 57 converts it to `SystemExit`, and cleanup catches `SystemExit` separately at lines 169 and 178.
- **Early returns bypass cleanup exit-code precedence — fixed:** `scripts/relay_operator_session.py:316` delegates work to `self._run(...)`, line 325 executes cleanup, and line 336 performs `return self.rc` afterward.
- **Real command failures can leave a successful result record — fixed:** `scripts/relay_operator_session.py:321` catches `BaseException`, including wrapper `SystemExit`, and line 322 records `self.fail(8, stage="error", ...)` before cleanup and result serialization.
- **Unknown proxy termination and incomplete log drains can report success — not fixed completely:** `scripts/relay_operator_session.py:453` correctly uses `if prc != 0:` and line 457 checks `if t.is_alive():`, but completed threads with failed reads/writes can still pass cleanup without rc 6, and lines 461–464 remove containers without recovering missing Docker logs.
- **Evidence failures do not stop authorized work — not fixed completely:** `scripts/relay_operator_session.py:287` opens evidence sinks before authority and line 263 calls `self.evidence_failed.set()`, but line 381 can still start the Runner before checking that flag, and the check at line 409 does not cover subsequent cleanup/drain failures.
- **Rotation and permission reservations are not atomic — fixed:** `scripts/relay-session-proxy.py:185` encloses both once-only checks and `STATE.ops[op] = record` in one lock; lines 355 and 417 call `reserve(..., once=("rotated", binding))` and `reserve(..., once=("decided", pid))`.
- **Stalled clients can consume the reserved control slot — not fixed completely:** `scripts/relay-session-proxy.py:640` still gates every connection with `self.readers.acquire(blocking=False)` before line 763 selects `self.server.control`; full reader saturation refuses renewal despite unused control capacity.
- **Upstream socket timeouts do not enforce a total deadline — not fixed completely:** `scripts/relay-session-proxy.py:589` adds `threading.Timer(budget, cut)`, but line 594 assigns `holder["sock"] = conn.sock` only after line 593's `conn.connect()`, leaving setup outside the effective cancellation mechanism.
- **Dispatch does not recheck cleanup-phase eligibility — fixed:** `scripts/relay-session-proxy.py:777` re-evaluates `ph = phase()`, line 778 rejects cleanup-ineligible plans, and line 780 selects the applicable runner or cleanup deadline.
- **Valid Connection: close responses become upstream failures — fixed:** `scripts/relay-session-proxy.py:594` retains `holder["sock"] = conn.sock` before `getresponse()`, and line 602 reads `resp.read(65536)` without dereferencing the subsequently cleared `conn.sock`; the closing-response HTTP test passed.
- **Resume learning omits profile, revision, and cwd validation — fixed:** `scripts/relay-session-proxy.py:231` computes `same_profile` from all three returned fields, and line 236 requires `ok = same_profile and ...` alongside source session/generation matching.
- **Refusal logs can contain secrets and request content — not fixed completely:** `scripts/relay-session-proxy.py:706` now logs `reason=r.code`, but line 104 preserves arbitrary segments through `s if re.fullmatch(r"[a-z][a-z-]{0,30}", s) else "*"`, reproducing lowercase secret leakage.
- **Proxy shutdown can abandon logout and mutation accounting — not fixed completely:** `scripts/relay-session-proxy.py:898` installs the signal handler before login and line 951 adds `drain_inflight(DRAIN_ON_SHUTDOWN)`, but startup work remains uncancelled and the additional drain can exhaust Docker's grace before line 956's `closed = logout()` finishes.
- **The prompt ceiling has drifted from the reviewed scope — not fixed as a scope/documentation issue:** `scripts/relay-session-proxy.py:510` now measures `len(body["text"].encode("utf-8"))`, but line 64 defines `MAX_PROMPT_BYTES = 16 * 1024` while the finalized plan's line 77 still says `text (≤ 8 KiB)`; the round-2 comment above responds to the pushback.

## Diff stat

```text
 docs/runbooks/relay.md                             |  79 ++
 ...026-11-27-relay-operator-session-impl-review.md | 105 +++
 scripts/relay-operator.py                          |  99 ++-
 scripts/relay-session-proxy.py                     | 965 +++++++++++++++++++++
 scripts/relay-session-smoke/bundle/smoke.py        |  60 ++
 scripts/relay-session-smoke/manifest.json          |  12 +
 scripts/relay_operator_session.py                  | 497 +++++++++++
 scripts/tests/test_relay_operator_session.py       | 379 ++++++++
 scripts/tests/test_relay_session_proxy.py          | 566 ++++++++++++
 9 files changed, 2749 insertions(+), 13 deletions(-)
```

## Resolution (Opus, after round 2)

Every round-2 finding was accepted and fixed; the prompt-limit nit by revising the plan to the server's own 16 KiB UTF-8 byte limit (as codex suggested). No disagreement remains, so nothing is escalated. The 2-round cap is reached: the PR goes through the reviewbot gate (reviewer-claude and reviewer-codex) as the independent check of the round-2 fixes.
