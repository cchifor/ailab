// local/route-targets.mjs NAMESPACE ALIAS - runs INSIDE the router pod (core/context.mjs inPod) with the pod's own admin
// bearer, never printed. Prints, as JSON, the route that ALIAS names (a pointer followed one hop, as the router resolves
// it) and the accounts of that route's ENABLED deployments: { route, via?, accounts }. A qwen provider checks its backend
// is one of them before it runs: a backend the route does not use cannot be measured via the router (every run would be
// placed elsewhere and discarded). `enabled` is a required boolean in the router's config schema.
const [namespace, alias] = process.argv.slice(2);
const r = await fetch('http://127.0.0.1:8787/admin/v1/config', { headers: { authorization: 'Bearer ' + process.env.ROUTER_ADMIN_TOKEN } });
if (!r.ok) { console.error(`config ${r.status}`); process.exit(1); }
const policies = (await r.json()).policies;
const named = policies.find((p) => p.namespace === namespace && p.alias === alias);
const route = named?.target === undefined ? named : policies.find((p) => p.namespace === namespace && p.alias === named.target);
if (!route) { console.error(`no route ${namespace}/${alias}${named ? ` (pointer to ${named.target})` : ''}`); process.exit(1); }
console.log(JSON.stringify({ route: route.alias, ...(route === named ? {} : { via: alias }), accounts: route.deployments.filter((d) => d.enabled === true).map((d) => d.accountId) }));
