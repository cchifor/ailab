# ADR 0005 — AI GPU access via LXC / bare-metal, not VM passthrough (deferred)

**Status:** ACCEPTED (2026-06-14) — privileged LXC AI appliance, keep Proxmox · **Date:** 2026-06-14

## Context
The Strix Halo iGPU (Radeon 8060S, gfx1151) is the AI compute. ROCm is "preview" tier but works.
Two ways to expose it from Proxmox:
- **LXC device passthrough** — bind `/dev/dri/*` + `/dev/kfd` into a container; host keeps `amdgpu`.
  Clean, multi-tenant, IaC-friendly, no DKMS.
- **VM PCIe passthrough** — suffers the AMD **reset bug** (iGPU passes once per host boot; VM reboot
  ⇒ host reboot), needs vBIOS extraction + fixed BIOS VRAM. Fragile for always-on.

The XDNA2 NPU has no usable Linux serving path today → inference on the iGPU only.

## Decision (provisional)
- AI/ROCm workloads run in a **privileged LXC** (or bare-metal Talos on the GPU node), **not** an
  iGPU-passthrough VM. Keep the GPU-node provisioning path **separate/pluggable** from the generic
  worker path so the rest of the cluster stays clean Talos VMs.
- Set a **fixed** iGPU VRAM split in BIOS (dynamic is unstable); treat it as a per-node IaC variable.

## Decision (final, 2026-06-14)
**Keep Proxmox on all 3 nodes. Run the LLM in a privileged LXC** (`/dev/kfd`+`/dev/dri`, host ROCm
already works) and expose it to Kubernetes via an `ExternalName`/Endpoints service — AI is an
external-to-k8s endpoint. Bare-metal Talos (native `amd.com/gpu`) was a strong alternative but was
rejected because it removes Proxmox + the storage fabric + host ROCm we already built. iGPU VM
passthrough remains rejected (AMD reset bug). See `docs/k8s-architecture.md`.
- Serve with llama.cpp (Vulkan) / Ollama; `HSA_OVERRIDE_GFX_VERSION=11.5.1`; fixed BIOS VRAM.
- Revisit bare-metal Talos GPU workers if/when first-class in-cluster GPU scheduling is needed.
