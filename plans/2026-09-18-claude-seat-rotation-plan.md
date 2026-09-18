# 2026-09-18 — Claude seat rotation, model ladder and usage watchdog for reviewer-1

Design for letting the claude persona hold three Claude Max subscriptions and route around a
spent one, prefer Fable and climb back to it when it reopens, and show all of that on the AI Lab
Fleet dashboard. Operator-directed 2026-09-18; design approved in chat before this was written.

Sibling of `2026-09-16-codex-seat-rotation-plan.md`: the seat machinery it built is reused
unchanged, and every invariant it records still holds here.

## Why now

reviewer-1 sat idle for 21 h on 2026-09-18: its single account hit `You've hit your weekly limit ·
resets Sep 19, 8pm (UTC)`, the worker parked every 15 minutes with 9 jobs queued and the oldest
11.6 h old, and reviewer-2 accumulated 7 merge-blocked PRs waiting for the claude verdict. Nothing
paged, because every alert that could see it is sized for hours, not a 37-hour window.

Measured on the host the same morning, from `GET https://api.anthropic.com/api/oauth/usage` with
the account's OAuth token (the call `/usage` makes; costs no quota):

```
limits[]:
  session       percent 0    resets null
  weekly_all    percent 100  resets 2026-09-19T19:59:59Z  is_active true
  weekly_scoped percent 100  resets 2026-09-19T19:59:59Z  scope.model.display_name "Fable"
```

Two things this settles that `host_vars/reviewer-1.yml` currently gets wrong:

1. **Fable's limit is a weekly window, not a dead credit pool.** It has a reset time, exposed by
   the API. The "two accounts exhausted it and neither recovered" reading was a 7-day window
   observed for less than 7 days.
2. **The usage API is the ground truth reviewbot never had.** Today a park is a 15-minute guess
   (`DEFAULT_PARK_S`) re-probed by a doomed call, because `RESET_RE` cannot parse the weekly
   message. The API names the exact reopening for every scope, per account.

## Decisions

1. **Three seats, one Claude Max account each, all pooled for the reviewer.** Accounts are the
   agentforge broker accounts `claude-max-1/2/3` whose long-lived tokens already live in OpenBao
   (`af/operator/broker/anthropic/claude-max-N/oauth`, field `CLAUDE_CODE_OAUTH_TOKEN`). The
   reviewer shares those weekly windows with the AgentForge dev agents; the codex plan measured
   the reviewer at ~96 % of its own load and accepted the same sharing.
2. **Tokens are hand-seeded once, not ansible-managed and not bao-agent-rendered.** A
   `setup-token` credential does not rotate, so the refresh-token-family hazard that forced the
   codex bao-agent design (and caused the 2026-09-10 outage) does not apply. Per-seat AppRole
   plumbing buys nothing here. Same posture as codex seats `b`/`c`.
3. **Model ladder `fable → opus → sonnet`, tier-major.** Any seat that can serve `fable` beats
   the current seat on `opus`. Descend only when every seat is parked for the current tier; stay
   on the sticky seat when descending. Sonnet stays because the API exposes an Opus-scoped weekly
   cap (`seven_day_opus`): a seat can be out of Opus while its account window is fine.
4. **Upward moves happen only in the hourly watchdog; downward moves happen on refusal.**
   A refusal at minute 1 must not idle the persona for 59 minutes, and a climb back to Fable
   must not ping-pong on every review. Poll interval 3600 s, as specified.
5. **The lossless-park property is still the invariant.** An exhausted account or tier consumes no
   attempt, leaves the queue intact and quarantines nothing. Every test below defends it.
6. **The email goes into Prometheus, not git.** `reviewbot_llm_seat_info{email=…}` is read from
   `/api/oauth/profile` at runtime and lands in the .prom textfile, the TSDB and Grafana. The
   dashboard JSON references the label name only. (reviewer-2's `dsh_codex_publisher.email`
   already put an address in host_vars; this design does not add another.)
