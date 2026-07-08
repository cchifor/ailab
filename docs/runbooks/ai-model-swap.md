# Runbook: on-demand LLM loading (llama-swap)

**Topology (2026-07-08).** The daily driver **Qwen3.6-35B (~24 GiB) is PINNED on node1 AND node2** —
two warm nodes give redundancy + LiteLLM load-balancing, and keep node2 light so its runners/dev-workers
have RAM (the old "gpt-oss pinned on node2" layout over-subscribed node2 → OOM). **node3 is the
heavyweight node: llama-swap serves ONLY qwen3.5-122b on demand**, idle-unloading after the TTL so
node3's GTT is free the rest of the time.

> **Single-model on node3 (2026-07-08).** node3 previously multi-served qwen3.5-122b **OR** gpt-oss-120b.
> But the two (~71 GiB + ~59 GiB) cannot co-reside in node3's 96 GiB iGPU LXC, so llama-swap held only
> one at a time. With interactive traffic on qwen3.5-122b and the deepagent platform on gpt-oss, the two
> demand streams **interleaved and forced a full evict + cold-reload on nearly every crossover** — measured
> **~55–82 s per request, 4/4**, turning ordinary use into "loading doesn't work". Fix: node3 serves a
> **single** heavyweight (qwen3.5-122b). gpt-oss-120b is retired locally and its LiteLLM `model_name` is
> **shimmed to the pinned qwen3.6** (`kubernetes/apps/apps/ai/litellm.yaml`) so callers keep working. The
> LiteLLM routing change alone stops the thrash (no traffic reaches node3's gpt-oss); re-provisioning node3
> to a single-model llama-swap config (below) is follow-up cleanup. To bring gpt-oss back you must give up
> qwen3.5-122b — they cannot share node3.

- provisioner: `kubernetes/infra/ai-lxc/provision.sh` — direct/pinned (`MODEL`) or llama-swap
  (`SWAP=true`, single `MODEL` or multi-model `MODELS_JSON`) — via `scripts/lxc-exec.py`
- config source-of-truth: `kubernetes/infra/ai-lxc/models.yaml`
- routing: `kubernetes/apps/apps/ai/llm-service.yaml` (Endpoints) + `litellm.yaml` (`request_timeout: 900`)

## Model → node → mode

| Node | ctid | LXC IP | Model(s) | Mode |
|---|---|---|---|---|
| ai-node1 | 5001 | .44 | Qwen3.6-35B (daily driver) | **pinned** (direct, from NFS) |
| ai-node2 | 5002 | .45 | Qwen3.6-35B (daily driver) | **pinned** (direct, from local NVMe) |
| ai-node3 | 5003 | .46 | Qwen3.5-122B (gpt-oss-120B retired) | **llama-swap** single-model, ttl 1800 s, local NVMe |

## Deploy (per managed node)

**Prerequisite — add the local model cache mount (OUT-OF-BAND, per node).** Do NOT add this via tofu:
bpg marks a `mount_point`'s volume/size as ForceNew, so a tofu-managed mount would destroy+recreate the
running LLM container. Add it with `pct set` (non-destructive; the CT restarts to pick up the mount):

```bash
# node2 (ctid 5002) + node3 (ctid 5003): a 160 GiB local-lvm volume at /models-local
python scripts/node-ssh.py 192.168.0.3 "pct set 5002 -mp1 local-lvm:160,mp=/models-local && pct reboot 5002"
python scripts/node-ssh.py 192.168.0.4 "pct set 5003 -mp1 local-lvm:160,mp=/models-local && pct reboot 5003"
```

`main.tf` has `lifecycle.ignore_changes = [mount_point]`, so this out-of-band mount persists and a future
`tofu apply` won't try to remove it. Then provision (below): `provision.sh` rsyncs the GGUF from NFS to
`/models-local` on first run (~1-5 min copy) and serves it from local NVMe. node1 is not touched.

`lxc-exec.py` pushes `provision.sh` + companions into the CT and runs it with the env below. Verify
the current llama-swap release tag first and pass `--env LLAMA_SWAP_VERSION=vNNN` if the default in
`provision.sh` is stale (asset: `llama-swap_<NNN>_linux_amd64.tar.gz`). `MODEL` points at the LOCAL copy;
`MODEL_STAGE_SRC` is the NFS source that gets staged in.

**Run these from PowerShell**, not Git Bash — MSYS rewrites the `/model...` paths in `--env` args (see
[[ailab-vm-renumber-gotchas]]). node1 is untouched. `\` = bash continuation (use `` ` `` in PowerShell).

