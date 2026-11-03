# Local-model agent benchmark: flash-next vs qwen3.8-27b as cloud3's resident default

## Codex Review

- The real agent harness, paired starts, clean snapshots, and failing-parent/passing-fix controls are strong foundations.
- This compares two deployed services; cloud2's 27B results alone cannot establish how a 27B deployment would perform as cloud3's resident model.
- Shared instructions already express a model preference, and filesystem, Docker, and network access can expose answers or other runs. Isolation and independent grading need strengthening.
- Six implementation tasks and three planning tasks with two repeats support screening, but close results need more evidence. Selective retries and outage exclusions could bias the recommendation.
- Speed measurements need clearer event and token accounting; production load, cancellation, disk limits, deployment stability, and reproducible evidence need explicit checks.

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

<!-- codex: The runbook identifies cloud2's 27B as W4A16 AutoRound, while cloud3's existing 27B deployment is fp8 with different capacity and throughput. Define whether the decision concerns an endpoint default or cloud3 residency; the latter requires qualifying the intended cloud3 27B deployment before transferring this ranking. -->
<!-- codex: Equal advertised context limits do not establish equal usable input capacity: output headroom and other requests consume KV capacity, especially flash-next's shared pool. Verify effective per-request limits and record context rejection, truncation and compaction rather than assuming 262144 input tokens are available. -->

flash-next's earlier agent failures past ~30k tokens of context (garbled copied values, `show show show` loops,
tool-call markup leaking into arguments) were traced to cloud3's faulty GPU3. On three cards the deterministic probes
pass, but no real long agent task has run since. This benchmark is that test. qwen3.8-27b is the dev workers'
production agent model, so load on cloud2 stays modest.
<!-- codex: The dev-worker runbook says the default route remains dw-N → pool-codex, with qwen-27b available as a local option. Verify the current workload and production role instead of using this characterization to infer either representativeness or spare capacity. -->

The harness is the real one: Codex CLI 0.166.0 on dev-worker-1 through llm-router, exactly as `local-agent` runs it
(`/usr/local/bin/local-agent`: a private `CODEX_HOME` seeded from `config.toml`, the model's profile and `AGENTS.md`,
then `codex exec -p <model> --skip-git-repo-check -C <dir> -s workspace-write ... </dev/null`).
<!-- codex: The checked-in local-agent wrapper conditionally inherits a parent sandbox instead of always using workspace-write. Specify the benchmark's launch context and verify its effective sandbox; otherwise the claimed equivalence and even successful startup can depend on how it is launched. -->

## Approach

### 1. Same settings for both models

Both profiles (`~/.codex/flash-next.config.toml`, `~/.codex/qwen-27b.config.toml`) set only `model` and
`model_context_window = 262144`. Everything else comes from the same `config.toml`: `model_reasoning_effort =
"xhigh"` (the router drops it for local accounts and discloses that), `model_provider = "llm-router"`,
`stream_max_retries = 8`, `request_max_retries = 6`, sandbox network on. The same `AGENTS.md` goes into both
private homes. Per-run overrides, identical for both models:
<!-- codex: Identical CLI settings do not guarantee identical effective generation settings after adaptation: record sampling defaults, thinking/template settings, output limits, route timeouts, forwardReasoning and actual serving accounts. Classify intentional deployment differences explicitly; xhigh being dropped does not prove equal reasoning budgets. -->
<!-- codex: The managed AGENTS.md block in ansible/roles/dev_worker/tasks/codex.yml calls qwen-27b “the better local choice” and says flash-next copies values unreliably. Remove this model-specific guidance from both private benchmark copies and audit other loaded instructions, skills and tools for answer hints or delegation to another model. -->

- `-c show_raw_agent_reasoning=true`
- `-c model_providers.llm-router.base_url="http://127.0.0.1:<port>/run/<run-id>/v1"`: the timing tap (§4).

The command line for one run (bash on dev-worker-1, `bash -lc` so `codex` is on PATH):

