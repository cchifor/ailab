"use strict";
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {DatabaseSync}=require('node:sqlite');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const crypto=require('node:crypto'), net=require('node:net');
const http=require('node:http');
const reader=require('./router.cjs'),{createFinalizationObserver}=require('./client.cjs');
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const id='key_'+'1'.repeat(32),tenant='00000000-0000-4000-8000-000000000001';
const uuid=n=>'00000000-0000-4000-8000-'+String(n).padStart(12,'0');
function database(t) {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'finalizer-sqlite-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const file=path.join(dir,'state.sqlite'),db=new DatabaseSync(file);db.exec('CREATE TABLE kv(ns TEXT,key TEXT,value TEXT,PRIMARY KEY(ns,key));CREATE TABLE trajectories(id TEXT,namespace TEXT,principal_id TEXT,status TEXT,started_at INTEGER,data TEXT)');
  const put=(ns,key,value)=>db.prepare('INSERT OR REPLACE INTO kv VALUES(?,?,?)').run(ns,key,JSON.stringify(value));
  const k={id,namespace:'default',createdAt:100,source:'relay',hash:'MUST_NOT_BE_SELECTED',managed:{tenant,attemptFence:{fenceId:uuid(1),revision:2}}};
  const a={v:1,keyId:id,keyCreatedAt:100,coveredFrom:100,consumedTokens:12,unknownUsage:1,unattributed:0,attempts:2};
  const f={v:1,keyId:id,tenant,revision:2,closedAt:200,closedAttempts:2,closedTokenUsageUnknown:1,
    terminalEvidenceSha256:'a'.repeat(64),activeRow:0,baselineAttempts:0,rowStartAttempts:0,completed:[],
    plan:{fenceId:uuid(1),keyCreatedAt:100,namespace:'default',principalId:'key:'+id,deploymentId:uuid(2),phaseLimit:2,rows:[{id:'p1',limit:2}]}};
  put('api-keys','state',{keys:[k],requests:[]});put('relay-key-meter','deployment',{v:1,id:uuid(2),createdAt:50});
  put('relay-key-meter','agg:'+id,a);put('relay-key-attempt-fences',uuid(1),f);
  db.prepare('INSERT INTO trajectories VALUES(?,?,?,?,?,?)').run(uuid(3),'default','key:'+id,'complete',100,
    JSON.stringify({requestId:uuid(4),endedAt:200,incomplete:false,attempts:2,captured:{secret:'MUST_NOT_BE_SELECTED'},
      events:[{sequence:1,type:'request.received',requestId:uuid(4),trajectoryId:uuid(3),data:{runtimeInstanceId:uuid(5)}}]}));
  t.after(()=>db.close());
  return {file,db,put,k,a,f,read:()=>reader.meter(file,[{router_key_id:id,namespace:'default'}])};
}
test('actual readonly SQLite exact meter/fence and no credential hash projection',t=>{
  const x=database(t),before=fs.readFileSync(x.file),r=x.read();
  assert.equal(r.keys[0].tokenUsageUnknown,1);assert.equal(r.keys[0].fenceState,'closed');
  assert(!JSON.stringify(r).includes('MUST_NOT_BE_SELECTED'));assert.deepEqual(fs.readFileSync(x.file),before);
});
test('missing aggregate/deployment/key never becomes zero',t=>{
  const x=database(t);x.db.prepare('DELETE FROM kv WHERE ns=? AND key=?').run('relay-key-meter','agg:'+id);assert.throws(x.read);
});
test('open row of ANY epoch refuses instead of guessed stale count',t=>{
  const x=database(t);x.put('relay-key-meter','open:'+id+':1',{v:1,epoch:'old',at:101});assert.throws(x.read);
});
test('corrupt attribution, fence continuity and covered prefix refuse',t=>{
  const x=database(t);
  for(const change of [{unattributed:1},{coveredFrom:101},{attempts:3}]){
    x.put('relay-key-meter','agg:'+id,{...x.a,...change});assert.throws(x.read);
  }
  x.put('relay-key-meter','agg:'+id,x.a);x.put('relay-key-attempt-fences',uuid(1),{...x.f,closedAt:undefined});assert.throws(x.read);
});
test('store symlink refuses',t=>{
  const x=database(t);fs.symlinkSync(x.file,x.file+'.link');assert.throws(()=>reader.meter(x.file+'.link',[{router_key_id:id,namespace:'default'}]));
});
test('lifetime trajectory retention gap refuses process absence evidence',t=>{
  const x=database(t);x.db.exec('DELETE FROM trajectories');assert.throws(x.read);
});
test('real process root, live child and actual joined absence',async t=>{
  const root=reader.procStat(process.pid),pin={pid:process.pid,startTicks:root.startTicks,executableSha256:sha(fs.readFileSync('/proc/self/exe'))};
  assert.deepEqual(reader.processCensus(pin).processes,[]);
  const child=spawn(process.execPath,['-e','process.stdout.write("ready\\n");setInterval(()=>{},1000)'],{stdio:['ignore','pipe','pipe']});
  t.after(()=>child.kill('SIGKILL'));await once(child.stdout,'data');
  const live=reader.processCensus(pin);assert(live.processes.some(p=>p.pid===child.pid));
  child.kill('SIGKILL');await once(child,'exit');
  assert.deepEqual(reader.processCensus(pin).processes,[]);
  assert.throws(()=>reader.processCensus({...pin,startTicks:'0'}));
});
function changingCensus(t, change) {
  // Synthetic /proc boundary only: the actual census must distinguish the
  // same numeric PID set from stable process identities and ancestry.
  const records=[{pid:1,ppid:0,startTicks:'10',state:'S'},
    {pid:10,ppid:1,startTicks:'100',state:'S'},
    {pid:20,ppid:1,startTicks:'200',state:'S'}];
  const executable=Buffer.from('synthetic-router-executable');let scans=0;
  const realRead=fs.readFileSync,realList=fs.readdirSync;
  t.mock.method(fs,'readdirSync',function(file,...args){
    if(file!=='/proc')return realList.call(this,file,...args);
    scans++;return records.map(p=>String(p.pid));
  });
  t.mock.method(fs,'readFileSync',function(file,...args){
    if(file==='/proc/10/exe')return executable;
    const match=typeof file==='string' && /^\/proc\/(1|10|20)\/stat$/.exec(file);
    if(!match)return realRead.call(this,file,...args);
    const p={...records.find(p=>p.pid===Number(match[1]))};
    if(scans===2 && p.pid===20)Object.assign(p,change);
    const fields=Array(22).fill('0');fields[0]=p.state;fields[1]=String(p.ppid);fields[19]=p.startTicks;
    return p.pid+' (synthetic) '+fields.join(' ');
  });
  return ()=>reader.processCensus({pid:10,startTicks:'100',executableSha256:sha(executable)});
}

