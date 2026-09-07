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

The 10 additions are **5 VMs + 5 `terraform_data.enable_guest_agent` resources** — importing the VMs
alone does not account for the other five. W6 handles both.

Contained in commit `7ab78e79` (this branch): entries commented out with corrected addresses recorded,
verified `No changes` afterwards. **Containment only protects operators working from this commit.**
Two consequences: land it on `main` promptly, and treat "no runners-module apply from any checkout
predating it" as an operating rule until W6 completes. Flux does not apply `infra/`, so merging the
containment provisions nothing by itself. Any rollback of `7ab78e79` must preserve both the corrected
addresses and the disabled unmanaged entries.

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

Six workstreams. They are **not** uniformly independent or zero-downtime, and the sequencing below is
load-bearing:

- **W1** is a self-contained `apps/` change, auto-reconciled by Flux. Genuinely no downtime.
- **W2** triggers a DaemonSet rolling update — brief per-node agent gaps.
- **W3** requires a consumer rollout *before* any Service deletion, so it spans two commits.
- **W4** mixes `apps/` changes with **operational actions** (reviewbot requeues, entitlement decisions)
  that are not simply "config".
- **W5** is capacity work on a Proxmox host; **W5.2 requires explicit per-action approval**, and
  **W5.1 now follows W5.2** (see below for why the original ordering was wrong).
- **W6** is manual OpenTofu state work and is a **prerequisite for any future runners-module apply,
  including W5.2** — not an optional follow-up.

W1-W4 can proceed in parallel with W6; only W5 is gated. Keep automatically-reconciled `apps/` commits
separate from hand-applied `infra/` work so rollback boundaries stay clean.

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
false positives from the wrong hosts and should clear once the target list is corrected.

Mechanics that matter:

- The Service is **selectorless**, so the `Endpoints` object is a plain manifest and an in-place Git
  edit is the right tool. `subsets` is an **atomic list**: ownership covers the whole list, so submit
  the complete intended value (all addresses *and* `ports`) rather than assuming a partial merge.
  Live `managedFields` confirms `kustomize-controller` owns `f:subsets` under the `infrastructure`
  Kustomization — verify that still holds before applying.
- Keep the object's **name, namespace, and owning Kustomization unchanged**. Removing addresses is a
  field update; renaming or moving it between inventories invites Flux pruning.
- The mirrored `EndpointSlice` is owned by the `Endpoints` object (verified). Let it converge on its
  own — do not hand-edit controller-owned slices, and do not fold an EndpointSlice migration into
  this fix.

Add a recurrence guard, scoped explicitly to `job="ci-runner-node"`: alert when a target matches
`node_uname_info{release=~".*-pve"}`. This is a narrow tripwire, not a completeness check — it cannot
catch a wrong *Ubuntu* host, a missing target, or a missing `node_uname_info` series. Pair it with an
assertion that the scraped `nodename` set equals the expected `ci-runner-*` identities, and keep the
existing target-availability alert.

### W2 — Cover the env-node with the infra DaemonSets (P1, brief rolling update)

`talos-env-node-1` joined 2026-09-01 carrying `dedicated=env:NoSchedule`. Three DaemonSets tolerate
only `dedicated=agent`, so each reports `desired=6, misscheduled=1`.

**Correcting an overstatement in the first draft:** `NoSchedule` does not evict a running pod, and
`misscheduled` does not mean the agent is broken. Verified on 2026-09-07: all three stranded pods are
`Ready`, owned by their DaemonSet (`controller: true`), and on the DS's **current** revision
(`alloy-zhhw8` = `5ffcfc5bf6`, same as its six siblings). The `velero/node-agent` pod on env-node has
completed **18 PodVolumeBackups**. So these agents are *working* — the defect is that they are
**unmanaged**: the controller will not roll them on a chart upgrade, and if one is deleted it will not
be recreated (`desired=6` excludes that node), silently losing coverage.

That settles the coverage question in favour of tolerating rather than deleting: removing the pods
would actively remove working log collection, storage probing, and backup coverage from env-node.
Each agent still needs its own justification, not an argument by analogy:

- `monitoring/alloy` — its manifest already states a node log collector "must cover ALL nodes, else
  agent-node/Kata-sandbox logs are silently uncollected". env-node runs test-env workloads; same
  mandate applies.
- `monitoring/storage-fabric-probe` — env-node consumes `testpool-iscsi` PVs from the same QNAP
  fabric, so per-node reachability probing is meaningful there.
- `velero/node-agent` — env-node has real PVs and has already produced 18 PVBs. Note the tension with
  W4: W4 excludes the `testpool` *namespace* from backup schedules, but the node agent must remain for
  any non-testpool volume that lands on that node. If a decision is made that env-node should hold
  **no** backed-up volumes, the correct action is to drop the agent and remove its stale pod, not to
  tolerate the taint. Resolve this explicitly before implementing.

Use an explicit `{ key: dedicated, operator: Equal, value: env, effect: NoSchedule }` entry alongside
the existing `agent` one. Avoid `operator: Exists` on the key: it would silently extend these agents
to every future dedicated node class, which is a placement-policy expansion needing its own
justification rather than a preventive default.

For the six DaemonSets that already show `desired=7` (cilium, cilium-envoy, csi-nfs-node,
node-exporter, iscsi-recovery-tmo, trident-node-linux) this is a coverage data point, not proof of
intended policy — hence the per-agent justification above. *(First draft said "seven other
DaemonSets" and listed six; the live count is six.)*

Check each rendered DaemonSet's `updateStrategy` before assuming stale pods are replaced. `alloy` is
`RollingUpdate` with `maxUnavailable: 1, maxSurge: 0` (verified); confirm the other two. Under
`RollingUpdate` the template change replaces pods automatically — wait for a normal rollout rather than
pre-emptively deleting. Under `OnDelete`, stale pods need individual manual deletion after the new
template reconciles. Changing scheduling eligibility can zero `misscheduled` **without** proving any
pod was replaced, so verify replacement directly.

### W3 — Retire node1's LLM route (P1, two-commit sequence)

`kubernetes/apps/apps/ai/llm-service.yaml` lists `192.168.0.44:8080` in both the `llm` Endpoints
(which drives the ServiceMonitor scrape) and the `llm-node1` Endpoints. Verified on the host:
`ai-llm-1` runs only `llama-swap-qwen38.service` on **:8082**; there is no `llama-server.service`,
while `ai-llm-2` (.45) has one on :8080. Result: a permanent `TargetDown{job=llm}`.

**Scope correction:** this is the retirement of **node1's deployment**, not of the model. `qwen3.6-35b-a3b`
was removed from the *main* `litellm.yaml` in #524/#525, but it is **still served on node2** and still
advertised by `litellm-local.yaml` to the agentforge workers. Do not remove node2's route.

**Confirmed live consumer.** `kubernetes/apps/apps/ai/litellm-local.yaml:33` registers
`qwen3.6-35b-a3b` against **both** node Services. Proven from inside the cluster on 2026-09-07:

```
llm-node1.ai.svc.cluster.local:8080 -> 000   (dead)
llm-node2.ai.svc.cluster.local:8080 -> 200
```

LiteLLM round-robins across a model's deployments, so agentforge workers requesting that model land on
a dead backend roughly half the time — the identical failure the main proxy was fixed for on
2026-08-15. Currently latent (litellm-local's log shows only health probes), but it is a live
misconfiguration, not merely cosmetic.

Ordering is load-bearing, because **litellm-local reads its config only at startup** (its pod is 23
days old):

1. **Commit 1 — remove the node1 deployment from `litellm-local.yaml`**, keeping the node2 deployment.
   Ensure the Deployment carries a config checksum/annotation that changes with the ConfigMap so Flux
   actually rolls the pod; editing the ConfigMap alone leaves the running pod using node1. Audit
   `litellm.yaml` in the same pass — the local manifest explicitly requires the two to stay in sync.
2. **Verify** the rollout: new pod, and its loaded routing config no longer contains `llm-node1`.
3. **Commit 2 — remove `.44` from the `llm` Endpoints and delete the `llm-node1` Service/Endpoints
   pair**, then verify Flux pruned them. One reconciliation does not guarantee application-level
   ordering, so these must be separate changes.

Also update the stale file header, which still claims node1 and node2 both run qwen3.6 pinned on :8080.

### W4 — Backup and job hygiene (P2, config + operational actions)

- **Velero `PartiallyFailed` (09-06, 09-07).** From the backup log: `failed to get PV ... for PVC
  testpool/dockerlib-env-std-pool-2ds7g: persistentvolumes "..." not found` — an env-pod PVC whose PV
  was already deleted mid-teardown. `testpool` is the ephemeral leasable test-env pool. This log line
  establishes **one** teardown race, not that every `testpool` resource is disposable nor that this is
  the only cause of a stalled success timestamp. Confirm the namespace's recovery contract before
  excluding it wholesale; then add `testpool` to the existing `excludedNamespaces` on **both** the
  daily and weekly schedules (`helmrelease.yaml:159` and `:168`), preserving the current
  `[kube-system, velero, trivy-system]` entries. See the W2 note on `velero/node-agent`.
- **Renovate.** The 09-03 failure was transient (GitHub rate-limited an `ls-remote`); later runs
  succeed, but the failed Job lingers (`failedJobsHistoryLimit: 3`, no TTL) and keeps `KubeJobFailed`
  firing. Add `ttlSecondsAfterFinished` to the jobTemplate — noting it applies only to **newly
  created** Jobs, so the existing failed Job must be removed explicitly after capturing its logs.
  Choose a retention long enough to investigate genuine failures. Verify Renovate is doing its actual
  job (opening dependency PRs), not merely that the alert stopped.
- **Renovate forge target — split out as its own change.** `config-configmap.yaml:11-12` sets
  `platform: 'github'` / `endpoint: 'https://api.github.com/'`, i.e. it targets the **read-only
  mirror**, and GitHub rejected its credential as *unauthenticated*. Repointing to Gitea is a real
  integration change — platform/endpoint settings, repository selection, token permissions, and
  preventing duplicate runs across both forges — not a URL edit. Do it separately, keep credentials in
  SOPS+age, and verify auth and repo access without printing secret material.
- **Reviewbot quarantines.** Two one-offs from 2026-09-06 ~20:10. **Do not blind-requeue.**
  - `.25` (codex) platform#1096 — `ambiguous POST: Connection reset by peer`. "Ambiguous" means the
    review may already have posted. Check whether the POST succeeded and rely on the tool's pre-post
    marker/idempotency check before requeuing, to avoid a duplicate external review.
  - `.24` (claude) platform#1095 — attempts exhausted against a Claude Max session limit. Requeue only
    after the allowance has reset, and confirm the review actually completes.
  - Verify by successful processing, not by `reviewbot_quarantined_recent_jobs` reaching 0.
  - Follow-up (separate): a rate limit should back off past the reset time rather than consume the
    attempt budget.
- **`ForgeProvisionerSeatEntitlementDrift`.** Three seats (`anthropic/claude-max-1`,
  `anthropic/claude-max-2`, `openai/codex-pro`, all `tenant-zero/playground`, kid
  `tz-213512e80fde7c84`) are granted in the broker kid registry but absent from
  `openbao/capability-kids-configmap`. The provisioner drift-corrects the auth roles every ~30 s and
  the finding immediately returns — a reconciliation loop, not a transient. **Establish which side is
  authoritative and whether these grants were intended before choosing** — declaring seats purely to
  silence drift would legitimize possibly-unintended access. Then update the source the competing
  reconcilers read, and confirm sustained agreement using non-secret metadata. Do not silence the alert.

### W5 — ai-node2 memory (P1 severity, gated on approval)

`ai-node2` is at **120/124 GiB used, 4 GiB available**, load average 20.7. Configured guest ceilings
total ~184 GiB against 124 GiB physical (talos-cp2 24G + ci-runner-2/5/9/10 at 24G each + dw2 16G +
dw5 16G + agent-node-2 16G + env-node-1 16G), plus the ai-llm-2 LXC. Ceilings establish **overcommit**;
they do not measure resident demand.

Measured directly (`qm monitor` / `info balloon`, 2026-09-07):

| VM | balloon `actual` | `total_mem` | `mem_swapped_out` (cumulative) | `major_page_faults` (cumulative) |
|---|---|---|---|---|
| dw2 (4202) | 4096 = its floor | 3.7 GiB | 21.6 GB | 3.9M |
| dw5 (4205) | 6144 = its floor | 5.75 GiB | 140.5 GB | 39.6M |

**Read those two right-hand columns carefully:** they are counters accumulated since boot
(2026-07-21, ~48 days), *not* current rates. They evidence a long history of swapping, not present
intensity. The **current-state** evidence is separate and is what justifies acting: dw5 at 82 % swap
occupancy with 1.69 GiB available, dw2 with 1.67 GiB available, `DevWorkerThrashing` pending as of
14:48 today, and the host at 4 GiB free. Before and after any change, capture **interval deltas**
(`rate()` on `node_vmstat_pswpout` / major faults), current available memory, swap occupancy, and PSI
pressure — that is the comparable measurement, and only two guests were sampled, so confirm whether
other balloonable guests on node2 are also pinned rather than assuming it.

This failure mode is documented in `dev-workers/variables.tf` — but for **ai-node1**, measured
2026-08-12 at 88 %, mitigated by 12 GiB floors on dw1/dw4. The problem has **moved**: node1 now has
25 GiB free (its LLM no longer pins qwen3.6) while node2 has gone 93 % → 97 %. dw5's 6144 override
(added after the 2026-09-01 swap-death incident) is insufficient, and **dw2 was never given an
override**. Pressure grew when ci-runner-9 + ci-runner-10 started on node2 on 2026-08-25 23:02
(+48 GiB of ceilings), then env-node-1 (09-01) and agent-node-2 (09-02).

**Ordering reversed from the first draft.** Raising balloon floors on a host with 4 GiB free is *not*
low-risk: higher guarantees plus guest boot peaks can force neighbours to reclaim or trigger host OOM
— and one of those neighbours is **talos-cp2, a control-plane node**. Capacity relief comes first.

**W5.1 (first) — relieve capacity. REQUIRES EXPLICIT PER-ACTION APPROVAL BEFORE ANY GUEST IS STOPPED.**

Move one guest off node2. Candidate destinations show 25 GiB (node1) and 22 GiB (node3) free, but a
free-memory snapshot does **not** prove headroom for a guest with a 24 GiB ceiling plus overhead —
budget source and destination *peak* demand, disk space, and host reserves before choosing. Likeliest
candidate is one of the two out-of-band runners that landed on node2 on 08-25 (ci-runner-9 or
ci-runner-10): most recent, most stateless, cheapest to recreate.

Do not assume a rebuild is required. Per-node `local-lvm` and `cpu: host` are the repo's stated
reasons, but Proxmox supports offline migration with local-disk transfer; check storage-transfer
support, CPU compatibility, and attached resources, then choose migration vs. offline migration vs.
rebuild on evidence.

If it is a rebuild, that is **replacement, not import** — so **W6 must complete first** (see below).
The approved action needs: runner job draining, registration and address handoff, a hard prohibition on
old and new guests holding the same IP simultaneously, and a tested return path. The standing
per-action approval rule applies to any ai-node outage.

**W5.2 (second) — set floors from a host-wide budget.** Only after relief is measured. Choose explicit
floor values for dw2/dw5 from a budget that reserves host headroom, with stated abort thresholds and
rollback instructions, codified in `dev_worker_nodes`.

Verify the installed PVE version's live-vs-pending balloon behaviour rather than assuming a reboot is
mandatory, and distinguish a configured minimum from the current allocation. If restarts are needed:
**drain active dev-worker work first** (these hosts carry long-lived tmux sessions and agentforge
jobs — a restart is disruptive to users even though the hypervisor stays up, so "no cluster impact" is
not the same as "no impact"), and restart one guest at a time.

### W6 — Bring the out-of-band guests under IaC (prerequisite, not optional)

Five runners (4106-4110), the env-node (4401), and the two reviewer VMs (4501-4502) exist with no tofu
state. **Completion of runner-module state reconciliation is a prerequisite for every subsequent
runners-module apply, including W5.1's rebuild option.** W1-W4 and the P0 containment do not depend on
it and can proceed in parallel.

Sequence, in a controlled local checkout with applies disabled throughout:

1. Confirm the correct module, its pinned provider, the ailab endpoint, and the active backend; back
   up `terraform.tfstate` securely and hold the lock against concurrent writers. Treat state and its
   backups as **sensitive** — never print their contents.
2. **Configure before importing.** OpenTofu requires matching configuration for the target address, so
   enable **only** the one `for_each` key being imported, with its verified address and live settings.
   Enabling all five at once makes intermediate plans propose creating the remaining four.
3. Import that instance, confirming the provider's expected import-ID format:
   ```
   tofu import 'proxmox_virtual_environment_vm.runner["ci-runner-6"]' ai-node3/4106
   ```
4. Verify the imported VM's identity and reconcile sizing, disks, placement, networking, and lifecycle
   against the live guest. **Stop on any unexpected create, update, destroy, or replace.**
5. Repeat for the next key. Also account for the **five `terraform_data.enable_guest_agent` resources**
   that made up the other half of `Plan: 10 to add` — decide per resource whether it is imported or
   legitimately created.
6. Finish with a **full, non-targeted** `tofu plan` with all intended entries enabled, expecting
   `No changes`.

Give the env-node and reviewer VMs their own verified resource addresses and state locations; they are
not part of the runners module.

**Caveat on the no-change check:** because `lifecycle { ignore_changes = [initialization] }` hides
`ipconfig0`, a clean plan cannot validate those fields. Separately compare each guest's recorded
address against live `qm config` and the IPAM registry before treating any future replacement as safe.
State-only import must not restart or replace a guest; record protected-state recovery instructions and
require manual review before any later apply.

### Out of scope (recorded, not fixed here)

- **QNAP orphaned LUNs.** Two PVs (`pvc-c61ddeac…`, `pvc-de79ab3b…`, 48Gi each, `Delete` policy) retry
  `Unmap IscsiTarget fail … Error Code : -19` roughly every 90 s; three more sit `Released`/`Retain`.
  Known pattern, needs QNAP-side LUN cleanup — a storage runbook task.
- **QNAP has no capacity monitoring.** `qnap-rules.yaml` covers only SMART, temperature, fan and
  exporter presence; the SNMP exporter emits 28 series and **none** are volume or pool usage. The array
  backs every `qnap-iscsi` PV, so this is a real gap — but adding capacity OIDs is its own change with
  its own verification.

## Critical files

| Path | Role |
|---|---|
| `kubernetes/infra/runners/variables.tf` | P0 contained in `7ab78e79`; W6 configures-then-imports, one key at a time |
| `docs/network-plan.md` | IPAM registry; landed in `7ab78e79`, source of truth for every address above |
| `kubernetes/apps/infrastructure/monitoring/ci-runners-node.yaml` | W1 — Endpoints `.20-.22` → `.29-.31` (submit full `subsets`) |
| `kubernetes/apps/infrastructure/monitoring/ci-runners-rules.yaml` | W1 — PVE-host tripwire + expected-nodename assertion |
| `kubernetes/apps/infrastructure/monitoring/alloy.yaml` (l.25) | W2 — add `dedicated=env` toleration |
| `kubernetes/apps/infrastructure/monitoring/storage-fabric-probe.yaml` (l.39) | W2 — add `dedicated=env` toleration |
| `kubernetes/apps/infrastructure/storage/velero/helmrelease.yaml` (l.78, l.159, l.168) | W2 toleration + W4 `testpool` exclusion (both schedules) |
| `kubernetes/apps/apps/ai/litellm-local.yaml` (l.33) | **W3 commit 1** — drop node1 deployment; needs config-checksum rollout |
| `kubernetes/apps/apps/ai/litellm.yaml` | W3 — consumer audit, kept in sync with litellm-local |
| `kubernetes/apps/apps/ai/llm-service.yaml` | **W3 commit 2** — drop `.44`, delete `llm-node1`, fix stale header |
| `kubernetes/infra/dev-workers/variables.tf` | W5.2 — dw2/dw5 floors, after relief |
| `kubernetes/apps/apps/renovate/cronjob.yaml` | W4 — jobTemplate TTL |
| `kubernetes/apps/apps/renovate/config-configmap.yaml` (l.11-12) | W4 (separate change) — forge target |
| `openbao/capability-kids-configmap` | W4 — declare or revoke, after establishing authority |

## Verification

Evidence before assertions. Note that "an alert stopped firing" is never sufficient on its own.

- **W1:** `count(up{job="ci-runner-node"} == 1) == 10` — count the *healthy* series, since `count(up)`
  also counts failed scrapes. Assert the ten scraped `nodename`s equal the expected `ci-runner-*` set;
  `up{job="ci-runner-node", instance=~"192.168.0.2[012]:9100"}` returns empty; every target's
  `gitea_runner_reclaim_last_run_seconds` is younger than the configured freshness threshold. Check the
  live Endpoints and Prometheus discovery *after* Flux reconciles. (`for:` governs entry into firing,
  not recovery latency, so allow for resolve delay.)
- **W2:** conditional on the per-agent coverage decision. If tolerated: `observedGeneration` advanced,
  `desired=7, ready=7, updatedNumberScheduled=7, misscheduled=0`, and each env-node pod's owner and
  **revision hash matches the new template** (proving replacement, not adoption). Then verify actual
  function: fresh env-node log lines arriving, probe series present, and a successful PVB from that
  node where backups are intended.
- **W3:** after commit 1, the running litellm-local pod is **new** and its loaded routing config
  contains no `llm-node1`; a minimal authenticated inference succeeds for each retained local model
  through each affected gateway (`/v1/models` lists configured inventory even when inference is
  broken, so listing alone proves nothing) — without exposing credentials. After commit 2, assert the
  expected surviving `llm` targets are present with `up=1` (an empty `up==0` result also passes when
  every target has vanished), `TargetDown` for ns `ai` clears, and the `llm-node1` objects are pruned.
- **W4:** the next **daily and weekly** backups both complete `Completed` with 0 errors, with a
  representative restore check for anything retained in `testpool`'s recovery contract; the old failed
  renovate Job is gone *and* Renovate has opened its expected dependency PRs; both reviewbot jobs
  reached a real verdict without duplicate posts; entitlement agreement holds across several
  reconciliation cycles, verified from non-secret metadata.
- **W5.1:** the moved runner accepts a representative CI job, keeps a unique registration and address,
  is scraped by W1's corrected target list, and its final state and placement are accurate. Confirm
  **sustained** relief on node2 and adequate destination headroom under representative load before
  retiring rollback resources.
- **W5.2:** judge by sustained available memory, swap and major-fault **rates** (not cumulative
  totals), and PSI pressure across a representative workload — `actual` may legitimately exceed the
  configured floor, and swap occupancy need not fall promptly once pressure subsides. Observe node2
  and its neighbours (especially talos-cp2) throughout, with explicit abort thresholds.
- **W6:** after each import, verify the VM's identity and every associated planned resource; finish
  with a full non-targeted `No changes` plan across each affected module. Confirm no guest restarted or
  was replaced.

Cluster-wide gate: for each Flux Kustomization, `status.lastAppliedRevision` equals the merged commit
(a stale `True` condition can describe an older revision) and affected HelmReleases report a completed
reconciliation — and note Flux says nothing about hand-applied `infra/`. Track residual alerts **by
identity** against per-workstream acceptance criteria rather than by counting down from 22: a falling
count can mask a target that disappeared instead of being fixed, and says nothing about deferred W5
capacity work. Confirm etcd is still 3/3 in sync.

<!-- codex-review-status: finalized -->