```sh
CODEX_HOME=$RUN/codex-home timeout --kill-after=30s ${CAP} codex exec -p $PROFILE --skip-git-repo-check \
  -C $WS -s workspace-write --json --output-last-message $RUN/last.md \
  -c show_raw_agent_reasoning=true -c model_providers.llm-router.base_url="\"http://127.0.0.1:$PORT/run/$ID/v1\"" \
  "$(cat $TASK)" </dev/null >$RUN/events.jsonl 2>$RUN/stderr.log
```

<!-- codex: Quote path variables, validate task-file reads and directory creation before launch, and persist the process exit status and monotonic start/end times even on timeout or interruption. The shown command alone does not distinguish harness startup failures, agent failures and cap expiry. -->

The per-run `CODEX_HOME` is kept (not deleted, unlike `local-agent`) until the analysis is done, for the session
rollout file. No configuration anywhere changes: not the router, llama-swap, LiteLLM, nor the workers' profiles.
<!-- codex: Freeze and hash the sanitized effective configuration and instruction files once, rather than recopying potentially changing worker files between runs. Record the CLI binary, router image/config revision and backend/model identities so later deployment or fleet changes cannot silently mix experimental conditions. -->

### 2. Task set

All tasks run in throwaway workspaces under `/workspace/c4/bench/` on dev-worker-1. Each workspace is
`git archive <base> | tar -x` plus `git init` and one commit `base`, so the agent has `git diff`/`git status` but
**no history**: it cannot look up the real fix. Nothing is pushed; everything under `/workspace/c4/bench/` is deleted
at the end.
<!-- codex: Removing Git history does not prevent answer lookup: workspace-write permits reads outside the workspace, and network access can retrieve forge history. Isolate each run from original clones, grading assets, reference plans, sibling runs and prior rollouts, and restrict tool egress while preserving the CLI's required inference connection; verify these boundaries with negative checks. -->
<!-- codex: Budget peak bytes and inodes for snapshots, node_modules, stores, container layers and rollouts before staging, and check both /workspace and / throughout execution. Stage only the active pair and define a stop threshold above the worker guard's 15% trigger; its emergency reclamation should not be the benchmark's capacity plan. -->

**Implementation tasks (test-graded).** Merged commits that fix a bug or add a small feature with tests. The agent
gets the repo at the commit's parent and an issue-style description written from the commit message (the behaviour
and the public names the tests import; never the code). The commit's test files are withheld: at the parent they are
the old versions, and the grader copies the commit's versions in after the run.
<!-- codex: Five of six implementation tasks concern one router, and several target previously observed local-model failures, so this is a targeted regression suite more than a representative coding sample. State that scope and reserve some independent, previously unused implementation tasks if the recommendation is meant to generalize. -->
<!-- codex: Freeze task prompts before candidate runs and distinguish required behavior from solution hints: “overlapping regex” and exact design constants can reveal part of the implementation. Conversely, every interface or policy demanded by hidden tests must be specified, or correct alternative solutions may fail for unstated requirements. -->
<!-- codex: The llm-router source is outside this repository, so its task SHAs, parent choices and test dependencies remain unverified here. Obtain a trusted source snapshot, resolve full commit IDs, and account for fixtures, snapshots, submodules or archive exclusions before declaring these tasks runnable. -->

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
<!-- codex: Require the parent failure to demonstrate the intended missing behavior, not missing dependencies, test discovery failure or unrelated flakiness, and record the exact failing assertions. Validate controls under the benchmark's resource limits and replace tasks before examining candidate outcomes. -->

