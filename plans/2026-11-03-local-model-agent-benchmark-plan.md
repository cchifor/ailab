# Local-model agent benchmark: flash-next vs qwen3.8-27b as cloud3's resident default

## Context

The owner asked (2026-11-03): "Run a benchmark including both planning and implementation in order to decide if
flash-next, is better than 27b. Based on this we can decide if flash-next is the default model or 27b." They added:
"run the test in parallel, both models in the same time, also measure the speed tokens per seconds, duration, etc".

The two candidates, as deployed:

| | `flash-next` | `qwen3.8-27b` |
|---|---|---|
| Router route / pointer | `qwen3.8-flash-next` / `flash-next` | `qwen3.8-27b` / `qwen-27b` |
| Host, engine | cloud3, llama-swap LXC 5101, llama.cpp b12020, UD-Q3_K_XL | cloud2, vLLM, TP=2, MTP |
| Model | 125B MoE, 6B active | 27B dense |
| GPUs | 3 × RTX 3090 (GPU3 pulled 2026-11-03) | 2 × RTX 3090 |
| Context | `-c 262144 --parallel 2 --kv-unified` (2 slots, one 262k pool) | `--max-model-len 262144` |
| Measured earlier | ~60 tok/s decode, ~1,500 tok/s prefill | ~116-141 tok/s decode, ~1,400 tok/s prefill |
| Router account | `lan-192.168.0.26-8080` | `lan-192.168.0.28-18020` (concurrency 4) |

flash-next's earlier agent failures past ~30k tokens of context (garbled copied values, `show show show` loops,
tool-call markup leaking into arguments) were traced to cloud3's faulty GPU3. On three cards the deterministic probes
pass, but no real long agent task has run since. This benchmark is that test. qwen3.8-27b is the dev workers'
production agent model, so load on cloud2 stays modest.

The harness is the real one: Codex CLI 0.166.0 on dev-worker-1 through llm-router, exactly as `local-agent` runs it
(`/usr/local/bin/local-agent`: a private `CODEX_HOME` seeded from `config.toml`, the model's profile and `AGENTS.md`,
then `codex exec -p <model> --skip-git-repo-check -C <dir> -s workspace-write ... </dev/null`).

## Approach

### 1. Same settings for both models

Both profiles (`~/.codex/flash-next.config.toml`, `~/.codex/qwen-27b.config.toml`) set only `model` and
`model_context_window = 262144`. Everything else comes from the same `config.toml`: `model_reasoning_effort =
"xhigh"` (the router drops it for local accounts and discloses that), `model_provider = "llm-router"`,
`stream_max_retries = 8`, `request_max_retries = 6`, sandbox network on. The same `AGENTS.md` goes into both
private homes. Per-run overrides, identical for both models:

- `-c show_raw_agent_reasoning=true`
- `-c model_providers.llm-router.base_url="http://127.0.0.1:<port>/run/<run-id>/v1"`: the timing tap (§4).

The command line for one run (bash on dev-worker-1, `bash -lc` so `codex` is on PATH):

```sh
CODEX_HOME=$RUN/codex-home timeout --kill-after=30s ${CAP} codex exec -p $PROFILE --skip-git-repo-check \
  -C $WS -s workspace-write --json --output-last-message $RUN/last.md \
  -c show_raw_agent_reasoning=true -c model_providers.llm-router.base_url="\"http://127.0.0.1:$PORT/run/$ID/v1\"" \
  "$(cat $TASK)" </dev/null >$RUN/events.jsonl 2>$RUN/stderr.log
```

The per-run `CODEX_HOME` is kept (not deleted, unlike `local-agent`) until the analysis is done, for the session
rollout file. No configuration anywhere changes: not the router, llama-swap, LiteLLM, nor the workers' profiles.

### 2. Task set

All tasks run in throwaway workspaces under `/workspace/c4/bench/` on dev-worker-1. Each workspace is
`git archive <base> | tar -x` plus `git init` and one commit `base`, so the agent has `git diff`/`git status` but
**no history**: it cannot look up the real fix. Nothing is pushed; everything under `/workspace/c4/bench/` is deleted
at the end.

**Implementation tasks (test-graded).** Merged commits that fix a bug or add a small feature with tests. The agent
gets the repo at the commit's parent and an issue-style description written from the commit message (the behaviour
and the public names the tests import; never the code). The commit's test files are withheld: at the parent they are
the old versions, and the grader copies the commit's versions in after the run.

