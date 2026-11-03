# Local-model agent benchmark, day 1 results: flash-next vs qwen3.8-27b

Plan: [`2026-11-03-local-model-agent-benchmark-plan.md`](2026-11-03-local-model-agent-benchmark-plan.md) (codex
plan-review, 2 rounds, then an amendment after the dry run). Run on 2026-11-03, 14:48Z-19:38Z, on dev-worker-1,
with Codex CLI 0.166.0 through llm-router. Every number below comes from the evidence bundle described at the end.

## Recommendation

**Today's result is inconclusive under the predeclared rule.** Until day 2, keep flash-next as cloud3's resident
default. That is a deployment choice (keep two model families), not a benchmark verdict.

The rule's first two criteria do not separate the models:

1. **Implementation is a practical tie.** Finished and correct within 35 min, counted per run: flash-next 4/7, qwen3.8-27b 4/7. Correct by the cap, finished or not: flash-next 6/7, qwen3.8-27b 7/7. Wrong at the cap: flash-next 1, qwen3.8-27b 0. No run finished with a wrong patch. The gaps are at most one run, inside the predeclared tie margin.
2. **Planning has no data.** Each model attempted one planning task (P2) and delivered no plan: both hit the 35-min
   cap still reading code. P1 and P3 did not run.

Planning is half of the owner's question, so the rule ends at "inconclusive" for now.

**What today shows, within one run per task (two for I2):**

- **qwen3.8-27b leads slightly on implementation, inside the tie margin.**
  - It was correct by the cap on every run.
  - It reached a finished, correct change faster on I2, twice: 18.4 and 18.1 min, against flash-next's capped run (a
    lower bound) and 30.0.
  - It was faster on I4: 14.8 min against 26.5.
  - flash-next was faster on I5: 31.5 min, against qwen's capped run.
  - I6 was a tie: 12.4 against 12.0.
  - flash-next's one miss was I3. It was still experimenting when the cap hit.
- **flash-next showed no long-context corruption in this sample.**
  - 217 of its requests had prompts of 30k-81k tokens; none reached 100k.
  - None of its runs showed tool-call markup leaks, `show show show` loops or garbled values in its diffs (checked by
    script and by hand). That failure was why it was distrusted. Today's sample did not reproduce it, but 217 correlated
    requests up to 81k is evidence, not proof.
  - qwen made 2 calls to a tool it was not offered (`apply_patch`, "unsupported call"). They did no harm.
- **qwen generates about twice the completion tokens, at about 2.4× the client-observed output rate.**
  - The median output rate was 120 tok/s for qwen and 49 for flash-next (client-observed; flash-next's engine-measured
    decode agrees: median 49).
  - qwen generated 1,219,331 completion tokens, flash-next 613,409. Reasoning is included and was most of it: vLLM
    reports qwen's reasoning tokens directly, llama.cpp reports none for flash-next.
  - Both models used most of the 35-min budget. Half their runs hit the cap, so those durations are lower bounds, not
    completion times. Model requests took 78-99 % of each run's wall time (median 96 %).
- **Redundancy against diversity.** cloud2 already serves qwen3.8-27b, so making 27B cloud3's default would leave the
  estate with two copies of one model and no second family. This concerns how capacity is spread, not checkpoint
  quality: cloudlab PR #54 (prepared, not applied) would install cloud2's exact checkpoint on cloud3.

**How day 2 decides, in the plan's order.** Day 2 adds the second implementation repeats and the planning runs. Only
matched runs count toward the decision: both models, same task, same cap.

1. **Implementation success** over days 1 and 2. A lead of more than one run decides.
2. **Plans**, only if (1) ties. A gap over 1.5 points (of 25) in the mean plan score decides. The mean is taken over
   every planning task that at least one model delivered on. A model that delivers no plan on such a task scores 5/25
   there: 1 on every criterion, the rubric's "wrong or missing". A task that neither model delivered on is left out.