The agents run tests through a wrapper, because dev-worker-1 has Node 20 and no pnpm while llm-router needs Node 24
and pnpm 10.32.1: `/workspace/c4/bench/bin/n24 <cmd...>` runs `<cmd>` in a `node:24` image with pnpm installed,
the workspace mounted at the same path, as uid 1000. `node_modules` is pre-installed in each llm-router workspace
(`pnpm install --frozen-lockfile` in the same image, one pnpm store on the same mount so the files are hard links).
Codex's sandbox can reach the docker socket (checked: `codex sandbox -c sandbox_mode="workspace-write" -- docker ps`).
I6 uses the host's Python 3.12, as an agent on the worker would. The task text tells both models the same thing.
<!-- codex: Access to the worker's rootful Docker socket defeats workspace confinement: an agent can mount host paths, read solutions or credentials, and modify unrelated workloads. Use an isolated execution environment or a constrained test runner that exposes only the current workspace; uid 1000 inside a container does not constrain control of the daemon. -->
<!-- codex: Writable hard-linked dependencies can contaminate the shared store and another run if edited in place. Use immutable shared dependency content or independent writable copies, and verify isolation rather than assuming separate node_modules paths suffice. -->
<!-- codex: Pin the Node image digest and pnpm version, prebuild the wrapper environment, and set container CPU, memory, PID and execution limits with bounded cleanup. Concurrent tests and installs on the same worker otherwise introduce tool-latency contention and can exhaust production worker resources. -->

**Planning tasks (judge-graded).** The agent gets a repo at the commit the original plan was written against, an
issue-style problem statement with the evidence the original author had measured (the agent cannot reach the estate),
and the instruction to write `plans/<slug>-plan.md` (Context, Approach, Critical files, Verification). It must not
implement anything. The reference answer is the real, codex-reviewed plan.
<!-- codex: “Cannot reach the estate” is not established by the current network-enabled, Docker-accessible setup. Enforce that boundary and verify the expected plan exists while no implementation files changed. -->
<!-- codex: Audit each exported tree, including docs and review artifacts, for the answer or later design knowledge; an old code base alone does not establish a clean planning problem. Freeze the supplied evidence and exclude reference requirements discovered only after that evidence was available. -->

| ID | Repo, base | Problem | Reference plan |
|---|---|---|---|
| P1 | llm-router `6e8104a` | Codex agents on local models die with "idle timeout waiting for SSE" when a turn thinks > 300 s | ailab `plans/2026-11-02-router-progress-events-plan.md` |
| P2 | llm-router `93108f5` | Codex agents on cloud2 get the router's immediate busy 429 and give up after 8 × 5 s | ailab `plans/2026-11-03-router-admission-queue-plan.md` |
| P3 | ailab `fcabf2d1^` | dev-worker-3's `/workspace` filled to 100% again five days after a cleanup | ailab `plans/2026-10-06-dev-worker-disk-guard-plan.md` |

### 3. Runs: paired, simultaneous, one at a time per model

- **2 repeats per task per model**: 9 tasks × 2 = 18 pairs. A third repeat for a task whose two repeats disagree
  (one pass and one fail on the same model), if time allows.
<!-- codex: Two repeats mainly reveal gross instability; they do not provide a reliable estimate across coding tasks, and planning scores have no defined pass/fail disagreement here. Predeclare a balanced repeat policy and expansion budget rather than selectively adding observations “if time allows.” -->
- **A pair** is one task-repeat run on both models at the same moment, each in its own fresh workspace: flash-next
  on cloud3 and qwen3.8-27b on cloud2. The next pair starts when both finish. So each model has one run at a time:
  cloud2 carries at most one extra stream (it is production), and flash-next's two slots never compete with each other
  for the shared 262k pool through this benchmark.
<!-- codex: One CLI process does not guarantee one backend stream: the admission-queue plan documents orphaned upstream legs after client reconnects. Confirm cancellation and release of the previous request before retries or the next pair can add load, including when timeout kills Codex but Docker tools or proxy connections survive. -->
<!-- codex: Simultaneous starts do not equalize load on different hosts, and unrelated flash-next callers can still occupy its second slot. Record backend concurrency, queues, KV use/preemptions and worker CPU/memory pressure, with production-health stop conditions; one long benchmark request can still materially affect cloud2. -->
- Pair order is fixed in advance and interleaves the tasks (I1 dry run first, then a shuffled order with a seed
  recorded in the results), so a slow period of the day does not land on one task only.
