# Plan review round 1 — Codex — stateless-reviewers

Reviewer: Codex (`gpt-6-astra`, plan-review profile, read-only), on plan commit `493ba541`. 32 findings
in the comprehensive pass plus a focused pass (4 findings, all duplicates). Dispositions refer to the
revised plan.

| # | Finding | Sev. | Disposition |
| --- | --- | --- | --- |
| C1 | Pre-POST checks do not enforce ownership at POST time (pause → takeover → both post) | blocker | Accepted: irrevocable `pub<G>` publication right per (kind, head); leases are efficiency only |
| C2 | Ambiguous-POST protection written too late (crash / failed `.q`) | blocker | Accepted: `pub<G>` without marker = ambiguous by construction |
| C3 | Instance names are not ownership tokens | blocker | Accepted: per-claim nonce, readback decides |
| C4 | Reset filtering restarts numbering → permanent 409 loop | important | Accepted: global high-water mark; `cut<hw+1>` |
| C5 | Requeue check-then-write races an owner | blocker | Accepted: `cut` refuses if leased; `pubvoid<G>` names an exact generation; publication closes the window |
| C6 | Late outcomes make state "exhausted" while an owner still runs | important | Accepted: counters only; posting governed by publication |
| C7 | Uncharged expiry releases loop forever | important | Accepted: lease exhaustion charged as deadline class; `released` cap |
| C8 | HTTP Date is not a monotonic coordination clock | important | Accepted: safety no longer depends on clocks; `tagger.date` sanity bound |
| C9 | Posting-margin check uses an aging timestamp | important | Accepted: Date + monotonic elapsed; moot for safety |
| C10 | Non-positive model budget after acquisition | important | Accepted: `min_llm_s`, `.rel.budget` counted |
| C11 | Approval upgrade cannot satisfy the fence | important | Accepted: upgrades unlocked, harmless `clean` duplicates; invariant "one review + approvals" |
| C12 | Abandoned `appr` blocks merge forever | important | Accepted: `appr` removed |
| C13 | `.ok` still affects coordination | nit | Accepted: markers are terminal input; `pubok` only a speed-up |
| C14 | Legacy markers do not prove coverage | important | Accepted: unknown coverage counts as not full (conservative) |
| C15 | Coverage field breaks older readers | important | Accepted: separate `v1.cov` comment; canonical marker unchanged |
| C16 | Switching to Gitea discards local quarantines | blocker | Accepted: drained `--export-coord` |
| C17 | Rollback ignores Gitea quarantines | blocker | Accepted: drained `--import-local`; not a toggle |
| C18 | Inventory cannot prevent mixed-mode execution | blocker | Accepted: mode fence tag checked at runtime by every Phase-1+ instance |
| C19 | Config mismatch can merge without a required persona | blocker | Accepted: authoritative `policy.json` in the coordination repo; mismatch refuses merges |
| C20 | `iter_reviews` silently truncates at 500 reviews | blocker | Accepted: fail closed |
| C21 | Retargeted base reuses a verdict | important | Pushback: pre-existing behaviour, rare; recorded as a known limitation; `v1.cov` carries `base=` for later |
| C22 | Sweep slots are not mutual exclusion | important | Accepted: sweep leader removed |
| C23 | Parked leader starves peers | important | Accepted: every instance discovers independently |
| C24 | Retention deletes quarantine decisions | blocker | Accepted: only merged PRs are ever cleaned |
| C25 | Janitor crash leaves partial histories | important | Accepted: merged PRs only, so partial deletion is harmless |
| C26 | Delayed deletes remove recreated claims | important | Accepted: merged PRs never get new tags |
| C27 | Delete failures lack retry/fairness | important | Accepted: continue past failures, batch, alert on 401/403 |
| C28 | Release history grows unbounded | important | Accepted: `released` cap → exhausted |
| C29 | Tagger is not authenticated identity | important | Accepted and confirmed by spike 2 (tagger is always `Gitea`): repo ACL only |
| C30 | Spike does not establish atomicity across failures/versions | important | Accepted: Phase 0 extended; correctness no longer depends on losers returning 500 |
| C31 | Prefix listing lacks claim records | important | Accepted: hydration via `/git/tags/{sha}` only for outcome-less claims and `pub` |
| C32 | Truncated listing → permanent 409 loop / hidden `.q` | important | Accepted: Phase 0 measures; fail closed at the page cap |
