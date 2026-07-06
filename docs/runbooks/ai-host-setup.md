# Runbook — AI LLM appliance (Strix Halo iGPU, llama.cpp Vulkan)

How the per-node **`ai-llm`** LXCs are built and operated. They serve an OpenAI-compatible API on
`:8080` from `llama.cpp` (Vulkan/RADV) on each Bosgame M5's Radeon 8060S iGPU (gfx1151), models from
the shared QNAP NFS store. Surfaced in Kubernetes as `llm.ai.svc.cluster.local:8080`.

- **IaC:** OpenTofu module `kubernetes/infra/ai-lxc/` (3 privileged LXCs, vmid 5001–5003, IP **.44–.46**),
  provisioning scripts in the same dir, k8s wiring in `kubernetes/apps/apps/ai/`.
  > **IP note (2026-06-16):** the LXCs moved from .51–.53 to **.44–.46**. The router's DHCP pool starts
  > above .50, so .51–.53 sat *inside* it and the router leased .53 to a DHCP client (an MXCHIP IoT
  > device) → ARP conflict → qwen3.5-122b intermittently unreachable. Keep the lab's static IPs (Talos
  > .41–.43, AI LXCs .44–.46) within the reserved .2–.50 block. The manual k8s Endpoints
  > (`llm-service.yaml`, `monitoring.yaml`) carry these IPs too — change both together.
- **Helpers:** `scripts/fetch-models.sh` (download GGUFs), `scripts/lxc-exec.py` (push+run provisioning).
- See ADR `docs/decisions/0008-ai-llm-appliance-implementation.md`.

## Rebuild from scratch

