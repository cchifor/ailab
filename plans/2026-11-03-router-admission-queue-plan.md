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
(`packages/modules/admission/index.ts`), one count per account pool (`scope(account)`: the `quotaPool`, else namespace,
provider and identity, `packages/modules/accounts/index.ts:22`), limit = the smallest `concurrency` of the pool's
enabled accounts.

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

**What a queue would have faced (the live journal, read-only, 8 days to 2026-11-03):**
- 76 busy 429s on cloud2: 3 on 10-29 and 6 on 10-30 (the `strive-platform-eval` key), 10 on 11-01 06:09Z (the same
  key), 57 in the 11-01 17:28-17:33Z incident (dev-worker-2's key and another dev-worker key, every 5 s: Codex's
  cadence, so many of the 76 are retries of the same work). The eval key's daily 33 refusals at 06:00-06:03Z are
  `offline` (cloud2 is off at night), not busy: a queue does not touch them.
- For each busy 429, the time until a cloud2 slot next freed (the earliest end among the requests then running; all 4
  slots were held every time): p50 20 s, p90 45.7 s, max 75.9 s. This is a lower bound on what each request would have
  waited, not a FIFO residence time: two requests waiting at once cannot both take the first free slot. It says the
  head of a queue would have been served within ~76 s every time, where Codex gave up after 40 s.
- cloud2 request durations (n = 1430): p50 5.9 s, p90 48 s, p99 300 s, max 330 s.

**cloud2's real capacity (ailab Prometheus, 2-minute buckets, 2026-10-19 → 11-03, the TP=2 era):**
- With 4 requests running, KV-cache use p95 0.76, max 0.82; zero preemptions in 15 days; requests waiting inside
  vLLM in 5 of those buckets. The pool is 407,266 tokens (`vllm:cache_config_info`, 1.55× at 262144);
  `--max-num-seqs 8`, `--max-num-batched-tokens 2048` (cloudlab runbook §12).
- Scaling linearly (no shared prefixes, equal sizes), a 5th sequence would bring the pool to ~0.95 (from the p95) to
  ~1.03 (from the max): close to full, where vLLM queues new sequences or preempts running ones. That is a reason for
  caution, not proof that every 5th request would queue or preempt; only a controlled load test would settle it.

**Orphaned legs.** In the 11-01 incident a Renovate roll of the `edge-proxy` DaemonSet cut the clients' streams at
~17:28:05; Codex resent at once while the old proxy kept the router connections (and their slots) until it exited
(Caddy aborts at 17:29:40). When the router sees a disconnect, the request signal aborts, `Watchdog.cancel` abandons
the read and the upstream close is bounded by `closeGrace` (10 s), then the failure is journaled (awaited, `:702`;
strict capture can wait on storage) and `reservation.release()` runs in the attempt's `finally` (`:723`). Normally that
is milliseconds; it is not a hard deadline. The router cannot see a client that left behind a proxy that keeps the
router connection open and reading.

## Approach

A **bounded, first-come-first-served admission queue for one account pool**, opt-in per account, on a request's
**initial admission only**. A request that today gets the busy 429 before any attempt was selected, and whose busy
targets are all in one pool that queues, waits instead, up to the pool's `maxWaitMs`, for a slot of that pool; it is
admitted in registration order when a slot frees, and leaves the queue at once when its client goes, the router shuts
down, or its whole-request cap passes. A full queue or an expired wait still answers the busy 429. Everything else is
unchanged: with no `queue` anywhere, or when the busy targets span two pools, or on a failover attempt, the router
answers exactly as today.

