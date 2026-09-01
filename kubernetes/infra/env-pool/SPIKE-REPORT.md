# env-pool spikes 0 + 1 — report (2026-09-01)

Executes the gating spikes of the test-environment-pool design (agentforge repo,
`plans/2026-09-01-test-env-pool-k8s-plan.md`, codex-finalized). **Verdict: the
Kata-DinD-block architecture is VIABLE on this estate — every linchpin assertion
passed** — with five concrete findings that adjust the implementation (F3–F7).
All numbers are n=1 spike-grade, not SLO evidence.

## Spike 0 — first env-pool node ✅

`talos-env-node-1`: vmid 4401 on ai-node2, 192.168.0.37, 16 GiB fixed / 8 vCPU,
joined cluster `ai` reusing the staged agent-nodes P2 image
(`local:import/talos-v1.11.2-agent-nocloud-amd64.raw`, schematic `0839748e…`,
kata 3.20 + gvisor baked). Ready ≈ 1 min after apply; label `ailab.io/env-pool=true`
+ taint `dedicated=env:NoSchedule` applied cluster-side (NodeRestriction trap, same
as agent-nodes). AgentForge's agent nodes untouched throughout — spike pods carry
the env-pool nodeSelector and cannot schedule anywhere else.

- **Sizing deviation from the plan, deliberate**: live `MemAvailable` measured
  ~24.7–25.0 GiB on ALL three hosts (the plan's ~55–60 GiB ledger figure was
  floors-vs-ceilings paper math; balloons are inflated). A 28 GiB `big` node fits
  nowhere today → spike node is 16 GiB (≈9 GiB host margin). The `big` node is
  gated on freeing host RAM (companion plan: dev-worker idle-stop / ceiling cuts).
- First `tofu apply` races node registration (label/taint fail `node not found`);
  second apply converges. Known shape; acceptable.

## Spike 1 — Kata-DinD-block linchpin ✅ (4 runs, each failure a finding)

| Assertion | Result |
|---|---|
| dockerd storage driver on block PVC | **overlay2** (`SPIKE-1B-DRIVER-OK`) |
| Kata isolation | guest kernel 6.12.42 ≠ node 6.12.48-talos |
| L-class workload in-env | postgres:16-alpine up + answering in 2 s; localhost publish in-guest |
| Image pulls via Zot mirror | 27 s cold for postgres+gitea (the cost the golden snapshot removes) |
| Golden snapshot (`qnap-iscsi` class) | **readyToUse in 2.7 s** (array-side CoW) |
| Restore → warm cache | restored env: `images=2`, **zero pulls**, overlay2 |
| Ephemeral-volume GC | PVCs deleted with pods ✅; **PVs leaked as Released** (F7) |
| exec consumption path | ✅ via the `control` container (F5) |
| Idle footprint (two parked envs, one 16 GiB node) | **97 Mi + 56 Mi** pod working set — the lazy-memory thesis confirmed hard |

### Timing (n=1 each, un-tuned)

Blank-PVC env: apply→dockerd-ready **≈39 s** = PVC provision ~20 s (includes
scheduler retry quanta) + iSCSI attach/map ~11 s + kata boot/start ~7 s +
in-guest ~10 s (of which `apk add e2fsprogs` ~3 s — production bakes it).
Restore-from-snapshot env: **≈95 s**, of which **~60 s was a single
kube-scheduler retry-backoff quantum** on the ephemeral-PVC bind — the array
clone itself is seconds. Consistent with the plan's cold-floor analysis; warm
pools pay this at fill time, invisibly.

### Findings

