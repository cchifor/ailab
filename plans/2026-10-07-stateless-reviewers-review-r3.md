# Plan review round 3 — forge portability and reviewer-bot findings — stateless-reviewers

Trigger: owner requirement (2026-10-07) that the reviewers and the coordination approach also work on
**GitHub**, plus the reviewer bots' findings on ailab#1127 (plan head `24158be3`).

## Forge portability analysis

reviewbot is Gitea-specific today in eleven places (`reviewbot.py` at `24158be3`): API base and
`token` auth (169–175), webhook headers `X-Gitea-Signature`/`X-Gitea-Event-Type` (270–274), event
names (`EVENTS`), `new_position`/`old_position` comments (2744), `APPROVED` event (2578, 2778),
`POST /merge {Do, head_commit_id}` (2559, 2583), combined status only (2539), the mutable
`/pulls/{n}.diff` (2667), `limit`/`page` pagination (2390, 2412), and the coordination primitive
(Gitea tag create with nonce readback). Section F of the plan maps each to GitHub and introduces a
forge adapter; the coordination primitive on GitHub is tag object + `POST /git/refs`, ownership by
the ref's target SHA. GitHub-only concerns: no inbound webhooks to LAN VMs (polling with ETags),
shared rate limits per identity, check runs vs statuses, diff-size limits, App approval counting,
merge queues, client-supplied tag dates.

## Reviewer-bot findings on ailab#1127 and dispositions

| ID | Reviewer | Finding | Sev. | Disposition |
| --- | --- | --- | --- | --- |
| B1 | codex | Proof the owner process is dead does not prove its POST cannot still commit server-side | blocker | Accepted: `pub_void_min_age_s` (1 h) + marker re-check by the replacement publisher; plan states the bound |
| B2 | codex | Base-ref check does not fence the merge (retarget between check and merge) | important | Accepted as a residual with detection: re-read before merge, post-merge base audit + critical alert; no forge offers an expected-base merge condition |
| B3 | claude | Proof of death via instance-name lookup breaks with duplicate names | important | Accepted: health URL in the claim message; unique names asserted by the role and at start |
| B4 | claude | Owner knows it will not POST again after an exception | nit | Accepted: `pubx<G>` single-writer outcome, accepted by `--requeue --force` |
| B5 | claude | Growth bound sentence wrong | nit | Accepted: bound stated per cut window |
| B6 | claude | Migrated rights have no owner to prove dead | important | Accepted: `migrated` sentinel treated as gone |
| B7 | claude | A late-landing own `pub<G>` has no caller in the claim cycle | important | Accepted: explicit branch in claim step 3 |
| B8 | claude | Restore epoch computed from the restored repo can repeat | important | Accepted: `rb1.epoch.<unix time>` from the operator's clock, compared by full name |
| B9 | claude | "approval adds clean" contradicts "no marker" | nit | Accepted: wording fixed |
| B10 | claude | Stale round-1 dispositions in the review trail | nit | Accepted: rows marked superseded |
