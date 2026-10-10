# ADR 0037 — Retire the leasable test-env pool (testpool, agent-sandbox, `tep`, the env node)

**Status:** ACCEPTED (2026-10-10), owner-directed. The owner asked whether the pool was worth
keeping; the analysis recommended removal and the owner said "go ahead". Execution plan, gates and
review trail: `plans/2026-10-10-retire-test-env-pool-plan.md`.
**Supersedes:**
- the test-env-pool design: agentforge repo `plans/2026-09-01-test-env-pool-k8s-plan.md`, and the
  spike report in `kubernetes/infra/env-pool/SPIKE-REPORT.md` (removed; see git history);
- the `tep` half of ADR 0021.

helmtest (ADR 0021) and platform access (ADR 0028) are unchanged.
**Relates to:** #865 (the pool was unusable for the platform e2e), #880 (warm pool paused), #972
(restore gate, closed by this ADR), #835 (env-node-2, abandoned).

## Context

From 2026-09-01 the estate ran a pool of disposable Kata DinD environments for the dev-worker
agents. Each env was restored from a golden snapshot on the QNAP, leased with the `tep` CLI, and
destroyed on release, so that heavy suites would not run on the shared workers. It ran on one
16 GiB Talos worker, `talos-env-node-1` (VM 4401). Six weeks later:

