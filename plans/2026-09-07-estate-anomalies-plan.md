# Estate anomaly remediation — 2026-09-07

## Context

A sweep of ailab + QNAP metrics and logs on 2026-09-07 found **22 firing alerts**. Triage separated
them into four genuine config defects, one real capacity problem, and a tail of stale/one-off noise.
Cluster fundamentals are healthy: etcd 3/3 in sync (leader cp2, term 387, identical raft index, no
errors), all 7 nodes Ready, every Flux kustomization Applied/True, QNAP hardware all-good (5× KC3000,
SMART GOOD, 54-55 °C).

The single most serious finding is **not** in the alert list, because nothing alerts on it.

### P0 — `tofu apply` on the runners module would collide with the cloudlab GPU cluster

`kubernetes/infra/runners/variables.tf` on `main` actively declared `ci-runner-6..10` (vmids
4106-4110), but `terraform.tfstate` tracks **only** `ci-runner-1..5`. Measured 2026-09-07 with main's
file in place:

```
Plan: 10 to add, 0 to change, 0 to destroy.
  + proxmox_virtual_environment_vm.runner["ci-runner-7"]  address = "192.168.0.20/24"
  + proxmox_virtual_environment_vm.runner["ci-runner-8"]  address = "192.168.0.21/24"
  + proxmox_virtual_environment_vm.runner["ci-runner-9"]  address = "192.168.0.22/24"
```

`.20/.21/.22` are **cloud1/cloud2/cloud3** — the cloudlab GPU hypervisors (bare metal, separate
Proxmox cluster, separate repo). A `just runners` would have tried to create five VMs at vmids that
already exist, three of them cloud-init'd onto another live cluster's addresses.
`lifecycle { ignore_changes = [initialization] }` makes `ip` documentation-only for an *existing* VM,
but **not at create time** — which is exactly the path an un-imported apply takes. This is the same
mechanism that produced the 2026-09-03 Talos collisions.

Already addressed in commit `7ab78e79` (this branch): entries commented out with correct addresses
recorded, verified `No changes` afterwards. What remains is bringing them under management.

### Ground truth established during triage

Live mapping, verified via node_exporter `nodename` and `qm list` on each host:

| runner | IP | host | vmid | in tofu state? | scraped? |
|---|---|---|---|---|---|
| ci-runner-1..5 | .14-.18 | node1/2/3 | 4101-4105 | yes | yes |
| ci-runner-6 | .19 | ai-node3 | 4106 | **no** | yes |
| ci-runner-7 | **.29** | ai-node3 | 4107 | **no** | **no** |
| ci-runner-8 | **.30** | ai-node1 | 4108 | **no** | **no** |
| ci-runner-9 | **.31** | ai-node2 | 4109 | **no** | **no** |
| ci-runner-10 | .23 | ai-node2 | 4110 | **no** | yes |

