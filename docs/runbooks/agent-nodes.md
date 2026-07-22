# Runbook — Talos agent node pool (AgentForge v2 compute)

Dedicated Talos **worker** pool for AgentForge v2 — three VMs (`agent-node-1/2/3`, IPs
`192.168.0.47–.49`, vmids `4301–4303`, one per Proxmox host) that JOIN the existing `ai` cluster,
labelled `ailab.io/agent-pool=true` and tainted `dedicated=agent:NoSchedule`. Module:
`kubernetes/infra/agent-nodes/` (separate local state). ADR 0019.

**P1 = plain workers (no Kata/gVisor yet).** Kata/gVisor are P2 image-factory extensions + the
nested-virt host prerequisite below; the VMs are already sized (`cpu.type=host`, fixed memory) so P2
adds Kata to the SAME VMs without a reshape.

> Why a separate module and not `dev-workers`-style plain VMs: these are Talos workers and MUST reuse
> the **existing cluster `machine_secrets`** (a fresh `talos_machine_secrets` forks the PKI and the
> node never joins). The module reads them **read-only** from the CP root module's state via
> `terraform_remote_state` (Option B, ADR 0019). It never mutates `infra/`.

## Prerequisites (in order)

1. **Stage the Talos nocloud image on each target node** (bpg cannot decompress the xz factory
   image): `scripts/stage-talos-image.sh` — same step the CP module needs. The VM disk imports from
   `local:import/talos-<ver>-nocloud-amd64.raw`.
2. **Expose the cluster PKI from `infra/` (one-time).** `kubernetes/infra/outputs.tf` now emits two
   sensitive outputs (`machine_secrets`, `client_configuration`). Run `tofu apply` in `infra/` once
   so they are present in its state. **This changes no infrastructure** — adding outputs does not
   touch the CP VMs (`for_each` keys are stable; no CP reboot). Verify with
   `& ~/.tofubin/tofu.exe -chdir=kubernetes/infra output machine_secrets` (redacted, sensitive).
3. **(P2 gate, harmless in P1) Nested virtualization on the Proxmox hosts — see below.**

## Apply

Run on **Windows PowerShell** (the `siderolabs/talos` provider is `windows_amd64`; WSL has no
internet for provider downloads). The `just agent-nodes-plan/apply` recipes are the WSL mirrors.

```powershell
& ~/.tofubin/tofu.exe -chdir=kubernetes/infra/agent-nodes init
& ~/.tofubin/tofu.exe -chdir=kubernetes/infra/agent-nodes plan
& ~/.tofubin/tofu.exe -chdir=kubernetes/infra/agent-nodes apply
```

VMs boot → the worker machine-config applies → nodes register against the VIP `.40`.

## Verify

```bash
kubectl --context admin@ai get nodes -l ailab.io/agent-pool=true          # 3 nodes, Ready
kubectl --context admin@ai get node agent-node-1 -o jsonpath='{.spec.taints}'  # dedicated=agent:NoSchedule
```

Ready follows once Cilium schedules its agent DaemonSet onto the new nodes (it tolerates the taint
via its cluster-wide toleration — verify).

## Nested virtualization prerequisite (P2 gate for Kata — DO NOT auto-apply here)

Kata's QEMU microVM needs `/dev/kvm` inside the Talos worker, which requires **`kvm_amd nested=1`
on each Proxmox host** AND `cpu.type=host` on the VM (already set in `main.tf`). **Nothing in the
repo enables nested virt today** — it is a genuine new host prerequisite and an **operator step**
(a host module/kernel-param change, not a tofu apply). It is harmless to do in P1 and a hard gate
for Kata in P2. Two placement options:

- **Ansible (preferred, idempotent, matches the estate):** add to `ansible/roles/pve_base/tasks/main.yml`
  a task writing `/etc/modprobe.d/kvm-nested.conf` with `options kvm_amd nested=1`, then apply via
  `just net` (tags base). A `modprobe -r kvm_amd` reload needs no running VMs on `kvm_amd`; otherwise
  a host reboot picks it up.
- **Quick one-off (no ansible run):**
  ```bash
  python scripts/node-ssh.py <.2|.3|.4> \
    "echo 'options kvm_amd nested=1' >/etc/modprobe.d/kvm-nested.conf; modprobe -r kvm_amd 2>/dev/null; \
     modprobe kvm_amd; cat /sys/module/kvm_amd/parameters/nested"   # expect Y
  ```
  If VMs are running on that host, `modprobe -r` fails → schedule a host reboot instead.
- **Fallback:** a host that can't enable nested virt has no `/dev/kvm` → those nodes run **gVisor
  (runsc)** only (user-space, no KVM) for compute-only roles. Privileged DinD/sandbox scheduling
  must **fail closed** onto Kata-capable nodes (never silently fall back to gVisor — it can't host
  privileged DinD). See the plan + ADR 0019.

## P2 follow-ups (not P1)

- Rebuild the agent-node image with the Kata/gVisor Talos system extensions (image factory /
  `talos_extensions`; confirm the gVisor extension exists on the Sidero Image Factory) → re-stage →
  re-import the VMs; uncomment the `vhost_net`/`vhost_vsock` kernel modules in `worker.yaml.tftpl`.
- Ship the `kata`/`gvisor` RuntimeClasses (`kubernetes/apps/apps/agentforge/runtimeclasses.yaml`)
  once the handlers exist on the nodes.
