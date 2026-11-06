// providers/lib/qwen-backend.mjs - the provider for one backend of the qwen3.8-27b route; providers/qwen.mjs and
// providers/qwen-cloud3.mjs are one instance each. Direct: Codex CLI to that backend's own /v1/responses (which replays
// reasoning natively). Via the router: the same Codex CLI and generated home with the llm-router provider, pointer
// qwen-27b (translated path; both backends replay append-only). Both arms share one generated CODEX_HOME (web search
// off, multi-agent off, the same retry and idle budgets, the profile's 262,144 window).
//
// The route balances new conversations across its backends, and a client cannot pick one. A router run placed on
// another backend is discarded and run again: placement is decided before the model says anything, so discarding by
// placement cannot favour either arm. It costs one session each time.
//
// Layer (a): a recorded direct request with its reasoning items, greedy, both ways: identical input tokens and the same
// greedy output (a cold-cache call may differ numerically; the check needs one match, among the router calls that
// reached this backend). The capture runs at the tasks' effort, xhigh: the route ignores `reasoning_effort` (declared in
// X-Router-Ignored-Params) and the template's default is xhigh, so a body at another effort renders differently via the
// router (+38 tokens at medium, 2026-11-05).
import { runCodexTask, sameBody, Misplaced } from './codex-session.mjs';
import { accountsOf } from '../../core/seats.mjs';

const MAX_PLACEMENTS = 8;
const config = (server) => `model_reasoning_effort = "xhigh"
web_search = "disabled"
model_context_window = 262144
[features]
multi_agent = false
api_key_model_discovery = false
[model_providers.llm-router]
name = "llm-router"
base_url = "https://router.chifor.me/v1"
wire_api = "responses"
auth = { command = "/usr/local/bin/codex-router-key" }
stream_max_retries = 8
request_max_retries = 6
stream_idle_timeout_ms = 900000
[model_providers.server]
name = "server"
base_url = "${server}"
wire_api = "responses"
stream_max_retries = 8
request_max_retries = 6
stream_idle_timeout_ms = 900000
`;

/** tap: the kit tap's target for this backend. account: the router's name for it. */
export function qwenProvider({ name, host, server, tap, account }) {
  const model = 'qwen3.8-27b';
  return {
    name,
    describe: `qwen3.8-27b on ${host} via Codex CLI 0.166 (direct: its own /v1/responses; router: pointer qwen-27b, runs placed on ${account})`,
    kinds: ['codex-exec'],
    async setup(ctx) {
      ctx.ssh(`mkdir -p ${ctx.remote}/kit/codex-home && chmod 700 ${ctx.remote}/kit/codex-home && echo ${Buffer.from(config(server)).toString('base64')} | base64 -d > ${ctx.remote}/kit/codex-home/config.toml`);
    },
    async layerA(ctx) {
      const both = { direct: 'vllm', home: 'kit', directModel: model, routerModel: 'qwen-27b', greedy: true, serverUrl: server, effort: 'xhigh' };
      const r = sameBody(ctx, { ...both, target: tap, run: `layer-a-capture-${name}`, model, n: 4 });
      if (r.error) return [{ name: 'same body both ways', pass: false, detail: r.error }];
      const placed = accountsOf(ctx, r.calls.filter((c) => c.arm === 'router').map((c) => c.trajectory));
      const here = r.calls.filter((c) => c.arm === 'router' && placed[c.trajectory] === account);
      if (r.complete && !here.length) throw new Error(`no same-body call reached ${account}`);
      const direct = new Set(r.calls.filter((c) => c.arm === 'direct').map((c) => c.hash));
      const inputs = [...new Set(r.calls.map((c) => c.input).filter((x) => x != null))].join(', ');
      // Both input-token checks cover every router call, wherever it was placed: the router renders the prompt and the
      // tokenizer is shared, so placement cannot change them, and a mismatch is a router difference either way. Only the
      // greedy output depends on the backend, so only that check filters by placement.
      // The router arm's later prompts are built from what the router relayed, so that body must render the same too.
      const rr = sameBody(ctx, { ...both, target: 'router', run: `layer-a-capture-${name}-router`, model: 'qwen-27b', n: 1 });
      return [
        { name: 'same body (with reasoning items): identical model input tokens', pass: r.same_input_tokens === true, detail: `input tokens ${inputs || 'none (every call failed)'}` },
        { name: `same body, greedy: the router output on this backend matches a direct output (${here.length} of ${r.calls.length / 2} router calls placed here)`, pass: r.complete === true && here.some((c) => direct.has(c.hash)),
          detail: r.calls.map((c) => `${c.arm}:${c.error ? `${c.http ?? 'error'} ${c.error.slice(0, 80)}` : c.hash}`).join(' ') },
        { name: 'body recorded on the router arm (the items it relayed): identical model input tokens', pass: rr.same_input_tokens === true, detail: rr.error ?? `input tokens ${[...new Set(rr.calls.map((c) => c.input).filter((x) => x != null))].join(', ') || 'none'}` },
        { name: 'declared adaptations of the route (reported, not failed)', pass: true, detail: [...new Set(r.calls.flatMap((c) => Object.entries(c.declared ?? {}).map(([k, v]) => `${k.replace('x-router-', '')}: ${v}`)))].join('; ') || 'none' },
      ];
    },
    async run(ctx, task, arm, runId) {
      const env = { SERVER_URL: server };
      if (arm === 'direct') return runCodexTask(ctx, task, { target: tap, home: 'kit', model, effort: 'xhigh' }, runId, env);
      for (let attempt = 1; ; attempt++) {
        try {
          return runCodexTask(ctx, task, { target: 'router', home: 'kit', model: 'qwen-27b', effort: 'xhigh', account }, attempt === 1 ? runId : `${runId}-p${attempt}`, env);
        } catch (e) {
          if (!(e instanceof Misplaced) || attempt === MAX_PLACEMENTS) throw e;
          ctx.log(`${runId}: ${e.message}: discarded, running it again`);
        }
      }
    },
  };
}
