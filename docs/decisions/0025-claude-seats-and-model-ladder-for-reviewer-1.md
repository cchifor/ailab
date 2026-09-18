# ADR 0025 — reviewer-1 rotates across three Claude accounts and a model ladder

**Status:** ACCEPTED (2026-09-18). **IMPLEMENTED** — Phase 1 (seats, ailab#777) and Phase 2
(ladder, watchdog, dashboard) of `plans/2026-09-18-claude-seat-rotation-plan.md`.

> **Credential change during Phase 1's rollout, same day.** Decision 2 below planned the seats
> on the brokers' long-lived tokens. Those answer `/api/oauth/usage` and `/profile` with
> `403 oauth_scope_insufficient` (they carry `user:inference` only), so the identity collapse
> and the watchdog could not see them. Every seat was moved to a **browser login in its own
> HOME** the same afternoon — one refresh-token family per seat, so the copy hazard that
> motivated decision 2 still does not arise. Decision 2 stands only as the record of why the
> token path exists in the wrapper. All four accounts on the estate were also at their weekly
> wall that day; the persona resumed when seat `c` was moved to an account with headroom.

## Context

On 2026-09-18 the claude persona idled for 21 h: its one account hit `You've hit your weekly
limit · resets Sep 19, 8pm (UTC)`, the worker parked every 15 minutes with 9 jobs queued, and
reviewer-2 accumulated 7 merge-blocked PRs waiting for a claude verdict. Nothing paged — the
window was 37 h and every alert that could see it is sized for hours.

The same morning, `GET https://api.anthropic.com/api/oauth/usage` with that account's token
(the call behind the CLI's `/usage`; consumes no quota) reported `weekly_all` at 100 % and a
`weekly_scoped` entry for `Fable` at 100 %, both with a reset time. Two consequences:

1. **Fable's limit is a weekly window**, not the dead credit pool `host_vars/reviewer-1.yml`
   had recorded after two accounts "never recovered" — each was watched for less than 7 days.
2. **The API is the ground truth reviewbot never had.** Its parks are 15-minute guesses
   re-probed by doomed calls because the weekly message has no parseable time.

reviewer-2 already solves the capacity half of this for codex: `pr_reviewer_llm_seats`, sticky
selection, a lossless park per seat (ADR 0024, `plans/2026-09-16-codex-seat-rotation-plan.md`).
That machinery is kind-agnostic.

## Decision

1. **Three seats for the claude persona** — `a/b/c` = `clauderun/clauderun2/clauderun3`, one
   Claude Max account each: the agentforge broker accounts `claude-max-1/2/3`, whose long-lived
   tokens already live in OpenBao. The reviewer shares their weekly windows with the dev agents,
   as the codex plan accepted (the reviewer is ~96 % of its own load).
2. **Tokens are hand-seeded once**, not rendered by a bao agent: a `setup-token` credential does
   not rotate, so the refresh-token-family hazard that forced the codex design does not exist
   here. The token reaches the CLI through the environment via `claude-seat.sh` in the seat's
   own HOME — never on argv, which sudo shows to every process.
3. **Model ladder `fable → opus → sonnet`, tier-major, sticky within a tier.** Descend only when
   every seat is parked for the current tier; climb back only from the hourly watchdog, which
   parks and unparks per seat and per tier from the usage API's numbers. (Phase 2.)
4. **The lossless park stays the invariant.** No attempt consumed, queue intact, nothing
   quarantined, whatever the scope of the refusal.
5. **The active account's email is a Prometheus label**, read from `/api/oauth/profile` at
   runtime. It lands in the textfile, the TSDB and Grafana — not in git.

## Consequences

* The claude persona survives one exhausted account with no operator action, and three
  accounts triple the runway that just proved insufficient for one. If exhaustion still
  recurs daily the answer is quota, not code — the same conclusion ADR 0024 reached.
* `resolve_seats()` now dedupes claude seats by profile account uuid; `ReviewbotSeatsDegraded`,
  `SeatExhausted`, `AllSeatsExhausted` and `SeatNeverServes` cover claude unchanged.
* Phase 1 keeps today's same-seat fallback for a Fable-scoped refusal; Phase 2 replaces it
  with the ladder, which is the point at which a spent Fable no longer costs a doomed call
  per review.
* One assumption is verified only in Phase 2: that the broker tokens answer the usage and
  profile endpoints. If they do not, rotation still runs on refusal text and the watchdog
  reports `usage_probe_ok=0` — the plan records the fallback.
