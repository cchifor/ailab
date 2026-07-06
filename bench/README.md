# LLM carve-vs-GTT tok/s benchmark

Measures decode + prefill **tok/s** for the Strix Halo heavyweight models over the *live*
`llama-server` `/completion` path, so it can be run **identically before and after** the BIOS UMA
(VRAM carve) change to quantify any carve→GTT performance penalty.

- Harness: [`scripts/bench-llm.py`](../scripts/bench-llm.py)
- Design/methodology: [`docs/superpowers/specs/2026-07-06-llm-carve-vs-gtt-benchmark-design.md`](../docs/superpowers/specs/2026-07-06-llm-carve-vs-gtt-benchmark-design.md)
- Context: the memory-allocation analysis — each Bosgame M5 carves a fixed **64 GiB** BIOS UMA VRAM
  block that the OS cannot reclaim without a BIOS change; the proposed fix is **small carve + large GTT**
  (`ttm.pages_limit`). It's only worth doing if heavyweight tok/s **hold** when the model is served from
  GTT instead of the carve. This benchmark is the go/no-go evidence.

## Reproduce (run the SAME command before and after)

```bash
# Baseline, already captured (both heavyweights, in place):
python scripts/bench-llm.py run --sizes 512,4096,7680 --label before-bios

# AFTER the BIOS carve reduction + kernel ttm.pages_limit change:
#   1. reload the SAME model on the SAME node with the SAME launch config (n_ctx=8192, same llama.cpp
#      build) — only the BIOS carve + kernel params should differ, so carve->GTT is the only variable.
#      (node2 -> gpt-oss-120b, node3 -> qwen3.5-122b; see docs/runbooks/ai-host-setup.md)
#   2. re-run with the identical --sizes:
python scripts/bench-llm.py run --sizes 512,4096,7680 --label after-bios

# Compare (globs ok):
python scripts/bench-llm.py compare bench/results/before-bios-*.json bench/results/after-bios-*.json
```

Notes:
- `--sizes` **must match** across runs. The current heavyweights launch at `n_ctx=8192`, so the sweep
  tops out at 7680 (7680 + 256 gen + margin < 8192); the default `16384` point would be auto-skipped.
  If you raise `n_ctx` after the carve change to exploit the freed memory, keep a `512,4096,7680` run for
  the clean carve→GTT delta, and optionally add a long-context run (`--sizes 512,16384,65536 --label
  after-bios-longctx`) to probe prefill where a GTT penalty is most visible.
- Determinism: `temperature 0`, `seed 0`, `ignore_eos` (exactly `n_predict` tokens), `cache_prompt:false`
  (real prefill every run). Prompts are built from the model's own tokenizer, byte-identical run-to-run.
- Each result JSON self-documents its **memory regime** (`mem_before`/`mem_after`: carve size = VRAM
  total, plus VRAM/GTT used) and the host `/proc/cmdline`, so a delta is always read against the regime
  that produced it.

## Baseline — `before-bios`, 2026-07-06 (64 GiB carve, GTT at default 31.2 GiB)

Median of 3 runs (+1 warmup discarded); run-to-run variance was <1%.

**node2 · gpt-oss-120b** — served entirely from the carve (VRAM 59.0 GiB used, GTT 0.6 GiB)

| prompt ctx | prefill tok/s | decode tok/s |
|---:|---:|---:|
| 512 | 426.3 | 49.2 |
| 4096 | 512.9 | 48.1 |
| 7680 | 496.9 | 46.6 |

**node3 · qwen3.5-122b** — already carve-maxed + spilling to GTT (VRAM 64.0 GiB, **GTT 7.9 GiB**)

| prompt ctx | prefill tok/s | decode tok/s |
|---:|---:|---:|
| 512 | 258.5 | 22.3 |
| 4096 | 281.1 | 22.1 |
| 7680 | 276.4 | 21.9 |

Interpretation for the after-BIOS comparison:
- **decode tok/s** is the headline latency metric users feel; **prefill tok/s** is the most
  bandwidth-sensitive and therefore where a carve→GTT penalty would show first.
- gpt-oss is a **pure-carve** baseline → its after-BIOS run (fully in GTT) is the cleanest carve-vs-GTT
  signal.
- the 122B baseline **already runs ~8 GiB from GTT** with no obvious penalty — encouraging, but after the
  carve reduction the *entire* ~72 GiB moves to GTT, which the after-run will measure directly.
