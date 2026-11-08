# Runbook: KubeVirt feasibility spike for Relay developer workers (operator-run, NOT yet run)

**Status: read-only preflight collected; runtime spike PENDING.** The operator ran the preflight
on 2026-11-08; both gates failed because the dedicated pool does not exist. S1–S9 remain not run.
Nothing here installs KubeVirt through Flux. The Relay KubeVirt renderer stays unbuilt until this spike
produces a passing evidence record (Relay plan `docs/plans/2026-11-08-infrastructure-workers/plan.md`,
phases 1 and 5).

This work is **new**. It does not revive the retired test pool (ADR 0037) or AgentForge (ADR 0041).
Do not reuse their names, namespaces, node labels, LUNs, OpenBao paths or Gitea identities: no
`testpool`, `agent-sandbox`, `tep`, `env-node`, `agentforge-*` or `af-*` names for new objects.

## What can be checked now, and by whom

| Check | Who | Tool | Today |
|---|---|---|---|
| Collector parsing, gate logic, fail-closed paths | anyone, offline | `python3 -m unittest scripts/tests/test_relay_worker_preflight.py` | passes (52 tests) |
| Repo-derived estate against the example binding | anyone, offline | `evaluate` on `scripts/tests/fixtures/relay-worker-preflight/snapshot-repo-derived.json` (synthetic, from manifests) | `fail`: no dedicated pool; gVisor pinned to the Relay/router agent pool; KubeVirt absent and unpinned; synthetic cluster UID differs from the live pin |
| Live cluster preflight | operator with `admin@ai` read access | `collect` + `evaluate` | **collected 2026-11-08; gates fail** (evidence below) |
| Spike S1–S9 below | operator | KubeVirt, talosctl, Proxmox | **not run** |

A dev-worker cannot run the live collector. Its `platform` identity is namespaced, cannot list
nodes, and its helper must not be widened for this. In that context the collector reports
`unknown`, which is the correct result. Do not look for other credentials.

## The read-only preflight

```bash
# Operator workstation. The context is explicit. The default context is home-lab, a different cluster.
# EVIDENCE_DIR is required and explicit: the K6 evidence store, outside the git checkout, never /tmp.
# (On a dev-worker, retained evidence goes under /workspace/archive/<task>/, per the worker's disk
#  hygiene rules. A dev-worker can only produce an `unknown` snapshot, see above.)
: "${EVIDENCE_DIR:?set EVIDENCE_DIR to the K6 evidence store}"
SNAP="$EVIDENCE_DIR/relay-worker-preflight-$(date -u +%Y%m%dT%H%M%SZ).json"
python3 scripts/relay-worker-preflight.py collect --context admin@ai --out "$SNAP"
python3 scripts/relay-worker-preflight.py evaluate --snapshot "$SNAP" \
    --binding docs/relay-workers/ailab-binding.example.json --json > "$SNAP.report.json"
sha256sum "$SNAP" "$SNAP.report.json"
```

- **Queries:** only `kubectl get` and `kubectl version`, for kube-system's UID, nodes, pods,
  StorageClasses, RuntimeClasses, the Cilium DaemonSet, and the KubeVirt CRD and CR. There are no
  Secrets, exec, port-forward, proxy or writes. The snapshot keeps only the fields that the
  evaluation reads; pod env, args and annotations are dropped before anything is written.
  kubectl's stderr is **never** stored or printed, because exec auth plugins and provider errors can
  echo credentials. A failed query records only a bounded class: `forbidden`, `unauthenticated`,
  `notfound` or `error`.
- **Exit codes:** `0` means every check passed, `1` means at least one check failed, and `2` means no
  check failed but at least one is `unknown`. Missing permissions, a missing context or an
  unparseable answer produce `unknown`, never `pass`.
- **What `pass` means:** a prerequisite is visible. The report always says `qualification:
  not-established-by-preflight`. Capability status comes only from the binding. A binding that
  marks a capability `qualified` without a 64-hex `receipt_sha256` of the live evidence record is
  rejected.
- **Headroom:** this is the scheduler's view, allocatable minus the effective requests of
  non-terminal pods. Pod-level `spec.resources` and restartable init sidecars are accounted for.
  A malformed request makes the result `unknown`. It is not a measurement of load.