7. **Two PRs.** PR 1 restores the persona on other accounts with today's fallback mechanism; you
   merge it over the missing claude verdict. PR 2 adds the ladder, the watchdog and the
   dashboard through the normal two-persona lane.

## Facts the design rests on (verified 2026-09-18 unless marked)

* `claude` 2.1.197 on reviewer-1. `--model fable` resolves (refused with the weekly-limit text,
  same as `opus`); `--model claude-fable-5-1` needs ≥ 2.1.251 and 400s. A bogus model name
  yields `api_error_status 404 … issue with the selected model … may not exist`.
* The CLI has no `usage` subcommand. `/api/oauth/usage` and `/api/oauth/profile` answer a
  `Bearer` OAuth token with header `anthropic-beta: oauth-2025-04-20`. Profile carries
  `account.uuid`, `account.email`, `organization.rate_limit_tier` (`default_claude_max_20x`).
* The CLI reads `CLAUDE_CODE_OAUTH_TOKEN` from the environment (the brokers run on it).
* **RESOLVED 2026-09-18, against the design: the broker tokens do NOT answer the two endpoints.**
  All three `claude-max-N` `setup-token` credentials get `403 oauth_scope_insufficient`
  (`required_scopes: user:profile`) on `/usage` and `/profile`; their authorize URL requests
  `scope=user:inference` only. Inference works with them (the CLI answered `429`, not `401`).
  The rollout therefore re-credentialed every seat by **browser login** as its own OS user
  (`claude auth login --claudeai`, one flow per seat, each its own refresh-token family; helper
  `/home/c4/seat-login.sh`), retired the token files, and the probe now answers on all three
  seats. `claude-seat.sh`'s fall-through path (no token file → `.credentials.json`) is the live
  path. The identity collapse works as designed on these logins.
* **Browser-login access tokens expire (~7 h `expiresAt`) and only the CLI refreshes them**, when it
  runs as that seat. reviewbot re-probes a parked seat at most every `MAX_PARK_S`, which keeps
  the credential fresh; the poller must NEVER refresh (two refreshers in one family is the
  2026-09-10 outage) and treats a `401` as "stale token, keep the last-known windows".
* **The CLI's weekly refusal carries a DATE** — `You've hit your weekly limit · resets Sep 20,
  11pm (UTC)` — which `RESET_RE` does not parse, so every such park today is `DEFAULT_PARK_S`.
  PR 2 teaches `parse_reset` that form (belt and braces beside the API).
* All four accounts on the estate were at their weekly wall at once on 2026-09-18 (three
  seats + the old c4 login); the persona resumed only when seat `c` was moved to an account with
  headroom. Three windows are three of the size one reviewer proved it can empty in a week —
  capacity is the standing risk, not code.
* `resolve_seats()` returns early for anything but codex today; `_run_llm`'s credential scan is
  codex-only; the `~/.codex` and `config.toml` seat tasks are gated on `llm_kind == "codex"`; the
  sudoers task and the `NoNewPrivileges` gate are already generic.
* `write_metrics` iterates fixed-size dicts by design (a dict that grows can raise inside its
  blanket `except` and freeze the whole textfile). Every new table below is pre-seeded at import.
* `scripts/gen-reporting-dashboard.py` writes `reporting-dashboard.yaml`; the PR Reviewers row
  ends with the Loki panel at `y=159, h=9`.
* `bump_meta(f"seat_reviews_total.{seat}")` on the success path and
  `ReviewbotSeatNeverServes` landed on main 2026-09-17 — the claude seats inherit both.

---

## PR 1 — claude seats (`feat/claude-seat-rotation`)

Ends the outage: three accounts under the rotation that exists, Fable preferred through today's
`llm_model`/`llm_fallback_model` pair.

### Config

```yaml
# ansible/host_vars/reviewer-1.yml
pr_reviewer_llm_cmd: ["/usr/local/lib/reviewbot/claude-seat.sh"]
pr_reviewer_llm_model: fable            # floating alias — see the facts above
pr_reviewer_llm_fallback_model: opus
pr_reviewer_llm_seats:
  - { name: a, sudo_user: clauderun }   # claude-max-1
  - { name: b, sudo_user: clauderun2 }  # claude-max-2
  - { name: c, sudo_user: clauderun3 }  # claude-max-3
