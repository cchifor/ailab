<!-- status: proposed (design only, revised after codex rounds 1 and 2; amends plans/2026-11-04-router-standard-design.md §5.5, §4.3 and §6; no code changed; Part 3b blocked on measurements, §16) -->
# The Claude seat path: one real Claude Code session per conversation, instead of a rebuilt transcript per turn

**Date:** 2026-11-05 · **Amends:** the router standard (`plans/2026-11-04-router-standard-design.md`) §5.5, "Why the
`claude` seat is a narrow translated target", and, where this design keeps provider state, §4.3 (state domains) and §6
(R5, pinning). **Code read:** llm-router `origin/main` `247b19f` (`packages/adapters/claude-sdk/index.ts`,
`packages/adapters/seat-host/index.ts`, `packages/plugins/provider-claude`, `packages/modules/inference`,
`packages/contracts`). **Conventions:** `plans/2026-11-05-router-part1-stream-inspectors-plan.md` §2.1.
**Status:** design only. Part 3a (§6) is small and can ship after Part 1; the rest at the priority the validation
lead's long agentic A/B sets.

**The owner's question:** why the Claude seat path is not a pass-through. **The hard constraint:** the seats are Claude
subscriptions, used only **through Claude Code**. The router never calls Anthropic's API with a seat's OAuth token and
never imitates Claude Code. A byte pass-through to Anthropic is therefore not available on seats, by policy; whether
API-key accounts are also used is open and this design does not assume either way (§10.1). The goal is the highest fidelity possible inside Claude Code. (Codex's review found no violation of
that constraint, direct or in spirit: Claude Code owns the seat's connection; the router supplies its inputs through MCP
and stdin; a declared import writes no signed state and does not claim it was produced by Claude Code.)

---

## 0. Summary

**Today** (the `sdk-resume` transport) every client turn is a new Claude Code process that resumes a **synthetic
transcript** the router writes from the client's history (`--fork-session --no-session-persistence --max-turns 1`,
hashed tool names, `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`). Claude Code never sees its own earlier output.

**What the validation lead measured on today's path** (2026-11-05, Fable on `claude-2`, the same tools code in both arms):
- **short tasks** (2 tasks × 2 ABBA pairs, 5 model calls, 7-10 tool calls): no measurable degradation (3/4 vs 3/4, the
  same failure; identical tool sequences; wall 0.91×). Layer (a) at step 4: hashed tool names ordered by hash; the
  environment note moved to the end with an empty system entry in its place; "Continue from where you left off."
  appended; the `<total_tokens>` reminders and the parallel-calls nudge missing; no thinking blocks (5,019 vs 5,550
  input tokens);
- **the long probe** (`textkit`, 15-40 dependent calls; pairs 3-6 still running, verdict pending), at a late step:
  19 signed thinking blocks direct, **0** via the router; 15 system entries direct, 2 via the router; 9,756 vs 8,652
  input tokens; and **the injected continuation text leaks into the output**: router-arm turns open with "Picking up
  where I left off…".

**The design, in three stages** (codex round 1's sequencing, adopted):
1. **Part 3a, now:** no injected text on today's path. The final user message, or the tool results, reach Claude Code as
   a `stream-json` message on stdin, and the transcript ends at the last assistant boundary. Gated by measurement M8 on
   both pinned versions and every trailing-input shape (§6).
2. **Part 3b:** one real Claude Code session per conversation **that carries an explicit `thread-id`**, with fixed
   settings, pinned to its seat. **Not yet safe to ship as specified** (codex round 2): four blockers remain open until
   the pinned-CLI measurements answer them (§16). Claude Code runs its own loop and keeps its own signed thinking,
   tool ids and reminders; the router delivers the client's tool results through its MCP bridge and new user messages
   on stdin. **On agent routes a lost session fails loudly with a remedy, never a silent rebuild** (the standard's §6
   R5: state is never stripped).
3. **Part 3c, later and only with evidence:** crash recovery with rewind, content-based identification for clients
   without ids (the reviewer bot), changing tool lists.

**Target:** at every step the request Anthropic receives is the same as direct Claude Code's, except for declared items
(§8), checked by a Layer (a) contract test at depth (15 steps) against an independent direct harness (§11).

---

## 1. The path today, precisely

`streamClaudeSdk` (`adapters/claude-sdk/index.ts:87-136`), per request: validates the history (`:45-84`); writes
`history.jsonl` in a fresh temp directory with fresh `uuid`s, a fresh `sessionId`, assistant messages with zero usage
and no thinking (`:117-126`); runs `claude -p … --resume <file> --fork-session --no-session-persistence --max-turns 1
--tools "" --strict-mcp-config --mcp-config <bridge> --allowedTools mcp__router__*` (`:127`). The runner adds
`CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` for every SDK launch (`adapters/seat-host/index.ts:113`) and writes an
initialisation message, not a user turn, to stdin (`:202`). The bridge serves `tools/list` under `t_<sha256/32>` names
(`claude-sdk:94`, `:108`) and answers every `tools/call` with `content: []` (`:109-110`). The runner stops after one
model call and maps `tool_use` blocks back to the client's names (`seat-host:157-161`); after a `max_tokens`,
`model_context_window_exceeded` or `refusal` stop it ends the run at once, because Claude Code would otherwise continue
on its own past `--max-turns 1` (`seat-host:58`, `:147`). Process slots are acquired per request, released after
reaping, with a 10-minute run timeout (`plugins/provider-claude/index.ts:137`).

---

## 2. The constraint, and what it allows

| Allowed | Not allowed |
|---|---|
| running Claude Code (the pinned versions) with the seat's own login, as today | calling `api.anthropic.com` with a seat's OAuth token from router code |
| Claude Code's own session files, written and resumed by Claude Code | the router writing requests "as Claude Code", or forging its client identity |
| the router as an MCP server Claude Code talks to; the router writing Claude Code's stdin | editing a session file Claude Code wrote |
| a **declared import**: the router writes a transcript of a client's history for a new session (as today's transport does), never with signed state, never presented as Claude Code's own output | inventing signatures or thinking; answering a tool call with content the client did not send |
| reading Claude Code's `stream-json` output | router-authored continuation prompts (§3.1) |

