# Runbook — AI LLM appliance (Strix Halo iGPU, llama.cpp Vulkan)

How the per-node **`ai-llm`** LXCs are built and operated. They serve an OpenAI-compatible API on
`:8080` from `llama.cpp` (Vulkan/RADV) on each Bosgame M5's Radeon 8060S iGPU (gfx1151), models from
the shared QNAP NFS store. Surfaced in Kubernetes as `llm.ai.svc.cluster.local:8080`.

- **IaC:** OpenTofu module `kubernetes/infra/ai-lxc/` (3 privileged LXCs, vmid 5001–5003, IP .51–.53),
  provisioning scripts in the same dir, k8s wiring in `kubernetes/apps/apps/ai/`.
- **Helpers:** `scripts/fetch-models.sh` (download GGUFs), `scripts/lxc-exec.py` (push+run provisioning).
- See ADR `docs/decisions/0008-ai-llm-appliance-implementation.md`.

## Rebuild from scratch

```bash
# 1. Models -> shared NFS (run ON a Proxmox host). Daily driver first (~18.6 GB).
ssh root@192.168.0.2 'bash -s' < scripts/fetch-models.sh daily      # or: coder | gpt-oss | qwen3.5 | all

# 2. Create the 3 LXCs (device passthrough + /models bind mount). Uses root@pam (see gotcha #1).
cp kubernetes/infra/terraform.tfvars kubernetes/infra/ai-lxc/terraform.tfvars   # then set pve_password
tofu -chdir=kubernetes/infra/ai-lxc init
tofu -chdir=kubernetes/infra/ai-lxc apply

# 3. Provision each LXC (llama.cpp Vulkan + node_exporter + amdgpu metrics)
python scripts/lxc-exec.py 192.168.0.2 5001
python scripts/lxc-exec.py 192.168.0.3 5002
python scripts/lxc-exec.py 192.168.0.4 5003

# 4. k8s endpoint + scrape targets are applied by Flux from kubernetes/apps/apps/ai/.
```

## Hardware reality (validated 2026-06-14)

| Fact | Value |
|---|---|
| iGPU | Radeon 8060S = **gfx1151**, Mesa **RADV** (Mesa 25.0.7 in Debian 13 supports it) |
| BIOS VRAM carve | **64 GiB** (`amdgpu: VRAM 65536M`); GTT ~31 GiB ⇒ **~95 GiB GPU-addressable** |
| System RAM (after carve) | ~62 GiB |
| Engine | llama.cpp `llama-server`, **Vulkan backend**, pinned build **b9631** |
| Daily driver | Qwen3-30B-A3B Q4_K_M (18.6 GB) → **~87–97 tok/s decode** |

