// core/seats.mjs - keeps pairs paired. The accounts a run's router requests reached, read from the router journal
// (local/journal-seats.mjs inside the pod). A run that failed over between accounts, or moved between them, is not a
// like-for-like comparison with the direct arm's seat (the router working as designed), so the provider marks it
// invalid and the gate reports the task INCOMPLETE.
import path from 'node:path';
import { HERE } from './context.mjs';

export function seatsOf(ctx, ids) {
  ids = (ids ?? []).filter(Boolean);
  if (!ids.length) return { accounts: [], moved: true, error: 'the run recorded no trajectory ids' };
  const r = ctx.inPod(path.join(HERE, 'local', 'journal-seats.mjs'), ids);
  // fail closed: a run whose seats cannot be read is not known to be paired
  let j;
  try { j = JSON.parse(r.stdout); } catch { return { accounts: [], moved: true, error: 'the router journal could not be read' }; }
  const missing = ids.filter((id) => !j[id]?.accounts?.length);
  if (missing.length) return { accounts: [], moved: true, error: `${missing.length} of ${ids.length} trajectories not in the router journal` };
  // every attempt counts: a failover inside one request shows as a second account. A retry on the same account is not a
  // move (it is judged by the retries margin).
  const all = [...new Set(ids.flatMap((id) => j[id].accounts))];
  return { accounts: all, moved: all.length > 1 };
}
