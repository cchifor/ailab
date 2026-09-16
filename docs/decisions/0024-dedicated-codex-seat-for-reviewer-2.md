# ADR 0024 — reviewer-2 gets more Codex capacity; review volume is not cut to fit one seat

> **AMENDED 2026-09-16, same day, before any seat was bought.** This ADR originally decided on a
> *dedicated* seat, reasoning that the reviewer was starved by sharing one with the AgentForge
> dev agents. **That premise was wrong and the measurement is below.** The dev agents make 41
> `codex exec` calls a week against the reviewer's ~980 — the reviewer is ~96% of its own
> problem, and a dedicated seat recovers ~4%. The reviewer exhausts a seat on its own, so
> un-sharing changes almost nothing.
>
> The decision is therefore now: **three licences, all pooled for the reviewer, with reviewbot
> rotating across them** — because a second or third seat is worth nothing unless the bot can
> route around a spent one. Design in
> `plans/2026-09-16-codex-seat-rotation-plan.md`. Everything below stands except where it argues
> for dedication-as-capacity; it is kept rather than rewritten because the measurement that
> overturned it is the useful part of the record.

**Status:** ACCEPTED (2026-09-16), AMENDED the same day (see above). **IMPLEMENTED** — all three
phases of `plans/2026-09-16-codex-seat-rotation-plan.md` are merged and live on reviewer-2:
rotation (Phase 1), two provisioned seats (Phase 2), and the four seat alerts (Phase 3).

**With one gap that is not code:** only TWO of the three intended licences exist. Verified
2026-09-16 across every host by `tokens.account_id` — `cfdea639…` (shared with the dev agents)
and `9c8a8cfb…`, and no third anywhere. So the deployed capacity is ~340–400 codex calls/day,
not the ~510–600 this ADR sized for. The third licence needs one `codex login --device-auth`
run and a one-line host_vars change; the runbook has the procedure. Until then the estate is
at roughly 1.6–1.9× present demand rather than 2.5–3×, against a rate that grew 2.4× in a week. What ships in the PR carrying this ADR is only the observability half: the
`ReviewbotRateLimited` alert, the upstream refusal text in the park note, and the model-scoped
primary/fallback counters. Buying the second Codex subscription, logging it in, and repointing
`codexrun` at it are MANUAL steps that have not been performed — see "What is still outstanding".
Until they are, reviewer-2 remains on the shared seat and the outage described below WILL recur.

**Relates to:** ADR 0020 (dev-worker OpenBao credentials — the shared codex login this moves away
from for the reviewer), ADR 0018 (the autonomous dev agents that share that login), the reviewers
section of `docs/runbooks/dev-workers.md`, and `kubernetes/apps/infrastructure/monitoring/reviewbot-rules.yaml`.

## Context

The codex persona (reviewer-2, `.25`, OS user `codexrun`, model `gpt-6-astra`) exhausted its
ChatGPT subscription quota **twice in 32 hours** on 2026-09-14/15, and was blocked for **20h21m of
a 32h29m span (62.6%)**:

| | Episode A | Episode B |
|---|---|---|
| Window | Sep 14 22:09 → Sep 15 06:18 UTC (8h33m) | Sep 15 18:40:45 → Sep 16 06:16:52 UTC (11h36m) |
| Code | before the park path existed | after it (deployed Sep 15 07:05:45) |
| Behaviour | 98 usage-limit errors; jobs 1401–1417 each burned all 5 attempts | 46 parks × 900s; `attempts=0` on all 7 queued jobs |
| Outcome | 17 jobs quarantined, needed `--requeue` by hand | zero quarantines; queue drained in ~60s on recovery; 4 PRs auto-merged |

The park path introduced between the two episodes works, and Episode B is the proof: same PID
throughout, `NRestarts=0`, no operator action, full recovery. **The remaining problem is purely
capacity.**

**The limit is ACCOUNT-scoped, so no fallback can rescue it.** `reviewbot.py` states this at
L697-698 and L968; `llm_fallback_model` is `""` for codex and the fallback block in `_run_llm()`
lives in the claude branch only. A fallback model billed to the same seat shares a budget that is
already gone — configuring one would reintroduce the doomed-fallback loop the parking logic exists
to remove.

**The blast radius is estate-wide, not persona-local.** `merge_personas: [claude, codex]` on BOTH
hosts, so a parked codex halts every automerge: during Episode B the *healthy* reviewer-1 logged
313 `codex=no review` merge-holds and carried `merge_blocked_seconds` to 44250 (12.3h). Two PRs
(`platform#1374`, `ailab#737`) were hand-merged past the two-persona gate to get work moving.

**The refusal text names a reset that is not the real one.** All 98 Episode A errors carry
`try again at Sep 21st, 2026 9:38 AM`, yet recovery happened Sep 15 06:20 and Sep 16 06:17 — the
same clock time two days running. What reopens is a rolling window, hours out, not the date in the
message. This is already why `RESET_RE` deliberately does not parse it; recording it here so the
date is not mistaken for a capacity fact.

