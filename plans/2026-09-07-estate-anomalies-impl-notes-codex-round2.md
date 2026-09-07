# Estate anomaly remediation — 2026-09-07

## Codex Review

- The concrete IP/VM mapping, initial no-op plan, explicit disruption gate, and separate storage follow-ups are useful foundations.
- W1 needs an explicit Git/Flux ownership procedure; W2 needs per-DaemonSet placement intent and update-strategy checks before deciding whether any pods need deletion.
- W3 has a concrete dependency: `litellm-local.yaml` still routes to `llm-node1`; neither retirement notes nor `/v1/models` prove the route is unused.
- Complete runner state adoption before W5.2, then relieve and verify host pressure before considering W5.1; higher balloon floors can worsen host OOM risk and harm neighbouring guests.
- W6 misses existing-state recovery and a runner helper that reboots VMs on apply; several health claims and success checks need stronger evidence, functional probes, and rollback criteria. This review checks repository files and quoted evidence, without independently remeasuring the live estate.

## Context

A sweep of ailab + QNAP metrics and logs on 2026-09-07 found **22 firing alerts**. Triage separated
them into four genuine config defects, one real capacity problem, and a tail of stale/one-off noise.
Cluster fundamentals are healthy: etcd 3/3 in sync (leader cp2, term 387, identical raft index, no
errors), all 7 nodes Ready, every Flux kustomization Applied/True, QNAP hardware all-good (5× KC3000,
SMART GOOD, 54-55 °C).
<!-- codex: Attach timestamped query/command outputs and the alert inventory, including the checkout revision and cluster identity; the quoted summaries are snapshots, not proof of sustained cluster health or of the four-defect classification. -->

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
<!-- codex: The plan excerpt proves proposed creates with unsafe addresses, not that those creates would succeed despite occupied VMIDs or cause an actual collision. Moreover, `docs/network-plan.md` describes the September 3 Talos incident as stale `ipconfig0` reapplied on reboot, which is a different trigger from an un-imported create. -->

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
<!-- codex: The workstreams are coupled: W2 and W4 edit the same Velero HelmRelease, and the `apps` Flux Kustomization depends on `infrastructure` being Ready. Template changes roll pods and Service deletion changes routing, so W1-W4 cannot collectively promise zero downtime merely because their edits are configuration. -->

### W1 — Stop scraping the wrong three machines (P1, no downtime)

`kubernetes/apps/infrastructure/monitoring/ci-runners-node.yaml` lists `.20/.21/.22` in its
`Endpoints`. Two consequences: **ci-runner-7/8/9 have zero alerting** (no disk-full, no memory, no
swap, no reclaim-beacon — the exact suite that exists to catch the failures behind ailab#249 and the
runner-reclaim outage), and every `job="ci-runner-node"` rule is evaluated against cloudlab
hypervisors, so a real cloudlab problem would page as a CI-runner problem.
<!-- codex: This establishes missing coverage in this scrape job, not necessarily zero alerting across every monitoring system; qualify that claim unless other discovery paths were checked. -->

Replace `.20/.21/.22` with `.29/.30/.31`. Final set: `.14-.19`, `.23`, `.29-.31` (10 targets, all
genuinely CI runners). Probed 2026-09-07: all three of `.29/.30/.31` serve `:9100` (HTTP 200), are
healthy (~70 % root used, 55-63 GiB free), and **do** emit fresh
`gitea_runner_reclaim_last_run_seconds` — so the three `CIRunnerMaintenanceBeaconMissing` alerts are
pure false positives from the wrong hosts and will clear once the target list is corrected.
<!-- codex: Commit the full manifest change to Flux's Gitea source and reconcile `flux-system/infrastructure`; a live-only edit will be reverted by kustomize-controller. Inspect `managedFields` and preserve the complete atomic `subsets` list, since partial SSA or a competing manager can replace unrelated addresses or conflict. [Flux apply policy](https://fluxcd.io/flux/components/kustomize/kustomizations/#override), [Endpoints schema](https://github.com/kubernetes/api/blob/master/core/v1/types.go). -->
<!-- codex: Keep the Endpoints object's kind/name/namespace and inventory membership unchanged: `infrastructure` has `prune: true`, so removing or renaming its manifest can delete the existing object, independently of field ownership. Review the rendered diff for exactly three address substitutions with ports and labels retained, and confirm the live result survives a subsequent Flux reconciliation. -->

