// core/margins.mjs - the §9.6 non-inferiority margins (plans/2026-11-04-router-standard-design.md) and the verdict.
// Pure functions: a list of per-run metrics in, one row per metric out. A provider reports only the metrics it can
// measure; a metric with no data on either arm is reported as "n/a", never as a pass.
//
// Run metrics (what a provider's run() returns): { pass, wall_s, tool_calls, rejected, ttft_s, uncached_tokens,
// output_tokens, requests, router_errors, retries }. Every field but pass and wall_s is optional.
//
// Known deviations: a task entry may declare `known: { <margin id>: { bound, reason } }` for a deviation that is
// measured, understood and accepted until a named fix lands. The metric is then judged against `bound` instead of the
// §9.6 bound: within it the status is KNOWN (it does not fail the gate, and the report prints the reason), beyond it
// FAIL. A metric back inside the §9.6 bound is a plain PASS, and the report says the known deviation no longer shows.

const median = (xs) => { const s = xs.filter((x) => typeof x === 'number' && !Number.isNaN(x)).sort((a, b) => a - b); return s.length ? (s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2) : null; };
const sum = (xs) => xs.reduce((n, x) => n + (typeof x === 'number' ? x : 0), 0);
const has = (runs, k) => runs.some((r) => typeof r[k] === 'number');

/** Below this median output, a session's time per token says nothing: raw wall is judged instead. */
const MIN_WORK_TOKENS = 200;

/** Status of a value against an upper bound: FAIL above it, NEAR within half a margin of it. */
function against(value, bound, halfMargin) {
  if (value === null || bound === null) return 'n/a';
  if (value > bound) return 'FAIL';
  if (value >= bound - halfMargin) return 'NEAR';
  return 'PASS';
}

/**
 * The margin table. Each entry: id, what is compared, how a value is computed from one arm's runs, and the check.
 * Adding a metric is one entry here; nothing else in the gate changes.
 */
export const MARGINS = [
  {
    id: 'success', label: 'Task success', margin: 'at most 1 paired failure more than direct in 6 pairs',
    value: (runs) => runs.filter((r) => !r.pass).length,
    // Under 6 pairs one extra failure is within half a margin and two are outside it (both: 3 more pairs, see
    // evaluate); from 6 pairs on, the margin is one extra failure per 6 pairs.
    check: (d, r, pairs) => {
      const extra = r - d;
      if (pairs < 6) return extra >= 2 ? 'FAIL' : extra === 1 ? 'NEAR' : 'PASS';
      return extra > Math.floor(pairs / 6) ? 'FAIL' : 'PASS';
    },
    show: (v, runs) => `${runs.length - v}/${runs.length}`,
  },
  {
    id: 'tool_validity', label: 'Rejected tool calls', margin: 'rate at most direct + 2 pp', needs: 'tool_calls',
    value: (runs) => { const c = sum(runs.map((r) => r.tool_calls)); return c ? sum(runs.map((r) => r.rejected)) / c : 0; },
    bound: 0.02, check: (d, r, _p, b) => against(r, d + b, b / 2),
    show: (v) => `${(v * 100).toFixed(1)} %`,
  },
  {
    id: 'uncached', label: 'Uncached prompt tokens', margin: 'total at most 1.2x direct', needs: 'uncached_tokens',
    value: (runs) => sum(runs.map((r) => r.uncached_tokens)),
    bound: 1.2, check: (d, r, _p, b) => (d === 0 ? (r === 0 ? 'PASS' : 'FAIL') : against(r / d, b, (b - 1) / 2)),
    show: (v) => String(Math.round(v)),
  },
  {
    id: 'ttft', label: 'TTFT median', margin: 'at most direct + 2 s', needs: 'ttft_s',
    value: (runs) => median(runs.map((r) => r.ttft_s)),
    bound: 2, check: (d, r, _p, b) => against(r, d + b, b / 2),
    show: (v) => (v === null ? '-' : `${v.toFixed(2)} s`),
  },
  {
    // The wall margin exists to catch time the ROUTER adds. An agent session's raw wall time is mostly model work, and
    // that is sampled: at qwen's temperature 1.0 one request ran from 214 to 3,335 output tokens on the same body
    // (2026-11-06), so two sessions of one task differ 2x with no router involved. So when a provider reports output
    // tokens, the same 1.25x bound is judged on wall time per 1k output tokens. Raw wall stays in the row, and the work
    // itself is the next row, so more work via the router stays visible. Without output tokens, raw wall is judged, and
    // so it is below MIN_WORK_TOKENS: on a 30-token session the ratio is mostly the denominator (2026-11-06: 171 vs 221 s
    // per 1k tokens on a session the router ran faster, 7.7 vs 7.1 s).
    id: 'wall', label: 'Wall time median', margin: `at most 1.25x direct, per 1k output tokens from ${MIN_WORK_TOKENS} tokens up`,
    value: (runs) => {
      const timed = runs.filter((r) => typeof r.wall_s === 'number');
      const enough = timed.length && timed.every((r) => r.output_tokens > 0) && median(timed.map((r) => r.output_tokens)) >= MIN_WORK_TOKENS;
      return { raw: median(timed.map((r) => r.wall_s)), perK: enough ? median(timed.map((r) => (r.wall_s / r.output_tokens) * 1000)) : null };
    },
    bound: 1.25,
    check: (d, r, _p, b) => {
      const [x, y] = d.perK !== null && r.perK !== null ? [d.perK, r.perK] : [d.raw, r.raw];
      return x ? against(y / x, b, (b - 1) / 2) : 'n/a';
    },
    show: (v) => (v.raw === null ? '-' : `${v.raw.toFixed(1)} s${v.perK !== null ? ` (${v.perK.toFixed(1)} s per 1k tok)` : ''}`),
  },
  {
    // Not a §9.6 margin: §9.6 lists output tokens as measured, with no bound. Reported so a router that makes the model
    // work harder cannot hide behind the per-token wall time. WATCH never fails the gate by itself; Layer (a) says whether
    // the model saw the same request. Same input and more work is sampling; a Layer (a) difference is a defect.
    id: 'work', label: 'Model work: output tokens, requests (median)', margin: 'reported; WATCH above 1.25x direct', needs: 'output_tokens',
    value: (runs) => ({ tokens: median(runs.map((r) => r.output_tokens)), requests: median(runs.map((r) => r.requests)) }),
    check: (d, r) => (d.tokens && r.tokens / d.tokens > 1.25 ? 'WATCH' : 'PASS'),
    show: (v) => `${v.tokens} / ${v.requests}`,
  },
  {
    // decisive at any sample size: a router-originated error is a defect, not a sampling question
    id: 'errors', label: 'Router errors, retries', margin: 'no router-originated error; retries at most direct', decisive: true,
    value: (runs) => ({ errors: sum(runs.map((r) => r.router_errors)), retries: sum(runs.map((r) => r.retries)) }),
    check: (d, r) => (r.errors > 0 || r.retries > d.retries ? 'FAIL' : 'PASS'),
    show: (v) => `${v.errors} / ${v.retries}`,
  },
];