3. **Robustness, then speed**, only if (1) and (2) tie. A long-context corruption weighs heavily.

If quality and robustness point opposite ways, or fewer than 2 of the 3 planning tasks have a plan from either model, the answer stays inconclusive.

If 27B does become the default, cloud3 would run cloud2's exact checkpoint, `Qwen3.8-27B-W4A16-AutoRound-fast`, the one
measured here: copied with a sha256 manifest, TP=2 on GPU0+GPU1, with cloud2's flags (cloudlab PR #54). It would still
need qualifying on cloud3 first, with #54's script and runbook §13i.

## What ran

- **Paired and simultaneous.** Each task ran on both models at the same moment, each in its own fresh workspace:
  flash-next on cloud3 and qwen3.8-27b on cloud2. A pair ended when both runs had exited. Each model had one run at a
  time.
- **One repeat per task today, two for I2.** The dry run showed that the plan's 18 pairs would not fit before the cloud
  cluster's 22:00 local auto-off. The coordinator, relaying the owner, approved one repeat per task in this priority
  order: I5, P2, I2, I3, P1, I6, I4, P3, I1. After P2, the coordinator approved moving I1 ahead of P1, P1 and P3 to
  tomorrow. Today's scored pairs, in order: I5, P2, I2, I3, I1, I6, I4, then I2 repeat 2. That was 8 pairs, 16 runs. I2 repeat
  2 started 19:05:52Z and was the only second repeat that fitted; the driver declined to start I5 repeat 2 at 19:37:58Z
  (deadline). P1 and P3 did not run; they move to tomorrow.
- **Cap 35 min per run.** The setting is the dev workers' real one: `model_reasoning_effort = "xhigh"` from
  `config.toml`. The router drops it on both local accounts, so each engine thinks at its default. A run that reached
  the cap is a **timeout**, kept separate from a wrong answer. It is still graded on the work it had done at the cap.
- **Graded in a fresh workspace.** The base tree gets fresh dependencies and only the agent's non-test changes. Then
  the commit's hidden test files are added. Pass = the patch applies, the graded files pass with the reference's exact
  test count, and the typecheck is clean.

## Results

### Per run

| Task | Model | Result | Tests | Typecheck | Wall (min) | Requests | Max prompt | Output tokens | Think share | Flags |
|---|---|---|---|---|---|---|---|---|---|---|
| I1-r1 | flash-next | TIMEOUT, correct at cap | 13/13 | True | 35.0 | 40 | 45,006 | 98,382 | 0.892 | rejected=1, incomplete=1 |
| I1-r1 | qwen3.8-27b | TIMEOUT, correct at cap | 13/13 | True | 35.0 | 44 | 54,522 | 235,093 | 0.942 | rejected=1 |
| I2-r1 | flash-next | TIMEOUT, correct at cap | 33/33 | True | 35.0 | 37 | 58,080 | 92,308 | 0.979 | incomplete=1 |
| I2-r2 | flash-next | PASS | 33/33 | True | 30.0 | 57 | 80,860 | 73,110 | 0.811 | – |
| I2-r1 | qwen3.8-27b | PASS | 33/33 | True | 18.4 | 69 | 77,610 | 103,866 | 0.877 | rejected=1 |
| I2-r2 | qwen3.8-27b | PASS | 33/33 | True | 18.1 | 87 | 79,100 | 93,220 | 0.842 | rejected=1 |
| I3-r1 | flash-next | TIMEOUT, not correct at cap | 37/40 | True | 35.0 | 22 | 41,768 | 99,364 | 0.95 | incomplete=1, outside_cmds=2 |
| I3-r1 | qwen3.8-27b | TIMEOUT, correct at cap | 40/40 | True | 35.0 | 71 | 103,676 | 213,490 | 0.778 | incomplete=1 |
| I4-r1 | flash-next | PASS | 29/29 | True | 26.5 | 42 | 53,575 | 70,129 | 0.877 | rejected=1 |
| I4-r1 | qwen3.8-27b | PASS | 29/29 | True | 14.8 | 34 | 48,741 | 92,538 | 0.923 | rejected=1 |
| I5-r1 | flash-next | PASS | 51/51 | True | 31.5 | 50 | 81,219 | 76,131 | 0.812 | – |
| I5-r1 | qwen3.8-27b | TIMEOUT, correct at cap | 51/51 | True | 35.0 | 45 | 70,700 | 224,801 | 0.938 | unsupported_tool=2, incomplete=1 |
| I6-r1 | flash-next | PASS | 20/20 | – | 12.4 | 36 | 45,997 | 26,791 | 0.471 | rejected=1 |
| I6-r1 | qwen3.8-27b | PASS | 20/20 | – | 12.0 | 42 | 60,476 | 74,249 | 0.767 | rejected=1 |
| P2-r1 | flash-next | NO PLAN (TIMEOUT) | – | – | 35.0 | 35 | 81,249 | 77,194 | 0.953 | incomplete=1 |
| P2-r1 | qwen3.8-27b | NO PLAN (TIMEOUT) | – | – | 35.0 | 42 | 89,798 | 182,074 | 0.978 | incomplete=1 |

