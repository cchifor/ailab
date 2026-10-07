# Plan review round 4 — Fable and Codex on forge portability — stateless-reviewers

Both reviewers read plan commit `46cd2374` (section F and fixes B1–B10). Fable checked the GitHub API
claims against the documentation. Both agree the GitHub primitive (atomic ref create pointing at a
content-addressed object that holds my nonce) is sound under concurrency.

| ID | Source | Finding | Sev. | Disposition |
| --- | --- | --- | --- | --- |
| P0 | Codex (blocker), Fable | B1: an age bound measured from `pub<G>` does not bound a POST sent later; step 3 lacks the promised marker re-check | blocker | Accepted: mandatory `pubsent<G>.<t>` before sending (no `pubsent` ⇒ provably never sent ⇒ void at once); possibly-sent rights need an hour on two clocks; marker re-check added to step 3; `pub_void_min_age_s` is a config key; residual stated |
| P1 | Fable, Codex | Retry re-mints the GitHub tag object with a new SHA → own right unrecognisable | important | Accepted: frozen payload per claim; ownership proven on both forges by the nonce in the pointed-at object; SHA equality only a fast path |
| P2 | Fable | `pub<G>` age on GitHub is client-supplied | important | Accepted: operator-side `pubseen<G>` first-observation record; Phase 0b looks for a server-set ref time |
| P3 | Fable, Codex | App permission scoping not expressible; merge needs `contents: write` | important | Accepted: permissions restated |
| P4 | Fable, Codex | `/pulls/{n}/files` fallback reintroduces the mutable diff | important | Accepted: compare JSON fallback, 300-file cap, fail closed |
| P5 | Fable, Codex | GitHub CI rule vacuously green with zero checks; unpaginated; merge-commit checks | important | Accepted: ≥ 1 check required, paginated, required names from rules; which commit checks attach to → Phase 0b |
| P6 | Codex | Pending-review cleanup contradicts the attribution guard | important | Accepted: delete only a pending review this attempt created; otherwise ambiguous |
| P7 | Codex | B2 audit lost with a lost merge response | important | Accepted: `mergeint` record + audit over recently merged PRs |
| P8 | Fable, Codex | Content-call budget understated; `Retry-After` optional; 304 exempt from the primary limit only | important | Accepted: budget recomputed (~9 per review), fallback backoff, per-instance parking |
| P9 | Fable | 422 is the normal race outcome; outcome tags cost two calls | nit | Accepted: lightweight refs for outcome/operator tags; 422 "already exists" is `lost` |
| P10 | Fable | Readback must use the singular exact ref endpoint; matching-refs has no pagination | nit | Accepted |
| P11 | Fable | Missing events `review_requested`, `converted_to_draft`, `closed` | nit | Accepted |
| P12 | Fable | Authenticate markers by bot user id, not login only | nit | Accepted |
| P13 | Fable | Loose ends: optional `health`, late-own-right claim vs caps, forge-neutral repo id, janitor via adapter, GitHub tag rulesets | nit | Accepted |
| P14 | Fable, Codex | Timestamp epoch can repeat or fail to become current | important | Accepted: random epoch id in an authoritative `epoch` file |
| P15 | Codex | Cut-window bound off by one | nit | Accepted: `hw − K + 1 ≥ 40` |
| — | Codex | B10 superseded rows "not available" | — | No change: the rows are marked in `…-review-r1-fable.md` (Codex read a stale copy) |

The plan is finalized after this round. The round-4 fixes are not re-reviewed (the review cap); the
implementation review covers them.
