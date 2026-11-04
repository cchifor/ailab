# Local-model agent benchmark, day 2 and the decision: flash-next vs qwen3.8-27b

Plan: [`2026-11-03-local-model-agent-benchmark-plan.md`](2026-11-03-local-model-agent-benchmark-plan.md). Day 1:
[`2026-11-03-local-model-agent-benchmark-results.md`](2026-11-03-local-model-agent-benchmark-results.md); its "Tomorrow"
section froze day 2's rules before any day-2 run. Day 2 ran on 2026-11-04 from 10:26Z to 16:38Z, on dev-worker-1,
with the same harness and settings as day 1: Codex CLI 0.166.0 through llm-router, the workers' `xhigh` setting,
both models paired and simultaneous, one run at a time per model.

## Decision (PROVISIONAL)

**This verdict is provisional.** It depends on the investigation into how flash-next's reasoning is replayed and
which effort it runs at (see "flash-next planning non-delivery" below). Under the predeclared rule, applied in
order, the result is **qwen3.8-27b**:

1. **Implementation is a practical tie.** Over all valid matched 35-min runs from both days (11 per model), the plan's
   success measure, finished and correct, is flash-next 7 and qwen3.8-27b 7. Correct by the cap,
   finished or not: flash-next 9, qwen3.8-27b 10. Neither measure differs by more than one run, so
   the rule moves on to planning.
2. **Planning decides for qwen3.8-27b.** The valid 90-min pairs are P1 and P2.
   - qwen delivered both: P1 in 42.8 min and P2 in 32.6 min. flash-next delivered neither.
   - Delivery-aware means: qwen3.8-27b 13.5/25 and flash-next 5.0/25. The gap is 8.5 points;
     the threshold is 1.5.
   - Neither P3 pair counts. P3-d2 overlapped the worker's memory exhaustion. P3-d2b was aborted on the owner's
     direction. qwen delivered in both (25.5 and 25.8 min); flash-next delivered in neither.
   - qwen's plans scored 16.0 and 11.0/25 (the uncounted P3 plans: 14.0 and 13.5), averaged over two blind judge passes each. That
     is "workable with gaps". flash-next lost by delivering nothing, not because qwen's plans were strong.
3. The rule does not reach robustness and speed. Both are reported below.

**Why this verdict is provisional.** The investigation's leading hypothesis is that our setup, not the model, stops
flash-next delivering:

- **The router drops each turn's replayed reasoning.** flash-next then re-derives its design every turn and never
  commits. The prompt sizes are consistent with this: a turn's 3k-6k output tokens, mostly reasoning, do not reappear
  in the next turn's prompt.
- **The router forces reasoning effort to `xhigh` for the local accounts.**

The investigation's working files are in `scratchpad\flashnext-plan-investigation\` under the e658dfd6a74824ef temp
root. A replay probe straight against llama-swap is testing the hypothesis now.

qwen3.8-27b shows the same pattern: "verify the citations, then write the plan", several times per run. It gets out of
the loop only because its decode is 2.5-2.8× faster (below).

**Recommendation:**

- Do not change cloud3's default on this result.
- Once the reasoning-replay and effort fix is live, rerun P1-P3 for **both** models on the fixed setup, at the 90-min
  cap, with the frozen judge, and apply the rule again.
- Also run the second implementation repeat of I4, which was not run.

If flash-next then delivers comparable plans, the decision falls to robustness and speed:

- **flash-next:** about half the completion tokens, and no long-context corruption up to 115k tokens. Against that, a
  runaway test it left running exhausted dev-worker-1's memory.
- **qwen3.8-27b:** 2.5-2.8× faster decode, and a median of 17.0 min to a finished, correct change against flash-next's
  26.5.

## flash-next planning non-delivery

