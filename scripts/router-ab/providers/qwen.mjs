// providers/qwen.mjs - qwen3.8-27b on cloud2's vLLM. Direct: Codex CLI to vLLM's own /v1/responses (which replays
// reasoning natively). Via the router: the same Codex CLI and generated home with the llm-router provider, pointer
// qwen-27b (translated path; cloud2 replays append-only since config rev 2229). Both arms share one generated
// CODEX_HOME (web search off, multi-agent off, the same retry and idle budgets, the profile's 262,144 window).
// Layer (a): a recorded direct request with its reasoning items, greedy, both ways: identical input tokens and the
// same greedy output (a cold-cache call may differ numerically; the check needs one match). The capture runs at the
// tasks' effort, xhigh: the route ignores `reasoning_effort` (declared in X-Router-Ignored-Params) and the template's
// default is xhigh, so a body at another effort renders differently via the router (+38 tokens at medium, 2026-11-05).
import { runCodexTask, sameBody } from './lib/codex-session.mjs';

const SERVER = 'http://192.168.0.28:18020/v1';
const ACCOUNT = 'lan-192.168.0.28-18020'; // the direct arm's server, as the router names it: the router arm must land here
const CONFIG = `model_reasoning_effort = "xhigh"
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
base_url = "${SERVER}"
wire_api = "responses"
stream_max_retries = 8
request_max_retries = 6
stream_idle_timeout_ms = 900000
`;
export default {
  name: 'qwen',
  describe: 'qwen3.8-27b on cloud2 vLLM via Codex CLI 0.166 (direct: vLLM /v1/responses; router: pointer qwen-27b)',
  kinds: ['codex-exec'],
  async setup(ctx) {
    ctx.ssh(`mkdir -p ${ctx.remote}/kit/codex-home && chmod 700 ${ctx.remote}/kit/codex-home && echo ${Buffer.from(CONFIG).toString('base64')} | base64 -d > ${ctx.remote}/kit/codex-home/config.toml`);
  },
  async layerA(ctx) {
    const r = sameBody(ctx, { target: 'vllm', run: 'layer-a-capture-qwen', home: 'kit', model: 'qwen3.8-27b', directModel: 'qwen3.8-27b', routerModel: 'qwen-27b', greedy: true, n: 2, serverUrl: SERVER, effort: 'xhigh' });
    if (r.error) return [{ name: 'same body both ways', pass: false, detail: r.error }];
    const inputs = [...new Set(r.calls.map((c) => c.input).filter((x) => x != null))].join(', ');
    // the router arm's later prompts are built from what the router relayed, so that body must render the same too
    const rr = sameBody(ctx, { target: 'router', direct: 'vllm', run: 'layer-a-capture-router', home: 'kit', model: 'qwen-27b', directModel: 'qwen3.8-27b', routerModel: 'qwen-27b', greedy: true, n: 1, serverUrl: SERVER, effort: 'xhigh' });
    return [
      { name: 'same body (with reasoning items): identical model input tokens', pass: r.same_input_tokens === true, detail: `input tokens ${inputs || 'none (every call failed)'}` },
      { name: 'same body, greedy: the router output matches a direct output', pass: r.router_hash_matches_direct === true, detail: r.calls.map((c) => `${c.arm}:${c.error ? `${c.http ?? 'error'} ${c.error.slice(0, 80)}` : c.hash}`).join(' ') },
      { name: 'body recorded on the router arm (the items it relayed): identical model input tokens', pass: rr.same_input_tokens === true, detail: rr.error ?? `input tokens ${[...new Set(rr.calls.map((c) => c.input).filter((x) => x != null))].join(', ') || 'none'}` },
      { name: 'declared adaptations of the route (reported, not failed)', pass: true, detail: [...new Set(r.calls.flatMap((c) => Object.entries(c.declared ?? {}).map(([k, v]) => `${k.replace('x-router-', '')}: ${v}`)))].join('; ') || 'none' },
    ];
  },
  async run(ctx, task, arm, runId) {
    return runCodexTask(ctx, task, arm === 'direct' ? { target: 'vllm', home: 'kit', model: 'qwen3.8-27b', effort: 'xhigh' } : { target: 'router', home: 'kit', model: 'qwen-27b', effort: 'xhigh', account: ACCOUNT }, runId, { SERVER_URL: SERVER });
  },
};