### Per model (scored runs)

The outcome columns count the 7 implementation runs per model. Requests and output tokens also include P2.

| Model | Finished and correct | Timeout, correct at cap | Timeout, not correct | Finished, wrong | Correct by the cap (any) | Mean plan score | Total requests | Total output tokens |
|---|---|---|---|---|---|---|---|---|
| flash-next | 4/7 | 2 | 1 | 0 | 6/7 | – (0 plans) | 319 | 613,409 |
| qwen3.8-27b | 4/7 | 3 | 0 | 0 | 7/7 | – (0 plans) | 434 | 1,219,331 |

### Speed per request (scored runs, HTTP 200 only)

| Model | Time to first token, ms | Output rate, tok/s (client) | Decode, tok/s (engine) | Prefill proxy, tok/s (client) | Prefill, tok/s (engine) | Request time, ms | Leading think, ms |
|---|---|---|---|---|---|---|---|
| flash-next | 1,491 / 3,473 (n=319) | 49 / 59 (n=312) | 49 / 59 (n=310) | 783 / 918 (n=241) | 787 / 951 (n=310) | 19,544 / 113,667 (n=319) | 8,136 / 105,714 (n=316) |
| qwen3.8-27b | 1,584 / 2,995 (n=434) | 120 / 144 (n=338) | – | 959 / 1,293 (n=431) | – | 7,042 / 77,329 (n=434) | 1,985 / 73,424 (n=431) |

median / p90 (n). Output rate: completion tokens (reasoning included) / (terminal event - first delta), requests with >= 50 tokens and >= 1 s. Prefill proxy: uncached prompt tokens / time to first token, requests with >= 500 uncached tokens.

### Speed by prompt size

This shows an association, not a controlled test: each model chose its own prompts along its own task trajectory, and requests within a run are correlated. p90 is omitted under 5 samples.

| Model | Prompt bucket | Requests | Time to first token, ms | Output rate, tok/s | Prefill proxy, tok/s | Engine decode, tok/s |
|---|---|---|---|---|---|---|
| flash-next | < 10k | 4 | 14,040 / – (n=4) | 61 / – (n=4) | 674 / – (n=4) | 61 / – (n=3) |
| flash-next | 10k-30k | 94 | 1,894 / 3,716 (n=94) | 55 / 60 (n=91) | 801 / 926 (n=80) | 55 / 61 (n=90) |
| flash-next | 30k-100k | 217 | 1,369 / 3,039 (n=217) | 47 / 51 (n=217) | 766 / 903 (n=157) | 47 / 51 (n=217) |
| flash-next | >= 100k | 0 | – | – | – | – |
| flash-next | no usage (stream cut at the cap) | 4 | 950 / – (n=4) | – | – | – |
| qwen3.8-27b | < 10k | 7 | 6,311 / 6,407 (n=7) | – | 1,485 / 1,489 (n=7) | – |
| qwen3.8-27b | 10k-30k | 94 | 1,688 / 2,995 (n=94) | 138 / 163 (n=53) | 1,256 / 1,399 (n=94) | – |
| qwen3.8-27b | 30k-100k | 326 | 1,542 / 2,816 (n=326) | 119 / 137 (n=281) | 907 / 1,088 (n=326) | – |
| qwen3.8-27b | >= 100k | 4 | 2,636 / – (n=4) | 105 / – (n=4) | 789 / – (n=4) | – |
| qwen3.8-27b | no usage (stream cut at the cap) | 3 | 2,225 / – (n=3) | – | – | – |