- **F1** Filesystem-mode PVC → kubelet host-mounts → kata shares via
  **virtio-fs** → `overlay2 EINVAL` → dockerd **silently falls back to vfs**
  (the design's predicted failure, observed verbatim). `volumeMode: Block` is
  REQUIRED; it hot-plugs as virtio-blk and overlay2 works.
- **F2** In-guest mkfs/mount of the block device works; the entrypoint MUST
  assert `Storage Driver: overlay2` and refuse vfs (kept from the plan).
- **F3** busybox `blkid` exits 0 on a blank device → mkfs skipped → mount
  EINVAL. Use **try-mount-else-mkfs**, never signature probing.
- **F4** The kata guest kernel has **no nf_tables**; docker's entrypoint
  selects iptables-legacy and that selection is load-bearing — bypassing
  `dockerd-entrypoint.sh` hard-fails dockerd. Keep the wrapper (with an explicit
  unix-only `--host` to skip the 15 s TCP/TLS-warning slowdown).
- **F5** `kubectl exec` into the dind container **breaks permanently** once
  dockerd enables cgroup-v2 subtree control (kata-agent EBUSY on
  `cgroup.procs`, the no-internal-process rule). The env pod REQUIRES the
  two-container shape: `dind` + `control` (docker CLI over a shared-emptyDir
  socket; `/work` shared at identical paths). All exec traffic (tep sync/run/
  logs) targets `control` — proven working.
- **F6** `dnsPolicy: None` + LAN resolvers works in-guest (no ClusterIP
  dependence); `--registry-mirror=https://registry.chifor.me` works from the
  inner dockerd.
- **F7** The `qnap-iscsi` StorageClass has `reclaimPolicy: Retain` → every
  ephemeral env volume leaks a Released PV + 40 Gi thin LUN. The pool needs its
  **own StorageClass (same driver, `reclaimPolicy: Delete`)**. Spike cleanup:
  patched 5 PVs to Delete → all reclaimed by Trident, zero orphans in this batch
  (the ≥50-cycle churn soak remains spike 3).

## State / handover notes

- This module's tofu state currently lives in the scratchpad clone that applied
  it (worktree-isolated session could not write the main checkout). After merge:
  move `terraform.tfstate` with the module directory into the main checkout,
  `tofu init`, verify a **no-op plan**.
- Spike manifests under `spike/` are throwaway records — everything they created
  was deleted (namespace, RuntimeClass, snapshot, PVs); only `talos-env-node-1`
  remains, plus this module.

## Spike 2 — agent-sandbox v1.0 lease layer ✅ (same day)

Installed the pinned release manifest (`agent-sandbox-controller:v1.0.0`, 4 CRDs,
own namespace; PSA-restricted audit warning on the controller pod — production
hardening item). `SandboxTemplate` carries the full two-container env-pod shape +
a `volumeClaimTemplates` entry restoring from `golden-spike-v2` on the NEW
`testpool-spike-iscsi` StorageClass (`reclaimPolicy: Delete` — the F7 fix).
Readiness authority = the control container's tcp ready-port (opens only after
dockerd answers `/_ping`; no exec probes — F5).

| Measurement | Result |
|---|---|
| Warm-pool fill (all cold stages, off-path) | **86 s** to member Ready (n≈3 consistent) |
| **Warm adoption (claim → bound+Ready)** | **0.355 s / 0.334 s / 0.98 s / 0.23 s** (n=4, incl. one from a dev-worker via the scoped SA) |
| Adopted env functional | warm cache present; `docker run postgres:16-alpine` from cache works |
| Replenishment | starts immediately after adoption, off the claim path; active env + refilling member coexist on the one 16 GiB node |
| Burst on a 1-member pool | first claim adopts (0.98 s); second **cold-creates** — Ready in **142.8 s** (v1.0's documented empty-pool behavior; confirms tep's queue-instead-of-cold-create gate) |
| Claim delete cascade | sandbox 0.3 s → pod 3.4 s → PVC 3.5 s → **PV deleted array-side ~2 min** (Delete class works; no Released leak) |
| **TTL enforcement** | `shutdownTime` fired **to the second** (teardown observed at the exact configured 11:16:43Z) |

## Spike 4 — tep end-to-end from dev-worker-6 ✅ (same day)

Standing namespace-scoped credential per the plan (SA + Role with exec/attach/
portforward `create`+`get`, long-lived token Secret → kubeconfig). `tep-mini.sh`
(in `spike/`) run **on dev-worker-6**: `lease` → **Ready in 0.23 s** → `sync`
(tar-over-exec, 116 KB in 0.23 s; rsync lands in the production image) → `run`:
file visible in `/work/src`, and **`docker run -v /work/src:/mnt` inside the env
served the same file** — the worker → control → dind → container same-path
bind-mount fidelity proof — → `release` (claim deleted, sandbox GC'd, pool
refilled). Node total with a warm env + churn running: **~1.1 GiB / 7%**.

## Spike 3 — clone-churn soak ✅ (completed same day)

50 cycles of ephemeral PVC-from-snapshot → runc-pod attach → delete on the
Delete-class SC (`spike/churn_soak.py`): **50/50 OK, zero pod failures, zero
timeouts, zero leftover PVs/PVCs** — the Velero-style orphan-LUN failure mode
did not manifest on the lease path. Cycle p50 **78.8 s** / p95 **85.8 s** / max
140.8 s (dominated by scheduler-retry quanta + attach, consistent with the
per-stage decomposition). Residual for production: the periodic orphan sweep
still ships (array-side audit, dry-run default) — 50 clean cycles is evidence,
not proof forever.

## Next (per the plan)

Production manifests (SandboxTemplates + `testpool-iscsi` Delete-policy
StorageClass + pre-pull DaemonSet + Flux-pinned agent-sandbox), full `tep`
(supervisor + extend-on-submit + drained-pool queueing), spike 5 (XL in `big`)
once host RAM is freed for the 24 GiB node.
