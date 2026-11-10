# Dedicated Relay worker capacity rollout

Status: **capacity provisioned on 2026-11-08** by the operator. `relay-worker-1` is Ready; the DHCP
pool starts at `.53`; both preflight gates pass. Evidence is in
[Operator rollout record](#operator-rollout-record-2026-11-08). The owner approved the capacity,
the address and the temporary Qwen stop for S7/S8. The KubeVirt spike ran on 2026-11-09 and 10,
with a pass and one owner-accepted deviation ([run record](../relay-workers/kubevirt-spike-record-2026-11-09.md)).
Qwen was restored afterwards. KubeVirt was removed after the spike and is **not** installed. The
binding marks `vm.kubevirt` and `vm.compose` qualified (#1703); no other runtime is qualified.
The worker's disk has QEMU I/O limits ([Disk I/O limits](#disk-io-limits)). The [operator report](https://git.chifor.me/cchifor/ailab/pulls/1675#issuecomment-106374)
measured the capacity and confirmed nested KVM; the [feasibility runbook](relay-worker-kubevirt-feasibility.md)
remains the acceptance gate.

The proposal allocates **vmid 4304, `relay-worker-1`, 192.168.0.52** on **ai-node3** with
**12 vCPU, 24 GiB fixed RAM, 200 GiB local-lvm** and host CPU passthrough. It adds an independent
OpenTofu map, Talos patch and `relay-gvisor` RuntimeClass. No resource iterates or changes the
existing agent-node map. The new pool never carries `ailab.io/agent-pool`.

The map defaults to empty. Since the 2026-11-08 rollout, the operator's copy is the gitignored
`relay-workers.auto.tfvars` beside `terraform.tfvars`. OpenTofu loads it on every plan and apply,
through `just` or a direct `tofu.exe`, so no invocation can leave it out. Before a rollout,
keep it explicit (`-var-file`), as step 6 shows, so that an ordinary agent-pool apply cannot
provision it by accident.
The VM has `prevent_destroy` so omitting it later fails the plan instead of silently destroying
the worker. Intentional retirement requires its own reviewed removal of that guard.

## Approval and ordering

The owner approved this capacity and removing `.52` from DHCP. The operator must
check the UniFi lease/client inventory first; ping/ARP silence is not sufficient. Moving DHCP
start to `.53` does not revoke an existing lease. If `.52` is occupied, stop and resolve the
conflict before changing either side. Do not reuse another static address.

1. Record the cleanup owner and external evidence directory for the spike. The owner also
   approved the temporary stop of `llama-swap-qwen38` on LXC 5003 for S7/S8, with the operator
   responsible for restoring it afterwards. Record the stop and restoration times and service
   health. This approval does not authorize a permanent model-service change.
2. Merge the reviewed edge exclusion, then verify the deployed edge rejects worker-source traffic
   from `.52` to private/admin hosts. `trusted_sources.exclude` removes the node from both LAN and
   admin trust, including its SNATed pod/VM egress. This must precede node admission.
3. On the operator workstation, run `just edge-gateway-plan --only dhcp`. Inspect the plan hash
   and controller lease evidence; the sole desired change is Home's DHCP start `.52` → `.53`.
   Apply that exact reviewed plan with `just edge-gateway-apply --only dhcp --plan-hash <hash>`.
   Preserve the reconciler's lease checks; do not bypass an active-client refusal. Record the
   verified live range and update `gateway-state.yaml` using the established gateway workflow.
4. Recheck ai-node3 memory/storage and nested KVM against the operator report. Only ai-node3 is
   budgeted. The proposed floor leaves approximately 9.6 GiB at the measured historical low,
   before a lazy Qwen load; monitor the physical host during every spike step.
5. Stage the dedicated image below on **ai-node3 only**. Verify the factory schematic and Talos
   version, retain the download digest and size, and use the exact unique import filename.
   Do not use the legacy generic `talos-<version>-nocloud-amd64.raw` filename: an already staged
   protected-pool image may have different extensions.
6. On the existing operator OpenTofu checkout (which owns the local state), run:

   ```text
   tofu -chdir=kubernetes/infra/agent-nodes plan -var-file=relay-workers.tfvars.example -out=relay-worker.tfplan
   tofu -chdir=kubernetes/infra/agent-nodes show relay-worker.tfplan
   ```

   The plan must add only the Relay VM, its machine-config apply and RuntimeClass.
   Talos owns the Node labels and taint; there are no competing Kubernetes Node PATCH resources.
   No existing VM, machine config, PKI, pool or node is changed or removed. A dev-worker cannot
   validate this against live state. After reviewing the nonsecret summary, the operator applies
   the saved plan. Keep plan/state files outside git; they contain sensitive configuration.
7. Wait for initial registration and readiness with a bounded operator check:
   `kubectl wait --for=condition=Ready node/relay-worker-1 --timeout=300s`. If the Node is not yet
   registered, retry this read-only check within a total five-minute registration window; stop
   and inspect Talos if either bound expires. The RuntimeClass can exist before registration
   because its selector admits no node without the exact dedicated labels.
   Verify the actual hostname is `relay-worker-1`, the dedicated label/taint are present and
   no agent-pool label exists. Verify qemu-guest-agent and gVisor extensions, KVM availability,
   `user.max_user_namespaces`, disk size, Cilium readiness, and absence of protected workloads.
   Run the collector with the updated binding. Any mismatch stops the rollout.
8. Execute the separately bounded KubeVirt spike and pod qualification. A Ready Talos node or a
   created RuntimeClass does not qualify a runtime. No Relay worker admission is enabled here.

The gateway reconciler is a manual operator CLI: repository search found only the `justfile`
recipes and documentation, with no scheduled task, Ansible hook, Flux object or CI invocation
that applies it (CI runs offline tests). A merge changes desired state, not the controller.
Until the lease and edge checks above are recorded, unrelated gateway work must explicitly
use `--only dns` or `--only forward`; an unfiltered plan includes the pending DHCP transition.
Apply always requires the recomputed exact plan hash, and live active-client checks run both
when planning and immediately before the DHCP write. Do not use `--ack-dhcp-leases` for this
reservation. Record the lease and edge evidence in this PR before the dedicated DHCP apply.

## Image and version selection

[Runtime pins](../relay-workers/runtime-pins.json) records the exact candidate artifacts.
KubeVirt **v1.9.1** is a stable release; its 1.9 support matrix includes Kubernetes **1.35**.
The upstream operator/CR downloads were hashed on 2026-11-08 and the operator OCI index digest
was resolved from Quay. Before S1, render a reviewed spike bundle from these downloads: pin the
operator image and its `VIRT_OPERATOR_IMAGE` to the digest, pin component images, and restrict
both infra and workloads placement to the dedicated pool. Upstream defaults alone do not establish
that placement. Record rendered hashes separately from upstream hashes. This proposal does not
apply the upstream manifests.

The Talos v1.14 guide permits pod-network masquerade; its optional Multus, CDI, NFS and live-migration
examples are outside this spike. The new schematic contains only `siderolabs/gvisor` and
`siderolabs/qemu-guest-agent`, available in the v1.14.2 factory catalog. The Talos patch requests
`kvm_amd` and `vhost_net` and sets the gVisor user namespace allowance. Verify module availability
on the new node; the schematic ID itself proves neither nested KVM nor containment.

For the operator's bounded image staging, use this exact source and destination:

```text
source: https://factory.talos.dev/image/8e8827b5b91420728f8415f3dc200fbf23b425ec07bf27cdc92a676367ee9edf/v1.14.2/nocloud-amd64.raw.xz
host: ai-node3 (192.168.0.4)
destination: /var/lib/vz/import/talos-v1.14.2-relay-workers-8e8827b5b91420728f8415f3dc200fbf23b425ec07bf27cdc92a676367ee9edf.raw
```

Use the established operator SSH tooling to download to a unique temporary filename on that host,
check the download digest against the factory artifact, decompress, then atomically rename to the
above destination. Do not overwrite a pre-existing destination without comparing its digest.
Remove only this task's temporary files afterwards. Stage it as `local:import`; the existing
`local` datastore must allow the `import` content type. No image is downloaded into git or a
worker's OS disk.

## Disk I/O limits

`relay-worker-1`'s only disk is a thin volume on ai-node3's `local-lvm`. That pool sits on the
host's single NVMe: a consumer Kingston OM8TAP4 2 TB with no power-loss protection. These guests
share the drive:
- `talos-cp3`, whose etcd needs low fsync latency;
- `talos-agent-node-3`, which runs Relay and the router;
- `ci-runner-3` and `ci-runner-6`;
- the Qwen LXC (5003);
- both reviewer VMs.

Spike finding F12 asked for an I/O limit on the worker's disk that covers bandwidth as well as
latency ([run record](../relay-workers/kubevirt-spike-record-2026-11-09.md#findings)).

**The limits** are set in `local.relay_worker_disk_speed` in
`kubernetes/infra/agent-nodes/relay-workers.tf`, and Proxmox writes them onto the drive line as
`mbps_*` and `iops_*`:

| | Sustained | Burst (QEMU's 1-second pool) |
|---|---|---|
| Read | 200 MB/s, 5000 IOPS | 500 MB/s, 10000 IOPS |
| Write | 100 MB/s, 8000 IOPS | 300 MB/s, 12000 IOPS |

**Why these numbers.** The guest's own `node_exporter` disk counters, at 1-minute resolution,
showed:

| Load in the spike | Guest disk | cp3 WAL fsync p99 |
|---|---|---|
| Soak run 2 (capped Compose load, 3 h) | writes ≤ 44 MB/s and ≤ 3.8k IOPS; reads ≤ 0.6k IOPS | 4–20 ms at 30-minute points (one 0.12 s) |
| S3 cache fill (two 1-minute direct-I/O bursts) | writes 454 and 579 MB/s | 8–15 ms |
| Soak run 1 thrash (guest OOM loop, 15:40–16:25Z) | reads 138–866 MB/s and 4.0k–13k IOPS (5-minute means; peaks 1.28 GB/s and 18.4k IOPS) | 0.02–0.39 s |

Run 1 does not prove the VM was the cause. cp3 was already at 0.2–0.46 s from 14:40 to 14:55Z,
when the VM was reading under 3 MB/s, and cp1 and cp2 spiked in the same hour. The limits
therefore bound the worker's worst case rather than cure a measured cause:
- The sustained values keep at least 2× headroom over the sustained load that was harmless (soak
  run 2).
- They cap the thrash regime at well under half its observed peaks.
- The burst pool lasts one second. It absorbs spikes of up to about 300 MB written or 500 MB read,
  and nothing longer.
- Longer transfers run at the sustained rate, so they are throttled even when they would have been
  harmless:
  - a 1 GB image pull writes for about 8 s;
  - S3's 19.5 GiB cache fill would take about 3.5 minutes; S3's writes ran at up to 579 MB/s.

  That is the cost of the guardrail. S3 showed only that minute-long write bursts were harmless;
  nothing measured longer ones.

The cap also bounds the worst case on endurance:
- 100 MB/s of sustained writes is 8.6 TB a day.
- Soak run 2's capped S4 Compose load averaged 33.4 MB/s, about 2.9 TB a day (record F12).
- At its 44 MB/s peak it would be 3.8 TB a day.

**What it does not do:**
- The other heavy writers on the drive are unthrottled. These are the CI runners, which wrote up
  to 37 MB/s each (5-minute means) during run 1.
- It does not replace power-loss protection. The durable fix for etcd is still dedicated CP disks.
- The limit applies to the whole worker disk: the node's root, containerd, kubelet, `local-path`
  PVs and every launcher's ephemeral storage share one budget.

**Changing the limits:**
1. Edit the local.
2. Run `just agent-nodes-plan`. The plan must show only an in-place `disk.speed` update on
   `relay-worker-1`.
3. Apply.

Proxmox applies a throttle change to the running VM through QEMU (`block_set_io_throttle`), so
neither the VM nor the node restarts. To check what is live:
- `qm config 4304` shows the drive options;
- `info block` on the VM's monitor shows the live throttle.

## Pool rule: no workload before an egress policy

Before the first workload on the pool (spike step S2 included), its namespace carries a default-deny
egress `CiliumNetworkPolicy` that allows only DNS and the approved endpoints. The node has no other
isolation from:
- **The LAN**, `192.168.0.0/23`.
- **Every node address.**
- **`cloud-power`.** Leaving `.52` out of its `ALLOW_FROM` does not isolate workers: that list also
  admits the pod CIDR `10.244.0.0/16`, and Cilium does not masquerade pod traffic to cluster nodes. A
  worker pod can therefore reach the `hostNetwork` WoL listener (`:8127` on every node, plus its
  Service). It only serves `POST /api/wake`, so it can power the GPU cluster **on**, never off; the
  power-off API sits behind its own NetworkPolicy.
- **The NAS storage IP `10.55.0.254`.** This node carries the storage-fabric `/32` route like the
  agent nodes, although the binding forbids NAS storage classes here.

S5 proves the policy: the WoL path must be refused both through the Service and through a node
address, and `10.55.0.254` must be unreachable.

## Rollback and evidence

Before a VM exists, rollback can remove the opt-in configuration. Keep `.52` excluded from edge trust
while any worker still uses it.

**Retiring the node**, one step at a time:
1. **Cleanup while the node still exists.** Stop admissions. S9 (the K5 owner): the worker
   namespaces are deleted, and no VM, VMI, PV, `virt-launcher` pod or `local-path` directory remains.
   The `local-path` cleanup helpers need the node. Never remove a bound PVC or finalizer just to make
   a teardown pass.
2. `kubectl --context admin@ai drain relay-worker-1 --ignore-daemonsets`.
3. **A reviewed OpenTofu removal.** It drops `prevent_destroy` and empties the opt-in map. It deletes
   the VM and drops the config-apply record, but it neither resets Talos nor deletes the Kubernetes
   Node.
4. `kubectl --context admin@ai delete node relay-worker-1`. Confirm that its `CiliumNode` is gone and
   that nothing still targets it.
5. Only then return `.52` to DHCP, updating IPAM, the desired gateway range and the live controller
   together.

**Rebuilding the VM:** `talos_machine_configuration_apply.relay_worker` is `replace_triggered_by` its
VM's id, so a replaced VM gets its config re-applied and joins again. A plan-only test on
2026-11-08, with `prevent_destroy` lifted in a scratch copy, showed it replaced "due to changes in
replace_triggered_by". Delete the old Node object first (step 4); `prevent_destroy` must be lifted
by a reviewed change for any rebuild.

Record applied commit, reviewed plan summary, gateway range, node identity, extension IDs,
allocatable resources and preflight report hashes. Update this document's pending status only
from that evidence. The existing spike's S1–S9 receipts remain a separate requirement.

## Operator rollout record, 2026-11-08

Every step above ran on 2026-11-08 from the operator workstation. Raw evidence lives outside git in
`C:\Users\chifo\work\home\evidence\relay-worker-kubevirt\`.

| Step | Result |
|---|---|
| 2 Edge | Flux at `main@8c82fa1a`, and the `edge-proxy` rollout is complete. The mounted Caddyfile's trusted ranges (`@not_lan`/`@not_admin` are `not remote_ip <trusted>`) list `.53/32 .54/31` where they used to list `.52/30`. `.52` is therefore **excluded** from the LAN and admin tiers, from `hostnames.yaml` `trusted_sources.exclude` `192.168.0.52/32` (#1676). |
| 3 DHCP | The live client list showed **no client on `.52`** (by DHCP or fixed IP); `.53` holds a DHCP client that stays in the pool. **The controller had re-IDed all its objects to UUIDs since 2026-10-27**, so `Home` became `c7406446-259e-48df-9f99-06a0d833fe8a`. `gateway.yaml` was corrected, then plan `1255431b5b8215b942fc` (forget, adopt and update Home, `.52` → `.53`) was applied. The read-back plan reports no changes. The forward and the 17 static-DNS records, stale the same way, were re-adopted on 2026-11-09 (plan `6e2e483d71bb8c436aed`, forget and adopt only, no controller write); an unfiltered plan then reported no changes. |
| 4 Host | ai-node3: MemAvailable 37.9 GiB, `kvm_amd` nested=1, `local-lvm` 1.14 TiB free, GTT 0. |
| 5 Image | The Image Factory confirmed that the schematic definition (gvisor + qemu-guest-agent) hashes to `8e8827b5…`. Download `.raw.xz`: sha256 `300fd28886bdc6f8b9d5ac13fdc3ac34de2c984b94d85d69d5298466f09a0db7`, 341 045 040 bytes. Image `.raw`: sha256 `dac67c75c1c3fb8f704105d0510ae24a22a886a5bfa01dd7f747536bf636fb12`, 4 453 302 272 bytes. Staged under the unique filename via a temp dir and an atomic rename. |
| 6 Plan/apply | `main@b81ba53b`, with `-var-file=relay-workers.tfvars` (renamed to `relay-workers.auto.tfvars` after the apply): **3 to add, 0 to change, 0 to destroy** (the VM, its config apply, RuntimeClass `relay-gvisor`). The saved plan was applied as reviewed, and the VM was created in 17 s. |
| 7 Verify | `relay-worker-1` was Ready about 30 s after the apply (v1.35.9, Talos v1.14.2). See the identity check below. |

Identity check on `relay-worker-1`:
- **Labels and taint:** `ailab.io/relay-worker-pool=true` and `ailab.io/runtime-gvisor=true`, with no
  `agent-pool`. The only taint is `dedicated=relay-worker:NoSchedule`. Talos owns both.
- **Schematic:** `8e8827b5…`, with only gvisor 20260831.0 and qemu-guest-agent 11.1.1.
- **KVM:** `/dev/kvm` and `/dev/vhost-net` are present; `kvm_amd` is loaded with Nested Paging, and
  SVM is visible.
- **Kernel and disk:** `user.max_user_namespaces=11255`; disk 215 GB.
- **Allocatable:** 11.95 CPU, 22.98 GiB, 176 GiB ephemeral.
- **DaemonSets running:** cilium, cilium-envoy, csi-nfs-node, node-exporter, trident iscsi-recovery-tmo
  and trident-node-linux. Trident logged only one-time startup warnings (no iSCSI initiator, NVMe or
  SMB on a schematic without `iscsi-tools`) and no errors, and its plugin registered.
- **Not running here:** `alloy` and velero's node-agent, which do not tolerate the taint.
- **Protected workloads:** no `relay` or `llm-router` pod is on the node.

Preflight after the rollout (`relay-worker-preflight-20261108T181637Z.json`, sha256 `56ca1b55…68e1`;
report sha256 `6a7e126a…b77c`):
- `lightweight_canary_prerequisites`: **pass**.
- `kubevirt_spike_prerequisites`: **pass**.
- `kubevirt.installed` fails until S1 installs it, and `kubevirt.kvm_device` stays unknown until
  KubeVirt's device plugin runs.