- **Scope: dedicated-pool only.** This collector has no co-location mode.

### Gates the report makes explicit

- `lightweight_canary_prerequisites` needs all of these:
  - the context, API server and kube-system UID match the binding;
  - Kubernetes is at its pin;
  - at least one **dedicated** node is Ready and amd64, carries `ailab.io/relay-worker-pool=true`
    and the `dedicated=relay-worker:NoSchedule` taint, and has **no** `ailab.io/agent-pool` or
    control-plane label;
  - no live pod in a protected namespace (`relay`, `llm-router`) on those nodes. If the pods cannot
    be read, this is `unknown`;
  - Cilium is at its pin;
  - lightweight request headroom exists on that node;
  - the workspace StorageClass is not `qnap-iscsi`.

  Relay, the llm-router and the other existing 16 GiB agent nodes never satisfy this gate. The
  `gvisor` RuntimeClass selects `ailab.io/agent-pool`, so `runtimeclass.gvisor` fails. Co-locating
  workers with Relay or the router is not something this collector can approve. It would need
  measured evidence and a later, separately reviewed change.
- `kubevirt_spike_prerequisites` needs the same context and pool checks, the Cilium pin,
  developer-VM headroom, and a selected KubeVirt version **and** operator digest. The digest must
  have the form `sha256:` followed by 64 lowercase hex characters. When it passes, S1
  may **start**. It does not mean KubeVirt works.

## Operator prerequisites (external; each is a separately reviewed change)