test('process census refuses non-root PID reuse between identical PID lists',t=>{
  assert.throws(changingCensus(t,{startTicks:'201'}),/finalization-router-unavailable/);
});

test('process census refuses reparenting into Router with unchanged PID/start',t=>{
  assert.throws(changingCensus(t,{ppid:10}),/finalization-router-unavailable/);
});

test('process census accepts stable identities despite unrelated scheduler-state change',t=>{
  const value=changingCensus(t,{state:'R'})();
  assert.deepEqual(value.processes,[]);assert.equal(value.processEvidence.complete,true);
});

test('exact node-stdin helper entry uses real SQLite/proc and only four metadata GETs',async t=>{
  const x=database(t),seen=[];
  const server=http.createServer((req,res)=>{
    seen.push(req.method+' '+req.url);res.setHeader('cache-control','no-store');res.setHeader('content-type','application/json');
    if(req.url==='/admin/v1/plugins/provider-claude/config')res.end(JSON.stringify({revision:'a'.repeat(64),
      values:{warmSessions:false,warmIdleMs:10000},active:{warmSessions:false,warmIdleMs:10000},pending:false}));
    else if(req.url==='/admin/v1/providers')res.end(JSON.stringify([{kind:'claude',runtime:{runtimeInstanceId:uuid(5),providerGeneration:1,observedAt:Date.now()}}]));
    else {res.statusCode=404;res.end('{}');}
  });
  server.listen(8787,'127.0.0.1');await once(server,'listening');t.after(()=>server.close());
  const root=spawn(process.execPath,['-e','process.stdout.write("ready\\n");setInterval(()=>{},1000)'],{stdio:['ignore','pipe','pipe']});
  t.after(()=>root.kill('SIGKILL'));await once(root.stdout,'data');
  const metadata=reader.meter(x.file,[{router_key_id:id,namespace:'default'}]),identity=reader.procStat(root.pid);
  const input={version:1,deadline:Date.now()+10000,source:'b'.repeat(40),credentials:[{router_key_id:id,namespace:'default'}],
    storeIdentity:metadata.storeIdentity,runtimeInstanceId:uuid(5),
    root:{pid:root.pid,startTicks:identity.startTicks,executableSha256:sha(fs.readFileSync('/proc/'+root.pid+'/exe'))}};
  const child=spawn(process.execPath,['-',Buffer.from(JSON.stringify(input)).toString('base64')],{
    env:{...process.env,ROUTER_DATABASE:x.file,ROUTER_SOURCE_COMMIT:'b'.repeat(40),ROUTER_ADMIN_TOKEN:'synthetic'},stdio:['pipe','pipe','pipe']});
  t.after(()=>child.kill('SIGKILL'));let stdout='',stderr='';child.stdout.on('data',b=>stdout+=b);child.stderr.on('data',b=>stderr+=b);
  child.stdin.end(fs.readFileSync(path.join(__dirname,'router.cjs')));
  const [code]=await once(child,'exit');assert.equal(code,0,stderr);
  const result=JSON.parse(stdout);assert.deepEqual(result.processes,[]);assert.equal(result.keys.length,1);
  assert.equal(seen.length,4);assert(seen.every(s=>s.startsWith('GET ')));
  root.kill('SIGKILL');await once(root,'exit');
});