```bash
# 1. Models -> shared NFS (run ON a Proxmox host). Daily driver first (~22 GB).
ssh root@192.168.0.2 'bash -s' < scripts/fetch-models.sh qwen3.6    # daily driver; or: gpt-oss | qwen3.5 | gemma4 | all
#    (`daily` still fetches the retired qwen3-30b-a3b GGUF, kept on NFS for revert.)

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
| Engine | llama.cpp `llama-server`, **Vulkan backend**, pinned build **b9672** (qwen35moe + gemma4 arches) |
| Daily driver | **Qwen3.6-35B-A3B** Q4_K_M (~21 GB), node1 :8080, 256K ctx → **~60 tok/s decode** (replaced qwen3-30b-a3b 2026-07-01) |

### Where memory actually lives (important)
With the **Vulkan backend + `-ngl 99`**, llama.cpp allocates the model + KV in the GPU heap that RADV
exposes (`Vulkan0`, ~95 GiB = the 64 GiB VRAM carve + ~31 GiB GTT). Measured for the daily driver:
`mem_info_vram_used ≈ 20.35 GiB`, **LXC cgroup `memory.current` ≈ 0.5 GiB**. So:
- The model is **NOT** charged to the container cgroup (it's in the firmware-reserved VRAM heap).
- The `lxc_memory_mib` cap (default **24 GiB**) is a host OOM fence, not the model budget; the daily
  driver uses ~0.5 GiB of it. (This holds only while weights are VRAM-carve-resident — see the carve→GTT
  rollout below, where GTT *is* charged here and the cap must be raised.)
- **Do not enable the ROCm/CPU path by accident** — on the CPU backend the model loads into cgroup
  RAM and OOM-kills at the cap (see gotcha #2).

### BIOS carve / VM sizing — evidence (carve→GTT reduction is now an active rollout, ↓)
The original plan considered reducing the BIOS carve + raising `amdgpu.gttsize`/`ttm.pages_limit`, and
downsizing the Talos CP VMs 32→24 GiB, to free system RAM. **Measurement made both largely unnecessary
for the daily driver** (the LXC uses ~0.5 GiB system RAM; a node runs the 32 GiB CP VM + the LXC with
~33 GiB still free). **Heavyweight validation (2026-06-14) confirmed both fit the current carve:**
- gpt-oss-120B (63 GB) loads **entirely in the 64 GiB VRAM heap** (59 GiB used), ~0.6 GiB GTT.
- Qwen3.5-122B (76.5 GB) **maxes VRAM (64 GiB) and spills ~8 GiB to GTT** (system RAM); it runs alongside
  a 32 GiB CP VM but pushes `free`→0. For **large contexts** on the 122B, give that node headroom: downsize
  its CP VM (`kubernetes/infra/variables.tf` `control_planes{}`, rolling reboot via `talosctl shutdown` — HA
  tolerates one CP down) or reduce the BIOS UMA carve (manual, per-node) + set kernel `amdgpu.gttsize=131072
  ttm.pages_limit=…`. **CP sizing is now per-node (2026-07-02, cp1 2026-07-03): cp1 24 / cp2 24 / cp3 28 GiB** — all three were
  downsized to free host RAM for the co-located dev-workers (CP working set is only ~8-10 GiB; see
  docs/runbooks/dev-workers.md).

## Reclaiming the BIOS UMA carve → GTT (per-node rollout)
**Why.** The fixed **64 GiB BIOS UMA VRAM carve** cannot be reclaimed by the OS without a BIOS change; it
strands ~40 GiB whenever a node runs the light daily driver (node1 even swaps while that carve sits idle).
Fix = **small BIOS carve (512 MB) + large GTT** (`ttm.pages_limit`), so each node borrows exactly what its
loaded model needs. Worth doing only if heavyweight tok/s **hold** on GTT — validate with
`scripts/bench-llm.py` (baseline + reproduce in `bench/README.md`; methodology in
`docs/superpowers/specs/2026-07-06-llm-carve-vs-gtt-benchmark-design.md`).

**Do ONE node at a time and pass the gate before advancing** — etcd needs ≥2/3 CPs, so sequential host
reboots are forced regardless. Order: **node2 (gpt-oss, cleanest carve→GTT signal) → node3 (122B, tightest
memory) → node1 (daily driver, ends its swapping)**. Conservative alt: node1 first, to prove the mechanics
on a node that runs no heavyweight. Host↔CP map: node1 .2 → cp1 .41 (vmid 4001) · node2 .3 → cp2 .42 (4002)
· node3 .4 → cp3 .43 (4003).

**Memory reframe — the model moves from the isolated carve into shared OS RAM (GTT), which is charged to
BOTH the host pool AND the LXC cgroup.** Today only the carve matters; post-carve two limits must hold:

1. **Host RAM pool (~127 GiB):** now holds VM working sets **+ the full model** (today the model sits in the
   separate carve, off the OS books). Validate for OOM/swap, not just tok/s.
2. **LXC cgroup cap (`lxc_memory_mib`):** GTT **is** charged to the CT's memcg — confirmed on node3 today
   (`memory.current` 8.8 GiB ≈ its 7.9 GiB GTT spill). The ai-lxc default cap is **24 GiB**; the full model
   in GTT (~59–72 GiB) blows past it → **OOM-kill on model load** unless raised first (see below). The IaC
   already documents this (`kubernetes/infra/ai-lxc/variables.tf` `lxc_memory_mib`: "raise toward ~96 GiB …
   after reducing the BIOS carve").

Host-pool headroom (estimated working-set RSS — all three run today on 62 GiB visible, so RSS is bounded;
configured VM max in parens, peak = RSS + model):

| node | VM RSS est. (configured) | + model in GTT | peak vs ~127 GiB |
|---|---|---|---|
| node2 (.3) | ~55 GiB (90 configured) | gpt-oss ~59 | ~114 — fits, tight |
| node3 (.4) | ~45 GiB (70 configured) | 122B ~72 | ~117 — tightest |
| node1 (.2) | ~55 GiB (90 configured) | daily driver ~24 | ~79 — comfortable |

### First, raise the LXC memory cap (once, before the per-node loop)
GTT is charged to the CT cgroup, so the default 24 GiB `lxc_memory_mib` fence would OOM-kill a heavyweight
served from GTT. Raise it to ~96 GiB (fits the 122B's ~72 GiB + headroom) via the ai-lxc module — it's a
cap, not a reservation, so it's harmless on the daily-driver nodes and on nodes not yet carve-reduced:
```bash
# kubernetes/infra/ai-lxc/  (OpenTofu, applied by hand — see CLAUDE.md)
#   set lxc_memory_mib = 98304   (96 GiB; variables.tf default is 24576)
tofu -chdir=kubernetes/infra/ai-lxc apply     # in-place memory update (no CT recreate)
#   the CT picks up the new cap on its next restart (the host reboot in step 3 below).
# Quick live alternative on one host: pct set <5001|5002|5003> -memory 98304
```

### Kernel cmdline (GRUB — `proxmox-boot-tool` is NOT in use here)
Append to `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`, then `update-grub`:
```
ttm.pages_limit=33554432 amdgpu.gttsize=131072
```
- `ttm.pages_limit=33554432` (×4 KiB = **128 GiB**) — **required.** Default is ~50% of visible RAM
  (~63 GiB post-carve), short of the 122B's ~72 GiB. Raises the ceiling (a cap, not a reservation).
- `amdgpu.gttsize=131072` (=128 GiB) — belt-and-suspenders; deprecated on recent kernels (may be a no-op).
- **Do NOT add `amd_iommu=off` initially.** It is safe here (verified: no VFIO, no `hostpci` passthrough on
  any node) but changes a 2nd variable, and IOMMU is on today with GTT already working (node3 spills 8 GiB).
  Add it **only if** the after-run shows large-GTT allocation failures or a big *prefill* regression, then
  re-bench.

Per-node target (append — node3 carries extra thunderbolt params, preserve them):
```
node1/node2:  GRUB_CMDLINE_LINUX_DEFAULT="quiet ttm.pages_limit=33554432 amdgpu.gttsize=131072"
node3:        GRUB_CMDLINE_LINUX_DEFAULT="quiet thunderbolt.host_reset=false pcie_aspm=off thunderbolt.clx=0 ttm.pages_limit=33554432 amdgpu.gttsize=131072"
```

### Per-node procedure (root on the Proxmox host unless noted)
```bash
# 0. workstation: confirm quorum BEFORE starting
_out/talosctl-1112.exe -n 192.168.0.41 etcd status            # 3/3 in-sync

