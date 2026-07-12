# Adjudication — sandbox owner-lease fencing (reaper takeover during apply)

## Ruling

BENIGN-WITH-CAVEATS — no concrete takeover interleaving corrupts `spec.cwd`, unsafely duplicates a forge effect, or causes two parties to modify authoritative bytes concurrently.

## Trace

1. **Reclaim during `import_tree`.** This race can occur. The guard at `sandbox.py:755` may pass, the Lease may then expire, and the reaper may CAS-acquire it at `kube.py:423-435`, prove the Job and Pods absent at `kube.py:437-451`, and remove `job_dir` at `kube.py:452` while `import_tree` is reading it at `sandbox.py:757`.

   Deleting the source does not necessarily make the import raise immediately: already-open descriptors may remain readable, while unopened entries may disappear or fail to open (`import_validator.py:491-507, 536-580`). Nevertheless, neither outcome reaches `apply_back`:

   - If import raises, control exits before `sandbox.py:765-766`.
   - If import completes—possibly with a partial view—or `read_result` completes through an already-open descriptor, the second `_require_claim` at `sandbox.py:765` observes the reaper’s holder, an expired/absent Lease, or the renewer’s permanent `lost_claim`. The flag is checked both before and after the GET at `sandbox.py:814-831`.

   Thus an incomplete imported tree is never applied. Position 1’s claim that source deletion must raise is too strong, but its safety conclusion is correct.

2. **Reclaim during `apply_back`.** The concrete interleaving is:

   1. The pre-apply guard passes at `sandbox.py:765`.
   2. The Lease expires and the renewer flags loss.
   3. The reaper CAS-acquires the Lease.
   4. The reaper removes `<staging_root>/<job_id>` at `kube.py:452`.
   5. The orchestrator continues `apply_back(import_dir, spec.cwd)` at `sandbox.py:766`.

   This does not create a byte race. `apply_back` reads `validated`, which is `import_dir`, and writes `spec.cwd` (`sandbox.py:295-316`). The reaper removes only `staging_root / job_id`; it neither reads nor removes `import_dir` or `spec.cwd`. All `job_dir` reads finished before `apply_back` began (`sandbox.py:757-765`).

   If apply succeeds, the post-apply guard at `sandbox.py:775` reports the lost claim and the run fails after the local mutation. If `apply_back` independently encounters an I/O failure, its destructive replacement can leave the checkout partial because it is not atomic—but the reaper cannot cause that failure by deleting its source, since its source is `import_dir`. That is a pre-existing local-apply property, not an owner-lease fencing race.

3. **Concurrent final removal of `job_dir`.** After the guard at `sandbox.py:775`, cleanup removes `import_dir` and then `job_dir` at `sandbox.py:776-777`. The Lease may expire between that guard and either removal. The reaper can then remove `job_dir` concurrently with the orchestrator’s own removal.

   Both paths tolerate absence and errors: the reaper uses `shutil.rmtree(..., ignore_errors=True)` at `kube.py:452`; `RealSandboxFS.remove` uses the same behavior at `sandbox.py:318-333`. No code reads `job_dir` after `read_result` at `sandbox.py:759`. The only shared operation is duplicate deletion of disposable scratch data, so no result or checkout bytes are corrupted.

   If the reaper has replaced or deleted the Lease, the orchestrator cannot delete the newer holder’s Lease: release carries the last resourceVersion and treats a conflict as loss at `sandbox.py:834-846`; an already-deleted Lease is a no-op at `kube.py:327-355`.

4. **Can the reaper reach `import_dir` or `spec.cwd`?** No. `list_job_ids` accepts only directory names of length 32 satisfying `isalnum()` at `kube.py:398-412`. Despite the comment saying “hex,” the actual predicate is alphanumeric; either way, `<id>.import` contains a dot and is longer than 32 characters, so it is excluded. `reclaim` constructs exactly `self._staging_root / job_id` at `kube.py:452`. There is no reaper path that writes or removes `<id>.import` or anything under the orchestrator-local `spec.cwd`.

5. **Apply succeeded, Lease lost, run failed, then retry.** After a successful apply, loss at the guard on `sandbox.py:775` prevents `SandboxExecutor.run` from returning its `ExecResult`. Therefore the caller does not proceed to tests, commits, pushes, PR creation, reviews, or transitions from that attempt. Workspace handlers discard the local checkout in `finally` (`roles.py:312-313`; `workspace.py:85-87`), and the orchestrator releases the epoch claim after handling the exception (`orchestrator.py:195-229`).

   A later execution obtains a new claim and a fresh per-claim checkout. The failed attempt produced no forge mutation to duplicate; PR dedup is not even needed for this particular interleaving. On ordinary successful workspace execution, the implementer checks the deterministic branch for an existing PR before creation (`roles.py:225, 299-308`).

   If loss occurs only after the final guard and cleanup completes, `run` may return success despite the Lease having changed. That is also safe: apply is complete, all required reads are complete, and no sandbox retry is triggered.

6. **Same-byte concurrency.** The only overlapping resources are:

   - `job_dir` being read by the importer while the reaper deletes it; the post-import ownership guard prevents any resulting tree from being consumed.
   - `job_dir` being deleted by both parties during final cleanup; it is disposable scratch and no longer has readers.

   There is no overlap involving authoritative bytes: the reaper never touches `import_dir` or `spec.cwd`, and the sandbox Job/Pods have already been proven absent before either importer or reaper deletion (`sandbox.py:740-742`; `kube.py:437-452`). Therefore no corrupted or duplicated authoritative artifact can be named.

## Residual effects (if benign)

A lost Lease after successful apply can cause a failed invocation, discarded local checkout, duplicated model execution/cost on retry, and possibly an orchestrator failure count or escalation. Concurrent final deletion is a benign double-cleanup.

There is also a concrete scratch-leak bug: `imported` starts false at `sandbox.py:729` and becomes true only after `import_tree` returns at `sandbox.py:758`; failure cleanup removes `import_dir` only when `imported` is true at `sandbox.py:792-793`. Therefore a mid-import exception can leave a partial `<id>.import`, and the reaper deliberately never discovers it. Set an `import_attempted` flag before entering `import_tree`, or otherwise remove the private destination on import failure. A hard orchestrator crash can likewise leave `.import` scratch and needs a conservative aged-scratch cleanup policy. These are storage/operational leaks, not corruption of `spec.cwd`.

## Recommendation

For R-1, the current isolation design plus the request-start deadline and post-GET `lost_claim` recheck—implemented at `sandbox.py:535-625, 720-728, 809-831`—is sufficient for data safety. A storage-honored fencing token or proof that the orchestrator is dead is not required because the stale orchestrator’s mutation source and sink (`import_dir` and its private local checkout) are disjoint from the reaper’s target (`job_dir`), and ownership is revalidated before any imported tree can be applied. Fix the partial `.import` cleanup leak and preserve the load-bearing rule that the reaper never sweeps `.import`; if a future design lets the reaper or another worker touch `import_dir` or the same `spec.cwd`, this ruling would change and real fencing or prove-dead takeover would then be required.