// core/abba.mjs - the run order: pairs interleaved in time (direct, router / router, direct / ...), so load and the time
// of day hit both arms alike (§9.6). One run at a time.

/** [{ pair: 1, arm: 'direct' }, { pair: 1, arm: 'router' }, { pair: 2, arm: 'router' }, ...] */
export function abba(pairs) {
  const order = [];
  for (let p = 1; p <= pairs; p++) {
    const arms = p % 2 === 1 ? ['direct', 'router'] : ['router', 'direct'];
    for (const arm of arms) order.push({ pair: p, arm });
  }
  return order;
}