- **It was not used.**
  - Zero leases from 09-12 to 09-25, while agents ran about 250 Playwright and 300 compose sessions
    on the workers themselves (#865).
  - One claim in the 15 days of Prometheus history up to 2026-10-10: a test lease.
- **It could not run the workload it was built for.**
  - The platform e2e stack is about 32 containers with about 17 GiB of declared limits.
  - An env was 8 Gi dind (an ~11 GiB Kata guest) on a node that also held the refill member. A
    full build OOM'd the node.
  - The platform repo's routing change was never merged, because it would have routed agents to a
    pool that could not finish the smoke run.
- **It damaged shared infrastructure.**
  - Clone churn on the NAS killed the shared iSCSI target of every golden clone (09-26).
  - A dangling testpool LUN then broke **all** new `qnap-iscsi` provisioning (09-27), which blocked
    a `strive-pg` replica re-clone.
  - Kata guest freezes wedged the env node's kubelet twice (09-20, 09-24); the root cause was never
    found.
- **It was off.** The warm pool had been at 0 replicas since 09-26. Restoring it (#972) needed:
  - NAS surgery;
  - a golden-v2 built from a retained source;
  - a containerd image store;
  - a second env node;
  - the freeze root cause.

  Even then it would have served one lease per 16 GiB node, for four workers that run several
  agents each.
- **It still cost.**
  - 16 GiB of fixed RAM and 8 vCPU on ai-node2.
  - Orphaned LUNs on the NAS; each attach/detach sweep is O(LUNs²) (`qnap-storage-setup.md` §9).
  - A log relay, reaper, controller, alerts and dashboard rows, all still running.
  - An extra node in every Talos upgrade.
- **It gave no isolation.** The pool was trusted-code-only, the same posture as a dev-worker.

## Decision

Remove all of it:
- the `testpool` and `agent-sandbox` Flux trees;
- the `tep` CLI and kubeconfig on the workers, and the `tep` targets of `openbao-k8stoken-sync`;
- the env-node alerts, `cri-log-relay` and the dashboard row/tile;
- the `env-image` build;
- `talos-env-node-1` and its tofu module;
- the pool's four LUNs on the NAS.

`.37` is freed and the `.39` reservation released. The RAM returns to ai-node2.

**Where heavy tests run instead.** Each repo says where each tier runs (platform: `CLAUDE.md` §
"Where tests run"). Without such a section:
- lint, unit and in-memory integration tests run on the worker;
- full-stack e2e is CI's job; CI brings up the same compose stack, with prebuilt images, on the
  runner pool.

A worker runs at most one compose stack at a time, whatever a repo says; a repo may narrow that
rule, never widen it. This is in the dev-worker skills (`ansible/roles/dev_worker/files/`).

**Kept:**
- the `kata` RuntimeClass and the `kata_gvisor` Talos schematic (agent-node-3 uses them);
- helmtest (unused today, but it reserves no capacity);
- the `dedicated=env:NoSchedule` tolerations on three DaemonSets, and the velero
  `excludedNamespaces: testpool` entry. Both are inert now, and changing them would only roll
  workloads.
- the Zot `testpool` sync prefix (its repos can be GC'd later).

### Accepted residue — do not "clean up" piecemeal

- **Trident records.** TridentVolumes `pvc-8e290587-6ea9-4912-8374-39c7b8b8d46b` (the golden source,
  state `deleting`), `pvc-1aacdbdc-11d8-40da-8102-9c19afe282a8`,
  `pvc-3e4f4cbc-66d7-4770-b7de-46721eef91a4`, `pvc-43285e38-0091-49ee-acd2-38c32b59812e`,
  `pvc-48c73270-ae89-4967-984f-9f5157e87aa7`, `pvc-743aa279-7722-4456-b545-a00dfc5d8d07`,
  `pvc-842a2790-9972-4a04-bf8b-e3a68c72ecda`, `pvc-864c27c9-6212-450e-b90f-929ef7a537cd`,
  `pvc-f75344b3-7a11-4083-8bc8-514819c5a1ac`. TridentSnapshot
  `pvc-8e290587-6ea9-4912-8374-39c7b8b8d46b-snapshot-c004c88c-5f06-4b70-9fd8-a828a44220f0`.
  - Their NAS backing is gone.
  - Deleting them through Trident calls the QNAP driver's delete, which loops on "not found" with
    an O(LUNs²) sweep per attempt (09-27). Deleting the CRs under a running controller is
    unsupported.
  - **Never delete the TridentSnapshot alone.** It keeps the source volume parked in `deleting`;
    without it Trident would try to delete the parent.
  - They are inert: no publications, no transactions, no log activity. Remove all ten together at
    the next planned Trident/operator restart (controller and operator scaled to 0), or leave them.
- **KV field `af/dev-workers/<host>.tep_kubeconfig`.** No longer written or rendered. It holds a
  token for a deleted ServiceAccount.
- **The `cri-log-relay` talosconfig.** Its `os:reader` client cert, minted 2026-09-21, cannot be
  revoked (Talos has no CRL) and expires about **2026-12-20**. Only the SOPS copy in git history
  remains.
- **The NAS placeholder zvol `zpool1/orphan_placeholder_from_lun9_20260927`.** It is 84.8K, with no
  LUN and no SCST device; it was left by the 09-27 LUN-9 repair. QuTS refuses to destroy its
  snapshots from the shell ("permission denied", even as root, no holds). It is harmless; remove it
  from the QuTS UI if it ever matters.
- **The retired tofu state.** The `kubernetes/infra/env-pool` state, its backups and the old plans
  embed the cluster machine secrets via remote state. They now exist only as
  `kubernetes/infra/_out/env-pool-retired-20261010.tar.age` (estate age key) in the ops checkout;
  the plaintext copies were deleted.

## Consequences

- **The off-worker path for heavy suites is gone.** It was documented but unusable. The estate
  skills now defer to the repo's "Where tests run" section rather than to `tep`.
- **Outside ailab:** `agentforge` (`dashboard/e2e-pool.sh`) and `agentforge-platform`
  (`webapp/e2e-pool.sh`, plus a unit test that requires Playwright to run inside `tep run`) still
  wrap `tep`.
  - They have failed since 09-26, when the pool went to 0.
  - Now they fail at "tep: command not found" instead of after a 15-minute queue.
  - AgentForge is outside this estate's change scope (owner, 2026-10-08), so those repos are left
    for their owner.
- **A legacy `tep-dwN-token` client is unidentified.**
  - `serviceaccount_legacy_tokens_total` rose on cp1 on 09-26 (the #877 live test) and 3× on 10-03
    ~19Z.
  - The audit log had rotated, so the client could not be found.
  - Its next attempt gets a 401, from a pool that has been off since 09-26.
- **Kata lessons worth keeping** for any Kata user (agent-node-3). The full runbook is
  `docs/runbooks/env-pool.md` at commit `2c731cb9`.
  - A frozen guest (a virtio-fs stall) can make its teardown hang forever. Hung teardowns
    accumulate until the kubelet wedges.
  - A host-side reaper that SIGKILLs the VMM of a pod stuck `Terminating` bounds the damage.
  - Readiness has to be decided inside the guest: an exec probe stayed Ready through a 7-minute
    freeze.
  - `talosctl reboot` hangs on such a node. A `qm reset` recovers it in about 40 s.
- **Rebuilding a pool later** starts from git history and a new golden. It is new provisioning
  (`apply_mode = "auto"`, a staged v1.14.2 image), not a re-apply.
- **Gains:** one fewer Talos node to upgrade, about 16 GiB back on ai-node2, and four fewer LUNs in
  every NAS attach/detach sweep.
