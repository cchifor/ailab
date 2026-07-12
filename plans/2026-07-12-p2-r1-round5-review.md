# Implementation review — agentforge-v2 P2 R-1 (owner-lease fencing) — round 5
<!-- codex-r1-review-status: pending -->

## Summary

- F5b-fencing is PARTIALLY-CLOSED: the renewer now spans stage through cleanup and loss is sticky, but its monotonic deadline is incorrectly advanced from response-arrival time rather than the Lease’s renewal/request time. API latency can therefore hide a real expiry and silently heal the lapse.
- NEW-pre-apply/delete-fencing is PARTIALLY-CLOSED: the resourceVersion-preconditioned release is correct, and the guards precede each mutation, but those point-in-time guards do not fence an unbounded synchronous `apply_back` or `remove`.
- The stated residual is not acceptable. A renewal stall after the final guard can let the Lease expire and the reaper acquire and rmtree while the orchestrator is still applying or cleaning up; exposure is bounded by the filesystem operation, not by one renewal tick.
- The new tests contain real assertions for the intended paths, but omit slow create/renew responses and takeover during a blocked apply/remove. The live-renewer change also introduced a `lost_claim` check/GET race.

## Findings

### [PARTIALLY-CLOSED] F5b-fencing — lifecycle is fixed, but the monotonic deadline does not track actual Lease expiry
**Location:** src/agentforge/adapters/exec/sandbox.py:575, src/agentforge/adapters/exec/sandbox.py:598, src/agentforge/adapters/exec/kube.py:247, src/agentforge/adapters/exec/kube.py:295  
**Severity:** blocker

**Proof:** The executor creates the Lease at `sandbox.py:691`, starts the renewer at `sandbox.py:698`, and only then stages at `sandbox.py:702`. Its only operational `stop()` is in `finally` at `sandbox.py:746`, after apply and cleanup. Thus there is no intentional mid-run stop or pause.

Loss state is thread-safe and permanent: `_lost` is a `threading.Event`, all loss paths set it at `sandbox.py:579-600`, and no path clears it. The successful record is protected by `_lock` at `sandbox.py:603-604` and `sandbox.py:609-610`. Each renewal is wrapped in `asyncio.wait_for` at `sandbox.py:587`, and a response arriving at or after the local prior deadline sets loss and returns before updating the record at `sandbox.py:598-604`.

The deadline itself is unsound, however. Kubernetes Lease `renewTime` is stamped before the API request at `kube.py:295-300`, while the renewer advances its deadline from response-arrival time at `sandbox.py:598-602`. The initial create has the same problem: `renewTime` is stamped at `kube.py:247-254`, but the initial monotonic deadline is not seeded until after the create response and renewer-thread startup at `sandbox.py:575`.

For example, with duration 10 seconds, a create stamped at t=0 that returns at t=9 produces a local deadline of t=19, although the Lease expires at t=10. The default first renewal is not attempted until roughly t=14. A reaper can CAS-acquire at t=10–14 while stage is still running; the later renewal merely gets a 409 and reports loss after takeover. Similarly, a renewal stamped at t=1 but returning at t=9 advances the local deadline to t=19 although that renewal actually expires at t=11. This still permits the “unobserved lapse healed by a late success” that the fix was intended to eliminate.

The injected-clock test at `tests/unit/test_sandbox_executor.py:560-580` is a real assertion: it causes one successful renew to return after its synthetic local deadline and verifies sticky loss plus no second renewal. It does not test response latency shifting the local deadline beyond the Lease’s real `renewTime + duration`. The slow-stage test at `tests/unit/test_sandbox_executor.py:583-624` proves normal, low-latency renewals happen during stage and stale RV acquisition conflicts, but likewise does not exercise this timing error.

<!-- codex: Capture a monotonic lower bound immediately before create and before every renew request. Pass the create-start value into the renewer, and advance the next deadline from renew-request-start + duration, never response-arrival + duration. Add a start/ready handshake or a claim guard before stage. Add delayed-create and delayed-renew tests whose response arrives near the duration boundary and verify that the lapse is permanently detected rather than healed. -->

