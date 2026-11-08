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
| Repo-derived estate against the example binding | anyone, offline | `evaluate` on `scripts/tests/fixtures/relay-worker-preflight/snapshot-repo-derived.json` (synthetic, from manifests) | `fail`: no dedicated pool; dedicated gVisor RuntimeClass absent; KubeVirt absent (candidate pinned separately); synthetic cluster UID differs from the live pin |
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
| K0 | **Dedicated capacity.** A new Talos worker for Relay workers, labelled `ailab.io/relay-worker-pool=true` and tainted `dedicated=relay-worker:NoSchedule`. | Operator proposal: `relay-worker-1`, vmid 4304 on ai-node3, **12 vCPU / 24 GiB fixed / 200 GiB local-lvm**, `cpu.type=host`. Not provisioned or approved. A 32 GiB allocation needs RAM freed first. Use a separate OpenTofu map so existing agent-pool labels never apply. Proposed `.52` needs a UniFi lease check and owner-approved DHCP start move `.52` → `.53`, reflected in `kubernetes/edge/gateway.yaml` and `docs/network-plan.md`. **Add the new node's `/32` to `kubernetes/edge/hostnames.yaml` `trusted_sources.exclude` before admitting workloads**: SNAT must not give worker code admin-device access. See measured capacity and S7/S8 prerequisite below. |
| K1 | **Version selection.** Choose one KubeVirt release whose published support matrix lists Kubernetes **1.35** (live `v1.35.9`, `kubernetes/infra/agent-nodes/variables.tf`). Check it against the Talos v1.14 KubeVirt guide (docs.siderolabs.com/talos/v1.14/advanced-guides/install-kubevirt). | Record the release tag, the `virt-operator` image **digest** and the SHA-256 of `kubevirt-operator.yaml` / `kubevirt-cr.yaml`. Put the tag and digest into the binding (`pins.kubevirt_version`, `pins.kubevirt_operator_digest`). Until then the collector reports `kubevirt.pin: unknown`. Candidate v1.9.1 and upstream/digest pins are recorded in `docs/relay-workers/runtime-pins.json`; its support matrix includes Kubernetes 1.35. This is a version selection only. Build and review the pinned, dedicated-placement spike bundle before S1; no live compatibility is established. |
| K2 | **Nested virtualization** on the Proxmox host of the K0 node. | Operator confirmed `kvm_amd=1` on all three hosts on 2026-11-08 (`check-nested-virt.py`: PASS); no enablement change is needed. Recheck before provisioning. Enabling it is the Ansible `pve_base` path in `docs/runbooks/agent-nodes.md`. |
| K3 | **Talos schematic** for the K0 node. | Follow the Talos guide's KubeVirt requirements, including the kernel modules and any extensions it lists for 1.14. If the schematic changes, record a new entry in `kubernetes/infra/talos-schematics.yaml` and use the upgrade wrapper. Do not touch agent-node-3's `kata_gvisor` schematic. The dedicated pool also needs its own gVisor RuntimeClass and matching schematic before `pod.gvisor` can qualify; the existing `gvisor` class deliberately selects the protected agent pool. |
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
workloads and cannot be repurposed. This is a capacity proposal, not an allocation.

**Before S7/S8:** the operator recommends temporarily stopping `llama-swap-qwen38` on
LXC 5003, subject to owner approval, because its lazy model can otherwise consume another
23.5 GiB of GTT. Record approval, stop/restore owner and restoration evidence; do not start
those steps until this impact is agreed. No existing model service is changed by this PR.

Re-run the live preflight after K0–K4. `kubevirt_spike_prerequisites` must be `pass` before S1.

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
