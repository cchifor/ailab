# Runbook: on-demand LLM loading (llama-swap)

The two heavyweight models are **rarely used** but each pins ~59 GiB (gpt-oss, node2) / ~71 GiB
(Qwen3.5-122B, node3) of **GTT (system RAM lent to the iGPU)** for the life of the `llama-server`
process. Pinned, they keep node2/node3 at ~85–95 % RAM, break virtio-ballooning, and block a 2nd
dev-worker per node. **llama-swap** fronts `:8080`, loads the model on the first request, and
**idle-unloads it after a TTL** — returning that GTT to the host so ballooning works and the
dev-workers fit. node1's daily driver stays pinned (interactive latency).

- provisioner: `kubernetes/infra/ai-lxc/provision.sh` (`SWAP=true` mode) via `scripts/lxc-exec.py`
- config source-of-truth: `kubernetes/infra/ai-lxc/models.yaml`
- router: `kubernetes/apps/apps/ai/litellm.yaml` (backends already point at `:8080`; `request_timeout: 900` covers cold start)

## Model → node → mode

| Node | ctid | LXC IP | Model | Mode | GTT freed when idle |
|---|---|---|---|---|---|
| ai-node1 | 5001 | .44 | Qwen3.6-35B (daily driver) | **pinned** (direct `llama-server.service`) | — (kept warm) |
| ai-node2 | 5002 | .45 | gpt-oss-120B | **llama-swap**, ttl 900 s | ~59 GiB |
| ai-node3 | 5003 | .46 | Qwen3.5-122B | **llama-swap**, ttl 900 s | ~71 GiB |

## Deploy (per managed node)

**Prerequisite — add the local model cache first.** `tofu -chdir=kubernetes/infra/ai-lxc apply` adds a
`/models-local` mount (managed local-lvm volume) to the node2/node3 LXCs (`ai_llm_nodes[].model_cache_gb`).
This restarts those CTs (brief LLM downtime) but leaves node1 untouched. Then provision: `provision.sh`
rsyncs the GGUF from NFS to `/models-local` (first run only, ~1-5 min copy) and serves it from local NVMe.

`lxc-exec.py` pushes `provision.sh` + companions into the CT and runs it with the env below. Verify
the current llama-swap release tag first and pass `--env LLAMA_SWAP_VERSION=vNNN` if the default in
`provision.sh` is stale (asset: `llama-swap_<NNN>_linux_amd64.tar.gz`). `MODEL` points at the LOCAL copy;
`MODEL_STAGE_SRC` is the NFS source that gets staged in.

```bash
# node2 — gpt-oss-120B, managed (idle-unload 15 min), served from local NVMe
python scripts/lxc-exec.py 192.168.0.3 5002 \
  --env SWAP=true --env TTL=900 \
  --env MODEL=/models-local/gpt-oss-120b/gpt-oss-120b-mxfp4-00001-of-00003.gguf \
  --env MODEL_STAGE_SRC=/models/gpt-oss-120b \
  --env MODEL_ALIAS=gpt-oss-120b --env CTX=8192 --env PARALLEL=1 --env EXTRA_ARGS=--no-mmap

# node3 — Qwen3.5-122B, managed. This is what frees node3 for dev-worker-6.
python scripts/lxc-exec.py 192.168.0.4 5003 \
  --env SWAP=true --env TTL=900 \
  --env MODEL=/models-local/qwen3.5-122b-a10b/Qwen3.5-122B-A10B-Q4_K_M-00001-of-00003.gguf \
  --env MODEL_STAGE_SRC=/models/qwen3.5-122b-a10b \
  --env MODEL_ALIAS=qwen3.5-122b --env CTX=8192 --env PARALLEL=1 --env EXTRA_ARGS=--no-mmap
```

Omit `MODEL_STAGE_SRC` (and point `MODEL` back at `/models/...`) to serve straight from NFS without the
local cache.

`provision.sh` (SWAP mode) installs the llama-swap binary, renders `/etc/llama-swap/config.yaml` (one
model, the `cmd` = the same `llama-server` invocation, `ttl` = `TTL`), writes `llama-swap.service` on
`:8080`, and **stops+disables the old `llama-server.service`** (freeing the port). node1 is left as-is
(no `SWAP` env) — do NOT run this on node1.

## The easy config knob (pin ↔ manage)

`ttl` is the switch. It lives in `models.yaml` (intent) and is passed as `TTL` at deploy time:
- `TTL=900` (or any >0) → **managed**: unload after N seconds idle.
- `TTL=0` → **pinned via llama-swap**: loads on first request, never idle-unloads (RAM stays held).
- `SWAP` unset → **direct**: fixed `llama-server.service`, always resident (node1's mode).

To change a model's behaviour, re-run the deploy with a new `TTL` (or add/remove `SWAP`). To make a
node serve *multiple* models (swap between them on demand), add more entries under `models:` in
`/etc/llama-swap/config.yaml` — llama-swap loads whichever the request names (one at a time per iGPU).

## Cost & the time-share rule

- **Cold start** (first request after idle — full GGUF re-read over NFS): gpt-oss ~1.5–4 min, the 122B
  ~4–5 min, plus GTT populate. `litellm request_timeout` (900 s) and llama-swap `healthCheckTimeout`
  (900 s) both cover it. Acceptable for rarely-used models; **node1 stays warm** for anything interactive.
- **A node serves EITHER its heavyweight OR its two dev-workers at full tilt — not both.** 125 GiB can't
  hold a ~70 GiB model + 2 workers-at-ceiling. When a heavyweight is loaded, its node's dev-workers are
  pinned near their 4 GiB balloon floor (fits: 71 + cp3 28 + runner 10 + 2×4 = 117 < 125). Since the
  models are used ~10 % of the time, workers balloon freely the rest of the time. See
  `docs/runbooks/dev-workers.md`.

## Monitoring

`ServiceMonitor ai-llm-llamacpp` scrapes `:8080/metrics`. With llama-swap the port always answers, but
the `llamacpp:*` series disappear while the model is unloaded (expected). Adjust any
`llamacpp`-absence alert to tolerate the managed nodes, or scrape llama-swap's own metrics endpoint.
`node_exporter` (:9100) and the `amdgpu_*` GTT metrics stay up. Gatus/Homepage don't probe the
heavyweights, so status pages stay green.

## Verify

```bash
# RAM actually freed after idle-unload (wait > TTL with no requests):
python scripts/node-ssh.py 192.168.0.3 "free -g; for f in /sys/class/drm/card*/device/mem_info_gtt_used; do cat \$f; done"
#   expect host 'available' up ~59 GiB (node2) / ~71 GiB (node3); GTT ~0 when unloaded.

# On-demand load works (cold, slow first token, then normal):
curl -s -m 900 http://litellm.ai.svc.cluster.local:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-oss-120b","messages":[{"role":"user","content":"hi"}]}' | head

# llama-swap state:
python scripts/node-ssh.py 192.168.0.3 "pct exec 5002 -- systemctl status llama-swap.service --no-pager | head; pct exec 5002 -- curl -s localhost:8080/v1/models"
```

## Revert a node to pinned/direct
Re-provision with the node's normal direct-mode command (no `SWAP`) — see `docs/runbooks/ai-host-setup.md`.
That rewrites `llama-server.service` and re-pins the model. (llama-swap.service is left installed but
inert once `llama-server.service` owns `:8080` again; `systemctl disable --now llama-swap.service` to remove.)