**Load is the driver, and it is real work, not churn.** Measured `codex exec` calls/day Sep 9→15:
86, 167, 101, 93, 125, **198**, **210**. The two exhaustion days are the two heaviest; Sep 11–12
(101, 93) had no block. The load is broad rather than pathological — over 7 days `cchifor/platform`
alone accounts for 510 of 845 jobs across **217 distinct PRs at 2.4 review rounds each**, with no
single hotspot PR (the busiest is 8 jobs).

**THE SHARE IS THE REVIEWER'S, NOT THE DEV AGENTS' (measured 2026-09-16 — this is what amended
the decision).** Over the same 7 days the six dev-workers made **41** `codex exec` calls between
them, against the reviewer's ~980: the reviewer is **~96%** of the shared seat's consumption.
They are genuinely the same account — `tokens.account_id` reads
`cfdea639-03a7-4690-9a8b-eaa4566d5063` on both reviewer-2's `codexrun` and dev-worker-1's `c4` —
so the sharing is real, it is simply not the constraint. Un-sharing buys ~4%.

That also brackets the wall empirically: 09-10 ran 167 calls without a refusal, 09-14 ran 198 and
09-15 ran 210 and both exhausted, so **one seat is worth roughly 170–200 calls/day** against
present demand of 210.

**Two different questions, which an earlier draft ran together** (reviewer-codex, round 2):

* *Does swapping the shared seat for a dedicated one fix it?* **No.** Without rotation the persona
  uses one credential at a time, so a dedicated seat is still 170–200/day against demand of 210 —
  at best parity, and it was this scenario the "parity, not headroom" line was about. Stated
  without that qualifier it read as a claim about two seats, which it is not.
* *What do two POOLED seats give?* 340–400/day against 210 — **roughly 60–90% headroom**, which is
  real. Three give 510–600, ~2.5–3×.

So the case for the third seat is not present demand, which two would already cover. It is growth:
86 → 210 calls/day inside one week, which on the arithmetic in the plan consumes even the
three-seat ceiling in 1–3 weeks. Buying the third now is buying re-provisioning time, and it is
worth saying plainly that it is a hedge rather than a fix.

## Decision

**Provision a second, dedicated Codex subscription for the reviewer identity, and do not reduce
review coverage to fit the existing one.**

Operator-directed, 2026-09-16, in those terms: *"Reviews are important, do not cut them. Add a
second codex license."* The reviewer stops sharing the estate's single login with the AgentForge
dev agents (ADR 0020) and gets a seat whose budget is consumed only by PR review.

## Consequences

- Recurring subscription cost for a second seat.
- `codexrun`'s credential stops coming from the shared `af/dev-workers/codex-auth` projection and
  needs a reviewer-scoped sibling plus its own role_id/secret_id in
  `ansible/secrets/reviewbot.sops.yaml`. The dev agents keep the existing shared login unchanged.
- **A second seat raises the ceiling; it does not remove it.** The trend is 86 → 210 calls/day in a
  week. If it keeps climbing this recurs on the new seat, and the next lever would have to be
  volume or a per-repo split. `ReviewbotRateLimited` is what makes the next occurrence legible
  within the hour instead of after an SSH.
- Reviews continue to be produced by both personas on every allowlisted repo. No repo loses
  coverage, no author class is skipped, and `pr_reviewer_llm_effort` stays at `medium` —
  all three were considered and rejected below.

## What is still outstanding (manual, not shipped by this PR)

1. Purchase the second Codex/ChatGPT subscription.
2. `codex login` ON reviewer-2 as `codexrun` against the new account — interactive, browser OAuth;
   no Ansible task can perform it. Never copy an `auth.json` between hosts (the 2026-09-10 claude
   outage was exactly that: copies share one refresh-token family and rotation invalidates all but
   whichever host refreshed last).
3. Project the new credential into OpenBao and point `pr_reviewer_enable_openbao` at it, so the
   login is managed rather than hand-seeded.
4. Confirm the fix: a full day above 200 `codex exec` calls with zero park cycles
   (`journalctl -u reviewbot | grep -c "parking the worker"`).

## Alternatives rejected

- **Drop `cchifor/platform` from the codex allowlist.** Cuts 60% of load (74–79% on the two
  exhaustion days) and would reliably prevent recurrence — but it removes second-persona review
  from the estate's busiest repo, which is the coverage most worth having. Rejected explicitly by
  the operator.
- **Skip bot-authored PRs (renovate-bot, agentforge-ci-bot) on codex only.** Narrower, but still a
  coverage cut, and it needs a persona-scoped author skip that does not exist today. Same rejection.
- **Lower `pr_reviewer_llm_effort` to `low`.** Cheapest to ship and entirely unmeasured: we have not
  established whether the window is denominated in requests or tokens, so it may buy nothing while
  degrading every review. Not done.
- **Configure a fallback model for the codex branch.** Cannot work — see the account-scoped
  paragraph above.
- **Wait it out.** This is what the park already does, and it is correct behaviour; it is not a fix.
  At the current cadence it costs roughly half of every wall-clock day of automerge.
