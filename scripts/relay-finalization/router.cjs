"use strict";
// Piped to Node24 inside the already-running Router. Fixed read-only metadata
// queries; never construct application services (startup can migrate/write).
const fs = require('node:fs');
const crypto = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const need = b => { if (!b) throw Error('finalization-router-unavailable'); };
const int = (v, min=0) => Number.isSafeInteger(v) && v >= min;
const hash = v => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
const uuid = v => typeof v === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
const KEY = /^key_[a-f0-9]{12,32}$/;
const TENANT = '00000000-0000-4000-8000-000000000001';
const KEY_SQL = `SELECT json_extract(j.value,'$.id') AS id,
 json_extract(j.value,'$.namespace') AS namespace, json_extract(j.value,'$.createdAt') AS createdAt,
 json_extract(j.value,'$.source') AS source, json_extract(j.value,'$.managed.tenant') AS tenant,
 json_extract(j.value,'$.managed.attemptFence') AS fence
 FROM kv,json_each(kv.value,'$.keys') j WHERE kv.ns='api-keys' AND kv.key='state'
 AND json_extract(j.value,'$.id')=? LIMIT 2`;
const KV_SQL = 'SELECT value FROM kv WHERE ns=? AND key=? LIMIT 2';
const OPEN_SQL = "SELECT key,value FROM kv WHERE ns='relay-key-meter' AND substr(key,1,length(?))=? LIMIT 257";
const TRAJECTORY_SQL = `SELECT id,namespace,principal_id,status,started_at,
 json_extract(data,'$.requestId') AS requestId,json_extract(data,'$.endedAt') AS endedAt,
 json_extract(data,'$.incomplete') AS incomplete,json_extract(data,'$.attempts') AS attempts,
 (SELECT json_group_array(json_object('sequence',e.value->'$.sequence','type',e.value->>'$.type',
 'requestId',e.value->>'$.requestId','trajectoryId',e.value->>'$.trajectoryId',
 'attemptId',e.value->>'$.attemptId','data',json(e.value->'$.data')))
 FROM json_each(trajectories.data,'$.events') e
 WHERE e.value->>'$.type' IN ('provider.process','request.received','attempt.close_timeout','recording.failed')) AS evidence
 FROM trajectories WHERE principal_id=? ORDER BY started_at,id LIMIT 1001`;

