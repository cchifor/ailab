# llm-router: a bounded per-account admission queue (wait for a slot instead of an immediate 429)

## Codex Review

- **Changes required before implementation:** ticket registration, reservation races, mixed-limit fairness, partial multi-pool membership and per-pool deadlines need explicit rules.
- The no-head and extended-patience arguments apply to initial admission. A later failover can queue on an already-open stream, where HTTP `Retry-After` cannot help and the client's idle timer is running.
- Cleanup must cover asynchronous selection and journal failures as well as sleeping waiters. The proposed tests do not yet cover these ownership transitions.
- Keeping concurrency 4 is a reasonable conservative baseline, but the KV measurements do not prove a fifth request always exceeds capacity. The proposed 90 s / 8-waiter settings remain a rollout hypothesis, not a demonstrated incident fix.
- Include existing body budgets, retained configuration and journal limits in the resource analysis, and require explicit verification results. This review checked source at `93108f5`; no files were changed and no builds or tests were run.

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
<!-- codex: The measured next-release delay is not a FIFO admission-delay distribution: several distinct waiters cannot all consume the same first free slot, and the 76 refusals include retries of the same work. Deduplicate logical requests and replay arrivals, service durations and queue positions before claiming every request would have been served within 76 s. -->
- cloud2 request durations (n = 1430): p50 5.9 s, p90 48 s, p99 300 s, max 330 s.

**cloud2's real capacity (ailab Prometheus, 2-minute buckets, 2026-10-19 → 11-03, the TP=2 era):**
- With 4 requests running, KV-cache use p95 0.76, max 0.82; zero preemptions in 15 days; requests waiting inside
  vLLM in 5 of those buckets. The pool is 407,266 tokens (`vllm:cache_config_info`, 1.55× at 262144);
  `--max-num-seqs 8`, `--max-num-batched-tokens 2048` (cloudlab runbook §12).
- So at today's agent context sizes (~83k tokens per running sequence at the observed max), 4 running sequences already
  fill ~80 % of the pool. A 5th would push it past 100 %: vLLM then queues it (`waiting`, reason `capacity`) or preempts
  a running sequence (recompute of a long prefix).
<!-- codex: This extrapolation assumes equal incremental KV use and ignores shared prefixes and request-size variation: scaling 0.76 by 5/4 gives 0.95, while scaling 0.82 gives 1.025. The evidence supports caution and retaining 4 pending a controlled load test, not the categorical claim that every fifth request must queue or preempt. -->

