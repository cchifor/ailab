# 2026-09-16 — Codex seat rotation for reviewer-2

Design for letting the codex persona hold N Codex subscriptions and route around an exhausted
one, instead of parking the whole worker. Operator-directed 2026-09-16: three licences total,
all three pooled for the reviewer, plan before implementation.

Supersedes the capacity half of **ADR 0024**, whose premise was wrong — see "What changed".

## What changed since ADR 0024

ADR 0024 framed the fix as *stop sharing a seat with the AgentForge dev agents*. Measured
2026-09-16, that is nearly worthless:

| Consumer (7d, `codex exec` invocations) | calls |
|---|---|
| reviewer-2 | ~980 |
| all six dev-workers combined | **41** |

Both are on the same account — `account_id cfdea639-03a7-4690-9a8b-eaa4566d5063`, read from
`tokens.account_id` on reviewer-2's `codexrun` and dev-worker-1's `c4` and identical. So the
reviewer is **~96% of its own problem**; moving the dev agents off recovers ~4% and the reviewer
still exhausts a seat on its own.

**The single-seat wall is bracketed by observation**, not guessed: 2026-09-10 ran 167 calls with
no refusal; 09-14 ran 198 and 09-15 ran 210, both of which exhausted the window. So one seat is
good for roughly **170–200 calls/day**, against present demand of 210/day and a trend of
86 → 210 in one week.

Therefore a second seat is only worth buying if reviewbot can *use* it when the first is spent.
Rotation is not an optimisation on top of the extra licences — it is the thing that makes them
do anything at all.

Capacity with three seats: **~510–600 calls/day**, ~2.5–3× present demand.

