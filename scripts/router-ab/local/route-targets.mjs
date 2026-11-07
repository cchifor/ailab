// local/route-targets.mjs NAMESPACE ROUTE - runs INSIDE the router pod (core/context.mjs inPod) with the pod's own admin
// bearer, never printed. Prints the accounts of the route's ENABLED deployments, as JSON. A qwen provider checks its
// backend is one of them before it runs: a backend the route does not use cannot be measured via the router (every run
// would be placed elsewhere and discarded).
const [namespace, alias] = process.argv.slice(2);
const r = await fetch('http://127.0.0.1:8787/admin/v1/config', { headers: { authorization: 'Bearer ' + process.env.ROUTER_ADMIN_TOKEN } });
if (!r.ok) { console.error(`config ${r.status}`); process.exit(1); }
const route = (await r.json()).policies.find((p) => p.namespace === namespace && p.alias === alias);
if (!route) { console.error(`no route ${namespace}/${alias}`); process.exit(1); }
console.log(JSON.stringify(route.deployments.filter((d) => d.enabled).map((d) => d.accountId)));
