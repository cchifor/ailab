# Estate anomaly remediation — 2026-09-07

## Codex Review

- The measured P0 reproduction, verified address mapping, and separation of QNAP follow-ups provide a strong basis for remediation.
- W1 is suitable for an in-place Git change, provided Flux owns the Endpoints fields; W2 needs an explicit coverage decision and a rollout procedure for stale pods.
- W3 has a confirmed consumer: `litellm-local.yaml` still routes to `llm-node1`. Remove that deployment and reload consumers before pruning the Service.
- W5.1 should follow capacity relief unless a quantified memory budget demonstrates safety. Higher balloon floors can endanger neighbouring guests, including talos-cp2.
- W6 needs configuration before import, protected state handling, and accounting for all ten planned resources; it must precede runner-module applies or rebuilds, without blocking independent monitoring fixes.

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

<!-- codex: Containment is only effective for operators using this commit; land it on main and prohibit runners-module applies from older checkouts until state reconciliation is complete. Flux does not apply infra/, so merging this containment does not itself provision VMs, and rollback must preserve the corrected addresses and disabled unmanaged entries. -->

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

<!-- codex: Independence and zero downtime are overstated: W2 can roll node agents, W3 requires consumer rollout before deletion, W4 includes operational requeues and entitlement changes, and W5.2 depends on W6 for the runners module. Separate automatically reconciled apps/ commits from manually applied infra/ work, with explicit rollout and rollback gates. -->

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

<!-- codex: The Service is selectorless, making an in-place Git edit appropriate; check live managedFields and the owning Flux Kustomization before reconciliation because Endpoints subsets is an atomic list whose ownership covers the whole list. Submit the complete intended subsets value, including ports and retained addresses, rather than relying on another manager's partial additions surviving [Flux server-side apply](https://fluxcd.io/flux/components/kustomize/kustomizations/). -->

<!-- codex: Keep the Endpoints name, namespace, and owning Kustomization unchanged: removing addresses updates a field, whereas removing or moving the object between inventories can trigger Flux pruning. Verify any mirrored EndpointSlices converge to the same addresses, and avoid independently editing controller-owned slices or introducing an EndpointSlice migration into this fix. -->

Add a guard so this cannot silently recur: an alert that fires when any `ci-runner-node` target
matches `node_uname_info{release=~".*-pve"}` — a Proxmox host has no business in this job.

<!-- codex: Scope the guard explicitly to job="ci-runner-node"; the kernel suffix catches this incident but cannot detect a wrong Ubuntu host, a missing target, or a missing node_uname_info series. Validate expected target identities against the established nodename mapping and retain separate target-availability checks. -->

### W2 — Cover the env-node with the infra DaemonSets (P1, no downtime)

`talos-env-node-1` joined 2026-09-01 carrying `dedicated=env:NoSchedule`. Three DaemonSets tolerate
only `dedicated=agent`, so each reports `desired=6, misscheduled=1` with a pod stranded on the
env-node that the controller will not manage:

<!-- codex: NoSchedule prevents new placement but does not evict an existing pod, so misscheduled does not establish that the controller has lost ownership or that the agent is nonfunctional. The supplied Alloy/storage-probe comments already identify stale pre-taint pods; check ownerReferences, pod revision, and actual agent output before describing coverage as absent. -->

- `monitoring/alloy` — log/metric shipping
- `monitoring/storage-fabric-probe` — storage probing
- `velero/node-agent` — **filesystem backup coverage**

Seven other DaemonSets (cilium, cilium-envoy, csi-nfs-node, node-exporter, iscsi-recovery-tmo,
trident-node-linux) already show `desired=7`, which is the evidence that covering the env-node is the
intended behaviour and these three were simply missed.

<!-- codex: Other DaemonSets' placement does not establish the intended policy for these three, and this list names six rather than seven. Alloy explicitly documents all-node coverage, but justify storage probing and Velero separately, especially if W4 excludes testpool; if an agent should be excluded, retain that policy and remove its stale pod instead of tolerating the taint merely to clear alerts. -->