/**
 * §9.6's sample-size rule: "Start with 3 pairs per task. A metric outside its margin, or within half a margin of it,
 * takes 3 more pairs, and the decision uses all 6." Below 6 pairs a metric outside its margin is therefore OUT (not yet
 * a FAIL); NEAR and OUT both ask for a rerun with 6 pairs. At 6 pairs the decision is made: NEAR is inside the margin
 * and passes. A `decisive` margin fails at any sample size.
 */
export const DECISION_PAIRS = 6;
export const NO_BASELINE = 'INVALID (no direct baseline; rerun)';

/**
 * Evaluates one task's runs: [{ arm: 'direct'|'router', pair, metrics }], with the task's known deviations (see the
 * top of this file). Returns rows, the task verdict and notes on the known deviations.
 */
export function evaluate(runs, known = {}) {
  const by = (arm) => runs.filter((x) => x.arm === arm).map((x) => x.metrics);
  const d = by('direct'), r = by('router'), pairs = Math.min(d.length, r.length);
  const notes = [];
  const rows = MARGINS.map((m) => {
    if (m.needs && !has(d, m.needs) && !has(r, m.needs)) return { id: m.id, label: m.label, margin: m.margin, direct: 'n/a', router: 'n/a', status: 'n/a' };
    const dv = m.value(d), rv = m.value(r);
    let status = m.check(dv, rv, pairs, m.bound);
    const k = known[m.id];
    if (k && m.bound !== undefined) {
      if (status === 'PASS') notes.push(`known deviation on ${m.label.toLowerCase()} no longer shows: retire it from the registry (${k.reason})`);
      else if (status === 'FAIL' && m.check(dv, rv, pairs, k.bound) !== 'FAIL') { status = 'KNOWN'; notes.push(`KNOWN, ${m.label.toLowerCase()}: ${k.reason}`); }
    }
    if (status === 'FAIL' && pairs < DECISION_PAIRS && !m.decisive) status = 'OUT';
    if (status === 'WATCH') notes.push(`WATCH, ${m.label.toLowerCase()}: more model work via the router; read it with Layer (a)`);
    return { id: m.id, label: m.label, margin: m.margin, direct: m.show(dv, d), router: m.show(rv, r), status };
  });
  const statuses = rows.map((x) => x.status);
  // Non-inferiority needs something to be inferior to. If every direct run failed, the arm's figures describe the
  // failure (2026-11-07: an expired login, 401 in 2.5 s), and any comparison with them passes or fails by accident.
  if (d.every((x) => !x.pass)) {   // true for no direct runs at all, which is no baseline either
    return { rows, verdict: NO_BASELINE, pairs, notes: [...notes, `no direct run passed, so nothing was compared: read the direct arm's error first`] };
  }
  const verdict = statuses.includes('FAIL') ? 'FAIL'
    : pairs < DECISION_PAIRS && (statuses.includes('OUT') || statuses.includes('NEAR')) ? 'RERUN (6 pairs decide, §9.6)'
    : statuses.includes('KNOWN') ? 'PASS (known deviation)' : 'PASS';
  return { rows, verdict, pairs, notes };
}

/** Layer (a): every check must pass. */
export function evaluateLayerA(checks) {
  return { checks, verdict: checks.length && checks.every((c) => c.pass) ? 'PASS' : checks.length ? 'FAIL' : 'n/a' };
}