`.20/.21/.22` = cloud1/2/3 (kernel `6.14.11-9-pve`, vs runners' `6.8.0-*-generic`).

## Approach

Six workstreams, ordered by risk. Each is independently shippable; W1-W4 are pure config and carry no
downtime. W5 is gated on explicit operator approval per the standing rule that no ai-node leaves
service without per-action sign-off.

### W1 — Stop scraping the wrong three machines (P1, no downtime)

`kubernetes/apps/infrastructure/monitoring/ci-runners-node.yaml` lists `.20/.21/.22` in its
`Endpoints`. Two consequences: **ci-runner-7/8/9 have zero alerting** (no disk-full, no memory, no
swap, no reclaim-beacon — the exact suite that exists to catch the failures behind ailab#249 and the
runner-reclaim outage), and every `job="ci-runner-node"` rule is evaluated against cloudlab
hypervisors, so a real cloudlab problem would page as a CI-runner problem.

Replace `.20/.21/.22` with `.29/.30/.31`. Final set: `.14-.19`, `.23`, `.29-.31` (10 targets, all
genuinely CI runners). Probed 2026-09-07: all three of `.29/.30/.31` serve `:9100` (HTTP 200), are
healthy (~70 % root used, 55-63 GiB free), and **do** emit fresh
`gitea_runner_reclaim_last_run_seconds` — so the three `CIRunnerMaintenanceBeaconMissing` alerts are
pure false positives from the wrong hosts and will clear once the target list is corrected.

Add a guard so this cannot silently recur: an alert that fires when any `ci-runner-node` target
matches `node_uname_info{release=~".*-pve"}` — a Proxmox host has no business in this job.

### W2 — Cover the env-node with the infra DaemonSets (P1, no downtime)

`talos-env-node-1` joined 2026-09-01 carrying `dedicated=env:NoSchedule`. Three DaemonSets tolerate
only `dedicated=agent`, so each reports `desired=6, misscheduled=1` with a pod stranded on the
env-node that the controller will not manage:

- `monitoring/alloy` — log/metric shipping
- `monitoring/storage-fabric-probe` — storage probing
- `velero/node-agent` — **filesystem backup coverage**

Seven other DaemonSets (cilium, cilium-envoy, csi-nfs-node, node-exporter, iscsi-recovery-tmo,
trident-node-linux) already show `desired=7`, which is the evidence that covering the env-node is the
intended behaviour and these three were simply missed.

Add a `dedicated=env` toleration to each. Prefer `operator: Exists` on key `dedicated` where the
chart allows it, so the next dedicated node class doesn't reopen this. Clears 6 of the 22 alerts
(`KubeDaemonSetMisScheduled` ×3, `KubeDaemonSetRolloutStuck` ×3).

### W3 — Retire the dead `llm` endpoint (P1, no downtime)

`kubernetes/apps/apps/ai/llm-service.yaml` still lists `192.168.0.44:8080` in both the `llm`
Endpoints (which drives the ServiceMonitor scrape) and the `llm-node1` Endpoints. Verified on the
host: `ai-llm-1` runs only `llama-swap-qwen38.service` on **:8082**; there is no
`llama-server.service` at all, while `ai-llm-2` (.45) has one on :8080. The retirement is intentional
— node1's qwen3.6 deployment was disabled 2026-08-15 and the model retired entirely in #524/#525 —
but the Endpoints and the `llm-node1` Service were never cleaned up, leaving a permanent
`TargetDown{job=llm}`.

Drop `.44` from the `llm` Endpoints and delete the now-unused `llm-node1` Service/Endpoints pair
(confirm no remaining referent first). Update the stale file header, which still claims node1 and
node2 both run qwen3.6 pinned on :8080.

### W4 — Backup and job hygiene (P2, no downtime)

- **Velero `PartiallyFailed` (09-06, 09-07).** Root cause from the backup log: `failed to get PV ...
  for PVC testpool/dockerlib-env-std-pool-2ds7g: persistentvolumes "..." not found` — an env-pod PVC
  whose PV was already deleted mid-teardown. `testpool` is the ephemeral leasable test-env pool;
  backing it up has no value and races teardown by construction. Exclude the namespace from the
  `velero-daily`/`velero-weekly` schedules. This also stops the chain's last-success timestamp from
  stalling.
- **Renovate.** The 09-03 failure was transient (GitHub rate-limited an `ls-remote`) and later runs
  succeed, but the failed Job lingers and keeps `KubeJobFailed` firing. Set `ttlSecondsAfterFinished`
  / tighten `failedJobsHistoryLimit` so a transient failure self-clears. Separately: Renovate targets
  `github.com/cchifor/ailab` — the **read-only mirror** — not the Gitea master forge, and GitHub
  rejected its credential as *unauthenticated*. Repoint at Gitea, or record an explicit decision to
  keep it on the mirror.
- **Reviewbot quarantines.** Two genuine one-offs, both 2026-09-06 ~20:10, needing a manual requeue:
  `.24` (claude) platform#1095, 5 attempts exhausted on a Claude Max session limit; `.25` (codex)
  platform#1096, `ambiguous POST: Connection reset by peer` (auto-retry correctly refused). Requeue
  both. The claude-side case is a known class — a rate limit should back off past the reset time
  rather than burn the attempt budget; file that as a follow-up rather than widening this change.
- **`ForgeProvisionerSeatEntitlementDrift`.** Three seats (`anthropic/claude-max-1`,
  `anthropic/claude-max-2`, `openai/codex-pro`, all `tenant-zero/playground`, kid
  `tz-213512e80fde7c84`) are granted in the broker kid registry but absent from
  `openbao/capability-kids-configmap`. The provisioner drift-corrects the auth roles every ~30 s and
  the finding immediately returns — a reconciliation loop, not a transient. Either declare the three
  seats or revoke the grants; do not silence the alert.

### W5 — ai-node2 memory (P1 severity, gated on approval)

`ai-node2` is at **120/124 GiB used, 4 GiB available**, load average 20.7. Guest ceilings total
~184 GiB against 124 GiB physical (talos-cp2 24G + ci-runner-2/5/9/10 at 24G each + dw2 16G + dw5 16G
+ agent-node-2 16G + env-node-1 16G), plus the ai-llm-2 LXC. That is far above PVE's 80 % auto-balloon
threshold, so every balloonable guest is pinned at its floor permanently. QEMU balloon stats:

| VM | balloon | total_mem | swapped out | major faults |
|---|---|---|---|---|
| dw2 (4202) | 4096 (floor) | 3.7 GiB | 21.6 GB | 3.9M |
| dw5 (4205) | 6144 (floor) | 5.75 GiB | **140.5 GB** | **39.6M** |

This failure mode is already documented in `dev-workers/variables.tf` — but for **ai-node1**, measured
2026-08-12 at 88 %, mitigated by 12 GiB floors on dw1/dw4. The problem has since **moved**: node1 now
has 25 GiB free (its LLM no longer pins qwen3.6) while node2 has gone 93 % → 97 %. dw5's 6144 override
(added after the 2026-09-01 swap-death incident) is demonstrably insufficient, and **dw2 was never
given an override at all**. Pressure grew when ci-runner-9 + ci-runner-10 started on node2 on
2026-08-25 23:02 (+48 GiB of ceilings), then env-node-1 (09-01) and agent-node-2 (09-02).

Two stages, the second gated:

1. **Immediate, low-risk.** Raise dw2's floor from the uniform 4096 and dw5's from 6144, codified in
   `dev_worker_nodes`. `qm set --balloon` cannot inflate a running guest, so this needs a reboot of
   those two dev-workers to take effect — dev-workers, not ai-nodes, so no cluster impact. Note this
   only redistributes scarcity on a host that is already oversubscribed: it buys headroom, it does
   not fix the ratio, and raising floors on a starved host can push pressure onto its neighbours.
2. **The actual fix — REQUIRES EXPLICIT APPROVAL BEFORE ANY GUEST IS STOPPED.** Move one guest off
   node2. node1 has 25 GiB free and node3 22 GiB, so there is somewhere to land. Per-node `local-lvm`
   + `cpu: host` means this is a rebuild, not a live migration. Best candidate is one of the two
   out-of-band runners that landed on node2 on 08-25 (ci-runner-9 or ci-runner-10): most recent, most
   stateless, cheapest to recreate. Rebuilding one through tofu also serves W6's import goal.
   **This step must not be executed without per-action sign-off.**

### W6 — Bring the out-of-band guests under IaC (P2, follow-up)

Five runners (4106-4110), the env-node (4401), and the two reviewer VMs (4501-4502) exist with no tofu
state. Import them so the module manages what actually exists, then uncomment their entries:

```
tofu import 'proxmox_virtual_environment_vm.runner["ci-runner-6"]' ai-node3/4106
```

Reconcile sizing against each live VM before enabling, and re-run `tofu plan` expecting `No changes`.
Do the imports one at a time, verifying `plan` after each. This is what makes the P0 fix permanent
rather than a comment someone eventually deletes.

### Out of scope (recorded, not fixed here)

- **QNAP orphaned LUNs.** Two PVs (`pvc-c61ddeac…`, `pvc-de79ab3b…`, 48Gi each, `Delete` policy) retry
  `Unmap IscsiTarget fail … Error Code : -19` roughly every 90 s; three more sit `Released`/`Retain`.
  Known pattern, needs QNAP-side LUN cleanup — a storage runbook task, not a config change.
- **QNAP has no capacity monitoring.** `qnap-rules.yaml` covers only SMART, temperature, fan and
  exporter presence; the SNMP exporter emits 28 series and **none** are volume or pool usage. The
  array backs every `qnap-iscsi` PV, so this is a real gap — but adding capacity OIDs is its own
  change with its own verification, and folding it in here would blur the diff.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/infra/runners/variables.tf` | P0 — already fixed in `7ab78e79`; W6 uncomments after import |
| `docs/network-plan.md` | IPAM registry; landed in `7ab78e79`, source of truth for every address above |
| `kubernetes/apps/infrastructure/monitoring/ci-runners-node.yaml` | W1 — Endpoints `.20-.22` → `.29-.31` |
| `kubernetes/apps/infrastructure/monitoring/ci-runners-rules.yaml` | W1 — add the "no PVE host in this job" guard |
| `monitoring/alloy*.yaml`, `monitoring/storage-fabric-probe*.yaml` | W2 — env toleration |
| Velero HelmRelease (`node-agent` tolerations + schedule `excludedNamespaces`) | W2 + W4 |
| `kubernetes/apps/apps/ai/llm-service.yaml` | W3 — drop `.44`, delete `llm-node1`, fix stale header |
| `kubernetes/infra/dev-workers/variables.tf` | W5.1 — dw2/dw5 balloon floors |
| renovate CronJob manifest | W4 — TTL + forge target |
| `openbao/capability-kids-configmap` | W4 — declare or revoke the three seats |

## Verification

Per workstream, evidence before assertion:

- **W1:** `count(up{job="ci-runner-node"}) == 10`; `up{job="ci-runner-node",
  instance=~"192.168.0.2[012]:9100"}` returns empty; `gitea_runner_reclaim_last_run_seconds` present
  for all 10; the three `CIRunnerMaintenanceBeaconMissing` alerts clear within one `for: 30m` window.
- **W2:** all three DaemonSets report `desired=7, ready=7, misscheduled=0`;
  `KubeDaemonSetMisScheduled` and `KubeDaemonSetRolloutStuck` clear. Confirm the previously-stranded
  pods are recreated from the DS's current template rather than adopted as-is.
- **W3:** `up{job="llm"} == 0` returns empty; `TargetDown` for ns `ai` clears; a `/v1/models` probe
  through LiteLLM still answers for every model it advertises (proves no live route was removed).
- **W4:** next `velero-daily` completes `Completed` with 0 errors; `kubectl -n renovate get jobs` shows
  no lingering `Failed`; both reviewbot personas report `reviewbot_quarantined_recent_jobs == 0`;
  provisioner logs stop emitting `seat-undeclared-grant`.
- **W5.1:** `qm monitor <vmid>` `info balloon` shows `actual` at the new floor after reboot;
  `node_memory_SwapFree` recovers and `DevWorkerSwapPressure`/`DevWorkerThrashing` clear for `.9`/`.12`;
  re-check ai-node2 `free -g` to confirm the neighbours did not get squeezed in exchange.
- **W6:** after each import, `tofu plan` in `kubernetes/infra/runners` reports `No changes. Your
  infrastructure matches the configuration.` — the same check that proved the P0 fix was a no-op.

Cluster-wide gate before calling this done: `kubectl get kustomization -A` all `True`, alert count down
from 22 to the expected residue (Watchdog + InfoInhibitor + whatever W5.2/QNAP items remain knowingly
open), and etcd still 3/3 in sync.

<!-- codex-review-status: pending -->
