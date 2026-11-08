# Dedicated Relay worker capacity rollout

Status: owner approved capacity/address allocation and the temporary Qwen stop for S7/S8 on
2026-11-08 in the implementation session. Operator lease/edge checks and apply remain pending.
No node, DHCP change, KubeVirt deployment or runtime
qualification has been applied. The [operator report](https://git.chifor.me/cchifor/ailab/pulls/1675#issuecomment-106374)
measured the capacity and confirmed nested KVM; the [feasibility runbook](relay-worker-kubevirt-feasibility.md)
remains the acceptance gate.

The proposal allocates **vmid 4304, `relay-worker-1`, 192.168.0.52** on **ai-node3** with
**12 vCPU, 24 GiB fixed RAM, 200 GiB local-lvm** and host CPU passthrough. It adds an independent
OpenTofu map, Talos patch and `relay-gvisor` RuntimeClass. No resource iterates or changes the
existing agent-node map. The new pool never carries `ailab.io/agent-pool`.

The map defaults to empty. After approval, the operator must preserve the explicit variable file
with the module's other persistent input files and pass it on every subsequent plan/apply.
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

   The plan must add only the Relay VM, its machine-config apply, label/taint and RuntimeClass.
   No existing VM, machine config, PKI, pool or node is changed or removed. A dev-worker cannot
   validate this against live state. After reviewing the nonsecret summary, the operator applies
   the saved plan. Keep plan/state files outside git; they contain sensitive configuration.
7. Verify the actual hostname is `relay-worker-1`, the dedicated label/taint are present and
   no agent-pool label exists. Verify qemu-guest-agent and gVisor extensions, KVM availability,
   `user.max_user_namespaces`, disk size, Cilium readiness, and absence of protected workloads.
   Run the collector with the updated binding. Any mismatch stops the rollout.
8. Execute the separately bounded KubeVirt spike and pod qualification. A Ready Talos node or a
   created RuntimeClass does not qualify a runtime. No Relay worker admission is enabled here.

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

## Rollback and evidence

Before a VM exists, rollback can remove the opt-in configuration. Keep `.52` excluded from edge
trust while any worker still uses it. After provisioning, stop admissions and confirm no worker,
VM, PVC or local-path data remains before intentionally retiring the node through a reviewed
OpenTofu removal. Only return `.52` to DHCP after verifying the node no longer uses it and updating
IPAM, desired gateway range and live controller together. Never remove a bound PVC or finalizer
just to make a teardown pass.

Record applied commit, reviewed plan summary, gateway range, node identity, extension IDs,
allocatable resources and preflight report hashes. Update this document's pending status only
from that evidence. The existing spike's S1–S9 receipts remain a separate requirement.
