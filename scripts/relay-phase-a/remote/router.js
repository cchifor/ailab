// router.js: read-only phase A observations INSIDE the llm-router pod (piped to `node -` by the host observer; the
// admin token never leaves the pod). One mode per call, one JSON line on stdout, exit 0 only on success.
//   trajectory KEY ROUTE fixed SINCE NOT_BEFORE      - the driver's snapshot envelope for an existing window
//   trajectory KEY ROUTE after SINCE_AFTER NOT_BEFORE - a new window: wait past SINCE_AFTER, since = pod now
//   route ALIAS ACCOUNT MODEL                         - the canary route is the single approved target
//   identity                                          - pod clock, capture-config digest and store identity
// Every wait happens on THIS clock (the clock of requestedAt/observedAt). Rows are projected to the driver's 11 fields.
// The first argument is an absolute deadline (epoch ms): the script exits at it and every fetch is bounded by it.
"use strict";
const crypto = require("node:crypto");
const ORIGIN = "http://127.0.0.1:8787";
const KEYS = ["id", "requestId", "namespace", "principalId", "route", "startedAt", "endedAt", "status", "incomplete",
  "attempts", "configRevision"];
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const fail = (why, code = 1) => { process.stderr.write(why + "\n"); process.exit(code); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let DEADLINE = 0;
const left = () => Math.max(1, DEADLINE - Date.now());
const opts = () => ({ headers: { authorization: "Bearer " + process.env.ROUTER_ADMIN_TOKEN },
  signal: AbortSignal.timeout(left()) });
const canonical = (v) => (Array.isArray(v) ? "[" + v.map(canonical).join(",") + "]"
  : v && typeof v === "object" ? "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}"
  : JSON.stringify(v));
const sha = (s) => crypto.createHash("sha256").update(s).digest("hex");
const int = (s) => { if (!/^[0-9]{1,16}$/.test(s)) fail("bad integer", 2); return Number(s); };

async function config() {
  const r = await fetch(ORIGIN + "/admin/v1/config", opts());
  if (!r.ok) fail("config HTTP " + r.status);
  return r.json();
}

async function identity() {
  const c = await config();
  // The recording store: a SQLite file (the path plus its device and inode: replaced only if the file is), or a
  // database URL (protocol, host, port and path; never its credentials). Anything else is not an identity.
  const db = process.env.ROUTER_DATABASE || "";
  let store;
  try {
    if (db.startsWith("/")) {
      const st = require("node:fs").statSync(db);
      if (!st.isFile()) throw new Error("not a file");
      store = sha("file:" + db + ":" + st.dev + ":" + st.ino);
    } else {
      const u = new URL(db);
      if (!u.hostname || !u.pathname || u.pathname === "/") throw new Error("incomplete");
      store = sha(u.protocol + "//" + u.hostname + ":" + u.port + u.pathname);
    }
  } catch { fail("the recording store identity is not established"); }
  if (!Number.isInteger(c.capture?.retentionDays) || c.capture.retentionDays < 1) fail("capture retention unknown");
  return { podNow: Date.now(), captureSha256: sha(canonical(c.capture ?? null)), captureLevel: c.capture?.level ?? null,
    retentionDays: c.capture?.retentionDays ?? null, storeSha256: store };
}

async function trajectory(key, route, mode, value, notBefore) {
  if (!/^key_[0-9a-f]{32}$/.test(key) || !/^canary-pi-(codex|qwen)$/.test(route)) fail("bad scope", 2);
  if (Number(process.env.ROUTER_PORT || 8787) !== 8787) fail("router port", 3);
  if (notBefore >= DEADLINE) fail("notBefore is past the deadline", 2);
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
  const r = await fetch(ORIGIN + "/admin/v1/trajectories?" + q, opts());
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

// ReplaySessions at 4e4bb34: one exact key, read-only SQLite, no principal token or admin HTTP.
// Do not construct the application's Store: its startup can migrate/write the database.
function branchRecord(database, namespace, principal, conversation, branch) {
  const fs = require("node:fs");
  const { DatabaseSync } = require("node:sqlite");
  const label = (x) => typeof x === "string" && /^[A-Za-z0-9._:-]{1,200}$/.test(x);
  if (namespace !== "default" || !/^key:key_[0-9a-f]{32}$/.test(principal) ||
      !label(conversation) || !label(branch) || typeof database !== "string" || !database.startsWith("/")) {
    throw Error("branch scope unavailable");
  }
  const before = fs.lstatSync(database, { bigint: true });
  if (!before.isFile() || before.isSymbolicLink() || fs.realpathSync(database) !== database) {
    throw Error("branch store unavailable");
  }
  const key = sha(JSON.stringify([namespace, principal, conversation, branch]));
  const sql = "SELECT value FROM kv WHERE ns=? AND key=?";
  const requestedAt = Date.now();
  const db = new DatabaseSync(database, { readOnly: true, allowExtension: false });
  let record, rawRecordSha256;
  try {
    db.exec("PRAGMA query_only=ON; PRAGMA busy_timeout=1000; BEGIN");
    const rows = db.prepare(sql).all("inference-session-v1", key);
    if (rows.length !== 1 || typeof rows[0].value !== "string" || Buffer.byteLength(rows[0].value) > 65536) {
      throw Error("branch row unavailable");
    }
    const r = JSON.parse(rows[0].value);
    rawRecordSha256 = sha(rows[0].value);
    if (!r || !label(r.epoch) || !label(r.operation) || !["running", "complete", "uncertain"].includes(r.state) ||
        !Number.isSafeInteger(r.updatedAt) || r.updatedAt < 0 ||
        (r.successor !== undefined && !label(r.successor))) throw Error("branch record unavailable");
    record = { epoch: r.epoch, operation: r.operation, state: r.state, updatedAt: r.updatedAt,
      ...(r.successor === undefined ? {} : { successor: r.successor }) };
    db.exec("COMMIT");
  } finally { db.close(); }
  const after = fs.lstatSync(database, { bigint: true });
  if (!after.isFile() || ["dev", "ino"].some((k) => before[k] !== after[k]) ||
      fs.realpathSync(database) !== database) throw Error("branch store changed");
  const observedAt = Date.now();
  if (record.updatedAt > observedAt + 5000) throw Error("branch record time unavailable");
  return { version: 1, requestedAt, observedAt, namespace, principal, conversation, branch,
    kvNamespace: "inference-session-v1", key, record, querySha256: sha(sql),
    rawRecordSha256,
    storeSha256: sha("file:" + database + ":" + before.dev + ":" + before.ino) };
}

// Remote.router pipes this exact file to `node -`; stdin has no require.main.
if (require.main === module || (module.id === "[stdin]" && process.argv[1] === "-")) (async () => {
  const [deadline, mode, ...a] = process.argv.slice(2);
  DEADLINE = int(deadline);
  if (DEADLINE <= Date.now()) fail("deadline elapsed", 2);
  setTimeout(() => fail("deadline", 4), left()).unref();
  if (mode === "trajectory" && a.length === 5) out(await trajectory(a[0], a[1], a[2], int(a[3]), int(a[4])));
  else if (mode === "route" && a.length === 3) out(await routeCheck(a[0], a[1], a[2]));
  else if (mode === "identity" && a.length === 0) out(await identity());
  else if (mode === "branch" && a.length === 4) out(branchRecord(process.env.ROUTER_DATABASE, ...a));
  else fail("usage", 2);
})().catch((e) => fail("error " + (e && e.name), 1));
module.exports = { branchRecord };