---

## 3. The session model (Part 3b)

### 3.1 What a client turn is

**The contract counts logical client turns.** One client request delivers its delta and receives the output of exactly
one model call; the router never lets Claude Code generate outside a client request. That is not enforced by withholding
tool results alone (codex round 1, finding 1): Claude Code also continues on its own after `max_tokens`,
`model_context_window_exceeded` and `refusal`, may compact, and may advance after a tool validation failure, a
permission denial or an MCP error. So every transition is specified:

| Event in the model call | Router action | The session afterwards |
|---|---|---|
| `end_turn` | finish `stop`; Claude Code awaits stdin | PARKED-USER |
| `tool_use` (all calls emitted, `message_stop`) | finish `tool_calls`; the bridge holds each MCP request as it arrives | PARKED-TOOLS |
| `max_tokens`, `model_context_window_exceeded`, `refusal` | today's early finish (the first response is the answer, with that finish reason); the process is stopped and confirmed gone before the client's response ends, so no continuation runs | CLOSED (resumable at that boundary, M5) |
| a second `message_start` the router did not ask for (a continuation, a retry, a compaction call) | the call is cut, the process stopped; `claude.session.protocol_violation` | CLOSED-UNSAFE: not resumed (agent routes refuse with a remedy) |
| a tool error Claude Code produced itself (validation, permission, an MCP error) | the same as an unrequested call: the bridge answers only client results, and anything else is a violation | CLOSED-UNSAFE |

**Detection is not prevention** (codex round 2, finding 1, blocker): a second `message_start`, or a stop after the first
`message_stop`, is evidence that Claude Code already sent another request to the seat. Stopping the process then does
not enforce "exactly one model call". The bridge is a real **pre-dispatch barrier** only for tool turns (Claude Code
cannot call the model again before the MCP results it waits for). For the other continuations (after `max_tokens`,
`model_context_window_exceeded`, `refusal`, or a compaction call) Part 3b needs a CLI mechanism that stops the next
request **before** it is dispatched, on both pins (measurement M14, counted at the loopback Anthropic, including
requests that never produce a `message_start`). Without one, the guarantee is restated as "one model call per tool turn;
on an early finish at most one further call, which the router does not deliver and which closes the session as
CLOSED-UNSAFE", and that weaker guarantee is the owner's decision. Unknown stop reasons are CLOSED-UNSAFE.

Automatic compaction is turned off in the session's settings (measurement M9 confirms the setting holds on both pins);
a client's own compaction arrives as a new history, which is a divergence (§4.2). **"No injected text"** means no
router-authored prompt; Claude Code's own reminders, nudges and environment note are what a direct run has, and stay.

### 3.2 States

```
   new conversation with an id ──► RUNNING ──end_turn──► PARKED-USER ──new user messages──► RUNNING
                                     │                        │
                                     └──tool_use──► PARKED-TOOLS ──all client results──► RUNNING
                                                              │
           idle past the live TTL, memory pressure, a router restart, an early finish
                                                              ▼
                                   CLOSED (Claude Code's session file kept) ──next request──► RESUMED → RUNNING
   a violation, an uncertain crash state, a failed resume ──► CLOSED-UNSAFE ──► agent: refuse with remedy; chat: declared import
```

### 3.3 The tool bridge

- **Names** `mcp__tools__<name>`: the server is named `tools`, and `<name>` is the client's function name when it matches
  `[A-Za-z0-9_-]` and the whole name fits the 64-character limit; otherwise a sanitised prefix plus `_` and 8 hex
  characters of its SHA-256, declared (`claude.tool_renamed`, once per session).
- **Order:** the client's order in `tools/list`.
- **Correlation** (finding 2): each MCP `tools/call` is matched to its Anthropic `tool_use` by the id Claude Code passes
  in the request's metadata (`_meta`, expected `claudecode/toolUseId`; measurement M10 on both pins). **Never by name,
  arguments or arrival order:** two identical parallel calls exist in the repository's real-CLI fixture
  (`tests/claude-sdk-real.test.ts:66`). Without a verified correlation id on a pinned version, Part 3b does not ship.
- **Three sets per model call:** the calls the model emitted (from the stream), the MCP requests Claude Code dispatched,
  and the client's results. The router returns the emitted calls to the client at `message_stop`; it accepts the
  client's next request only with a result for **every** emitted call; it answers each MCP request **as it arrives**
  (Claude Code may dispatch them one at a time, so waiting for all requests before answering could deadlock).
- **Timers:** Claude Code's HTTP MCP client has its own request and idle timeouts (by its documentation, 60 s per request
  and 5 minutes idle by default; `MCP_TOOL_TIMEOUT` and the related settings). The session sets them above the parked
  TTL, and measurement M1 confirms on both pins that a parked call survives the whole TTL with no continuation.
- **The whole batch first:** the router validates the client's entire result batch (one result per emitted call, no
  duplicate and no unknown id) before it answers any MCP request; the parked TTL includes the request's time in the
  admission queue. **A delta of results followed by user text is refused in Part 3b** (a narrow delta contract):
  answering the last MCP call and then writing user text to stdin would race the next generation (round 2, finding 2).
- **Results** carry the client's text. Claude Code may store a large MCP result as a file reference instead of inline;
  measurement M11 pins the size at which that happens, and a result above it is refused with the reason (a declared
  limit), not silently transformed.