<!-- codex: Decide beforehand whether I1's dry run is scored, and exclude pilot results if prompts or harness behavior change afterward. Record actual launch skew and ensure repeats are interleaved rather than immediately benefiting from the previous run's warm caches. -->
- **Wall cap per run:** 40 min. A run that reaches it is a failure ("timeout"); its work so far is still graded.
<!-- codex: Eighteen pairs can consume 12 hours before preparation, grading and reruns; schedule against cloud2's documented night shutdown and declare an overall budget. The cap is a valid productivity constraint, but report correctness at cutoff separately from completion within the cap. -->
- **Before each pair:** on cloud3 (read-only) `pct exec 5101 -- nft list tables` shows no `cloud3hold`, and
  `/running` shows `qwen3.8-flash-next` ready. If anything else is resident, stop and ask; never request another
  cloud3 model. Before the first pair, one warm-up request loads flash-next (the coordinator approved swapping out the
  resident fp8 on 2026-11-03).
<!-- codex: Absence of a hold and a ready model are point-in-time checks, not a reservation against another caller swapping models mid-run. Establish the initial authorized warm-up order, verify residency throughout each pair, and pause for coordinated maintenance rather than repeatedly swapping models. -->
<!-- codex: Specify warm-up and cache treatment for both models, including repeat-to-repeat prefix reuse and cache eviction from production traffic. Keep cold-load latency separate from warm task latency without flushing production caches. -->
- **Discard and rerun** any run hit by an outage: a router 429, a router pod restart (the planned admission-queue
  roll, a `Recreate` with 30-60 s down), the planned cloud3 llama-swap restart behind its nft hold, a reconnect that
  is not the model's own fault, or the cloud2 queue acceptance window. Identified from the router journal (status,
  `admission.queued`, gaps) and the pod's start time. Each discard and its reason is listed in the results.
<!-- codex: A busy 429 or queue wait is ordinary deployed-service behavior, not necessarily an outage, and removing it can favor the more congested endpoint. Report all-attempt operational outcomes alongside a separately defined model-quality subset, using objective exclusion rules rather than “not the model's own fault.” -->
<!-- codex: Finish the queue rollout, acceptance load test and backend restart before collecting scored data, then keep deployment/configuration revisions stable. A successful run after a behavior-changing rollout is not equivalent to one before it merely because neither overlapped downtime. -->
<!-- codex: For a genuinely invalidated comparison, rerun the whole pair from fresh workspaces and retain both original observations with their reasons. Bound reruns and do not keep only a later successful attempt. -->

### 4. Measurements

**Correctness.**
- Implementation: pass = every test in the graded files passes (vitest JSON reporter / unittest), run in docker on
  dev-worker-1 after copying the commit's test files over the agent's workspace. Also recorded: tests passed / total
  (partial credit), and for llm-router `tsc --noEmit` (`n24 pnpm typecheck`) as a secondary signal.
<!-- codex: Preserve the candidate diff before replacing tests and grade in a fresh copy with trusted test files, runner configuration and dependencies. Require expected test discovery and no unexpected skips so edits to scripts, setup files or test selection cannot manufacture a pass; retain candidate-authored tests separately. -->
<!-- codex: Passing the selected files can conceal regressions elsewhere or a broken build; define a bounded set of relevant regression checks and require no new typecheck failures relative to the base. Use the same per-task runtime for controls, agent tests and grading, resolving the host-Python versus Docker ambiguity for I6. -->
<!-- codex: Whole-file passed/total counts can award almost full credit for leaving the bug untouched because most tests already pass at the parent. Report the targeted regressions separately and weight overall implementation success by task, not by test count. -->
- Planning: a blind judge, `codex exec --profile review` (gpt-6-astra, xhigh, read-only) run by hand on the
  workstation, not the dispatcher. Per task it gets the problem statement, the reference plan (labelled as one
  reviewed answer, not the only valid one), a read-only snapshot of the repo at the base commit to verify claims, and
  the four candidate plans renamed `A`-`D` in a random order (seed recorded), with any model name in them redacted.
  Rubric, 1-5 each: **correctness** (the diagnosis and design are right for this code), **completeness** (the cases
  and failure modes the reference covers), **feasibility** (buildable as written, no over-engineering), **verification**
  (concrete tests and checks), **grounding** (the files, functions, config keys and commands it names exist and do what
  it says). The judge answers in JSON with a one-line reason per score; the total is out of 25.
