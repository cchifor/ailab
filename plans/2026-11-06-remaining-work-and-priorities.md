<!-- status: active (the owner's working plan; update it as items close) -->
# Remaining work and priorities, from 2026-11-06

**Date:** 2026-11-06, written after the day's changes. **Scope:** the cloud GPU estate and llm-router: the qwen3.8-27b
migration, router quality, the hardware faults on cloud1 and cloud3, and the move to two cards per host.

## Where things stand

| Area | State |
|---|---|
| flash-next, fp8 | Retired from every consumer, the router and cloud3 |
| qwen3.8-27b | Serving on cloud2 and cloud3 through the router (rev 2277, `key-edit` release) and LiteLLM |
| `!` degenerate stream | About 0.2 % of requests in the lab, on both machines, direct to vLLM, not the router. Cause unknown |
| Router guard | Live in `observe` on both qwen accounts. It logs only, so a `!` stream can still stall an agent |
| Router, token fix | Merged and deployed: `x-codex-turn-state` is relayed. Its effect on the cache is unproven |
| cloud3 memory | Hardware fault. 236 GiB test clean on 11-06. `memmap=64K$0x295aad0000` held through a clean power-off and the unattended 06:00Z boot on 11-07: no `Caught <`, 16 frames reserved. Two boots prove little |
| cloud1 memory | 116 GiB test FAILED: 391 errors, all in `0x7f9850000`-`0x7f9857fff` (8 pages), byte lane 5. With the known block `0x153f6c0000`-`0x153f6c7fff`, 16 pages in two 32 KiB blocks are retired by soft-offline; no boot-time exclusion yet |
| cloud1 provisioning | Fixed in cloudlab #57 (merged): it no longer installs NVIDIA 550 packages over the runfile driver. Stale package purged |
| Router Part 2 | Foundations, native wire and availability probes merged (llm-router #148, #151, #153), all default-off and none released. No native route serves traffic |
| Router Part 3 | On hold, not implemented. The plan is in the llm-router repository: `docs/plans/2026-11-06-claude-live-sessions.md` |

### Validation, on image `225bb804`, 6 pairs

| | Codex | Claude (`claude-4`, Sonnet) |
|---|---|---|
| Quality | 18/18 in both arms | 30/30 in both arms |
| Model input | Identical | Identical, apart from declared items |
| Speed | Clean: 19.7 vs 20.1 s against a no-router control | **1.29-1.41x slower** on four of five tasks, 1-2 s per call |
| Cache | Intermittent dip on the live router; gate fails at 1.48x. About 3 % more input tokens on a short session | Not measurable through this path |

The router's code is cleared for the cache dip: the same image on the host login matched the control (90 % vs 93 %). The
live router scored 60 %, then 96 % minutes later. Concurrent router traffic is a lead, from one pair of windows.

### Gate on the live image `3715a86c`, 2026-11-07, 6 pairs

This closes the two rows the `key-edit` release left open. Quality is unchanged. The overall report reads FAIL on Claude's
known start-up cost, not on anything new.

| | Direct | Router | Verdict |
|---|---|---|---|
| qwen (cloud2) `code-edit` | 6/6, 14.0 s | 6/6, 12.7 s | PASS, identical model input |
| Claude, 4 tasks, quality | 24/24 | 24/24 | PASS |
| Claude `recall`, wall time | 4.0 s | 5.0 s | **FAIL** at the 1.25x limit |
| Claude `stock-agentic`, wall time | 22.9 s | 30.6 s | Known deviation |

## 2026-11-07, in priority order

### 1. Start of day
- **Done:** cloud3's unattended 06:00Z boot was clean, and its 20:01Z power-off finished. All three hosts up, qwen ready on both.
- **Done:** the guard's journal since it went live: 163 qwen requests, 0 marks. At a 0.2 % fault rate that says nothing, so see item 2.
- Owner: merge cloudlab #58 (#55's cloud1 work) and #59 (the moved-map guard, item 3). **Not #56:** it merges on the day of the card move.

### 2. Stop the `!` stalls: guard to `enforce`
- It is the only protection against the known hang. It trips in about 6 s, and a retry on the other machine is invisible.
- **False positives, measured 2026-11-07:** the real detector over fresh streams from both backends, 248 requests, the
  first 3,000 tokens of each. Normal agent-style prompts: **0 trips in 200** (1.9M reasoning characters; with the journal's 163,
  0 in 363, so at most about 0.8 % at 95 %). Prompts that need a long exact repetition: **9 trips in 48**, led by a
  60-column markdown table (4 of 4), then a decimal of 1/3, 10^300 and 2^400 - 1 in binary.
- **What one costs:** every such trip came late, at 1,408 characters or more, past the 256 that are held back. So it is not
  invisible: the client gets a retryable `DEGENERATE_OUTPUT`, and for a prompt like the table the retry trips too. The real fault
  trips at exactly 256, because it starts on the first token. No `!` stream appeared in this run (248 requests, expected about 0.5).
- **Owner decision.** Recommended: turn `enforce` on now, since it is one config edit and one to undo, then run the gate for Codex and
  qwen. Next release: an option that enforces only a loop at the very start of reasoning, which covers the known fault and every
  false positive measured here, at the cost of not stopping a loop that starts later.

### 3. Hardware session 1: the card move (needs the owner at the machines, after 06:00Z on 2026-11-07)
Ready once item 1 and the steps below are done. It needs over 2 h of soak before the 20:00Z power-off that day, so start early.
- **Changed 2026-11-07: cloud1 opts in to `memmap` LAST, not first.** A table line is a physical address, and the firmware moves all
  RAM above 4 GiB when its PCI hole changes size. cloud1's did on 2026-09-04: 1 GiB, same DIMMs, with the card count. It has
  `Above 4G Decoding` off today, and this session turns it on, so its map is likely to move. On a moved map every bad-page check
  passes while the bad frames are in service. cloud3's map has not moved in 74 boots, through the GPU3 pull.
- Agent, before: merge #59, then provision cloud1 and cloud3 so both publish the check. Expect `map_moved=0` on each.
- Agent, after the first boot with the card and BIOS: read `map_checked` and `map_moved`. If it moved, stop the guests, then a host
  `stressapptest` must show the fault at the translated addresses before the table changes (runbook section 15). That is up
  to 2 h, and it can share the soak window. Only on the final map: add cloud1 to `host/memmap-hosts`, provision, reboot.
- Owner: Gen4 x16 risers on `PCIEX16_1` and `PCIEX16_2` plus a spare; two separate 8-pin cables per card; monitor and keyboard.
- Move `GPU-ab0457e9` (cloud3 `23:00.0`) into cloud1 `PCIEX16_1`, after a fan-pulse check.
- Leave the DIMMs alone. A change voids the retired-page tables, and so may a BIOS or card change: see above.
- cloud3's shutdown can hang: check for a crash first, stop guests first, hold the power button if it is still on after 3 minutes.
- Merge #56 and apply on the day. CI on `cloud-ci-1/2` then runs about 15 % slower on heavy jobs, up to about 40 %.
- Then: soak, the on-demand image path, and move images to cloud1.

### 4. Claude speed: measure the API path
- A small, capped test of `pool-claude-api` against the seat path: wall time and quality on the same gate tasks. It costs real money.
- If it clears the 1.25x limit, decide which workloads move, starting with Codex on Fable on dev-worker-4.

### 5. Router Part 2: native pass-through to vLLM
- #151 and the availability probe are merged. Next: a one-route canary. Each roll needs the owner's go.
- Dependency: gate 1 needs cloudlab to publish deployment metadata for cloud2; a native route serves nothing without it.
- Before any canary: larger-body accounting, because this PR caps bodies at 2 MiB and a long Codex session would get a 413.
- During a canary: review the journaled unmeasured fields before widening traffic. Unknown fields stay forwarded (D2).

### 6. Find the `!` cause
- Blind runs cannot show a cause at 0.2 %, and 0 of 31 attempts reproduced it. Capture first: have the router keep a request's
  body when the guard trips. Then test prefix caching, speculative decoding and the fp16 state on a real trigger.
- **Owner decision before this is built:** a captured body holds user prompts and possibly secrets. Proposed: local qwen accounts
  only, never seat or pool accounts; encrypted at rest with a key the router alone holds; retained 7 days, then deleted; read only
  by the owner, for this diagnosis.

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
- Done 2026-11-07: the runbook's "halves" note is corrected, and the gate rows of the `key-edit` release are closed.
- Still to do: record the 2026-11-06 validation in the results document.
- Alert on `Freezing execution`; a short output check after each boot.
- A backup retention rule: the router's `/data` is 4.9 GB, and each backup takes about 250 MB.
- `codex-4` seat re-auth; cloud2 GPU2 temperature; the Phoenix serial; remove temporary worktrees.

### After that
Hardware session 2 (cloud2's image card to cloud1), then the availability probe, then cloud1 joins the route as the third backend.