- **Closing:** before the router abandons parked calls (TTL, shutdown), it stops the process and confirms it gone, so a
  failed call cannot provoke a continuation.
- **The tool list is fixed for the session** (Part 3b). A request whose tools differ is a divergence (§4.2). Changing
  lists (`notifications/tools/list_changed`, M4, verified refresh) is Part 3c.

### 3.4 The CLI runner

- **Argv** (new transport `session`; `print` and `sdk-resume` stay): `-p --input-format stream-json --output-format
  stream-json --verbose --include-partial-messages --model <m> --session-id <uuid>` (or `--resume <uuid>`) `--tools ""
  --strict-mcp-config --mcp-config <bridge> --allowedTools mcp__tools__* --disable-slash-commands --setting-sources ""
  --settings <fixed: hooks off, auto-compaction off>` and the system prompt. Gone: `--fork-session`,
  `--no-session-persistence`, `--max-turns 1`, `CLAUDE_CODE_RESUME_INTERRUPTED_TURN`.
- **One stable working directory per session** (Claude Code names it in its environment note and keys its files by it).
- **The seat's own config directory and login,** the env allowlist, process-group reaping and bounded lines, as today.
- **Fixed settings** (Part 3b): model, effort, output limit, system prompt and tool list are bound to the session at
  creation; a request that changes any of them is a divergence (§4.2), not a silent respawn (codex round 1, finding 8).

---

## 4. Identity, verification and the ledger

### 4.1 Which session a request belongs to (finding 5)

- **Part 3b requires a `thread-id`:** the header the router already reads for placement
  (`modules/protocol/index.ts:309`), hashed as today. `prompt_cache_key` is a cache hint that clients may reuse across
  conversations, so it does not identify one (round 2, finding 5). The id is **client-supplied**, scoped by the
  authenticated principal; it is not authenticated provenance. A request without one uses today's transport (with
  Part 3a's fix); the reviewer bot sends none, so it stays there.
- **The key** is `(namespace, principal, conversation id)`, from the engine's authenticated principal, never from the
  content.
- **Content is a check, not an identity:** each message of the client-visible history gets a **versioned canonical
  encoding** (unambiguous framing; message and block order kept; the Responses and chat codecs' normalisation of
  assistant grouping, null content, text blocks and argument strings defined; text itself never normalised), and the
  history a chain `d_i = HMAC(key, d_{i-1} ‖ enc(m_i))` (keyed, so a predictable history does not give a predictable
  digest; the key is a router secret). The ledger stores the chain at each tip the router returned.
- Part 3c may add content-based lookup for clients without ids; it returns zero, one or several candidates and never
  picks on ambiguity.

### 4.2 Verification and the delta