### [PARTIALLY-CLOSED] NEW-pre-apply/delete-fencing — CAS release is closed, but the critical section remains lease-TOCTOU vulnerable
**Location:** src/agentforge/adapters/exec/sandbox.py:716, src/agentforge/adapters/exec/sandbox.py:726, src/agentforge/adapters/exec/sandbox.py:736, src/agentforge/adapters/exec/sandbox.py:770, src/agentforge/adapters/exec/kube.py:421  
**Severity:** blocker

**Proof:** The guards are correctly placed before import at `sandbox.py:716`, before apply at `sandbox.py:726`, and before cleanup at `sandbox.py:736`. A detected loss raises before `apply_back` at `sandbox.py:727`, shared-directory removal at `sandbox.py:738`, and release at `sandbox.py:747-748`. The mid-import regression at `tests/unit/test_sandbox_executor.py:627-672` genuinely forces a renewal conflict and asserts that apply, shared rmtree, and Lease delete are all skipped.

The delete half is closed. The executor supplies `renewer.last_record.resource_version` at `sandbox.py:748` and passes it to `delete_lease` at `sandbox.py:792`. `KubernetesLeaseClient` constructs `V1DeleteOptions(V1Preconditions(resource_version=...))` at `kube.py:338-345`; 404 is idempotent and 409 becomes `LeaseConflict` at `kube.py:350-355`. `_release` catches that conflict and logs it at `sandbox.py:793-795`. The fake delete matrix at `tests/unit/test_lease.py:134-166` has substantive state assertions for unconditional, matching-RV, stale-RV/new-holder preservation, and absent-Lease cases.

The mutation fence remains incomplete. A concrete interleaving is:

1. `_require_claim` returns while the Lease is fresh.
2. The orchestrator enters synchronous `apply_back` at `sandbox.py:727`, which performs unbounded filesystem deletion/copying at `sandbox.py:295-314`.
3. The renewer’s next API call stalls or fails. The Lease reaches its expiry while `apply_back` remains blocked.
4. The reaper reads the expired Lease and CAS-acquires it at `kube.py:421-429`. Because the executor already quiesced the Job at `sandbox.py:712`, the reaper’s absence proof can complete immediately.
5. The reaper re-confirms its RV at `kube.py:440-445` and rmtrees the shared job directory at `kube.py:447` while the orchestrator is still applying the imported result to `spec.cwd`.
6. The renewer eventually sets `lost_claim`, and the next guard prevents cleanup, but it cannot undo the already-running or completed apply.

The same race exists during cleanup: after the guard at `sandbox.py:736`, `remove(import_dir)` at `sandbox.py:737` can block past expiry; the reaper can then acquire and rmtree `job_dir`, after which the orchestrator resumes and calls `remove(job_dir)` at `sandbox.py:738`. That is an actual concurrent/double destructive cleanup.

There is also a narrower newly introduced guard race: `_require_claim` checks `renewer.lost_claim` only before its awaited GET at `sandbox.py:770-774`. The renewer can set permanent loss during that await, while GET still returns a fresh self-held record; the guard then passes without rechecking the authoritative flag.

Thus the claimed exposure is not “sub-renewal-tick scale.” Loss detection may occur at the next tick, but the synchronous filesystem mutation continues for an unbounded duration after detection.

<!-- codex: Recheck lost_claim after the awaited GET, but do not treat that as the complete fix. A synchronous renew immediately before mutation also cannot fence an unbounded operation. The mutation endpoint needs an enforceable fence: for example, a storage-backed exclusive lock/fencing token honored by both executor and reaper, or an execution owner whose process/pod the reaper can revoke and prove terminated before takeover. Add deterministic tests that block apply_back and remove across expiry while driving a real reaper acquire; the reaper must be unable to rmtree until the old mutation owner is definitively stopped. -->

## Verdict

R-1 owner-lease fencing is not yet sound to proceed to R-2. The resourceVersion CAS release and intended renewer lifecycle are materially improved, but the incorrect deadline anchor can hide actual Lease expiry, and a genuine renewal failure after a guard still permits reaper takeover during unbounded apply or cleanup. These are code-level lease-safety blockers independent of the image-gated reaper-enable and live-NFS v1.1-flip prerequisites.