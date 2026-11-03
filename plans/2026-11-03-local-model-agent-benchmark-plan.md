# Local-model agent benchmark: flash-next vs qwen3.8-27b as cloud3's resident default

## Context

The owner asked (2026-11-03): "Run a benchmark including both planning and implementation in order to decide if
flash-next, is better than 27b. Based on this we can decide if flash-next is the default model or 27b." They added:
"run the test in parallel, both models in the same time, also measure the speed tokens per seconds, duration, etc".

The two candidates, as deployed:

| | `flash-next` | `qwen3.8-27b` |
|---|---|---|
| Router route / pointer | `qwen3.8-flash-next` / `flash-next` | `qwen3.8-27b` / `qwen-27b` |
| Host, engine | cloud3, llama-swap LXC 5101, llama.cpp b12020, UD-Q3_K_XL | cloud2, vLLM, W4A16 (AutoRound), TP=2, MTP |
| Model | 125B MoE, 6B active | 27B dense |
| GPUs | 3 × RTX 3090 (GPU3 pulled 2026-11-03) | 2 × RTX 3090 |
| Context | `-c 262144 --parallel 2 --kv-unified` (2 slots, one 262k pool) | `--max-model-len 262144` |
| Measured earlier | ~60 tok/s decode, ~1,500 tok/s prefill | ~116-141 tok/s decode, ~1,400 tok/s prefill |
| Router account | `lan-192.168.0.26-8080` | `lan-192.168.0.28-18020` (concurrency 4; an admission queue is being rolled out today) |

**What is decided.** Which model family the estate should prefer as its local agent model, and so which one cloud3
keeps resident. The 27B measured here is cloud2's W4A16 build. If the answer is "27b", cloud3 would host its fp8 build
(a different quantisation, TP=2 on two cards, ~70-74 tok/s), which this benchmark does not measure (the coordinator's
rule: no fp8 requests on cloud3 during it). The results say so; quality transfers from W4A16 to fp8 at least as well
(higher precision), speed does not.