### Per task, side by side

| Task | flash-next: result; wall min; last source edit min; requests; output tokens | qwen3.8-27b: same |
|---|---|---|
| I1 | TIMEOUT, correct at cap; 35.0; 26.2; 40; 98,382 | TIMEOUT, correct at cap; 35.0; 34.8; 44; 235,093 |
| I2 | TIMEOUT, correct at cap; 35.0; 33.7; 37; 92,308 / PASS; 30.0; 20.8; 57; 73,110 | PASS; 18.4; 13.7; 69; 103,866 / PASS; 18.1; 13.8; 87; 93,220 |
| I3 | TIMEOUT, not correct at cap; 35.0; –; 22; 99,364 | TIMEOUT, correct at cap; 35.0; 33.4; 71; 213,490 |
| I4 | PASS; 26.5; 25.5; 42; 70,129 | PASS; 14.8; 9.3; 34; 92,538 |
| I5 | PASS; 31.5; 26.9; 50; 76,131 | TIMEOUT, correct at cap; 35.0; 28.4; 45; 224,801 |
| I6 | PASS; 12.4; 9.2; 36; 26,791 | PASS; 12.0; 9.6; 42; 74,249 |
| P2 | no plan, timeout; 35.0; –; 35; 77,194 | no plan, timeout; 35.0; –; 42; 182,074 |

## How each model spends its time

Three things are kept apart here: time to a correct implementation, time to finish, and tokens spent.

| Task | flash-next: correct source by / finished at | qwen3.8-27b: correct source by / finished at | Output tokens, flash-next / qwen |
|---|---|---|---|
| I1 | 26.2 / cap | 34.8 / cap | 98,382 / 235,093 |
| I2 | 33.7 / cap | 13.7 / 18.4 | 92,308 / 103,866 |
| I2 (repeat 2) | 20.8 / 30.0 | 13.8 / 18.1 | 73,110 / 93,220 |
| I3 | not correct / cap | 33.4 / cap | 99,364 / 213,490 |
| I4 | 25.5 / 26.5 | 9.3 / 14.8 | 70,129 / 92,538 |
| I5 | 26.9 / 31.5 | 28.4 / cap | 76,131 / 224,801 |
| I6 | 9.2 / 12.4 | 9.6 / 12.0 | 26,791 / 74,249 |

The columns:

- **Correct source by** is when the agent last edited a non-test source file, in minutes from launch. It is given
  only for runs whose final patch passes the graded tests. From that edit on, the source was in its final, correct
  state, so this is an upper bound on the time to a correct implementation. The source may have been correct earlier.
  Measuring that would mean grading intermediate states, which was not done. The time is read from the timestamped
  Codex rollout: the last shell command that writes one of the changed source files.
- **Finished at** is when Codex exited. "cap" means the run was stopped at 35 min, so its time to finish is only a
  lower bound.
- Between the last source edit and the exit, the agents ran tests, wrote new tests, reviewed their diffs and cleaned
  up.

- **Model requests take nearly all the wall time.** In-flight model requests covered 78-99 % of each run's wall time
  (median 96 %). Tool execution between requests (tests in docker, file reads) took 5-179 s per run.