function meter(database, credentials) {
  need(typeof database === 'string' && database.startsWith('/') && fs.realpathSync(database) === database);
  const before = fs.lstatSync(database, {bigint:true});
  need(before.isFile() && !before.isSymbolicLink());
  need(Array.isArray(credentials) && credentials.length > 0 && credentials.length < 384 &&
    new Set(credentials.map(c => c.router_key_id)).size === credentials.length);
  const db = new DatabaseSync(database, {readOnly:true, allowExtension:false});
  const rows = [], processIdentities = [];
  function kv(ns,key) {
    const a = db.prepare(KV_SQL).all(ns,key);
    need(a.length === 1 && typeof a[0].value === 'string' && Buffer.byteLength(a[0].value) <= 65536);
    return JSON.parse(a[0].value);
  }
  let deployment;
  try {
    db.exec('PRAGMA query_only=ON; PRAGMA busy_timeout=1000; BEGIN');
    deployment = kv('relay-key-meter','deployment');
    need(deployment.v === 1 && uuid(deployment.id) && int(deployment.createdAt,1));
    for (const c of credentials) {
      need(KEY.test(c.router_key_id) && c.namespace === 'default');
      const keys = db.prepare(KEY_SQL).all(c.router_key_id);
      need(keys.length === 1);
      const k = keys[0];
      need(k.id === c.router_key_id && k.namespace === 'default' && k.source === 'relay' &&
           k.tenant === TENANT && int(k.createdAt,1));
      const a = kv('relay-key-meter','agg:'+k.id);
      need(Object.keys(a).sort().join() === ['v','keyId','keyCreatedAt','coveredFrom','consumedTokens','unknownUsage','unattributed','attempts'].sort().join());
      need(a.v === 1 && a.keyId === k.id && a.keyCreatedAt === k.createdAt && a.coveredFrom === a.keyCreatedAt &&
           ['consumedTokens','unknownUsage','unattributed','attempts'].every(n=>int(a[n])) && a.unattributed === 0);
      const prefix = 'open:'+k.id+':';
      const open = db.prepare(OPEN_SQL).all(prefix,prefix);
      // A nonempty scan is unresolved regardless of runtime epoch. No attempt
      // to report stale=0 by guessing the in-memory meter epoch.
      need(open.length === 0);
      let fenceState = 'not-applicable';
      if (k.fence !== null) {
        const ref = JSON.parse(k.fence);
        need(uuid(ref.fenceId) && int(ref.revision,1) && Object.keys(ref).length === 2);
        const f = kv('relay-key-attempt-fences',ref.fenceId), p = f.plan;
        need(f.v === 1 && f.keyId === k.id && f.tenant === TENANT && f.revision === ref.revision &&
          p.fenceId === ref.fenceId && p.keyCreatedAt === k.createdAt && p.namespace === 'default' &&
          p.principalId === 'key:'+k.id && p.deploymentId === deployment.id && int(f.closedAt,1) &&
          f.closedAt <= Date.now()+5000 && f.closedAttempts === a.attempts &&
          f.closedTokenUsageUnknown === a.unknownUsage && hash(f.terminalEvidenceSha256));
        need(Array.isArray(p.rows) && p.rows.length > 0 && p.rows.length <= 32 && int(f.activeRow) &&
          f.activeRow < p.rows.length && Array.isArray(f.completed) && f.completed.length === f.activeRow &&
          int(f.baselineAttempts) && int(f.rowStartAttempts) && f.rowStartAttempts >= f.baselineAttempts &&
          int(p.phaseLimit,1) && f.closedAttempts >= f.rowStartAttempts &&
          f.closedAttempts-f.baselineAttempts <= p.phaseLimit &&
          f.closedAttempts-f.rowStartAttempts <= p.rows[f.activeRow].limit);
        let sum = f.baselineAttempts;
        f.completed.forEach((row,i)=>{
          need(row.rowId === p.rows[i].id && int(row.used) && int(row.retiredUnused) &&
               row.used+row.retiredUnused === p.rows[i].limit && hash(row.terminalEvidenceSha256));
          sum += row.used;
        });
        need(sum === f.rowStartAttempts);
        fenceState = 'closed';
      }
      rows.push({keyId:k.id, keyCreatedAt:k.createdAt, coveredFrom:a.coveredFrom,
        openRequests:0, staleOpenRequests:0, unattributed:a.unattributed,
        attempts:a.attempts, tokenUsageUnknown:a.unknownUsage, fenceState});
      // Lifetime attempt totals must match retained rows. Retention/eviction is
      // an evidence gap, never permission to forget an old detached child.
      const trajectories=db.prepare(TRAJECTORY_SQL).all('key:'+k.id);
      need(trajectories.length<1001);
      let total=0;
      for(const t of trajectories) {
        need(t.namespace==='default' && t.principal_id==='key:'+k.id && t.incomplete===0 &&
             ['complete','cancelled','failed'].includes(t.status) && int(t.endedAt) && int(t.attempts));
        total+=t.attempts; need(int(total));
        const events=JSON.parse(t.evidence);need(Array.isArray(events) && events.length<256);
        const received=events.filter(e=>e.type==='request.received');
        need(received.length===1 && uuid(received[0].data.runtimeInstanceId));
        need(!events.some(e=>['attempt.close_timeout','recording.failed'].includes(e.type)));
        for(const e of events.filter(e=>e.type==='provider.process')) {
          const d=e.data,identity=d.identity;
          need(e.requestId===t.requestId && e.trajectoryId===t.id && identity && typeof identity.id==='string' &&
               int(identity.pid,1) && int(identity.ppid,1) && /^\d+$/.test(identity.startTicks));
          processIdentities.push({pid:identity.pid,ppid:identity.ppid,startTicks:identity.startTicks,
            runtimeInstanceId:received[0].data.runtimeInstanceId});
        }
      }
      need(total===a.attempts);
    }
    db.exec('COMMIT');
  } finally { db.close(); }
  const after = fs.lstatSync(database,{bigint:true});
  need(before.dev===after.dev && before.ino===after.ino && fs.realpathSync(database)===database);
  need(processIdentities.length<4096);
  return {keys:rows, deploymentId:deployment.id, processIdentities,
    storeIdentity:sha(database+':'+before.dev+':'+before.ino), querySha256:sha(KEY_SQL+KV_SQL+OPEN_SQL+TRAJECTORY_SQL)};
}

