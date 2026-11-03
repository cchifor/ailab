# Results review - local-model-agent-benchmark - round 1 (codex, `--profile review`, gpt-6-astra)

All 13 findings were accepted and fixed in the results file. None was pushed back, so no round 2 was needed.

<!-- codex-impl-review-status: finalized -->

## Findings

### Preserve the ordered decision rule
**Location:** Recommendation — “What would make 27B the default”
**Severity:** important
**Disposition:** Accepted. The recommendation now restates day 2's decision in the plan's order (implementation lead over 1 run, then plans over 1.5 points on tasks both delivered, then robustness and speed), counts only matched runs, and calls keeping flash-next a deployment choice, not a benchmark verdict.

### Narrow the long-context reliability claim
**Location:** Recommendation — “flash-next’s long-context failure is gone”
**Severity:** important
**Disposition:** Accepted. Now "no long-context corruption in this sample": 217 correlated requests up to 81k, none at 100k, evidence not proof. The typecheck is no longer used as evidence for copied values.

### Separate request time, reasoning time, and output tokens
**Location:** Recommendation; How each model spends its time; Robustness — I1
**Severity:** important
**Disposition:** Accepted. Model-request occupancy is labelled as such. Think share is defined (leading-reasoning interval over generation interval, summed per run). "Thinks twice" became "twice the completion tokens", and I1's 218k is output tokens throughout.

### Keep client rates distinct from engine measurements
**Location:** Recommendation; Caveats — Client-observed speed
**Severity:** important
**Disposition:** Accepted. The 2.4× is labelled client-observed. The agreement claim is limited to the output-rate and decode aggregates (median 49 and p90 59 for both), and the caveat says the prefill proxy differs from llama.cpp (4 tok/s at the median, 33 at the p90).

### Restore the workload-comparison caveat
**Location:** Speed by prompt size; How each model spends its time — “Context growth slows both”
**Severity:** important
**Disposition:** Accepted. The bucket table and the bullet say it is an association, not a controlled test.

### Treat capped durations as censored completion times
**Location:** Recommendation; How each model spends its time — “similar wall times”
**Severity:** important
**Disposition:** Accepted. Capped durations are called lower bounds; "similar wall times" was removed.

### Qualify the attribution to self-checking
**Location:** How each model spends its time — “Correct source by” and “Self-checking is extensive”
**Severity:** important
**Disposition:** Accepted. The time after the last source edit is described as an observation, with no causal claim; first-correct latency is named as unmeasured.

### Define which follow-up runs affect the decision
**Location:** Tomorrow — items 2–4
**Severity:** important
**Disposition:** Accepted. Tomorrow's rules are frozen. Matched 35-min second repeats count. The 90-min reruns are diagnostic only, matched for both models on I1, I2, I3 and I5, and called 90-min capped runs. Selective third repeats were dropped.

### Specify how incomplete planning affects tomorrow’s comparison
**Location:** Tomorrow — Planning
**Severity:** important
**Disposition:** Accepted. Delivery is reported separately (today's P2: 0/1 for each). The judged mean covers only tasks both models delivered on, and fewer than 2 of 3 makes planning inconclusive. The two judge passes are described as a noise check, not replication.

### Reconcile the cloud2 load statements
**Location:** Invalid or uncounted runs; Caveats — Speed is as deployed
**Severity:** important
**Disposition:** Accepted. Both places now say what the 30-s samples showed, and that activity between samples was not measured.

### Account for requests missing from prompt buckets
**Location:** Speed by prompt size
**Severity:** nit
**Disposition:** Accepted. Requests with no usage (streams cut at the cap) now have their own bucket row (4 and 3).

### Suppress percentiles below the declared sample minimum
**Location:** Speed by prompt size
**Severity:** nit
**Disposition:** Accepted. p90 is omitted under 5 samples.

### Correct the repeat description and label denominators
**Location:** What ran; Caveats — Small sample; Per model table
**Severity:** nit
**Disposition:** Accepted. Now "one repeat per task, two for I2" (7 implementation runs and 1 planning attempt per model). The per-model table says the outcome columns count implementation runs, and that requests and tokens include P2.