# 1. edit grub (append once; sed is NOT idempotent — verify the line)
cp /etc/default/grub /etc/default/grub.bak.$(date +%F)
sed -i -E 's/^(GRUB_CMDLINE_LINUX_DEFAULT=".*)"/\1 ttm.pages_limit=33554432 amdgpu.gttsize=131072"/' /etc/default/grub
grep GRUB_CMDLINE_LINUX_DEFAULT /etc/default/grub             # sanity-check it appended exactly once
update-grub

# 2. workstation: gracefully stop THIS host's Talos CP (NOT qm shutdown / ACPI — won't stop Talos)
_out/talosctl-1112.exe shutdown -n <cp-ip>                    # cp1 .41 / cp2 .42 / cp3 .43
#    wait until stopped:  qm status <4001|4002|4003>  ->  stopped

# 3. reboot the host and ENTER BIOS -> UMA Frame Buffer / iGPU VRAM = 512 MB (or Auto/min) -> save & exit
reboot                                                        # also restarts the AI LXC + co-located VMs
```

### Validation gate — ALL must pass before touching the next node
```bash
free -g                                                       # ~126 GiB total; not swapping
cat /sys/module/ttm/parameters/pages_limit                   # 33554432
for f in vram_total gtt_total gtt_used vram_used; do echo $f=$(cat /sys/class/drm/card0/device/mem_info_$f); done
#   expect: vram_total ~512 MiB, gtt_total ~120+ GiB
ctid=<5001|5002|5003>; for c in memory.current memory.max; do echo "cgroup $c=$(cat /sys/fs/cgroup/lxc/$ctid/$c)"; done
#   expect: memory.current (≈ model size in GTT) < memory.max (the raised ~96 GiB cap)
dmesg | grep -i amdgpu | grep -iE 'error|fail|reset' || echo 'amdgpu clean'
```
1. **Quorum** restored: `talosctl … etcd status` = **3/3 in-sync** (CP rejoined).
2. **Reclaimed:** `free -g` ~126 GiB; `vram_total` ~512 MiB; `gtt_total` ~120+ GiB.
3. **Model loads from GTT (no cgroup OOM):** reload the heavyweight (same `n_ctx=8192`, same build), `/health`
   ok, `gtt_used` ≈ model size, `vram_used` tiny, and the CT cgroup `memory.current` < `memory.max` (the
   raised cap — else it OOM-kills on model load), dmesg clean.
4. **No memory pressure:** `free -g` not swapping under VMs + model.
5. **tok/s holds:** `python scripts/bench-llm.py run --sizes 512,4096,7680 --label after-bios --targets <node>`
   then `compare` vs the baseline. Treat >~10% decode or >~20–30% prefill regression as *investigate* (a big
   prefill drop points at IOMMU overhead → try `amd_iommu=off`, re-bench).

### Rollback
If quorum won't restore, OOM/heavy swap, GPU ring resets, or tok/s regresses past tolerance: restore
`/etc/default/grub.bak.*` → `update-grub` → raise the BIOS carve back → reboot that host. Do not advance.

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
  --env CTX=16384 --env PARALLEL=1 --env "EXTRA_ARGS=--image-min-tokens 1024"
```
- **Use GPU mmproj offload (the default — do NOT pass `--no-mmproj-offload`).** GPU offload is both
  correct and fast for Qwen3-VL-8B (~0.5 s image-encode, ~2 s end-to-end). `--no-mmproj-offload`
  (CPU encoder, a defensive workaround for a Vulkan vision bug that does NOT affect this model) made
  image description take **>1 minute** — only reach for it if you ever observe garbled descriptions.
