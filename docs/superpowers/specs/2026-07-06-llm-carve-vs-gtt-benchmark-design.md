# Design — LLM carve-vs-GTT tok/s benchmark

**Date:** 2026-07-06 · **Status:** APPROVED (design) → implementation
**Related:** ADR 0008 (AI LLM appliance), `docs/runbooks/ai-host-setup.md`, the memory-allocation
analysis (2026-07-05/06): each Bosgame M5 carves a **fixed 64 GiB BIOS UMA VRAM** block that cannot be
reclaimed by the OS without a BIOS change; the proposed fix is **small BIOS carve + large GTT**
(`ttm.pages_limit`). That fix is only worth doing if heavyweight decode/prefill tok/s **hold** when the
model is served from GTT instead of the VRAM carve.

## Goal
A reproducible, non-disruptive benchmark of **decode tok/s** and **prefill tok/s** for the two
heavyweight models, runnable **identically before and after** the BIOS UMA change, so we can measure any
carve→GTT performance penalty per (model, context-size). Each result self-documents its memory regime
(carve size + VRAM/GTT split) so the before/after comparison is interpretable.

## Non-goals
- Not `llama-bench` / not stopping the server (explicitly chose the live serving path).
- Not benchmarking the daily driver (Qwen3.6) — the carve→GTT question is about the heavyweights that
  actually approach/exceed the carve.
- Not automating the BIOS change or kernel-cmdline edit (tracked separately).
- No concurrency/throughput-under-load test — single-stream latency/tok/s only.

## Method
Drive the running `llama-server` on each node via its **native `/completion`** endpoint (avoids chat-template
token variance) and read the `.timings` block:
- **decode tok/s** = `timings.predicted_per_second`
- **prefill tok/s** = `timings.prompt_per_second`

Request params (fixed for determinism): `temperature:0, seed:0, n_predict:256, cache_prompt:false,
stream:false`. `cache_prompt:false` forces a real prefill on every run. Requests are issued **sequentially**
(one in flight) so the benchmark does not disrupt normal serving.

## Targets & node map
Default targets = the two heavyweights, benchmarked in place:
- **node2** — gpt-oss-120B — LXC `192.168.0.45:8080` — Proxmox host `192.168.0.3`
- **node3** — Qwen3.5-122B — LXC `192.168.0.46:8080` — Proxmox host `192.168.0.4`

Built-in node map (LXC endpoint → Proxmox host, for sysfs reads over SSH):
`node1{host .2, lxc .44} · node2{host .3, lxc .45} · node3{host .4, lxc .46}`.
Targets are overridable on the CLI so any node/model/port (incl. a future small-carve node) can be run.

## Workload (standard sweep)
Prompt sizes **[512, 4096, 7680]** tokens (the default; matches the committed baseline at `n_ctx=8192`) ×
**(1 warmup + 3 measured)**, `n_predict:256`.
- Prompts generated **deterministically** (a fixed filler paragraph repeated to the target size) so
  before/after prompts are byte-identical. Actual `prompt_n` from the response is recorded regardless.
- **Auto-fit guard:** read `n_ctx` from `/props`; skip (and log) any sweep size that would not fit
  `n_ctx − n_predict − margin`. e.g. a server launched at `-c 8192` skips the 16384 point — and skips it
  again after BIOS (same launch config), keeping the comparison consistent.
- Report **median** decode/prefill tok/s per size, plus min/max across the 3 measured runs.

## Memory-regime capture (interpretability)
Immediately before and after each model's sweep, SSH the mapped Proxmox host via
`python scripts/node-ssh.py <host> "<cmd>"` and record, from the LXC's card0 device:
`mem_info_vram_total` (**the carve size** — identifies big-carve vs small-carve), `mem_info_vram_used`,
`mem_info_gtt_total`, `mem_info_gtt_used`, plus host `/proc/cmdline`. Also capture, from the server:
`/props` (`n_ctx`, model path, `n_parallel`, build/system info if present) and `/v1/models` (served id).

## Output & comparison
- **`run`** writes one JSON to `bench/results/<label>-<UTCstamp>.json` (repo-tracked so the "before"
  survives until the BIOS change and stays diffable). `--label` default `before-bios`.
- JSON schema (per file): `{label, started_at, tool_version, targets:[{node,label,endpoint, model_id,
  n_ctx, build, cmdline, mem_before:{carve_bytes,vram_used,gtt_total,gtt_used}, mem_after:{…},
  sweep:[{prompt_size, prompt_n, decode_tps:{median,min,max}, prefill_tps:{median,min,max}, runs:[…]}]}]}`.
- **`compare`** takes ≥2 result files → prints a table per (model, context-size) with decode/prefill
  medians and **% delta**, prefixed by a memory-regime line (carve 64 GiB→512 MB, GTT used X→Y GiB) so a
  delta is read against the regime that produced it. Matches targets across files by `model_id`.

## Implementation notes
- Single script `scripts/bench-llm.py`, **stdlib only** (`urllib.request`, `json`, `argparse`,
  `subprocess`, `statistics`, `time`) — matches the repo's dependency-light tooling; runs on the Windows
  host (Python 3.14) which already reaches `192.168.0.0/24`.
- `subprocess` shells out to `python scripts/node-ssh.py <host> "…"` for sysfs (reuse existing access
  path; no new SSH code).
- Per-request HTTP timeout generous enough for a cold 16k prefill on the 122B (minutes) — configurable,
  default 600 s.

## Guardrails
- Abort a target if `/health` ≠ ok or `/props` unreachable (record the failure, continue other targets).
- Read-only except the benchmark completions themselves; **no restarts, no model loads/unloads**.
- Auto-skip over-context sizes rather than erroring.

## How to run
```bash
# Baseline (before BIOS), both heavyweights in place (node2+node3 default; sizes match the baseline):
python scripts/bench-llm.py run --sizes 512,4096,7680 --label before-bios
# After the BIOS carve reduction + ttm.pages_limit, reload same models on same nodes, then (identical --sizes):
python scripts/bench-llm.py run --sizes 512,4096,7680 --label after-bios
python scripts/bench-llm.py compare bench/results/before-bios-*.json bench/results/after-bios-*.json
```

## Acceptance criteria
1. `run` produces a valid JSON with non-zero decode+prefill medians for every fitting sweep point on both
   heavyweights, plus captured carve size and VRAM/GTT split per target.
2. A second `run` with the same params yields tok/s within a small run-to-run variance (sanity of
   determinism) — spot-checked, not asserted in code.
3. `compare` prints a per-(model,context) delta table across two files.
4. The baseline `before-bios` result is committed under `bench/results/`.
