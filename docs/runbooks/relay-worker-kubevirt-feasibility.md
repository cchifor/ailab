# Runbook: KubeVirt feasibility spike for Relay developer workers (operator-run, NOT yet run)

**Status as of 2026-11-09: dedicated node ready; both preflight gates pass; runtime spike PENDING.**
The operator provisioned `relay-worker-1` at `192.168.0.52` on ai-node3 with
12 vCPU, 24 GiB memory and 200 GiB disk; DHCP now starts at `.53`, and existing dev workers
are unchanged. The [rollout record](relay-worker-capacity.md#operator-rollout-record-2026-11-08)
supersedes the initial failed preflight retained below. Capacity PRs #1675, #1676 and #1681
are merged; #1677 was closed as a duplicate of #1676. **S1 (KubeVirt installation) is next;
S1–S9 and the eight-hour soak have no passing live evidence yet.** The operator owns the spike.
Install and verify the prescribed namespace egress policy **before the first workload or VM**.
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
| Live cluster preflight | operator with `admin@ai` read access | `collect` + `evaluate` | **operator-reported pass, collected 2026-11-08 at 18:16:37 UTC** ([receipt](#passing-operator-evidence-2026-11-08)); collector head awaiting confirmation |
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
| K0 | **Dedicated capacity.** A new Talos worker for Relay workers, labelled `ailab.io/relay-worker-pool=true` and tainted `dedicated=relay-worker:NoSchedule`. | Operator proposal: `relay-worker-1`, vmid 4304 on ai-node3, **12 vCPU / 24 GiB fixed / 200 GiB local-lvm**, `cpu.type=host`. Owner approved capacity/address allocation on 2026-11-08; **provisioned the same day**: `relay-worker-1` is Ready and the preflight's two gates pass ([rollout record](relay-worker-capacity.md#operator-rollout-record-2026-11-08)). A 32 GiB allocation needs RAM freed first. Use a separate OpenTofu map so existing agent-pool labels never apply. `.52` was lease-checked, and the controller's DHCP start moved `.52` → `.53` on 2026-11-08 (`kubernetes/edge/gateway.yaml`, `docs/network-plan.md`). **The node's `/32` is in `kubernetes/edge/hostnames.yaml` `trusted_sources.exclude`** (#1676, live in the edge proxy since `main@8c82fa1a`, before the node existed): SNAT must not give worker code admin-device access. See measured capacity and S7/S8 prerequisite below. |
| K1 | **Version selection.** Choose one KubeVirt release whose published support matrix lists Kubernetes **1.35** (live `v1.35.9`, `kubernetes/infra/agent-nodes/variables.tf`). Check it against the Talos v1.14 KubeVirt guide (docs.siderolabs.com/talos/v1.14/advanced-guides/install-kubevirt). | Record the release tag, the `virt-operator` image **digest** and the SHA-256 of `kubevirt-operator.yaml` / `kubevirt-cr.yaml`. Put the tag and digest into the binding (`pins.kubevirt_version`, `pins.kubevirt_operator_digest`). Until then the collector reports `kubevirt.pin: unknown`. Candidate v1.9.1 and upstream/digest pins are recorded in `docs/relay-workers/runtime-pins.json`; its support matrix includes Kubernetes 1.35. This is a version selection only. Build and review the pinned, dedicated-placement spike bundle before S1; no live compatibility is established. |
| K2 | **Nested virtualization** on the Proxmox host of the K0 node. | Operator confirmed `kvm_amd=1` on all three hosts on 2026-11-08 (`check-nested-virt.py`: PASS); no enablement change is needed. Recheck before provisioning. Enabling it is the Ansible `pve_base` path in `docs/runbooks/agent-nodes.md`. |
| K3 | **Talos schematic** for the K0 node. | Follow the Talos guide's KubeVirt requirements, including the kernel modules and any extensions it lists for 1.14. Recorded as `relay_workers` (`8e8827b5…`, gvisor + qemu-guest-agent) in `kubernetes/infra/talos-schematics.yaml`, and live on `relay-worker-1` since 2026-11-08. If the schematic changes, record the new entry there and use the upgrade wrapper. Do not touch agent-node-3's `kata_gvisor` schematic. The dedicated pool also needs its own gVisor RuntimeClass and matching schematic before `pod.gvisor` can qualify; the existing `gvisor` class deliberately selects the protected agent pool. |
| K4 | **Pin the cluster UID.** Copy `kubectl --context admin@ai get ns kube-system -o jsonpath='{.metadata.uid}'` into `cluster.expected_kube_system_uid`. | Pinned from the operator report to `faf3a521-bd08-4d6d-ba5c-9dc7da903819`; this is the identity check that keeps the collector off the wrong cluster. |
| K5 | **Cleanup owner** named in the spike record (person and date). | Owns the S9 teardown, and any stuck VM, PVC or launcher pod, until the inventory below is clean. |
| K6 | **Evidence store** outside git, plus the SHA-256 of each artifact. | The operator names the path explicitly as `EVIDENCE_DIR`; `/tmp` does not count. On a dev-worker, retained evidence goes under `/workspace/archive/`. Raw logs and metrics stay outside the repo. Only the digests come back into a binding as `receipt_sha256`. |

### Initial operator evidence, 2026-11-08 (before capacity rollout)

[Operator report on PR #1675](https://git.chifor.me/cchifor/ailab/pulls/1675#issuecomment-106374)
was collected from head `25b391de`: all nine queries succeeded; both gates failed. Kubernetes
v1.35.9, Cilium v1.19.8 and local-path/WaitForFirstConsumer matched; KubeVirt is absent.
The report predates the cluster-UID pin and dedicated-node rollout; it describes the earlier
state, not the current passing prerequisites, and is not a qualification receipt.

Evidence lives on the workstation at `C:\Users\chifo\work\home\evidence\relay-worker-kubevirt\`:

| Artifact | SHA-256 |
|---|---|
| `relay-worker-preflight-20261108T164446Z.json` | `674d3fe392b4ceb6b2b9737bd1ae26e6ee1e0545efce31188317cc7a7d9ee7c2` |
| `relay-worker-preflight-20261108T164446Z.json.report.json` | `7b477044793542229211fa0fe065463caf3987bceae45cc437490f400e349900` |

Host 7-day minimum MemAvailable was 2.5 GiB / 10.4 GiB / 33.6 GiB on ai-node1/2/3.
Only ai-node3 can accommodate the proposed 24 GiB fixed worker; 32 GiB would leave only
about 1.6 GiB at the observed low point. All existing agent nodes host protected Relay/router
workloads and cannot be repurposed. These measurements supported the subsequent owner-approved
24 GiB allocation recorded above; they are not runtime qualification evidence.

**Before S7/S8:** the operator recommends temporarily stopping `llama-swap-qwen38` on
LXC 5003, approved by the owner on 2026-11-08 with operator restoration afterwards, because its lazy model can otherwise consume another
23.5 GiB of GTT. Record the stop/restore owner and restoration evidence before closing S7/S8. No existing model service is changed by this PR.

The operator re-ran the live preflight after K0–K4 and both gates passed. Confirm that the
current report still matches the pinned cluster and dedicated pool before S1; a prerequisite
pass permits the spike to start and does not qualify KubeVirt.

### Passing operator evidence, 2026-11-08

[Operator rollout comment #106617](https://git.chifor.me/cchifor/ailab/pulls/1676#issuecomment-106617)
records both prerequisite gates passing after the dedicated-node rollout. The collection
was on **2026-11-08 at 18:16:37 UTC**, not the 2026-11-09 status-update date above.
The evidence directory is `C:\Users\chifo\work\home\evidence\relay-worker-kubevirt\`.

| Artifact | SHA-256 / provenance |
|---|---|
| `relay-worker-preflight-20261108T181637Z.json` | `56ca1b550d8d8ca7d0275c716be65f34c12999d130e8afab6faac77e1be468e1`, published in the operator comment |
| Corresponding report | The capacity rollout record retains only `6a7e126a…b77c`; the full digest and exact report filename still need operator confirmation from the evidence store. |
| Collector revision | Not attested in the retained comment. `main@b81ba53b` identifies the saved capacity plan/apply, and must not be assumed to identify the collector. |

The full report digest and collector revision are evidence gaps, not values to reconstruct
from abbreviations. Before S1, the operator must record them or collect a fresh report with
its exact revision and full artifact hashes using the command above. The documented pass
is the operator's prerequisite report; it is not a runtime qualification receipt.

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

### Spike bundle: dry render vs apply

`scripts/relay-kubevirt-spike.py` (package `scripts/relay_kubevirt_spike/`, tests
`scripts/tests/test_relay_kubevirt_spike_*.py`) renders the operator-run spike artifacts from the
pins in `docs/relay-workers/runtime-pins.json` (`kubevirt.spike_bundle`). It is a **dry renderer
only**: it never contacts the cluster and is not the Relay production KubeVirt renderer, which stays
unbuilt until S1–S9 pass. Every `kubectl` below is an operator action on `admin@ai` and is **pending**.

**Render (anyone; no credentials).** Keep upstream downloads outside git, under
`$EVIDENCE_DIR/public-pins/` (the local test cache `.data/public-pins/` is not tracked).
Re-fetch them from the URLs in `spike_bundle.sources` if they are missing. The render refuses any
SHA-256 change, any resource outside the reviewed list, any tag-only component and any placement
outside the dedicated pool.

```bash
: "${EVIDENCE_DIR:?}"; B="$EVIDENCE_DIR/kv-spike-bundle"
python3 scripts/relay-kubevirt-spike.py verify-image-pins           # read-only HTTPS; exit 1 if any tag moved
python3 scripts/relay-kubevirt-spike.py render-operator --source-dir "$EVIDENCE_DIR/public-pins" --out-dir "$B"
```

What the render changes, all recorded in `bundle-metadata.json`:
- The virt-operator image **and** `VIRT_OPERATOR_IMAGE` are pinned by digest. So is every child
  image env that v1.9.1 `util/config.go` reads: api, controller, handler, launcher, exportproxy,
  exportserver, synchronization-controller, libguestfs, pr-helper, sidecar-shim and the two
  virt-template images. Unset envs would make the operator derive mutable `:v1.9.1` tags.
- The operator pod and KubeVirt `spec.infra`/`spec.workloads` select
  `ailab.io/relay-worker-pool=true`, tolerate only `dedicated=relay-worker:NoSchedule`, and require
  `DoesNotExist` on agent-pool/control-plane/master labels.
- Replicas go from 2 to 1, operator and infra alike, because there is a single dedicated node.
- The Beta gates `Template`, `Snapshot`, `ImageVolume` and `DeclarativeHotplugVolumes` are
  disabled, and `evictionStrategy: None` is set.
- The upstream CRD, PriorityClass, ClusterRoles and ClusterRoleBinding are kept **unchanged**.
  virt-operator manages cluster-scoped CRDs, webhooks, APIServices and component RBAC, so this
  is **not** a namespaced install. Review those rights as upstream ships them. The `kubevirt`
  namespace keeps upstream's PSA `privileged` label, because virt-handler is privileged.

**Guest evidence before S2 (operator).** `render-workload` refuses without nonsecret evidence that
is fresh (≤14 days), not in the future, and names the pinned guest and launcher images and a
non-root launcher UID. The copied base image plus the root disk's full virtual size
must fit in 30 GiB − 20 GiB cache − 1 GiB reserve; both can occupy disk concurrently.
1. `python3 scripts/relay-kubevirt-spike.py evidence-template > "$EVIDENCE_DIR/guest-evidence.json"`.
2. On the operator workstation, outside git and never on a dev-worker's OS disk, download the
   single amd64 layer blob named in `guest_candidate.amd64_layer` (about 620 MB). Then run
   `measure-guest-layer --blob <file>`. It checks the blob's SHA-256 and reports the expanded tar
   bytes and the qcow2 virtual size from the header. Copy those values into the evidence and
   delete the blob.
3. `launcher_uid`: the launcher's OCI config `User` is `107` (`spike_bundle.launcher_amd64`,
   matching `NonRootUID` in `pkg/util/util.go`). Record it with source `oci-config-user`, then
   confirm it against the first launcher pod's `securityContext` in S2.

**Workload render, then apply (operator).**
```bash
python3 scripts/relay-kubevirt-spike.py render-workload --evidence "$EVIDENCE_DIR/guest-evidence.json" --out-dir "$B"
sha256sum "$B"/*                                                   # record in the evidence record
# --- APPLY BOUNDARY: everything below writes to admin@ai ---
kubectl --context admin@ai apply --server-side -f "$B/10-operator.yaml"
kubectl --context admin@ai -n kubevirt rollout status deploy/virt-operator --timeout=300s
kubectl --context admin@ai apply --server-side -f "$B/20-kubevirt-cr.yaml"
kubectl --context admin@ai -n kubevirt wait kv/kubevirt --for=condition=Available --timeout=900s   # S1
kubectl --context admin@ai -n kubevirt get pods -o wide             # every pod on relay-worker-1 only
kubectl --context admin@ai apply --server-side -f "$B/30-workload.yaml"
kubectl --context admin@ai -n relay-kv-spike wait job/kv-spike-1-prep --for=condition=Complete --timeout=600s
kubectl --context admin@ai -n relay-kv-spike logs job/kv-spike-1-prep > "$EVIDENCE_DIR/s2-prep.log"  # qemu-img info + uid
kubectl --context admin@ai -n relay-kv-spike delete job/kv-spike-1-prep --wait=true
virtctl --context admin@ai -n relay-kv-spike start kv-spike-1      # virtctl v1.9.1, SHA-256 in spike_bundle.virtctl
kubectl --context admin@ai -n relay-kv-spike wait vmi/kv-spike-1 --for=condition=Ready --timeout=1200s
```
The VM starts `Halted`. VMI `Ready` comes from the in-guest exec probe
(`/usr/local/sbin/relay-spike-ready`), which needs qemu-guest-agent and also checks that both
virtio disks are mounted, that `docker compose` is running the pinned smoke container, and that it
wrote to the workspace. `Running` alone is not readiness. The guest recipe (cloud-init) installs
`qemu-guest-agent`, `docker.io` and `docker-compose-v2` from Ubuntu archives at first boot. That is
why the reviewed egress allows only the Ubuntu archives and quay.io on 80/443, plus kube-dns. The
estate LAN `192.168.0.0/23`, RFC 1918 172.16/12 and the tailnet CGNAT range stay denied, even
through those FQDNs. If the packages cannot install, readiness never passes, and S2 records
`fail`, not success. A NetworkPolicy also denies all ingress and all other egress for the namespace.

`relay-kv-spike` explicitly enforces PSA `privileged`: KubeVirt's generated masquerade
launcher needs `NET_ADMIN`, which baseline PSA rejects. Audit and warning policies remain
`restricted`. This exception applies only to this disposable spike namespace on the dedicated
node; it does not change the Relay pod tier or any existing namespace. Do not grant worker
identities access to this namespace. S2 must capture the actual generated launcher Pod,
its security context/capabilities, and admission events. An admission refusal is a failed S2,
not a reason to widen policy elsewhere. While the VM is stopped and its launcher removed,
server-dry-run the captured Pod to validate admission independently of KubeVirt:

```bash
# Capture before stopping; use the actual launcher name from get pods, never another namespace.
kubectl --context admin@ai -n relay-kv-spike get pod "$LAUNCHER_POD" -o json > "$EVIDENCE_DIR/s2-launcher-pod.json"
virtctl --context admin@ai -n relay-kv-spike stop kv-spike-1
kubectl --context admin@ai -n relay-kv-spike wait --for=delete pod/"$LAUNCHER_POD" --timeout=300s
# Strip server-owned metadata and status; retain the complete generated spec for admission.
python3 - "$EVIDENCE_DIR/s2-launcher-pod.json" "$EVIDENCE_DIR/s2-launcher-admission.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
assert p['kind'] == 'Pod' and p['metadata']['namespace'] == 'relay-kv-spike'
assert p['metadata']['labels'].get('kubevirt.io') == 'virt-launcher'
p.pop('status', None)
p['metadata'] = {k: v for k, v in p['metadata'].items() if k in ('name', 'namespace', 'labels', 'annotations')}
json.dump(p, open(sys.argv[2], 'w'))
PY
kubectl --context admin@ai create --dry-run=server -f "$EVIDENCE_DIR/s2-launcher-admission.json" -o json > "$EVIDENCE_DIR/s2-launcher-admitted.json"
virtctl --context admin@ai -n relay-kv-spike start kv-spike-1
```

The smoke container uses `restart: unless-stopped`, so Docker restores it after an in-guest
reboot even though cloud-init `runcmd` runs once. S2 also requires one operator-triggered
in-guest reboot through the guest agent, recording changed boot ID, mounted disks, Docker
restart and the recovered exec readiness probe. Stop/start into a fresh VMI is a separate
test and does not substitute for this guest reboot. The offline tests validate this configuration;
the reboot/admission evidence remains pending until the operator runs these checks.

Evidence paths, all under `$EVIDENCE_DIR`, with SHA-256 recorded:
- `kv-spike-bundle/*`
- `guest-evidence.json`
- `s1-*`, `s2-prep.log`, `s2-launcher-pod.json`, then `s3-*` through `s9-*`, one per step
- `qwen-stop-restore.txt`: stop/restore times and health for LXC 5003 `llama-swap-qwen38`, S7/S8

Cleanup is the K5 owner's job, per S9 below. Delete in reverse order, and do not remove finalizers:
`kubectl --context admin@ai delete -f "$B/30-workload.yaml" --wait=true`. Keep KubeVirt for a
reviewed Flux adoption, or remove it by deleting `20-kubevirt-cr.yaml`, waiting until the
`kubevirt` namespace pods are gone, and then deleting `10-operator.yaml`. This bundle runs none of
the S3–S9 measurements, the eight-hour soak or headroom checks. Those remain operator steps with
their own evidence.

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
   - From the guest, verify the `cloud-power` WoL Service, a node address, and
     NAS `10.55.0.254` are all refused. The dedicated node has the NAS route and
     the WoL listener admits pod CIDRs, so network reachability alone is unsafe.
     These destinations must already be denied before S2 boots: the bundle
     applies default-deny and Cilium rules before the halted VM is started.
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
