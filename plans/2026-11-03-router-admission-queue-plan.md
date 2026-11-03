# llm-router: a bounded per-account admission queue (wait for a slot instead of an immediate 429)

## Context

On 2026-11-01 two concurrent qwen-27b Codex agents on the dev workers got router 429s. The owner approved "Fix
cloud2's 429s" (plan first, codex-reviewed). Diagnosed on 2026-11-02 (memory `local-model-sse-idle-reasoning-invisible`);
the facts below were re-checked or measured on 2026-11-03 against llm-router `main` `93108f5` (live image
`sha256:7ec9726b…`, which includes #131 progress events and #132 reasoning forwarding).

**The router never queues.** `RouterEngine.run` (`packages/modules/inference/index.ts`) selects and reserves inside one
exclusive section (`:448-540`). When every candidate of every route in the chain is refused only for being busy
(`BUSY` = `concurrency_exhausted`, `duplicate_account`, `already_attempted`, at least one `concurrency_exhausted`,
`:535`), it throws 429 `CAPACITY_UNAVAILABLE` with `data: { reason: 'busy', capacity }` and `retryAt = now + 5 s`
(`BUSY_RETRY_MS`, `:23-24`, `:537`), in about 1 ms. The HTTP error handler turns `retryAt` into `Retry-After` and sets
`x-should-retry: true` (`packages/kernel/http.ts:477-482`). Slots are counted by `Admission`
(`packages/modules/admission/index.ts`), one count per account pool (`scope(account)`), limit = the smallest
`concurrency` of the pool's enabled accounts.

**cloud2.** Account `lan-192.168.0.28-18020` (vLLM 0.28 at `192.168.0.28:18020`, model `qwen3.8-27b`, TP=2 on two
RTX 3090s) has `concurrency: 4` and `forwardReasoning: true` (live config revision 2215). Routes: `qwen3.8-27b`
(priority, one target, `maxAttempts` 1, no fallbacks, `timeoutMs` 300000, `idleTimeoutMs` 120000, no
`maxDurationMs`: owner decision D1) and the pointer `qwen-27b` → it. The dev workers reach it as
`https://router.chifor.me/v1`, which resolves to the LAN edge VIP `192.168.0.51` (checked on dev-worker-2), so their
path has no proxy response-header timeout (`kubernetes/apps/apps/edge/proxy/Caddyfile`, `kubernetes/apps/apps/llm-router/edge.yaml`:
"no response/read timeouts"). Cloudflare's tunnel path waits at most 100 s for a response head.

**How the Codex CLI treats the router's busy 429 (measured 2026-11-03, docker, the real CLI against the in-process
router at concurrency 1 with a 240 s request holding the slot, the workers' `stream_max_retries = 8` and
`request_max_retries = 6`):** Codex 0.162.1 (the workers' version) retries **exactly at `Retry-After`** (5.0 s apart),
counts each 429 as a **stream** retry ("Reconnecting... n/8"; `request_max_retries` does not apply), sends 9 POSTs and
exits 1 with `exceeded retry limit, last status: 429 Too Many Requests`. Codex 0.166.0 does exactly the same. **Its
whole patience for a busy route is 8 × 5 s = 40 s.**

**Codex waits for a response head (measured the same way, a proxy holding each request before the router sees it):**
0.162.1 and 0.166.0 waited 45 s with `stream_idle_timeout_ms = 20000` and completed; 0.162.1 waited 330 s with the
default 300000 and completed. The idle timer starts with the stream, not with the request.

**What a queue would have had to absorb (the live journal, read-only, 8 days to 2026-11-03):**
- 76 busy 429s on cloud2: 3 on 10-29 and 6 on 10-30 (the `strive-platform-eval` key), 10 on 11-01 06:09Z (the same
  key), 57 in the 11-01 17:28-17:33Z incident (dev-worker-2's key and another dev-worker key, every 5 s: Codex's
  cadence). The eval key's daily 33 refusals at 06:00-06:03Z are `offline` (cloud2 is off at night), not busy: a queue
  does not touch them.
- For each busy 429, the time until a cloud2 slot actually freed (the earliest end among the requests then running;
  all 4 slots were held every time): **p50 20 s, p90 45.7 s, max 75.9 s**. Every one of them would have been served by
  the first free slot within 76 s; Codex gave up after 40 s.
- cloud2 request durations (n = 1430): p50 5.9 s, p90 48 s, p99 300 s, max 330 s.

**cloud2's real capacity (ailab Prometheus, 2-minute buckets, 2026-10-19 → 11-03, the TP=2 era):**
- With 4 requests running, KV-cache use p95 0.76, max 0.82; zero preemptions in 15 days; requests waiting inside
  vLLM in 5 of those buckets. The pool is 407,266 tokens (`vllm:cache_config_info`, 1.55× at 262144);
  `--max-num-seqs 8`, `--max-num-batched-tokens 2048` (cloudlab runbook §12).
- So at today's agent context sizes (~83k tokens per running sequence at the observed max), 4 running sequences already
  fill ~80 % of the pool. A 5th would push it past 100 %: vLLM then queues it (`waiting`, reason `capacity`) or preempts
  a running sequence (recompute of a long prefix).

**Orphaned legs.** In the 11-01 incident a Renovate roll of the `edge-proxy` DaemonSet cut the clients' streams at
~17:28:05; Codex resent at once while the old proxy kept the router connections (and their slots) until it exited
(Caddy aborts at 17:29:40). The router releases a slot promptly when it sees a disconnect (the request signal aborts,
`Watchdog.cancel` abandons the read, the upstream close is bounded by `closeGrace` 10 s, then `reservation.release()`
in the attempt's `finally`, `:552-553`, `:648-666`, `:723`); it cannot see a client that left behind a proxy that keeps
the router connection open and reading.

## Approach

A **bounded, FIFO, per-account-pool admission queue**, opt-in per account. A request that today gets the busy 429
waits instead, up to the account's `maxWaitMs`, for a slot of a pool that refused it; it is admitted in arrival order
when a slot frees, and it leaves the queue at once when its client goes, the router shuts down, or its whole-request
cap passes. A full queue or an expired wait still answers the busy 429. No account changes behaviour unless it sets
`queue`; with no `queue` anywhere the router is byte-identical to today.

Why this and not the alternatives (all four directions were evaluated):
- **Raising `concurrency` alone** (direction 2): rejected for cloud2 on the KV evidence above; extra admissions would
  queue inside vLLM, where a waiting request sends no bytes, so the router's 300 s wait timer and Codex's 300 s idle
  timer both run on a request making no progress (#131's progress events fire only on upstream bytes), and a
  preemption recomputes a long prefix. The router-side queue is the better place to wait: ordered, cancellable,
  visible, and before any timer starts.
- **A longer `Retry-After` alone** (direction 4): Codex honours it exactly, so `Retry-After: 15` would stretch its
  patience to 120 s with no code beyond a constant. Rejected as the fix: a freed slot then sits idle until some
  client's next retry (up to 15 s), there is no order (the luckiest retry wins, the agent that waited longest can lose
  every time), and every SDK client of every route would wait longer. The 429 shape is already what Codex retries
  (`type: router_error`, `x-should-retry: true`, `Retry-After`); the queue only changes the `Retry-After` of its own
  two outcomes (point 4).
- **Releasing orphaned legs** (direction 3): no change. The router already releases on every disconnect it can see
  (above). A leg the edge keeps open is invisible to it; "supersede an identical resend of the same conversation"
  would release it but is a heuristic with false positives (a client that deliberately sends the same request twice),
  and the edge's grace for streams is an ailab edge setting. The queue absorbs the window instead: the resend waits for
  the orphan's slot (~95 s on 11-01) rather than failing. Both are listed as follow-ups.

### 1. Configuration: `Account.queue`

`queue?: { maxWaitMs: number; maxWaiting?: number }` on `Account` (`packages/contracts/index.ts`), validated in
`packages/runtime/config.ts` as a strict object: `maxWaitMs` an integer 1000-600000, `maxWaiting` an integer 1-100,
default 8. Set with `PUT /admin/v1/config` like `forwardReasoning` (no Assistant tool, no UI). Accounts sharing a pool
share one queue (as they share one limit): the pool queues only if every enabled account of the pool sets `queue`, and
then with the smallest `maxWaitMs` and the smallest `maxWaiting` among them (the "smallest wins" rule `poolLimit`
already applies to `concurrency`). A rollback image ignores the field and drops it on its next validated write (the
account schema is not strict), so it must be removed explicitly before a rollback if it is to stay off (point 9).

Contract change (`packages/contracts/index.ts` is a contract file, AGENTS.md "ask first"): additive and optional; the
owner asked for this fix on 2026-11-02; the PR names it. cchifor/platform vendors the module and compiles against the
type; an optional field does not break it.

### 2. `Admission` keeps the waiters (the single authority for slots)

`packages/modules/admission/index.ts` gains a FIFO of tickets per pool. Everything stays synchronous except `wait`.
- `join(entries: Array<{ pool; limit; maxWaiting }>): Ticket | undefined` registers one ticket (a global, increasing
  sequence number) in every listed pool whose list is shorter than its `maxWaiting`, remembering that pool's `limit`
  (from the request's configuration) on the ticket; `undefined` when every list is full.
- `ahead(pool, ticket?)`: the tickets registered on `pool` before `ticket`; without a ticket, all of them.
- `reserve(pool, limit, ticket?)` refuses when `used(pool) + ahead(pool, ticket) >= limit`. So a request that is not
  waiting never takes a slot that a waiter is owed (no barging), and the head waiter sees exactly the free slots.
  The existing two-argument call (provider-claude's usage probe, `packages/plugins/provider-claude/index.ts:228`)
  counts every waiter, so a probe never jumps the queue either. Without waiters `ahead` is 0: unchanged.
- `wait(ticket, signals, ms): Promise<'woken' | 'timeout' | 'aborted'>`: settles on a wake, after `ms`, or when any of
  `signals` aborts; every path removes its timer and listeners.
- `leave(ticket)` removes the ticket from every list; idempotent.
- **Wake-ups.** A release and a `leave` bump a version number and "pump" the affected pools: walking a pool's
  tickets in order with `i` tickets ahead, it wakes a waiting ticket when `used + i < ticket.limit(pool)` and the
  version changed since that ticket was last woken. `wait` pumps the ticket's pools when it starts, so a slot freed
  between the busy decision and the wait is not missed. The version check means a woken ticket whose selection still
  failed (a slot taken by something outside the queue, or a limit that differs from its own) is woken again only after
  something changes: no tight loop.

### 3. The engine waits on the busy answer, outside the exclusive section

In `RouterEngine.run`, the selection-and-reservation step becomes a loop around the existing exclusive section:
1. The section is unchanged except that its snapshot and its reservation use the request's ticket:
   `evaluate`'s `inFlight` is `used + ahead(pool, ticket)` (`snapshot()`, `:251`, also for `locate()`'s snapshots,
   so a placement sees the same view), and `reserve(key, limit, ticket)` (`:483`). At the busy branch (`:535`) it also
   records which accounts refused for `concurrency_exhausted`. On success it calls `leave(ticket)` before returning, so
   the waiter never counts twice outside the section.
2. On the busy answer, the engine maps those accounts to their pools and keeps the pools that queue (point 1, from the
   request's configuration). None: today's 429 (or the last attempt's failure, as today, `:540`).
3. First time: `join`. Full: 429 (point 4). Otherwise journal `admission.queued` `{ accounts, ahead, maxWaitMs }` and
   fix the deadline: now + the largest `maxWaitMs` of the pools joined. The deadline is per wait phase: a later
   failover wait (after an attempt ran) joins again with a fresh ticket and deadline.
4. `wait` on the request signal (client close, `shutdown()`), the consumer's `gone` signal, and a new per-run
   controller that the `maxDurationMs` timer aborts (`:424`; today it only trips the running attempt's watchdog,
   which does not exist while waiting).
5. After the wait: aborted → `signal.throwIfAborted()` (CANCELLED, cause CLIENT_CLOSED or SHUTDOWN, as today), or
   `return` when the consumer returned (as the attempt loop does, `:616`); cap passed → `timeoutFault('max_duration')`;
   timeout → 429 (point 4); woken → run the whole section again (all routes of the chain, placement included).
6. A `finally` leaves the queue on every path and journals `admission.dequeued` `{ accounts, waitedMs, outcome }`
   (`admitted` | `timeout` | `cancelled`) once per wait phase, before `admission.reserved` when admitted.

Waiting consumes no attempt (`maxAttempts` counts upstream attempts only). The route's wait/idle timers, the
`Watchdog`, `attempt.started` and the live view all start at admission, as today. `requests` counts a waiter, so the
drain sees it.

### 4. The 429s the queue still gives

Both keep today's code, status and shape (`CAPACITY_UNAVAILABLE`, `reason: 'busy'`, `capacity`, `x-should-retry: true`)
and add `queue`:
- **Queue full** (every queueing pool at `maxWaiting`): `queue: { outcome: 'full', maxWaiting }`, the message adds
  "and N requests are already waiting", `Retry-After: 5` (unchanged), journal `admission.queue_full`.
- **Wait expired**: `queue: { outcome: 'timeout', waitedMs }`, the message says how long it waited,
  `Retry-After: 1`: the client already waited `maxWaitMs`, so its retry should rejoin at once. With Codex this makes
  its patience about (8 + 1) × `maxWaitMs` instead of 40 s.
- The trajectory is `deferred` with `errorCode` `CAPACITY_UNAVAILABLE` and `errorCause` `QUEUE_FULL` or
  `QUEUE_TIMEOUT` (the busy 429 without a queue keeps no cause, as today). When an earlier attempt of the same request
  failed retryably, that failure is still the answer (`lastFault`, `:540`), the queue outcome stays in the journal.

### 5. No transport change

The HTTP handler sends a stream's head when the engine has *selected* the first attempt
(`packages/plugins/api-openai/index.ts:446-453`), so a queued request has no head, no `: ok` and no keepalives until it
is admitted, then streams exactly as today (progress events, reasoning, terminal events). Measured: Codex waits for the
head (330 s and counting) and its idle timer starts with the stream; the LAN path has no head timeout. A client behind
Cloudflare gets a 524 after 100 s without a head: the README says to keep `maxWaitMs` under 100 s for accounts such
clients use. A client that closes while queued aborts the request signal (`reply.raw` `close`, `:393`), which ends the
wait at once.

### 6. Interactions (each one covered by a test, point "Verification")

- **Watchdog wait timer:** not running while queued (it is per attempt, created after reservation, `:548`).
- **Progress events / Codex's 300 s idle timer:** no stream exists while queued; after admission, as today.
- **Failover:** the queue is entered only when every candidate of every route in the chain is busy, so it never holds
  a request that a free target could serve; each wake re-evaluates the whole chain (a fallback that freed first takes
  it). Waiting on several pools is one ticket in each.
- **Conversation placement:** each selection re-locates; a bound account that frees is preferred as today.
- **Strict affinity:** unchanged (`AFFINITY_UNAVAILABLE`; the busy branch already excludes it).
- **Drain/SIGTERM:** a waiter is an active request: the drain waits for it up to `drainMs`, then `shutdown()` cancels it
  (SHUTDOWN); having no head, its connection is reset (`api-openai` `:566`) and Codex's retry reaches the next process
  through the edge, as for any request ended before its stream. A waiter admitted during the drain runs and is cut at
  the deadline like any request started then.
- **Whole-request cap:** the wait counts toward `maxDurationMs`, the image deadline and `imageProxy`'s cap; the cap ends
  a queued request with 504 `TIMEOUT` phase `max_duration`.
- **Memory:** a waiter holds its parsed request (subset bodies ≤ 2 MiB; native bodies up to `codexNativeMaxBodyMiB`,
  16 MiB) plus a timer and a ticket; bounded by `maxWaiting` per pool (≤ 100). Tickets leave on every path (tested).
- **Configuration changes while waiting:** a waiter keeps the limits of the configuration it started with, like every
  request; new requests use the new ones. A raise reaches the waiters as they are woken or time out (≤ `maxWaitMs`).
- **Journal size:** a waiter adds two events plus one `routing.evaluated` per wake; wakes are targeted (version check).

### 7. Observability

- Journal: `admission.queued`, `admission.dequeued`, `admission.queue_full` (above).
- Metrics (`packages/plugins/metrics/index.ts`, fed by the journal like the others): histogram
  `llm_router_admission_wait_seconds{account, provider, outcome}` (outcome `admitted`, `timeout`, `cancelled`,
  `full` with 0 s), one observation per account waited on.

### 8. Docs

README: the busy 429 paragraph (it says "the router does not queue"), the account fields, streaming ("a queued request
gets its head when admitted"), the journal events and the metric; `docs/implementation-status.md`.

### 9. Release and config (the main session does these; this change stops before any of them)

- Image build and roll as usual (llm-router release staging).
- **Proposal Q1:** after the roll, `queue: { maxWaitMs: 90000, maxWaiting: 8 }` on `lan-192.168.0.28-18020` through
  the admin API (`If-Match`, read back, revision recorded). 90 s covers the slowest slot release the journal shows for
  a busy 429 (75.9 s) and stays under Cloudflare's 100 s for any client on that path; Codex's patience becomes about
  13-14 minutes. 8 waiters is twice the slots (4 dev workers; the incident had at most 2 clients waiting at once).
- **Concurrency stays 4** (KV evidence above). Revisit after a week of `llm_router_admission_wait_seconds`: long
  admitted waits with KV p95 < 0.7 at 4 running would justify trying 5.
- **Rollback:** first remove `queue` from the account (`If-Match`, read back), then the image.

## Out of scope

- Queueing on other refusals (cooldown, quota, offline, strict affinity); a per-request or per-key opt-out header
  (a follow-up if a client with its own fallback, like the platform eval, prefers the instant 429).
- Releasing orphaned legs (supersede on resend; the edge's grace for streams): follow-ups.
- Keeping a client's queue position across its retries; priority classes; a queue-depth gauge or Status/Assistant view.
- Changing `BUSY_RETRY_MS` for accounts without a queue.

## Critical files

llm-router (branch `feat/admission-queue` off `main` `93108f5`):
- `packages/modules/admission/index.ts` - tickets, `ahead`, `reserve(..., ticket)`, `wait`, `leave`, pumping.
- `packages/modules/inference/index.ts` - the wait loop around the exclusive section, the ticket in `snapshot`/`locate`/
  `reserve`, the queue's 429s and journal events, the cap's abort of a wait.
- `packages/contracts/index.ts`, `packages/runtime/config.ts` - `Account.queue` and its schema.
- `packages/plugins/metrics/index.ts` - the wait histogram.
- Tests: `tests/admission-queue.test.ts` (new: HTTP and engine), unit tests next to `Admission`, the config schema and
  metrics tests. Docs: `README.md`, `docs/implementation-status.md`.

## Verification

1. **Tests first** (failing before the change where they describe new behaviour):
   - `Admission` unit: FIFO wake order; no barging (a non-ticket `reserve` refused while waiters are owed the slot,
     the head ticket admitted); `maxWaiting`; `wait` settles on wake, timeout and abort with no timer or listener left;
     `leave` idempotent and pumping; the version check (a woken ticket that fails is not re-woken until something
     changes); a release with no waiters is today's behaviour.
   - HTTP (holding upstream, concurrency 1, `queue` set): B waits while A runs and gets 200 when A ends, its journal
     `routing.evaluated` (busy) → `admission.queued` → `admission.dequeued` (admitted, `waitedMs` > 0) →
     `admission.reserved`; FIFO across B, C and a late D; no `queue` configured → the 429 is byte-identical to today;
     wait expired → 429, `Retry-After: 1`, `queue.outcome: timeout`, `deferred`/`QUEUE_TIMEOUT`; queue full → 429,
     `Retry-After: 5`, `queue.outcome: full`; a client that disconnects while queued (a real socket) leaves the queue
     and is journaled CLIENT_CLOSED, and the next waiter moves up; `shutdown()` while queued resets the connection and
     journals SHUTDOWN; a route `maxDurationMs` shorter than the wait → 504 `max_duration`; a Responses stream gets no
     head until admitted, then `: ok` and `response.created`; failover: a free second target or fallback route serves
     at once while the first is busy and queueing; two queueing pools, a release on either admits; a failed attempt
     then a full queue answers the attempt's failure; the consumer's `return()` while queued ends the run and frees
     the ticket; accounts sharing a pool queue by the smallest settings.
   - Config: bounds, strictness, the default `maxWaiting`. Metrics: the histogram from the journal events.
   - **Mutation checks** on the key guards (each must fail a test): no anti-barging (`ahead` 0 for non-tickets); LIFO
     pumping; the wait ignoring the request signal; the deadline ignored; `leave` skipped on timeout; the version
     check removed; `maxWaiting` ignored.
2. **CI gates in docker** (node:24, pnpm 10.32.1, `pnpm install --frozen-lockfile`): typecheck, test, build,
   build:lib, test:production, check:architecture (only the known #79 line); the known load-flaky tests
   (`subscription-login`, `system-messages` failover, `image-memory`, llm-router #129) pass when run alone.
3. **The real Codex CLI end to end** (0.162.1 and 0.166.0, docker, the in-process router at `concurrency: 1` with
   `queue: { maxWaitMs: 120000 }`, a fake vLLM-style upstream that thinks 60 s, the workers' retry settings): two
   agents started together both exit 0, the second's upstream request starts when the first's ends, no 429 is sent.
   Baseline on `main`: the second agent exits 1 after 9 × 429 (40 s).

<!-- codex-review-status: pending -->