flash-next's earlier agent failures past ~30k tokens of context (garbled copied values, `show show show` loops,
tool-call markup leaking into arguments) were traced to cloud3's faulty GPU3. On three cards the deterministic probes
pass, but no real long agent task has run since. This benchmark is that test. qwen3.8-27b on cloud2 serves other
agents (the dev workers' local agent option, the platform eval key), so load there stays at one benchmark stream.

The harness is the real one: Codex CLI 0.166.0 on dev-worker-1 through llm-router, as `local-agent` runs it from a
plain shell (`/usr/local/bin/local-agent`: a private `CODEX_HOME` seeded from `config.toml`, the model's profile and
`AGENTS.md`, then `codex exec -p <model> --skip-git-repo-check -C <dir> -s workspace-write ... </dev/null`). The
benchmark launches from an ssh shell, not from inside a Codex tool call, so `-s workspace-write` is the effective
sandbox.

**Deadline.** The cloud cluster's auto-off is at 22:00 local (~20:00Z, `cloud-power-state`), so every scored run must
end by ~19:45Z on 2026-11-03.

## Approach

### 1. Same settings for both models

Both profiles (`~/.codex/flash-next.config.toml`, `~/.codex/qwen-27b.config.toml`) set only `model` and
`model_context_window = 262144`. Everything else comes from the same `config.toml`: `model_reasoning_effort =
"xhigh"`, `model_provider = "llm-router"`, `stream_max_retries = 8`, `request_max_retries = 6`, sandbox network on.
The router drops `reasoning_effort` on both local accounts (journal `parameter_dropped:reasoning_effort`), so each
engine thinks at its own default: an intentional deployment difference, recorded with the backends' command lines
(llama-swap `/running`, the vLLM args from the cloudlab runbook) and the router's route config revision.

**One frozen home template**, copied once at the start and hashed (sha256 in the results): `config.toml`, the two
profiles, and `AGENTS.md` **with the managed "local models" block removed**. That block (`ansible/roles/dev_worker/
tasks/codex.yml`) calls qwen-27b "the better local choice" and says flash-next copies values unreliably: model
guidance no candidate should read. The rest of `AGENTS.md` (disk hygiene, credentials, Gitea) stays, identical for
both. Each run gets a private `CODEX_HOME` copied from the template.

Per-run overrides, identical for both models: `-c show_raw_agent_reasoning=true` and
`-c model_providers.llm-router.base_url="http://127.0.0.1:<port>/run/<run-id>/v1"` (the timing tap, §4). The command
line for one run (bash on dev-worker-1, a login shell so `codex` is on PATH):

```sh
CODEX_HOME="$RUN/codex-home" timeout --kill-after=30s 40m codex exec -p "$PROFILE" --skip-git-repo-check \
  -C "$WS" -s workspace-write --json --output-last-message "$RUN/last.md" \
  -c show_raw_agent_reasoning=true -c "model_providers.llm-router.base_url=\"http://127.0.0.1:$PORT/run/$ID/v1\"" \
  "$(cat "$TASKDIR/prompt.md")" </dev/null >"$RUN/events.jsonl" 2>"$RUN/stderr.log"
```

The runner records the exit status, epoch start/end, and whether `timeout` fired, even when the run fails to start.
Nothing else changes: not the router, llama-swap, LiteLLM, nor the workers' profiles.

### 2. Task set

Scope: a targeted suite. Five of six implementation tasks are llm-router changes (TypeScript), several touching the
very code paths local models exercise, plus one Python script in ailab. It measures these models as Codex agents on
this estate's code, not coding in general.

All tasks run in throwaway workspaces under `/workspace/c4/bench/runs/` on dev-worker-1. Each workspace is the base
tree only (`git archive <base>`, extracted), `git init`, one commit tagged `base`: the agent has `git diff`/`status`
but **no history**. **No answer material is on the worker while agents run**: the bare clones, task metadata naming the
fix commits, and the validation workspaces are removed after the base tarballs are built; the commit's test files and
the reference plans stay on the workstation and reach the worker only after both runs of a pair have exited. The
agents keep the real environment (sandbox network on, the docker socket, the worker's forge credentials), so leakage is
detected rather than prevented: every command a run executed (Codex JSONL) is audited for forge or internet access
(`git clone|fetch`, `curl`, `wget` to anything but localhost), and for paths outside its own workspace except
`/workspace/c4/bench/bin`. A run that touched any of these is flagged and reviewed by hand; if it read an answer it is
excluded and reported.
<!-- codex: Access to the worker's rootful Docker socket defeats workspace confinement: an agent can mount host paths, read solutions or credentials, and modify unrelated workloads. Use an isolated execution environment or a constrained test runner that exposes only the current workspace; uid 1000 inside a container does not constrain control of the daemon. -->
<!-- opus-pushback: The docker socket, the sandbox network and the forge credentials ARE the production agent environment this benchmark is meant to measure; a constrained runner would test a different harness. The answer is removed from the worker during runs, so what remains is the forge, and every executed command is audited and a hit excluded. -->

**Implementation tasks (test-graded).** Merged commits that fix a bug or add a small feature with tests. The agent
gets the repo at the commit's parent and an issue-style description written from the commit message: the behaviour,
plus every public name or constant the hidden tests import (otherwise a correct alternative would fail on an unstated
interface); never the code. The task texts are frozen before the first scored run and appended to the results.

| ID | Repo, commit (parent = base) | What it asks | Graded tests | Why |
|---|---|---|---|---|
| I1 | llm-router `59a5214` | A typed approval code after an ordinary word ("Approve code ABCD EFGH") must still count | `tests/api-keys.test.ts` | small; also the dry run |
| I2 | llm-router `01cf00f` | A Responses reply's text and its following `function_call` items become ONE assistant chat message (the qwen early-stop fix) | `tests/protocol-translation.test.ts`, `tests/codex-chat-routes.test.ts` | exact semantics in dense one-line code |
| I3 | llm-router `72faf03` | Upstream image validation must reject undecodable PNGs: IHDR fields, bounded inflate of IDAT to the exact size, row filters | `tests/images.test.ts` | algorithmic, spec-heavy |
| I4 | llm-router `1abae92` | A frame sink that throws must never escape the waiting-label timer | `tests/agent-loop.test.ts` | timers, async error handling |
| I5 | llm-router `6041c57` | The engine's re-key gate needs evidence that the provider serves others (15-min window, 10 samples, `REKEY_BROAD_OVERLOADS = 2`) | `tests/codex-native.test.ts` | long files: a 68 KB engine file and a 57 KB test file |
| I6 | ailab `5de43272` | `ci-queue-stats.py`: a `run_attempt > 1` is a known rerun; bound the run-list scan (`--max-run-pages`, `RunScanLimit` carrying the partial runs) | `scripts/tests/test_ci_queue_stats.py` | Python, a second language and a large repo |

**Validated (2026-11-03, docker on dev-worker-1, the benchmark's own scripts):** at the base the graded files fail
on exactly the assertions the fix is about (I1 1/13 failing, I2 3/33, I3 3/40, I4 1/29, I5 2/51, I6 2/20; no
discovery, dependency or suite-level failures); with the real commit applied they all pass and typecheck is clean.

Test environment: dev-worker-1 has Node 20 and no pnpm, llm-router needs Node 24 and pnpm 10.32.1. The agents use
`/workspace/c4/bench/bin/n24 <cmd...>`: a `bench-node24:local` image (`node:24-bookworm-slim` + pnpm 10.32.1, digest
recorded) run as uid 1000 with the repository root mounted at the same path, `--cpus 3 --memory 6g --pids-limit 4096`,
labelled `bench=1`. Each llm-router workspace gets its own `node_modules` (`pnpm install --frozen-lockfile`,
`package-import-method=copy`, so no run can alter another's dependencies; ~230 MB each). I6 uses the host's Python
3.12, as an agent on the worker would, and is graded in `python:3.12-slim`. The task text tells both models the same.

**Planning tasks (judge-graded).** The agent gets a repo at the commit the original plan was written against, an
issue-style problem statement with the evidence the original author had measured, and the instruction to write
`plans/<slug>-plan.md` (Context, Approach, Critical files, Verification) without implementing anything. The supplied
evidence is limited to what the original plan's Context section states as known before its design. The base trees
were checked for the answer (`git grep` for the reference design's key names at the base).

| ID | Repo, base | Problem | Reference plan |
|---|---|---|---|
| P1 | llm-router `6e8104a` | Codex agents on local models die with "idle timeout waiting for SSE" when a turn thinks > 300 s | ailab `plans/2026-11-02-router-progress-events-plan.md` |
| P2 | llm-router `93108f5` | Codex agents on cloud2 get the router's immediate busy 429 and give up after 8 × 5 s | ailab `plans/2026-11-03-router-admission-queue-plan.md` |
| P3 | ailab `fcabf2d1^` | dev-worker-3's `/workspace` filled to 100% again five days after a cleanup | ailab `plans/2026-10-06-dev-worker-disk-guard-plan.md` |

A planning run passes the sanity check when the plan file exists and no tracked file outside `plans/` changed.

### 3. Runs: paired, simultaneous, one at a time per model

- **Pair** = one task-repeat run on both models at the same moment (launch skew recorded), each in a fresh workspace:
  flash-next on cloud3, qwen3.8-27b on cloud2. The next pair starts when both have exited and their tool containers
  are gone (`docker rm -f` of `bench=1` containers) and the tap shows no open request. So each model has one stream at
  a time; cloud2 carries at most one benchmark stream.
- **Repeats, predeclared before any scored run:** every task gets the same number of repeats, R = 2, if the dry run's
  pair time says 18 pairs fit before ~19:45Z; otherwise R = 2 for the implementation tasks and R = 1 for the planning
  tasks. No selective extra repeats after outcomes are seen.
- **Order:** the dry run (I1, unscored), then all scored pairs in a seeded shuffle (seed in the results), repeat 1 of
  every task before any repeat 2, so a slow hour does not land on one task and a repeat does not directly follow its
  twin.
- **Wall cap per run:** 40 min. A run that reaches it is a "timeout" failure; its work so far is still graded and
  reported separately ("correct at cutoff").
- **Before each pair** (read-only): cloud3 `pct exec 5101 -- nft list tables` shows no `cloud3hold`, `/running` shows
  `qwen3.8-flash-next` ready; cloud2's vLLM `num_requests_waiting` is 0 (else wait up to 5 min); `/workspace` on
  dev-worker-1 has more than 30 % free (stop below 25 %, above the disk guard's 15 % trigger). Before the first pair,
  one warm-up request loads flash-next (approved by the coordinator; the resident fp8 was loaded only to restore an old
  preload state). Residency is re-checked after each pair; a run during which another model was loaded is invalid.
- **Invalid runs, by objective rules** (from the router journal, the router pod's start time and cloud3's llama-swap
  journal): a router pod restart during the run; a cloud3 hold or llama-swap restart during the run; a request that
  failed on the router's or the backend's side (a 5xx, a busy 429, a connection reset) rather than the model's output;
  a run overlapping the release agent's announced cloud2 acceptance window. An invalid run's whole pair is rerun from
  fresh workspaces, once; both observations are kept and listed with the reason. Ordinary admission-queue waits are
  deployed-service behaviour: kept, and reported separately from model latency.
- The router roll (admission queue) may land mid-benchmark. Each request's `configRevision` and the router image are
  recorded; the results say which runs came before and after it.
<!-- codex: Finish the queue rollout, acceptance load test and backend restart before collecting scored data, then keep deployment/configuration revisions stable. A successful run after a behavior-changing rollout is not equivalent to one before it merely because neither overlapped downtime. -->
<!-- opus-pushback: The roll is another agent's release with no fixed time, and the 22:00 auto-off leaves no room to wait for it. The queue changes behaviour only when all 4 cloud2 slots are busy, which the per-request queue-wait record shows; runs overlapping the roll or the acceptance window are invalid by rule. -->


### 4. Measurements

**Correctness.**
- Implementation: the agent's diff is saved first (`git add -A; git diff --cached base`). Grading runs on the agent's
  tree after restoring the test infrastructure from `base` (`package.json`, `pnpm-lock.yaml`, `vitest.config.ts`,
  `tsconfig*.json`) and copying in the commit's test files. **Pass** = every test in the graded files passes, with the
  same test count as the validated reference (no skips or missing tests). Recorded beside it: typecheck clean or not
  (`tsc --noEmit`, the base and the reference are clean), the targeted tests (the ones failing at the base) passed,
  and, after the last pair, a full-suite run per llm-router workspace compared with the base's own full-suite result
  (new failures = regressions). The decision uses task-level pass, not test counts.
- Planning: a blind judge, `codex exec --profile review` (gpt-6-astra, xhigh, read-only), run by hand on the
  workstation, not the dispatcher. Per task it gets the problem statement, a frozen task-specific checklist of what a
  good plan must address (written from the problem statement, before any candidate exists), the reference plan
  (labelled as one reviewed answer; valid alternatives score as well), a read-only snapshot of the repo at the base,
  and the candidate plans as files named by random letters (seed recorded), nothing else. Rubric, 1-5 each with
  anchors (1 = wrong or missing, 3 = workable with gaps, 5 = would ship as written): **correctness**, **completeness**
  (against the checklist, not the reference's length), **feasibility** (buildable, not over-engineered),
  **verification** (concrete tests and checks), **grounding** (the files, functions, config keys and commands it
  says exist do exist and behave as claimed; names it proposes to add are not penalised). JSON out, a one-line reason
  per score. **Two judge passes per task with different letter orders**; a score is the mean of the two, and a
  criterion where the passes differ by 2+ is reported.
- Planning, automatic grounding: a diagnostic only, not in the score. Path-like code spans checked against the base
  snapshot (exists or not); the share is reported, proposed new files counted separately by hand.

**Speed, per request.** A timing tap on dev-worker-1: a small Node pass-through proxy, one container per model
(`--network host`, listening on 127.0.0.1), forwarding to `https://router.chifor.me` (the host's resolver, the same LAN
edge VIP `192.168.0.51` the worker uses; checked) unchanged, streaming without buffering, no retries of its own, and
closing the upstream request when the client goes away. It parses SSE across chunk boundaries and records, per HTTP
request: when it arrived, the response head, the first non-empty `response.reasoning_text.delta`, the first output
delta (`response.output_text.delta`, `response.function_call_arguments.delta`), the terminal event
(`response.completed`, `response.incomplete`, `response.failed`, an error, or the stream ending without one) with its
`usage`, the HTTP status, and the value of the router's `x-trajectory-id` header (the only header value it keeps; no
bodies). Progress events (`response.in_progress`) and `response.created` never count as tokens. The router's journal
cannot give a first-token time: `stream.first_content` fires on the first text or tool event, after the thinking.

Per request (labels are part of the definitions):
- prompt tokens, cached prompt tokens (where the engine reports them; missing = unknown, not 0), completion tokens
  (both engines count reasoning inside completion tokens: checked on the dry run against `reasoningChars`);
- **queue wait**: the journal's `admission.queued` → `admission.reserved` duration (0 when absent);
- **time to first token** (client-observed): arrival → first reasoning or output delta, reported raw and minus the
  queue wait;
- **leading think time**: first reasoning delta → first output delta (the router forwards only the reasoning before
  the first output, so this is the observed leading-reasoning interval);
- **output rate** (client-observed, approximate): completion tokens / (terminal − first token). For cloud3 the
  engine's own decode and prefill rates come from llama.cpp's per-request timings in the llama-swap journal, matched by
  time and token counts; for cloud2 there is no per-request engine timing, so cloud2's rates stay client-observed;
- **prefill proxy**: uncached prompt tokens / (time to first token − queue wait), labelled a latency-derived proxy;
- the journal's `attempt.started` → `stream.first_content` → `attempt.completed`, joined by trajectory id, as the
  cross-check. A trajectory may hold several attempts and a Codex turn several HTTP requests; the join is
  request → trajectory → attempts, never assumed one-to-one.
- Short, incomplete or zero-duration responses are excluded from rate statistics (listed).

**Speed, per task.** Wall time from launch to exit (grading excluded); model requests; Codex turns and retries (from
the JSONL); total prompt and completion tokens; leading think time vs the rest of the generation time; time between
requests (tools, CLI processing). Speed is shown beside the pass rate: an agent that stops early is not "fast", and a
capped run's duration is a lower bound.

**Robustness (failure modes).** Flagged per run from the Codex JSONL, the rollout file and the journal, then
adjudicated by hand with the same rules for both models (a flag is not automatically a failure): tool-call errors
(malformed arguments, unknown tools); tool-call markup in text or arguments (`<tool_call>`, `<parameter`,
`</function>`) that was not in the source being edited; loops (the same command or text 3+ times in a row, repeated
n-grams like `show show show`); garbled copied values (a path, identifier or number that does not exist where the agent
copied it from); early stops (a final message without the work done); timeouts; reconnects ("Reconnecting... n/8");
429s; refusals; Codex errors; context rejections or compaction. Reported as runs affected and as rate per request.

### 5. Report

`plans/2026-11-03-local-model-agent-benchmark-results.md`:
- implementation: pass per run, per task, per model (each repeat shown), the pass rate, typecheck and regressions;
- planning: judge scores per criterion per run, both passes, and the grounding diagnostic;
- per-task side-by-side table: wall time, requests, tokens, think share;
- per-request speed: median and p90 per model for time to first token, output rate and prefill proxy, and the same
  bucketed by prompt size (< 10k, 10k-30k, 30k-100k, ≥ 100k tokens, bucket counts shown, no p90 under 5 samples).
  Requests within a run are correlated and the two models' agents choose different prompts: these describe the
  workloads, not a controlled throughput test;
- failure modes with excerpts; every invalid run and why; the leakage audit result;
- the exact command lines, base commits, frozen task texts, checklists, the judge prompt, the seeds, the template hash;
- the caveat that speed is "as deployed": 3 cards on cloud3 against 2 on cloud2, different engines and quantisations,
  cloud2 shared with production traffic; not a like-for-like hardware comparison;
- **decision rule, fixed now:** correctness first (implementation tasks passed, counted per task and repeat; then the
  mean plan score), robustness second (a long-context corruption on flash-next weighs heavily: it is why it was
  distrusted), speed third. A gap of at most one implementation run or 1.5 plan points (of 25) is a **practical tie**,
  then robustness and speed decide; if quality and robustness point opposite ways, or the evidence is too thin (many
  invalid runs), the recommendation says **inconclusive** and names the extra tasks that would settle it.

Phase B of the planning process is a codex review (`--profile review`) of the results report: a check that the
evidence supports the numbers and that the decision rule above was applied as written.

**Evidence kept:** the harness (tap, wrapper, runner, grader, judge prompt), the frozen task texts and checklists, the
per-request and per-run tables (CSV/JSON), the judge outputs, the agents' diffs and plans, and the Codex JSONL and
rollouts, as one tarball in dev-worker-1's `/workspace/archive/` (the managed evidence location) and copied to the
workstation, with its sha256 in the results. Credentials are never in it (the tap logs none; private homes hold no
token: the provider's key comes from `/usr/local/bin/codex-router-key` at run time).

## Critical files

- `plans/2026-11-03-local-model-agent-benchmark-plan.md` (this plan), `plans/2026-11-03-local-model-agent-benchmark-results.md`.
- The harness lives in the session scratchpad on the workstation and in `/workspace/c4/bench/` on dev-worker-1. Nothing
  in any repository's code or config changes.

## Verification

- Each implementation task: base fails the graded tests on the targeted assertions, base + real commit passes (done).
- Dry run: I1 on both models end to end (workspace, run, tap, journal join, grading, leakage audit) before the scored
  set. The tap is checked on it: every journal trajectory of the run has exactly one tap record with the same id, and
  the stream completes as it does without the tap (a direct `curl` of one small streamed request with and without the
  tap: same events, overhead under 50 ms).
- Long context: the per-request table shows how many requests per model went over 30k and over 100k prompt tokens; if
  no flash-next run went over 30k, the results say the old failure mode was not exercised.
- Clean-up: before the first run, `df -h / /workspace` and `docker ps`/`docker images` are recorded; at the end, only
  benchmark-owned things go (`/workspace/c4/bench`, `bench=1` containers, the `bench-node24:local` image, the
  `node:24-bookworm-slim`/`python:3.12-slim` pulls if they were not there before), and the evidence tarball stays.

<!-- codex-review-status: pending -->