Add a `dedicated=env` toleration to each. Prefer `operator: Exists` on key `dedicated` where the
chart allows it, so the next dedicated node class doesn't reopen this. Clears 6 of the 22 alerts
(`KubeDaemonSetMisScheduled` ×3, `KubeDaemonSetRolloutStuck` ×3).

<!-- codex: Prefer an explicit key=dedicated, operator=Equal, value=env, effect=NoSchedule entry alongside agent; Exists silently extends these agents' access and resource usage to future dedicated node classes. That broader placement policy needs its own justification rather than being a preventive default. -->

<!-- codex: Inspect each rendered DaemonSet's tolerations and updateStrategy: RollingUpdate should replace stale pods, while OnDelete requires manual deletion after the new template has reconciled. Wait for a normal rollout first, then delete only identified stale pods individually if required and verify replacement; changing eligibility can clear misscheduled counts without proving pod replacement ([Kubernetes rollout behavior](https://kubernetes.io/docs/tasks/manage-daemon/update-daemon-set/)). -->

### W3 — Retire the dead `llm` endpoint (P1, no downtime)

`kubernetes/apps/apps/ai/llm-service.yaml` still lists `192.168.0.44:8080` in both the `llm`
Endpoints (which drives the ServiceMonitor scrape) and the `llm-node1` Endpoints. Verified on the
host: `ai-llm-1` runs only `llama-swap-qwen38.service` on **:8082**; there is no
`llama-server.service` at all, while `ai-llm-2` (.45) has one on :8080. The retirement is intentional
— node1's qwen3.6 deployment was disabled 2026-08-15 and the model retired entirely in #524/#525 —
but the Endpoints and the `llm-node1` Service were never cleaned up, leaving a permanent
`TargetDown{job=llm}`.

<!-- codex: “The model retired entirely” conflicts with the supplied evidence: node2:8080 is healthy and litellm-local.yaml still advertises qwen3.6-35b-a3b through both node Services. Treat this as retirement of node1's deployment and preserve node2's route unless a separate model-retirement decision is established. -->

Drop `.44` from the `llm` Endpoints and delete the now-unused `llm-node1` Service/Endpoints pair
(confirm no remaining referent first). Update the stale file header, which still claims node1 and
node2 both run qwen3.6 pinned on :8080.

<!-- codex: A remaining referent is already confirmed: litellm-local-config points at http://llm-node1.ai.svc.cluster.local:8080/v1, so deletion leaves a live gateway configured with an unresolvable backend even though that backend is already dead. Remove only that deployment, preserve the node2 deployment, and include the corresponding consumer audit for litellm.yaml, which the local manifest explicitly requires to stay in sync. -->

<!-- codex: litellm-local reads configuration only at startup and requires its checksum/config annotation to change; updating the ConfigMap alone leaves existing pods using node1. Reconcile and verify consumer rollouts first, then remove the Service/Endpoints in a subsequent Git change and verify pruning, since one Flux reconciliation does not guarantee the required application-level ordering. -->

### W4 — Backup and job hygiene (P2, no downtime)

- **Velero `PartiallyFailed` (09-06, 09-07).** Root cause from the backup log: `failed to get PV ...
  for PVC testpool/dockerlib-env-std-pool-2ds7g: persistentvolumes "..." not found` — an env-pod PVC
  whose PV was already deleted mid-teardown. `testpool` is the ephemeral leasable test-env pool;
  backing it up has no value and races teardown by construction. Exclude the namespace from the
  `velero-daily`/`velero-weekly` schedules. This also stops the chain's last-success timestamp from
  stalling.

<!-- codex: The log establishes one teardown race, not that every resource in testpool has no recovery value or that this is the only cause of stalled success timestamps. Confirm the namespace's recovery contract before excluding it wholesale, preserve existing exclusions, and require successful subsequent backups plus a representative restore check for retained workloads. -->

- **Renovate.** The 09-03 failure was transient (GitHub rate-limited an `ls-remote`) and later runs
  succeed, but the failed Job lingers and keeps `KubeJobFailed` firing. Set `ttlSecondsAfterFinished`
  / tighten `failedJobsHistoryLimit` so a transient failure self-clears. Separately: Renovate targets
  `github.com/cchifor/ailab` — the **read-only mirror** — not the Gitea master forge, and GitHub
  rejected its credential as *unauthenticated*. Repoint at Gitea, or record an explicit decision to
  keep it on the mirror.

