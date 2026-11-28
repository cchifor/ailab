"use strict";
// Explicit offline integration: actual pinned A Runner.preflight + Unix client +
// Python collector. Deployment/GET replies are synthetic; PG rows are actual
// disposable-database query outputs. No clock/prototype/validator substitution.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const {execFileSync} = require('node:child_process');
const {createHash,webcrypto} = require('node:crypto');
const {createObserverClient} = require('./observer-client.cjs');
const hash = v=>createHash('sha256').update(v).digest('hex');
const driver=process.env.PHASE_A_DRIVER;
assert.ok(driver, 'PHASE_A_DRIVER exact f1 input required');
assert.equal(hash(fs.readFileSync(driver)), 'a9ff8fc0f37c5a79d4e956acd718e512c320fba593f4731cea6df92da481ed98');
const a=require(driver);
assert.ok(process.env.OBSERVER_SQL_READBACKS,'actual synthetic PostgreSQL readbacks required');
const root=process.env.OBSERVER_SQL_READBACKS;
const u=n=>n.toString(16).padStart(8,'0')+'-0000-4000-8000-000000000000';
const domain='relay-predecessor-command-projection/content-v1\n';
const p=a.PROFILES.codex;
const originalTime=Date.parse('2026-11-26T12:00:00.000Z');
const date=n=>new Date(n).toISOString();
const route='/api/agent-sessions/'+a.STOPPED_SESSION;
function report() {
  const baseline={version:1,source:a.STOPPED_SOURCE,origin:'http://127.0.0.1:8787',
    scope:{namespace:'default',principal:'key:'+p.key,route:'canary-pi-codex',since:originalTime-1000},
    requestedAt:originalTime,observedAt:originalTime+1,rows:[],fetchedRows:0,totalAttempts:0};
  return {version:1,relay:a.SOURCE,router:a.STOPPED_SOURCE,status:'stopped',reason:'Unexpected tool permission',completed:false,
    runs:[{provider:'codex',runId:p.run,effort:false,prompts:1,deadline:originalTime+900000,
           firstSnapshot:baseline,lastSnapshot:structuredClone(baseline)}],
    evidence:[{kind:'launch',id:u(20),operationId:u(21),sessionId:a.STOPPED_SESSION,profileId:p.id,revision:1,
               journalId:a.HOLDER,holderId:u(7),at:date(originalTime),recovery:false},
              {kind:'pre-launch',marker:{runId:p.run,phase:'baseline'}},
              {kind:'cleanup',status:'closed-and-released',at:date(originalTime+10000)}],
    operations:[{status:'received',path:'/api/agent-launches',body:{operationId:u(21)}},
                {status:'received',kind:'lease-acquisition',path:route+'/control'},
                {status:'received',path:'/api/agent-permissions/'+a.STOPPED_PERMISSION+'/decision',body:{outcome:{outcome:'cancelled'}}},
                ...['prompt','cancel','close'].map((kind,i)=>({status:'received',path:route+'/commands',body:{kind,generation:1,
                  operationId:u(50+i),...(kind==='prompt'?{text:'Reply exactly READY. Do not use tools or modify files.'}:{})}}))]};
}
const prior=a.stoppedPredecessor(JSON.stringify(report()));
const reportHash=hash(a.canonical(prior.report));
const queryHash=hash(fs.readFileSync(path.join(__dirname,'sql/predecessor-projection.sql')));
// Independent fixture expectation is constructed before callback from the fixture
// inserts and exact query bytes. Never derive a trusted pin from returned content.
const expected={kind:'relay-predecessor-command-projection',version:1,source:a.RUN_SOURCE,schemaVersion:93,
  tenantId:u(1),databaseIdentity:'relay.relay-postgres-0.relay',querySha256:queryHash,
  sessionId:a.STOPPED_SESSION,generation:1,priorReportCanonicalSha256:reportHash,
  commands:['prompt','cancel','close'].map((kind,i)=>({id:u(40+i),operation_id:u(50+i),session_id:a.STOPPED_SESSION,
    generation:1,kind,state:'completed',receipt:{status:'completed',result:kind==='prompt'?{stopReason:'cancelled'}:{cancelSent:true}}}))};
const pin={mode:'canonical-content-v1',sha256:hash(domain+a.canonical(expected)),tenantId:expected.tenantId,
  databaseIdentity:expected.databaseIdentity,querySha256:queryHash,
  commands:expected.commands.map(c=>({operationId:c.operation_id,commandId:c.id,kind:c.kind}))};