<!-- codex: “Completeness” against a reviewed reference rewards matching its design and later discoveries, even when an alternative solves the supplied problem. Freeze task-specific behavioral criteria and anchored 1–5 scores before generation, and assess valid alternatives against those criteria rather than reference similarity or length. -->
<!-- codex: One joint judging pass has position, anchoring and stochastic effects; for a close decision, repeat judging with permuted labels or independently adjudicate decisive disagreements. Preserve the judge prompt, outputs and cited evidence, and handle additional repeats rather than hard-coding four candidates. -->
<!-- codex: Restrict the judge to sanitized candidate files and the base snapshot, and treat candidate text as evidence rather than executable instructions. Redacting model names alone does not blind paths, metadata or neighboring transcripts; preserve task-relevant model names when they describe the problem itself. -->
- Planning, automatic grounding: a script extracts every path-like and identifier-like code span from each plan and
  checks it against the snapshot (path exists; identifier found by `git grep`). Reported as found / named.
<!-- codex: Valid plans introduce new paths and symbols, while an identifier occurring somewhere does not validate a claimed behavior; prose formatting also changes this denominator. Keep this optional diagnostic out of the decision score and manually distinguish proposed names from claims about existing code rather than building a complex pseudo-semantic grader. -->

**Speed, per request.** A timing tap on dev-worker-1: a small Node pass-through proxy (one docker container per model,
`--network host`, listening on 127.0.0.1) that forwards to `https://router.chifor.me` unchanged and records, per
request, the time it was sent, the response head, the first `response.reasoning_text.delta`, the first output delta
(`response.output_text.delta` or `response.function_call_arguments.delta`), `response.completed` and its `usage`, and
the router's `x-trajectory-id` header. It logs no header values and no bodies except the usage and event types. It
gives what the journal cannot: the router's `stream.first_content` fires on the first text or tool event, after the
thinking, so the journal has no first-token time. Per request:
<!-- codex: The proxy needs tested path-prefix removal, authentication/status propagation, streaming without buffering, backpressure and upstream cancellation on client disconnect, with no retries of its own. Keep it small and validate it against a scripted SSE stream before relying on it for either timing or production-safe cancellation. -->
<!-- codex: Verify the container resolves router.chifor.me to the same LAN edge VIP as the worker; the runbook documents split DNS and materially different Cloudflare timeout behavior. A proxy that changes the route would change the harness being measured. -->
<!-- codex: Use monotonic timestamps with explicit boundaries for request arrival, upstream send, first content, last content and stream close; parse SSE across chunk boundaries. Record failed/incomplete streams and HTTP errors, and explicitly allow the trajectory-id header value despite the stated “no header values” rule. -->

- prompt tokens, cached prompt tokens (where the engine reports them), completion tokens;
<!-- codex: Establish whether each engine's usage counts reasoning and tool arguments and whether cache counts represent actual reuse; missing usage must remain unknown, not zero. Tokenizers and accounting can differ, so raw tokens/sec is supplementary to task completion time, and retry token totals must not silently omit aborted generations. -->
- reasoning characters (journal `attempt.completed.output.reasoningChars`) and the think time (first reasoning
  delta → first output delta);
