// local/journal-seats.mjs TRAJECTORY_ID... - runs INSIDE the router pod (core/context.mjs inPod) with the pod's own admin
// bearer, never printed. For each trajectory: the accounts its attempts went to. The gate uses it to keep pairs paired:
// a run whose requests failed over to another account, or moved between accounts, is marked INVALID, since the pairing
// with the direct arm's seat is broken (the router working as designed, but not a like-for-like comparison).
const H = { authorization: 'Bearer ' + process.env.ROUTER_ADMIN_TOKEN };
const out = {};
for (const id of process.argv.slice(2)) {
  const r = await fetch(`http://127.0.0.1:8787/admin/v1/trajectories/${id}`, { headers: H });
  if (!r.ok) { out[id] = { error: r.status }; continue; }
  const j = await r.json(); const t = j.trajectory ?? j;
  out[id] = { accounts: (t.events ?? []).filter((e) => e.type === 'attempt.started').map((e) => e.data?.accountId) };
}
console.log(JSON.stringify(out));