- **Leading reasoning dominates generation time.** Think share is the time from each request's first reasoning delta
  to its first answer delta, summed over the run, divided by the summed time from first delta to the end of the
  stream. It was 0.78-0.98 on the llm-router tasks. On I6 it was 0.47 for flash-next and 0.77 for qwen.
- **qwen3.8-27b generates about twice the completion tokens, at about 2.4× the client-observed rate.**
  - Over the scored runs qwen generated 1,219,331 completion tokens, flash-next 613,409.
  - qwen's single turns reached 25.8k tokens. On I1 it had four turns of 18.6k-24.2k tokens, each 169-222 s.
    flash-next's largest turn was 14.0k tokens.
  - qwen made more requests in the same time: 434 against 319.
- **Neither model starts editing first consistently.**
  - qwen edited first on I2 (8.2 and 7.9 min, against 27.4 and 17.1), on I3 (16.0; flash-next never edited the source)
    and on I4 (9.3 against 15.1).
  - flash-next edited first on I1 (22.0 against 32.3), on I5 (7.6 against 14.4) and on I6 (2.8 against 4.5).
  - On I1 qwen named the bug in its first message. It then generated 218k output tokens before its first source edit,
    at 32.3 min.
- **Both models check their own work.** Both wrote new tests and re-ran the suites. On I1, flash-next restored the base
  file to show that its new test catches the bug. On I3, qwen built and benchmarked 12 prototype scripts. Of today's
  14 implementation runs, 5 hit the cap with a source that was already correct. In those runs, the
  time after the last source edit went on activities like these. That is an observation, not a measured cause.
- **Larger prompt buckets go with lower output rates for both models.** This is an association, not a controlled
  measurement: each model chose its own prompts.
  - flash-next's engine decode on cloud3 was ~61 tok/s under 10k prompt tokens (4 requests), ~55 at 10k-30k and ~47 at
    30k-100k.
  - qwen's client-observed rate was ~138 at 10k-30k, ~119 at 30k-100k and ~105 at or above 100k (4 requests).
  - Most of each prompt was prefix-cached on both engines (median ~97 %), so prefill was a small part of each request.

## Robustness and failure modes

Flagged automatically from the Codex JSONL, the rollouts, stderr and the journal, then checked by hand. The same
rules apply to both models.

| Failure mode | flash-next | qwen3.8-27b |
|---|---|---|
| Tool-call markup in commands or messages (`<tool_call>`, `<parameter`, `</function>`) | 0 | 0 |
| Loops (same command 3× in a row, repeated n-grams) | 0 | 0 |
| Garbled copied values (hand review of the diffs and messages) | none found | none found |
| Calls to a tool not offered ("unsupported call: apply_patch") | 0 | 2 (I5) |
| Commands rejected by Codex's safety policy (`rm -f …`) | 3 | 5 |
| Early stop (final answer without the work) | 0 | 0 |
| Reconnects, 429s, HTTP errors, refusals | 0 | 0 |
| Run hit the 35-min cap | 4 of 8 | 4 of 8 |
| Scratch files left in the work tree | I3: 3 (`.scratch/`) | I3: 12 (`.tmp/`) |

Notable failures:

- **flash-next, I3: not correct at the cap.** It never changed `images.ts`. The whole 35 min went on probes: "Running
  a focused probe of the zlib callback semantics (exact size, truncation, bound, RSS)", then "Let me get a real PNG
  decoder to validate my Adam7 math — trying pillow or a known interlaced fixture". That included `curl` downloads of
  PNG fixtures from w3.org and GitHub. Those were test data, not the answer: the leakage audit (below) judged them
  benign. It made 22 requests, averaging 4.5k output tokens each.
- **qwen3.8-27b, I1: correct at the cap, but its first source edit came at 32.3 min.** It named the bug in its first
  message ("The bug is in `typedCodes` in tools.ts"), then spent 218k output tokens, including four turns of 18.6k-24.2k tokens, before
  editing.