The client's history must equal, message for message, what the session has seen up to its recorded tip, followed by a
valid delta: exactly one result per emitted call (PARKED-TOOLS), or user messages (PARKED-USER; several in one delta are
delivered in order, M3). Anything else is a **divergence**: an edited or removed message, a different result for an
answered call, a truncated history (a client's compaction), a changed tool list or setting, a delta of the wrong kind.
A divergence never edits the session. What follows it is §7.

### 4.3 The ledger (finding 8)

One row per session, in the router's SQLite store behind a narrow port implemented by `adapters/sqlite`:
`{ sessionId, key (namespace, principal, conversation), seat: { accountId, identity, credentialOwner,
credentialGeneration }, model, cliVersion, formatVersion, settingsFingerprint, toolMapFingerprint, cwd, epoch, revision,
tip, emittedCalls, state, attempt: { id, expectedRevision, delivery: DeliveryState },
lastCliMessageUuid, createdAt, lastUsedAt }` plus the tip chain. Digests and ids only; the content is in Claude Code's
session file (§9).
- **`DeliveryState`** (`contracts/claude-session.ts`), the one type for this field, kept per stdin message and per MCP
  answer, and for the attempt as the worst of them: `none` (nothing reached the process), `partial` (some of it did, or
  a write's result is unknown), `complete` (all of it did). Only `none` permits a replay (§5.1); `complete` permits the
  next step of §5.3; `partial` is CLOSED-UNSAFE.
- **The seat binding:** a credential rotation for the same identity keeps the session but stops its parked process
  (a running process cannot take the new credential; the session resumes on the next request with it); a different
  identity on the same account id (a re-sign-in to another subscription) makes it unusable
  (`claude.session.seat_changed`); a revocation stops every process bound to the credential at once.
- **The settings fingerprint** covers the **effective** controls (round 2, finding 8): the resolved model the CLI reports
  in its init event (an alias such as `fable` can move between launches), effort, output limit, system prompt, the tool
  list **and `tool_choice`** (`none` changes the offered tools, `claude-sdk:95`), and the tool-name map, which must be
  injective (a generated name may not collide with a valid client name).
- **Authorisation is rechecked on every request:** a session confers no access to a route or account the principal has
  lost.

---

## 5. Concurrency, admission and lifetimes

### 5.1 One request per session (finding 6)

A request's **revision is derived, not sent:** it is the revision of the recorded tip its history uniquely matches
(§4.2), so an unmodified Codex client needs no new field; a history that matches no tip, or more than one, is a
divergence. Each request also carries a fingerprint of its delta and settings. Under the session's lock:
- **another request is running from the same revision:** if its client is still connected, the new one gets a 409
  `CONVERSATION_BUSY` (an overlapping duplicate is not proof that the first client went away); if the first client has
  disconnected, the first call is cancelled and confirmed stopped, and the new request proceeds **only if the ledger
  proves the first attempt delivered nothing** (no stdin write, no MCP answer: delivery state `none`). Otherwise the
  session is CLOSED-UNSAFE: a cancellation does not undo consumed input (round 2, finding 6);
- **the session has already moved past the request's revision** (the client lost the response and resends): this is a
  fork. Part 3b refuses it on agent routes with a remedy (`codex resume` from the last answer, or a new session);
  rewinding with Claude Code's own resume-at-message option (the Agent SDK documents `resumeSessionAt`; the CLI's
  equivalent on the pins is measurement M6) is Part 3c.
- The request revalidates everything after it takes the lock.

### 5.2 Admission and process lifetimes (findings 7, 10)

Three lifetimes, three owners:
- **session ownership** (the session controller, keyed by `sessionId`);
- **resident-process permits** (one per live Claude Code process, held while it is parked, released only when the process
  is confirmed reaped; a pod-wide and a per-seat cap);
- **inference admission** (the engine's account slots, as today), acquired by each request **before** the router answers
  an MCP call or writes stdin, released when the model call ends.

A parked process holds a permit but no admission slot, so it does not count against the account's concurrency while it
waits, and an admitted request never waits for its own session's permit.

**Placement before selection** (round 2, finding 7): the engine asks a `ContinuityPort` before it selects a target, and
gets a **decision**: `new` (with an atomic claim on the conversation key, so two simultaneous first requests cannot
create sessions on two seats), `pinned { accountId, transport, revision, epoch }`, or `refused { reason }` (lost,
tombstoned, diverged, unsafe). The engine enforces it on **every attempt and fallback**: an agent route waits in the
pinned account's admission queue or fails loudly; a chat route may move only where §7 allows. An unavailable
continuity service is `refused`, never `new`. The provider sees a **trusted invocation scope** (the authenticated
principal, the conversation key, the route's kind, the decision) that `BoundInvocation` does not carry today; without
it the session transport refuses.

**Acquisition order** (round 2, finding 10: one permit and one slot could otherwise deadlock): a request that needs a
**new** process acquires the resident permit first, then account admission; a parked continuation already holds its
permit and acquires only admission; no request holds account admission while it waits for a permit. Permits are shared
across provider generations and probes, released only after confirmed reaping, and a successful HTTP response's
cancellation is detached from the parked session's lifetime.

**Memory:** the router's own comments estimate ~310 MiB for the router and ~210 MiB per Claude Code process; the
checked-in deployment limit is 768 MiB and the live one 1280 Mi. The caps are sized from measured peaks at depth (M12),
and the roll that enables the transport sets the pod's limit from them. Under pressure the oldest PARKED-USER session
closes first (it resumes cleanly); a PARKED-TOOLS session is closed only at its TTL.

### 5.3 Restarts and plugin lifecycle

**The recovery protocol** (round 2, finding 9, blocker), in order for every delivery:
1. durable **intent** (attempt id, expected revision, what will be delivered) before anything reaches the process;
2. **delivery tracking** per stdin message and per MCP answer (`DeliveryState`, §4.3);
3. a **verified completed boundary**: after the model call ends, the router reads the session file's tail and checks it
   holds exactly the turn it streamed (message ids, tool-use ids, the stop), not just a uuid;
4. an atomic **successor checkpoint** (revision + 1, the new tip) in the ledger;
5. only then the finish reaches the client (no replayable output before the checkpoint).

A router restart ends live processes. On start, before any input reaches a session, the controller compares each
ledger row with the session's artifacts: a session whose file ends at its checkpointed boundary is CLOSED (resumable);
anything else (an intent without a checkpoint, a partial delivery, a file that disagrees, a ledger write that failed) is
CLOSED-UNSAFE. Early finishes and TTL-killed parked sessions are classified the same way. What an interrupted file
contains on each pin is measured now (M6); only rewinding is deferred. Disabling the provider plugin stops new sessions,
drains running calls within a bound, stops parked processes, and leaves the files for retention, which keeps access
through a maintenance port that survives the plugin's disposal.

---

## 6. Part 3a: no injected text on today's path

The leak of §0 does not wait for sessions. Today's launch makes Claude Code answer a transcript whose last row is the
user's message by setting `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`, and Claude Code appends "Continue from where you left
off.". Instead:
- **a first request** (no assistant turn in the history) is a fresh launch with the user message on stdin, no resume
  file;
- **a later request** writes the transcript up to the **last assistant boundary**, resumes it, and sends everything
  after that boundary on stdin as `stream-json` messages: a user message; the tool results of the last assistant
  turn's calls; the tool results followed by more user text;
- the runner no longer sets `CLAUDE_CODE_RESUME_INTERRUPTED_TURN` and sends the user message instead of an
  initialisation message (`seat-host:113`, `:202`).

**Construction chosen before launch, by a qualification matrix** (round 2, finding 3): for each pinned version and each
trailing-input shape, the qualified construction is recorded; a shape not qualified on a version keeps today's
construction **declared** (`x-router-mutations: claude-legacy-resume`, journaled), chosen before the process starts. The
router never tries the new construction and then retries the old one after a failure that may have consumed input. A
release with any legacy cell is described as "no injected text except the listed shapes", never generally.

**Gate (M8), on 2.1.281 and 2.1.283 separately (2.1.318's help is a lead, not a qualification), each shape:**
first requests with one or several user messages; a completed assistant history followed by one or several user
messages; one unresolved call; several calls, identical names and arguments included; complete results followed by
more user text or messages; with supported system content, text-block forms, empty results where accepted, Unicode and
order preserved. It must show **exactly one generation consuming the complete delta** (upstream requests counted at the
loopback Anthropic, silent continuations included; several stdin messages produce one generation and leave nothing
queued); no empty resume file on fresh requests; the transcript ending at the intended assistant boundary; every later
input delivered once, results still attached to their calls; no repair result, placeholder, dropped or duplicated input
or continuation text (Claude Code's own reminders told apart from client content); correct terminal behaviour (text and
tool finishes, output-limit, context-limit and refusal stops, cancellation, usage, confirmed process exit); and equal
client-visible text, tool ids, argument strings and finish reasons through both front doors. Then the live A/B,
including the long probe's leaking step: the recorded input without the injected prompt is the decisive evidence, the
absence of "picking up" prose supporting. Also each of the following: exactly one model call; the recorded Messages body ends with
the client's input, unaltered, with no repair placeholder, no dropped or duplicated message and no continuation text;
the answer does not open with a "picking up" phrase on the long probe's step. **The case that may not work** is a
transcript ending with an assistant `tool_use` whose results arrive on stdin (the unanswered-tool resume, M2): if a pin
rejects or repairs it, Part 3a keeps today's construction for that one shape on that pin (declared), and only the shapes
that pass change. Part 3a is A/B-gated like any release: it changes what Anthropic receives on every Claude seat turn.

---

## 7. When a session cannot continue (finding 4)

| Situation | Agent route | Chat route |
|---|---|---|
| no session yet, history without assistant turns | new session | new session |
| no session yet, history with assistant turns | **refused**, unless the account explicitly allows imports (`claudeSession.importHistory: true`): a missing ledger record is not proof that no state existed (a deleted row, an expired tombstone, a restored backup) | declared import |
| the session is lost or unsafe (CLOSED-UNSAFE, evicted with a tombstone, a failed resume, a seat change) | **refused**, `CLAUDE_SESSION_LOST`, naming the remedy (start a new session; Codex: a new `thread-id`) | declared import (`claude.session.rebuilt {reason}`), disclosed in `x-router-mutations` |
| a divergence (§4.2) | refused, `CLAUDE_SESSION_DIVERGED`, naming what diverged | declared import |
| its seat is unavailable | waits in the seat's queue, then fails loudly (the standard's pin) | may move and import |

An import writes the client's history up to the last assistant boundary (Part 3a's construction) for a new session id,
with no signed state, and delivers the rest on stdin.

**No escape paths** (round 2, finding 4, blocker):
- **a transport rollback** (the plugin back to `sdk-resume`, the account's opt-in removed, the plugin disabled) stops
  creating sessions; an existing session's conversation either continues on a compatible implementation or is refused
  on agent routes, never served by the rebuild path, which would strip its state;
- **foreign or unknown state is judged before lossy adaptation:** the Codex adaptation drops encrypted reasoning
  (`protocol/index.ts:192`), so by the time a request reaches the provider a GPT turn's history looks stateless; the
  front door records the facts (encrypted items present) before adapting, and the continuity decision reads them;
- **retention never turns a refusal into an import:** a tombstone outlives the content (§9). **This amends the standard** §4.3 (a Claude seat conversation
holds `anthropic`-domain state inside Claude Code's session, produced and replayed by Claude Code) and §6 (R5: agent
routes never strip it; the import is permitted only where no such state existed).

---

## 8. What stays different from direct Claude Code (declared)

| Item | Why |
|---|---|
| The MCP server name `tools` | the router serves the tools |
| The session's working directory in the environment note | one stable directory per session |
| `metadata.user_id` | Claude Code's own per-session value; differs between two direct runs too |
| A renamed tool (`claude.tool_renamed`) | a name outside Anthropic's rules |
| Everything before an import point | §7 |
| Thinking is not returned to the client | the seat transport returns text and tool calls; the thinking stays in the session, which is what keeps it valid |

Images and schemas stay refused (`UNSUPPORTED_SEAT_REQUEST`).

---

## 9. Content storage, retention and bounds (finding 11)

**Session files are conversation content:** full transcripts, signed thinking and tool results, written by Claude Code
in plaintext on the router's data volume. That is a new storage policy, separate from the bounded diagnostic capture,
and needs the owner's decision on retention. Proposed:
- **where:** a dedicated directory per seat on the router's PVC, mode 0700, owned by the router's user, excluded from the
  database backup job (the transcripts are not router state), on the volume's existing at-rest protection;
- **retention:** 24 hours idle by default (a resumed Codex session after a day off then needs a new session: an agent
  route refuses with the remedy), configurable up to the ledger's 30 days by the owner;
- **bounds:** session count and bytes per principal, per seat and per pod, **including active growth** (a live session
  whose file, artifacts or tip chain passes its per-session limit is closed as CLOSED-UNSAFE with
  `claude.session.limit_reached`); at a bound, new sessions are refused (agent routes) or the least recently used closed
  conversation is evicted with a **tombstone**;
- **tombstones outlive content:** a tombstone (key, epoch, reason, time) is kept for the ledger's retention (30 days)
  after the content is deleted, so an expired agent conversation is refused with a remedy, never imported silently;
- **backups:** the ledger is in the router's database backup and the transcripts are not, so a restore makes every
  session whose file is missing CLOSED-UNSAFE at reconciliation (§5.3);
- **at-rest protection is not established:** the repository's encryption covers its own capture, not files a CLI
  writes; the volume's protection is the estate's iSCSI storage. Storing plaintext transcripts there is an explicit
  owner decision (§13);
- **deletion:** under the session lock, mark deletion intent in the ledger, stop any writer, delete the inventoried paths
  (the session file, the working directory, and every artifact Claude Code created for the session in the shared seat
  configuration directory, inventoried by measurement M13),
  then remove the row; a failed step is reconciled on the next pass;
- **revocation:** a principal's revoked key or a removed account deletes its sessions.

---

## 10. Architecture

Following the conventions (Part 1 §2.1). **No new extension point:** the provider registry already holds transports;
the session path is a transport of `provider-claude`.

| Unit | Kind | Single responsibility | Public interface | Depends on |
|---|---|---|---|---|
| `contracts/claude-session.ts` | contracts | the shapes the engine and the store see | `SessionKey`, `SessionRecord`, `DeliveryState`, `SessionLedgerPort` (transactional: `get`, `compareAndSet(revision)`, `tombstone`, `list(expiredBefore)`), `ContinuityPort` (`decide(key, route) → ContinuityDecision`: `new { claim }`, `pinned { accountId, transport, revision, epoch }` or `refused { reason }`, §5.2; an unavailable service is `refused`) | contracts |
| `contracts/index.ts` (changed) | contracts | `BoundInvocation.scope?: { principalId, namespace, conversation?, routeKind, decision }` (`routeKind`: `RoutePolicy.kind`; `decision`: the `ContinuityDecision`, §5.2), set by the engine only | | |
| `modules/claude-sessions/` | pure | the state machine; the canonical encoding (hashing injected); verification and the delta; the §7 decisions | `encode(history)`, `verify(record, chain, history) → { delta } \| { diverged }`, `next(state, event)`, `decide(route, situation)` | contracts |
| `adapters/sqlite` (changed) | adapter | the ledger tables and migration, behind `SessionLedgerPort` | | |
| `plugins/claude-sessions/` (new) | service plugin | the session coordinator: the ledger port, claims, continuity decisions, reconciliation, retention through the maintenance port; stays active when `provider-claude` is disabled | provides `ContinuityPort` | the ports, `modules/claude-sessions` |
| `adapters/claude-code/session-controller.ts` | adapter | owns one session: the runner, the bridge, imports, locks, lifetimes; acts on the `ContinuityDecision` in the trusted scope, and refuses without one | `SessionController` | the leaves below, the ports |
| `adapters/claude-code/cli-runner.ts` | adapter (leaf) | a long-lived Claude Code process: spawn, stdin, `stream-json` events, stop and confirm | internal | |
| `adapters/claude-code/tool-bridge.ts` | adapter (leaf) | the loopback MCP server: names, order, correlation, parked requests | internal | |
| `adapters/claude-code/transcript-writer.ts` | adapter (leaf) | imports only (Part 3a's construction) | internal | |
| `plugins/provider-claude/` (changed) | provider plugin | the `session` transport, account opt-in, account-aware capability; passes the decision to the controller | descriptor | the controller |
| `modules/inference/` (changed) | pure | asks `ContinuityPort` before selection and enforces its `ContinuityDecision` on every attempt and fallback (`refused` fails, `pinned` selects only that account); passes the trusted scope with the decision | engine option `continuity?` | contracts |
| `plugins/retention/` (changed) | plugin | expiry and deletion through the maintenance port | | the ledger port |

**Config** (round 2, finding 12): per account `claudeSession?: { enabled: boolean, importHistory?, liveUserTtlMs?,
liveToolsTtlMs?, maxLiveSessions?, retentionHours? }`, its shape in contracts, its defaults and validation in
`modules/claude-sessions` (Part 1 §2.1). **Precedence:** `enabled: false` is a real off, whatever the plugin's
`transport`; `enabled: true` uses the session transport for requests with a `thread-id` on that account; absent = the
plugin's transport as today. Default-off. **Prerequisites the module map now includes:** a route kind
(`RoutePolicy.kind?: 'agent' | 'chat'`, which the standard's pinning rules need and today's config lacks);
account-aware capability resolution in the provider registry (support is provider-wide today,
`runtime/provider-registry.ts:36`); the runtime schema and the router-engine composition for `ContinuityPort` and the
trusted scope. **Ownership:** a provider-independent **session coordinator** (global: the ledger port, claims,
continuity decisions, reconciliation, retention through a maintenance port) stays active when `provider-claude` is
disabled; a **session controller** per session owns its process, bridge and imports.

**Observability** (in the shared catalog): journal `claude.session.created`, `.resumed`, `.parked`, `.imported`,
`.rebuilt`, `.diverged`, `.protocol_violation`, `.seat_changed`, `.evicted`, `claude.tool_renamed`; runtime
`claude.session.closed`, `claude.session.reconciled`; metrics `llm_router_claude_sessions{seat,state}`,
`llm_router_claude_session_outcomes_total{seat,outcome}`, `llm_router_claude_park_seconds`, the cache share of input
tokens per seat. The digests and the conversation key never reach telemetry.

### 10.1 Where an API-key Claude provider plugs in (not assumed either way)

Whether Claude is also served with an API key is the owner's open decision. This design holds in both cases, and the
seats keep their constraint in both: an API key is a separate credential on a separate account, never a route around
Claude Code for a seat. The router already has the slot: the `anthropic` provider kind (`plugins/provider-anthropic`,
`access: 'api'`, `adapters/providers/anthropic.ts`), a sibling of `provider-claude`. A `claude-api` provider is that
kind, or another entry beside it in the provider registry; no new extension point either way.

- **Session model (§3 to §5):** does not apply. An API account has no Claude Code process to keep alive; each request
  carries the client's history, so there is nothing to park, import or verify, and `claudeSession` is refused on it.
- **Fidelity:** belongs to Part 2's native wires. A byte pass-through to the Messages API would be a wire with its own
  qualification manifest, not a change to this path.
- **The session coordinator (§10):** already provider-independent. `ContinuityPort` is the one touch point: a thread
  held by a seat session is held to that seat, and a route that mixes seats and API accounts is a §7 situation that
  `decide(route, situation)` answers, like any other target that cannot continue the session. No silent move between
  them, because the two do not share signed thinking or cache.
- **Part 3a and the Layer (a) contract test:** unchanged while any route uses a seat. The test's direct recording is
  the same reference for both.

If the owner chooses API keys for the agent routes, the demand for Part 3b falls with it; §13's order is the place to
change, not the architecture.

---

## 11. Tests (finding 13)

- **Unit** (`modules/claude-sessions`): the encoding (versioned, framing, every codec normalisation), chains, every
  divergence class, deltas, the state machine including every row of §3.1 and cancellation, the §7 table.
- **Contract, with the real pinned CLI against a fake Anthropic** (a loopback server that records each Messages request
  and replies with scripted SSE; `ANTHROPIC_BASE_URL` points at it; no seat token is used), compared with an
  **independent direct harness** (Claude Code with the same tools behind an MCP stub named `tools`, the same order,
  written separately from the router's code):
  - **Layer (a) at depth:** a 15-step dependent tool loop, a 5-step loop and a 3-turn chat; for every step N the two
    recorded request bodies are equal after normalising only §8's items (narrowly specified: the cwd string,
    `metadata.user_id`), with tool and block order, signatures, reminders and results compared exactly;
  - **independent assertions:** the number of upstream requests per client turn (exactly one), every result delivered
    to its own call, no input dropped or duplicated, the mutation header;
  - **cases:** cancellation after a partial result delivery; exhausted resident capacity; a transport rollback; the
    provider disabled; a tombstone's expiry; a restored backup; foreign state removed by the front door's adaptation;
    M2 with files produced by **real parked-process shutdown**, not only hand-written transcripts; duplicate parallel
    calls; serialised dispatch; results delayed past the MCP timeouts' defaults; a
    transport disconnect; a changed tool list or setting (a divergence); a large result (M11); auto-compaction off
    (M9); crash cut points (before and after stdin delivery, before and after Claude Code's write, before and after the
    ledger commit);
  - **through both front doors** (`/v1/responses` and `/v1/chat/completions`) and their codecs;
  - **a negative control:** today's synthetic path fails the continuity assertions (thinking carried, reminders present).
  The fake's signed blocks test that the bytes are carried, not that a signature is valid: the live check is the gate.
- **Live** (the validation lead, the release gate): the long agentic probe and the Layer (a) recording on a seat,
  direct vs the session path, short tasks first.

## 12. Measurements (each on 2.1.281 and 2.1.283, the pinned CLI against the fake Anthropic)

| # | Question | Gate |
|---|---|---|
| M1 | A parked `tools/call` survives the whole live TTL (with the timeout settings raised), with no continuation | Part 3b blocker (a long run, not a short one) |
| M2 | A resumed session ending in an unanswered `tool_use`: is a stdin `tool_result` accepted without injected text? | blocker for recovering tool sessions; failure limits Part 3b to live parking, fail-closed |
| M3 | Several user messages in one stdin delta; a stdin message while a call runs (the controller never sends one) | safety |
| M4 | `tools/list_changed` honoured, with a verified refresh | Part 3c |
| M5 | The request after `--resume` is unchanged except declared items | required for resume |
| M6 | After a cut call: what the session file holds; a resume-at-message option on the CLI | Part 3c (Part 3b refuses forks) |
| M7 | Earlier signed thinking blocks are sent again on later calls, live and after a resume | Part 3b blocker |
| M8 | Part 3a's shapes (§6) | Part 3a blocker |
| M9 | Auto-compaction off holds | Part 3b blocker |
| M10 | The `tools/call` correlation id (`_meta` `claudecode/toolUseId`) | Part 3b blocker |
| M11 | The MCP result size above which Claude Code stores a file reference | declared limit |
| M12 | Peak memory per process at depth, and with N parked | sizing |
| M13 | Every artifact Claude Code creates in a session's directory and config | deletion inventory |
| M14 | A CLI mechanism that stops the next model request **before dispatch** after an early finish (or the weaker guarantee of §3.1) | Part 3b blocker |

## 13. Consumers and rollout

- **Part 3a** first, its own small PR, gated by M8's matrix and the A/B (it changes every Claude seat turn).
- **Owner decisions** before Part 3b: the plaintext transcript storage and its retention (§9), and, if M14 finds no
  pre-dispatch barrier, the weaker early-finish guarantee (§3.1).
- **Part 3b:** Codex on dev-worker-4 (an agent loop with Fable: 129 requests in 7 days; it sends `thread-id`) on one
  seat, default-off, gated by Layer (a) and the long probe; `sdk-resume` stays the per-account rollback.
- **The reviewer-claude bot** (1,000+ requests in 7 days, chat completions, no conversation id) stays on `sdk-resume` with
  Part 3a's fix, unless its continuity gain is shown to justify persistent state.

## 14. The amendment to the standard

**§5.5**, replaced: *The Claude seat is used through the Claude Code CLI, the sanctioned client for a subscription. For a
conversation with an explicit id, the router keeps one real Claude Code session, pinned to its seat: Claude Code runs its
own loop and keeps its own signed thinking, tool ids and reminders; the router delivers the client's tool results through
its MCP bridge and new messages on stdin. A conversation without an id stays on the per-request transport and never gets
a session. A conversation with an id first seen with assistant history is refused on agent routes unless the account
allows imports (`claudeSession.importHistory: true`); a chat route imports it, declared. It is not a byte pass-through and cannot be one: the seat's token is used only by
Claude Code. Images and schemas stay refused until qualified.*
**§4.3**, added: the `anthropic` domain includes **server-held** state: the state inside a seat's Claude Code session,
which the client never replays. Pinning applies to it even when a request carries no reasoning payload, which is the
exception to the standard's client-replayed-state assumption.
**§6 R5**, added: on agent routes a lost, expired or diverged session is refused with a remedy, never stripped by a
rebuild, a transport rollback or a retention expiry; an initial import is a separately authorized account setting (`claudeSession.importHistory`).
**§5.5's prohibition is kept:** the Claude seat is never a failover target for a conversation holding another domain's
state, judged on the request's facts before any lossy adaptation; persistent sessions (opt-in, with a `thread-id`) are
distinguished from disposable per-request runs (everything else).

## 15. Codex cross-validation

**Round 1** (`codex exec --profile review`, gpt-6-astra, xhigh, read-only, on the first draft): **request changes**, 6
blockers, 7 high; no policy violation; Part 3a worth shipping separately with a broader M8.

| # | Finding | Disposition |
|---|---|---|
| 1 | Blocker: withholding MCP results does not enforce one model call per turn (early-finish continuations, compaction, tool errors) | Every transition specified; unrequested calls are violations; auto-compaction off (M9); "no injection" defined (§3.1) |
| 2 | Blocker: MCP timeouts; correlation by request metadata, not order; serial dispatch | Timer settings and M1; `_meta` correlation (M10) as a ship gate; three sets; answer as requests arrive; stop before abandoning (§3.3) |
| 3 | Blocker: the unanswered-tool fallback is circular; Part 3a shares it; the runner injects too | Part 3a gated per shape on both pins, a fresh launch for first requests, the runner's injection and init removed; M2's failure keeps today's construction for that shape and limits Part 3b to live parking (§6, §12) |
| 4 | Blocker: automatic rebuilds strip agent state (§6 R5) | Agent routes refuse lost or diverged sessions with a remedy; imports only where no state existed; the standard amended (§7, §14) |
| 5 | Blocker: content digests are not identity | Explicit conversation ids for Part 3b; content as a keyed check; ambiguity never resolved by guessing (§4.1) |
| 6 | Blocker: a same-tip request does not prove the first client left | Revisions and delta fingerprints; 409 while the first client is connected; forks refused on agent routes (§5.1) |
| 7 | No trusted scope in `BoundInvocation`; continuity decided too late | A trusted scope; `ContinuityPort` consulted before selection (§5.2, §10) |
| 8 | The ledger lacks seat identity, model, settings, epoch bindings; keyed digests | §4.3; fixed settings (§3.4); HMAC chains (§4.1) |
| 9 | No crash consistency | Attempt intent, expected revision, delivery state, CLI uuids, reconciliation before input (§4.3, §5.3) |
| 10 | Process permits, admission and session lifetimes conflated; memory | Three lifetimes and owners; sizing from measured peaks (§5.2, M12) |
| 11 | 30-day retention of full transcripts is a new content policy; bounds incomplete | A storage policy for the owner (24 h default), bounds, tombstones, a deletion protocol (§9) |
| 12 | Module contracts incomplete; pure module cannot hash; SQL in the adapter; no new registry needed | §10: hashing injected, the ledger behind a port in `adapters/sqlite`, one controller owning the leaves, a maintenance port; the citation fixed |
| 13 | Layer (a) necessary, not sufficient | An independent direct harness, narrow normalisation, independent assertions, the case list, both front doors, a negative control, the live gate (§11) |

**Round 2** (on the revision): **request changes**; Part 3a suitable for a separate, conditional release; Part 3b not
yet safe as specified; no policy violation.

| # | Finding | Disposition |
|---|---|---|
| 1 | Blocker: detecting a second request is not preventing it | The tool-turn barrier is real; for early finishes a pre-dispatch mechanism (M14) or an explicitly weaker guarantee for the owner (§3.1) |
| 2 | The bridge: whole-batch validation, queue time, results plus user text | Batch validated first; TTL includes the queue; results plus text refused in 3b (§3.3) |
| 3 | Part 3a's fallback still injects; choose before launch | A version-by-shape matrix chosen before launch; legacy cells declared; no retry into another construction; M8 detailed (§6) |
| 4 | Blocker: rollback and import escape paths; foreign state hidden by adaptation | Rollback never serves an established session by rebuild; imports a separately authorized setting; foreign state judged before adaptation; tombstones outlive content (§7, §9, §14) |
| 5 | `prompt_cache_key` is not a conversation id | `thread-id` only; client-supplied, principal-scoped (§4.1) |
| 6 | Blocker: a disconnect does not authorize replay; where revisions come from | Proceed only when nothing was delivered; revisions derived from the matched tip (§5.1) |
| 7 | `heldBy` too weak; atomic claims; unavailable service | A decision type with claims and tokens, enforced on every attempt; unavailable = refused (§5.2) |
| 8 | Effective settings, `tool_choice`, moving aliases, credentials | The effective-settings fingerprint; credential rotation and revocation (§4.3) |
| 9 | Blocker: no recovery protocol | Intent, per-delivery tracking, a verified boundary, an atomic checkpoint, then exposure; reconciliation fails closed (§5.3) |
| 10 | Permit and admission ordering can deadlock | Permit first for new processes; no admission held while waiting for a permit (§5.2) |
| 11 | Storage bounds, tombstone lifetime, at-rest, backups | Active-growth limits, tombstones for 30 days, at-rest an owner decision, restore reconciliation (§9) |
| 12 | Config precedence, maintenance owner, account-aware capability, route kind | §10's precedence, the session coordinator, the prerequisites listed |
| 13 | Tests (resolved); add the failure cases | §11's cases extended |

## 16. Open before Part 3b can be implemented

1. **M14 or the weaker guarantee** (§3.1): a pre-dispatch barrier for early finishes, or the owner's acceptance of "at
   most one undelivered further call, then CLOSED-UNSAFE".
2. **M1, M2, M5, M6, M7, M9, M10** on both pins (§12), M2 on files from real parked-process shutdown.
3. **The owner's storage decision** (§9).
4. **The route kind** (`RoutePolicy.kind`) and account-aware capability resolution, as their own small changes.

Part 3a has none of these dependencies beyond M8 and can be implemented now.

**Round 3:** after the measurements.