```bash
# node2 -> Qwen3.6-35B PINNED (no SWAP), staged to local NVMe. Same config as node1 (256K ctx, q8_0 KV,
# vision mmproj) so both nodes serve an identical model behind the load-balanced `llm` Service.
python scripts/lxc-exec.py 192.168.0.3 5002 --env LLAMA_BUILD=b9672 \
  --env MODEL=/models-local/qwen3.6-35b-a3b/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf \
  --env MODEL_STAGE_SRC=/models/qwen3.6-35b-a3b \
  --env MMPROJ=/models-local/qwen3.6-35b-a3b/mmproj-F16.gguf \
  --env MODEL_ALIAS=qwen3.6-35b-a3b --env CTX=262144 --env PARALLEL=1 --env CACHE_TYPE_K=q8_0 --env CACHE_TYPE_V=q8_0

# node3 -> llama-swap SINGLE-MODEL: qwen3.5-122b only (gpt-oss-120b retired 2026-07-08 to end swap-thrash).
# Re-running this rewrites /etc/llama-swap/config.yaml with ONE model, so a stale gpt-oss entry is dropped.
python scripts/lxc-exec.py 192.168.0.4 5003 --env SWAP=true --env LLAMA_BUILD=b9631 `
  --env MODEL=/models-local/qwen3.5-122b-a10b/Qwen3.5-122B-A10B-Q4_K_M-00001-of-00003.gguf `
  --env MODEL_STAGE_SRC=/models/qwen3.5-122b-a10b `
  --env MODEL_ALIAS=qwen3.5-122b --env CTX=8192 --env PARALLEL=1 --env EXTRA_ARGS=--no-mmap --env TTL=1800
```

`MODELS_JSON` (a jq array) overrides the single `MODEL` and lets one node's llama-swap serve several
models. Omit staging (`stage_from`/`MODEL_STAGE_SRC`) + point `gguf`/`MODEL` at `/models/...` to serve
straight from NFS. LiteLLM routing is set by `llm-service.yaml` Endpoints (llm -> .44+.45, llm-gptoss +
llm-qwen35 -> .46).

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

## OOM protection (why an over-subscribed load never kills the cluster)

node3 (the heavyweight node) can still momentarily over-subscribe — a loaded model + a busy runner + 2
busy dev-workers exceeds 125 GiB. On **2026-07-08 this OOM-killed cp2 AND cp3** (both Talos CPs),
dropping etcd below quorum and taking the k8s API down until they were restarted. To make that
non-fatal, **`scripts/oom-protect-guests.sh`** (a per-host systemd timer, `oom-protect-guests.timer`,
re-applied every 60 s) biases the host OOM killer:

- **Talos CPs (4001/4002/4003) → `oom_score_adj = -1000`** — never killed (etcd quorum protected).
- **GHA-runner + dev-worker VMs → `+750`** — rebuildable, the preferred victims (a runner re-registers,
  a worker is re-created) so a memory crunch sacrifices one of them, not a CP or the loaded model.
- The AI-LLM LXCs keep the default score — protected only relative to the de-prioritised guests, so a
  loaded model usually survives (a runner/worker dies first) without the absolute pin the CPs get.

Deployed to all 3 hosts via `scripts/node-ssh.py`. **Recovery if a CP is OOM-killed anyway:** free the
node's RAM (`pct exec <ctid> -- systemctl restart llama-swap.service` unloads the model) then
`qm start <cp-vmid>`; etcd rejoins on boot. TODO: fold the timer into the `pve_base` ansible role.

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
