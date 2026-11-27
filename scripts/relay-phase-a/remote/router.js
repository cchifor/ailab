// router.js: read-only phase A observations INSIDE the llm-router pod (piped to `node -` by the host observer; the
// admin token never leaves the pod). One mode per call, one JSON line on stdout, exit 0 only on success.
//   trajectory KEY ROUTE fixed SINCE NOT_BEFORE      - the driver's snapshot envelope for an existing window
//   trajectory KEY ROUTE after SINCE_AFTER NOT_BEFORE - a new window: wait past SINCE_AFTER, since = pod now
//   route ALIAS ACCOUNT MODEL                         - the canary route is the single approved target
//   identity                                          - pod clock, capture-config digest and store identity
// Every wait happens on THIS clock (the clock of requestedAt/observedAt). Rows are projected to the driver's 11 fields.
"use strict";
const crypto = require("node:crypto");
const ORIGIN = "http://127.0.0.1:8787";
const KEYS = ["id", "requestId", "namespace", "principalId", "route", "startedAt", "endedAt", "status", "incomplete",
  "attempts", "configRevision"];
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const fail = (why, code = 1) => { process.stderr.write(why + "\n"); process.exit(code); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const h = { headers: { authorization: "Bearer " + process.env.ROUTER_ADMIN_TOKEN } };
const canonical = (v) => (Array.isArray(v) ? "[" + v.map(canonical).join(",") + "]"
  : v && typeof v === "object" ? "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}"
  : JSON.stringify(v));
const sha = (s) => crypto.createHash("sha256").update(s).digest("hex");
const int = (s) => { if (!/^[0-9]{1,16}$/.test(s)) fail("bad integer", 2); return Number(s); };

async function config() {
  const r = await fetch(ORIGIN + "/admin/v1/config", h);
  if (!r.ok) fail("config HTTP " + r.status);
  return r.json();
}

async function identity() {
  const c = await config();
  let store = "";
  try {
    const u = new URL(process.env.ROUTER_DATABASE || "");
    store = sha(u.protocol + "//" + u.hostname + ":" + u.port + u.pathname);  // no credentials
  } catch { store = sha("unparsed"); }
  return { podNow: Date.now(), captureSha256: sha(canonical(c.capture ?? null)), captureLevel: c.capture?.level ?? null,
    retentionDays: c.capture?.retentionDays ?? null, storeSha256: store };
}

async function trajectory(key, route, mode, value, notBefore) {
  if (!/^key_[0-9a-f]{32}$/.test(key) || !/^canary-pi-(codex|qwen)$/.test(route)) fail("bad scope", 2);
  if (Number(process.env.ROUTER_PORT || 8787) !== 8787) fail("router port", 3);
  while (Date.now() < notBefore) await sleep(Math.min(250, notBefore - Date.now()));
  let since;
  if (mode === "fixed") since = value;
  else if (mode === "after") {
    while (Date.now() <= value) await sleep(Math.min(250, value - Date.now() + 1));
    since = Date.now();
    await sleep(2);  // since <= requestedAt strictly ordered on this clock
  } else fail("bad mode", 2);
  const principal = "key:" + key;
  const requestedAt = Date.now();
  if (since > requestedAt) fail("since in the future", 2);
  const q = new URLSearchParams({ namespace: "default", principal, route, limit: "499" });
  const r = await fetch(ORIGIN + "/admin/v1/trajectories?" + q, h);
  if (!r.ok) fail("trajectory HTTP " + r.status);
  const d = await r.json();
  let all;
  if (Array.isArray(d)) all = d;
  else if (d && Array.isArray(d.items)) all = d.items;
  else fail("unrecognized trajectory envelope");  // never an empty list by default
  const observedAt = Date.now();
  if (all.length >= 499) fail("listing may be truncated");
  const norm = (t) => (t.route === undefined && t.model === route ? { ...t, route } : t);
  const odd = all.map(norm).filter((t) => t.principalId !== principal || t.route !== route || t.namespace !== "default");
  if (odd.length) fail("rows outside the scope");
  const rows = all.map(norm).filter((t) => t.startedAt >= since).map((t) => Object.fromEntries(KEYS.map((k) => [k, t[k]])));
  const unsettled = rows.filter((t) => t.incomplete !== false || !Number.isInteger(t.endedAt)).length;
  return { envelope: { version: 1, source: process.env.ROUTER_SOURCE_COMMIT || null, origin: ORIGIN,
    scope: { namespace: "default", principal, route, since }, requestedAt, observedAt, fetchedRows: all.length, rows,
    totalAttempts: rows.reduce((s, t) => s + (t.attempts || 0), 0) }, unsettled, identity: await identity() };
}

async function routeCheck(alias, account, model) {
  const c = await config();
  const p = (c.policies || []).find((x) => x.alias === alias);
  const d = (p && p.deployments) || [];
  return { alias, ok: !!p && d.length === 1 && d[0].enabled === true && d[0].accountId === account &&
    d[0].model === model && p.maxAttempts === 1 && !(p.fallbackAliases || []).length, podNow: Date.now() };
}

(async () => {
  const [mode, ...a] = process.argv.slice(2);
  if (mode === "trajectory" && a.length === 5) out(await trajectory(a[0], a[1], a[2], int(a[3]), int(a[4])));
  else if (mode === "route" && a.length === 3) out(await routeCheck(a[0], a[1], a[2]));
  else if (mode === "identity" && a.length === 0) out(await identity());
  else fail("usage", 2);
})().catch((e) => fail("error " + (e && e.name), 1));