- **Both models on P2: capped while still reading code.** flash-next at the cap: "Let me pin the exact control flow
  around the reservation and the busy decision, since the plan must describe a wait loop inside it." qwen: "I've now
  traced the full mechanism through the code. Let me capture the exact line numbers for the remaining key sites, then
  write the plan." They read 60 and 67 commands' worth of code, with prompts up to 81k and 90k tokens. Neither wrote a
  line of the plan. This is a timeout for both, not a quality result.

**Leakage audit.** Every executed command was scanned for:

- the forge (`git clone|fetch|pull`, `git.chifor.me`);
- internet access (`curl`, `wget`);
- other runs' directories;
- credentials helpers (`codex-router-key`, `cred`, `bao`).

The only hits were flash-next's I3 fixture downloads above. No run read another run's work or any answer.

## Invalid or uncounted runs

- **The dry run (I1, unscored by design).** The router's admission-queue roll at 14:55:10Z (`Recreate`) cut both
  streams mid-response. Codex reconnected (1/8) and carried on. flash-next finished in 21.8 min. qwen3.8-27b was
  stopped by hand at 29.6 min to start the scored set. Both patches pass the graded tests. These runs are not in the
  tables.
- **No scored run was invalid.** For every scored pair:
  - the router pod was unchanged (`llm-router-687c467686-nvtz5`, started 14:55:11Z);
  - there was no `cloud3hold`, and flash-next was resident and `ready` before and after the pair;
  - there were no busy 429s, no HTTP errors and no admission-queue waits (no `admission.queued` events in the journal);
  - in cloud2's vLLM gauges, sampled every 30 s during each pair, at most one request was running (the benchmark's
    own) and KV use stayed ≤ 0.28. Activity between samples was not measured.

  Every `cancelled` trajectory in the journal ends at the moment a run hit its cap (or the dry-run cut/kill). The
  router config revision was 2221 for all scored requests.
- **Timeouts are not invalid.** A run that hit the 35-min cap is counted as a timeout, separately from a wrong answer,
  and its work at the cap is graded. P2 produced no plans, so it is a timeout for both models and is not in the plan
  comparison.

## Caveats

- **Speed is as deployed, not like for like.** flash-next runs on 3 RTX 3090s on cloud3 (llama.cpp b12020,
  UD-Q3_K_XL MoE, 6B active). qwen3.8-27b runs on 2 RTX 3090s on cloud2 (vLLM, W4A16 AutoRound, dense, MTP). The two
  differ in engines, quantisations and card counts. cloud2 is shared production: it was available to other agents
  throughout, though its 30-s samples showed no other running request during the pairs.