### Where memory actually lives (important)
With the **Vulkan backend + `-ngl 99`**, llama.cpp allocates the model + KV in the GPU heap that RADV
exposes (`Vulkan0`, ~95 GiB = the 64 GiB VRAM carve + ~31 GiB GTT). Measured for the daily driver:
`mem_info_vram_used ≈ 20.35 GiB`, **LXC cgroup `memory.current` ≈ 0.5 GiB**. So:
- The model is **NOT** charged to the container cgroup (it's in the firmware-reserved VRAM heap).
- The `lxc_memory_mib` cap (default **16 GiB**) is a host OOM fence, not the model budget; the daily
  driver uses ~0.5 GiB of it.
- **Do not enable the ROCm/CPU path by accident** — on the CPU backend the model loads into cgroup
  RAM and OOM-kills at the cap (see gotcha #2).

### BIOS carve / VM sizing — deferred, evidence-based
The original plan considered reducing the BIOS carve + raising `amdgpu.gttsize`/`ttm.pages_limit`, and
downsizing the Talos CP VMs 32→24 GiB, to free system RAM. **Measurement made both largely unnecessary
for the daily driver** (the LXC uses ~0.5 GiB system RAM; a node runs the 32 GiB CP VM + the LXC with
~33 GiB still free). **Heavyweight validation (2026-06-14) confirmed both fit the current carve:**
- gpt-oss-120B (63 GB) loads **entirely in the 64 GiB VRAM heap** (59 GiB used), ~0.6 GiB GTT.
- Qwen3.5-122B (76.5 GB) **maxes VRAM (64 GiB) and spills ~8 GiB to GTT** (system RAM); it runs alongside
  a 32 GiB CP VM but pushes `free`→0. For **large contexts** on the 122B, give that node headroom: downsize
  its CP VM to 24 GiB (`kubernetes/infra/variables.tf` `control_planes{}`, rolling reboot — HA tolerates one
  CP down) or reduce the BIOS UMA carve (manual, per-node) + set kernel `amdgpu.gttsize=131072 ttm.pages_limit=…`.

## Running a heavyweight model (on-demand)
Both heavyweights are staged on the NFS and **validated on the current 64 GiB carve** (2026-06-14):

| Model | size | VRAM used | GTT spill | decode | host RAM |
|---|---|---|---|---|---|
| gpt-oss-120B (MXFP4) | 63.4 GB | 59 GiB (fits VRAM) | 0.6 GiB | **53 tok/s** | tight |
| Qwen3.5-122B (Q4_K_M) | 76.5 GB | **64 GiB (maxed)** | **8 GiB** | **23 tok/s** | `free`≈0 |

Switch a node's `llama-server` to one by re-running provisioning with overrides (this restarts the
service → loads the model; revert by re-running with no `--env`):

```bash
# NOTE (Windows/Git Bash): prefix with MSYS_NO_PATHCONV=1 or the /models/... arg gets mangled to a
# Windows path. Use --no-mmap for heavyweights (keeps the GGUF out of page cache).
MSYS_NO_PATHCONV=1 python scripts/lxc-exec.py 192.168.0.2 5001 \
  --env MODEL=/models/gpt-oss-120b/gpt-oss-120b-mxfp4-00001-of-00003.gguf \
  --env MODEL_ALIAS=gpt-oss-120b --env CTX=8192 --env PARALLEL=1 --env EXTRA_ARGS=--no-mmap
```
Both leave the LXC cgroup near the 16 GiB cap and system RAM near `free`=0 (GTT spill + the 32 GiB CP VM)
— stable, but for **large contexts** on the 122B give that node headroom: downsize its CP VM to 24 GiB
(`control_planes{}`, rolling reboot) or shrink the BIOS UMA carve. **Don't** point the `llm` k8s service
at a heavyweight — it advertises `qwen3-30b-a3b`; hit a heavyweight directly on the node IP, or add a
model router (see `docs/k8s-followups.md`). Steady state: all 3 nodes on the daily driver.

## Vision / image input (Qwen3-VL-8B)
Text models are text-only; image input needs a vision model + its `mmproj` projector (else
"image input is not supported - provide the mmproj"). A dedicated **Qwen3-VL-8B** runs as a 3rd
instance on node1 `:8082` (`scripts/fetch-models.sh vision` → `/models/qwen3-vl-8b/`), surfaced as
`qwen3-vl-8b` in LiteLLM with `model_info.supports_vision: true`. Provision command:
```bash
MSYS_NO_PATHCONV=1 python scripts/lxc-exec.py 192.168.0.2 5001 \
  --env INSTANCE=vision --env PORT=8082 \
  --env MODEL=/models/qwen3-vl-8b/Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf \
  --env MODEL_ALIAS=qwen3-vl-8b --env MMPROJ=/models/qwen3-vl-8b/mmproj-F16.gguf \
  --env CTX=16384 --env PARALLEL=1
```
- **Use GPU mmproj offload (the default — do NOT pass `--no-mmproj-offload`).** GPU offload is both
  correct and fast for Qwen3-VL-8B (~0.5 s image-encode, ~2 s end-to-end). `--no-mmproj-offload`
  (CPU encoder, a defensive workaround for a Vulkan vision bug that does NOT affect this model) made
  image description take **>1 minute** — only reach for it if you ever observe garbled descriptions.
- In Open WebUI select `qwen3-vl-8b` for images; enable its Vision capability if the upload button
  is hidden (Admin → Models → qwen3-vl-8b → Capabilities → Vision).
- For dense OCR/grounding the model wants ≥1024 image tokens (`--image-min-tokens 1024`).

## Verify
```bash
curl http://192.168.0.51:8080/health                      # {"status":"ok"}
curl http://192.168.0.51:8080/v1/models                   # lists the served model
# decode rate: POST /v1/chat/completions, read .timings.predicted_per_second
kubectl --kubeconfig kubernetes/infra/_out/kubeconfig -n ai get svc,endpoints,servicemonitor
# in-cluster: wget -qO- http://llm.ai.svc.cluster.local:8080/v1/models
```
Grafana/Prometheus: `llamacpp:*` (throughput, KV, queue depth) + `amdgpu_*` (busy %, VRAM/GTT used,
temp, power, sclk) per node.

## Gotchas (learned the hard way)

1. **LXC device passthrough + bind mounts are `root@pam`-only.** The bpg provider's API *token* (even
   with full Administrator privileges) gets `HTTP 403 — only allowed for root@pam`. The `ai-lxc` module
   therefore authenticates with **root@pam username+password** (`pve_password`, = the node root
   password), not the token. This is unavoidable; it's a hard Proxmox restriction (`authuser eq 'root@pam'`).

2. **The Vulkan ICD filename varies** (`radeon_icd.json` vs `radeon_icd.x86_64.json`). Pointing
   `VK_ICD_FILENAMES` at a nonexistent file makes the loader load **no driver** → llama.cpp silently
   falls back to the **CPU backend** → the model loads into cgroup RAM and **OOM-kills** at the memory
   cap. `provision.sh` auto-detects the ICD; verify the backend with
   `llama-cli --list-devices` (must show `Vulkan0: … RADV GFX1151`), not just `vulkaninfo`.

3. **node_exporter textfile collector**: the `.prom` must be **world-readable** (node_exporter runs
   non-root) and node_exporter must run with `--collector.textfile.directory=…` (restart it after
   setting `/etc/default/prometheus-node-exporter`). Symptom of either miss: `node_textfile_scrape_error 1`
   and missing `amdgpu_*` series. `amdgpu-textfile.sh` writes `0644`; `provision.sh` sets ARGS + restarts.

4. **LXC template storage**: the cluster's `local` datastore is content-typed only for `import` (Talos
   image). The module downloads the Debian 13 template to the shared **`qnap-nfs`** datastore (already
   content-typed for `vztmpl`), so it downloads once for all nodes.

5. **Devices appear as group `kvm` inside the CT** — that's just gid 993's name in Debian 13; the
   `render` gid on the host is 993. `provision.sh` adds the `llama` user to the gid-993 group regardless
   of its name.
