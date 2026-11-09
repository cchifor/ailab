# KubeVirt feasibility spike: operator run record, 2026-11-09

The operator ran [the spike](../runbooks/relay-worker-kubevirt-feasibility.md) on the dedicated
node `relay-worker-1` (vmid 4304, ai-node3, `192.168.0.52`), from the operator workstation with
`kubectl --context admin@ai`.
- **Owner and cleanup owner (K5):** the operator (cchifor), 2026-11-09.
- **Raw evidence:** outside git, in `C:\Users\chifo\work\home\evidence\relay-worker-kubevirt\`. Its
  `SHA256SUMS` lists every artifact, and the [artifact table](#evidence-artifacts) gives the main
  ones.
- **What this record is:** evidence for the binding decision, not the binding change itself.

**Result: SOAK_RESULT_PLACEHOLDER**

## Pins as applied

| Item | Value |
|---|---|
| KubeVirt | `v1.9.1`. virt-operator is `quay.io/kubevirt/virt-operator@sha256:a8adf7b7…5df9`, and every component is pinned by digest (`docs/relay-workers/runtime-pins.json`). `verify-image-pins` passed for all 15 tags at 09:33Z. |
| Upstream manifests | `kubevirt-operator.yaml` is `9ac45a43…292da`, and `kubevirt-cr.yaml` is `43106136…84a2af`. They match the pins. |
| Operator bundle | `10-operator.yaml` is `e4b2d748b31596b2d740c85808e2138e0254e67230619e26ed05bf3420a00606`. `20-kubevirt-cr.yaml` is `7d9f26e80c84966aae0411c6b437cd2571687850eab12ab7bf2ef3b2ea02e3b5`. Both re-render byte-identically from this branch. |
| Workload, first render (S2 first boot) | `30-workload.yaml` is `66e37193f369d4ac3c8708327b5aeae1d3e539f5ae60d7fc8fa1ed18f1747d65`. |
| Workload, second render (from S2's second boot on; this branch) | `30-workload.yaml` is `3a0ce9a352d9ccfca7c0fe3fb99f6b4cbbe08574dc5bd3f3aee186669f9e6ad5`. It was applied without the prep Job: the applied subset is `b0da98f7…901a`, and the VM-only file for S7 and S8 is `d3d0ee59…8e8d`. See [deviations](#deviations-from-the-reviewed-procedure). |
| Guest | The containerDisk is `quay.io/containerdisks/ubuntu@sha256:ea164c51…f3a4`. `measure-guest-layer` reports 621,284,352 bytes expanded and a qcow2 virtual size of 3,758,096,384. `guest-evidence.json` was measured at 2026-11-09T09:34:31Z. |
| Launcher UID | 107. It comes from the OCI config `User`, and the live launcher's `runAsUser` confirms it. |
| virtctl | v1.9.1, windows-amd64, `61b49733…f4c8`. |
| S4 images | `quay.io/sclorg/postgresql-16-c9s@sha256:29e66548679f0d14b63e2a210dfcc783c376330366bd24b97f2968e7c3bff0f7` and `quay.io/sclorg/redis-7-c9s@sha256:d772acab11507231a2b152850f47c8b9b859dedb6652ca624949e97cb28c5e6f`. The app is built from the pinned busybox. |

**Preflight.** Both runs used the binding example.
- **Before S1** (collector `main@f569080d`, 09:35:27Z): both gates pass.
  - Snapshot: `fbc48899e609a77ca2d1fb6b16da29dfe2b6b1318dba704880a64db137c2ef28`.
  - Report: `8d57adb45b63f650f3a09b02aae8dafbcd4202fc35fe3a2d9d2052d4cd7703b1`.
- **After S1** (09:39:59Z): exit 0, with `kubevirt.installed` and `kubevirt.kvm_device` both
  passing, and the KVM device on `relay-worker-1` only.
  - Snapshot: `d5b71679804b564bc872939edc17c98d4ebd5e116c0a708a4df70bc5c39da31d`.
  - Report: `9a9bfedb7ddb3dcf8626d6bdd1cf5f82276055923f4f5c351a57a1c0a594383e`.

This also closes the two gaps #1690 left open in the 2026-11-08 18:16:37Z pass:
- **Report digest:** the full SHA-256 is
  `6a7e126a3370772cfe9e4f65eb6de4b77af249527e800b3a1d466e372970b77c`.
- **Collector revision:** `main@b81ba53b`. The checkout's reflog shows HEAD there from 18:14Z.

## Results

| Step | Result | Measurements |
|---|---|---|
| S1 install | **pass** | Operator rollout took 71 s, and `kv/kubevirt` was `Available` 117 s after the CR apply. All five components run on `relay-worker-1` only, and no other node carries KubeVirt labels. `devices.kubevirt.io/kvm` is allocatable, and `useEmulation` is unset (hardware KVM, nested `kvm_amd`). |
| S2 boot | **pass** after D1 and D2 | See S2 detail below. |
| S3 storage | **pass** | See S3 detail below. |
| S4 Compose | **pass** | One stack with `db`, `cache`, `app`, `pgload` and `redisload`, sampled 48 times over 12 minutes with no failed samples. See S4 detail below. |
| S5 Cilium | **pass** | See S5 detail below. |
| S6 stop and delete | **pass** | The VMI and launcher were gone 17 s after stop, the VM object deleted immediately, and nothing was left `Terminating`. |
| S7 node loss | **pass** | See S7 detail below. |
| S8 soak | SOAK_ROW_PLACEHOLDER | 20 of 20 cycles passed (create, boot, Compose, stop, delete): Ready took 91–137 s (median 121.5 s), Compose 48–63 s, and teardown 16–22 s, against a 300 s limit. Nothing was left behind, and Relay and the router were checked after every cycle. SOAK_DETAIL_PLACEHOLDER |
| S9 cleanup | S9_PLACEHOLDER | |

**S2 detail.**
- **Timings:** `Running` at 18–38 s (38 s included the 19 s containerDisk pull), guest agent
  connected at 74 s, and the in-guest readiness probe passed at 115 s.
- **Launcher:** QoS `Guaranteed`. Memory is 8678 Mi, the 8 GiB guest plus 486 Mi of overhead.
  Ephemeral storage is 30 Gi + 50 M. It fits the quota with about 1.9 GiB to spare.
- **Security:** UID 107, non-root, all capabilities dropped except `NET_BIND_SERVICE`.
- **Admission:** after the VM stopped, a server dry run admitted the captured launcher.
- **In-guest reboot:** requested through the guest agent. The boot ID changed while the VMI stayed
  the same, the smoke container restarted 15 s after boot, and the probe passed afterwards.

**S3 detail.**
- **Workspace:** a 1 GiB blob (`71e5dbf3…6e18`) survived a stop and start, an OOM crash, an
  eviction, the S7 node reset and the following cycles.
- **Cache disk:** the guest got ENOSPC at 19.5 GiB written. kubelet charged the launcher
  20.29 GiB, under its 30.05 GiB limit.
- **Eviction:** after a direct-I/O filler, kubelet evicted the launcher with "Pod ephemeral local
  storage usage exceeds the total limit of containers 32262254720". Free space on the node never
  dropped below 157 GiB, and node use returned to 9.05 GiB afterwards.
- **PV:** node-affine to `relay-worker-1`.

**S4 detail.**
- **Peaks:**
  - guest memory 1.09 of 7.75 GiB;
  - launcher working set 4.98 GiB, against a limit of 8.47;
  - node working set 7.36 GiB, with at least 16.09 GiB always available;
  - node disk 11.82 GiB;
  - guest disks: `/` 2.24 GiB, workspace 2.78 GiB, cache 0.77 GiB.
- **Throughput:** pgbench at 1650–1835 transactions per second, with 4.4 ms average latency.

**S5 detail.**
- **Allowed:** quay.io by name (HTTP 401 from `/v2/`) and by IP.
- **Blocked from inside the guest:**
  - github.com, by name and by IP;
  - `1.1.1.1`;
  - the LAN gateway, the edge VIP and the registry;
  - both kubelets;
  - the WoL listener, through the node address and the Service;
  - the cloud-power API;
  - NAS `10.55.0.254`, on both iSCSI and NFS;
  - the API server.
- **Inbound:** a pod in another namespace could not reach the guest's sshd, which is listening.
- **Controls:** an unrestricted pod on the same node **reaches** the WoL listener, the WoL Service
  and NAS iSCSI. The policy is what isolates the VM.
- **Hubble:** recorded "Policy denied" drops.

**S7 detail.**
- **Reset:** `qm reset 4304` at 10:48:25Z. The node was NotReady at 29 s and Ready again at 41 s.
- **Stale status:** the KubeVirt control plane went down with the node, so the VMI kept reporting
  `Running` until virt-controller returned.
- **Failed, not recreated:** at 66 s the VMI was `Failed`, and it was not recreated
  (`runStrategy: Manual`). The old launcher failed admission with `UnexpectedAdmissionError`
  ("no healthy devices… kvm") because kubelet re-admitted it before the device plugin
  re-registered.
- **Explicit start:** Ready in 92 s with a new boot ID. The workspace was intact. Compose was
  recreated with new container IDs, not resumed.
- **Protected workloads:** Relay and the router were untouched throughout.
- **Teardown:** 19 s, clean.

## Findings

| # | Finding | Effect | Action in this change |
|---|---|---|---|
| F1 | Guest DNS through masquerade reaches Cilium as the **untranslated** ClusterIP `10.96.0.10` (identity `cidr:10.96.0.0/12`). The cluster runs kube-proxy replacement with `bpf-lb-sock=false`. An ordinary pod under the same policy resolves fine. | The bundle's `toEndpoints` kube-dns rule never matched, so the first boot could not install packages and S2 failed. | The renderer adds `toCIDR: <kube-dns ClusterIP>/32` (DNS port, DNS-proxy rules). A `/32` service IP gets its own identity, `cidr:10.96.0.10/32`. The ClusterIP is pinned in `runtime-pins.json` (`cluster_dns.cluster_ip`). The renderer refuses a missing, non-IPv4 or estate value, and the runbook checks the pin against the live Service before the apply. Relay design: VM workers cannot reach **any** ClusterIP by `toEndpoints` or `toServices`. Use pod IPs or `/32` CIDR rules, as with Kata (agentforge, 2026-07-15). |
| F2 | Docker 29 (noble `docker.io`) uses the containerd image store, which keeps image content in `/var/lib/containerd` on the root partition (about 2.4 GiB, under 100 MiB free after the install). | The first real pull hit ENOSPC, and the 20 Gi cache disk went unused. | For the spike, the guest recipe sets `features.containerd-snapshotter: false`, which gives `overlay2` under `data-root` on the cache disk. That flag returns Docker 29 to the graphdriver store that upstream deprecates, so it is a spike workaround. The durable fix for a production template is to keep the containerd store and move containerd's root (all of `/var/lib/containerd`) onto the cache disk. The root partition stays tight either way. |
| F3 | The launcher's memory limit leaves about 486 Mi above the guest. A buffered writer inside the launcher's cgroup triggered a memcg OOM that killed `qemu-kvm`, and `memory.oom.group` killed the whole compute container. QEMU's own disk I/O bypasses the page cache, so the 20 GiB guest fill was fine. | The first eviction attempt crashed the VM instead. | The runbook's eviction step now uses `oflag=direct`. Nothing in production may run buffered I/O inside a launcher. |
| F4 | virt-api's two `pods/eviction` webhooks are cluster-wide (`failurePolicy: Fail`, no selectors), and virt-api has a single replica on the dedicated node. | While `relay-worker-1` is down, every eviction-API call in the cluster fails, including drains. Measured window in S7: about 40–66 s. | The runbook documents it, and nothing may drain during S7. A production adoption needs either HA infra off the worker pool or an accepted drain dependency. |
| F5 | The KubeVirt control plane (virt-api, virt-controller, virt-operator) lives on the one worker node. | VMI status is stale while the node is down. | Record only. It is a placement decision for any Flux adoption. |
| F6 | The v1.9.1 launcher passes PSA `baseline` and fails `restricted` only on `seccompProfile`. There is no `NET_ADMIN`. | The `privileged` exception was unnecessary. | The spike namespace now enforces `baseline`. Every launcher since the second render was admitted under it. The prep Job is not in the r2 apply, so its Pod template was server-dry-run against the live `baseline` namespace and admitted with no `restricted` warning (`s2-prep-baseline-dryrun.log`). The whole bundle is therefore admission-verified. |
| F7 | KubeVirt grows `disk.img` to the PVC capacity minus 5.5 % (20,293,091,328 B) on first start. | The workspace bound is the PVC, not 18 GiB, and a second prep Job run fails its size check. | The runbook says so. The r2 apply excluded the Job. |
| F8 | `virtctl start` on a `Halted` VM sets `runStrategy: Always`. | KubeVirt silently restarted the VM after the OOM crash and the eviction (S3). | S7 used `Manual` to keep the post-rejoin start explicit. The Relay controller must choose this deliberately. |
| F9 | The readiness probe's 60 × 30 s failure budget means VMI `Ready` did not drop during an in-guest reboot. | `Ready` lags a guest outage by up to 30 minutes. | The runbook notes it. A Relay health signal needs its own liveness check. |
| F10 | On Windows, the renderer and its test fixtures wrote CRLF, so the printed SHA-256 did not match the file, and 7 operator tests failed locally. | Hashes in evidence did not match the files. | The renderer writes bytes, so LF on every OS, and the fixtures do too. All spike tests pass on Windows. |

## Deviations from the reviewed procedure

- **D1, DNS.** During S2 a separate additive CiliumNetworkPolicy allowed DNS to `10.96.0.10/32`
  (`kv-spike-deviations/31-dns-clusterip.yaml`, `4cf13111…bd98`). It was replaced by the
  re-rendered bundle rule (F1) and deleted.
- **D2, second render.** From S2's second boot, the workload is this branch's re-render: F1, F2,
  F6 and LF output. It was applied after stopping the VM and **without the prep Job**, because the
  Job had already prepared the disk and cannot re-run (F7). `--force-conflicts` took
  `runStrategy` back from `virtctl`.
- **S7.** The VM was patched to `runStrategy: Manual`, so the post-rejoin start is the explicit one
  the runbook requires (F8).
- **Extra checks.** Two short-lived probe pods ran: one in `relay-kv-spike` to separate the policy
  from the datapath (F1), and one in a throwaway `relay-kv-spike-probe` namespace for the S5
  inbound test and controls. Both were deleted.
- **In-guest access.** S2–S8 used the QEMU guest agent (`guest-exec`) through `virsh` in the
  launcher. It needs no console, SSH or ingress.

## Qwen stop and restore (S7/S8)

`llama-swap-qwen38` on LXC 5003 was healthy (HTTP 200 on `:8082/health`) with no model loaded.
It was stopped at 2026-11-09T10:44:56Z under the owner's approval of 2026-11-08.
QWEN_RESTORE_PLACEHOLDER

## Evidence artifacts

EVIDENCE_TABLE_PLACEHOLDER