| ID | Prerequisite | Notes |
|---|---|---|
| K0 | **Dedicated capacity.** A new Talos worker for Relay workers, labelled `ailab.io/relay-worker-pool=true` and tainted `dedicated=relay-worker:NoSchedule`. | **Owner-approved 2026-11-08 and declared** in `kubernetes/infra/relay-workers` (its own state, so no apply there can touch the agent nodes): `talos-relay-worker-1`, vmid 4304 on ai-node3, **12 vCPU / 24 GiB fixed / 200 GiB local-lvm**, `cpu.type=host`, at `.52` (DHCP pool start moved `.52` → `.53` in `kubernetes/edge/gateway.yaml`; `.52` excluded in `kubernetes/edge/hostnames.yaml` `trusted_sources.exclude`). Build order and checks: [Building K0](#building-k0) below. A 32 GiB allocation needs RAM freed first. Further pool nodes (one per host) wait for the owner's approval of the dev-worker replacement. |
| K1 | **Version selection.** Choose one KubeVirt release whose published support matrix lists Kubernetes **1.35** (live `v1.35.9`, `kubernetes/infra/agent-nodes/variables.tf`). Check it against the Talos v1.14 KubeVirt guide (docs.siderolabs.com/talos/v1.14/advanced-guides/install-kubevirt). | Record the release tag, the `virt-operator` image **digest** and the SHA-256 of `kubevirt-operator.yaml` / `kubevirt-cr.yaml`. Put the tag and digest into the binding (`pins.kubevirt_version`, `pins.kubevirt_operator_digest`). Until then the collector reports `kubevirt.pin: unknown`. No version is chosen here: none was verified against this cluster. |
| K2 | **Nested virtualization** on the Proxmox host of the K0 node. | Operator confirmed `kvm_amd=1` on all three hosts on 2026-11-08 (`check-nested-virt.py`: PASS); no enablement change is needed. Recheck before provisioning. Enabling it is the Ansible `pve_base` path in `docs/runbooks/agent-nodes.md`. |
| K3 | **Talos schematic** for the K0 node. | **Resolved 2026-11-08:** the Talos 1.14 KubeVirt guide lists no extension, kernel module or machine-config patch (KVM is in the Talos kernel; only nested virtualization and `local-path`, both present, are prerequisites). The node therefore runs the plain schematic `53513e54…` (infra `schematic_id`, the one agent-node-1/2 run), and `talos-schematics.yaml` is unchanged. agent-node-3's `kata_gvisor` schematic is untouched. The dedicated pool still needs its own gVisor RuntimeClass and a gVisor-capable schematic before `pod.gvisor` can qualify; the existing `gvisor` class deliberately selects the protected agent pool. |
| K4 | **Pin the cluster UID.** Copy `kubectl --context admin@ai get ns kube-system -o jsonpath='{.metadata.uid}'` into `cluster.expected_kube_system_uid`. | Pinned from the operator report to `faf3a521-bd08-4d6d-ba5c-9dc7da903819`; this is the identity check that keeps the collector off the wrong cluster. |
| K5 | **Cleanup owner** named in the spike record (person and date). | Owns the S9 teardown, and any stuck VM, PVC or launcher pod, until the inventory below is clean. |
| K6 | **Evidence store** outside git, plus the SHA-256 of each artifact. | The operator names the path explicitly as `EVIDENCE_DIR`; `/tmp` does not count. On a dev-worker, retained evidence goes under `/workspace/archive/`. Raw logs and metrics stay outside the repo. Only the digests come back into a binding as `receipt_sha256`. |

### Operator evidence, 2026-11-08

[Operator report on PR #1675](https://git.chifor.me/cchifor/ailab/pulls/1675#issuecomment-106374)
was collected from head `25b391de`: all nine queries succeeded; both gates failed. Kubernetes
v1.35.9, Cilium v1.19.8 and local-path/WaitForFirstConsumer matched; KubeVirt is absent.
The report predates the cluster-UID pin in this revision and is not a qualification receipt.

Evidence lives on the workstation at `C:\Users\chifo\work\home\evidence\relay-worker-kubevirt\`:

| Artifact | SHA-256 |
|---|---|
| `relay-worker-preflight-20261108T164446Z.json` | `674d3fe392b4ceb6b2b9737bd1ae26e6ee1e0545efce31188317cc7a7d9ee7c2` |
| `relay-worker-preflight-20261108T164446Z.json.report.json` | `7b477044793542229211fa0fe065463caf3987bceae45cc437490f400e349900` |

Host 7-day minimum MemAvailable was 2.5 GiB / 10.4 GiB / 33.6 GiB on ai-node1/2/3.
Only ai-node3 can accommodate the proposed 24 GiB fixed worker; 32 GiB would leave only
about 1.6 GiB at the observed low point. All existing agent nodes host protected Relay/router
workloads and cannot be repurposed. The owner approved the 24 GiB node on 2026-11-08 (K0).

**Before S7/S8:** the operator recommends temporarily stopping `llama-swap-qwen38` on
LXC 5003, subject to owner approval, because its lazy model can otherwise consume another
23.5 GiB of GTT. Record approval, stop/restore owner and restoration evidence; do not start
those steps until this impact is agreed. No existing model service is changed by this PR.

Re-run the live preflight after K0–K4. `kubevirt_spike_prerequisites` must be `pass` before S1.

### Building K0

Each step must succeed before the next. The edge exclusion goes live before anything holds `.52`.

1. **Merge, then confirm the edge is live.** Flux deploys the re-rendered Caddyfile as a new
   hash-suffixed `edge-proxy-caddy-*` ConfigMap, which rolls the `edge-proxy` DaemonSet. The ConfigMap
   the running pods mount must list `192.168.0.53/32 192.168.0.54/31` where it used to list
   `192.168.0.52/30`, in both the `@not_lan` and `@not_admin` matchers, and the rollout must be complete.
2. **Move the DHCP pool start.** `just edge-gateway-plan --only dhcp` must show exactly one op, the
   `dhcp` update of `Home` from `192.168.0.52-192.168.1.254` to `192.168.0.53-192.168.1.254`, and no error. The
   reconciler refuses to take back an address an active client holds; do not acknowledge such a
   conflict, find the client instead. Then `just edge-gateway-apply --plan-hash <hash> --only dhcp`,
   and commit `kubernetes/edge/gateway-state.yaml` if it changed.
3. **Stage the image on ai-node3:** `TALOS_VER=v1.14.2 NODES=192.168.0.4 bash scripts/stage-talos-image.sh`.
   Its default `SCHEMATIC` is the plain one, which is what `talos.tf` installs. The file name does not
   encode the schematic, and the script keeps an existing file ("present in import"), so only a fresh
   download proves which schematic was staged. It was fresh on 2026-11-08: the plain schematic
   `53513e54…`, 4 453 302 272 bytes. Step 5 confirms it on the booted node.
4. **Plan and apply.** Run tofu on Windows, from the main checkout (it reads `../terraform.tfstate`).
   `terraform.tfvars` is gitignored; it takes the same values as `agent-nodes/terraform.tfvars`.
   `just relay-workers-plan` must show exactly 2 to add (the VM and its machine-config apply),
   0 to change and 0 to destroy. Then `just relay-workers-apply` (it runs `nested-virt-verify` first).
5. **Verify.**
   - `talos-relay-worker-1` is Ready.
   - Its labels include `ailab.io/relay-worker-pool=true` and not `ailab.io/agent-pool`.
   - Its only taint is `dedicated=relay-worker:NoSchedule`.
   - No `relay` or `llm-router` pod runs on it.
   - `talosctl -n 192.168.0.52 ls /dev/kvm` shows the device.
   - `talosctl -n 192.168.0.52 get extensions` reports schematic `53513e54…` with exactly
     qemu-guest-agent, iscsi-tools and util-linux-tools, the same as agent-node-1. A different schematic
     means the staged image was not the plain one. Destroy and rebuild from a freshly staged image
     before any spike step: the first upgrade would otherwise change the node's extensions.
   - Re-run the preflight: `placement.dedicated_pool`, `placement.protected_workloads_absent` and
     both `headroom.*` checks pass. `kubevirt_spike_prerequisites` then fails or is unknown only on
     `kubevirt.pin` (K1).

**Deliberate differences from the agent nodes:**
- No storage-fabric `/32` route. Relay workers must not mount QNAP storage, and the route would give
  SNATed worker egress a path to the NAS storage plane.
- `cloud-power`'s `ALLOW_FROM` does not list `.52`. That alone does **not** keep worker code away
  from it: the list also admits the whole pod CIDR (`10.244.0.0/16`), and Cilium does not masquerade
  pod traffic to cluster nodes. A worker pod can therefore reach the `hostNetwork` WoL listener (`:8127`
  on every node, plus its Service). That listener only serves `POST /api/wake`, so it can power the GPU
  cluster **on**, never off; the power-off API sits behind its own NetworkPolicy. The pool rule below
  closes the gap.

**Pool rule: no workload before an egress policy.** Before the first workload on the pool (S2 for the
spike), its namespace carries a default-deny egress `CiliumNetworkPolicy` that allows only DNS and the
approved endpoints. That denies the LAN `192.168.0.0/23`, every node address (and with them the WoL
listener) and the `cloud-power` Services. S5 then proves it: the WoL path must be refused both through
the Service and through a node address.

**Known gaps:** `monitoring/alloy` (logs) and `velero/node-agent` do not tolerate the taint, so the
pool's pod logs are not in Loki and its volumes are not backed up. Spike evidence goes to `EVIDENCE_DIR`
(K6). Adding either is a separate change.

**Talos upgrades** reboot the node, and with no live migration every KubeVirt VM on it stops. It is
in the kubelet roll of `docs/runbooks/talos-upgrade.md`; schedule OS upgrades when no spike step is
running.

**Teardown** runs in reverse, one step at a time:
1. **Cleanup while the node still exists.** S9 (the K5 owner): the worker namespaces are deleted, and
   no VM, VMI, PV, `virt-launcher` pod or `local-path` directory remains. The `local-path` cleanup
   helpers need the node.
2. `kubectl --context admin@ai drain talos-relay-worker-1 --ignore-daemonsets`.
3. `tofu destroy` in `kubernetes/infra/relay-workers`. It deletes the VM and drops the config-apply
   record. It does **not** reset Talos and does **not** delete the Kubernetes Node.
4. `kubectl --context admin@ai delete node talos-relay-worker-1`. Confirm that its `CiliumNode` is gone
   and that nothing still targets it.
5. Only then, in one change: return `.52` to the pool (`dhcp_start: 192.168.0.52`), remove the `/32`
   from `trusted_sources.exclude`, and update `docs/network-plan.md`.

**Rebuild:** `talos_machine_configuration_apply` is `replace_triggered_by` its VM, so a replaced VM gets
its config re-applied and joins again. Delete the old Node object first (step 4), or the new kubelet
registers over a stale one.

## Spike procedure (operator-owned; all objects in a throwaway namespace)

**Names:**
- namespace: `relay-kv-spike`
- VM: `kv-spike-1`
- labels: `ailab.io/spike=relay-kubevirt`

KubeVirt itself goes in its own namespace, `kubevirt`. It is installed by hand from the pinned K1
manifests, not by Flux. If it is kept after the spike, a separate reviewed Flux PR adds it with the
same pins.

**Bounds:**
- **Guest:** 4 vCPU and 8 GiB memory. The guest must not be overcommitted. Set
  `spec.domain.memory.guest: 8Gi`, `spec.domain.resources.requests.memory: 8Gi` and
  `spec.domain.resources.limits.memory: 8Gi` explicitly. KubeVirt adds launcher overhead;
  do not copy guest limits directly onto the launcher. S2 must verify the resulting pod
  requests and limits are present and fit the namespace quota before proceeding.
- **Workspace:** one RWO PVC requesting at most 20 Gi on `local-path`, with
  **`volumeMode: Filesystem`**, exposed to the guest as a **virtio disk**. This is not a
  Kubernetes raw Block-mode PVC: `rancher.io/local-path` provisions directories and cannot
  supply raw block volumes. The PVC root must contain a raw disk image named `disk.img`.
  Prepare a bounded sparse raw image (initial maximum virtual size **18 GiB**, leaving space
  for filesystem overhead) with an operator-owned, digest-pinned preparation Job scheduled
  on the dedicated node. Verify its raw format, virtual size, and read/write ownership for
  the pinned KubeVirt launcher UID before removing the preparation Job and attaching the VM.
  Record the preparation image digest, `qemu-img info` result and effective launcher UID in
  evidence. `local-path` capacity requests do not enforce filesystem quotas: verify both
  guest virtual-disk bounds and actual node disk use during S3. If raw Block-mode storage
  becomes necessary, select and qualify a different CSI provisioner in a separate change.
- **Docker cache:** an `emptyDisk` of 20 Gi. Set VMI
  `spec.domain.resources.requests.ephemeral-storage: 30Gi` and
  `spec.domain.resources.limits.ephemeral-storage: 30Gi` explicitly, covering the 20 Gi
  cache, containerDisk copy and logs. Pin a root image whose expanded size fits the remaining
  allowance. Verify the rendered launcher request/limit and actual node headroom before boot;
  namespace quota alone does not cap writes, and sparse images can grow to their full size.
- **Root disk:** a `containerDisk` with a digest-pinned image.
- **Network:** pod network with `masquerade` only.
- **ResourceQuota on `relay-kv-spike`:**
  - `requests.cpu: 6`, `requests.memory: 12Gi`, `limits.memory: 12Gi`
  - `requests.storage: 20Gi`, `persistentvolumeclaims: 1`
  - `requests.ephemeral-storage: 32Gi`, `limits.ephemeral-storage: 32Gi` (includes preparation/launcher overhead; verify the final launcher fits)
- **Forbidden:**
  - `hostDisk`, `filesystems`/virtiofs, host devices, PCI or GPU passthrough
  - Multus or bridge networking
  - CDI clones, snapshots, `qnap-iscsi` and `nfs-csi`
  - live migration
  - console or VNC as a readiness signal

**Steps.** Record each step's outcome as `pass`, `fail` or `not-run`, with timestamps.

1. **S1, install.** Apply the K1 manifests by digest. Wait for the KubeVirt CR to reach `Deployed`.
   Re-run the collector:
   - `kubevirt.installed` must be `pass`;
   - `kubevirt.kvm_device` must be `pass` on the K0 node only;
   - `/dev/kvm` must come from hardware, not emulation: `useEmulation` stays unset.
2. **S2, boot.**
   - Create `kv-spike-1` with a node selector for the K0 node and a toleration for its taint.
   - Record the following:
     - the time to `Running`;
     - the time to **in-guest** readiness, from a guest-agent or readiness probe that runs inside the guest;
     - the virt-launcher pod's requests and limits, and its overhead relative to the guest;
     - the pod's QoS class.
3. **S3, storage.**
   - Write 1 GiB to the workspace disk, then stop and start the VM. The data must still be there.
   - Fill the Docker cache disk to its bound. The guest must get ENOSPC; measure node use
     against the launcher limit. Capture the actual launcher request/limit and kubelet eviction
     events. The 20 Gi cache alone cannot exceed the 30 Gi launcher limit. For the eviction test,
     the operator resolves the launcher's disk-backed emptyDir mount from its Pod spec and writes
     a separate disposable file there (outside `disk.img`, never on the workspace PVC). Bound
     that file so total measured pod use can exceed the limit by at most 1 Gi; monitor node free
     space and abort before the node's eviction reserve is reached. Record the chosen path,
     byte bound and observed eviction. Then recreate the VM and verify the workspace survives. Quota is admission accounting,
     not a filesystem quota: if kubelet does not account for the disk or enforce the pod limit,
     S3 fails. Do not claim a hard byte ceiling from quota alone.
   - Note that the PV is node-affine; that is expected.
4. **S4, Compose.**
   - Run one representative Compose stack in the guest, with no more than one stack per worker
     (ADR 0037).
   - Record peak guest RSS, node memory and disk high-water marks.
5. **S5, Cilium.**
   - Apply a `CiliumNetworkPolicy` to the launcher pod. Allowed egress: DNS and one approved
     endpoint. Deny everything else, the LAN `192.168.0.0/23` included.
   - From inside the guest, verify that the allowed connection succeeds and that the denied ones
     fail, by both IP and name.
   - Verify that an inbound connection from another namespace is denied.
6. **S6, stop/delete.**
   - Stop, then delete the VM.
   - Record the time until both the launcher pod and the VMI are gone.
   - Nothing may be stuck `Terminating`. Do not remove finalizers to force progress.
7. **S7, node loss under a live workload.**
   - **First, recreate the workload.** Create a fresh `kv-spike-1` that reuses the S3 workspace
     PVC. Wait for in-guest readiness and start the S4 Compose stack.
   - Record the guest's boot ID and the Compose container IDs as the pre-reset baseline. A node-loss
     test with nothing running proves nothing.
   - Hard-reset the K0 node from Proxmox (`qm reset`).
   - Expected results:
     - the VMI is reported failed or unknown, not silently recreated elsewhere;
     - the workspace PVC and its S3 data survive;
     - the node rejoins;
     - a fresh, explicit start after rejoin works, with a new boot ID. Compose state is recreated,
       not resumed;
     - no Relay or router pod is affected.
   - Then stop and delete the VM. Teardown must be clean, by the same S6 criteria.
8. **S8, soak.**
   - Run 20 create/boot/Compose/stop/delete cycles, then run the VM for 8 hours under sustained
     Compose load.
   - Pass criteria:
     - no guest freeze;
     - no kubelet or containerd wedge;
     - no teardown longer than 5 minutes;
     - memory on the K0 node stays inside its allocatable;
     - Relay and router stay healthy throughout.
   - This step tests the ADR 0037 freeze lesson: hung teardowns accumulate until the kubelet wedges.
9. **S9, cleanup.** The K5 owner deletes `relay-kv-spike` and then verifies all of the following:
   - no VMs, VMIs or DataVolumes remain;
   - no PVs bound to the namespace remain;
   - no `local-path` directories remain on the node;
   - no `virt-launcher` pods remain.

   KubeVirt either stays, until a reviewed Flux PR adopts it, or is removed with its own pinned
   manifests in reverse order. Either way, record the outcome.

**Result.**
- The spike passes only if S1–S9 all pass.
- If it passes, write an evidence record with the pins, timings and high-water marks, and the
  digests of the raw artifacts. Then a binding PR may move `vm.kubevirt`, and only if S4 passed
  `vm.compose`, to `qualified` with `receipt_sha256`.
- Any `fail` or `not-run` keeps the capability `pending`, and the developer template stays
  unavailable.

## Abort conditions (stop and clean up)

Stop the spike and run the cleanup if any of these happens:
- any Relay, router or control-plane alert;
- a kubelet NotReady that does not follow the planned S7 reset;
- etcd not at 3/3;
- the NAS reports iSCSI or NFS errors;
- a teardown is stuck for more than 15 minutes.

Recover a wedged node the way ADR 0037 describes: a `qm reset` of that node only. Do not run
`talosctl reboot` on it.
