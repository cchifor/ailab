# Alignment round 1 — Fable and Codex — stateless-reviewers

Both reviewers read plan commit `b350df36` (after spike 3).

- **Codex:** ALIGNED. Suggested simplifying the pending-review rule to "delete any pending review once
  `pub<G>` is owned". **Pushback:** spike 3 shows a pending review can come from a failed POST on
  another head of the same PR; deleting it while that POST may still be in flight is exactly the risk
  the rule avoids. Kept: any foreign pending review → do not send; the operator's void deletes it.
- **Fable:** NOT ALIGNED — no blocker; one important gap and simplifications.

| ID | Finding / proposal | Disposition (plan v3) |
| --- | --- | --- |
| A1 | A head whose review was posted (`pubok`) then deleted can never be voided while its owner lives | Accepted via S3: `pubfin<G>` (definitive answer or never sent) satisfies the void by itself |
| A2 | Self-fence the send after a client pause | Accepted (section 5 step 5, monotonic) |
| A3 | `pubsent` failure must be judged by readback | Accepted (step 4) |
| A4 | Lost publication race has no outcome | Accepted: `rel.pub`, uncharged |
| A5 | Void ordering | Accepted: delete pending reviews, then `pubvoid` |
| A6 | `mergeint` name length / failure | Moot (S7) |
| S1 | Drop local mode, mode fence, dual code path | Accepted; `--export-coord` and an emergency `--import-local` stay |
| S2 | One authoritative `policy.json` (with `epoch`), loaded not compared | Accepted; replaces policy hash, mismatch alert, epoch file |
| S3 | One terminal outcome `pubfin` instead of `pubok`/`pubx` | Accepted |
| S4 | Drop the health-URL probe | Accepted: `pubfin` or `--owner-stopped <instance>` |
| S5 | Drop `pubseen` (Fable withdraws P2) | Accepted: forge time of `pubsent` vs the forge's current `Date` |
| S6 | Drop `a<N>.done` and the cross-head wait (disputes R11) | Accepted: the head watcher bounds overlap; a marker makes the head done |
| S7 | Drop `mergeint` + audit sweep (disputes P7) | Accepted: post-merge re-read in the same code path; residual stated |
| S8 | Fewer caps | Accepted: charged (incl. abandoned) ≥ 5, timeouts ≥ 2, window 40 |
| S9 | `iter_reviews` paginates to the end | Accepted |
| S10 | Drop the GitHub compare fast path | Accepted |
| S11 | Defer the janitor | Accepted (rollout step 4) |
| — | Codex pushback-free; Opus addition | A transient read error in the pre-send checks is retried within the budget before `pubfin` |