<!-- codex: A jobTemplate TTL change applies to newly created Jobs, so explicitly handle the existing failed Job after preserving useful diagnostics; choose retention long enough to investigate real failures. Successful later execution also needs evidence of intended dependency-update behavior, rather than merely disappearance of KubeJobFailed. -->

<!-- codex: Moving Renovate to Gitea is a separate integration change involving platform/endpoint settings, repository selection, token permissions, and duplicate-run prevention; it is not a URL-only hygiene fix. Keep credentials in SOPS+age and verify authentication and repository access without printing secret material. -->

- **Reviewbot quarantines.** Two genuine one-offs, both 2026-09-06 ~20:10, needing a manual requeue:
  `.24` (claude) platform#1095, 5 attempts exhausted on a Claude Max session limit; `.25` (codex)
  platform#1096, `ambiguous POST: Connection reset by peer` (auto-retry correctly refused). Requeue
  both. The claude-side case is a known class — a rate limit should back off past the reset time
  rather than burn the attempt budget; file that as a follow-up rather than widening this change.

<!-- codex: Before requeuing platform#1096, determine whether the ambiguous POST already succeeded and use existing deduplication or idempotency handling to avoid duplicate external actions. Requeue platform#1095 only after its session allowance has reset, and verify successful processing rather than quarantine count alone. -->

- **`ForgeProvisionerSeatEntitlementDrift`.** Three seats (`anthropic/claude-max-1`,
  `anthropic/claude-max-2`, `openai/codex-pro`, all `tenant-zero/playground`, kid
  `tz-213512e80fde7c84`) are granted in the broker kid registry but absent from
  `openbao/capability-kids-configmap`. The provisioner drift-corrects the auth roles every ~30 s and
  the finding immediately returns — a reconciliation loop, not a transient. Either declare the three
  seats or revoke the grants; do not silence the alert.

<!-- codex: Establish the intended authorized entitlements and authoritative writer before choosing between declaration and revocation; declaring seats merely to clear drift can legitimize unintended access. Update the source that competing reconcilers consume and verify sustained agreement using non-secret metadata. -->

### W5 — ai-node2 memory (P1 severity, gated on approval)

`ai-node2` is at **120/124 GiB used, 4 GiB available**, load average 20.7. Guest ceilings total
~184 GiB against 124 GiB physical (talos-cp2 24G + ci-runner-2/5/9/10 at 24G each + dw2 16G + dw5 16G
+ agent-node-2 16G + env-node-1 16G), plus the ai-llm-2 LXC. That is far above PVE's 80 % auto-balloon
threshold, so every balloonable guest is pinned at its floor permanently. QEMU balloon stats:

<!-- codex: Configured ceilings establish overcommit but do not measure resident demand, and two guests' balloon snapshots do not establish that every balloonable guest is permanently at its floor. Collect current allocations, balloon targets, host/LXC overhead, and pressure over time before attributing the whole host's behavior to a fixed threshold. -->

| VM | balloon | total_mem | swapped out | major faults |
|---|---|---|---|---|
| dw2 (4202) | 4096 (floor) | 3.7 GiB | 21.6 GB | 3.9M |
| dw5 (4205) | 6144 (floor) | 5.75 GiB | **140.5 GB** | **39.6M** |

<!-- codex: Swapped-out bytes and major-fault totals are cumulative counters, not current swap occupancy or thrashing rates. Use interval deltas, current guest available memory, swap usage, and memory/I/O pressure to substantiate ongoing distress and compare remediation outcomes across reboots. -->

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

<!-- codex: W5.1 is not low-risk with only 4 GiB available: higher guarantees and guest boot peaks can force neighbouring guests to reclaim or trigger host OOM, including disruption to talos-cp2. Relieve capacity first through the approved W5.2 action, then choose explicit floor values from a host-wide budget with reserved headroom, abort thresholds, and rollback instructions. -->

