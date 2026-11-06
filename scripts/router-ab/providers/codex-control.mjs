// providers/codex-control.mjs - the Codex seat with the client held constant. Codex builds a different request for an API
// provider than for its own login, so providers/codex.mjs's arms differ in the client as well as in the router. Here both
// arms are the same Codex CLI on the same provider entry (llm-router, API mode), on the same seat:
//   direct: the control. kit/tap.mjs target `fwd` relays inference to the ChatGPT backend with the host login, doing only
//           what the router's codex adapter does to a native request, with no router logic. The model catalogue still
//           comes from the router, so the client sees the same thing in both arms.
//   router: the same client via the router, kept on the host login's seat (a run placed on another seat runs again).
// The two arms differ only by the router. Layer (a): a body recorded on the control arm, sent both ways.
import { runPlaced, sameBody } from './lib/codex-session.mjs';

const MODEL = 'gpt-6-astra';
const SEAT = process.env.RAB_CODEX_ACCOUNT ?? 'codex-2'; // the router's name for the seat of the worker's own login
export default {
  name: 'codex-control',
  describe: `ChatGPT seat via Codex CLI 0.166 in API-provider mode in both arms (direct: plain forwarder on the host login; router: pool-codex, runs placed on ${SEAT})`,
  kinds: ['codex-exec'],
  forwarder: true, // the tap's `fwd` target stays off unless a selected provider asks for it
  async layerA(ctx) {
    const r = sameBody(ctx, { target: 'fwd', direct: 'chatgpt', run: 'layer-a-capture-codex-control', home: 'user', model: MODEL, directModel: MODEL, routerModel: MODEL, greedy: false, n: 2 });
    if (r.error) return [{ name: 'same body both ways', pass: false, detail: r.error }];
    return [
      { name: 'same API-mode body: identical model input tokens direct and via the router', pass: r.same_input_tokens === true, detail: `input tokens ${[...new Set(r.calls.map((c) => c.input).filter((x) => x != null))].join(', ')}` },
      { name: 'same API-mode body: every call answered', pass: r.complete === true, detail: r.calls.map((c) => `${c.arm}:${c.error ? `${c.http ?? 'error'} ${c.error.slice(0, 80)}` : 'ok'}`).join(' ') },
    ];
  },
  async run(ctx, task, arm, runId) {
    if (arm === 'direct') return runPlaced(ctx, task, { target: 'fwd', home: 'user', model: MODEL }, runId);
    return runPlaced(ctx, task, { target: 'router', home: 'user', model: MODEL, account: SEAT }, runId);
  },
};
