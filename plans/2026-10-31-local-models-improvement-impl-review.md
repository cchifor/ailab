# Implementation review — local-models-improvement — round 1

<!-- codex-impl-review-status: finalized -->

Plan: `plans/2026-10-31-local-models-improvement-plan.md` (finalized, owner decisions D1–D3).

Each change was reviewed codex-first (the `review` profile, gpt-6-astra) and then by the review bots:
cchifor/ailab #1530, #1531 and #1542; cchifor/llm-router #126; cchifor/cloudlab #46 and #47. This file records
the codex review of the whole implementation against the plan, run after everything had merged. It ran 214 commands
across the three repositories, the live configuration and the execution log.

## Findings and what was done

1. **fp8 and 122B fail as Assistant models.** fp8 reasons without calling a tool until the turn cap. The 122B gives
   no real answer, and its tool calls leak template text into their arguments.
   *Disposition:* escalated to the owner, as the plan requires. These are model behaviour on unchanged servers, so
   nothing in this change can fix them.
2. **Verification incomplete when it ran.**
   *Done since:* the clean final runs, the consumer checks (dsh, both gateways) and the non-streaming test.
   - **Recorded in the llm-router runbook:** the benchmark table and the Assistant results.
   - **Still open:** the next-day durability check (WS4.5).
3. **The harness lacks some metadata and a longer backend header timeout.** It does not record the router
   revision, build or flags, and in backend mode undici's 300 s header timeout cut a cold 122B.
   *Ruling:* deferred to a follow-up. The results record builds and revisions by hand.
4. **The router revisions were not in the runbook.**
   *Fixed:* 2093, 2128, 2136 and 2158 are now recorded there.
5. **The non-streaming test.** A non-streaming request to a cold fp8 through the public URL returned **200 after
   130.3 s**: Cloudflare did not cut it at the expected ~100–125 s.
   *Fixed:* the runbook statement is corrected, and streaming stays the advice for multi-minute loads.
6. **The benchmark key.**
   *Done:* `key_e0805717e635` was revoked; the token now gets 401, and its file is deleted.
7. **The live rollback smoke for b10857 was skipped.** The same binary and flags were exercised during
   qualification.
   *Disposition:* left to the owner, given cloud3's outage that day.
8. **Stale operational comments in `litellm.yaml`.**
   *Fixed:* fp8's driver and preload, and flash-next's build and figures. The w4a16 pool comment keeps its history
   under the dated REMOVED note added in #1530.
9. **Worktrees and artifacts.**
   *Done:* plan worktrees removed; the evidence is kept in the session scratchpad.
10. **WS2.1 diagnosis captured no `iostat` or cgroup `memory.stat`.**
    *Ruling:* waived. Major faults, bytes read and PSI established the mechanism, and b12020's prefetch removed it.

## Rulings made during execution

The full list is in the PR descriptions and below:
- **Manual ledger.** The plan is shaped as workstreams, so the plan-execution scripts did not apply.
- **No AI attribution.** The estate rule.
- **The P0 baseline is preliminary.** It ran with mixed harness versions; the flash-next A/B of record is the
  backend-mode qualification.
- **122B tool results.** Its calls stay a FAIL even where the harness reported pass, because the arguments contain
  template text.
- **`STEP_TIMEOUT_MS`** is kept as a deprecated export for vendoring hosts.
- **The account idle bound** is 5 s–10 min, the route bounds; the plan text said 1 s–30 min.
- **`122b` stays on its old build.** b12020 does not fix its tool calls.
- **MTP is deferred.**
- **The CUDA toolkit** is the 12.9.1 runfile under `/models-local`, plus the glibc 2.41 header patch. Debian's
  toolkit package would uninstall the runfile 580 driver.
- **No separate rollback-smoke redeploy** (item 7).