Why this narrow scope: cloud2 needs one pool and one initial admission. Waiting on several pools at once (partial
registration, per-pool deadlines, priority on pools a ticket is not in) and waiting during a failover (the stream head
is already out, so the wait would run under the client's stream idle timer and end in-stream, without `Retry-After`)
each need their own rules; both are follow-ups, not part of this change.

Why this and not the alternatives (all four directions were evaluated):
- **Raising `concurrency` alone** (direction 2): not recommended for cloud2 on the KV evidence above. Extra admissions
  would queue inside vLLM, where a waiting request sends no bytes, so the router's 300 s wait timer and Codex's 300 s
  idle timer both run on a request making no progress (#131's progress events fire only on upstream bytes), and a
  preemption recomputes a long prefix. The router-side queue is the better place to wait: ordered, cancellable,
  visible, and before any per-attempt timer starts.
- **A longer `Retry-After` alone** (direction 4): Codex honours it exactly, so `Retry-After: 15` would stretch its
  patience to 120 s with no code beyond a constant. Not the fix: a freed slot then sits idle until some client's next
  retry (up to 15 s), there is no order (the luckiest retry wins, the agent that waited longest can lose every time),
  and every SDK client of every route would wait longer. The 429 shape is already what Codex retries
  (`type: router_error`, `x-should-retry: true`, `Retry-After`); the queue only sets the `Retry-After` of its own two
  outcomes (point 4).
- **Releasing orphaned legs** (direction 3): no change. The router already releases on every disconnect it can see. A
  leg the edge keeps open is invisible to it; "supersede an identical resend of the same conversation" would release
  it but is a heuristic with false positives (a client that deliberately sends the same request twice), and the edge's
  grace for streams is an ailab edge setting. With a queue, a resend waits for the orphan's slot instead of failing at
  once; whether a given resend outlasts a ~95 s orphan within `maxWaitMs` depends on the settings (an 80 s wait does
  not cover a 95 s orphan by itself; Codex's next retry rejoins after 1 s). Both changes are follow-ups.

### 1. Configuration: `Account.queue`

`queue?: { maxWaitMs: number; maxWaiting?: number }` on `Account` (`packages/contracts/index.ts`), validated in
`packages/runtime/config.ts` as a strict object: `maxWaitMs` an integer 1000-600000, `maxWaiting` an integer 1-100,
default 4 (8 in #133; lowered in llm-router #134 after review, for the body budget in point 6). Set with `PUT /admin/v1/config` like `forwardReasoning` (no Assistant tool, no UI).
- **Scope:** the setting belongs to the account's pool, so it applies to every route, direct-model request, protocol
  and caller that reaches the pool (the Assistant, the platform eval key with its own client-side fallback, chat
  clients), not only Codex. That is deliberate: the slots are the pool's.
- **Shared pools:** accounts that share a pool (same `quotaPool`, or same namespace, provider and identity; never the
  endpoint URL) share one queue, as they share one limit: the pool queues only if every enabled account of the pool
  sets `queue` (an enabled member outside the requested route counts too), with the smallest `maxWaitMs` and the
  smallest `maxWaiting` among them (the "smallest wins" rule `poolLimit` applies to `concurrency`).
- **Rollback:** an older image ignores the field and drops it on its next validated write (the account schema is not
  strict), so it must be removed explicitly before a rollback (point 9).

Contract change (`packages/contracts/index.ts` is a contract file, AGENTS.md "ask first"): additive and optional; the
owner asked for this fix on 2026-11-02; the PR names it. cchifor/platform vendors the module and compiles against the
type; an optional field does not break it.

### 2. `Admission` keeps the waiters (the single authority for slots)

`packages/modules/admission/index.ts` gains a line of waiters per pool. Everything stays synchronous except `wait`;
nothing in it calls back into routing.
- `join(pool, limit, maxWaiting): Ticket | undefined` registers a ticket at the end of the pool's line (registration
  order is the queue's order), remembering `limit` (from the request's configuration); `undefined` when the line holds
  `maxWaiting` tickets.
- `ahead(pool, ticket?)`: the tickets in `pool`'s line before `ticket` when `ticket` is in that line; otherwise (no
  ticket, or a ticket of another pool) all of them.
- `reserve(pool, limit, ticket?)` refuses when `used(pool) + ahead(pool, ticket) >= limit`. So a request that is not
  waiting in that pool never takes a slot that its waiters are owed (no barging), and the head waiter sees exactly the
  free slots. The existing two-argument call (provider-claude's usage probe, `packages/plugins/provider-claude/index.ts:228`,
  which passes `account.concurrency`) counts every waiter, so a probe never jumps a queue either. Without waiters
  `ahead` is 0: unchanged.
- **What the order guarantees.** Freed slots go to the waiters at the front of the line, as many as slots freed, before
  any newcomer and before any waiter further back (capacity protection in line order). Among the waiters let through
  together, whichever completes its selection first reserves first (a head whose journal write is slow can be
  overtaken by the second waiter when two slots free at once; both are admitted). Tested with the head's journal write
  held.
- `wait(ticket, signals, ms): Promise<'woken' | 'timeout' | 'aborted'>` settles on a wake, after `ms` (`ms <= 0`
  settles as a timeout unless a slot is free at that moment), or when any signal aborts (an already-aborted signal
  settles at once). Whatever settles first wins; every path removes its timer and listeners and drops its resolver.
- `leave(ticket)` removes the ticket from its line (an empty line is deleted), settles a pending wait as `aborted`,
  then wakes whoever may now pass; idempotent.
- **Wake-ups.** A release and a `leave` bump one global version number and pump the pool: walking its line with `i`
  tickets ahead, it wakes a ticket whose wait is pending when `used + i < limit` (the ticket's own limit) and the
  version changed since that ticket was last woken. A version is consumed only by a pending wait it settled. `wait`
  pumps when it starts, so a slot freed between the busy decision and the wait (or while the ticket was re-selecting)
  is seen. A woken ticket whose selection still failed (its limit differs from a newer configuration's, or something
  outside the queue took the slot) is woken again only after something changes: no tight loop.
- **Limits and configuration changes.** Every request, waiting or not, uses the configuration it started with, as
  today. So FIFO holds among requests that share the pool's limit; after a `concurrency` change, a newcomer under the
  new limit may pass older waiters under the old one, and a raise does not wake old waiters by itself (the next
  release or leave does). A queue switched off drains: its waiters are still owed slots and are served or time out
  within their `maxWaitMs`, while new requests get the busy 429. A changed pool identity is a different line. All of
  this is bounded by `maxWaitMs`.

### 3. The engine waits on the busy answer of an initial admission, outside the exclusive section

In `RouterEngine.run`, the first attempt's selection-and-reservation step becomes a loop around the existing exclusive
section (`reserveOrWait`); later attempts (failover) call the section once, exactly as today.
1. The section uses the request's ticket: `evaluate` (`packages/modules/routing/index.ts`) gains an optional `owed`
   callback, the waiters ahead of this request in the account's pool (`ahead(pool, ticket)`), which counts toward
   eligibility only (`inflight + owed >= concurrency` is `concurrency_exhausted`) while utilization and
   `Candidate.load` stay what runs, so usage and smart ranking among eligible targets are unchanged. The engine passes
   it from `snapshot()` (`:251`, also for `locate()`'s snapshots, so placement sees the same view), and reserves with
   `reserve(key, limit, ticket)` (`:483`). `routeCapacity` and `GET /v1/routes` keep describing configured capacity.
2. **Registration inside the section.** At the busy branch (`:535`), when no attempt has been selected yet, the
   accounts refused only for `concurrency_exhausted` (a decision whose reasons are exactly that; `already_attempted`
   pools never join) are mapped to pools; when they are exactly one pool and that pool queues (point 1, from the
   request's configuration), the request joins its line there, synchronously, before the section ends. Arrival order
   is this registration order (HTTP arrival and `execute()` order can differ before the section is reached). On
   success the section calls `leave(ticket)` before returning, so the ticket and its slot are never both counted
   outside it. The selection otherwise stays read-only until a reservation succeeds (`keep()` runs only then).
3. The busy 429 is still thrown; `reserveOrWait` catches it. No ticket: rethrown (today's answer). Line full: the queue
   429 (point 4). Ticket: the deadline is the registration time plus the pool's `maxWaitMs`; `admission.queued`
   `{ accounts, ahead, maxWaitMs }` is journaled; cleanup ownership (the `finally` below) is in place before that
   event is awaited.
4. `wait` on the request signal (client close, `shutdown()`), the consumer's `gone` signal, and a per-run controller
   that the whole-request cap timer aborts (`:424`; today it only trips the running attempt's watchdog, which does not
   exist while waiting). For the whole wait phase, including the `admission.queued` journal write and a re-selection
   after a wake (queued behind other sections or awaiting store/journal I/O), the request leaves the line at once when
   any of these ends it: the request signal, `gone`, the cap, or the queue's own deadline (a phase timer set at
   registration). The waiters behind it move up without waiting for its section.
5. **The commit point.** Right before `reserve` (synchronously), a request that has a ticket no longer in its line
   (it left for one of those reasons) takes no slot: the section throws an internal `QUEUE_LEFT`, and `reserveOrWait`
   answers by why it left (below). So a wake before the deadline cannot reserve after it, and a consumer that returned
   mid-selection takes no slot (no `CONCURRENCY_EXHAUSTED` from its own departure either). Tested with a held journal
   write and with a return during a held re-selection.
6. After the wait (or `QUEUE_LEFT`): the request signal → `signal.throwIfAborted()` (CANCELLED, cause CLIENT_CLOSED or
   SHUTDOWN: the first reason wins, as today); the consumer returned → `return` (as the attempt loop does, `:616`); the
   cap → `timeoutFault('max_duration')`; the queue's deadline → the queue 429 (point 4); woken → run the whole section
   again (all routes of the chain, placement included).
7. A `finally` leaves the line on every path and journals `admission.dequeued` `{ accounts, waitedMs, outcome }` once
   per wait, never awaited (so a journal that cannot take it neither fails the request nor loses an admitted slot; the
   recorder numbers it before its first await, so it precedes `admission.reserved`). Outcomes: `admitted`, `timeout`,
   `cancelled` (client, shutdown, consumer return), `max_duration`, and `refused` (a re-selection that ended the
   request another way: the account went offline or ineligible, or an error).

Waiting consumes no attempt (`maxAttempts` counts upstream attempts only). The per-attempt `Watchdog` (its wait and
idle timers), `attempt.started` and the live view start at admission, as today; the whole-request cap, the implicit
image deadline and a caller's cap (`:402`, `:424`) run from the start of the run and do bound the wait. The run counts
the request (`requests`) from its first read, so a wait longer than `UNREAD_MS` stays counted and the drain sees it.

### 4. The 429s the queue still gives

Both keep today's code, status and shape (`CAPACITY_UNAVAILABLE`, `reason: 'busy'`, `capacity`,
`x-should-retry: true`) and add `queue`; `RouterError.cause` is set explicitly, so the journal's `errorCause` has it.
- **Line full** (the pool's line holds `maxWaiting`): `queue: { outcome: 'full', maxWaiting }`, the message adds
  "Its queue is full: N requests are already waiting for a slot" (N = that pool's `maxWaiting`), `Retry-After: 5`
  (unchanged), journal `admission.queue_full`, `errorCause` `QUEUE_FULL`.
- **Wait expired**: `queue: { outcome: 'timeout', waitedMs }`, the message says how long it waited, `Retry-After: 1`
  (the client already waited `maxWaitMs`; its retry rejoins at the back), `errorCause` `QUEUE_TIMEOUT`. This is a retry
  policy, not a fairness guarantee: a retry loses its place, and waits that expire together retry together. For Codex
  the cumulative queue-wait budget is (8 + 1) × `maxWaitMs` when every wait expires, plus eight `Retry-After` delays of
  at least 1 s and each request's upload, selection and journal time (about 12 minutes at 80 s); full queues, client
  deadlines and other failures shorten it.
- The trajectory is `deferred` with `errorCode` `CAPACITY_UNAVAILABLE` (the busy 429 without a queue keeps no cause, as
  today). There is no earlier failure to prefer: the queue exists only before the first attempt.

### 5. No transport change

The HTTP handler sends a stream's head when the engine has *selected* the first attempt
(`packages/plugins/api-openai/index.ts:446-453`), so a queued request has no head, no `: ok` and no keepalives until it
is admitted, then streams exactly as today (progress events, reasoning, terminal events). This keeps the HTTP error
semantics (`Retry-After` on the queue's 429s) and the selected account's disclosure headers; opening the stream early
would need a transport redesign and comments alone would not satisfy Codex's event-counting idle timer. Measured:
Codex 0.162.1 and 0.166.0 wait for the head (330 s and counting with 0.162.1) and their idle timer starts with the
stream; the LAN path has no head timeout. A client behind Cloudflare gets a 524 after 100 s without a head, counted
from its request (upload, selection, journal and tokenizer work included): the README says to keep `maxWaitMs` well
under that (about 80 s) for accounts such clients use. A client that closes while queued aborts the request signal
(`reply.raw` `close`, `:393`), which ends the wait at once.

### 6. Interactions (each covered by a test, point "Verification")

- **Watchdog:** not running while queued (per attempt, created after reservation, `:548`).
- **Progress events / Codex's 300 s idle timer:** no stream exists while queued; after admission, as today.
- **Failover:** the queue is entered only when every candidate of every route in the chain is busy and no attempt was
  selected, so it never holds a request that a free target could serve; each wake re-evaluates the whole chain (a
  fallback that freed first takes it). A failover attempt that finds its targets busy answers as today (the earlier
  attempt's failure).
- **Mixed refusals:** busy plus offline, disabled, cooldown, quota or capability refusals does not enter the busy branch
  (`:535`, `tests/busy-capacity.test.ts:163`), so it does not queue, even where waiting for the busy target could help.
- **Conversation placement:** each selection re-locates with the ticket's view; a bound account that frees is preferred
  as today; queue contention reads as busy, so it spills (keeps the binding) rather than moves.
- **Strict affinity:** an existing strict binding keeps `AFFINITY_UNAVAILABLE` (the busy branch excludes it). An unbound
  strict request may queue; if a binding appears while it waits, the re-selection honours it (and answers
  `AFFINITY_UNAVAILABLE` when that account is busy: outcome `refused`).
- **Drain/SIGTERM:** a waiter is an active request: the drain waits for it up to `drainMs`, then `shutdown()` cancels it
  (SHUTDOWN); having no head, its connection is reset (`api-openai` `:566`) and Codex's retry reaches the next process
  through the edge, as for any request ended before its stream. A waiter admitted during the drain runs and is cut at
  the deadline like any request started then.
- **Whole-request cap:** the wait counts toward `maxDurationMs`, the image deadline and `imageProxy`'s cap (their
  minimum, fixed at the start of the run, never restarted by a wake); the cap ends a queued request with 504 `TIMEOUT`
  phase `max_duration` (`failed`, `MAX_DURATION`). An image request holds its slot until its image is delivered
  (bounded by `imageDeliveryMs`), so an image queue moves at delivery, not at generation.
- **Memory and budgets:** a waiter holds its HTTP body and parsed request, a cloned configuration, its trajectory and
  transport state, a timer and a ticket; for `/v1/responses` it also keeps its share of the process and per-principal
  body budgets until it ends (`packages/plugins/api-openai/index.ts:650`, `:710`), so many large queued requests can
  make unrelated requests meet `body_budget` 429s; image edits keep their admission too. Measured (ailab Prometheus, 7
  days to 2026-11-03): `/v1/responses` bodies p50 0.68 MB, p90 1.59 MB, p99 2.05 MB; every body of 2 MiB or less
  (the Codex agents on every route, ChatGPT-backed `dw-N` included) shares the small floor of 13.4 MB
  (`llm_router_native_body_budget_bytes{pool="small",state="limit"}`), whose 7-day peak use was 8.6 MB. Eight queued
  p90-sized bodies alone (12.7 MB) would nearly fill it; four (6.4 MB) leave room. `maxWaiting` bounds the waiters
  per pool, not process memory; only accounts that set `queue` have waiters. Tickets and budgets are released on every
  exit (tested).
- **Journal size:** a waiter adds `admission.queued`, `admission.dequeued` and, per wake, one selection's events (one
  `routing.evaluated` per alias tried); wakes are targeted (a ticket is woken when its turn comes, then once per change),
  so a typical wait adds 3-4 events against the recorder's per-trajectory cap of 256.

### 7. Observability

- Journal: `admission.queued`, `admission.dequeued`, `admission.queue_full` (above).
- Metrics (`packages/plugins/metrics/index.ts`, fed by the journal like the others): histogram
  `llm_router_admission_wait_seconds{account, provider, outcome}` (outcome `admitted`, `timeout`, `cancelled`,
  `max_duration`, `refused`, and `full` with 0 s), one observation per wait, labelled with the first account of the
  pool that refused (a pool shared by several accounts is counted once), its own buckets up to 600 s, under the
  existing series cap.

### 8. Docs

README: the busy 429 paragraph (it says "the router does not queue"), the account fields, streaming ("a queued request
gets its head when admitted"), the journal events and the metric; `docs/implementation-status.md`.

### 9. Release and config (the main session does these; this change stops before any of them)

- Image build and roll as usual (llm-router release staging).
- **Proposal Q1 (provisional):** after the roll, `queue: { maxWaitMs: 80000, maxWaiting: 4 }` on
  `lan-192.168.0.28-18020` through the admin API (`If-Match`, read back, revision recorded).
  - **80 s** leaves margin under Cloudflare's 100 s for the account's clients on that path: the dev workers reach the
    router on the LAN, but dsh and relay use `router.chifor.me` through Cloudflare
    (`kubernetes/apps/apps/dsh/settings.seed.yaml`) and dsh offers the local models (lowered from 90 s after review on
    llm-router #133 and this PR). What the journal supports is narrower than "enough": 80 s is above the slowest
    next-slot release seen for a busy 429 (75.9 s), so it would have covered the head of the line every time; that
    figure is a lower bound on residence (the 11-01 sample also counts Codex's 5 s retries of the same work), and a
    later waiter needs several releases, so it can still meet `QUEUE_TIMEOUT` and retry at the back. The acceptance run
    and the wait histogram, not this statistic, validate the value.
  - **4 waiters** covers the 4 dev workers (the incident had at most 2 clients waiting at once). It does not by itself
    keep the shared small body budget clear: the 7-day peak use of 8.6 MB plus four p90 bodies (6.4 MB) is 15 MB
    against 13.4 MB, so a deep queue during a busy hour can still give unrelated `/v1/responses` requests
    `body_budget` 429s (memory stays protected). The default 8 would make that worse; it is 4 since llm-router #134.
  - **Acceptance before relying on it** (the main session's release): 4 running plus queued requests, overflow and
    cancellation against cloud2, with representative small-body Responses traffic on other routes running meanwhile and
    `llm_router_native_body_budget_bytes{pool="small"}` and `body_budget` 429s watched.
  - **Revisit when:** `timeout` or `full` outcomes for cloud2 above a handful a day, any `body_budget` 429 while cloud2
    has waiters, or an end-to-end Codex failure on qwen-27b (a longer `maxWaitMs` only if no Cloudflare-path client
    uses the account; a smaller `maxWaiting` or a larger `codexNativeBodyBudgetMiB` if the budget bites).
- **Concurrency stays 4** (KV evidence above). A trial of 5 would need a controlled load test comparing peak and
  growing KV use, preemptions, vLLM-side waiting, token throughput and client latency under representative prompts;
  the queue's wait histogram alone cannot establish that.
- **Rollback:** first remove `queue` from the account (`If-Match`, read back; existing waiters drain within 80 s),
  then the image.

## Out of scope

- Queueing on other refusals (cooldown, quota, offline, strict binding), on failover attempts, and across several
  pools at once; a per-request or per-key opt-out header (a follow-up if a client with its own fallback, like the
  platform eval, prefers the instant 429).
- Releasing orphaned legs (supersede on resend; the edge's grace for streams): follow-ups.
- Keeping a client's queue position across its retries; priority classes; a queue-depth gauge or Status/Assistant view.
- Changing `BUSY_RETRY_MS` for accounts without a queue.
- The pre-existing race between the section's awaited evaluation and `reserve` (point "Verification" note).

## Critical files

llm-router (branch `feat/admission-queue` off `main` `93108f5`):
- `packages/modules/admission/index.ts` - the line, `ahead`, `reserve(..., ticket)`, `join`, `wait`, `leave`, pumping.
- `packages/modules/inference/index.ts` - `reserveOrWait` around the first attempt's section, the ticket in
  `snapshot`/`locate`/`reserve`, registration at the busy branch, the queue's 429s and journal events, the cap's abort.
- `packages/contracts/index.ts`, `packages/runtime/config.ts` - `Account.queue` and its schema.
- `packages/plugins/metrics/index.ts` - the wait histogram.
- Audited, unchanged: `packages/plugins/provider-claude/index.ts` (the probe's `reserve`), `packages/modules/routing/index.ts`
  (`poolLimit`, `evaluate`), `packages/plugins/api-openai/index.ts` (head timing, body budgets), `packages/kernel/drain.ts`.
- Tests: `tests/admission-queue.test.ts` (new: unit, engine and HTTP). Regression surfaces: `tests/busy-capacity.test.ts`,
  `tests/placement.test.ts`, `tests/cancel-cause.test.ts`, `tests/stream-timeouts.test.ts`, `tests/drain.test.ts`,
  `tests/metrics.test.ts`, the image and body-budget suites. Docs: `README.md`, `docs/implementation-status.md`.

## Verification

1. **Tests first** (failing before the change where they describe new behaviour):
   - `Admission` unit: arrival-order wakes; no barging (a newcomer and a probe refused while a waiter is owed the slot,
     the head admitted); a ticket of another pool counts every waiter there; `maxWaiting`; `wait` settles on wake,
     timeout, abort and `leave` with no timer or listener left; already-aborted and non-positive waits; a slot freed
     before the wait or during a re-selection is seen; several slots freeing together wake that many; a woken ticket
     that fails is not re-woken until something changes; a head that leaves (refused) lets the next through; empty lines
     are deleted; without waiters, today's counts.
   - Engine: a journal that refuses `admission.queued` fails the request and leaves no ticket; the consumer's
     `return()` while queued ends the run, frees the ticket and finalizes the request (cancelled, CONSUMER_CLOSED); a
     selection error after a wake is outcome `refused` with no ticket left.
   - HTTP (scripted upstream, concurrency 1, `queue` set): B waits while A runs and gets 200 when A ends, its journal
     `routing.evaluated` (busy) → `admission.queued` → `admission.dequeued` (admitted, `waitedMs` > 0) →
     `admission.reserved`; order across B, C and a late D; no `queue` → the 429 byte-identical to today with no
     `admission.*` event; wait expired → 429, `Retry-After: 1`, `queue.outcome: timeout`, `deferred`/`QUEUE_TIMEOUT`;
     line full → 429, `Retry-After: 5`, `queue.outcome: full`, `QUEUE_FULL`; a client that disconnects while queued (a
     real socket) leaves at once and is journaled CLIENT_CLOSED, and the waiter behind it moves up; `shutdown()` while
     queued resets the connection and journals SHUTDOWN; the drain coordinator with a queued request (real sockets):
     the request is ended at the deadline, its connection reset, `active()` reaches 0, exit 0; a route `maxDurationMs`
     shorter than the wait → 504 `max_duration`; a wait whose deadline passes during a slow `admission.queued` write
     takes no slot that freed meanwhile (429 `QUEUE_TIMEOUT`, no upstream request); a consumer that returns during a
     held re-selection takes no slot and the waiter behind it is served; two slots freeing together with the head's
     journal write held admit the first two waiters, the third keeps waiting; `owed` decides eligibility only
     (utilization and load unchanged); a queued Responses stream gets no head until admitted, then `: ok` and
     `response.created`; a chat stream likewise; a free second target or fallback serves at once; busy targets in two
     pools → today's 429 (no queue); a retryable first attempt then busy targets → the attempt's failure at once (no
     queue on failover); a conversation bound to a busy single-account route waits and keeps its binding; accounts
     sharing a pool queue only when all set it, by the smallest values; body budgets released after a queued
     Responses request ends.
   - Config: bounds, strictness, the default `maxWaiting`. Metrics: the histogram from the journal events, one
     observation per wait, the full outcome.
   - **Mutation checks** on external behaviour (each must fail a test): no anti-barging (a newcomer ignores waiters);
     the wait ignoring the request signal; the deadline ignored; `leave` skipped on timeout; the version check removed
     (a re-woken loop shows up as repeated selections); `maxWaiting` ignored; registration on failover attempts.
2. **CI gates in docker** (node:24, pnpm 10.32.1, `pnpm install --frozen-lockfile`): `pnpm verify` (typecheck, test,
   production build) plus build:lib, test:production and check:architecture, each reported with its result, against
   the same run on unchanged `93108f5`; any failure present on the baseline is named as such; the known load-flaky
   tests (`subscription-login`, `system-messages` failover, `image-memory`, llm-router #129) pass when run alone and
   are reported as flaky, not as passing; the PostgreSQL auth tier and the browser suite reported as skipped.
3. **The real Codex CLI end to end** (0.162.1 and 0.166.0, docker, the in-process router at `concurrency: 1`, a fake
   vLLM-style upstream that thinks 60 s, the workers' retry settings, POST counts and upstream start times recorded):
   (a) `queue: { maxWaitMs: 120000 }`: two agents started together both exit 0, two POSTs, the second's upstream request
   starts when the first's ends, no 429; (b) `maxWaitMs` 20000: the second agent meets a 429 with `Retry-After: 1`,
   rejoins, and exits 0; (c) baseline on `main`: the second agent exits 1 after 9 × 429 (40 s). After each scenario
   the router has no waiter and no request in flight.
   Note: the section's awaited evaluation before `reserve` can still lose a slot to the Claude usage probe, which
   reserves outside the section (`CONCURRENCY_EXHAUSTED`); that race predates this change and the queue does not widen
   it (the probe counts every waiter; a waiter's own departure is caught at the commit point, point 3.5).
4. **Images** (round 2): a queued generation ends at the image deadline (the route's wait timeout) and at the
   `imageProxy` deadline as 504 `max_duration` without reaching the server, and one queued behind a held generation is
   served when the slot frees; a queued edit keeps one of the two edit admissions until it ends, and cancelling it
   frees its place and that admission (`tests/images.test.ts`).

<!-- codex-review-status: finalized -->