flash-next wrote no plan in any of its planning runs: day 1's 35-min P2, day 2's 90-min P1, P2 and P3 (invalid),
and the P3 rerun (aborted at 45.7 min). qwen3.8-27b wrote one in every day-2 run, in 25-43 min. The owner suspects a cause on our side, not the model itself: sampling, the chat template, the llama.cpp build,
the sparse-attention indexer setting, the quant, or the router's or Codex's metadata. A separate read-only
investigation is checking. **Its leading hypothesis, now under test, is a configuration cause.** The router drops
each turn's replayed reasoning, so flash-next re-derives its design every turn and never commits to one. The router
also forces reasoning effort to `xhigh` for the local accounts. Its working files are in
`scratchpad\flashnext-plan-investigation\` (e658dfd6a74824ef temp root). A replay probe straight against llama-swap
is running. **Until that is settled, this result is provisional.** After a fix, P1-P3 are rerun for both models.

What the runs show (times in minutes from launch; Codex rollouts and the timing tap):

| Run | Wall | Requests | Commands | Max prompt | Output tokens (largest turn) | Decode, last 5 requests (engine) | Said it would write the plan at | Last message |
|---|---|---|---|---|---|---|---|---|
| P2-r1 (day 1, 35-min cap) | 35.0 min, cap | 35 | 60 | 81,249 | 77,194 (11,013) | 42.4, 40.5, 39.3, 39.2 | never | 32.5 min (16:29:54Z): "Let me pin the exact control flow around the reservation and the busy decision, since the plan must describe a wait loop inside it." |
| P1-d2 | 90.0 min, cap | 52 | 97 | 114,924 | 192,499 (15,403) | 35.6, 35.8, 35.0, 35.0 | 68.0, 85.7 | 85.7 min (11:52:32Z): "Checking assertions that a heartbeat could disturb, then writing the plan." |
| P2-d2 | 90.0 min, cap | 42 | 80 | 113,931 | 193,017 (12,577) | 34.4, 35.6, 34.4, 34.5 | never | 72.3 min (13:43:50Z): "Found a prior decision that rejected holding requests — I need to reconcile with it. Reading that design doc." |
| P3-d2 (invalid pair) | 90.7 min, cap | 51 | 91 | 98,284 | 161,940 (15,749) | 32.0, 30.3, 32.5, 30.5 | 40.4, 87.9 | 87.9 min (15:30:25Z): "Grounding done. Let me pin two citations I want exact, then write the plan." |
| P3-d2b (aborted) | 45.7 min, aborted | 44 | 79 | 100,385 | 95,062 (13,098) | 32.2, 35.7, 32.2, 37.7 | 38.9 | 43.0 min (16:35:38Z): "Let me verify the existing test suite passes as a baseline, and check the promtool gate locally." |

The pattern is the same in every run:

- **It explores the whole time.** Each run reads the repository and keeps verifying line numbers to cite. More
  than once it announces the write ("I have everything I need. Writing the plan to `plans/…`", P1 at 68.0 min; "Grounding
  done. Let me pin two citations I want exact, then write the plan.", P3 at 87.9 min). Then it goes back to checking
  instead of writing the file. In P1 it created `plans/` at 68 min and never wrote into it.
- **No errors.** Every model request completed (`response.completed`, HTTP 200). There were no tool-call errors,
  markup leaks, loops, garbled values, refusals or context errors, and Codex's stderr is clean. The runs ended at the
  cap (`timeout` exit 124, or 137 where the kill came after the grace) or, for P3-d2b, at the owner-directed abort.
- **Context and speed.** Prompts reached 98k-115k tokens. At that size, cloud3's engine decode was ~35 tok/s (about 30
  in the P3 run that overlapped the two-lane phase and the worker's memory exhaustion). flash-next's turns were 12k-16k
  tokens each, mostly reasoning. One turn therefore took 6-8 minutes, and a 90-min run fitted only 42-52 requests.
  qwen3.8-27b made 60-83 requests in 25-43 min.
- **Same thoroughness, much slower turns.** Both models spend most of their tokens thinking, and both re-verify
  citations. qwen ran the same "final checks" at ~2.5-2.8× flash-next's decode rate, so it finished.
- **Consistent with the hypothesis.** Each turn's prompt grows only by the tool output, not by the 3k-6k reasoning
  tokens the previous turn produced. So the reasoning behind the design is gone at the next turn, and the model
  re-derives it. A model that takes 6-8 minutes a turn cannot recover from that within the cap; qwen at 2-3 min a turn
  can.
- **Not yet known:** whether it is that, the forced effort, or the model itself. That is what the probe and the rerun
  after the fix will show.

## What ran on day 2

- **Before the first pair** (10:24Z-10:26Z): an overnight power outage had restarted the router pod (still config
  revision 2221, cloud2's queue intact) and cloud3. Preflight found:
  - no `cloud3hold`, and `qwen3.8-flash-next` ready on cloud3 (preloaded at boot);
  - cloud2 idle, and no benchmark request in flight.

  The timing taps restarted and passed an end-to-end streamed request through each model.
- **Order, frozen on day 1:** decision pairs first, alternating P1 (90 min), I5 (35), P2 (90), I3 (35), P3 (90),
  I1 (35), I6 (35), I4 (35). Then the diagnostic 90-min reruns, I1, I3, I2 and I5, last and cut first. A pair started
  only if a run at its cap would end by 19:45Z. The day ran in three phases:

- **Sequential, as frozen** (10:26Z-14:02Z): P1-d2, I5-r2, P2-d2.
- **Two lanes, an owner-approved deviation from the frozen schedule** (14:02Z-15:52Z). The owner asked: "speed up the
  evaluation. can you run pr in parallel on both?"
  - Lane A ran P3-d2. Lane B ran I3-r2, I1-r2, I6-r2 and I4-r2, so each model had at most two benchmark runs at once.
  - Every run of an overlapping pair is tagged *concurrent*. Its correctness and wall time count, because both models
    had the same concurrency. Its speed is left out of the speed tables.
  - At 15:28Z the lane drivers were swapped, without interrupting the running pairs, for a version that keeps run
    directories on dev-worker-1 (a separate investigation reads them). From then on only the commit's hidden test files
    are removed after grading.
- **Single lane again** (15:52Z-16:38Z). The two-lane phase ended when dev-worker-1 ran out of memory (below).
  - P3-d2b, the rerun of the invalid P3 pair, started at 15:52Z. qwen delivered its plan in 25.8 min.
  - At 16:38Z, on the owner's direction ("Don't waste time, validate the issues, implement the fix, then test
    again"), the benchmark was stopped. flash-next's P3-d2b run was terminated at 45.7 min, and I4-r2b was not
    started. Both count as **not run / aborted on owner direction: configuration fix in progress**.
  - The x90 diagnostic reruns were dropped. That was first by the frozen rule (they are cut first), then on the
    coordinator's instruction, to free flash-next for the investigation's probe.
- **Counted runs:** only matched runs count toward the decision: both models, same task, same cap. These are the
  35-min implementation runs from both days and the 90-min planning runs from day 2. Day 1's 35-min P2 attempt
  counts only as a delivery result (0/1 for each model).

## Results

### Implementation, decision runs (35-min cap, matched pairs)

| Task | Repeat | flash-next | qwen3.8-27b |
|---|---|---|---|
| I1 | r1 | TIMEOUT, correct at cap (13/13; finished cap; correct source by 26.2; 98,382 tok) | TIMEOUT, correct at cap (13/13; finished cap; correct source by 34.8; 235,093 tok) |
| I1 | r2 (concurrent) | PASS (13/13; finished 23.6 min; correct source by 21.5; 39,784 tok) | PASS (13/13; finished 17.0 min; correct source by 14.4; 107,668 tok) |
| I2 | r1 | TIMEOUT, correct at cap (33/33; finished cap; correct source by 33.7; 92,308 tok) | PASS (33/33; finished 18.4 min; correct source by 13.7; 103,866 tok) |
| I2 | r2 | PASS (33/33; finished 30.0 min; correct source by 20.8; 73,110 tok) | PASS (33/33; finished 18.1 min; correct source by 13.8; 93,220 tok) |
| I3 | r1 | TIMEOUT, not correct at cap (37/40; finished cap; correct source by –; 99,364 tok) | TIMEOUT, correct at cap (40/40; finished cap; correct source by 33.4; 213,490 tok) |
| I3 | r2 (concurrent) | TIMEOUT, not correct at cap (37/40; finished cap; correct source by –; 67,682 tok) | TIMEOUT, not correct at cap (31/40; finished cap; correct source by –; 165,818 tok) |
| I4 | r1 | PASS (29/29; finished 26.5 min; correct source by 25.5; 70,129 tok) | PASS (29/29; finished 14.8 min; correct source by 9.3; 92,538 tok) |
| I5 | r1 | PASS (51/51; finished 31.5 min; correct source by 26.9; 76,131 tok) | TIMEOUT, correct at cap (51/51; finished cap; correct source by 28.4; 224,801 tok) |
| I5 | r2 | PASS (51/51; finished 29.7 min; correct source by 26.6; 78,523 tok) | PASS (51/51; finished 22.8 min; correct source by 6.2; 130,794 tok) |
| I6 | r1 | PASS (20/20; finished 12.4 min; correct source by 9.2; 26,791 tok) | PASS (20/20; finished 12.0 min; correct source by 9.6; 74,249 tok) |
| I6 | r2 (concurrent) | PASS (20/20; finished 25.2 min; correct source by 21.7; 42,455 tok) | PASS (20/20; finished 5.9 min; correct source by 5.5; 40,168 tok) |

| Model | Runs | Finished and correct | Timeout, correct at cap | Timeout, not correct | Finished, wrong | Correct by the cap (any) | Output tokens |
|---|---|---|---|---|---|---|---|
| flash-next | 11 | 7 | 2 | 2 | 0 | 9 | 764,659 |
| qwen3.8-27b | 11 | 7 | 3 | 1 | 0 | 10 | 1,481,705 |

### Planning, decision runs (90-min cap)

| Task | Model | Delivered | Wall (min) | Correctness | Completeness | Feasibility | Verification | Grounding | Total /25 | Judge passes (max spread) |
|---|---|---|---|---|---|---|---|---|---|---|
| P1 | flash-next | no (TIMEOUT) | 90.0 | – | – | – | – | – | 5.0 (missing) | – |
| P1 | qwen3.8-27b | yes | 42.8 | 3.0 | 3.0 | 3.5 | 3.5 | 3.0 | 16.0 | 2 (1) |
| P2 | flash-next | no (TIMEOUT) | 90.0 | – | – | – | – | – | 5.0 (missing) | – |
| P2 | qwen3.8-27b | yes | 32.6 | 2.0 | 2.0 | 2.0 | 3.0 | 2.0 | 11.0 | 2 (0) |

| Model | Plans delivered (90 min) | Delivery-aware mean over tasks with any plan |
|---|---|---|
| flash-next | 0/2 | 5.00 over 2 tasks |
| qwen3.8-27b | 2/2 | 13.50 over 2 tasks |

### Invalid runs (kept, not counted)

| Pair | Reason | flash-next | qwen3.8-27b |
|---|---|---|---|
| P3-d2 | dev-worker-1 memory exhaustion 15:15-15:48Z | no plan; 90.7 min | plan delivered; 25.5 min; judged 14.0/25 |
| I4-r2 | dev-worker-1 memory exhaustion 15:15-15:48Z (stopped at ~19.5 min) | stopped at 19.0 min (28/29 at the stop) | stopped at 19.0 min (28/29 at the stop) |
| P3-d2b | aborted on owner direction at 16:38Z: configuration fix in progress | no plan; 45.7 min | plan delivered; 25.8 min; judged 13.5/25 |

### Speed per request (solo runs only: day 1 and day 2 before the two-lane phase; HTTP 200)

| Model | Time to first token, ms | Output rate, tok/s (client) | Decode, tok/s (engine) | Prefill proxy, tok/s (client) | Prefill, tok/s (engine) | Request time, ms |
|---|---|---|---|---|---|---|
| flash-next | 1,724 / 3,864 (n=516) | 48 / 58 (n=504) | 48 / 58 (n=504) | 783 / 938 (n=408) | 796 / 993 (n=504) | 19,408 / 152,145 (n=516) |
| qwen3.8-27b | 1,893 / 3,546 (n=678) | 116 / 144 (n=538) | – | 922 / 1,298 (n=675) | – | 6,728 / 95,746 (n=678) |

median / p90 (n); p90 omitted under 5 samples.

| Model | Prompt bucket | Requests | Time to first token, ms | Output rate, tok/s | Engine decode, tok/s |
|---|---|---|---|---|---|
| flash-next | < 10k | 6 | 12,463 / 14,802 (n=6) | 61 / 62 (n=5) | 61 / 61 (n=5) |
| flash-next | 10k-30k | 126 | 2,000 / 4,408 (n=126) | 56 / 60 (n=122) | 56 / 61 (n=122) |
| flash-next | 30k-100k | 356 | 1,555 / 3,471 (n=356) | 47 / 51 (n=356) | 47 / 51 (n=356) |
| flash-next | >= 100k | 21 | 1,973 / 4,145 (n=21) | 36 / 37 (n=21) | 36 / 37 (n=21) |
| flash-next | no usage (stream cut at the cap) | 7 | 1,044 / 2,938 (n=7) | – | – |
| qwen3.8-27b | < 10k | 9 | 6,311 / 6,438 (n=9) | – | – |
| qwen3.8-27b | 10k-30k | 123 | 1,851 / 3,633 (n=123) | 143 / 163 (n=64) | – |
| qwen3.8-27b | 30k-100k | 488 | 1,816 / 3,318 (n=488) | 116 / 137 (n=421) | – |
| qwen3.8-27b | >= 100k | 55 | 2,020 / 4,363 (n=55) | 100 / 113 (n=53) | – |
| qwen3.8-27b | no usage (stream cut at the cap) | 3 | 2,225 / – (n=3) | – | – |

An association, not a controlled test: each model chose its own prompts.

### Robustness flags (all scored runs, both days)

| Model | Runs | Markup | Loops (cmd / n-gram) | Unsupported tool calls | Rejected commands | Reconnects | Non-200 | Max prompt |
|---|---|---|---|---|---|---|---|---|
| flash-next | 17 | 0 | 0 / 0 | 0 | 5 | 0 | 0 | 114,924 |
| qwen3.8-27b | 17 | 0 | 0 / 0 | 2 | 6 | 0 | 0 | 133,518 |

## How each model spends its time

Over all valid implementation runs that finished correct (7 per model, both days):

- **Time to finish:** median 26.5 min for flash-next, 17.0 min for qwen3.8-27b. qwen was faster on I1-r2 (17.0 against
  23.6), I2 (18.4 and 18.1 against the cap and 30.0), I4-r1 (14.8 against 26.5), I5-r2 (22.8 against 29.7) and I6-r2
  (5.9 against 25.2). flash-next was faster only on I5-r1 (31.5 min; qwen hit the cap). I6-r1 was a tie.
- **Correct source by** (an upper bound on the time to a correct implementation: the agent's last edit to a non-test
  source file, from the rollout): median 21.7 min for flash-next, 9.6 min for qwen.
- **Tokens:** over all 17 scored runs per model, qwen generated 2.31M completion tokens and flash-next 1.49M. qwen
  also made more requests: 877 against 661.
- **Where the time goes:** model requests occupied a median 98 % (flash-next) and 98 % (qwen) of each run's wall time.
  Tool execution between requests took at most ~3 min per run. The leading-reasoning share of generation time had a
  median of 0.89 and 0.88.
- **Speed by context:** these medians are from solo runs only.
  - flash-next's engine decode: 61, 56, 47 and 36 tok/s in the <10k, 10-30k, 30-100k and ≥100k buckets.
  - qwen's client-observed output rate: 143, 116 and 100 tok/s at 10-30k, 30-100k and ≥100k.
  - The ratio is about 2.5× at 10-30k and 30-100k, and 2.8× at ≥100k.
  - Most of the ≥100k bucket comes from day 2's long planning prompts: 21 flash-next requests and 55 qwen (day 1 had
    4, all qwen).

  This is an association, not a controlled test: each model chose its own prompts.
- **Concurrency:** every lane-B pair ran during the two-lane phase. Their correctness and wall time are flagged
  *concurrent* in the tables but count, because both models had the same concurrency. Their speed is excluded.

## Robustness and failure modes

| Failure mode, both days | flash-next | qwen3.8-27b |
|---|---|---|
| Tool-call markup, loops, garbled values in diffs | 0 | 0 |
| Calls to a tool not offered (`apply_patch`) | 0 | 2 (day 1, I5) |
| Commands rejected by Codex's safety policy | 5 | 6 |
| Reconnects, 429s, HTTP errors, refusals, context errors | 0 | 0 |
| Planning runs without a plan within the cap | 5 of 5 (incl. the invalid and aborted P3s, and day 1's 35-min P2) | 1 of 5 (day 1's 35-min P2) |
| Implementation runs never edited the source by the cap | 2 (I3, both days) | 0 |
| Implementation runs left with a broken source at the cap | 0 | 1 (I3-r2: undefined names, typecheck fails) |
| Runaway process left behind | 1 (I6-r2: an orphaned test grew to 14.9 GB, twice OOM-killed) | 0 |

Notable failures:

- **flash-next, every planning run:** explores until the cap; see the next section.
- **flash-next, I6-r2:** a test command that never returned was left running, and its `pkill` clean-up killed itself.
  The orphan exhausted dev-worker-1's memory (see "Invalid or uncounted runs"). Its final patch still passed.
- **flash-next, I3 (both days):** it spent the whole 35 min on probes of zlib and PNG behaviour and never edited
  `images.ts`.
- **qwen3.8-27b, I3-r2:** hit the cap mid-edit. Its source referenced names it had not defined yet (`offset0`,
  `bytes0`, `rowBytesOf`).

## Invalid or uncounted runs

**dev-worker-1 ran out of memory, 15:15Z-15:48Z.** The kernel OOM killer fired twice:

| Time | Process killed | RSS |
|---|---|---|
| 15:15:25Z | python3 | 14.9 GB |
| 15:48:32Z | python3 | 14.8 GB |

Between ~15:35Z and 15:48:44Z SSH timed out in the banner exchange. Proxmox showed the VM at 16.6 of 17.2 GB memory,
reading swap heavily.

Both processes came from flash-next's I6-r2 run:

- Its intermediate `runs_since` code loops forever under the tests' mocks. One test command (15:22:23Z) never
  returned, and Codex left it running in the background.
- Its clean-up, `pkill -f "unittest discover -s scripts/tests"` (15:23:42Z), matched its own shell and killed only
  itself (exit 143).
- The orphaned test kept growing after the run ended.

flash-next's final I6 patch still passed. qwen3.8-27b ran its tests under `timeout 60` and never hit this.

Consequences, under the predeclared "independently evidenced outage" rule:

- **P3-d2** overlapped the exhaustion and is invalid. Both observations are kept: qwen3.8-27b delivered in 25.5 min
  (14:28Z, before the exhaustion), judged 14.0/25; flash-next hit the cap with no plan. The rerun, P3-d2b, was then aborted on the owner's direction
  (qwen had delivered in 25.8 min, judged 13.5/25; flash-next had no plan at 45.7 min).
- **I4-r2** overlapped it and is invalid. It was stopped at ~19 min, when both runs were at 28/29. Its rerun, I4-r2b,
  was not run (owner-directed stop).
- **I6-r2** is valid. The outage came from that run itself, so it counts as a robustness and hygiene finding against
  flash-next, not as an external outage. Both models passed.
- **What the two-lane phase contributed:** two lanes doubled the load on the 16 GB worker, which made the runaway
  test's effect worse.

**Other checks:**

- Router: the pod was unchanged through day 2 (restart count 1, from the overnight outage) and the config revision was
  2221 throughout.
- cloud3: no `cloud3hold`, and flash-next was `ready` before and after every pair. Both slots ran at once in the
  two-lane phase, and llama.cpp logged no KV-cache or context errors.
- cloud2: no busy 429s and no admission-queue waits.
- The router's admin API rate-limited the journal collector once (`RATE_LIMITED`). The collector was made to back off
  and re-run, with no errors the second time. The analysis rests on the timing tap's per-request records, which are complete.

## Caveats

These carry over from day 1:

- Speed is as deployed. flash-next runs on 3 RTX 3090s with llama.cpp and a Q3 MoE; qwen3.8-27b runs on 2 RTX 3090s
  with vLLM, a W4A16 dense model and MTP. cloud2's output rates are client-observed. cloud3's decode is engine-measured
  and agrees with the client side in aggregate.
- The suite is targeted: five llm-router tasks and one ailab script for implementation, and three planning problems
  from this estate.
- Each cell still has only one or two runs, and requests within a run are correlated.
- The 27B measured is cloud2's `Qwen3.8-27B-W4A16-AutoRound-fast`. A 27B default on cloud3 would run that same
  checkpoint (cloudlab PR #54), after qualification on cloud3.

## Method changes from day 1

- `run.sh` takes the cap from `CAP` (minutes, default 35) and records it as `cap_min` in `times.json`. `drive3.sh` reads
  a per-pair cap from the order file (`TASK TAG CAP_MIN`) and applies the deadline rule to that cap.
- The day-2 router journal is collected into its own file (`journal-d2.jsonl`), because the router pod restarted
  overnight.
- Nothing else changed: the same tasks, templates (hashes as on day 1), grading, timing tap, judge prompt and
  checklists.

## Evidence

`model-bench-2026-11-04.tar.gz`, the same layout as day 1's bundle plus the day-2 drivers (`drive3.sh` to
`drive5.sh`), `tables2.py`, `nondelivery.py`, `journal-d2.jsonl` and the judge outputs (`judge/*.scores.json` with their
letter mappings). sha256 `73d4f3a6c7d8709216812f29f345d4b740337ccbcfa4d8fcd64bbce9b0cad465`. It is in dev-worker-1's `/workspace/archive/` (mode 600) and in the session
scratchpad on the workstation. Every file was scanned for credential patterns before packing; none were found.

Day 2's run directories from I6-r2 on (I6-r2, I4-r2, P3-d2, P3-d2b) are also still in `/workspace/c4/bench/runs/` on
dev-worker-1, with their Codex sessions, for the investigation. Only the commit's hidden test files were removed after
grading. `/workspace/c4/bench` is otherwise kept until the reruns. The timing taps are stopped.
