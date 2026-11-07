// providers/lib/qwen-backend.mjs - the provider for one backend of the qwen3.8-27b route; providers/qwen.mjs,
// providers/qwen-cloud3.mjs and providers/qwen-cloud1.mjs are one instance each. Direct: Codex CLI to that backend's own /v1/responses (which replays
// reasoning natively). Via the router: the same Codex CLI and generated home with the llm-router provider, pointer
// qwen-27b (translated path; both backends replay append-only). Both arms share one generated CODEX_HOME (web search
// off, multi-agent off, the same retry and idle budgets, the profile's 262,144 window).
//
// The route balances new conversations across its backends, and a client cannot pick one. A router run placed on
// another backend is discarded and run again: placement is decided before the model says anything, so discarding by
// placement cannot favour either arm. It costs one session each time. A backend that is not an enabled deployment of the
// route is refused at setup (INVALID, in seconds): no run could be placed on it.
//
// Layer (a): a recorded direct request with its reasoning items, greedy, both ways: identical input tokens and the same
// greedy output (a cold-cache call may differ numerically; the check needs one match, among the router calls that
// reached this backend). The capture runs at the tasks' effort, xhigh: the route ignores `reasoning_effort` (declared in
// X-Router-Ignored-Params) and the template's default is xhigh, so a body at another effort renders differently via the
// router (+38 tokens at medium, 2026-11-05).
import { runCodexTask, runPlaced, sameBody } from './codex-session.mjs';
import path from 'node:path';
import { accountsOf } from '../../core/seats.mjs';
import { HERE } from '../../core/context.mjs';

const LAYER_A_TRIES = 3;
const ROUTE = 'qwen3.8-27b'; // what pointer qwen-27b names
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

/** tap: the kit tap's target for this backend. account: the router's name for it. optIn: run only when named. */
export function qwenProvider({ name, host, server, tap, account, optIn = false }) {
  const model = 'qwen3.8-27b';
  return {
    name, optIn,
    describe: `qwen3.8-27b on ${host} via Codex CLI 0.166 (direct: its own /v1/responses; router: pointer qwen-27b, runs placed on ${account})`,
    kinds: ['codex-exec'],
    async setup(ctx) {
      const r = ctx.inPod(path.join(HERE, 'local', 'route-targets.mjs'), ['default', ROUTE]);
      let on;
      try { on = JSON.parse(r.stdout); } catch { throw new Error(`cannot read route ${ROUTE}: ${(r.stderr || r.stdout || '').slice(-160)}`); }
      if (!on.includes(account)) throw new Error(`${account} is not an enabled deployment of route ${ROUTE} (enabled: ${on.join(', ') || 'none'}): no router run can be placed on it`);
      ctx.ssh(`mkdir -p ${ctx.remote}/kit/codex-home && chmod 700 ${ctx.remote}/kit/codex-home && echo ${Buffer.from(config(server)).toString('base64')} | base64 -d > ${ctx.remote}/kit/codex-home/config.toml`);
    },
    async layerA(ctx) {
      const both = { direct: 'vllm', home: 'kit', directModel: model, routerModel: 'qwen-27b', greedy: true, serverUrl: server, effort: 'xhigh', n: 4 };
      // every comparison uses only the router calls that reached this backend, and the direct calls to it
      const here = (x) => x.calls.filter((c) => c.arm === 'router' && x.placed[c.trajectory] === account);
      const there = (x) => x.calls.filter((c) => c.arm === 'direct');
      const sameInput = (x) => x.complete === true && new Set([...there(x), ...here(x)].map((c) => c.input)).size === 1;
      const inputs = (x) => [...new Set([...there(x), ...here(x)].map((c) => c.input).filter((v) => v != null))].join(', ') || 'none (every call failed)';
      const placedBody = (opts) => {
        for (let attempt = 1; attempt <= LAYER_A_TRIES; attempt++) {
          const x = sameBody(ctx, { ...both, ...opts, run: `${opts.run}-${attempt}` });
          if (x.error) return x;
          x.placed = accountsOf(ctx, x.calls.filter((c) => c.arm === 'router').map((c) => c.trajectory));
          if (!x.complete || here(x).length) return x;
          ctx.log(`${name}: no same-body call reached ${account}: capturing again`);
        }
        throw new Error(`no same-body call reached ${account} in ${LAYER_A_TRIES} tries`);
      };
      const r = placedBody({ target: tap, run: `layer-a-capture-${name}`, model });
      if (r.error) return [{ name: 'same body both ways', pass: false, detail: r.error }];
      // the router arm's later prompts are built from what the router relayed, so that body must render the same too
      const rr = placedBody({ target: 'router', run: `layer-a-capture-${name}-router`, model: 'qwen-27b' });
      const direct = new Set(there(r).map((c) => c.hash));
      return [
        { name: 'same body (with reasoning items): identical model input tokens', pass: sameInput(r), detail: `input tokens ${inputs(r)}` },
        { name: `same body, greedy: the router output on this backend matches a direct output (${here(r).length} of ${r.calls.length / 2} router calls placed here)`, pass: r.complete === true && here(r).some((c) => direct.has(c.hash)),
          detail: r.calls.map((c) => `${c.arm}:${c.error ? `${c.http ?? 'error'} ${c.error.slice(0, 80)}` : c.hash}`).join(' ') },
        { name: 'body recorded on the router arm (the items it relayed): identical model input tokens', pass: !rr.error && sameInput(rr), detail: rr.error ?? `input tokens ${inputs(rr)}` },
        { name: 'declared adaptations of the route (reported, not failed)', pass: true, detail: [...new Set(r.calls.flatMap((c) => Object.entries(c.declared ?? {}).map(([k, v]) => `${k.replace('x-router-', '')}: ${v}`)))].join('; ') || 'none' },
      ];
    },
    async run(ctx, task, arm, runId) {
      const env = { SERVER_URL: server };
      if (arm === 'direct') return runCodexTask(ctx, task, { target: tap, home: 'kit', model, effort: 'xhigh' }, runId, env);
      return runPlaced(ctx, task, { target: 'router', home: 'kit', model: 'qwen-27b', effort: 'xhigh', account }, runId, env);
    },
  };
}