**Orphaned legs.** In the 11-01 incident a Renovate roll of the `edge-proxy` DaemonSet cut the clients' streams at
~17:28:05; Codex resent at once while the old proxy kept the router connections (and their slots) until it exited
(Caddy aborts at 17:29:40). The router releases a slot promptly when it sees a disconnect (the request signal aborts,
`Watchdog.cancel` abandons the read, the upstream close is bounded by `closeGrace` 10 s, then `reservation.release()`
in the attempt's `finally`, `:552-553`, `:648-666`, `:723`); it cannot see a client that left behind a proxy that keeps
the router connection open and reading.
<!-- codex: The 10 s bound covers the upstream iterator's close, not all time until capacity release: awaited attempt-failure recording precedes reservation.release() (packages/modules/inference/index.ts:702, packages/modules/inference/index.ts:723), and strict recording can wait on storage (packages/modules/trajectory/index.ts:48). Qualify “promptly” and distinguish measured normal cleanup from a guaranteed release deadline. -->

## Approach

A **bounded, FIFO, per-account-pool admission queue**, opt-in per account. A request that today gets the busy 429
waits instead, up to the account's `maxWaitMs`, for a slot of a pool that refused it; it is admitted in arrival order
when a slot frees, and it leaves the queue at once when its client goes, the router shuts down, or its whole-request
cap passes. A full queue or an expired wait still answers the busy 429. No account changes behaviour unless it sets
`queue`; with no `queue` anywhere the router is byte-identical to today.
<!-- codex: Cloud2 needs one pool and one initial admission, while this proposal introduces a general scheduler spanning failover, multiple pools and configuration revisions. Either specify and test those scheduler invariants fully below or explicitly narrow the first release; “FIFO” and “bounded” are currently stronger than the described algorithm establishes. -->

Why this and not the alternatives (all four directions were evaluated):
- **Raising `concurrency` alone** (direction 2): rejected for cloud2 on the KV evidence above; extra admissions would
  queue inside vLLM, where a waiting request sends no bytes, so the router's 300 s wait timer and Codex's 300 s idle
  timer both run on a request making no progress (#131's progress events fire only on upstream bytes), and a
  preemption recomputes a long prefix. The router-side queue is the better place to wait: ordered, cancellable,
  visible, and before any timer starts.
<!-- codex: Queueing precedes the per-attempt Watchdog, but not every timer: the whole-request cap, implicit image deadline and caller cap start when run begins (packages/modules/inference/index.ts:402, packages/modules/inference/index.ts:424). State that distinction here rather than promising a timer-free wait. -->
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
<!-- codex: The proposed 90 s wait expires before this stated 95 s orphan window, potentially before upstream cleanup too. Describe recovery through a subsequent retry as a hypothesis; this example does not establish that one queued resend survives until admission. -->

### 1. Configuration: `Account.queue`

`queue?: { maxWaitMs: number; maxWaiting?: number }` on `Account` (`packages/contracts/index.ts`), validated in
`packages/runtime/config.ts` as a strict object: `maxWaitMs` an integer 1000-600000, `maxWaiting` an integer 1-100,
default 8. Set with `PUT /admin/v1/config` like `forwardReasoning` (no Assistant tool, no UI). Accounts sharing a pool
share one queue (as they share one limit): the pool queues only if every enabled account of the pool sets `queue`, and
then with the smallest `maxWaitMs` and the smallest `maxWaiting` among them (the "smallest wins" rule `poolLimit`
already applies to `concurrency`). A rollback image ignores the field and drops it on its next validated write (the
account schema is not strict), so it must be removed explicitly before a rollback if it is to stay off (point 9).
<!-- codex: Pool-level queue depth matches the shared capacity resource, but maxWaitMs is also a caller-latency policy: this setting affects every route, direct-model request and protocol using the pool, including the eval client with its own fallback. Document that deliberate scope and that an enabled member outside the requested route can disable queueing; pool membership comes from quotaPool or namespace/provider/identity, not the endpoint URL (packages/modules/accounts/index.ts:22, packages/modules/routing/index.ts:223). -->

Contract change (`packages/contracts/index.ts` is a contract file, AGENTS.md "ask first"): additive and optional; the
owner asked for this fix on 2026-11-02; the PR names it. cchifor/platform vendors the module and compiles against the
type; an optional field does not break it.
<!-- codex: Record approval of this concrete Account.queue contract as part of the plan decision; the generic fix request and mentioning it in a PR do not themselves document the contract decision required by AGENTS.md:42. This read-only review does not approve that change or Q1. -->

### 2. `Admission` keeps the waiters (the single authority for slots)

`packages/modules/admission/index.ts` gains a FIFO of tickets per pool. Everything stays synchronous except `wait`.
- `join(entries: Array<{ pool; limit; maxWaiting }>): Ticket | undefined` registers one ticket (a global, increasing
  sequence number) in every listed pool whose list is shorter than its `maxWaiting`, remembering that pool's `limit`
  (from the request's configuration) on the ticket; `undefined` when every list is full.
<!-- codex: Partial registration loses wake-ups from omitted pools: if A's queue is full and the ticket joins B, a later release on A never pumps this ticket, although re-selection could serve it there. The same problem applies to a busy fallback without queue enabled; specify subscriptions or membership reconciliation for every pool whose release should make the request runnable. -->
- `ahead(pool, ticket?)`: the tickets registered on `pool` before `ticket`; without a ticket, all of them.
<!-- codex: Define the case where a ticket exists but is not registered on this pool: it must not inherit priority there from its global sequence number. Otherwise a ticket admitted only to B can bypass A's waiters when the whole routing chain is re-evaluated. -->
- `reserve(pool, limit, ticket?)` refuses when `used(pool) + ahead(pool, ticket) >= limit`. So a request that is not
  waiting never takes a slot that a waiter is owed (no barging), and the head waiter sees exactly the free slots.
  The existing two-argument call (provider-claude's usage probe, `packages/plugins/provider-claude/index.ts:228`)
  counts every waiter, so a probe never jumps the queue either. Without waiters `ahead` is 0: unchanged.
<!-- codex: This does not guarantee FIFO across differing limits: with used=0 and an older ticket limited to 1, a new caller limited to 4 passes 0+1<4 and can occupy the slot the older ticket needs. The Claude probe actually supplies account.concurrency rather than poolLimit (packages/plugins/provider-claude/index.ts:230), so define one effective pool-limit policy for tickets, new requests and probes, including configuration changes. -->
- `wait(ticket, signals, ms): Promise<'woken' | 'timeout' | 'aborted'>`: settles on a wake, after `ms`, or when any of
  `signals` aborts; every path removes its timer and listeners.
<!-- codex: Specify immediate settlement for already-aborted signals and non-positive remaining time, plus precedence when wake, timeout and cancellation coincide. Listener cleanup must also run on synchronous setup failure and leave(), and an abandoned wait must not retain its resolver or request closure. -->
- `leave(ticket)` removes the ticket from every list; idempotent.
<!-- codex: Remove all memberships before pumping and delete empty pool/version state so configuration churn cannot leave an unbounded map of historical pools. Keep pumping synchronous and limited to resolving wake notifications; it must never invoke routing re-entrantly inside the engine's exclusive section (packages/modules/inference/index.ts:227). -->
- **Wake-ups.** A release and a `leave` bump a version number and "pump" the affected pools: walking a pool's
  tickets in order with `i` tickets ahead, it wakes a waiting ticket when `used + i < ticket.limit(pool)` and the
  version changed since that ticket was last woken. `wait` pumps the ticket's pools when it starts, so a slot freed
  between the busy decision and the wait is not missed. The version check means a woken ticket whose selection still
  failed (a slot taken by something outside the queue, or a limit that differs from its own) is woken again only after
  something changes: no tight loop.
<!-- codex: Define whether versions are one monotonic global epoch or per-pool epochs, and consume an epoch only when an actual pending wait is settled. A release while the ticket is awake/re-selecting must remain observable when it re-arms, the first wait must inspect currently free capacity, and simultaneous releases across its pools must coalesce without suppressing a later useful wake. -->

### 3. The engine waits on the busy answer, outside the exclusive section

In `RouterEngine.run`, the selection-and-reservation step becomes a loop around the existing exclusive section:
1. The section is unchanged except that its snapshot and its reservation use the request's ticket:
   `evaluate`'s `inFlight` is `used + ahead(pool, ticket)` (`snapshot()`, `:251`, also for `locate()`'s snapshots,
   so a placement sees the same view), and `reserve(key, limit, ticket)` (`:483`). At the busy branch (`:535`) it also
   records which accounts refused for `concurrency_exhausted`. On success it calls `leave(ticket)` before returning, so
   the waiter never counts twice outside the section.
<!-- codex: The section is not atomic against all admission callers: snapshot evaluation and routing.evaluated are awaited before reserve(), so the usage probe or queue membership changes can invalidate the decision (packages/modules/inference/index.ts:459, packages/modules/inference/index.ts:470, packages/modules/inference/index.ts:483). Admission currently throws CONCURRENCY_EXHAUSTED (packages/modules/admission/index.ts:7), which bypasses the busy branch and lastFault mapping; explicitly re-evaluate this reservation race without consuming an attempt or leaking a ticket. -->
<!-- codex: Feeding ahead into inFlight also changes utilization and Candidate.load, hence usage/smart ranking, rather than only filtering capacity (packages/modules/routing/index.ts:315, packages/modules/routing/index.ts:320, packages/modules/routing/index.ts:334). Decide whether this virtual load is intentional and test it; routeCapacity should continue to describe configured running capacity, not running-plus-waiting requests (packages/modules/routing/index.ts:244). -->
2. On the busy answer, the engine maps those accounts to their pools and keeps the pools that queue (point 1, from the
   request's configuration). None: today's 429 (or the last attempt's failure, as today, `:540`).
<!-- codex: Only join pools that can become eligible for this request when concurrency clears: evaluate can attach both already_attempted and concurrency_exhausted to an excluded pool (packages/modules/routing/index.ts:278, packages/modules/routing/index.ts:316). Registering there would reserve queue priority on a pool the request can never use; also deduplicate pools across accounts, deployments and fallback routes before join(). -->
<!-- codex: Intercept the busy result before the existing .catch substitutes lastFault (packages/modules/inference/index.ts:540), then apply that substitution only to a final capacity refusal. Otherwise retryable-attempt failover never reaches the proposed wait loop; cancellation, whole-request timeout and recording errors must retain their own precedence. -->
3. First time: `join`. Full: 429 (point 4). Otherwise journal `admission.queued` `{ accounts, ahead, maxWaitMs }` and
   fix the deadline: now + the largest `maxWaitMs` of the pools joined. The deadline is per wait phase: a later
   failover wait (after an attempt ran) joins again with a fresh ticket and deadline.
<!-- codex: Establish ticket registration and its deadline synchronously with the busy decision before relinquishing exclusive ownership; otherwise anti-barging starts only after join and an outside-chain probe can reserve a newly freed slot in the gap (packages/modules/inference/index.ts:227, packages/plugins/provider-claude/index.ts:230). Define arrival order as this registration order, since HTTP arrival and execute() order can differ before the section is reached. -->
<!-- codex: Taking the largest maxWaitMs violates smaller pools' bounds: a ticket joining A at 1 s and B at 90 s remains ahead on A for 90 s. Either expire each pool membership at its own deadline or choose an explicit common budget that honors those bounds, and start it at registration before awaiting admission.queued. -->
4. `wait` on the request signal (client close, `shutdown()`), the consumer's `gone` signal, and a new per-run
   controller that the `maxDurationMs` timer aborts (`:424`; today it only trips the running attempt's watchdog,
   which does not exist while waiting).
<!-- codex: Cancellation and deadline ownership must span the whole wait phase, including time after a wake while queued behind exclusive work or awaiting snapshot/binding/journal I/O, not just the wait() promise. Recheck signal, gone, the absolute queue deadline and the request cap inside the section before committing a reservation, and arrange ticket removal on cancellation even while re-selection is suspended (packages/modules/inference/index.ts:459, packages/modules/inference/index.ts:499, packages/modules/inference/index.ts:542). -->
5. After the wait: aborted → `signal.throwIfAborted()` (CANCELLED, cause CLIENT_CLOSED or SHUTDOWN, as today), or
   `return` when the consumer returned (as the attempt loop does, `:616`); cap passed → `timeoutFault('max_duration')`;
   timeout → 429 (point 4); woken → run the whole section again (all routes of the chain, placement included).
6. A `finally` leaves the queue on every path and journals `admission.dequeued` `{ accounts, waitedMs, outcome }`
   (`admitted` | `timeout` | `cancelled`) once per wait phase, before `admission.reserved` when admitted.
<!-- codex: Install cleanup ownership before join/queued recording and protect a successful reservation before awaiting dequeued recording: that event can throw under strict capture (packages/modules/trajectory/index.ts:48), while the existing attempt finally does not own the reservation until later (packages/modules/inference/index.ts:509, packages/modules/inference/index.ts:556, packages/modules/inference/index.ts:723). A dequeue-journal exception must neither leak capacity/lease/live tracking nor mask the original exit before timer/listener cleanup runs. -->
<!-- codex: The outcome enum has no honest value for re-selection becoming offline/ineligible, a selection exception or a recording failure; these are neither admission nor cancellation. Specify those exits and distinguish the queue's own expiry from a max_duration failure, whose existing trajectory is failed/TIMEOUT/MAX_DURATION (packages/modules/inference/index.ts:83, packages/modules/inference/index.ts:733). -->

Waiting consumes no attempt (`maxAttempts` counts upstream attempts only). The route's wait/idle timers, the
`Watchdog`, `attempt.started` and the live view all start at admission, as today. `requests` counts a waiter, so the
drain sees it.
<!-- codex: Preserve execute/run's count handoff: the first iterator.next() clears UNREAD_MS and transfers counting to run, so a legitimate queue wait beyond 60 s must remain active (packages/modules/inference/index.ts:351, packages/modules/inference/index.ts:371, packages/modules/inference/index.ts:395). Keep the decrement in the outermost finally after final recording and create no ticket for an unread execution or return() before its first read (packages/modules/inference/index.ts:372, packages/modules/inference/index.ts:753). -->

### 4. The 429s the queue still gives

Both keep today's code, status and shape (`CAPACITY_UNAVAILABLE`, `reason: 'busy'`, `capacity`, `x-should-retry: true`)
and add `queue`:
- **Queue full** (every queueing pool at `maxWaiting`): `queue: { outcome: 'full', maxWaiting }`, the message adds
  "and N requests are already waiting", `Retry-After: 5` (unchanged), journal `admission.queue_full`.
<!-- codex: A single maxWaiting and “N requests” are ambiguous when several pools have different bounds or share the same tickets. Define whether N is a particular pool's actual depth or unique requests across pools, and do not imply the configured routeCapacity includes fallback pools (packages/modules/routing/index.ts:241). -->
- **Wait expired**: `queue: { outcome: 'timeout', waitedMs }`, the message says how long it waited,
  `Retry-After: 1`: the client already waited `maxWaitMs`, so its retry should rejoin at once. With Codex this makes
  its patience about (8 + 1) × `maxWaitMs` instead of 40 s.
<!-- codex: Nine full waits plus eight 1 s delays is only the all-timeout case; queue-full responses, body-budget refusals, client deadlines and retries after an opened stream can shorten it substantially. Retain 1 s only as an explicit retry policy, not a fairness guarantee: retries lose position, synchronized expiries can create bursts, and Retry-After is emitted only on the HTTP error path (packages/kernel/http.ts:477). -->
- The trajectory is `deferred` with `errorCode` `CAPACITY_UNAVAILABLE` and `errorCause` `QUEUE_FULL` or
  `QUEUE_TIMEOUT` (the busy 429 without a queue keeps no cause, as today). When an earlier attempt of the same request
  failed retryably, that failure is still the answer (`lastFault`, `:540`), the queue outcome stays in the journal.
<!-- codex: Qualify the deferred/QUEUE_* assertion to requests whose final fault is the queue 429: a preserved lastFault may instead produce failed and its original cause (packages/modules/inference/index.ts:733). Set RouterError.cause explicitly for the queue faults; putting an outcome in error.data does not populate trajectory.errorCause (packages/contracts/index.ts:463, packages/modules/inference/index.ts:735). -->

### 5. No transport change

The HTTP handler sends a stream's head when the engine has *selected* the first attempt
(`packages/plugins/api-openai/index.ts:446-453`), so a queued request has no head, no `: ok` and no keepalives until it
is admitted, then streams exactly as today (progress events, reasoning, terminal events). Measured: Codex waits for the
head (330 s and counting) and its idle timer starts with the stream; the LAN path has no head timeout. A client behind
Cloudflare gets a 524 after 100 s without a head: the README says to keep `maxWaitMs` under 100 s for accounts such
clients use. A client that closes while queued aborts the request signal (`reply.raw` `close`, `:393`), which ends the
wait at once.
<!-- codex: This is true only before the first selection: select() runs before provider.open(), so a retryable first attempt can fail with the stream already open and its fallback then queue (packages/modules/inference/index.ts:587, packages/modules/inference/index.ts:596, packages/modules/inference/index.ts:715). That wait gets comments but no upstream-driven progress, runs under the client's stream idle timer, and ends through the in-stream error path rather than an HTTP 429 with Retry-After (packages/plugins/api-openai/index.ts:505, packages/plugins/api-openai/index.ts:555). -->
<!-- codex: Waiting before the initial head preserves HTTP error semantics and the first selected account's disclosure headers; opening early would require an explicit transport/protocol redesign, and comments alone would not solve Codex's event-idle timeout (packages/plugins/api-openai/index.ts:442, packages/plugins/api-openai/index.ts:453). Keep the initial-head choice, but bound the claim to the measured clients and reserve proxy head-timeout margin for upload, selection, strict recording and tokenizer work; maxWaitMs below 100 s alone is insufficient (packages/modules/inference/index.ts:582). -->

### 6. Interactions (each one covered by a test, point "Verification")

- **Watchdog wait timer:** not running while queued (it is per attempt, created after reservation, `:548`).
- **Progress events / Codex's 300 s idle timer:** no stream exists while queued; after admission, as today.
<!-- codex: Preserve the distinction between synthesized progress and provider output: progress does not prevent failover, but yielded reasoning already sets emitted and prevents another attempt (packages/modules/inference/index.ts:642, packages/modules/inference/index.ts:715). Test both with queueing enabled, including a prior attempt's late activity callback while the next attempt is queued. -->
- **Failover:** the queue is entered only when every candidate of every route in the chain is busy, so it never holds
  a request that a free target could serve; each wake re-evaluates the whole chain (a fallback that freed first takes
  it). Waiting on several pools is one ticket in each.
<!-- codex: Preserve the existing mixed-refusal boundary explicitly: busy plus offline, disabled, cooldown or capability refusal does not enter this branch, even when waiting for the busy target could help (packages/modules/inference/index.ts:535, tests/busy-capacity.test.ts:163). Also, per-pool FIFO is not global work-conserving matching: a ticket holding priority in A and B can temporarily block an A-only request while ultimately selecting B; document this tradeoff and avoid promising strict global arrival order. -->
- **Conversation placement:** each selection re-locates; a bound account that frees is preferred as today.
<!-- codex: Re-location must use the ticket's view everywhere and remain read-only until a real reservation succeeds; otherwise its own queue position can make a usable bound seat look busy or change the binding while merely waiting (packages/modules/inference/index.ts:286, packages/modules/inference/index.ts:505). Test bound fallback preference, same-seat aliases, warm spills and binding preservation; queue contention must remain a spill reason rather than an accidental move (packages/modules/placement/index.ts:11, packages/modules/placement/index.ts:42, packages/modules/placement/index.ts:79). -->
- **Strict affinity:** unchanged (`AFFINITY_UNAVAILABLE`; the busy branch already excludes it).
<!-- codex: The exclusion is specifically an existing strict binding, not every strict-affinity request (packages/modules/inference/index.ts:534). An unbound strict request can encounter all-busy capacity, and another request can establish its agent binding while it waits; test that re-selection honors the new binding and does not admit a different account (packages/modules/inference/index.ts:255, packages/modules/routing/index.ts:330). -->
- **Drain/SIGTERM:** a waiter is an active request: the drain waits for it up to `drainMs`, then `shutdown()` cancels it
  (SHUTDOWN); having no head, its connection is reset (`api-openai` `:566`) and Codex's retry reaches the next process
  through the edge, as for any request ended before its stream. A waiter admitted during the drain runs and is cut at
  the deadline like any request started then.
<!-- codex: Test the drain coordinator and real sockets, not only engine.shutdown(): readiness/listener closure, admission before the common deadline, Connection: close, queued-request finalization and active() reaching zero must work together (packages/kernel/drain.ts:84, packages/kernel/drain.ts:111, packages/plugins/api-openai/index.ts:453). Include cancellation while a woken ticket waits behind the exclusive chain and both CLIENT_CLOSED/SHUTDOWN orderings; the first request-signal reason must still win (packages/modules/inference/index.ts:433). -->
- **Whole-request cap:** the wait counts toward `maxDurationMs`, the image deadline and `imageProxy`'s cap; the cap ends
  a queued request with 504 `TIMEOUT` phase `max_duration`.
<!-- codex: Compute the queue's remaining budget against the existing minimum of route cap, image waitMs and ExecuteOptions.maxDurationMs, without restarting execution.deadlineAt on wake or failover (packages/modules/inference/index.ts:402, packages/modules/inference/index.ts:423). Add queued generation/edit and imageProxy tests; images() holds admission through bounded delivery, so freeing an image slot is later than merely receiving the upstream image (packages/plugins/api-openai/index.ts:601, packages/plugins/api-openai/index.ts:612). -->
- **Memory:** a waiter holds its parsed request (subset bodies ≤ 2 MiB; native bodies up to `codexNativeMaxBodyMiB`,
  16 MiB) plus a timer and a ticket; bounded by `maxWaiting` per pool (≤ 100). Tickets leave on every path (tested).
<!-- codex: This omits the retained HTTP body, parsed representation, cloned RouterConfig, trajectory and transport state; a per-pool count is not a process memory bound, especially across many pools or requests awaiting exclusive selection before join (packages/modules/inference/index.ts:323, packages/runtime/config.ts:46). Include image edits up to 40 MiB of inputs, whose existing two-edit admission remains held throughout the handler (packages/plugins/api-openai/index.ts:722, packages/plugins/api-openai/index.ts:736). -->
<!-- codex: Responses already reserve a process/per-principal body budget until the handler settles, so queued requests retain that budget and can cause unrelated routes to receive body_budget/principal_budget 429s before this queue is reached (packages/plugins/api-openai/index.ts:650, packages/plugins/api-openai/index.ts:710, packages/kernel/http.ts:405). Preserve that ownership and test release on every queued exit; eight available ticket positions do not imply eight large native requests can be accepted. -->
- **Configuration changes while waiting:** a waiter keeps the limits of the configuration it started with, like every
  request; new requests use the new ones. A raise reaches the waiters as they are woken or time out (≤ `maxWaitMs`).
<!-- codex: A raise never reaches an existing waiter's captured configuration or ticket limit under this design; execute() clones config once (packages/modules/inference/index.ts:323), and config changes do not pump Admission. Define behavior for increases, decreases, queue disablement and changed pool identity explicitly, including whether old tickets continue blocking new requests after queue is removed. -->
- **Journal size:** a waiter adds two events plus one `routing.evaluated` per wake; wakes are targeted (version check).
<!-- codex: A wake can evaluate several aliases and repeat after unbinding, not just emit one routing.evaluated (packages/modules/inference/index.ts:454, packages/modules/inference/index.ts:470, packages/modules/inference/index.ts:476). Version checks do not bound total event volume, and the recorder drops all further events, including metric taps, after 256 entries; test that repeated wakes cannot silently lose dequeue/final observability (packages/modules/trajectory/index.ts:41). -->

### 7. Observability

- Journal: `admission.queued`, `admission.dequeued`, `admission.queue_full` (above).
- Metrics (`packages/plugins/metrics/index.ts`, fed by the journal like the others): histogram
  `llm_router_admission_wait_seconds{account, provider, outcome}` (outcome `admitted`, `timeout`, `cancelled`,
  `full` with 0 s), one observation per account waited on.
<!-- codex: Define whether an account label means “considered while waiting” or “actually admitted here”: observing every member of a shared pool or every alternative duplicates one request's wait and can label a losing pool admitted. Use self-contained event metadata for the chosen attribution, preserve the existing series cap, and choose buckets covering 90 s and the permitted 600 s rather than blindly reusing first-content buckets (packages/plugins/metrics/index.ts:40, packages/plugins/metrics/index.ts:52, packages/plugins/metrics/index.ts:105). -->

### 8. Docs

README: the busy 429 paragraph (it says "the router does not queue"), the account fields, streaming ("a queued request
gets its head when admitted"), the journal events and the metric; `docs/implementation-status.md`.

### 9. Release and config (the main session does these; this change stops before any of them)

- Image build and roll as usual (llm-router release staging).
- **Proposal Q1:** after the roll, `queue: { maxWaitMs: 90000, maxWaiting: 8 }` on `lan-192.168.0.28-18020` through
  the admin API (`If-Match`, read back, revision recorded). 90 s covers the slowest slot release the journal shows for
  a busy 429 (75.9 s) and stays under Cloudflare's 100 s for any client on that path; Codex's patience becomes about
  13-14 minutes. 8 waiters is twice the slots (4 dev workers; the incident had at most 2 clients waiting at once).
<!-- codex: Treat 90 s / 8 as provisional: the evidence measures next release rather than queue residence, and eight waiting jobs behind four running jobs can require multiple service waves with 300–330 s tails. Before rollout, exercise four active plus eight queued requests, overflow and cancellation, and define acceptance/rollback criteria using timeout/full rates and end-to-end success as well as admitted-wait latency. -->
- **Concurrency stays 4** (KV evidence above). Revisit after a week of `llm_router_admission_wait_seconds`: long
  admitted waits with KV p95 < 0.7 at 4 running would justify trying 5.
<!-- codex: Admitted waits alone exclude requests that timed out or cancelled, and KV p95 alone hides the tail that threatens a fifth long-context request. A trial of 5 should also compare peak/growing KV demand, preemptions, upstream waiting, token throughput and client latency under representative prompts; the new histogram cannot establish those conditions by itself. -->
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
<!-- codex: Include packages/plugins/provider-claude/index.ts in the implementation audit, since its reserve limit differs from the engine's, and reuse or centralize pool-limit calculation with packages/modules/routing/index.ts:221. The critical regression surfaces also include tests/busy-capacity.test.ts, tests/placement.test.ts, tests/cancel-cause.test.ts, tests/stream-timeouts.test.ts, tests/drain.test.ts and the image/body-budget suites. -->

## Verification

1. **Tests first** (failing before the change where they describe new behaviour):
   - `Admission` unit: FIFO wake order; no barging (a non-ticket `reserve` refused while waiters are owed the slot,
     the head ticket admitted); `maxWaiting`; `wait` settles on wake, timeout and abort with no timer or listener left;
     `leave` idempotent and pumping; the version check (a woken ticket that fails is not re-woken until something
     changes); a release with no waiters is today's behaviour.
<!-- codex: Add deterministic scheduling barriers for release before join, between join and wait, during re-selection and immediately before re-arming; also exercise a late arrival already ahead in the exclusive promise chain and several slots freeing together. Assert eventual admission, reservation counts and no residual tickets/timers/listeners, not merely promise wake order. -->
<!-- codex: Add mixed-limit old/new requests and the real probe caller, partially full multi-pool joins, non-member ticket access, distinct pool deadlines, duplicate pool entries and departure from all pools. Include a case where an unusable or expired head would otherwise block eligible followers, and verify empty pool bookkeeping is reclaimed. -->
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
<!-- codex: Missing failure injection covers queued/dequeued journal rejection, selection/store/provider-acquire exceptions and cancellation/cap expiry during awaited work after wake; assert no upstream call after termination and no capacity/lease/live-count leak. Also test a wait longer than UNREAD_MS, return() before the first read, and simultaneous return/cancel/timeout precedence without an unhandled abandoned next() rejection (packages/modules/inference/index.ts:358, packages/modules/inference/index.ts:616). -->
<!-- codex: Add an actual retryable attempt followed by a successful queued fallback, queued-fallback expiry after the SSE head, and lastFault precedence on both full and expired queues; the listed free-fallback test never exercises that state. Cover native Responses, subset Responses, Chat Completions and non-streaming behavior, plus the placement, strict-affinity, image, body-budget and real-drain cases identified above. -->
   - Config: bounds, strictness, the default `maxWaiting`. Metrics: the histogram from the journal events.
<!-- codex: Config tests also need omitted queue, enabled members without queue, disabled members, members outside the route, pool identity/namespace isolation and simultaneous old/new configuration snapshots. Metric tests should pin attribution and exactly-once observations across multiple pools, failure exits and full queues, including labels when an account is removed while a request waits. -->
   - **Mutation checks** on the key guards (each must fail a test): no anti-barging (`ahead` 0 for non-tickets); LIFO
     pumping; the wait ignoring the request signal; the deadline ignored; `leave` skipped on timeout; the version
     check removed; `maxWaiting` ignored.
<!-- codex: Mutation checks should prove external safety and liveness, not require one internal pump traversal: with concurrency 1, reversing traversal can still wake only the eligible head and preserve correct behavior. Prioritize adversarial admission/cancellation schedules over a mutation that only fails an implementation-shaped wake-order assertion. -->
2. **CI gates in docker** (node:24, pnpm 10.32.1, `pnpm install --frozen-lockfile`): typecheck, test, build,
   build:lib, test:production, check:architecture (only the known #79 line); the known load-flaky tests
   (`subscription-login`, `system-messages` failover, `image-memory`, llm-router #129) pass when run alone.
<!-- codex: Run the required pnpm verify entry point and report each additional gate's actual result; a known architecture failure or a suite failure followed by an isolated pass is not a passing gate (AGENTS.md:40, AGENTS.md:44). Establish any baseline failure on unchanged 93108f5 under the same environment, justify browser-test omission or run it, and report unavailable PostgreSQL integration coverage as skipped. -->
3. **The real Codex CLI end to end** (0.162.1 and 0.166.0, docker, the in-process router at `concurrency: 1` with
   `queue: { maxWaitMs: 120000 }`, a fake vLLM-style upstream that thinks 60 s, the workers' retry settings): two
   agents started together both exit 0, the second's upstream request starts when the first's ends, no 429 is sent.
   Baseline on `main`: the second agent exits 1 after 9 × 429 (40 s).
<!-- codex: This validates the simplest initial-admission path, not Q1's queue depth, timeout/retry behavior or failover on an open stream. Keep the supplied CLI baseline measurements as established evidence and extend the new-queue scenarios to expiry/rejoin and client cancellation; record POST counts, upstream start times and complete resource cleanup rather than only exit codes. -->

<!-- codex-review-status: complete -->