- **The 27B measured is cloud2's W4A16 build, `Qwen3.8-27B-W4A16-AutoRound-fast`.** If 27B became cloud3's resident
  model, cloud3 would run that same checkpoint (cloudlab PR #54, prepared, not applied): copied with a sha256 manifest,
  TP=2 on GPU0+GPU1, cloud2's flags. Quality should carry over. Speed on cloud3's cards needs #54's qualification run
  (runbook §13i). cloud3's existing `qwen3.8-27b-fp8` entry (the uncensored FP8 variant) is a different model and not a
  candidate default.
- **Small sample.** Today has one run per task per model (two for I2), on a targeted suite: five llm-router tasks and
  one ailab script. That is 7 implementation runs and 1 planning attempt per model. One run per cell cannot separate a
  real difference from run-to-run variance. Tomorrow's repeats narrow that, without closing it.
- **Client-observed speed for cloud2.** cloud3's decode and prefill figures are llama.cpp's own per-request timings,
  matched to the requests. vLLM reports no per-request timing, so cloud2's figures are client-observed from the
  timing tap. On cloud3, the client-observed output rate and the engine decode agree in aggregate: median 49 for
  both, p90 59 for both. This validates the client-side output-rate method in aggregate. It does not validate prefill:
  the prefill proxy and llama.cpp's prompt rate differ by 4 tok/s at the median and 33 at the p90, so the prefill proxy
  is only indicative.
- **Codex has no catalog metadata for either model.** Both runs log "Model metadata for `<model>` not found.
  Defaulting to fallback metadata". The effect is the same for both models.

## Tomorrow (2026-11-04)

Proposed for 2026-11-04 (cloud cluster on from 08:00 local), same harness, same settings:

These rules are frozen now, before any day-2 run.

1. **Planning, 90-min cap** (coordinator, 2026-11-03).
   - Runs: P1, P2 and P3, one pair each, about 4.5 h with both models in parallel.
   - Reporting: delivery (a plan written within the cap) is reported per model and task, separately from judged
     quality. Today's P2 counts as 0/1 delivered for each model.
   - Scoring (delivery-aware): each model's plan mean is taken over every task that at least one model delivered on.
     A missing plan scores 5/25 on such a task (1 on every criterion). A task neither model delivered on is left
     out. If fewer than 2 of the 3 tasks have a plan from either model, the planning comparison is inconclusive. So a
     model cannot gain by failing to deliver.
   - Judge: two blind passes per task, letters swapped, `codex exec --profile review`, with the frozen checklists. The
     two passes check judge noise; they do not replicate the planning run.
2. **Second implementation repeats**, cap 35 min, matched (both models, same task, same cap), in this order: I5, I3,
   I1, I6, I4. I2 already has two. They count toward the decision, together with today's runs.
3. **90-min capped reruns (diagnostic only, not in the decision).** Rerun both models on I1, I2, I3 and I5, the tasks
   where at least one model hit today's 35-min cap. They measure time to finish under a common, longer budget; a run
   that still hits 90 min stays censored.
4. **Not planned:** selective third repeats of divergent tasks. If the result stays close, the next round is new,
   matched tasks outside llm-router (a platform Python change, an ailab Ansible change) for both models, decided
   before they run.

The harness needs no change: `drive2.sh` with a new order file (`CAP_MIN=90` and the run cap in `run.sh` for the
planning pairs). `/workspace/c4/bench` on dev-worker-1 is kept overnight: task tarballs, templates, image and taps,
about 100 MB, with no answer material in it. It is deleted after day 2.

## Method details

- **Harness**: per run, a private `CODEX_HOME` copied from a frozen template: `config.toml`, the model's profile, and
  `AGENTS.md` without the managed "local models" block, which ranks the two models. For P3 the template also drops
  the disk-hygiene block, which describes P3's answer. Template sha256: `AGENTS.md` 2210adcef242…, `config.toml`
  d80db2948d42…, `flash-next.config.toml` f7abb14f9d0f…, `qwen-27b.config.toml` 508a303e36b1…; P3's `AGENTS.md`
  ce204b4fdc56….
- **Command** (`/workspace/c4/bench/bin/run.sh`, bash login shell on dev-worker-1, two at once per pair):

  ```sh
  CODEX_HOME="$RUN/codex-home" timeout --kill-after=30s 35m codex exec -p "$PROFILE" --skip-git-repo-check \
    -C "$WS" -s workspace-write --json --output-last-message "$RUN/last.md" \
    -c show_raw_agent_reasoning=true -c "model_providers.llm-router.base_url=\"http://127.0.0.1:$PORT/run/$ID/v1\"" \
    "$(cat "$TASKDIR/prompt.md")" </dev/null >"$RUN/events.jsonl" 2>"$RUN/stderr.log"
  ```

  `PROFILE` = `flash-next` (tap port 18901) or `qwen-27b` (18902). There are no other overrides. Settings from
  `config.toml`: `model_reasoning_effort = "xhigh"` (the router drops it for both local accounts:
  `parameter_dropped:reasoning_effort`), `stream_max_retries = 8`, `request_max_retries = 6`, sandbox network on.
- **Workspaces**: the base tree from `git archive <base>`, `git init`, one commit tagged `base`, and no history.
  llm-router workspaces get their own `node_modules` (`pnpm install --frozen-lockfile --package-import-method=copy`
  in `bench-node24:local` = `node:24-bookworm-slim` + pnpm 10.32.1). Agents run Node commands through
  `/workspace/c4/bench/bin/n24` (docker, `--cpus 3 --memory 6g`), because the host has Node 20 and no pnpm. While
  agents ran, no answer material was on the worker: no clones, no fix commits, no test files from the commits. Each
  run directory was copied to the workstation and deleted from the worker before the next pair.
- **Tasks and base commits**: I1 llm-router `59a5214` (base `e6ba73c`), I2 `01cf00f` (`346cbe8`), I3 `72faf03`
  (`6ce2c3d`), I4 `1abae92` (`7e149cf`), I5 `6041c57` (`6a5dd26`), I6 ailab `5de43272` (`34937dee`); P1 llm-router
  `6e8104a`, P2 `93108f5`, P3 ailab `fcabf2d1^`. The task texts are frozen and kept in the evidence bundle.
  Validation: at the base, the graded files failed on the fix's own assertions (I1 1/13, I2 3/33, I3 3/40, I4 1/29,
  I5 2/51, I6 2/20). With the real commit applied, they all passed and the typecheck was clean.
