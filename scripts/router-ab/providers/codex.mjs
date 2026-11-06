// providers/codex.mjs - the Codex (ChatGPT) seat. Direct: Codex CLI with the worker's own ChatGPT login (built-in
// openai provider). Via the router: the same Codex CLI and home with the llm-router provider and the worker key, into
// pool-codex's native pass-through. Both arms use the real ~/.codex in place (the login is never copied: a second
// refresher would rotate its refresh token), model gpt-6-astra at effort medium.
// Layer (a): a recorded direct request sent both ways must reach the model with the same input tokens.
// Note: Codex builds a different request for an API provider than for its own login (no internal metadata, no remote
// skills), so the first request of a router-arm session misses the cross-conversation prefix cache. That is the client
// (ailab plans/2026-11-05-router-transparency-and-planning-revalidation.md §1.4); the uncached margin is reported, and
// read with that in mind.
import { runCodexTask, sameBody } from './lib/codex-session.mjs';

const MODEL = 'gpt-6-astra';
export default {
  name: 'codex',
  describe: 'ChatGPT seat via Codex CLI 0.166 (direct: own login; router: dw-N key, native pass-through)',
  kinds: ['codex-exec'],
  async layerA(ctx) {
    const r = sameBody(ctx, { target: 'chatgpt', run: 'layer-a-capture-codex', home: 'user', model: MODEL, directModel: MODEL, routerModel: MODEL, greedy: false, n: 2 });
    if (r.error) return [{ name: 'same body both ways', pass: false, detail: r.error }];
    const inputs = [...new Set(r.calls.map((c) => c.input).filter((x) => x != null))].join(', ');
    return [
      { name: 'same body: identical model input tokens direct and via the router', pass: r.same_input_tokens === true, detail: `input tokens ${inputs}` },
      { name: 'same body: every call answered', pass: r.complete === true, detail: r.calls.map((c) => `${c.arm}:${c.error ? `${c.http ?? 'error'} ${c.error.slice(0, 80)}` : 'ok'}`).join(' ') },
      { name: 'declared adaptations of the route (reported, not failed)', pass: true, detail: [...new Set(r.calls.flatMap((c) => Object.entries(c.declared ?? {}).map(([k, v]) => `${k.replace('x-router-', '')}: ${v}`)))].join('; ') || 'none' },
    ];
  },
  async run(ctx, task, arm, runId) {
    return runCodexTask(ctx, task, { target: arm === 'direct' ? 'chatgpt' : 'router', home: 'user', model: MODEL }, runId);
  },
};