<!-- codex: This is an observed reasoning interval, not exact thinking compute: the runbook says Responses forwards only leading reasoning and counts later reasoning without forwarding it. Handle absent reasoning, reasoning-only output and missing visible deltas explicitly, and do not derive token shares from character counts. -->
- time to first token (sent → first delta of any kind), minus any `admission.queued` wait from the journal;
<!-- codex: Count only non-empty generated reasoning, text or tool-argument content, excluding response.created, progress events and other metadata; one delta can contain multiple tokens. Report raw client-observed latency alongside queue-adjusted latency, using measured admission wait duration rather than an event's presence. -->
- decode tok/s = completion tokens / (completed − first token);
<!-- codex: This is an approximate client-observed output rate, not backend decode throughput: usage includes tokens already present in the first chunk, while the denominator includes buffering and terminal-event delay. Prefer matched engine timings for decode throughput and mark short, buffered, zero-duration or incomplete responses unsuitable for this estimate. -->
- prefill tok/s ≈ (prompt − cached) / (time to first token − queue wait);
<!-- codex: Queue wait is subtracted twice if “time to first token” means the already adjusted metric above. Even with that fixed, the denominator includes routing, transport, backend scheduling and initial generation, so label this a latency-derived proxy rather than prefill throughput unless matched backend prefill timings are available. -->
- the journal's own `attempt.started` → `stream.first_content` and → `attempt.completed`, joined by trajectory id,
  as the cross-check. For cloud3, llama.cpp's own per-request `timings` where llama-swap logs them; for cloud2,
  vLLM's metric deltas only as a sanity check (cloud2 serves other agents at the same time).
<!-- codex: A trajectory can contain multiple backend attempts, while one Codex turn can make several HTTP requests; persist request, trajectory and attempt mappings rather than assuming a one-to-one join. Confirm per-request backend timing availability in the dry run and leave unavailable throughput unknown instead of attributing shared vLLM metric deltas to this benchmark. -->

**Speed, per task.** Wall duration; number of model requests (turns); total prompt and completion tokens; thinking
vs answering share of the generation time; idle time between requests (tool execution).
<!-- codex: HTTP requests are not synonymous with turns, and gaps also include retry backoff, CLI processing and compaction rather than only tool execution. Derive tool intervals from Codex events, distinguish logical turns/retries/compaction, and define wall time from launch to exit with external grading excluded. -->

**Robustness (failure modes).** Counted per run from the Codex JSONL events, the rollout file and the journal:
tool-call errors (malformed arguments, unknown tools), tool-call markup in text or arguments (`<tool_call>`,
`<parameter`, `</function>`), loops (the same command or the same text 3+ times in a row, repeated n-grams like
`show show show`), garbled copied values (a path, identifier or number the agent copied that does not exist in the
source it read; checked by hand on failing runs), early stops (a final message without the work done), timeouts,
reconnects ("Reconnecting... n/8"), 429s, refusals, and Codex errors. Each with a short excerpt.
<!-- codex: Repeated polling, literal markup in source and newly introduced identifiers can be legitimate, so treat these patterns as flags requiring adjudication rather than automatic failures. Review flagged successful runs too, apply the same rules to both models, and report both run-level incidence and exposure counts such as tool calls or requests. -->

### 5. Report

`plans/2026-11-03-local-model-agent-benchmark-results.md`:
- implementation pass rate per model per task, and overall, with both repeats shown (variance, not only means);
- plan scores per model per task (each judge criterion), the automatic grounding rate;
- per-task side-by-side table: wall time, turns, tokens, think share;
<!-- codex: Show success-conditioned speed together with failure rate and capped run durations; an agent that stops early can otherwise appear faster. A timeout duration is a lower bound on time to completion, not an observed completion time. -->
- per-request speed: median and p90 per model of time to first token, decode tok/s, prefill tok/s; and the same
  bucketed by prompt size (< 10k, 10-30k, 30-100k, > 100k), since a 6B-active MoE and a dense 27B scale differently;