<!-- codex: Establish the installed PVE version's live-versus-pending balloon behavior before asserting that reboot is mandatory, and distinguish a configured minimum from the current allocation. If restarts are required, drain active dev-worker jobs and restart one guest at a time; keeping the hypervisor running does not establish “no cluster impact.” -->

2. **The actual fix — REQUIRES EXPLICIT APPROVAL BEFORE ANY GUEST IS STOPPED.** Move one guest off
   node2. node1 has 25 GiB free and node3 22 GiB, so there is somewhere to land. Per-node `local-lvm`
   + `cpu: host` means this is a rebuild, not a live migration. Best candidate is one of the two
   out-of-band runners that landed on node2 on 08-25 (ci-runner-9 or ci-runner-10): most recent, most
   stateless, cheapest to recreate. Rebuilding one through tofu also serves W6's import goal.
   **This step must not be executed without per-action sign-off.**

<!-- codex: Snapshot free memory of 25/22 GiB does not prove safe capacity for a runner with a 24 GiB ceiling plus overhead, nor that moving one guest resolves node2's deficit. Budget source and destination peak demand, disk space, and host reserves before selecting the guest and destination. -->

<!-- codex: Local disks and cpu: host do not by themselves prove a rebuild is necessary; check storage-transfer support, CPU compatibility, and other attached resources before choosing migration, offline migration, or rebuild. Proxmox documents migration options for local storage, so this conclusion needs environment-specific evidence ([Proxmox administration guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)). -->

<!-- codex: Rebuilding is replacement, not import: complete the runner module's state reconciliation before any apply or rebuild, then review replacement effects separately. The approved action needs runner job draining, registration and address handoff, a prohibition on simultaneous old/new guests using the same IP, and a tested return path; retain the standing per-action approval requirement for any ai-node outage. -->

### W6 — Bring the out-of-band guests under IaC (P2, follow-up)

Five runners (4106-4110), the env-node (4401), and the two reviewer VMs (4501-4502) exist with no tofu
state. Import them so the module manages what actually exists, then uncomment their entries:

<!-- codex: Reverse this sequence in a controlled local checkout: configure the exact for_each key with the verified address and live settings before importing that instance, with all applies disabled during the transition. OpenTofu requires matching configuration before import; enable only the instance being imported so intermediate plans do not propose creating all remaining guests ([OpenTofu import usage](https://opentofu.org/docs/cli/import/usage/)). -->

```
tofu import 'proxmox_virtual_environment_vm.runner["ci-runner-6"]' ai-node3/4106
```

<!-- codex: Run from the correct module with its pinned provider, confirmed ailab endpoint, active backend/workspace, and provider-verified import ID format; securely back up state and retain locking while excluding concurrent writers. Give env-node and reviewer VMs their own verified resource addresses and state locations, and protect state/backups as sensitive without printing their contents. -->

Reconcile sizing against each live VM before enabling, and re-run `tofu plan` expecting `No changes`.
Do the imports one at a time, verifying `plan` after each. This is what makes the P0 fix permanent
rather than a comment someone eventually deletes.

<!-- codex: Ten proposed additions for five VMs imply five additional resources that VM imports alone do not explain; identify every planned address and decide how its existing object or required creation is handled. Review disks, placement, networking, lifecycle behavior, and dependent resources as well as sizing, and stop on unexpected create, update, destroy, or replacement actions. -->

<!-- codex: Make completion of runner state reconciliation a prerequisite for every subsequent runners-module apply, including W5.2, rather than an optional P2 follow-up; independent W1-W4 changes and the P0 containment can proceed. A no-change plan cannot validate initialization fields hidden by ignore_changes, so separately compare their recorded addresses with live/IPAM facts before treating future replacement as safe. -->

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

<!-- codex: Add kubernetes/apps/apps/ai/litellm-local.yaml as a required W3 change, including its rollout checksum, and identify the matching primary LiteLLM consumer file for implementation review. Update the runners row to reflect configuration-before-import sequencing. -->

## Verification

Per workstream, evidence before assertion:

- **W1:** `count(up{job="ci-runner-node"}) == 10`; `up{job="ci-runner-node",
  instance=~"192.168.0.2[012]:9100"}` returns empty; `gitea_runner_reclaim_last_run_seconds` present
  for all 10; the three `CIRunnerMaintenanceBeaconMissing` alerts clear within one `for: 30m` window.

<!-- codex: count(up) includes failed scrapes, so require exactly the ten expected instances with up=1, their verified nodenames, and reclaim timestamps younger than the configured freshness threshold. Check the live Endpoints/discovery result after Flux reconciles; an alert's for duration governs entry into firing, not necessarily its recovery delay. -->

- **W2:** all three DaemonSets report `desired=7, ready=7, misscheduled=0`;
  `KubeDaemonSetMisScheduled` and `KubeDaemonSetRolloutStuck` clear. Confirm the previously-stranded
  pods are recreated from the DS's current template rather than adopted as-is.

<!-- codex: Make desired=7 conditional on the coverage decision and also check observed generation, updatedNumberScheduled, and each env-node pod's owner/revision. Verify fresh env-node logs, probe output, and backup-agent functionality where backups are intended, since readiness and cleared scheduling alerts do not establish service coverage. -->

- **W3:** `up{job="llm"} == 0` returns empty; `TargetDown` for ns `ai` clears; a `/v1/models` probe
  through LiteLLM still answers for every model it advertises (proves no live route was removed).

<!-- codex: An empty up==0 result also passes when all llm targets disappear; assert the expected surviving scrape targets and up=1 explicitly. /v1/models can return configured inventory while inference is broken, so verify the loaded routing configuration and perform an authenticated minimal inference for each retained local model through each affected gateway, without exposing credentials. -->

- **W4:** next `velero-daily` completes `Completed` with 0 errors; `kubectl -n renovate get jobs` shows
  no lingering `Failed`; both reviewbot personas report `reviewbot_quarantined_recent_jobs == 0`;
  provisioner logs stop emitting `seat-undeclared-grant`.

<!-- codex: Also verify weekly schedule exclusions, successful intended Renovate/reviewbot work, and entitlement agreement across multiple reconciliation cycles. Removing failed Jobs or suppressing the symptoms through disabled processing could satisfy these checks without repairing the underlying behavior. -->

- **W5.1:** `qm monitor <vmid>` `info balloon` shows `actual` at the new floor after reboot;
  `node_memory_SwapFree` recovers and `DevWorkerSwapPressure`/`DevWorkerThrashing` clear for `.9`/`.12`;
  re-check ai-node2 `free -g` to confirm the neighbours did not get squeezed in exchange.

<!-- codex: Balloon actual may legitimately exceed the configured floor, and swap occupancy need not immediately fall after pressure subsides; judge success using sustained available memory, swap/fault rates, pressure, and workload recovery. Observe host and neighbouring guests during representative load, with explicit abort thresholds, rather than relying on a single post-reboot free snapshot. -->

<!-- codex: W5.2 has no verification entry: require the moved runner to accept a representative job, preserve its unique registration and address, remain scraped, and have accurate final state/placement. Confirm sustained source-host relief and destination headroom before retiring rollback resources or declaring the capacity problem resolved. -->

- **W6:** after each import, `tofu plan` in `kubernetes/infra/runners` reports `No changes. Your
  infrastructure matches the configuration.` — the same check that proved the P0 fix was a no-op.

<!-- codex: Verify each imported VM's identity and every associated planned resource, then run a full non-targeted plan with all intended entries enabled in each affected module, including env/reviewer modules. State-only import should cause no guest restart or replacement; record protected state recovery instructions and require manual review before any later apply. -->

Cluster-wide gate before calling this done: `kubectl get kustomization -A` all `True`, alert count down
from 22 to the expected residue (Watchdog + InfoInhibitor + whatever W5.2/QNAP items remain knowingly
open), and etcd still 3/3 in sync.

<!-- codex: Confirm Flux observed the intended Git revision and relevant HelmReleases completed reconciliation; existing True conditions alone can describe an older revision and say nothing about manually applied infra/. Track residual alerts by identity and workstream acceptance criteria, since lowering an aggregate count can hide missing targets and does not establish completion of deferred capacity work. -->

<!-- codex-review-status: complete -->
