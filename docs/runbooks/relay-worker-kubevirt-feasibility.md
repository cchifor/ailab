# Runbook: KubeVirt feasibility spike for Relay developer workers (operator-run, NOT yet run)

**Status: live evidence PENDING.** Nothing in this runbook has been executed against ailab. Nothing
here installs KubeVirt through Flux. The Relay KubeVirt renderer stays unbuilt until this spike
produces a passing evidence record (Relay plan `docs/plans/2026-11-08-infrastructure-workers/plan.md`,
phases 1 and 5).

This work is **new**. It does not revive the retired test pool (ADR 0037) or AgentForge (ADR 0041).
Do not reuse their names, namespaces, node labels, LUNs, OpenBao paths or Gitea identities: no
`testpool`, `agent-sandbox`, `tep`, `env-node`, `agentforge-*` or `af-*` names for new objects.

## What can be checked now, and by whom

| Check | Who | Tool | Today |
|---|---|---|---|
| Collector parsing, gate logic, fail-closed paths | anyone, offline | `python3 -m unittest scripts/tests/test_relay_worker_preflight.py` | passes (49 tests) |
| Repo-derived estate against the example binding | anyone, offline | `evaluate` on `scripts/tests/fixtures/relay-worker-preflight/snapshot-repo-derived.json` (synthetic, from manifests) | `fail`: no dedicated pool; gVisor pinned to the Relay/router agent pool; KubeVirt absent and unpinned; cluster UID unpinned |
| Live cluster preflight | operator with `admin@ai` read access | `collect` + `evaluate` | **not run** |
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
| K0 | **Dedicated capacity.** A new Talos worker for Relay workers, labelled `ailab.io/relay-worker-pool=true` and tainted `dedicated=relay-worker:NoSchedule`. | Needs a reviewed OpenTofu change, a vmid and a **network address**. The registry `docs/network-plan.md` (at `6881689b`) records "Free static space: NONE" in `.2`–`.51`, and allocation follows its procedure (a release or a deliberate DHCP-pool shrink). This runbook has not re-verified the segment; the operator does that at allocation time. Size it from the RAM budget in `docs/runbooks/dev-workers.md`. The suggested floor is 12 vCPU / 32 GiB fixed / 200 GiB local disk, which allows one developer VM plus overhead. The binding's headroom block takes the final figure. `cpu.type=host` is required. |
| K1 | **Version selection.** Choose one KubeVirt release whose published support matrix lists Kubernetes **1.35** (live `v1.35.9`, `kubernetes/infra/agent-nodes/variables.tf`). Check it against the Talos v1.14 KubeVirt guide (docs.siderolabs.com/talos/v1.14/advanced-guides/install-kubevirt). | Record the release tag, the `virt-operator` image **digest** and the SHA-256 of `kubevirt-operator.yaml` / `kubevirt-cr.yaml`. Put the tag and digest into the binding (`pins.kubevirt_version`, `pins.kubevirt_operator_digest`). Until then the collector reports `kubevirt.pin: unknown`. No version is chosen here: none was verified against this cluster. |
| K2 | **Nested virtualization** on the Proxmox host of the K0 node. | `python scripts/check-nested-virt.py <host>` must exit 0. Enabling it is the Ansible `pve_base` path in `docs/runbooks/agent-nodes.md`. |
| K3 | **Talos schematic** for the K0 node. | Follow the Talos guide's KubeVirt requirements, including the kernel modules and any extensions it lists for 1.14. If the schematic changes, record a new entry in `kubernetes/infra/talos-schematics.yaml` and use the upgrade wrapper. Do not touch agent-node-3's `kata_gvisor` schematic. |
| K4 | **Pin the cluster UID.** Copy `kubectl --context admin@ai get ns kube-system -o jsonpath='{.metadata.uid}'` into `cluster.expected_kube_system_uid`. | This is the identity check that keeps the collector off the wrong cluster. |
| K5 | **Cleanup owner** named in the spike record (person and date). | Owns the S9 teardown, and any stuck VM, PVC or launcher pod, until the inventory below is clean. |
| K6 | **Evidence store** outside git, plus the SHA-256 of each artifact. | The operator names the path explicitly as `EVIDENCE_DIR`; `/tmp` does not count. On a dev-worker, retained evidence goes under `/workspace/archive/`. Raw logs and metrics stay outside the repo. Only the digests come back into a binding as `receipt_sha256`. |

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
- **Guest:** 4 vCPU and 8 GiB memory. The guest must not be overcommitted.
- **Workspace:** one PVC of at most 20 Gi on `local-path`, attached as a virtio block disk.
- **Docker cache:** an `emptyDisk` of 20 Gi.
- **Root disk:** a `containerDisk` with a digest-pinned image.
- **Network:** pod network with `masquerade` only.
- **ResourceQuota on `relay-kv-spike`:**
  - `requests.cpu: 6`, `requests.memory: 12Gi`, `limits.memory: 12Gi`
  - `requests.storage: 20Gi`, `persistentvolumeclaims: 1`
  - `requests.ephemeral-storage: 30Gi`
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
   - Fill the Docker cache disk to its bound. The guest must get an ENOSPC error, and node
     ephemeral storage must stay inside quota.
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
