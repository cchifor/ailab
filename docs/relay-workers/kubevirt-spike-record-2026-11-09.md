# KubeVirt feasibility spike: operator run record, 2026-11-09

The operator ran [the spike](../runbooks/relay-worker-kubevirt-feasibility.md) on the dedicated
node `relay-worker-1` (vmid 4304, ai-node3, `192.168.0.52`), from the operator workstation with
`kubectl --context admin@ai`.
- **Owner and cleanup owner (K5):** the operator (cchifor), 2026-11-09.
- **Raw evidence:** outside git, in `C:\Users\chifo\work\home\evidence\relay-worker-kubevirt\`. Its
  `SHA256SUMS` lists every artifact, and the [artifact table](#evidence-artifacts) gives the main
  ones.
- **What this record is:** evidence for the binding decision, not the binding change itself.

**Result: pass, with one deviation the owner accepted (D5).**
- S1–S7 and S9 pass.
- S8's 20 cycles pass. Neither soak run reached 8 hours. The operator stopped run 1 under the
  abort rule after 4 h 36 min (D4), and the alert watch stopped run 2 after 2 h 57 min (F14). On
  2026-11-10 the owner accepted S8 on the evidence of both runs.
- KubeVirt was removed after S9, and the node is back to its pre-S1 state.
- Moving `vm.kubevirt` and `vm.compose` to `qualified` is a separate binding PR. This record does not
  change the binding.
- F15 must be fixed before any KubeVirt install.

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

**Preflight.** Every run used the binding example. The run after KubeVirt's removal is in the
[S9 detail](#results).
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
| S1 install | **pass** | Operator rollout took 71 s, and `kv/kubevirt` was `Available` 117 s after the CR apply. All five components run on `relay-worker-1` only, and no other node carries KubeVirt labels. `devices.kubevirt.io/kvm` is allocatable, and `useEmulation` is unset (hardware KVM, nested `kvm_amd`). S1's checks did not cover a side effect found later: the new CRDs OOM-killed cert-manager's cainjector on cp3 (F15). |
| S2 boot | **pass** after D1 and D2 | See S2 detail below. |
| S3 storage | **pass** | See S3 detail below. |
| S4 Compose | **pass** | One stack with `db`, `cache`, `app`, `pgload` and `redisload`, sampled 48 times over 12 minutes with no failed samples. See S4 detail below. |
| S5 Cilium | **pass** | See S5 detail below. |
| S6 stop and delete | **pass** | The VMI and launcher were gone 17 s after stop, the VM object deleted immediately, and nothing was left `Terminating`. |
| S7 node loss | **pass** | See S7 detail below. |
| S8 soak | **pass with deviation D5** (owner-accepted) | 20 of 20 cycles passed (create, boot, Compose, stop, delete): Ready took 91–137 s (median 121.5 s), Compose 48–63 s, and teardown 16–22 s, against a 300 s limit. Nothing was left behind, and Relay and the router were checked after every cycle. The soak ran twice, 7 h 33 min under load in total, and neither run reached 8 hours (D4, D5). In both runs every pass criterion held: no guest freeze (run 1's three missed guest-agent checks came during the out-of-memory thrash, F11), no kubelet or containerd wedge, teardowns of 17 s and 16 s, node memory at most 48 % of allocatable, and Relay and the router healthy at all 88 checks. See S8 detail. |
| S9 cleanup | **pass** | Deleting the workload bundle removed the namespace, its PV and the `local-path` directory in 7 s. No VM, VMI, `virt-launcher` pod, PV or `local-path` directory remained. There was no DataVolume: the CRD was never installed. The probe namespace was already gone. KubeVirt was then removed in 29 s. See S9 detail. |

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
- **Restart wave:** `ControlPlaneRestartWave` fired at 10:56–10:58Z. The containers that restarted
  were `relay-worker-1`'s own DaemonSet pods: cilium-agent, cilium-envoy, trident-node,
  csi-nfs-node, node-exporter and the iSCSI reconciler. A node reset trips this alert by design.
  The runbook's alert watch now ignores it for 15 minutes after the reset.
- **Teardown:** 19 s, clean.

**S8 detail.**
- **Cycles:** see the results table.
- **Soak run 1 (12:00:57Z to 16:36:42Z, stopped at 4 h 36 min):**
  - **Health:** 53 health checks. Node, kubelet, Relay and router were healthy at all 53, and the
    guest's boot ID never changed.
  - **Peaks:**
    - launcher working set 8.108 GiB against a limit of 8.475 (F11);
    - node working set 10.98 GiB, which is 48 % of the 22.98 GiB allocatable;
    - at least 12.47 GiB always available;
    - node disk 25.60 GiB;
    - launcher ephemeral storage 12.49 GiB.
  - **Out-of-memory loop:** the test load's uncapped Redis filled the guest by about 15:20Z. The
    guest OOM-killed it three times (F11).
  - **Guest agent:** missed 3 checks, at most 2 in a row.
  - **Redis fix:** the operator capped Redis at about 16:25Z (D3).
  - **Alerts and stop:** a later alert review found that `etcdHighCommitDurations` had fired on
    `talos-cp3` at 14:48Z and 15:53Z (F12). The soak loop did not watch alerts (F13). The
    operator stopped the soak under the runbook's abort rule at 16:36:42Z (D4). Teardown took
    17 s, and nothing was left behind.
- **Soak run 2 (17:38:23Z to 20:35:54Z, stopped at 2 h 57 min):**
  - **Setup:** the loop checked the Redis cap at the start (1 GiB, `allkeys-lru`) and recorded 15
    firing alerts as the alert watch's baseline. Ready took 141 s.
  - **Health:** 35 health checks and 176 samples, all healthy, with no missed guest-agent check
    and one boot ID. Redis used 55 MB to 1.01 GB.
  - **Peaks:**
    - launcher working set 5.66 GiB against a limit of 8.475, with 2.88 GiB of headroom;
    - node working set 8.56 GiB, which is 37 % of allocatable;
    - at least 14.89 GiB always available;
    - node disk 15.64 GiB;
    - launcher ephemeral storage 1.89 GiB;
    - guest memory 1.62 of 7.75 GiB;
    - guest load1 median 6.59.
  - **Flat after warm-up:** the launcher was at 5.53 GiB 30 minutes in and 5.66 GiB at the stop,
    about 50 MiB an hour. Ephemeral storage went from 1.83 to 1.89 GiB.
  - **Stop:** the alert watch saw an etcd leader change at 20:35:54Z. The stall began on cp2, on
    ai-node2, and not on the disk the VM shares (F14). Teardown took 16 s, and nothing was left
    behind.
  - **Other new alerts, logged and not acted on:**
    - `KubeQuotaAlmostFull` on the spike's own quota;
    - `InfoInhibitor`;
    - `CloudlabAsleep`, with `TargetDown` for the cloudlab exporters, which are off at night by
      design.
- **KubeVirt's own control plane:** virt-operator and virt-controller restarted with `Error` at
  16:50, 18:25 (virt-operator only), 20:35 and 22:35Z.
  - Each time matches a control-plane disk stall: cp1 at 16:50 and 18:25Z, and cp2 at 20:35 and
    22:35Z (API server p99 25 s at 22:34Z). That is the lease-loss signature of the 13:33Z wave.
  - virt-api and virt-handler did not restart.
  - Run 2's VM was not affected: its 18:29Z check was healthy.

**S9 detail (2026-11-10, 07:14Z).**
- **Cleanup:** `kubectl delete -f 30-workload.yaml` (the second render) deleted `relay-kv-spike`
  with its PVC, quota and policies. The `local-path` PV (reclaim policy `Delete`) and its directory
  on `relay-worker-1` were gone within the 7 s the delete took.
- **KubeVirt removal:** the CR (`20-kubevirt-cr.yaml`) first, then the operator bundle
  (`10-operator.yaml`), with the default `uninstallStrategy`. It took 29 s. Nothing KubeVirt's
  remained:
  - no CRD;
  - no webhook configuration;
  - no APIService;
  - no cluster role or binding;
  - no priority class;
  - no `virt-*` pod.
- **Node metadata:** KubeVirt had added 454 labels (`*.node.kubevirt.io/*`, `kubevirt.io/*` and
  `cpumanager`) and two annotations (`kubevirt.io/heartbeat` and
  `kubevirt.io/ksm-handler-managed`) to `relay-worker-1`. Removing KubeVirt left them there, so
  the operator removed them. The node's labels now equal the 9 in the pre-S1 snapshot.
- **cert-manager:** the CRD deletion OOM-killed `cert-manager-cainjector` at 07:14:40Z (F15).
  At 07:18:27Z it restarted again: it lost its leader lease in an etcd stall on cp2 that began
  2–3 minutes after the removal's writes had ended (F14).
- **Preflight after removal** (collector `main@f569080d`, 07:14:55Z): the same as the pre-S1 run,
  check for check.
  - Both gates pass.
  - `kubevirt.installed` fails and `kubevirt.kvm_device` is unknown, because KubeVirt is gone.
  - Every capability is still `pending`.
  - Snapshot: `62b07d32f4ff34163a2fd6ab5e13ea1dcd955756655968f1f18f4209e4528a33`.
  - Report: `7de19c187c2075b4af5edec636a453fed21285f8fa0d6685723658139bc0ea9e`.

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
| F11 | Memory exhaustion stays inside the guest. In soak run 1 an uncapped Redis filled the 7.75 GiB guest. The guest kernel OOM-killed only `redis-server` (15:42, 15:54 and 16:19Z). Nothing else restarted, and the guest never rebooted. The node and the protected workloads were unaffected. The launcher stayed under its limit: 8.108 GiB at peak against 8.475 GiB, 374 MiB of headroom, because QEMU keeps every page the guest has touched. Launcher ephemeral storage follows the high-water mark of guest writes. It reached 12.49 GiB while the two guest filesystems it is charged for (root and the cache disk; the workspace is a PVC) held at most 3.3 GiB. The repeated Redis saves wrote new blocks, and the space was not given back while the VM ran. The 30 Gi limit is sized for that growth. | A full guest is the guest's problem, not the node's. While the guest thrashed, the guest agent and the readiness probe stopped answering for minutes, and VMI `Ready` stayed `True` (F9). | The runbook now requires a bounded soak load. Relay design: a worker health signal must tell a thrashing guest from a dead one. |
| F12 | `relay-worker-1` shares ai-node3's `local-lvm` NVMe, which has no power-loss protection, with `talos-cp3` and two CI runners. In soak run 1 the VM wrote 29–36 MB/s (30-minute averages, Proxmox RRD). While the guest thrashed (15:30–16:25Z) it also read 330–360 MB/s. cp3's etcd backend-commit p99 (5 minutes) was 0.008–0.11 s from 12:00 to 14:30Z, and 0.17–0.62 s from 15:00Z until the stop. `etcdHighCommitDurations` fired on cp3 at 14:48Z and 15:53Z, its first firing in 7 days. | **Not proven to be the VM.** After the VM stopped, cp3 stayed at 0.008–0.03 s for about 15 minutes, then spiked to 0.27 s at 16:56Z without it. cp1 and cp2, on other hosts, spiked the same way in the same hour. The 13:33Z `ControlPlaneRestartWave` came from cp2 on ai-node2, not from this host: WAL fsync p99 0.95 s and apiserver p99 6.4 s, and the containers that restarted were leader-elected controllers, which is the lease-loss signature. **Run 2, with the load capped, separates the two.** The VM wrote a steady 33 MB/s (mean; at most 43) and read almost nothing. ai-node3's iowait stayed under 10 %. cp3 had 13 WAL fsyncs over 1 s in 177 minutes, against 10 and 18 in the same hours on the two days before. 8 of the 13 came at 18:26Z, during a larger stall on cp1 on another host (69 between 18:24 and 18:31Z). In run 1 cp3 had 48 in 276 minutes, against 14 the day before. The uncapped thrash plausibly hurt cp3; the capped load did not measurably. | Run 1 was stopped under the abort rule, and run 2 watched cp3's alerts. Relay design: a VM worker on a host disk shared with a control-plane node needs an I/O limit on the worker's disk, or the planned move of the CP disks to dedicated drives with power-loss protection. The capped S4 load alone writes about 2.9 TB a day, so the limit should cover bandwidth as well as latency. |
| F13 | The run-1 soak loop checked the node, kubelet, guest and the Relay and router pods, but not Prometheus alerts, which the runbook listed as an abort condition. The listed rule, "any control-plane alert", is not usable anyway: cp1 and cp2 raise `etcdHighCommitDurations` most days. | cp3's alert went unnoticed for about 1 h 45 min. | The runbook's abort section now defines a concrete alert watch, and S8 requires it in the soak loop. Run 2 used it. |
| F14 | The alert watch's "any etcd leader change" stop cannot tell where a stall came from. Run 2 stopped on a leader change that began on cp2's disk, on ai-node2. cp2 had a WAL fsync p99 of 3.6 s, 21 fsyncs over 1 s between 20:32 and 20:35:30Z, and heartbeat failures. ai-node2's iowait reached 16 % while dev-worker-2 (20–45 MB/s) and ci-runner-10 (16–48 MB/s) wrote heavily. cp3, on the shared disk, had no fsync over 1 s then and became the leader. At 1-minute resolution there were four leader changes from 2026-11-01 to 11-09: 11-02 00:18Z, 11-03 03:08Z, 11-09 09:37Z and run 2's at 20:34Z. None began on cp3. The 09:37Z one fell inside S1, but it began on cp1 (ai-node1) at 09:35Z, before S1's first apply at 09:36:13Z. cp1 had 67 fsyncs over 1 s between 09:34 and 09:40:30Z. On 11-10 at 07:18Z cp2 stalled again (fsync p99 8.2 s, 23 fsyncs over 1 s in four minutes, no leader change), with ai-node2's iowait at 22 % and dev-worker-2 writing up to 72 MB/s. | A soak can be stopped by a stall on another host, and neither soak run reached 8 hours (D4, D5). cp2's stalls on 2026-11-09 and 11-10 both came while dev-worker-2 was writing heavily on the same host. That is a cluster risk outside the spike. | The runbook records the limit. Before the next soak, decide whether a leader change or a restart wave counts only when the CP on the worker's disk lost leadership or had a WAL fsync over 1 s in the same 10 minutes. Run 2 would not have stopped under that rule. Dev-worker infrastructure is frozen until the owner approves the Relay workers, so the dev-worker-2 pairing is recorded only. |
| F15 | Installing and removing KubeVirt each OOM-killed `cert-manager-cainjector` on cp3. cainjector watches CRDs for CA injection. Its memory limit is 128 Mi (`kubernetes/apps/infrastructure/cert-manager/helmrelease.yaml`), and before S1 it was at 95 % of it. After the CR apply (09:37:38Z) it thrashed for about 6 minutes, with about 900 major page faults a second and 2.2 GB/s of reads from ai-node3's shared NVMe, then restarted at about 09:45Z. The node itself had 17.5 GiB available. S9's CRD deletion OOM-killed it at 07:14:40Z. It has restarted 15 times in 31 days. Prometheus holds four of them; the other two are 2026-11-02 00:20Z, two minutes after a leader change, and 11-10 07:18Z (F14). | The read burst competed with cp3's etcd for the disk: cp3's fsync p99 was 0.44 s at 09:39Z. A Flux install or removal of KubeVirt would do the same. | Record only; cert-manager is not changed here. Before any KubeVirt adoption, raise cainjector's memory limit, or narrow what it watches, in a reviewed cert-manager change. |

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
- **D3, Redis cap in soak run 1.** At about 16:25Z the operator recreated the guest's `cache`
  service with `maxmemory 1gb` and `allkeys-lru` and dropped its data volume. That ended an
  out-of-memory loop that the test load had caused (F11). The cap is a `compose.override.yaml` on
  the workspace PVC. One guest-exec attempt reported a failure but had run anyway. Its retry found
  the removal in progress, and both ended in the same state (`s8-soak-redis-cap.log`). The S4
  recipe, with the override, is in `s4-recipe/`.
- **D4, soak re-run.** The operator stopped run 1 at 16:36:42Z under the abort rule, after 4 h
  36 min (F12, F13). The owner approved a re-run the same evening. It used the alert watch that is
  now in the runbook, which was not yet reviewed then. Qwen stayed stopped for the re-run.
- **D5, S8 accepted without an 8-hour soak.**
  - Run 2 started at 17:38:23Z with the Redis cap in force and the alert watch in the loop.
  - The watch stopped it at 20:35:54Z, after 2 h 57 min, on an etcd leader change that began on cp2
    (F14).
  - On 2026-11-10 the owner accepted S8 on the evidence of both runs, 7 h 33 min under load in
    total, and asked for it to be recorded as this deviation. No third run was made.
  - The deviation rests on two things:
    - every pass criterion held in both runs;
    - run 2's memory and storage were flat after its first 30 minutes.

    It does not show what a single 8-hour run would add: any run past 4 h 36 min, and the capped
    load past 3 hours.

## Qwen stop and restore (S7/S8)

`llama-swap-qwen38` on LXC 5003 was healthy (HTTP 200 on `:8082/health`) with no model loaded.
It was stopped at 2026-11-09T10:44:56Z under the owner's approval of 2026-11-08.
It was started again at 2026-11-09T20:38:11Z, after run 2's teardown. At 20:38:20Z the service
was active, `/health` returned HTTP 200, and no model was loaded, which is the same state as before
the stop. It was down for 9 h 53 min (`qwen-stop-restore.txt`).

## Evidence artifacts

| Artifact | What it is | SHA-256 |
|---|---|---|
| `SHA256SUMS` | The checksums of the other 134 files. Its own digest is the receipt for the whole set. | `341ed1442af5eca864f5c075bea77e54f3a97b5ec341bac4dc219a59d5edfa18` |
| `kv-spike-bundle-r2/10-operator.yaml` | KubeVirt operator bundle, applied in S1 and deleted in S9 | `e4b2d748b31596b2d740c85808e2138e0254e67230619e26ed05bf3420a00606` |
| `kv-spike-bundle-r2/20-kubevirt-cr.yaml` | KubeVirt CR | `7d9f26e80c84966aae0411c6b437cd2571687850eab12ab7bf2ef3b2ea02e3b5` |
| `kv-spike-bundle-r2/30-workload.yaml` | Workload, second render (D2); its delete was the S9 cleanup | `3a0ce9a352d9ccfca7c0fe3fb99f6b4cbbe08574dc5bd3f3aee186669f9e6ad5` |
| `kv-spike-bundle-r2/vm-only.yaml` | The VM alone, for S7 and S8 | `d3d0ee596c4fd504cf9fd7ec540ece5bbf24bc06971933b7288d767842aa8e8d` |
| `relay-worker-preflight-20261109T093527Z.json.report.json` | Preflight report before S1 | `8d57adb45b63f650f3a09b02aae8dafbcd4202fc35fe3a2d9d2052d4cd7703b1` |
| `s1-preflight-20261109T093959Z.json.report.json` | Preflight report after S1 | `9a9bfedb7ddb3dcf8626d6bdd1cf5f82276055923f4f5c351a57a1c0a594383e` |
| `relay-worker-preflight-20261110T071455Z.json.report.json` | Preflight report after the KubeVirt removal | `7de19c187c2075b4af5edec636a453fed21285f8fa0d6685723658139bc0ea9e` |
| `s1-install.log` | S1 install timings | `4392c115b495376c9d495aae5dfea3919b3750294433eec44befeaffdd4954f9` |
| `s1-checks.log` | S1 checks | `e5a98ed460baee9b666010e48d2a1da1b1b1a809c0fa0e833a9bf2dd1ee7e23e` |
| `s2-boot.log` | S2 boot | `b9a64b299a8569c166bcb145d30b2efe493b511c1b5d02c3fca1d878c1f09694` |
| `s2-launcher-summary.txt` | S2 launcher QoS, resources and security | `cf3b0944dd3f3487cf43416704b309966e5411f8b7dcfef6820ff1534344a9b5` |
| `s3-eviction.log` | S3 eviction | `034cc3e72b12c6e6fb5a4ff461fa8cb8d90b62657e26c7f2d9c7602f83569956` |
| `s3-workspace-persistence.log` | S3 workspace persistence | `654b37de76142c835294e7ac184fc86f7180a0c9da2cfc9e566632a22ed69da7` |
| `s4-summary.txt` | S4 Compose summary | `caefb2a2aa1276cfbb3f55ac8c63a30341df5bc6dab76938fac08ad26f61c1e5` |
| `s4-recipe/manifest.txt` | S4 recipe, with the Redis cap override (D3) | `598f5342f3c1e6c8c4fac73210373de4612b377f5c703f9ea63b7df1a84efa02` |
| `s5-network.log` | S5 egress and inbound tests | `637c0f376e0931df3233b0ba11b0974c2e62aef26348f4344f923901c6341c86` |
| `s6-stop-delete.log` | S6 stop and delete | `e2761887e960e9c55fe2671b661f52e8df1d08b56d1f9a5d79f631531598bbd2` |
| `s7-node-loss.log` | S7 node reset | `051959ab06bc6c7ae018c8a7994b6f5d753766afb09b84bdb78363a2172b4432` |
| `s8-cycles-summary.txt` | S8 20 cycles | `15cdd53d7215f6275896d5a66d8fb40d084a868ea0b8ae06e0cccd7c13fb4bb9` |
| `s8-soak-summary.txt` | S8 soak run 1 summary | `33650d8fa61806a718ae948edbd936a02e671808ca595e10b10bbfcb9556f72c` |
| `s8-soak-health.jsonl` | S8 soak run 1 health checks and stop | `d2b71ef4a88293e0cf5db67821e49e42e5941a859954592cf430ef493121d839` |
| `s8-soak-memexhaust-note.txt` | Run 1 out-of-memory loop (F11) | `c76787bf1a5e8987614a5e777d81e156916d422f29628736c0d71b88112cc126` |
| `s8-soak-alerts-investigation.log` | Run 1 alert and etcd queries (F12) | `6f3f0eecf9381c1eb4158bafb6afb2e1513129c4dba2284586cd87951a646e65` |
| `s8-soak2-summary.txt` | S8 soak run 2 summary | `2816880802012ab8e57ee7a3bcc538af4c2b6cce39e49f659715bd298bea4e82` |
| `s8-soak2-health.jsonl` | S8 soak run 2 health checks, alert watch and stop | `143f5a76500428a396ebaed1399aa8c00df91c05061fa5d606612fa7f99a7ad9` |
| `s8-soak2-abort-note.txt` | Run 2 stop analysis (F14) | `d87d50f8006a56d87a2c524bca939fc9e614015053dcf6bd940ca3c18c1116d9` |
| `s8-soak2-leader-investigation.log` | Run 2 leader change, from Prometheus | `bed417c1c236b9697da9c933c9ecc6dfd5dfd35dafa4a25dc3e0d704790a446d` |
| `s8-soak2-pve-io-summary.txt` | Proxmox per-guest I/O for runs 1 and 2, all three hosts | `159283328b2f3e9e065e83d0b9273ee27d8760bd04e304bb9c7b05fb1bc2e0d4` |
| `etcd-slow-fsync-exact-counts.txt` | Exact counts of WAL fsyncs over 1 s per CP, for every window the record quotes | `4cabb33e7ee088e23ffe4e2873d897ff87ca43d2a3bfcbf28715dfc7f30d8750` |
| `etcd-leader-changes-2026-11-01-to-09.txt` | Every leader change, 2026-11-01 to 11-09 | `05113871ee4e20bf858b55c94a908305ec97cfbe43078fb711dcf46c5bdf1310` |
| `s1-leader-change-investigation.log` | The 09:37Z leader change during S1 (cp1) | `08c966ba174cd3e45d6935ab57bff2ac59408c2c1caa2481dc9b4af3f6fae0a2` |
| `kubevirt-restart-history.txt` | KubeVirt component restarts | `4fce0495dab5f7c89d31c010c6256ac5a5be4b7fbd0ae64c47aa4ecfe8d6fe3a` |
| `kubevirt-restart-vs-etcd.log` | CP stalls at each virt-operator restart | `9a63a8bd2ab9a184f7b4a8821f539d232dbb650d76917ead668c619f99f2f10b` |
| `s1-cp3-read-burst-source.log` | cainjector thrash on cp3 during S1 (F15) | `d14d4d85c87ddce59662ed79b051c90f1410e78c696f016627f24433c2b9a46b` |
| `cainjector-restart-history.txt` | cainjector restarts and memory (F15) | `f03107828e584e4b5d7e42ab3682368ad7a0fa0d20dfbdcfc418ba683c693608` |
| `cainjector-state-20261110.txt` | cainjector state and limit after S9 (F15) | `2670ef1ca8d048e6907d9a0fd96e0028e7d7406bafaaef11c7cfad7b20a04bc8` |
| `s9-cleanup.log` | S9 cleanup, KubeVirt removal, node metadata and post-removal preflight | `4ccd06c4f7684fe15a97e26e1d3642875f41da3f039376c859bcacf8740a4456` |
| `s9-etcd-stall-investigation.log` | The 07:18Z cp2 stall after S9 (F14) | `d770966665a6ca2345cc3b17c77e0fc1b8c4fd36633a7c15764c07b3fc0b0fb3` |
| `qwen-stop-restore.txt` | Qwen stop and restore | `17d16f6a506e6b8da974e9dc965b6fe11c2a10e44b1feedaab535eba9bd73291` |
| `scripts/alertwatch.py` | Alert watch used in run 2 | `0411b2186c69731cc1a1456a7bbd5f1e0647e88ade9b218fbe48d4d3916e88f1` |
| `scripts/s8-soak2.sh` | Run 2 soak loop | `72de192b0cfd46bc3d5b90838eae4d523affd5f21f7479082bd23fc4ad91049e` |
| `scripts/s9-cleanup.sh` | S9 and removal script | `db2f7e14e42f6343f3c0c25597b13f8548bb3456d2e32ab4c14997ecfb8e0774` |

The other 92 files are listed only in `SHA256SUMS`. They include the raw samples, the
per-minute Proxmox exports, and every script and query file.
