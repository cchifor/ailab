# Runbook — Talos agent node pool (AgentForge v2 compute)

Dedicated Talos **worker** pool for AgentForge v2 — three VMs (`agent-node-1/2/3`, IPs
`192.168.0.14–.16`, vmids `4301–4303`, one per Proxmox host) that JOIN the existing `ai` cluster,
labelled `ailab.io/agent-pool=true` and tainted `dedicated=agent:NoSchedule`. Module:
`kubernetes/infra/agent-nodes/` (separate local state). ADR 0019.

**P2 = Kata/gVisor-capable workers.** The pool now boots a Kata/gVisor-enabled Talos image
(`siderolabs/kata-containers` + `siderolabs/gvisor` system extensions baked in via the Image Factory —
`kubernetes/infra/agent-nodes/image.tf`) and enables the `vhost_net`/`vhost_vsock` kernel modules
(`machine-config/worker.yaml.tftpl`). The VMs are unchanged from P1 (`cpu.type=host`, fixed memory) —
only the boot image + kernel modules + the host nested-virt prereq changed. To revert to the PLAIN P1
pool, drop `kata-containers`/`gvisor` from `var.talos_extensions` and re-stage (see below).

> Why a separate module and not `dev-workers`-style plain VMs: these are Talos workers and MUST reuse
> the **existing cluster `machine_secrets`** (a fresh `talos_machine_secrets` forks the PKI and the
> node never joins). The module reads them **read-only** from the CP root module's state via
> `terraform_remote_state` (Option B, ADR 0019). It never mutates `infra/`.

## Prerequisites (in order)

1. **Stage the agent-pool Kata/gVisor image on each target node** (bpg cannot decompress the xz
   factory image). This pool boots its OWN image (schematic = base + `kata-containers` + `gvisor`),
   staged under a DISTINCT `-agent-` basename so it coexists with the plain CP image on the same
   node's `local:import`. Init the module first so the outputs resolve, then stage:
   ```bash
   & ~/.tofubin/tofu.exe -chdir=kubernetes/infra/agent-nodes init
   SCHEMATIC=$(~/.tofubin/tofu.exe -chdir=kubernetes/infra/agent-nodes output -raw schematic_id) \
   FILE=$(~/.tofubin/tofu.exe -chdir=kubernetes/infra/agent-nodes output -raw agent_image_file) \
     scripts/stage-talos-image.sh
   ```
   The VM disk imports from `local:import/talos-<ver>-agent-nocloud-amd64.raw`. (To fall back to the
   PLAIN pool, drop `kata-containers`/`gvisor` from `var.talos_extensions`, re-run the two commands.)
2. **Expose the cluster PKI from `infra/` (one-time).** `kubernetes/infra/outputs.tf` now emits two
   sensitive outputs (`machine_secrets`, `client_configuration`). Run `tofu apply` in `infra/` once
   so they are present in its state. **This changes no infrastructure** — adding outputs does not
   touch the CP VMs (`for_each` keys are stable; no CP reboot). Verify with
   `& ~/.tofubin/tofu.exe -chdir=kubernetes/infra output machine_secrets` (redacted, sensitive).
3. **Nested virtualization on the Proxmox hosts (REQUIRED for Kata) — see below.**

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

Then verify the Kata runtime is actually usable (needs the image staged + nested virt below):

```bash
kubectl --context admin@ai get runtimeclass kata gvisor                    # both present
# Smoke test: a Kata pod boots into its OWN guest kernel (different uname from the node kernel).
kubectl --context admin@ai run kata-smoke --rm -it --restart=Never \
  --overrides='{"spec":{"runtimeClassName":"kata"}}' --image=busybox -- uname -a
# On an agent node, confirm /dev/kvm exists (nested virt live):
python scripts/lxc-exec.py ...   # or: talosctl -n 192.168.0.14 read /dev/kvm  (device present)
```

## Nested virtualization prerequisite (REQUIRED for Kata)

Kata's QEMU microVM needs `/dev/kvm` inside the Talos worker, which requires **`kvm_amd nested=1`
on each Proxmox host** (Strix Halo = AMD) AND `cpu.type=host` on the VM (set in `main.tf`). This is a
genuine host prerequisite and an **operator step** (a host kernel-param change, NOT a tofu apply) — a
host without nested virt has no `/dev/kvm`, so its `kata` pods fail.

- **Ansible (preferred, now IaC):** `ansible/roles/pve_base` writes `/etc/modprobe.d/kvm-nested.conf`
  (`options kvm_amd nested=1`) when `pve_enable_nested_virt: true` (default, `group_vars/all.yml`) and
  prints a warning if the runtime value isn't yet `Y`. Apply with `just net` (pve_base tags). It does
  **NOT** hot-reload the module (a live `modprobe -r kvm_amd` fails while guests run) — the option
  takes effect on the next kvm_amd load: a **host reboot**, or a manual reload when the host has **no
  running VMs**.
- **Quick one-off (no ansible run):**
  ```bash
  python scripts/node-ssh.py <.2|.3|.4> \
    "echo 'options kvm_amd nested=1' >/etc/modprobe.d/kvm-nested.conf; modprobe -r kvm_amd 2>/dev/null; \
     modprobe kvm_amd; cat /sys/module/kvm_amd/parameters/nested"   # expect Y
  ```
  If VMs are running on that host, `modprobe -r` fails → schedule a host reboot instead.
- **Verify runtime state (any host):**
  ```bash
  python scripts/node-ssh.py <.2|.3|.4> "cat /sys/module/kvm_amd/parameters/nested"   # expect Y
  ```
- **Fallback:** a host that can't enable nested virt has no `/dev/kvm` → those nodes run **gVisor
  (runsc)** only (user-space, no KVM) for compute-only roles. Privileged DinD/sandbox scheduling
  must **fail closed** onto Kata-capable nodes (never silently fall back to gVisor — it can't host
  privileged DinD). See the plan + ADR 0019.

## What P2 wired (this slice)

- **Agent-pool image (`kubernetes/infra/agent-nodes/image.tf`):** its own Image Factory schematic =
  base extensions + `siderolabs/kata-containers` (handler `kata`, QEMU) + `siderolabs/gvisor` (handler
  `runsc`). Both are official Factory extensions for v1.11.2; the handlers are auto-registered by the
  extensions (no CRI config patch). Staged under `talos-<ver>-agent-nocloud-amd64.raw` (distinct from
  the CP image). Switch back to plain = drop the two runtimes from `var.talos_extensions` + re-stage.
- **Kernel modules (`machine-config/worker.yaml.tftpl`):** `vhost_net` + `vhost_vsock` activated.
- **RuntimeClasses (`kubernetes/apps/infrastructure/agentforge-runtimeclasses/`):** `kata` + `gvisor`,
  each with the agent-pool `nodeSelector` + `dedicated=agent` toleration in `scheduling` (so a pod's
  `runtimeClassName` alone lands it on the pool). Flux-reconciled via its own
  `clusters/ai/agentforge-runtimeclasses.yaml` Kustomization.
- **Nested virt:** IaC in `pve_base` (above).

Still P2-follow-up (owned elsewhere): the ephemeral SandboxExecutor + admission-pinned pod shape +
per-pod Cilium egress (agentforge apps + `infrastructure/{security,autoscaling}` operators).
