// core/abba.mjs - the run order: pairs interleaved in time (direct, router / router, direct / ...), so load and the time
// of day hit both arms alike (§9.6). One run at a time. The gate runs it through core/pairing.mjs schedule(), which
// replays a slot whose pair was not paired with that slot's own orientation.

/** The arm order of one pair (or ABBA slot): odd direct first, even router first. */
export const armsOf = (slot) => (slot % 2 === 1 ? ['direct', 'router'] : ['router', 'direct']);

/** [{ pair: 1, arm: 'direct' }, { pair: 1, arm: 'router' }, { pair: 2, arm: 'router' }, ...] */
export function abba(pairs) {
  const order = [];
  for (let p = 1; p <= pairs; p++) for (const arm of armsOf(p)) order.push({ pair: p, arm });
  return order;
}