**How long that lasts — corrected.** An earlier draft said 5–8 weeks; that does not follow from
the growth rate this document itself cites, and both reviewers caught it (round 1 of ailab#742's
successor, #743). Working it properly from 210/day against a 510–600/day ceiling:

* **Linear** (+124 calls/day per week, the observed 86 → 210 step): reaches 510 in ~2.4 weeks,
  600 in ~3.1 weeks.
* **Exponential** (×2.44 per week, the same two points read as a ratio): reaches 510 in ~1.0
  weeks, 600 in ~1.2 weeks.

So the honest figure is **1–3 weeks**, not 5–8. Worth stating plainly because it changes what
this project is: three seats buy time to fix review volume, they are not themselves the fix.

And a caveat on all of the above — this is **two data points a week apart**. Sep 9–15 also
contains 167, 101, 93 and 125, which is not a smooth curve, so neither model is well supported.
Treat 1–3 weeks as an order of magnitude and re-measure rather than trusting the extrapolation.

The durable lever remains review volume — most cheaply the 2.4 review rounds per PR, not the
repo allowlist.

## Decisions

1. **Three seats, all pooled for the reviewer.** The dev agents stay on the seat they already
   use. Once the reviewer can route around a refusal, their ~6 calls/day are harmless noise;
   fencing a seat off for them would idle it ~97% and cost a third of the capacity.
2. **Sticky selection, NOT round-robin.** Use one seat until it refuses, then move to the next.
   Round-robin spreads load evenly and therefore drives all three seats toward their walls
   *simultaneously*, converting three staggered recoveries into one synchronised outage. Sticky
   keeps the windows staggered so a fresh seat almost always exists. Same total quota, strictly
   better failure shape.
3. **The lossless-park property is the invariant.** Whatever else changes, an exhausted account
   must still consume no attempt, leave the queue intact, and quarantine nothing. That property
   is what made the 2026-09-15 episode recoverable (46 parks, 11h36m, 7 jobs held, drained in
   ~60s on reopening) where the 09-14 episode before it quarantined 17 jobs. Every test below
   exists to defend it.

## Design

### Config

```yaml
# ansible/roles/pr_reviewer/defaults/main.yml
pr_reviewer_llm_seats: []          # [] = single-seat, exactly today's behaviour
# host_vars/reviewer-2.yml
pr_reviewer_llm_seats:
  - { name: a, sudo_user: codexrun }       # the existing seat, unchanged
  - { name: b, sudo_user: codexrun2 }
  - { name: c, sudo_user: codexrun3 }
```

**On the two spellings** (reviewer-claude, round 1): `pr_reviewer_llm_seats` is the Ansible
variable; `llm_seats` is the key it renders into `/etc/reviewbot/config.json`, which is what
reviewbot.py reads. That is the existing convention for every option here —
`pr_reviewer_llm_model` → `llm_model`, `pr_reviewer_llm_timeout_s` → `llm_timeout_s` — not an
inconsistency. Prose and tests below use the config.json spelling because that is the name in
the code.

`llm_seats: []` falls back to the current `llm_sudo_user`, so the claude persona and any
single-seat host are untouched by this change and Phase 1 deploys as a no-op.

### Isolation — one OS user per seat

Each seat gets its own OS user whose `$HOME/.codex/auth.json` holds **exactly one** credential.
This is not incidental: `codexrun` exists so a prompt-injected diff has no credentials to read,
and `_run_llm()` already scans model output for the auth token before posting. Swapping several
credentials in and out of one HOME would put all three within reach of a single compromised run
and defeat that. The existing scan reads `/home/<sudo_user>/.codex/auth.json`, so pointing
`sudo_user` at the active seat carries the protection across unchanged.

### `account_id` uniqueness guard — load-bearing

Rotating *within one account* is precisely the doomed-fallback loop the park path exists to
kill; it cost 463 doomed calls over four days in 2026-09. The only thing separating "rotation"
from that mistake is that the seats are genuinely different accounts.

So at startup and on each reload, read `tokens.account_id` from every seat and **collapse
duplicates to a single seat**, loudly:

* log at WARN naming the colliding seats,
* export `reviewbot_llm_seats_total` (configured) and `reviewbot_llm_seats_distinct` (usable),
* alert when they differ.

Collapsing rather than refusing to start is deliberate: a review bot that will not boot is worse
than one running at reduced capacity, and the metric makes the reduced capacity visible instead
of silent. A mis-provisioned "third licence" must show up as capacity you can see, never as a
retry loop against a wall.

### Rotation state

Replace the module global `RATE_LIMITED_UNTIL` with `SEAT_PARKED_UNTIL = {seat: epoch}`.

* `active_seat()` returns the current seat if its deadline has passed, else the first other seat
  whose deadline has passed, in configured order.
* On `RateLimited` for seat S: record S's deadline, select the next available seat, and **retry
  the same job immediately, consuming no attempt** (the PR did nothing wrong — unchanged from
  today's `next_failure_state()` contract).
* When **every** seat is parked: fall through to exactly today's park path — worker sleeps until
  the earliest seat deadline, queue intact, nothing quarantined. This is the property from
  decision 3 and must be identical to current behaviour.

### Budget interaction

A rotate-and-retry inside one job shares that job's `llm_timeout_s`, exactly as the claude
fallback does. Add `pr_reviewer_llm_seat_switch_min_s` (mirroring `llm_fallback_min_s`, default
60): if less than that remains, do not start a retry on the next seat — defer the job, still
without consuming an attempt. Without this, a refusal arriving near the deadline burns the
remainder on a run that cannot finish, which is the pathology `llm_fallback_min_s` already
exists to prevent on the claude branch.

### Metrics

```
reviewbot_llm_seat_parked{persona,seat}                     0|1
reviewbot_llm_seat_parked_seconds_remaining{persona,seat}
reviewbot_llm_active_seat_info{persona,seat}                1
reviewbot_llm_seats_total{persona}
reviewbot_llm_seats_distinct{persona}
reviewbot_llm_seats_available{persona}
reviewbot_llm_seat_switches_total{persona}
```

### Alerting — `ReviewbotRateLimited` must be re-scoped

This is a direct consequence of rotation and is easy to miss. `ReviewbotRateLimited` (added in
#742) fires on `increase(reviewbot_llm_rate_limited_total[1h]) >= 3`, and its threshold was
measured against **worker parks** — where a refusal meant the persona had stopped. With rotation
a refusal means "we moved to another seat and carried on", so the same expression would page on
a service that is working fine.

Split it:

* **`ReviewbotAllSeatsExhausted`** — `reviewbot_llm_seats_available == 0`, `for: 10m`. This is
  the real outage and inherits the old rule's severity and runbook.
* **`ReviewbotSeatExhausted`** — `reviewbot_llm_seat_parked == 1`, `for: 30m`, warning. One seat
  down, service continuing: a capacity signal, not an outage.
* **`ReviewbotSeatsDegraded`** — `reviewbot_llm_seats_distinct < reviewbot_llm_seats_total`, the
  duplicate-`account_id` guard above.

Both new thresholds must be re-measured against real series once seats b and c exist; do not
carry the `>= 3/h` number across, it does not mean the same thing any more.

### Credentials (ADR 0020 pattern)

Three OpenBao projections, one per seat, each rendered by the bao agent into its own user's
HOME with `refresh_token` stripped server-side; three AppRole role_id/secret_id pairs under
`reviewbot_openbao_credentials` in `ansible/secrets/reviewbot.sops.yaml`. Note
`openbao-agent` is currently **inactive** on reviewer-2 and its `auth.json` was hand-seeded —
the runbook says otherwise and is wrong; fix that as part of this work rather than inheriting it.

Each `codex login` is an interactive browser OAuth on the host, per seat. Never copy an
`auth.json` between hosts or seats: copies share one refresh-token family and rotation
invalidates every copy but the last to refresh — that is the 2026-09-10 outage.

## Rollout

* **Phase 1 — code, no behaviour change.** Land rotation with `llm_seats: []` everywhere. Both
  personas keep running exactly as today; the only risk surface is the refactor, which the tests
  below pin. Deploy and confirm a no-op.
* **Phase 2 — seats b and c.** Provision the two licences, create `codexrun2`/`codexrun3`,
  `codex login` each, project into OpenBao, add to `reviewer-2` host_vars, restart.

  **One seat is already provisioned (2026-09-16).** Use `codex login --device-auth` — the headless
  flow, which the CLI mentions only when a browser login fails and which `codex login --help` does
  not list at 0.153.4. It prints a URL and a one-time code, then polls. The resulting credential is
  parked at `/home/c4/.codex-seat3/.codex/auth.json` on reviewer-2 (0700/0600), deliberately NOT in
  `codexrun`'s HOME so the credential currently serving reviews was never touched; it moves to its
  own seat user as part of this phase. Verified live: `account_id`
  `9c8a8cfb-1147-4e67-8131-4f91174e5ee7`, **distinct** from the in-use
  `cfdea639-03a7-4690-9a8b-eaa4566d5063` — which is the precondition the uniqueness guard above
  exists to enforce — `auth_mode=chatgpt`, and a `codex exec -m gpt-6-astra` probe returned `OK`
  for 3,045 tokens.
* **Phase 3 — alerts.** Re-measure and land the three rules above against real per-seat series.
* **Rollback at any point:** empty `pr_reviewer_llm_seats` and restart. Single-seat behaviour is
  the same code path, not a separate one.

## Tests

Extending `scripts/tests/test_reviewbot.py`, mirroring the existing `RateLimitTest` class which
already pins the single-seat contract:

1. A refusal on seat A rotates to seat B and **consumes no attempt**.
2. All seats refused → the worker parks, queue intact, nothing quarantined, attempts unchanged.
3. Per-seat deadlines are independent; seat B stays usable while A is parked.
4. Two seats sharing an `account_id` collapse to one, and `seats_distinct < seats_total`.
5. A refusal with less than `llm_seat_switch_min_s` remaining defers instead of rotating.
6. Sticky: with all seats healthy, consecutive jobs stay on the same seat (no round-robin).
7. `llm_seats: []` reproduces today's single-seat behaviour exactly, including the park path.
8. **Seat park state does NOT survive a restart, deliberately.** It stays in memory, exactly as
   `RATE_LIMITED_UNTIL` does today: a restart forgets which seats were spent, re-probes the first
   one and re-parks it, costing one refused call per spent seat. That is cheap and self-correcting,
   and persisting it would add a durability problem for no gain. The test asserts the forgetting so
   the choice is recorded rather than accidental. (An earlier draft of this line said the opposite
   in its first clause — reviewer-claude, round 1.)

## Risks

* **Provider terms — RAISED, AND ACCEPTED BY THE OPERATOR.** This is not left open, so it should
  not be re-raised on every review of this file. OpenAI's Terms of Use prohibit "circumventing any
  rate limits or restrictions", and the Business terms prohibit "violate or circumvent Usage
  Limits **or otherwise configure the Services to avoid Usage Limits**". Owning three licences is
  not what that clause is about; the exposure is narrower and specific to this design — rotation
  *triggers on a usage-limit refusal and retries the same work on another account*, which is close
  to a literal description of the prohibited behaviour. That was put to the operator on 2026-09-16
  with the clause quoted, alongside the alternatives (no rotation; manual seat switch only), and
  the direction was to build it as planned. Recorded here as an accepted, operator-acknowledged
  risk rather than an unexamined one.
* **The invariant.** The refactor touches the one path that makes an exhaustion recoverable.
  Tests 1, 2 and 7 are the gate; a regression here converts a self-healing outage back into a
  quarantine storm.
* **Sticky concentrates load on seat A**, which will be the seat that exhausts. That is intended
  (it preserves stagger), but per-seat metrics are needed to tell it apart from a broken seat.
* **Headroom is 1–3 weeks, not permanent** — see the corrected arithmetic in "What changed": 2.5–3×
  capacity against a rate that did 2.4× in a single week buys very little time, and the two-point
  extrapolation it rests on is itself weak. Re-measure rather than trust it.