| ID | Repo, commit | What it asks | Graded tests | Why |
|---|---|---|---|---|
| I1 | llm-router `59a5214` | A typed approval code after an ordinary word ("Approve code ABCD EFGH") must still match (overlapping regex) | `tests/api-keys.test.ts` | small; the dry run |
| I2 | llm-router `01cf00f` | A Responses reply's text and its following `function_call` items become ONE assistant chat message (the qwen early-stop fix) | `tests/protocol-translation.test.ts`, `tests/codex-chat-routes.test.ts` | exact semantics in dense one-line code |
| I3 | llm-router `72faf03` | Upstream image validation must reject undecodable PNGs: IHDR fields, bounded inflate of IDAT to the exact size, row filters | `tests/images.test.ts` | algorithmic, spec-heavy |
| I4 | llm-router `1abae92` | A frame sink that throws must never escape the waiting-label timer | `tests/agent-loop.test.ts` | timers, async error handling |
| I5 | llm-router `6041c57` | The engine's re-key gate needs evidence that the provider serves others (15-min window, 10 samples, `REKEY_BROAD_OVERLOADS = 2`) | `tests/codex-native.test.ts` | **long context**: a 68 KB engine file and a 57 KB test file |
| I6 | ailab `5de43272` | `ci-queue-stats.py`: a `run_attempt > 1` is a known rerun; bound the run-list scan (`--max-run-pages`, `RunScanLimit` carrying the partial runs) | `scripts/tests/test_ci_queue_stats.py` | Python, a second language and a large repo |

Each task is validated before any timed run: at the parent, the graded tests fail (at least one); with the real
commit applied, they all pass. A task that fails either check is replaced.

The agents run tests through a wrapper, because dev-worker-1 has Node 20 and no pnpm while llm-router needs Node 24
and pnpm 10.32.1: `/workspace/c4/bench/bin/n24 <cmd...>` runs `<cmd>` in a `node:24` image with pnpm installed,
the workspace mounted at the same path, as uid 1000. `node_modules` is pre-installed in each llm-router workspace
(`pnpm install --frozen-lockfile` in the same image, one pnpm store on the same mount so the files are hard links).
Codex's sandbox can reach the docker socket (checked: `codex sandbox -c sandbox_mode="workspace-write" -- docker ps`).
I6 uses the host's Python 3.12, as an agent on the worker would. The task text tells both models the same thing.

**Planning tasks (judge-graded).** The agent gets a repo at the commit the original plan was written against, an
issue-style problem statement with the evidence the original author had measured (the agent cannot reach the estate),
and the instruction to write `plans/<slug>-plan.md` (Context, Approach, Critical files, Verification). It must not
implement anything. The reference answer is the real, codex-reviewed plan.

| ID | Repo, base | Problem | Reference plan |
|---|---|---|---|
| P1 | llm-router `6e8104a` | Codex agents on local models die with "idle timeout waiting for SSE" when a turn thinks > 300 s | ailab `plans/2026-11-02-router-progress-events-plan.md` |
| P2 | llm-router `93108f5` | Codex agents on cloud2 get the router's immediate busy 429 and give up after 8 × 5 s | ailab `plans/2026-11-03-router-admission-queue-plan.md` |
| P3 | ailab `fcabf2d1^` | dev-worker-3's `/workspace` filled to 100% again five days after a cleanup | ailab `plans/2026-10-06-dev-worker-disk-guard-plan.md` |

### 3. Runs: paired, simultaneous, one at a time per model

- **2 repeats per task per model**: 9 tasks × 2 = 18 pairs. A third repeat for a task whose two repeats disagree
  (one pass and one fail on the same model), if time allows.
- **A pair** is one task-repeat run on both models at the same moment, each in its own fresh workspace: flash-next
  on cloud3 and qwen3.8-27b on cloud2. The next pair starts when both finish. So each model has one run at a time:
  cloud2 carries at most one extra stream (it is production), and flash-next's two slots never compete with each other
  for the shared 262k pool through this benchmark.
- Pair order is fixed in advance and interleaves the tasks (I1 dry run first, then a shuffled order with a seed
  recorded in the results), so a slow period of the day does not land on one task only.
- **Wall cap per run:** 40 min. A run that reaches it is a failure ("timeout"); its work so far is still graded.
- **Before each pair:** on cloud3 (read-only) `pct exec 5101 -- nft list tables` shows no `cloud3hold`, and
  `/running` shows `qwen3.8-flash-next` ready. If anything else is resident, stop and ask; never request another
  cloud3 model. Before the first pair, one warm-up request loads flash-next (the coordinator approved swapping out the
  resident fp8 on 2026-11-03).
- **Discard and rerun** any run hit by an outage: a router 429, a router pod restart (the planned admission-queue
  roll, a `Recreate` with 30-60 s down), the planned cloud3 llama-swap restart behind its nft hold, a reconnect that
  is not the model's own fault, or the cloud2 queue acceptance window. Identified from the router journal (status,
  `admission.queued`, gaps) and the pod's start time. Each discard and its reason is listed in the results.

### 4. Measurements

**Correctness.**
- Implementation: pass = every test in the graded files passes (vitest JSON reporter / unittest), run in docker on
  dev-worker-1 after copying the commit's test files over the agent's workspace. Also recorded: tests passed / total
  (partial credit), and for llm-router `tsc --noEmit` (`n24 pnpm typecheck`) as a secondary signal.