- In Open WebUI select `qwen3-vl-8b` for images; enable its Vision capability if the upload button
  is hidden (Admin → Models → qwen3-vl-8b → Capabilities → Vision).
- `--image-min-tokens 1024` (set above via EXTRA_ARGS) gives the model enough visual tokens for
  dense OCR/grounding — costs ~1.7 s prompt vs ~0.5 s without, ~3 s end-to-end (worth it).
  (`lxc-exec.py` shell-quotes env values, so multi-word EXTRA_ARGS passes through intact.)

## Model refresh 2026-06: Qwen3.6-35B-A3B + Gemma-4-26B-A4B (node1)
Web-validated upgrades (ADR/validation): **Qwen3.6-35B-A3B** (hybrid Gated-DeltaNet MoE, ~35B/3B
active, coding **+ vision**) replaces `qwen3-coder-30b-a3b` on `:8081`; **Gemma-4-26B-A4B** (Google
QAT, vision image+video — NOT audio) replaces `qwen3-vl-8b` on `:8082`. Both need the new arches, so
the llama.cpp pin is bumped **b9631 → b9672** (adds `qwen35moe` + `gemma4`). node1 then holds general
(~18.6G) + Qwen3.6 (~21G) + Gemma-4 (~15.6G) ≈ 55G — fits the 64 GB carve (tight; smoke-test first).
```bash
# 1. Download the GGUFs to NFS (on a Proxmox host)
ssh root@192.168.0.2 'bash -s' < scripts/fetch-models.sh qwen3.6
ssh root@192.168.0.2 'bash -s' < scripts/fetch-models.sh gemma4

# 2. Bump node1's llama.cpp to b9672 by re-provisioning the DEFAULT instance (restarts general :8080
#    on the new build — verify it still answers before continuing).
python scripts/lxc-exec.py 192.168.0.2 5001   # LLAMA_BUILD now defaults to b9672

# 3. SMOKE-TEST each new arch on Vulkan/gfx1151 as a throwaway instance BEFORE cutting over the
#    routing (the qwen35moe hybrid + gemma4 vision paths are newer on the Vulkan backend):
MSYS_NO_PATHCONV=1 python scripts/lxc-exec.py 192.168.0.2 5001 \
  --env INSTANCE=qwen36 --env PORT=8081 \
  --env MODEL=/models/qwen3.6-35b-a3b/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf \
  --env MODEL_ALIAS=qwen3.6-35b-a3b --env MMPROJ=/models/qwen3.6-35b-a3b/mmproj-F16.gguf \
  --env CTX=32768 --env PARALLEL=1   # PARALLEL=1 => full 32768 tok/request. llama.cpp splits -c (CTX)
                                     # across --parallel slots, so PARALLEL=2 gave only 16384/request,
                                     # which overflowed on tool-heavy agent prompts ("list workflows"
                                     # ~= 20k tok -> 400 ContextWindowExceededError).
MSYS_NO_PATHCONV=1 python scripts/lxc-exec.py 192.168.0.2 5001 \
  --env INSTANCE=gemma4 --env PORT=8082 \
  --env MODEL=/models/gemma-4-26b-a4b/gemma-4-26B_q4_0-it.gguf \
  --env MODEL_ALIAS=gemma-4-26b-a4b --env MMPROJ=/models/gemma-4-26b-a4b/gemma-4-26B-it-mmproj.gguf \
  --env CTX=16384 --env PARALLEL=1
# Confirm: /health ok on :8081 + :8082, a text completion, and an image describe (gemma4 vision is
# newer in llama.cpp — if mmproj load errors, see issues #21402/#21497 and pin a known-good build).
```
The old `qwen3-coder-30b` / `qwen3-vl-8b` GGUFs stay on NFS (revert by re-pointing the instances).
The k8s Services were renamed `llm-coder→llm-qwen36`, `llm-vision→llm-gemma4` and the LiteLLM
`model_list` updated (both `supports_vision: true`); a `git push` reconciles them via Flux.

