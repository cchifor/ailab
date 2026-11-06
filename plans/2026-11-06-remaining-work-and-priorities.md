<!-- status: active (the owner's working plan; update it as items close) -->
# Remaining work and priorities, from 2026-11-06

**Date:** 2026-11-06, written after the day's changes. **Scope:** the cloud GPU estate and llm-router: the qwen3.8-27b
migration, router quality, the hardware faults on cloud1 and cloud3, and the move to two cards per host.

## Where things stand

| Area | State |
|---|---|
| flash-next, fp8 | Retired from every consumer, the router and cloud3 |
| qwen3.8-27b | Serving on cloud2 and cloud3 through the router (rev 2262) and LiteLLM |
| `!` degenerate stream | About 0.2 % of requests in the lab, on both machines, direct to vLLM, not the router. Cause unknown |
| Router guard | Live in `observe` on both qwen accounts. It logs only, so a `!` stream can still stall an agent |
| Router, token fix | Merged and deployed: `x-codex-turn-state` is relayed. Its effect on the cache is unproven |
| cloud3 memory | Hardware fault. 236 GiB test clean on 11-06. Rebooted with `memmap=64K$0x295aad0000`; healthy so far, and one boot proves little |
| cloud1 memory | 116 GiB test FAILED: 391 errors, all in `0x7f9850000`-`0x7f9857fff`, byte lane 5. 16 pages retired by soft-offline; no boot-time exclusion yet |
| cloud1 provisioning | Fixed in cloudlab #57: it no longer installs NVIDIA 550 packages over the runfile driver. Stale package purged |
| Router Part 2 | Plan in review (ailab #1645); foundations PR in review (llm-router #148); no behaviour change yet |
| Router Part 3 | On hold, not implemented. The plan is in llm-router `docs/plans/2026-11-06-claude-live-sessions.md` |

### Validation, on image `225bb804`, 6 pairs

| | Codex | Claude (`claude-4`, Sonnet) |
|---|---|---|
| Quality | 18/18 in both arms | 30/30 in both arms |
| Model input | Identical | Identical, apart from declared items |
| Speed | Clean: 19.7 vs 20.1 s against a no-router control | **1.29-1.41x slower** on four of five tasks, 1-2 s per call |
| Cache | Intermittent dip on the live router; gate fails at 1.48x. About 3 % more input tokens on a short session | Not measurable through this path |

The router's code is cleared for the cache dip: the same image on the host login matched the control (90 % vs 93 %). The
live router scored 60 %, then 96 % minutes later. Concurrent router traffic is a lead, from one pair of windows.

## Tomorrow, in priority order

### 1. Start of day (about 15 min)
- Owner: merge cloudlab #54, then #55 (retargeted to `main`), then #57. **Not #56:** it merges on the day of the card move.
- Check cloud3's unattended 06:00Z boot: `memmap` in `/proc/cmdline`, no `Caught <` in the journal.
- Read the guard's `observe` data. Router traffic to qwen has been light, so this needs real load to mean anything.

### 2. Stop the `!` stalls: guard to `enforce`
- It is the only protection against the known hang. It trips in about 6 s, and a retry on the other machine is invisible.
- Owner decision, on the `observe` data. False positives on reasoning text are unmeasured; one costs a retry.
- Then run the release gate for Codex and qwen.

### 3. Hardware session 1: the card move (needs the owner at the machines, after 06:00Z)
Ready once item 1 and the steps below are done. It needs over 2 h of soak before the 20:00Z power-off, so start early.
- Mine, before: add cloud1 to `host/memmap-hosts` for its two blocks, so the owner is present at its first `memmap` boot.
- Owner: Gen4 x16 risers on `PCIEX16_1` and `PCIEX16_2` plus a spare; two separate 8-pin cables per card; monitor and keyboard.
- Move `GPU-ab0457e9` (cloud3 `23:00.0`) into cloud1 `PCIEX16_1`, after a fan-pulse check.
- Leave the DIMMs alone. A change voids the retired-page tables.
- cloud3's shutdown can hang: check for a crash first, stop guests first, hold the power button if it is still on after 3 minutes.
- Merge #56 and apply on the day. CI on `cloud-ci-1/2` then runs about 15 % slower on heavy jobs, up to about 40 %.
- Then: soak, the on-demand image path, and move images to cloud1.

### 4. Claude speed: measure the API path
- A small, capped test of `pool-claude-api` against the seat path: wall time and quality on the same gate tasks. It costs real money.
- If it clears the 1.25x limit, decide which workloads move, starting with Codex on Fable on dev-worker-4.

### 5. Router Part 2: native pass-through to vLLM
- Finish #1645 and #148, then the native wire, default-off, then a one-route canary. Each roll needs the owner's go.
- Dependency: gate 1 needs cloudlab to publish deployment metadata for cloud2.

### 6. Find the `!` cause
- Blind runs cannot show a cause at 0.2 %, and 0 of 31 attempts reproduced it. Capture first: have the router keep a request's
  body, encrypted and short-lived, when the guard trips. Then test prefix caching, speculative decoding and the fp16 state on
  a real trigger.

### 7. Codex cache dip
- The live router with and without concurrent load, at a fixed time, with nothing else running. Do not run gates in parallel
  when measuring cache.

### 8. Owner decisions still open
- cloud3's gate exception: uncached tokens 1.20-1.29x, quality 6/6. Recommended: keep it on the route.
- D9, the patched llama-swap for cloud1. Treated as yes; correct it if not.
- Whether to lower cloud1's RAM speed. The errors are local, not scattered, so it looks like a chip defect and the speed is
  not the likely cause. Recommended: leave it, and replace the DIMM eventually.
- Whether cloud1's and cloud3's DIMMs get replaced.

### 9. Housekeeping
- Correct the runbook's "halves" note. Record today's validation in the results document.
- Alert on `Freezing execution`; a short output check after each boot.
- A backup retention rule: the router's `/data` is 4.9 GB, and each backup takes about 250 MB.
- `codex-4` seat re-auth; cloud2 GPU2 temperature; the Phoenix serial; remove temporary worktrees.

### After that
Hardware session 2 (cloud2's image card to cloud1), then the availability probe, then cloud1 joins the route as the third backend.