Add a guard so this cannot silently recur: an alert that fires when any `ci-runner-node` target
matches `node_uname_info{release=~".*-pve"}` — a Proxmox host has no business in this job.
<!-- codex: Scope the guard explicitly to `job="ci-runner-node"` so legitimate Proxmox scrapes do not trigger it, and validate its firing/non-firing cases. A kernel suffix only catches Proxmox misidentification; it cannot guarantee that missing, duplicate, or wrong non-Proxmox targets never recur. -->

### W2 — Cover the env-node with the infra DaemonSets (P1, no downtime)

`talos-env-node-1` joined 2026-09-01 carrying `dedicated=env:NoSchedule`. Three DaemonSets tolerate
only `dedicated=agent`, so each reports `desired=6, misscheduled=1` with a pod stranded on the
env-node that the controller will not manage:
<!-- codex: `NoSchedule` can leave existing DaemonSet pods running and controller-owned while making the node ineligible for new placement; `misscheduled=1` does not prove the pod is unmanaged or its service is broken. Check pod owner-reference UIDs, readiness, revision, and events before prescribing deletion. [DaemonSet controller eligibility logic](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/daemon/daemon_controller.go). -->

- `monitoring/alloy` — log/metric shipping
- `monitoring/storage-fabric-probe` — storage probing
- `velero/node-agent` — **filesystem backup coverage**

Seven other DaemonSets (cilium, cilium-envoy, csi-nfs-node, node-exporter, iscsi-recovery-tmo,
trident-node-linux) already show `desired=7`, which is the evidence that covering the env-node is the
intended behaviour and these three were simply missed.
<!-- codex: The parenthesis lists six DaemonSets, not seven; provide the missing name or correct the inventory. -->
<!-- codex: Other DaemonSets' coverage does not establish placement policy for these three: confirm logging/probing requirements and whether Velero has any retained backup workload on this node after W4 excludes `testpool`. If a daemon is intentionally excluded, removing its stale pod is appropriate; if coverage is required, deletion alone leaves a lasting gap because the replacement remains ineligible. -->

