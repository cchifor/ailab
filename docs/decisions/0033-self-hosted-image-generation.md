# ADR 0033 — Self-hosted image generation: Qwen-Image-2.1 on cloud2's idle GPU, one OpenAI-shaped API

**Status:** ACCEPTED (2026-09-29), operator-directed: *"install the Qwen-Image-2.1 on one of the cloud
machines"*, with Open WebUI using it for generation **and** editing at best quality, and a fast
variant *"accessible through API so agents can generate images"*.
**Relates to:** ADR 0022 (model registration: LiteLLM is the one edit), cloudlab ADR 0001 (day-only
operation). Serving side: `git.chifor.me/cchifor/cloudlab` runbook §14; research plan
`~/.claude/plans/analyze-on-the-web-precious-clover.md` (session record, not in-repo).

## Context

Nothing on the estate generated images. Qwen-Image-2.1 (Alibaba Qwen, 2026-09-20) is a 7B DiT with a
Qwen3-VL-8B text encoder that does text-to-image **and** instruction editing (up to 10 references)
in one model, with strong text rendering. It is under the **Qwen Research License: non-commercial**.

Constraints found on 2026-09-29:
- **Memory.** BF16 peaks at 34–56 GB, so it does not fit on one 24 GB RTX 3090. Comfy-Org ships
  official **INT8 ConvRot** weights (7.3 GB transformer, 9.4 GB encoder, 0.7 GB VAE) that do fit.
  Ampere has INT8 tensor cores; FP8 would only save memory there.
- **Where a card is free.** cloud3's four cards hold the default vLLM engine, and its
  `gpu-idle-guard.sh` fails every llama-swap load if *any* card is busy. cloud1's only card is
  faulty. **cloud2's `GPU-db1d635e` (`4a:00.0`) has been idle since the 2026-09-20 TP=2 degrade**, is
  already bound into LXC 5103, and the host runs the newest driver (580 / CUDA 13.0).
- **Serving stacks.** ComfyUI has native day-0 support. vLLM-Omni's support is an unmerged PR tested
  only on H200/GB200 in BF16. SGLang/diffusers are not validated on sm_86.

## Decision

1. **ComfyUI on loopback + our own OpenAI-shaped API on the LAN**, in LXC 5103 on cloud2, pinned by
   GPU UUID the way the vLLM unit pins its cards. `comfyui.service` listens on 127.0.0.1:8188 only
   (its `/prompt` executes any graph); `qwen-image-api` on `192.168.0.28:8190` is the LAN surface and
   only submits its own committed workflows. Model id picks the workflow:
   - `qwen-image-2.1` — the Comfy-Org template, INT8, 40 steps (edits 25). **Open WebUI's default**
     for both generation and editing.
   - `qwen-image-2.1-fast` — Viggle's turbo v0.2.1 LoRA, 6 steps, applied **unmerged** through
     Viggle's own node (ComfyUI's stock loaders merge it, which on INT8 weights adds noise ~4× the
     update). **For agents.**
2. **Registered on the main LiteLLM only**, as `qwen-image-2.1-cloud` / `qwen-image-2.1-fast-cloud`
   with `model_info.mode: image_generation` (kept out of the chat pickers by the consumer generator).
   **Not** on `litellm-local` / `litellm-vkeys`: those serve the Strive platform's tenant keys, and
   the license is non-commercial.
3. **Open WebUI uses its `openai` image engine through LiteLLM**, not its native `comfyui` engine:
   the native engine would need the workflow JSON and node map in env here (env is authoritative,
   `ENABLE_PERSISTENT_CONFIG=false`), duplicating what the cloudlab API owns.
4. **Admission control in the API, not in LiteLLM.** One GPU runs one graph at a time; the API admits
   4 requests and answers 429 beyond that, because LiteLLM does not propagate client cancels.

## Measured (2026-09-29, on the box, warm, 1024²)

| | generate | edit | 2048² |
|---|---|---|---|
| `qwen-image-2.1` | 22.5 s (40 steps; 14.2 s at 25) | 24.5 s | 113 s (25 steps) |
| `qwen-image-2.1-fast` | 4.8 s | 12.7 s | 33 s |

Peak 22.1 GiB VRAM (2048²), 250 W cap held, 70 °C, no Xid through a 30-minute burn-in. 40 steps
replaced the template's 25 after a dense-text test in which 25 added a garbled extra line. The fast
model garbled one line of the same 5-line menu, which is why it is not Open WebUI's default.

## Consequences

- **TP=4 conflict.** Refitting cloud2's fourth card and returning vLLM to TP=4 needs this card back:
  stop and disable `qwen-image-api` + `comfyui` first (and pull these routes), or keep vLLM at TP=2.
- **Shared blast radius.** The card had Xid 79 events on 2026-09-06 (riser reseated), and a GSP hang
  on this host has wedged it before (cloudlab runbook §10a) — which would take the vLLM route down
  with it. Burn-in was clean; `ImageApiFailing`/`ImageApiDown` (cloudlab monitoring) watch it, and
  rollback is `systemctl disable --now qwen-image-api comfyui`.
- **Day-only**, like every cloud route: the image button errors while the `pve` cluster is off.
- **Cosmetic.** Open WebUI's External connection discovers everything LiteLLM's `/v1/models` lists, so
  the two image ids appear in the chat picker's External group, as the embedding routes already do.
  Selecting one for chat fails; hide them per-model in Open WebUI's admin if that matters.
- **ComfyUI nightly churn** is contained by pinning its commit, the torch build and every weight's
  sha256 in `host/install-comfyui-cloud2.sh`; an upgrade is a deliberate bump plus
  `scripts/image-api-test.py`, whose oracle is a vision model reading back a random number painted
  into the image (HTTP 200 is not health).
