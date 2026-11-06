# router-ab: the llm-router release gate

This gate checks that llm-router does not change what a model receives, or degrade how it answers, for any provider.
Each provider runs the same short tasks **direct** (the client to the provider) and **via the router**, in ABBA pairs,
and the results are judged against the non-inferiority margins of the router standard, §9.6
(`plans/2026-11-04-router-standard-design.md`).

Run it after every router image roll, and before every config change that touches a route. The procedure is in
`docs/runbooks/llm-router.md`, § "Release gate (router-ab)". The method and the first results are in
`plans/2026-11-05-router-transparency-and-planning-revalidation.md`.

```sh
node scripts/router-ab/gate.mjs --plan                 # what would run
node scripts/router-ab/gate.mjs --out gate.md          # everything: about 20 min
node scripts/router-ab/gate.mjs --providers qwen --pairs 3
RAB_CLAUDE_MODEL=fable node scripts/router-ab/gate.mjs --providers claude --tasks textkit-agentic --pairs 6
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | PASS |
| 1 | FAIL |
| 2 | Refused: unknown provider or task, bad `--pairs`, or nothing selected. Nothing ran |
| 3 | RERUN (under 6 pairs a metric is outside its margin, or within half a margin of it: §9.6 takes 3 more pairs and decides on all 6, so rerun with `--pairs 6`) or INCOMPLETE (a run was invalid: a seat limit, a failover, a harness error) |

**Where it runs.**
- The gate itself runs on the operator workstation: `kubectl --context admin@ai`, ssh to `c4@192.168.0.8`
  (dev-worker-1), and docker.
- The worker holds the direct arms' own logins (the ChatGPT seat, the Claude seat) and reaches the LAN model servers.
  The kit runs there from a temp directory.

## Architecture

```text
gate.mjs                       CLI: plan -> deploy -> per provider: setup, Layer (a), tasks x ABBA -> verdict -> teardown
core/                          shared, provider-agnostic
  abba.mjs                     run order: pairs interleaved in time
  margins.mjs                  the §9.6 margin table (one entry per metric) and evaluate()
  report.mjs                   the Markdown verdict table and the overall verdict
  context.mjs                  hosts (ssh, push, in-pod exec), secrets (never printed), temp dirs, teardown
  seats.mjs                    the pairing guard: which accounts a run's router requests reached
providers/                     one module per provider; discovered, never named by the core
  index.mjs                    discovery and the interface check
  codex.mjs                    ChatGPT seat via the Codex CLI (direct: own login; router: dw-N key, native pass-through)
  claude.mjs                   Claude seat via Claude Code (direct: own login; router: claude/<model>, sdk-resume)
  qwen.mjs, qwen-cloud3.mjs     qwen3.8-27b, one provider per backend of the route (direct: its /v1/responses; router: pointer qwen-27b)
  lib/codex-session.mjs        shared by Codex-CLI providers: one session through the tap, metrics, the same-body check
  lib/qwen-backend.mjs         the qwen provider, parameterised by backend
tasks/
  registry.mjs                 tasks as data: id, kind, providers, fixture, prompt or turns, grader, cap, gate on/off
  graders.mjs                  graders by name (tests run in docker)
  fixtures/                    task workspaces (calc, stock, stock-hidden, textkit)
