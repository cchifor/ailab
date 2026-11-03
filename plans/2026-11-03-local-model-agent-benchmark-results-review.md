# Results review - local-model-agent-benchmark - round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The main totals reconcile: requests, output tokens, implementation outcomes, and timeout counts match the per-run table.
- “Inconclusive” is consistent with tied implementation success and missing planning scores, but the proposed switching conditions do not preserve the ordered decision rule.
- Claims about reasoning, long-context reliability, and completion speed exceed what the measurements establish.
- Tomorrow’s work needs explicit scoring boundaries for longer and selective reruns, plus a policy for planning runs that still produce no plan.

## Findings

### Preserve the ordered decision rule
**Location:** Recommendation — “What would make 27B the default”
**Severity:** important
<!-- codex: The independent “or” conditions omit the rule’s ordering: planning decides only after implementation ties, and corruption that conflicts with quality requires “inconclusive,” not an automatic switch. Restate the >1-run implementation threshold over all eligible runs, followed by planning and then robustness/speed under their stated conditions; distinguish keeping flash-next for capacity diversity from a benchmark-derived preference. -->

### Narrow the long-context reliability claim
**Location:** Recommendation — “flash-next’s long-context failure is gone”
**Severity:** important
<!-- codex: Zero observed failures across 217 correlated requests at 30k–81k supports “not observed in this sample,” not “the failure is gone”; no flash-next request reached 100k. Limit the conclusion to the observed runs and context range, and avoid treating typechecking as proof that every copied value was correct. -->

### Separate request time, reasoning time, and output tokens
**Location:** Recommendation; How each model spends its time; Robustness — I1
**Severity:** important
<!-- codex: The 78–99% measures time inside model requests, not pure generation or thinking, while think share measures a leading-reasoning time fraction rather than a reasoning-token fraction; twice the completion tokens therefore does not establish twice the thinking. Define the think-share calculation and aggregation, relabel request occupancy accurately, and reconcile I1’s “218k reasoning tokens” with the later “218k output tokens” unless a separate reasoning-token measurement supports the former. -->

### Keep client rates distinct from engine measurements
**Location:** Recommendation; Caveats — Client-observed speed
**Severity:** important
<!-- codex: The approximately 2.4× comparison comes from client-observed output rates (120/49 tok/s), not measured vLLM engine decode, so label it accordingly. The claimed agreement within ~2 tok/s also cannot cover prefill: flash-next’s client/engine medians differ by 4 tok/s and p90s by 33; restrict the agreement claim to the displayed decode aggregates rather than implying per-request or cross-engine validation. -->

### Restore the workload-comparison caveat
**Location:** Speed by prompt size; How each model spends its time — “Context growth slows both”
**Severity:** important
<!-- codex: These buckets contain model-chosen prompts from different task trajectories, and requests within a run are correlated, so the table does not isolate context length as the cause of lower rates or constitute a controlled throughput comparison. Restore the plan’s explicit caveat and describe the observed association between larger prompt buckets and lower rates without asserting causation. -->

### Treat capped durations as censored completion times
**Location:** Recommendation; How each model spends its time — “similar wall times”
**Severity:** important
<!-- codex: Four of eight runs per model were stopped at 35 minutes, so similar recorded durations partly reflect the imposed cutoff and cannot establish similar time to actual completion. Label capped durations as lower bounds on completion time and distinguish wall time consumed under the budget from time to a finished, correct result. -->

### Qualify the attribution to self-checking
**Location:** How each model spends its time — “Correct source by” and “Self-checking is extensive”
**Severity:** important
<!-- codex: The last edit of a final passing patch is reasonably described as an upper bound on first correctness, but it does not identify that first moment or establish that self-checking caused most timeout latency. Describe the observed post-edit activities without that causal conclusion; measuring first-correct latency would require grading intermediate source states. -->

### Define which follow-up runs affect the decision
**Location:** Tomorrow — items 2–4
**Severity:** important
<!-- codex: “Uncapped” reruns with a 90-minute cap remain censored, and the asymmetric I2/I5 reruns or third repeats selected only from divergent tasks change exposure if their outcomes enter the >1-run decision threshold. Freeze eligibility, time budgets, aggregation, and stopping criteria before day 2; keep selective extended runs diagnostic or use matched comparisons under a common budget, and call them 90-minute capped runs. -->

### Specify how incomplete planning affects tomorrow’s comparison
**Location:** Tomorrow — Planning
**Severity:** important
<!-- codex: A 90-minute cap does not guarantee judgeable plans, and averaging only completed plans could compare different task subsets and conceal delivery failures; today’s P2 supplies a 0/1 delivery result for each model, whereas P1/P3 were unattempted. Report delivery separately from judged quality, specify when missing plans require “inconclusive,” and acknowledge that two judge passes on one artifact do not provide independent planning-run replication. -->

### Reconcile the cloud2 load statements
**Location:** Invalid or uncounted runs; Caveats — Speed is as deployed
**Severity:** important
<!-- codex: “cloud2 never ran more than one request: the benchmark’s own” conflicts with “cloud2 also served other agents during the runs,” and 30-second gauge sampling cannot establish continuous absence of overlap. Distinguish shared-service availability from observed concurrent load, reporting what the samples showed and acknowledging that brief activity between samples was unmeasured. -->

### Account for requests missing from prompt buckets
**Location:** Speed by prompt size
**Severity:** nit
<!-- codex: The bucket counts sum to 315 flash-next requests and 431 qwen requests, versus overall totals of 319 and 434, leaving four and three requests unexplained. Add an unknown/excluded category or a reconciliation note, including how incomplete requests and unavailable usage affect the rate samples and token totals. -->

### Suppress percentiles below the declared sample minimum
**Location:** Speed by prompt size
**Severity:** nit
<!-- codex: The table reports p90s for flash-next below 10k prompts (n=4, engine n=3) and qwen at or above 100k (n=4), contrary to the plan’s explicit “no p90 under 5 samples” requirement. Suppress those p90s while retaining the medians and sample counts. -->

### Correct the repeat description and label denominators
**Location:** What ran; Caveats — Small sample; Per model table
**Severity:** nit
<!-- codex: “One run per task per model” contradicts the I2 repeat-2 rows: each model has seven implementation runs across six tasks, plus one P2 run. State “one repeat except I2, which has two,” and label the per-model table so readers can distinguish implementation-only outcome denominators from request/token totals that include P2. -->