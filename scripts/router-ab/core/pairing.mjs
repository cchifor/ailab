// core/pairing.mjs - account pairing for routes the client cannot pin. The Codex backend caches prompts per account: a
// turn on an account that has not seen the conversation is mostly uncached (median 33 % cached against 99 % on the same
// account, docs/runbooks/llm-router.md, "Warm spills release"). The router places each new Codex conversation on any
// account of the pool, and no request field pins it there, so a router-arm run that starts on another account than the
// direct arm's login measures the account, not the router.
//
// The rule (providers/lib/codex-session.mjs): only a run's FIRST router request decides. Its placement is made before the
// model says anything, so a run discarded for it is a random draw, independent of its time, cache, errors or work. A run
// discarded on its later requests would be a selection on outcome: a long, slow or failing session has more requests that
// can fail over or spill, so retrying those keeps the fast, cached runs and can turn a FAIL into a PASS. So:
//   - first request elsewhere: the run is discarded and redone (runPlaced); still elsewhere after the last try, the pair
//     is INVALID, "not paired", left out of every metric, and its slot runs again;
//   - first request on the account, later requests elsewhere: kept in every metric (a failover or a spill is router
//     behaviour), listed in the notes, and the verdict says how many pairs moved;
//   - every discarded first placement is listed in the notes.
// Pure functions; no hosts.
import { armsOf } from './abba.mjs';
import { evaluate, NO_BASELINE } from './margins.mjs';

export const NOT_PAIRED = 'INVALID (not paired; rerun)';

/** Undefined when every account is `want`; otherwise the reason the run is not paired. */
export function notPaired(accounts, want) {
  if (accounts.length && accounts.every((a) => a === want)) return undefined;
  return `not paired: router served ${accounts.length ? accounts.join(', ') : 'no account'}, the direct arm is on ${want}`;
}


/**
 * The run plan of one task: `pairs` logical slots in ABBA order. A slot whose pair turns out INVALID (not paired) runs
 * again later with the SAME orientation, so the kept pairs keep the requested direct-first/router-first balance. At most
 * `pairs` replacement pairs. next() -> { pair, slot, arms } or null; done(pair, valid) after each pair.
 */
export function schedule(pairs) {
  const queue = Array.from({ length: pairs }, (_, i) => i + 1);
  let pair = 0;
  const slotOf = new Map();
  return {
    next() {
      if (!queue.length || pair >= pairs * 2) return null;
      const slot = queue.shift(); pair += 1; slotOf.set(pair, slot);
      return { pair, slot, arms: armsOf(slot) };
    },
    done(p, valid) { if (!valid) queue.push(slotOf.get(p)); },
  };
}

/** Pair numbers whose runs are all present and none is `unpaired`, in order. */
export function validPairs(runs) {
  const by = new Map();
  for (const r of runs) by.set(r.pair, [...(by.get(r.pair) ?? []), r]);
  return [...by].filter(([, rs]) => rs.some((r) => r.arm === 'direct') && rs.some((r) => r.arm === 'router') && !rs.some((r) => r.metrics.unpaired)).map(([p]) => p);
}

/**
 * evaluate() on the valid pairs only, with the pairing in the notes and the verdict:
 * - a pair with an unpaired run is left out of every metric; fewer than `want` valid pairs gives no verdict (NOT_PAIRED);
 * - every discarded first placement and every kept mid-run move is listed;
 * - kept pairs that moved accounts mid-run are named in the verdict: "PASS (2 pairs moved accounts mid-run)".
 * With no pairing event at all this is exactly evaluate(runs, known).
 */
export function judgePairs(runs, { want, known } = {}) {
  const unpaired = runs.filter((r) => r.metrics.unpaired);
  const discarded = runs.filter((r) => r.metrics.discarded?.length && !r.metrics.unpaired);
  const ok = new Set(validPairs(runs));
  const moved = runs.filter((r) => r.metrics.moved && ok.has(r.pair));
  if (!unpaired.length && !discarded.length && !moved.length) return evaluate(runs, known);
  const v = evaluate(runs.filter((r) => ok.has(r.pair)), known);
  const notes = [
    ...unpaired.map((r) => `pair ${r.pair} INVALID, ${r.metrics.unpaired} (first request of each of ${r.metrics.discarded?.length ?? 1} tries): left out of every metric`),
    ...discarded.map((r) => `pair ${r.pair}: ${r.metrics.discarded.length} router run(s) discarded on their first request's account (${r.metrics.discarded.join(', ')}) before the kept one`),
    ...moved.map((r) => `pair ${r.pair}: the router run started on the direct arm's account and moved mid-run (${r.metrics.moved.join(', ')}): kept, a failover or spill is router behaviour`),
    ...v.notes,
  ];
  if (ok.size < want) {
    return { ...v, verdict: NOT_PAIRED, notes: [...notes, `${ok.size} of the ${want} pairs a verdict needs started on the direct arm's account: no verdict`] };
  }
  const movedPairs = new Set(moved.map((r) => r.pair)).size;
  const verdict = movedPairs && v.verdict !== NO_BASELINE ? `${v.verdict} (${movedPairs} pair${movedPairs > 1 ? 's' : ''} moved accounts mid-run)` : v.verdict;
  return { ...v, verdict, notes };
}