## Qwen3.6 daily driver + long-context config for agentic flows (2026-07-01, ADR 0015)
Qwen3.6 is node1's **daily driver on :8080** (the `llm` Service) and serves its **native 256K** window
(`n_ctx_train=262144`) instead of 32K, so tool-heavy agent prompts stop hitting `400
ContextWindowExceededError`. It **replaced qwen3-30b-a3b** (retired 2026-07-01 — Qwen3.6 is a strict
upgrade: coding + image/video vision) and was **consolidated from the old :8081 `qwen36` instance onto
:8080** (that unit + the `llm-qwen36` Service were removed). **Gemma-4 is on-demand** on :8082
(`systemctl disable --now llama-server-gemma4`); Qwen3.6 covers image+video, so no steady-state vision is
lost. Steady-state launch is the **default instance** (:8080) — source-of-truth, **keep in sync with
`litellm.yaml`**:
```bash
MSYS_NO_PATHCONV=1 python scripts/lxc-exec.py 192.168.0.2 5001 \
  --env MODEL=/models/qwen3.6-35b-a3b/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf \
  --env MODEL_ALIAS=qwen3.6-35b-a3b --env MMPROJ=/models/qwen3.6-35b-a3b/mmproj-F16.gguf \
  --env CTX=262144 --env PARALLEL=1 \
  --env CACHE_TYPE_K=q8_0 --env CACHE_TYPE_V=q8_0   # default instance (:8080); provision.sh KV knobs
```
Key facts (measured; see ADR 0015 for the table):
- **No YaRN** — native `n_ctx_train` is already 262144; just raise `-c`. `--cache-type-k/v q8_0` is
  near-lossless with flash-attn (auto). On this **hybrid Gated-DeltaNet + SWA** model the KV is tiny:
  256K KV ≈ **+2.2 GiB** over the 32K baseline. node1 now runs **only Qwen3.6** (~21 GiB weights + KV ⇒
  VRAM **~23 GiB / 64**, GTT ~0) after retiring qwen3-30b-a3b and moving Gemma-4 on-demand.
  A big `-c` is **free until actually used** (cost scales with real prompt length, not the window).
- **Prefill dominates long-context latency.** Cold/divergent prefill ~895 tok/s @10K, tapering to
  ~549 tok/s @56K (≈1–5 min at 64K–256K fill); decode ~60 tok/s. A **single growing conversation** on the
  one slot reuses the retained recurrent state (measured ~1 s to re-attach a 34K prefix); a conversation
  **switch / edited history / interleaved request** forces a full re-prefill (`forcing full prompt
  re-processing …` in the journal). Future win: bump the llama.cpp pin when hybrid/SWA prompt-caching improves.
- **Reasoning model.** Output is a `<think>` trace in `reasoning_content` **before** the answer/tool-call;
  too small a `max_tokens` returns empty `content` + `finish_reason:"length"`. LiteLLM `max_input_tokens`
  is **245760** (256K − ~16K output headroom). Tool-calling verified through `--jinja`.
- **Revert:** re-run with `--env CTX=32768` and drop the `CACHE_TYPE_*` envs. To restore qwen3-30b-a3b as a
  separate model (GGUF kept on NFS): re-provision it as an `INSTANCE=<name> PORT=8081` instance + re-add its
  LiteLLM entry and the `llm-qwen36` Service. Re-enable Gemma-4 with `systemctl enable --now llama-server-gemma4`.

## Verify
```bash
curl http://192.168.0.44:8080/health                      # {"status":"ok"}  (node1 LXC; .45=node2, .46=node3)
curl http://192.168.0.44:8080/v1/models                   # lists the served model
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
