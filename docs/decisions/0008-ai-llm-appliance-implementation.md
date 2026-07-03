# ADR 0008 — AI LLM appliance: implementation + empirical refinements

**Status:** ACCEPTED (2026-06-14) — daily driver live on all 3 nodes · **Date:** 2026-06-14
**Extends:** ADR 0005 (GPU via LXC, keep Proxmox).

## Context
ADR 0005 decided *where* the AI runs (privileged LXC). This ADR records *how* it was built and the
measurements that corrected several assumptions. Research was validated against June-2026 sources and
cross-reviewed; the build then corrected the research where hardware reality differed.

## Decisions
1. **Engine: llama.cpp `llama-server`, Vulkan/RADV**, pinned prebuilt **b9631** (`-bin-ubuntu-vulkan-x64`).
   Not Ollama (vendored llama.cpp lags), not vLLM (slow on gfx1151), not ROCm for serving (Vulkan wins at
   normal context and needs no ROCm stack). The tarball extracts to `llama-<build>/` (flat libs).
2. **Placement: one privileged LXC per node** (`kubernetes/infra/ai-lxc/`, bpg 0.109.0), `device_passthrough`
   of `/dev/dri/renderD128` + `/dev/kfd` (gid 993), `/mnt/pve/qnap-nfs/models` bind-mounted to `/models`,
   non-root `llama` service user, cgroup memory fence.
3. **Models: MoE GGUF on shared NFS.** Daily driver **Qwen3-30B-A3B Q4_K_M** (live); Qwen3-Coder-30B-A3B,
   gpt-oss-120B (MXFP4), Qwen3.5-122B-A10B staged on-demand. `GLM-4.x 355B` excluded (does not fit).
4. **k8s surface: ClusterIP `llm` Service (no selector) + core/v1 Endpoints** → mirrored to EndpointSlices,
   Cilium load-balances across the 3 LXCs. `llm.ai.svc.cluster.local:8080`. (Chose a real ClusterIP over
   `ExternalName` for in-cluster LB; core/v1 Endpoints over a bare EndpointSlice for Prometheus SD compat.)
5. **Monitoring:** llama-server `/metrics` + amdgpu **sysfs** via node_exporter textfile collector
   (`rocm-smi`/`amd-smi` are blind on gfx1151), scraped by the existing kube-prometheus-stack.

## Empirical refinements to the plan / ADR 0005
- **Auth:** LXC device passthrough + bind mounts are **root@pam-only** — API tokens get 403. The module
  uses root@pam password auth (the one place we don't use the tofu token).
- **VRAM split:** ADR 0005 said "fixed BIOS VRAM, dynamic is unstable." In practice the Vulkan backend
  uses the **64 GiB VRAM carve + ~31 GiB GTT (~95 GiB)** heap fine; the model lives there, **not** in the
  LXC cgroup (measured: VRAM 20.35 GiB used, cgroup 0.5 GiB). No carve change was needed.
- **VM downsizing dropped:** the plan's 32→24 GiB CP-VM resize was premised on the LXC needing system RAM.
  It needs ~0.5 GiB, and a node runs the 32 GiB CP VM + the LXC with ~33 GiB free. Kept VMs at 32 GiB;
  downsize/carve-tuning **deferred** to heavyweight validation (see runbook).
- `HSA_OVERRIDE_GFX_VERSION` is irrelevant on the Vulkan path (ROCm-only).

## Status / follow-ups
Daily driver validated on all 3 nodes (~87–97 tok/s, end-to-end via the k8s service, all Prometheus
targets up). **Heavyweights validated on the current 64 GiB carve (2026-06-14):** gpt-oss-120B 53 tok/s
(59 GiB VRAM, fits), Qwen3.5-122B 23 tok/s (64 GiB VRAM + 8 GiB GTT spill) — so the BIOS carve change is
not required; the 122B is only RAM-tight with a 32 GiB CP VM. An optional model router and internet
exposure are tracked in `docs/k8s-followups.md`. Operations: `docs/runbooks/ai-host-setup.md`.

**Update (2026-07-03):** the deferred CP-VM downsize (above) was later done — CPs are now **cp1 24 /
cp2 24 / cp3 28 GiB**, freeing host RAM for the co-located dev-worker/runner VMs (measured CP working
set ~9 GiB, so the 32 GiB was mostly reclaimable cache). node3's 122B is now slightly less RAM-tight
(28 GiB CP). See ADR 0009 (Update 2026-07-03) + cchifor/ailab#85, #86.