```

The file's long comment is rewritten: the "dead credit pool" narrative goes, the account-swap
history and the "ask the host, not the file" rule stay, and the usage-API measurement above is
recorded as the reason.

### `files/claude-seat.sh` → `/usr/local/lib/reviewbot/claude-seat.sh` (root, 0755)

```sh
#!/bin/sh
# reviewbot's claude entry point. The seat's long-lived OAuth token must reach the CLI through
# the ENVIRONMENT: sudo would show it on argv to every process on the host. No token file ->
# the ordinary ~/.claude/.credentials.json login, so the single-seat host is unchanged.
t="$HOME/.claude/oauth-token"
if [ -r "$t" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(cat "$t")"; export CLAUDE_CODE_OAUTH_TOKEN
fi
exec "${REVIEWBOT_CLAUDE_BIN:-/usr/bin/claude}" "$@"
```

`REVIEWBOT_CLAUDE_BIN` exists for the test. `wrap_sudo` is unchanged — it already prefixes
`sudo -n -u <user> HOME=<home>`, and `sudo` resets the environment, so the token can only come
from inside the seat's HOME.

### `files/claude-usage.py` → `/usr/local/lib/reviewbot/claude-usage.py` (root, 0755)

Stdlib-only. Runs **as the seat user** (`sudo -n -u <user> HOME=<home> …`), reads
`$HOME/.claude/oauth-token`, else `claudeAiOauth.accessToken` from `$HOME/.claude/.credentials.json`,
GETs `/api/oauth/profile` and `/api/oauth/usage` (15 s each), prints ONE JSON document and exits 0
even on failure (the failure is in the document):

```json
{"ok": true, "error": "",
 "account": {"uuid": "…", "email": "…", "plan": "default_claude_max_20x"},
 "limits": [{"kind": "weekly_all", "model": "", "percent": 100.0, "resets_at": 1789848000, "active": true},
            {"kind": "weekly_scoped", "model": "Fable", "percent": 100.0, "resets_at": 1789848000, "active": false}]}
```

`resets_at` is an epoch or `null`. Unknown `kind`s pass through untouched; the caller decides.
The service user never holds a seat's token for this — the same isolation argument `_run_llm`
makes. PR 1 uses only `account.uuid`; PR 2 uses the rest.

### reviewbot.py

* `resolve_seats()`: drop the `llm_kind != "codex"` early return. Identity per kind:
  codex → `tokens.account_id` from `auth.json` (unchanged); claude → `account.uuid` from
  `claude-usage.py` via sudo (30 s timeout). Unreadable/failed → keep the seat, identity unknown,
  exactly as codex does. The single-seat synthesised `default` seat still skips the scan
  (`len(SEATS) < 2`).
* Credential scan: factor the codex read into `seat_secrets(seat) -> list[str]`:
  codex → every string value in `auth.json`; claude → the token file's content, else
  `accessToken`/`refreshToken` from `.credentials.json`; empty list + unreadable →
  `llm_credscan_skipped_total` as today. Applied to the claude branch after the envelope is
  parsed, before return. `_run_llm` already binds the whole invocation to one seat; the scan
  follows it.
* No selection changes. `RateLimited` from the JSON envelope (`result` carries the weekly text)
  already parks and rotates through `run_llm`'s loop; `MODEL_LIMIT_RE` still takes the same-seat
  fallback. Characterised, not changed, so PR 2's diff is the ladder and nothing else.

### Ansible (`roles/pr_reviewer/tasks/main.yml`)

* New task "Ensure each seat's ~/.claude exists" — `0700`, owned by the seat user, gated
  `pr_reviewer_llm_kind == "claude"` and `item.sudo_user | length > 0`, tag `seats`. It holds the
  token AND the CLI's own `projects/`, `sessions/` writes.
* Install `claude-seat.sh` and `claude-usage.py` (`copy`, `0755`, root), tags `[seats,
  reviewbot]` — present before the first restart that could use them; no restart notify.
* `-t seats` therefore provisions users, homes and sudoers WITHOUT touching the service — the
  staging the codex runbook depends on.

### Docs

* `docs/runbooks/dev-workers.md` § seats: a reviewer-1 table (seat → user → account), the seeding
  recipe below, and the note that `~/.claude/projects` grows by one directory per review (c4's
  has 1 926; pre-existing behaviour, now per seat).
* `docs/decisions/0025-claude-seats-and-model-ladder-for-reviewer-1.md`: one page, status
  ACCEPTED, links here; records decisions 1–4 and the Fable-is-a-window measurement.
* `host_vars/reviewer-1.yml` comment rewrite (above).

### Tests (`scripts/tests/test_reviewbot.py`, stdlib unittest)

`ClaudeSeatTest`, mirroring `SeatRotationTest` / `IsolatedSeatUserTest` (argv-position dispatch):

1. a weekly-limit JSON envelope on seat `a` rotates to `b`, parks `a`, consumes no attempt;
2. `resolve_seats()` on `llm_kind=claude` collapses two seats whose `claude-usage.py` output
   carries the same `account.uuid`, drops one that cannot be sudo'd to, keeps one whose probe
   fails (identity unknown);
3. the credential scan `cat`s `<home>/.claude/oauth-token` as the SAME user that ran the model,
   refuses output containing it, and counts a skip when neither file is readable;
4. `MODEL_LIMIT_RE` text still takes the same-seat fallback (characterisation for PR 2);
5. `claude-seat.sh` exports the token and execs `REVIEWBOT_CLAUDE_BIN` (`skipUnless(bash)`; the
   Gitea runners are Linux).

All 214 existing tests stay green.

### Rollout (operator, in this order — the codex procedure)

1. Merge PR 1 (manual, over the missing claude verdict).
2. `ansible-playbook reviewers.yml -l reviewer-1 -t seats` — users, `~/.claude`, sudoers,
   wrapper. No restart.
3. Seed each token, from the workstation, never via a scratch copy on the host:
   ```
   bao kv get -field=CLAUDE_CODE_OAUTH_TOKEN af/operator/broker/anthropic/claude-max-1/oauth \
     | ssh c4@192.168.0.24 'sudo install -o clauderun -g clauderun -m 0600 /dev/stdin /home/clauderun/.claude/oauth-token'
   ```
   (repeat for `claude-max-2 → clauderun2`, `claude-max-3 → clauderun3`; `kubectl -n
   agentforge-broker get secret broker-anthropic-max1-oauth -o jsonpath=… | base64 -d` is the
   equivalent source).
4. `ansible-playbook reviewers.yml -l reviewer-1 -t reviewbot` (or the converge entry point) —
   installs reviewbot.py, re-renders config, restarts.
5. Verify: `journalctl -u reviewbot | grep "seats:"` shows `['a', 'b', 'c']`;
   `reviewbot_llm_seats_distinct{persona="claude"} 3`; a review lands (`last_success` moves); the
   7 merge-blocked PRs on reviewer-2 drain.

---

## PR 2 — model ladder, usage watchdog, dashboard

### Config

```yaml
# roles/pr_reviewer/defaults/main.yml
pr_reviewer_llm_models: []      # ordered tiers; [] = [llm_model, llm_fallback_model]
pr_reviewer_usage_poll_s: 0     # 0 = no watchdog (codex host)
# host_vars/reviewer-1.yml
pr_reviewer_llm_models: [fable, opus, sonnet]
pr_reviewer_usage_poll_s: 3600
```

`config.json.j2` gains `llm_models` and `usage_poll_s`. `llm_model`/`llm_fallback_model` stay
(codex uses `llm_model`; the empty-list synthesis keeps every existing test on the same
statements).

### reviewbot.py — state

```python
MODELS = CFG.get("llm_models") or [m for m in (CFG.get("llm_model"), CFG.get("llm_fallback_model")) if m] or [""]
MODEL_PARKED_UNTIL = {(s["name"], m): 0.0 for s in SEATS for m in MODELS}   # fixed-size, pre-seeded
CURRENT_MODEL = MODELS[0]
USAGE = {s["name"]: None for s in SEATS}                                    # last probe snapshot per seat
```

`_apply_seats()` prunes/extends `MODEL_PARKED_UNTIL` and `USAGE` the way it does
`SEAT_PARKED_UNTIL`. `RateLimited` gains `scope` (`"account"` | `"model"`) and `model`.

### reviewbot.py — selection

* `model_parked(seat, model, now)`; `park_model(seat, model, until)` — same clamp as `park()`
  (`+60 s … MAX_PARK_S`), bumps `model_parks_total.<seat>.<model>`.
* `seat_usable(seat, now)` = not `seat_parked` and some tier not `model_parked`.
  `seat_reopens_at(seat)` = `max(SEAT_PARKED_UNTIL[seat], min over tiers of MODEL_PARKED_UNTIL)`.
  `seats_available()` and `all_parked_until()` are rewritten on these two, so the worker gate,
  `RATE_LIMITED_UNTIL` and `ReviewbotAllSeatsExhausted` stay truthful with tiers.
* `active_choice(now, exclude=())` → `(seat, model)` or `None`: for each tier in order, the sticky
  `CURRENT_SEAT` first, then seat order; skip parked pairs and excluded pairs.
* `run_llm` loop: choose a pair; `_run_llm(…, seat, model)`; on `RateLimited`: park the seat
  (account scope) or the pair (model scope), add the pair to `tried`, pick the next choice under
  the existing `llm_seat_switch_min_s` floor; bump `llm_seat_switches_total` when the seat changes
  and `llm_model_switches_total` when the model does. Bound: `len(SEATS) × len(MODELS)` = 9
  refusals at ~0.5 s each. On success: `seat_reviews_total.<seat>` (exists) and, when
  `model != MODELS[0]`, `llm_fallback_used_total` — so `ReviewbotPrimaryModelDown` keeps its
  meaning ("served below the top tier").
* `_run_llm` claude branch: `args = claude_args(model)`; the in-function fallback retry is removed.
  On non-zero exit: `MODEL_LIMIT_RE` or `MODEL_404_RE`
  (`issue with the selected model|does not support this model`) → `RateLimited(scope="model")`
  (404 → no reset, so `MAX_PARK_S`); `RATE_LIMIT_RE` → `RateLimited(scope="account")`; anything
  else → `ModelError`. `run_llm` answers `ModelError` the way the old fallback did: bump
  `llm_primary_failed_total` if it was the top tier, then try the next tier on the SAME seat if
  one exists and the budget allows — no park; when every tier errored, re-raise as an ordinary
  failure. A transient 500 on `fable` therefore still costs one attempt at most, never a
  quarantine storm, and a persistently broken tier still surfaces through the attempt budget.
* The worker's `except RateLimited` and `next_failure_state` are unchanged: no attempt, queue
  intact.

### reviewbot.py — watchdog

`usage_ticker()` daemon thread, started in `main()` after `resolve_seats()`; returns immediately
when `usage_poll_s <= 0`. One synchronous `poll_usage()` runs BEFORE the worker thread so the first
job never walks into a known-spent seat; then every `usage_poll_s`.

`poll_usage()` for each seat: `probe_usage(seat)` → the JSON above (via sudo when the seat has a
user, directly otherwise; 30 s timeout; any exception → `{"ok": false}`); then

| payload | action |
|---|---|
| `kind ∈ {session, weekly_all}`, `percent ≥ 100` | `park(resets_at, seat)` |
| same, `percent < 100` and seat parked | `SEAT_PARKED_UNTIL[seat] = 0.0` |
| `kind == weekly_scoped`, `model` maps to a tier, `percent ≥ 100` | `park_model(seat, tier, resets_at)` |
| same, `percent < 100` | `MODEL_PARKED_UNTIL[(seat, tier)] = 0.0` |
| `ok == false` | no park change; snapshot records the failure |

Tier mapping: case-insensitive substring of the payload's model display name in the configured
tier (`Fable`→`fable`, `Opus`→`opus`/`claude-opus-5`, `Sonnet`→`sonnet`). Unmapped scoped limits
are kept in the snapshot for the metrics and change no park.

The API's word wins over a text-derived deadline in both directions; a refusal between polls
still parks for `DEFAULT_PARK_S` and the next poll corrects it. Parks stay clamped to
`MAX_PARK_S` and the poll re-extends them, so a dead poller can never leave a 7-day park behind.

Then `rebalance()`: `best = active_choice()`; if `best` sits on a HIGHER tier than
`(CURRENT_SEAT, CURRENT_MODEL)` → `use_seat`/`use_model`, log `climbing to '<model>' on seat
'<seat>'`, bump `llm_model_switches_total`. Same tier → no move (sticky).
`RATE_LIMITED_UNTIL = all_parked_until()` at the end of every poll.

### Metrics (all through `_label()`)

```
reviewbot_llm_seat_info{persona,seat,email,plan} 1                      # per seat with a known account
reviewbot_llm_active_model_info{persona,model} 1
reviewbot_llm_model_parked{persona,seat,model} 0|1
reviewbot_llm_model_parked_seconds_remaining{persona,seat,model}
reviewbot_llm_model_parks_total{persona,seat,model}
reviewbot_llm_model_switches_total{persona}
reviewbot_llm_usage_probe_ok{persona,seat} 0|1
reviewbot_llm_usage_probe_timestamp_seconds{persona,seat}
reviewbot_llm_usage_percent{persona,seat,limit}                         # limit: session | weekly_all | weekly_<model>
reviewbot_llm_usage_resets_at_seconds{persona,seat,limit}               # 0 when the API gives null
```

`limit` for a scoped entry is `weekly_` + lower-cased display name, so an unknown scope still
renders a stable, distinct series. `write_metrics` reads `USAGE` snapshots by assignment (no
in-place mutation) and iterates `SEATS`/`MODELS`, both fixed at import.

### Dashboard (`scripts/gen-reporting-dashboard.py`, PR Reviewers row; regenerate and commit both files)

Inserted after the two `ts` panels at `y=152`; the Loki panel moves from `y=159` to `y=171`.

| panel | type | expr / notes |
|---|---|---|
| Active Claude Account | stat, text mode name | `reviewbot_llm_active_seat_info{persona="claude"} * on(persona,seat) group_left(email,plan) reviewbot_llm_seat_info{persona="claude"}`, legend `{{email}} · seat {{seat}}` |
| Active Claude Model | stat, text mode name | `reviewbot_llm_active_model_info{persona="claude"}`, legend `{{model}}` |
| Claude Usage per Account | bargauge 0–100 % | `reviewbot_llm_usage_percent{persona="claude"} * on(persona,seat) group_left(email) reviewbot_llm_seat_info{persona="claude"}`, legend `{{email}} · {{limit}}`, thresholds green/orange 80/red 100 |
| Time to Reset | table, unit s (dtdurations) | `clamp_min(reviewbot_llm_usage_resets_at_seconds{persona="claude"} - time(), 0) * on(persona,seat) group_left(email) reviewbot_llm_seat_info{persona="claude"}` |
| Tier Parked per Seat | state-timeline | `reviewbot_llm_model_parked{persona="claude"}`, legend `{{seat}} / {{model}}` |
| Usage Probe | stat | `min(reviewbot_llm_usage_probe_ok{persona="claude"})`, 0 red |

A `state_timeline()` helper is added beside `bargauge()`; everything else uses helpers that exist.

### Alerts (`reviewbot-rules.yaml` + `reviewbot-rules.test.yaml`)

* `ReviewbotUsageProbeFailing`: `reviewbot_llm_usage_probe_ok == 0` `for: 3h`, warning. The
  silent failure a ~1-year token expiry would otherwise be. Fixture: fires on `0x200`, silent on
  `1x200` and on a 1-hour blip.
* `ReviewbotPrimaryModelDown`: expression unchanged; description rewritten — "reviews are being
  served below the top tier; check `reviewbot_llm_model_parked` and the usage panels; the
  watchdog climbs back on its own when the API shows the tier reopened".
* `ReviewbotAllSeatsExhausted` / `SeatExhausted` / `SeatsDegraded` / `SeatNeverServes`: untouched;
  they read persona-agnostic series and now cover claude too.

### Tests (PR 2)

`ModelLadderTest`: tier-major selection; a model-scoped refusal parks `(a, fable)` and moves to
`(b, fable)`, not to `opus`; an account-scoped refusal parks seat `a` for every tier; descent to
`opus` only once all three seats are parked for `fable`, and on the sticky seat; the 404-model text
parks the pair for `MAX_PARK_S`; a `ModelError` on the top tier descends without parking and bills
one attempt; `seats_available`/`all_parked_until`/`RATE_LIMITED_UNTIL` under the new semantics;
`exclude=tried` bounds the loop at 9.

`UsageWatchdogTest`: `probe_usage` mocked with recorded payloads — parks on 100 %, clears on
< 100 %, maps `Fable`→`fable`, ignores unknown scopes, leaves parks alone on `ok=false`, climbs
back from `(c, opus)` to `(b, fable)` and never moves within a tier; the pre-worker poll runs
once; `usage_poll_s: 0` starts nothing (the `SharedBudgetTest` no-subprocess canary stays
meaningful).

`MetricsTest` additions: the new series are unique per line, escaped, and present as zeros for
unparked pairs; `seat_info` is omitted for a seat with no account.

### Docs (PR 2)

Runbook: "how the ladder chooses", "what the watchdog does and does not do", how to read the new
panels — and the **credential model as it actually is**: `host_vars/reviewer-1.yml`'s comment and
the runbook § "Seats on reviewer-1" still describe OpenBao-seeded setup-tokens; both are rewritten
for browser logins per seat, with `/home/c4/seat-login.sh` as the documented re-login procedure
(start a detached flow, paste the code, verify with the probe) and the scope finding recorded.
ADR 0025 gets its "IMPLEMENTED" line and a note on the credential change.

---

## Rollout of PR 2

1. Normal lane: PR reviewed by both personas (reviewer-1 is reviewing again since 2026-09-18
   10:53 UTC), automerged, converged.
2. Verify: `journalctl -u reviewbot | grep -E "usage|climbing|tier"`, the six new panels
   populated, `reviewbot_llm_usage_probe_ok{seat=~"a|b|c"} 1` (the probe was already verified
   on every seat during PR 1's rollout).

## Risks

| risk | handling |
|---|---|
| ~~Broker tokens rejected by the usage/profile endpoints~~ | Happened (403, scope). Resolved on 2026-09-18 by moving every seat to a browser login; the setup-token path stays supported by the wrapper for a seat that needs it, without usage visibility. |
| Sharing weekly windows with the dev agents | Sticky selection staggers exhaustion; `ReviewbotSeatExhausted` names the seat; three windows ≈ 3× the runway that just proved insufficient for one. Capacity, not code, if it recurs. |
| `fable` alias stops resolving | 404-model park keeps the persona on `opus`; `ReviewbotPrimaryModelDown` says so. |
| A poller bug parks everything | Parks are clamped to `MAX_PARK_S`; the worker gate self-heals; the poll is `try/except` per seat so one bad seat cannot skip the others. |
| Email label cardinality | Three values, rewritten wholesale every 15 s; a swapped account replaces its series rather than adding one. |
| `~/.claude/projects` growth per seat | Pre-existing (c4 has 1 926 dirs). Noted in the runbook; a cleanup timer is a separate change. |
| The existing c4 login | Stays for `claude auth status`; out of the rotation. A distinct account could become seat `d` later — the wrapper already handles the `.credentials.json` path. |

## Out of scope

Per-seat bao-agent rendering; upgrading the CLI to use versioned Fable IDs; any change to the
codex persona; a `~/.claude/projects` cleanup timer.