- **Grading** (`grade.sh`): the agent's whole diff is saved. Then a fresh workspace gets only the agent's non-test
  changes: never its tests, test setup, runner or TypeScript config, `package.json` or lockfile. The commit's test
  files are copied in. Then `vitest run <graded files>` (JSON reporter) or `unittest`, plus `tsc --noEmit`, all in
  docker.
- **Timing tap** (`tap.mjs`, one container per model, `--network host`, 127.0.0.1): a pass-through to
  `https://router.chifor.me`, which is the LAN edge VIP 192.168.0.51, the same path the worker uses. Per request it
  records arrival, response head, the first reasoning delta, the first output delta, the terminal event and its usage,
  and the `x-trajectory-id` value. It logs no other header values and no bodies.
- **Engine timings (cloud3)**: llama.cpp `print_timing` lines from llama-swap's upstream log history
  (`/logs/stream/upstream`), read every ~4 min and after each pair. Each line is matched to a tap request by
  generated-token count and uncached prompt-token count.
- **Journal**: `GET /admin/v1/trajectories?principal=key:key_056782fff988` and `/admin/v1/trajectories/<id>`, read
  inside the router pod. The admin token stays in the pod.
- **Driver checks before every pair**: no `cloud3hold`, flash-next `ready` in `/running`, cloud2 vLLM waiting 0,
  running ≤ 2 and KV < 0.6 on two consecutive readings, no running trajectory of the benchmark key, and dev-worker-1
  `/workspace` ≤ 70 % used. cloud2's gauges were sampled every 30 s during each pair.

## Evidence

`model-bench-2026-11-03.tar.gz`, 9.0 MB, sha256 `604c17df09ca72395dc4a25750404c7a61da686c608adb23b12540fb6adb14b7`. It is
in dev-worker-1's `/workspace/archive/` (mode 600; the archive is capped at 20 GB, oldest item dropped first) and in
the session scratchpad on the workstation. It contains:

- the harness (`tap.mjs`, `n24`, `mkws.sh`, `run.sh`, `pair.sh`, `grade.sh`, the workstation driver, the analysis
  and table scripts);
- the frozen task texts, the judge checklists and the judge prompt;
- each run's Codex JSONL, its rollout (timestamps), stderr, final message, the agent's whole diff and the graded diff,
  and the grading logs;
- both taps' per-request records, the router journal of the benchmark key (14:48Z-19:38Z), cloud3's llama.cpp timing
  logs, the cloud2 load samples, and the generated `runs.csv` and `requests.csv`.

Before packing, every file was scanned for credential patterns (router keys, Gitea and OpenBao tokens, bearer headers)
and none were found. Codex's plugin caches were dropped from the private homes. The taps are stopped (`docker start
bench-tap-fn bench-tap-qw` brings them back for day 2).