kit/                           shipped to the worker for one gate run, deleted at teardown
  tap.mjs                      recording proxy (bodies and streams, never header values)
  codex-arm.sh, summarize.py   one Codex session and its metrics
  same_body.py                 Layer (a): one recorded body sent both ways
  claude-direct.sh             the Claude direct arm (Claude Code in docker, flags mirroring the router's launch)
  claude-replica.mjs           a source-faithful replica of the router's sdk-resume launch (Layer a)
  claude_diff.py               field-by-field diff of what Anthropic receives, declared vs undeclared
  tools.mjs, mcp-tools.mjs     the agentic tasks' workspace tools, one implementation for both arms
  mcp-stub.mjs                 inert tools for chat tool-choice tasks
local/                         runs on the workstation or inside the router pod
  claude-chat-pod.mjs          Claude router arm for chat tasks (in the pod, its own admin bearer)
  harness-router.mjs           Claude router arm for agentic tasks (docker on the workstation)
  journal-seats.mjs            which accounts a run's router requests reached (failover detection)
tests/core.test.mjs            the core and the registries (node --test; no network)
```

**The provider interface** (`providers/index.mjs`):

```js
export default {
  name: 'example', describe: 'one line', kinds: ['codex-exec'],   // the task kinds it can run
  async setup(ctx) {},                                             // optional: deploy what it needs
  async layerA(ctx) { return [{ name, pass, detail }]; },          // deterministic transparency checks
  async run(ctx, task, arm, runId) {                               // arm: 'direct' | 'router'
    return { pass, detail, wall_s, tool_calls, rejected, ttft_s, uncached_tokens, router_errors, retries };
  },
  async teardown(ctx) {},                                          // optional
};
```

- **To add a provider:** add one file in `providers/`. Nothing in `core/` or `gate.mjs` changes. A Codex-CLI provider
  reuses `providers/lib/codex-session.mjs`, as `qwen.mjs` does.
- **To add a task:** add one entry in `tasks/registry.mjs`, plus its fixture if it needs a workspace. Use an existing
  grader, or add one in `tasks/graders.mjs`. `gate: false` keeps a long task out of the default run (`--tasks <id>`).
- **To add a metric:** add one entry in `core/margins.mjs`. Providers that report the field get it judged; the others
  show `n/a`.
- **To accept a known deviation:** add `known: { <margin id>: { bound, reason } }` to the task's registry entry. The
  metric is then judged against that looser bound: within it the status is KNOWN (it does not fail the gate, and the
  report prints the reason), beyond it FAIL. Once the metric is back inside the §9.6 bound, the report says the entry
  can be retired. Each entry names its measurement and the fix that removes it.

## The checks

**Layer (a): what the model receives.**
- **codex:** a recorded direct request, sent both ways, must reach the model with identical input tokens.
- **qwen:** a recorded direct request with its reasoning items, greedy, sent both ways: identical input tokens, and the
  router's output equal to a direct output. Then a request recorded on the router arm, sent both ways: identical input
  tokens, so what the router relayed renders the same on the next turn.
- **claude:** what Anthropic receives from the direct `claude -p` and from a replica of the router's launch, recorded
  through the tap. Any undeclared field difference fails. The declared differences of the sdk-resume transport are
  listed, not failed: the working-directory path, per-session metadata, hashed tool names, the transcript's markers.

**Layer (b): short tasks** (the registry's `gate: true` entries).

| Provider | Tasks |
|---|---|
| codex | code-edit (3 bugs, unittest), secret-continuity (reasoning across a resumed turn) |
| claude | math, tool-choice, recall (2 turns), stock-agentic (a multi-file fix, hidden tests) |
| qwen, qwen-cloud3 | code-edit |

The margins, and §9.6's sample-size rule: 3 pairs first; a metric outside its margin, or within half a margin of it,
takes 3 more pairs, and the decision uses all 6. Below 6 pairs a metric outside its margin is OUT, not yet FAIL. A
router-originated error is decisive at any sample size.

| Metric | The router arm must be |
|---|---|
| task success | at most 1 paired failure more than direct in 6 pairs |
| rejected tool calls | at most direct + 2 pp |
| uncached prompt tokens | at most 1.2× direct |
| TTFT median | at most direct + 2 s |
| wall-time median | at most 1.25× direct, judged per 1k output tokens where the provider reports them (below) |
| router errors and retries | none; retries at most direct |

**Wall time is judged per unit of model work.** The margin exists to catch time the router adds, but an agent session's
raw wall time is mostly model work, and that is sampled. At qwen's temperature 1.0 one request ran from 214 to 3,335
output tokens on the same body, so two sessions of one task differ 2x with no router involved. Codex-CLI providers report
output tokens, so the same 1.25× bound applies to wall time per 1k output tokens. Raw wall stays in the row. The `work`
row reports output tokens and requests, and says WATCH above 1.25×: same input and more work is sampling, and Layer (a)
says which it is. Claude reports no output tokens, so its raw wall is judged: its overhead is a fixed cost per call,
which a per-token figure would dilute.

**What is excluded from a metric.**
- Codex-CLI providers measure wall time from the first request to the last response. That leaves out process start and
  the ChatGPT login's websocket attempts, which the tap refuses: a harness artefact.
- The Claude direct arm measures from the CLI's start to the arrival of its `result` event, less the worker's measured
  `docker run` start. The router arm measures the client's own span: the HTTP time inside the pod for chat tasks, and
  the agentic harness's first request to its last tool result. The router's own per-request work is inside it.
- Uncached tokens leave out each session's first request. That request's cache depends on the client's request shape,
  not on the router (the results plan, §1.4).

**What makes a run invalid.** These say nothing about the router. Such a task's verdict is INVALID, and the run is
repeated:
- a seat throttled (429), in either arm;
- the router failed over between accounts mid-run, which breaks the pairing, or ran a Claude run on another seat than the
  worker's login (`RAB_CLAUDE_ACCOUNT`, default `claude-2`);
- the pairing cannot be verified: the run recorded no trajectory ids, or the router journal does not have them (the
  guard fails closed);
- a route with several backends kept placing the run on another server than the direct arm's (qwen, after 8 tries);
- a harness error.

Codex is the one route that cannot be held to the direct seat: the pool places each session on any seat. The guard holds
each run to one seat and prints it, and the seats serve the same model.

**A route with several backends.** The router balances new conversations, and a client cannot pick the backend, so each
qwen backend has its own provider. A router run placed on the other backend is discarded and run again. Placement is
decided before the model says anything, so this cannot favour either arm. `same_body` compares only the router calls that
reached that backend. To add a backend: one provider file, and the task's `providers` entry.

**Known deviations** (the registry's `known` entries):

| Task | Margin | Measured (baseline 2026-11-05) | Accepted bound | Mechanism, and the fix |
|---|---|---|---|---|
| claude `stock-agentic` | wall time | 1.28-1.37x direct over 3 pairs; the same 5 model calls and 10 tools in both arms | 1.5x | the sdk-resume transport starts one Claude Code process per request (about +1.6 s before the first content of each call); the Claude real-session design removes it |

## Safety

- **No credential is created, copied or printed.**
  - The worker's logins are used in place. Claude Code gets the seat's ACCESS token as an environment variable only;
    the refresh token never leaves the host.
  - The router admin bearer is used inside the router pod from its own environment. For the agentic router arm it is
    read from its Secret into one docker child's environment.
- **No recording is kept beyond the run.** Request bodies and streams are conversation content. They live under one temp
  directory per run, on the worker (`/tmp/rab-*`) and on the workstation, and teardown deletes both, whatever the
  outcome. `--keep` leaves them, for debugging only.
- **Seat use is modest.** A full gate makes about 40 Codex sessions, 30 short Claude calls plus 6 short agentic runs on
  Sonnet, and 12 qwen sessions. Runs go one at a time.