test('actual pinned Runner consumes fresh SQL projection through actual Unix observer client',async()=>{
  const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'observer-support-'));
  const sock=path.join(tmp,'observer.sock');
  const rawRows=['predecessor.json','predecessor-fresh.json'].map(f=>JSON.parse(fs.readFileSync(path.join(root,f),'utf8')));
  assert.notEqual(rawRows[0].observedMs,rawRows[1].observedMs,'two distinct actual database observations required');
  const replies=[],frames=[];
  let activeRow=rawRows[0], alteration=null;
  const server=net.createServer(socket=>{
    let input='';
    socket.on('data',chunk=>{
      input+=chunk;
      if(!input.endsWith('\n'))return;
      const envelope=JSON.parse(input); frames.push(envelope);
      assert.equal(envelope.call,'predecessor');
      assert.equal(envelope.context.source,a.RUN_SOURCE);
      try {
        const result=JSON.parse(execFileSync('python3',['-m','scripts.tests.test_relay_phase_a_support','--predecessor'],
          {input:JSON.stringify({report:prior.report,context:envelope.context,row:activeRow}),encoding:'utf8',timeout:10000}));
        let value=result.value;
        if(alteration)value=alteration(value);
        replies.push({value,observations:result.observations});
        socket.end(JSON.stringify({v:1,ok:true,value})+'\n');
      } catch { socket.end(JSON.stringify({v:1,ok:false,code:'failed'})+'\n'); }
    });
  });
  await new Promise(resolve=>server.listen(sock,resolve));
  const client=createObserverClient(sock);
  async function preflight(independentPin=pin){
    const calls=[],saved=[];
    let reachedReview=false;
    const r=new a.Runner({origin:a.ORIGIN,crypto:webcrypto,save:s=>saved.push(structuredClone(s)),
      predecessorEvidencePin:independentPin,readPredecessorEvidence:client.readPredecessorEvidence,
      ask:async()=>{reachedReview=true;throw Error('FIXTURE-REVIEW-BOUNDARY');},
      fetch:async(url,init)=>{
        calls.push({url,method:init.method}); assert.equal(init.method,'GET');
        let result;
        if(url===route)result={id:a.STOPPED_SESSION,kind:'holder',connector_id:'b3b1be1a-275c-4a2e-b515-428b39c43171',
                              generation:1,rebinding:false,parent_session_id:null,state:'closed'};
        else if(url===route+'/control')result={lease:null};
        else if(url==='/api/agent-permissions/'+a.STOPPED_PERMISSION)result={id:a.STOPPED_PERMISSION,session_id:a.STOPPED_SESSION,
          generation:1,state:'resolved',decision:{outcome:'cancelled'},context:{title:'Allow mcp__relay__relay_messages_read?'}};
        else throw Error('unexpected synthetic API read');
        return {ok:true,status:200,headers:{get:()=> 'application/json'},text:async()=>JSON.stringify(result)};
      }});
    // Primed metadata from a synthetic renewed credential. Clock and driver stay exact.
    r.effectiveExpiry=Date.now()+60000;
    r.state.predecessor={canonicalSha256:reportHash};
    let error;
    try{await r.replacementPreflight(prior);}catch(e){error=e;}
    return {r,calls,saved,reachedReview,error};
  }
  try {
    for(const row of rawRows){
      activeRow=row;
      const result=await preflight();
      assert.equal(result.error?.message,'FIXTURE-REVIEW-BOUNDARY');
      assert.equal(result.reachedReview,true);
      const proof=result.r.state.evidence.find(e=>e.kind==='operator-predecessor-projection');
      assert.equal(proof.pinMode,'canonical-content-v1');
      assert.equal(proof.pinSha256,pin.sha256);
      assert.equal(proof.sha256,hash(replies.at(-1).value));
      assert.equal(result.calls.length,3);
    }
    assert.notEqual(hash(replies[0].value),hash(replies[1].value));
    const rawPin={...pin}; delete rawPin.mode; rawPin.sha256=hash(replies.at(-1).value);
    assert.equal((await preflight(rawPin)).reachedReview,true,'legacy exact raw mode');
    for(const change of [v=>({...v,observedUtc:date(Date.now()-120001)}),
                        v=>({...v,observedUtc:date(Date.now()+10000)}),
                        v=>({...v,source:'0'.repeat(40)}),v=>({...v,schemaVersion:94}),
                        v=>({...v,tenantId:u(99)}),v=>({...v,querySha256:'0'.repeat(64)}),
                        v=>({...v,commands:v.commands.map((c,i)=>i?c:{...c,receipt:{status:'completed',result:{stopReason:'end_turn'}}})})]){
      alteration=raw=>a.canonical(change(JSON.parse(raw)));
      const result=await preflight();
      assert.equal(result.reachedReview,false);
      assert.equal(result.r.state.evidence.some(e=>e.kind==='operator-predecessor-projection'),false);
      assert.equal(result.calls.length,2);
    }
    alteration=null;
    assert.equal((await preflight({...pin,mode:'unknown'})).reachedReview,false);
    assert.ok(frames.every(f=>!('signal' in f.context)));
    if(process.env.OBSERVER_INTEGRATION_EVIDENCE)fs.writeFileSync(process.env.OBSERVER_INTEGRATION_EVIDENCE,
      JSON.stringify({scope:'synthetic preflight only; no full lifecycle/live authority',driverSha256:hash(fs.readFileSync(driver)),
                      pin,frames,replies},null,2));
  } finally {await new Promise(resolve=>server.close(resolve));fs.rmSync(tmp,{recursive:true,force:true});}
});