function ctx(){return {version:1,stage:'preflight',reportPins:{},decisionSha256:'a'.repeat(64),sources:{},scope:{},
  launches:[],markers:[],originalIdle:10000,notBefore:Date.now()-1,deadline:Date.now()+2000,signal:new AbortController().signal};}
async function server(t,fn){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'finalizer-client-')),sock=path.join(dir,'s');
  const connections=new Set(),s=net.createServer(c=>{connections.add(c);c.on('close',()=>connections.delete(c));fn(c);});
  s.listen(sock);await once(s,'listening');t.after(()=>{for(const c of connections)c.destroy();s.close();fs.rmSync(dir,{recursive:true,force:true});});return sock;}
test('actual Unix client carries exact fixed checkpoint and strips only AbortSignal',async t=>{
  const socketPath=await server(t,c=>c.once('data',b=>{
    const r=JSON.parse(b);assert.equal(r.call,'checkpoint');assert.equal(r.kind,'finalization');assert(!('signal' in r.context));
    c.end(JSON.stringify({v:1,ok:true,value:{observed:true}})+'\n');
  }));
  assert.deepEqual(await createFinalizationObserver({socketPath,qualificationSha256:'a'.repeat(64)}).observe(ctx()),{observed:true});
});
test('actual Unix abort closes peer, no late success',async t=>{
  let peer;const socketPath=await server(t,c=>{peer=c;c.resume();});
  const controller=new AbortController(),call=createFinalizationObserver({socketPath,qualificationSha256:'a'.repeat(64)}).observe({...ctx(),signal:controller.signal});
  await new Promise(r=>setTimeout(r,30));const close=once(peer,'close');controller.abort();
  await assert.rejects(call,/aborted/);await close;
});
test('framing/oversized reply and remote refusal never become observation',async t=>{
  for(const reply of ['{}\n{}\n',JSON.stringify({v:1,ok:false,code:'failed'})+'\n','x'.repeat(524289)]) {
    const socketPath=await server(t,c=>c.once('data',()=>c.end(reply)));
    await assert.rejects(createFinalizationObserver({socketPath,qualificationSha256:'a'.repeat(64)}).observe(ctx()));
  }
});
test('real existing gate + new Host + client two stages (synthetic metadata)',async t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'finalizer-host-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const scripts=path.resolve(__dirname,'..'),socket=path.join(dir,'observer.sock');
  const py=`import sys,json,subprocess,importlib.util\nsys.path.insert(0,${JSON.stringify(scripts)})\nfrom tests.test_relay_finalization_observer import fixture,Remote,Evidence\nimport relay_finalization_observer as F\np,d,h,r,f=fixture()\nev=F.Evidence(${JSON.stringify(path.join(dir,'evidence.jsonl'))})\ngate=subprocess.Popen([sys.executable,'-c',"import sys,importlib.util; s=importlib.util.spec_from_file_location('g',"+repr(${JSON.stringify(path.join(scripts,'relay-observer-gate.py'))})+");m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.Gate("+repr(${JSON.stringify(socket)})+",sys.stdout.buffer,sys.stdin.buffer).serve()"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)\nreader=F.Reader(plan=p,decision_raw=d,authorization_sha256=F.sha(d),remote=Remote(h,r,f),evidence=ev)\nhost=F.FinalizationHost(gate=gate,reader=reader,evidence=ev,on_fault=lambda *a:None,lease_ok=lambda:True)\ntry:\n host.start();print(json.dumps(p),flush=True);sys.stdin.readline()\nfinally:\n host.stop();assert host.drain(3);gate.kill();gate.wait();ev.close()\n`;
  const child=spawn('python3',['-c',py],{stdio:['pipe','pipe','pipe'],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
  let stderr='';child.stderr.on('data',b=>stderr+=b);t.after(()=>child.kill('SIGKILL'));
  const p=await new Promise((resolve,reject)=>{let b='';child.stdout.on('data',v=>{b+=v;if(b.includes('\n'))resolve(JSON.parse(b.split('\n')[0]));});child.on('exit',()=>reject(Error(stderr)));});
  const observer=createFinalizationObserver({socketPath:socket,qualificationSha256:'a'.repeat(64)});
  for(const stage of ['preflight','renderer']) {
    const body={version:1,stage};for(const k of ['reportPins','decisionSha256','sources','scope','launches','markers','originalIdle'])body[k]=p[k];
    Object.assign(body,{notBefore:Date.now()-5000,deadline:Date.now()+5000,signal:new AbortController().signal});
    const value=await observer.observe(body);assert.equal(value.stage,stage);assert.equal(value.sessions.length,10);
  }
  child.stdin.end('\n');const [code]=await once(child,'exit');assert.equal(code,0,stderr);
  assert.equal(fs.readFileSync(path.join(dir,'evidence.jsonl'),'utf8').trim().split('\n').length,2);
});