function procStat(pid) {
  const text=fs.readFileSync('/proc/'+pid+'/stat','utf8');
  const end=text.lastIndexOf(')'), first=text.indexOf('(');
  need(first>0 && end>first && Number(text.slice(0,first).trim())===pid);
  const a=text.slice(end+2).trim().split(/\s+/);
  need(a.length>=22 && /^\d+$/.test(a[19]));
  return {pid,ppid:Number(a[1]),startTicks:a[19],state:a[0]};
}
function processCensus(root, retained=[]) {
  need(root && int(root.pid,1) && typeof root.startTicks==='string' && /^\d+$/.test(root.startTicks) && hash(root.executableSha256));
  const checkRoot=()=>{
    const actual=procStat(root.pid);
    need(actual.startTicks===root.startTicks && actual.state!=='Z' &&
      sha(fs.readFileSync('/proc/'+root.pid+'/exe'))===root.executableSha256);
    return actual;
  };
  const first=checkRoot();
  const list=()=>fs.readdirSync('/proc').filter(x=>/^\d+$/.test(x)).map(Number).sort((a,b)=>a-b);
  const ids=list(); need(ids.length<8192);
  // An unreadable or disappearing member refuses the census, including an
  // ancestry gap. No missing /proc record is converted to absence.
  const all=ids.map(procStat), byId=new Map(all.map(x=>[x.pid,x]));
  need(Array.isArray(retained) && retained.length<4096);
  const descendants=all.filter(p=>{
    if(p.pid===root.pid) return false;
    if(retained.some(v=>v.pid===p.pid && v.startTicks===p.startTicks))return true;
    let current=p, seen=new Set();
    while(current.ppid>0) {
      need(!seen.has(current.pid)); seen.add(current.pid);
      if(current.ppid===root.pid) return true;
      const next=byId.get(current.ppid); need(next);
      current=next;
    }
    return false;
  });
  const finalIds=list();
  need(JSON.stringify(ids)===JSON.stringify(finalIds));
  // PID membership alone cannot exclude reuse or reparenting during the scan.
  // Compare identity/ancestry, not transient scheduler state, for every member.
  const final=finalIds.map(procStat);
  need(all.every((p,i)=>p.pid===final[i].pid && p.startTicks===final[i].startTicks && p.ppid===final[i].ppid) &&
       JSON.stringify(first)===JSON.stringify(checkRoot()));
  return {processes:descendants, processEvidence:{complete:true,root:first,
    censusSha256:sha(JSON.stringify(all)),observedMs:Date.now(),count:all.length}};
}

async function getMetadata(route,deadline) {
  need(int(deadline) && deadline>Date.now());
  const r=await fetch('http://127.0.0.1:8787'+route,{
    headers:{authorization:'Bearer '+process.env.ROUTER_ADMIN_TOKEN}, signal:AbortSignal.timeout(deadline-Date.now()),redirect:'error'});
  need(r.ok && r.headers.get('cache-control')==='no-store');
  let body='',size=0;
  for await (const b of r.body) { size+=b.length; need(size<=65536); body+=Buffer.from(b).toString('utf8'); }
  return JSON.parse(body);
}
async function readSettings(deadline) {
  const v=await getMetadata('/admin/v1/plugins/provider-claude/config',deadline);
  need(hash(v.revision) && v.values?.warmSessions===false && int(v.values.warmIdleMs,1000) && v.values.warmIdleMs<=300000 &&
       v.active?.warmSessions===false && v.active.warmIdleMs===v.values.warmIdleMs && v.pending===false);
  return {revision:v.revision,warmSessions:v.values.warmSessions,warmIdleMs:v.values.warmIdleMs};
}
async function main(input) {
  need(input.version===1 && int(input.deadline) && input.deadline>Date.now() && input.deadline<=Date.now()+120000);
  const timer=setTimeout(()=>{process.stderr.write('reader-deadline\n');process.exit(2);},input.deadline-Date.now());
  try {
    const source=process.env.ROUTER_SOURCE_COMMIT;
    need(typeof source==='string' && /^[a-f0-9]{40}$/.test(source) && source===input.source);
    const a=await readSettings(input.deadline);
    const runtime=async()=>{
      const providers=await getMetadata('/admin/v1/providers',input.deadline);
      need(Array.isArray(providers));
      const c=providers.filter(p=>p.kind==='claude');
      need(c.length===1 && c[0].runtime?.runtimeInstanceId===input.runtimeInstanceId &&
        int(c[0].runtime.providerGeneration) && int(c[0].runtime.observedAt) &&
        Math.abs(c[0].runtime.observedAt-Date.now())<=5000);
      return {runtimeInstanceId:c[0].runtime.runtimeInstanceId,providerGeneration:c[0].runtime.providerGeneration};
    };
    const runtimeBefore=await runtime();
    const readings=meter(process.env.ROUTER_DATABASE,input.credentials);
    need(readings.storeIdentity===input.storeIdentity);
    need(uuid(input.runtimeInstanceId) && readings.processIdentities.every(p=>p.runtimeInstanceId===input.runtimeInstanceId && p.ppid===input.root.pid));
    const census=processCensus(input.root,readings.processIdentities);
    const b=await readSettings(input.deadline);
    need(JSON.stringify(a)===JSON.stringify(b) && JSON.stringify(runtimeBefore)===JSON.stringify(await runtime()) && Date.now()<input.deadline);
    return {version:1,source,observedMs:Date.now(),settings:a,runtime:runtimeBefore,...readings,...census};
  } finally { clearTimeout(timer); }
}
module.exports={meter,processCensus,procStat,main};
// Source is piped on stdin; the fixed JSON argument is base64, never shell text.
if(require.main===module || (module.id==='[stdin]' && process.argv[1]==='-')) {
  (async()=>{ const raw=Buffer.from(process.argv[2]||'','base64'); need(raw.length<=131072);
    process.stdout.write(JSON.stringify(await main(JSON.parse(raw.toString('utf8'))))+'\n');
  })().catch(()=>{process.stderr.write('finalization-router-unavailable\n');process.exitCode=1;});
}