<!-- codex: Report bucket counts, exact inclusive boundaries and missing observations, and avoid stable-looking p90s from tiny buckets. Agent-selected prompts differ between models and requests within a task are correlated, so pooled request statistics describe their workloads rather than a controlled throughput comparison. -->
- failure modes with excerpts; every discarded run and why;
- the exact command lines, base commits, task texts (in an appendix or a linked file), and the seeds;
- the caveat that speed is "as deployed": 3 cards on cloud3 against 2 cards on cloud2, different engines and quants,
  cloud2 shared with production traffic; not a like-for-like hardware comparison;
- a recommendation for cloud3's resident default model, with confidence and caveats. The decision rule, fixed now:
  correctness first (implementation pass rate, then plan score); robustness second (any long-context corruption on
  flash-next weighs heavily, since it was the reason it was distrusted); speed third. A difference of one task in 12
  runs is within noise and is reported as a tie.
<!-- codex: Twelve implementation runs represent only six task clusters, and a fixed one-run tie threshold has no statistical basis; distinguish a practical tie from insufficient evidence. Predeclare meaningful quality margins and symmetric robustness criteria, report paired task-level uncertainty, and allow an inconclusive recommendation that triggers additional independent tasks. -->

Phase B of the planning process is a codex review (`--profile review`) of the results report instead of a code diff.
<!-- codex: Make this a check of evidence, exclusions and decision-rule application, not an opportunity to redefine scoring after seeing the winner. A report review cannot compensate for missing samples or contaminated runs. -->

## Critical files

- `plans/2026-11-03-local-model-agent-benchmark-plan.md` (this plan), `plans/2026-11-03-local-model-agent-benchmark-results.md`.
- Harness scripts live in the session scratchpad on the workstation and in `/workspace/c4/bench/` on dev-worker-1
  (both temporary; the results file records the commands). Nothing in the repo's code or config changes.
<!-- codex: Command lines cannot reproduce a deleted proxy, wrapper, grader or judging prompt. Retain a compact, versioned harness bundle plus sanitized manifests, candidate patches/plans and raw measurement artifacts outside the worker, with hashes and a durable location linked from the report. -->

## Verification

- Each implementation task: parent fails the graded tests, parent + real commit passes them (recorded).
- Dry run: I1 on both models end to end (workspace, run, timing tap, journal join, grading) before the full set.
<!-- codex: I1 alone does not exercise the harness's important failure paths: add small scripted checks for fragmented SSE, reasoning-only output, failed/incomplete terminals, missing usage, retries and cancellation. Also prove isolation from answers and sibling runs, and that termination leaves no benchmark tool container or upstream request active. -->
- The timing tap is checked on the dry run against the journal: same request count, matching trajectory ids, and
  the tap's request durations within ~1 s of the journal's.
<!-- codex: Tap and journal intervals have different boundaries, particularly during admission waits, retries and proxy holds, so a universal one-second agreement requirement is invalid. Compare matched attempt intervals and explain queue/transport differences; validate measurement overhead with a scripted direct-versus-tap control. -->
- At least one run per model reaches prompts over 30k tokens (from the tap's usage); the planning tasks over 100k.
<!-- codex: Large repository files do not guarantee large active prompts: agents choose what to read, and compaction can remove earlier content. If these thresholds are required, predefine identical relevant context inputs or a separate bounded stress task, verify upstream prompt sizes and checks after the threshold, and do not infer reliability from one long run. -->
- Every number in the results comes from a file kept in the scratchpad until the PR merges.
<!-- codex: Retaining evidence only until merge prevents later audit of the resident-model decision. Define a modest durable retention period and preserve sanitized raw observations, exclusions and scoring outputs while removing copied credentials and unnecessary bulk data. -->
- Clean-up check at the end: `/workspace/c4/bench` gone, no bench containers or images left, `df -h /workspace` back.
<!-- codex: Capture pre-run disk/inode and Docker inventories, label benchmark resources, and clean up on failure as well as success after evidence is exported. Verify resolved deletion paths and remove only benchmark-owned artifacts; shared images/stores and unrelated worker data must not be pruned to force disk usage back to an exact baseline. -->

<!-- codex-review-status: complete -->