- Planning: a blind judge, `codex exec --profile review` (gpt-6-astra, xhigh, read-only) run by hand on the
  workstation, not the dispatcher. Per task it gets the problem statement, the reference plan (labelled as one
  reviewed answer, not the only valid one), a read-only snapshot of the repo at the base commit to verify claims, and
  the four candidate plans renamed `A`-`D` in a random order (seed recorded), with any model name in them redacted.
  Rubric, 1-5 each: **correctness** (the diagnosis and design are right for this code), **completeness** (the cases
  and failure modes the reference covers), **feasibility** (buildable as written, no over-engineering), **verification**
  (concrete tests and checks), **grounding** (the files, functions, config keys and commands it names exist and do what
  it says). The judge answers in JSON with a one-line reason per score; the total is out of 25.
- Planning, automatic grounding: a script extracts every path-like and identifier-like code span from each plan and
  checks it against the snapshot (path exists; identifier found by `git grep`). Reported as found / named.

**Speed, per request.** A timing tap on dev-worker-1: a small Node pass-through proxy (one docker container per model,
`--network host`, listening on 127.0.0.1) that forwards to `https://router.chifor.me` unchanged and records, per
request, the time it was sent, the response head, the first `response.reasoning_text.delta`, the first output delta
(`response.output_text.delta` or `response.function_call_arguments.delta`), `response.completed` and its `usage`, and
the router's `x-trajectory-id` header. It logs no header values and no bodies except the usage and event types. It
gives what the journal cannot: the router's `stream.first_content` fires on the first text or tool event, after the
thinking, so the journal has no first-token time. Per request:

- prompt tokens, cached prompt tokens (where the engine reports them), completion tokens;
- reasoning characters (journal `attempt.completed.output.reasoningChars`) and the think time (first reasoning
  delta → first output delta);
- time to first token (sent → first delta of any kind), minus any `admission.queued` wait from the journal;
- decode tok/s = completion tokens / (completed − first token);
- prefill tok/s ≈ (prompt − cached) / (time to first token − queue wait);
- the journal's own `attempt.started` → `stream.first_content` and → `attempt.completed`, joined by trajectory id,
  as the cross-check. For cloud3, llama.cpp's own per-request `timings` where llama-swap logs them; for cloud2,
  vLLM's metric deltas only as a sanity check (cloud2 serves other agents at the same time).

**Speed, per task.** Wall duration; number of model requests (turns); total prompt and completion tokens; thinking
vs answering share of the generation time; idle time between requests (tool execution).

**Robustness (failure modes).** Counted per run from the Codex JSONL events, the rollout file and the journal:
tool-call errors (malformed arguments, unknown tools), tool-call markup in text or arguments (`<tool_call>`,
`<parameter`, `</function>`), loops (the same command or the same text 3+ times in a row, repeated n-grams like
`show show show`), garbled copied values (a path, identifier or number the agent copied that does not exist in the
source it read; checked by hand on failing runs), early stops (a final message without the work done), timeouts,
reconnects ("Reconnecting... n/8"), 429s, refusals, and Codex errors. Each with a short excerpt.

### 5. Report

`plans/2026-11-03-local-model-agent-benchmark-results.md`:
- implementation pass rate per model per task, and overall, with both repeats shown (variance, not only means);
- plan scores per model per task (each judge criterion), the automatic grounding rate;
- per-task side-by-side table: wall time, turns, tokens, think share;
- per-request speed: median and p90 per model of time to first token, decode tok/s, prefill tok/s; and the same
  bucketed by prompt size (< 10k, 10-30k, 30-100k, > 100k), since a 6B-active MoE and a dense 27B scale differently;
- failure modes with excerpts; every discarded run and why;
- the exact command lines, base commits, task texts (in an appendix or a linked file), and the seeds;
- the caveat that speed is "as deployed": 3 cards on cloud3 against 2 cards on cloud2, different engines and quants,
  cloud2 shared with production traffic; not a like-for-like hardware comparison;
- a recommendation for cloud3's resident default model, with confidence and caveats. The decision rule, fixed now:
  correctness first (implementation pass rate, then plan score); robustness second (any long-context corruption on
  flash-next weighs heavily, since it was the reason it was distrusted); speed third. A difference of one task in 12
  runs is within noise and is reported as a tie.

Phase B of the planning process is a codex review (`--profile review`) of the results report instead of a code diff.

## Critical files

- `plans/2026-11-03-local-model-agent-benchmark-plan.md` (this plan), `plans/2026-11-03-local-model-agent-benchmark-results.md`.
- Harness scripts live in the session scratchpad on the workstation and in `/workspace/c4/bench/` on dev-worker-1
  (both temporary; the results file records the commands). Nothing in the repo's code or config changes.

## Verification

- Each implementation task: parent fails the graded tests, parent + real commit passes them (recorded).
- Dry run: I1 on both models end to end (workspace, run, timing tap, journal join, grading) before the full set.
- The timing tap is checked on the dry run against the journal: same request count, matching trajectory ids, and
  the tap's request durations within ~1 s of the journal's.
- At least one run per model reaches prompts over 30k tokens (from the tap's usage); the planning tasks over 100k.
- Every number in the results comes from a file kept in the scratchpad until the PR merges.
- Clean-up check at the end: `/workspace/c4/bench` gone, no bench containers or images left, `df -h /workspace` back.

<!-- codex-review-status: pending -->