Add a `dedicated=env` toleration to each. Prefer `operator: Exists` on key `dedicated` where the
chart allows it, so the next dedicated node class doesn't reopen this. Clears 6 of the 22 alerts
(`KubeDaemonSetMisScheduled` ×3, `KubeDaemonSetRolloutStuck` ×3).
<!-- codex: Prefer explicit `Equal`, `value: env`, `effect: NoSchedule` unless each daemon is deliberately approved for every dedicated node class; `Exists` broadens future placement and host-data access, especially for Velero's hostPath mounts. Preserve the existing tolerations and inspect the rendered chart values rather than assuming a values key is effective. -->
<!-- codex: Update the DaemonSet pod template through its Git/Helm owner: with `RollingUpdate`, controller-owned old pods should be replaced automatically once the node is eligible, whereas `OnDelete` requires selective deletion to get the new template. Check the actual strategy, rollout limits, available resources, and stuck-pod events first; manual deletion is a fallback after convergence, not an unconditional step. [DaemonSet updates](https://kubernetes.io/docs/tasks/manage-daemon/update-daemon-set/). -->

### W3 — Retire the dead `llm` endpoint (P1, no downtime)

`kubernetes/apps/apps/ai/llm-service.yaml` still lists `192.168.0.44:8080` in both the `llm`
Endpoints (which drives the ServiceMonitor scrape) and the `llm-node1` Endpoints. Verified on the
host: `ai-llm-1` runs only `llama-swap-qwen38.service` on **:8082**; there is no
`llama-server.service` at all, while `ai-llm-2` (.45) has one on :8080. The retirement is intentional
— node1's qwen3.6 deployment was disabled 2026-08-15 and the model retired entirely in #524/#525 —
but the Endpoints and the `llm-node1` Service were never cleaned up, leaving a permanent
`TargetDown{job=llm}`.
<!-- codex: Absence of one systemd unit does not establish absence of any listener on :8080; retain socket/process and direct backend probe evidence. The retirement chronology also conflicts with tracked configuration: `litellm-local.yaml` still advertises qwen3.6, while `llm-service.yaml` says node1's qwen3.8 :8082 instance was retired, so reconcile live state, deployed revision, and retirement records before treating intent as established. -->

Drop `.44` from the `llm` Endpoints and delete the now-unused `llm-node1` Service/Endpoints pair
(confirm no remaining referent first). Update the stale file header, which still claims node1 and
node2 both run qwen3.6 pinned on :8080.
<!-- codex: `kubernetes/apps/apps/ai/litellm-local.yaml:33` contains `api_base: http://llm-node1.ai.svc.cluster.local:8080/v1`, and that file is included in the AI kustomization, directly contradicting 'now-unused'. Resolve this consumer and its intended model behavior first, recompute its documented `checksum/config`, and verify the running gateway has loaded the new configuration before deleting the Service. -->
<!-- codex: Make 'confirm no remaining referent' a gate covering rendered/live ConfigMaps, environment/Secret references, other repositories, and off-cluster clients using short DNS names, FQDNs, or the Service ClusterIP. Repository search and a quiet traffic sample cannot prove no intermittent or hardcoded client exists; if ownership is unresolved, defer Service deletion while correcting the independently verified scrape target. -->
<!-- codex: Delete through Git only after consumers have converged: `apps` has `prune: true`, and live deletion while Git still declares the pair lets Flux recreate it. A single reconcile containing both consumer updates and deletions is not proof of a safe application cutover; record a rollback that also accounts for possible ClusterIP changes. [Flux pruning](https://fluxcd.io/flux/components/kustomize/kustomizations/#prune). -->

### W4 — Backup and job hygiene (P2, no downtime)

- **Velero `PartiallyFailed` (09-06, 09-07).** Root cause from the backup log: `failed to get PV ...
  for PVC testpool/dockerlib-env-std-pool-2ds7g: persistentvolumes "..." not found` — an env-pod PVC
  whose PV was already deleted mid-teardown. `testpool` is the ephemeral leasable test-env pool;
  backing it up has no value and races teardown by construction. Exclude the namespace from the
  `velero-daily`/`velero-weekly` schedules. This also stops the chain's last-success timestamp from
  stalling.
  <!-- codex: One missing-PV error does not prove it is the sole cause of both partial failures, nor does an ephemeral namespace imply that every object in it is disposable. Inventory `testpool` data/configuration and its rebuild path, inspect all backup errors, and preserve the existing exclusions and filesystem-backup settings when appending this namespace. -->
- **Renovate.** The 09-03 failure was transient (GitHub rate-limited an `ls-remote`) and later runs
  succeed, but the failed Job lingers and keeps `KubeJobFailed` firing. Set `ttlSecondsAfterFinished`
  / tighten `failedJobsHistoryLimit` so a transient failure self-clears. Separately: Renovate targets
  `github.com/cchifor/ailab` — the **read-only mirror** — not the Gitea master forge, and GitHub
  rejected its credential as *unauthenticated*. Repoint at Gitea, or record an explicit decision to
  keep it on the mirror.
  <!-- codex: Set a concrete TTL under `jobTemplate.spec` and address the existing failed Job separately, because changing the CronJob template does not retrofit its Jobs. A smaller nonzero `failedJobsHistoryLimit` can still retain the last failure indefinitely; preserve enough logs and failure visibility before cleanup. [Job TTL behavior](https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/). -->
  <!-- codex: Repointing Renovate is a forge integration change beyond stale-Job cleanup: validate platform/endpoint, least-privilege Gitea credentials, existing PR handling, and a successful dry run separately. An unauthenticated GitHub request may concern dependency metadata rather than repository access, so correlate the exact failing request before declaring the credential or forge target to be the cause. -->
- **Reviewbot quarantines.** Two genuine one-offs, both 2026-09-06 ~20:10, needing a manual requeue:
  `.24` (claude) platform#1095, 5 attempts exhausted on a Claude Max session limit; `.25` (codex)
  platform#1096, `ambiguous POST: Connection reset by peer` (auto-retry correctly refused). Requeue
  both. The claude-side case is a known class — a rate limit should back off past the reset time
  rather than burn the attempt budget; file that as a follow-up rather than widening this change.
  <!-- codex: For the ambiguous POST, inspect the exact PR head and existing review/deduplication marker before requeueing; the original POST may have succeeded despite the connection reset. `reviewbot.py` deliberately requires `--force` for this case, so do not bypass that guard merely to clear a quarantine metric. -->
  <!-- codex: The tracked `reviewbot.py` already contains subscription-rate-limit handling; compare the deployed version and incident timestamps before opening a duplicate implementation follow-up. Confirm the subscription reset has passed and the PR remains actionable before retrying the Claude job. -->
- **`ForgeProvisionerSeatEntitlementDrift`.** Three seats (`anthropic/claude-max-1`,
  `anthropic/claude-max-2`, `openai/codex-pro`, all `tenant-zero/playground`, kid
  `tz-213512e80fde7c84`) are granted in the broker kid registry but absent from
  `openbao/capability-kids-configmap`. The provisioner drift-corrects the auth roles every ~30 s and
  the finding immediately returns — a reconciliation loop, not a transient. Either declare the three
  seats or revoke the grants; do not silence the alert.
  <!-- codex: A grant's presence in the broker registry is not evidence that the entitlement is authorized; reconcile it against the intended tenant/seat policy before declaring access. Identify the authoritative issuer and reload path so revocation is not immediately recreated and declaration does not silently expand credential access. -->

### W5 — ai-node2 memory (P1 severity, gated on approval)

`ai-node2` is at **120/124 GiB used, 4 GiB available**, load average 20.7. Guest ceilings total
~184 GiB against 124 GiB physical (talos-cp2 24G + ci-runner-2/5/9/10 at 24G each + dw2 16G + dw5 16G
+ agent-node-2 16G + env-node-1 16G), plus the ai-llm-2 LXC. That is far above PVE's 80 % auto-balloon
threshold, so every balloonable guest is pinned at its floor permanently. QEMU balloon stats:
<!-- codex: The 80% policy concerns actual host usage, not the sum of guest ceilings, and the configured balloon target must be checked on this PVE version. Two guest snapshots do not establish that every guest is permanently at its floor; record host/guest usage over representative load and include LXC/GPU allocations. [Proxmox balloon target](https://pve.proxmox.com/pve-docs-9-beta/pve-admin-guide.pdf). -->

| VM | balloon | total_mem | swapped out | major faults |
|---|---|---|---|---|
| dw2 (4202) | 4096 (floor) | 3.7 GiB | 21.6 GB | 3.9M |
| dw5 (4205) | 6144 (floor) | 5.75 GiB | **140.5 GB** | **39.6M** |
<!-- codex: Swap-out bytes and major-fault counts are cumulative statistics, not current swap occupancy or fault rates; a large lifetime total alone does not prove ongoing thrashing or that the current floor is insufficient. Capture counter deltas/rates, guest uptime, fresh balloon-stat timestamps, memory PSI, and available memory before drawing that conclusion. [QEMU balloon statistics](https://qemu-project.gitlab.io/qemu/interop/virtio-balloon-stats.html). -->

This failure mode is already documented in `dev-workers/variables.tf` — but for **ai-node1**, measured
2026-08-12 at 88 %, mitigated by 12 GiB floors on dw1/dw4. The problem has since **moved**: node1 now
has 25 GiB free (its LLM no longer pins qwen3.6) while node2 has gone 93 % → 97 %. dw5's 6144 override
(added after the 2026-09-01 swap-death incident) is demonstrably insufficient, and **dw2 was never
given an override at all**. Pressure grew when ci-runner-9 + ci-runner-10 started on node2 on
2026-08-25 23:02 (+48 GiB of ceilings), then env-node-1 (09-01) and agent-node-2 (09-02).
<!-- codex: The start dates and added ceilings are plausible contributors, but they do not quantify current resident demand or prove that the problem 'moved' from node1. Compare measured guest/LXC consumption and workload changes over the cited periods before assigning causality. -->

Two stages, the second gated:
<!-- codex: Reverse the operational order: complete the approved W5.2 capacity relief and verify source/destination headroom before W5.1 raises any floors. At 4 GiB available, protecting more guest RAM can force neighbouring guests into reclaim or cause host OOM affecting talos-cp2; if the move is awaiting approval, that does not make the floor increase safe to proceed. -->

1. **Immediate, low-risk.** Raise dw2's floor from the uniform 4096 and dw5's from 6144, codified in
   `dev_worker_nodes`. `qm set --balloon` cannot inflate a running guest, so this needs a reboot of
   those two dev-workers to take effect — dev-workers, not ai-nodes, so no cluster impact. Note this
   only redistributes scarcity on a host that is already oversubscribed: it buys headroom, it does
   not fix the ratio, and raising floors on a starved host can push pressure onto its neighbours.
   <!-- codex: The proposed new floors are unspecified, so the risk cannot be calculated: state exact MiB values and the combined increase, budget all guest floors plus non-balloonable memory and host/LXC reserve, and define abort thresholds. Reassess whether higher floors are still necessary after capacity relief rather than inheriting node1's 12 GiB workaround. -->
   <!-- codex: The mandatory-reboot conclusion does not follow from `qm set` alone failing to resize a running guest: `docs/runbooks/dev-workers.md:80` and `dev-workers/variables.tf:197` document successful live floor adjustment plus a monitor balloon command. Verify the supported mechanism, units, pending configuration, and persistence on the installed PVE version before scheduling disruption. -->
   <!-- codex: 'No cluster impact' is unsupported: dev-worker reboots interrupt active sessions/jobs, and reclaim on their shared host can affect Kubernetes guests. Quiesce affected workloads and define recovery/rollback before any reboot or memory redistribution. -->
2. **The actual fix — REQUIRES EXPLICIT APPROVAL BEFORE ANY GUEST IS STOPPED.** Move one guest off
   node2. node1 has 25 GiB free and node3 22 GiB, so there is somewhere to land. Per-node `local-lvm`
   + `cpu: host` means this is a rebuild, not a live migration. Best candidate is one of the two
   out-of-band runners that landed on node2 on 08-25 (ci-runner-9 or ci-runner-10): most recent, most
   stateless, cheapest to recreate. Rebuilding one through tofu also serves W6's import goal.
   **This step must not be executed without per-action sign-off.**
   <!-- codex: Snapshot free RAM does not prove a safe landing zone or that moving one runner fixes balloon starvation: at the stated 80% target, 120 GiB used must fall below about 99.2 GiB before adding margin. Measure the candidate's actual resident memory released, destination peak demand, boot-time allocation, LLM load, and storage capacity; its 24 GiB ceiling is not the amount necessarily freed. -->
   <!-- codex: `local-lvm` and `cpu: host` do not by themselves require a rebuild: Proxmox supports migration with local disks, and CPU compatibility, passthrough devices, snapshots, storage/network mappings, and the installed provider determine the options. Assess offline migration as well as compatible live migration, then review the actual tofu action instead of assuming a node-name change recreates the VM. [Proxmox migration options](https://pve.proxmox.com/pve-docs-9-beta/pve-admin-guide.pdf), [pinned provider VM behavior](https://github.com/bpg/terraform-provider-proxmox/blob/v0.111.0/docs/resources/virtual_environment_vm.md). -->
   <!-- codex: If rebuilding is chosen, drain/disable runner job acceptance, confirm no unique workspace data, preserve a recoverable disk/configuration copy, and verify registration, labels, secrets, IP/VMID uniqueness, and a real CI job before retiring the old instance. Specify the exact candidate, destination, expected outage, and rollback in the per-action approval. -->

### W6 — Bring the out-of-band guests under IaC (P2, follow-up)
<!-- codex: Runner state recovery/import and a safe baseline for the whole runners module must precede any other functional changes or applies to that module, especially W5.2's move. Allow only configuration preparation needed for adoption during this phase; recreating an unmanaged runner is not an import, and combining adoption with relocation obscures unintended changes. -->

Five runners (4106-4110), the env-node (4401), and the two reviewer VMs (4501-4502) exist with no tofu
state. Import them so the module manages what actually exists, then uncomment their entries:
<!-- codex: `env-pool/backend.tf` and `reviewers/backend.tf` say these modules were applied from scratchpad clones and their authoritative local state needs handover. Locate and validate that state before importing into a second state; absence from the current checkout is not evidence that no state exists. -->
<!-- codex: Freeze concurrent applies and identify the authoritative root directory, backend/workspace, tfvars, provider lockfile, PVE endpoint, and VM identity; take a protected state backup before each import and retain normal locking. These are local backends, so a lock in one worktree does not protect another state copy, and state/plan files may contain credentials. -->
<!-- codex: The sequence is reversed for a `for_each` instance: first add the corrected entry to the effective configuration, then import immediately without an intervening apply; a commented-out key is not a configured import target. Keep each successfully imported entry enabled, since removing it afterwards can plan destruction. [OpenTofu import usage](https://opentofu.org/docs/cli/import/usage/). -->

```
tofu import 'proxmox_virtual_environment_vm.runner["ci-runner-6"]' ai-node3/4106
```

Reconcile sizing against each live VM before enabling, and re-run `tofu plan` expecting `No changes`.
Do the imports one at a time, verifying `plan` after each. This is what makes the P0 fix permanent
rather than a comment someone eventually deletes.
<!-- codex: Importing the VM alone cannot yield the stated no-op: `runners/guest-agent.tf` creates `terraform_data.enable_guest_agent` for each enabled key, whose create provisioner enables the agent and sends a reboot request. Recover existing helper state or explicitly make adoption bypass those create-time side effects, then inspect the entire plan; never apply ancillary creates merely to make the next plan quiet. -->
<!-- codex: Match all live VM settings, not just sizing, and account for the runners module's shared CPU/memory/disk defaults so reconciling one import does not resize existing runners. Retain a no-create/no-update/no-destroy baseline before separately reviewing any intended configuration drift. -->
<!-- codex: Env-pool and reviewers are separate root modules with additional resources: env-pool includes Talos configuration application and Kubernetes labels/taints, while reviewers includes an image resource. Inventory and recover/import those dependencies as appropriate rather than applying the non-VM remainder blindly; the runner example is not a complete adoption procedure for all eight guests. -->

### Out of scope (recorded, not fixed here)

- **QNAP orphaned LUNs.** Two PVs (`pvc-c61ddeac…`, `pvc-de79ab3b…`, 48Gi each, `Delete` policy) retry
  `Unmap IscsiTarget fail … Error Code : -19` roughly every 90 s; three more sit `Released`/`Retain`.
  Known pattern, needs QNAP-side LUN cleanup — a storage runbook task, not a config change.
  <!-- codex: The unmap error and PV phase do not establish that the backing LUN is orphaned or safe to delete; `Released`/`Retain` can deliberately preserve recovery data. Any follow-up cleanup needs exact PV-to-LUN mapping, attachment/session checks, and retention/backup evidence. -->
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
<!-- codex: Use `kubectl --context admin@ai` consistently: `CLAUDE.md` warns that the default context is a different k3s cluster. Record source/applied revision and live generations, render/diff the changes before rollout, and define rollback triggers and recovery steps for each workstream. -->

- **W1:** `count(up{job="ci-runner-node"}) == 10`; `up{job="ci-runner-node",
  instance=~"192.168.0.2[012]:9100"}` returns empty; `gitea_runner_reclaim_last_run_seconds` present
  for all 10; the three `CIRunnerMaintenanceBeaconMissing` alerts clear within one `for: 30m` window.
  <!-- codex: `count(up) == 10` also passes with failed or incorrectly identified targets; require all ten `up` values to equal 1, exact expected instance membership, and ten distinct expected runner nodenames. Scope the beacon check to this job, verify its value is plausibly fresh for its execution cadence, and repeat after Flux reconciles the intended revision. -->
  <!-- codex: `for: 30m` delays firing, not resolution; without `keep_firing_for`, the alert resolves when its expression stops returning that instance, subject to scrape/discovery/evaluation and notification timing. Use those intervals for the expected clearing bound. [Prometheus alert lifecycle](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/). -->
- **W2:** all three DaemonSets report `desired=7, ready=7, misscheduled=0`;
  `KubeDaemonSetMisScheduled` and `KubeDaemonSetRolloutStuck` clear. Confirm the previously-stranded
  pods are recreated from the DS's current template rather than adopted as-is.
  <!-- codex: Also require the current generation to be observed and `updatedNumberScheduled` to match desired, with pod ownership/revisions checked; ready counts alone can describe old pods. Verify env-node logs arrive, probe results are fresh, and Velero completes a retained volume backup where coverage is intended, avoiding an active backup during node-agent replacement. -->
- **W3:** `up{job="llm"} == 0` returns empty; `TargetDown` for ns `ai` clears; a `/v1/models` probe
  through LiteLLM still answers for every model it advertises (proves no live route was removed).
  <!-- codex: An empty `up{job="llm"} == 0` result also occurs when the job disappears entirely; assert the expected remaining target identities/count and successful scrapes, plus removal of the obsolete Endpoints/EndpointSlices. -->
  <!-- codex: `/v1/models` can list configured aliases without contacting their inference backends, so it does not prove routing works or that nobody used the deleted Service. Compare against the pre-change intended model inventory and exercise small inference requests through both `litellm` and `litellm-local`, checking backend selection and errors. -->
- **W4:** next `velero-daily` completes `Completed` with 0 errors; `kubectl -n renovate get jobs` shows
  no lingering `Failed`; both reviewbot personas report `reviewbot_quarantined_recent_jobs == 0`;
  provisioner logs stop emitting `seat-undeclared-grant`.
  <!-- codex: Check both rendered schedules and backups produced from them, their expected included volumes, and an isolated representative restore; `Completed` alone can conceal accidentally excluded data. The existing partial-failure alert uses a 24-hour lookback, so one successful backup need not immediately clear it. -->
  <!-- codex: A zero quarantine gauge can result from requeueing or expiry without a successful review, and quiet provisioner logs can result from a stopped or unscraped controller. Verify the specific review outcomes and effective intended entitlements, controller health, and continued metrics, rather than only absence of symptoms. -->
- **W5.1:** `qm monitor <vmid>` `info balloon` shows `actual` at the new floor after reboot;
  `node_memory_SwapFree` recovers and `DevWorkerSwapPressure`/`DevWorkerThrashing` clear for `.9`/`.12`;
  re-check ai-node2 `free -g` to confirm the neighbours did not get squeezed in exchange.
  <!-- codex: The balloon floor is a lower bound, not a requirement that `actual` equal it; verify current allocation lies within the configured range and the setting persists. Use the actual exported metric name, normally `node_memory_SwapFree_bytes`, and judge sustained swap-in/out, fault rates, PSI, and workload responsiveness rather than a reboot's temporary swap/counter reset. -->
  <!-- codex: One host `free` sample cannot show that neighbours escaped pressure; observe host and per-guest memory, OOM logs, and cp2/etcd health over representative CI/backup load. Add explicit W5.2 checks for placement, source/destination headroom, unique runner identity, a successful CI job, monitoring continuity, and a post-move no-op plan. -->
- **W6:** after each import, `tofu plan` in `kubernetes/infra/runners` reports `No changes. Your
  infrastructure matches the configuration.` — the same check that proved the P0 fix was a no-op.
  <!-- codex: Run the full baseline plan in each affected root/state, not only `runners`, and reject unexpected creates, replacements, updates, or destroys instead of applying them to obtain a later no-op. Because `initialization` is ignored, independently compare declared IPs, live guest networking, and Proxmox `ipconfig0`/cloud-init metadata; a no-op plan cannot verify those addresses. -->

Cluster-wide gate before calling this done: `kubectl get kustomization -A` all `True`, alert count down
from 22 to the expected residue (Watchdog + InfoInhibitor + whatever W5.2/QNAP items remain knowingly
open), and etcd still 3/3 in sync.
<!-- codex: Require positive scrape/controller health and readiness at the intended revision, since losing metrics can reduce the alert count without repairing anything. List accepted remaining alerts with owners and follow-up criteria explicitly; an unexecuted W5.2 leaves the P1 capacity problem open and should not be reported as remediated. -->

<!-- codex-review-status: complete -->