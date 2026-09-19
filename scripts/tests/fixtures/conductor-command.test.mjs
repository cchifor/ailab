import {test} from 'node:test';
import assert from 'node:assert/strict';
import {apply} from '../../../kubernetes/apps/apps/dsh/conductor-command.mjs';
const id='12345678-1234-1234-1234-123456789abc';
function fixture(){
 const runs=[],controls=[];let creates=0,command;
 const service={config:{enabled:true,admission:{maxCalls:40,maxReservedTokens:40000000}},status:()=>runs,budget:()=>({calls:0,reservedTokens:0,reservedMicrousd:0}),createTask:async()=>{creates++;const run={id,state:'PLANNING',branch:'team/test'};runs.push(run);return run;},pause:x=>{controls.push(['pause',x]);return runs[0];},resume:(x,ack)=>{controls.push(['resume',x,ack]);return runs[0];}};
 const ctx={inject:(deps,fn)=>{assert.deepEqual(deps,['commands','teamConductor']);fn({get:()=>service,commands:{register:c=>{command=c;}},provide:(name,value)=>{assert.equal(name,'conductorControlsReady');assert.equal(value.maxRuns,1);}});}};
 apply(ctx);
 return {service,runs,controls,ctx,creates:()=>creates,invoke:(rawInput,extra={})=>command.handler({rawInput,agent:{session:{header:{}}},...extra})};
}
test('activation and status create no task',async()=>{const f=fixture();assert.equal(f.creates(),0);assert.equal((await f.invoke('status')).kind,'success');assert.equal(f.creates(),0);});
test('one persisted run consumes the initial admission slot even when terminal',async()=>{const f=fixture();assert.equal((await f.invoke('start Fix the documentation')).kind,'success');f.runs[0].state='MERGED';assert.equal((await f.invoke('start Another task')).kind,'error');assert.equal(f.creates(),1);});
test('concurrent and re-registered commands cannot duplicate pending creation',async()=>{const f=fixture();let release;f.service.createTask=()=>new Promise(r=>{release=r;});const first=f.invoke('start One');assert.equal((await f.invoke('start Two')).kind,'error');apply(f.ctx);assert.equal((await f.invoke('start Three')).kind,'error');release({id,state:'PLANNING'});assert.equal((await first).kind,'success');});
test('uncertain creation with persisted state cannot be retried into another run',async()=>{const f=fixture();f.service.createTask=async()=>{f.runs.push({id,state:'PLANNING'});throw Error('lost acknowledgement');};assert.equal((await f.invoke('start One')).kind,'error');assert.equal((await f.invoke('start Two')).kind,'error');assert.equal(f.runs.length,1);});
test('child sessions and pre-aborted commands cannot admit work',async()=>{const f=fixture();assert.equal((await f.invoke('start One',{agent:{session:{header:{parentSession:'parent'}}}})).kind,'error');assert.equal((await f.invoke('start One',{signal:AbortSignal.abort()})).kind,'error');assert.equal(f.creates(),0);});
test('disabled or mismatched allowance refuses admission',async()=>{for(const mutate of [s=>s.config.enabled=false,s=>s.config.admission.maxCalls=41,s=>s.config.admission.maxReservedTokens=41000000]){const f=fixture();mutate(f.service);assert.equal((await f.invoke('start One')).kind,'error');assert.equal(f.creates(),0);}});
test('pause and resume acknowledge only explicit resume-ack',async()=>{const f=fixture();f.runs.push({id,state:'PAUSED'});for(const verb of ['pause','resume','resume-ack'])assert.equal((await f.invoke(verb+' '+id)).kind,'success');assert.deepEqual(f.controls,[['pause',id],['resume',id,false],['resume',id,true]]);});
test('invalid input does not touch run controls or create work',async()=>{const f=fixture();for(const input of ['start','start '+'x'.repeat(8001),'pause ../state','unknown'])assert.equal((await f.invoke(input)).kind,'error');assert.equal(f.creates(),0);assert.equal(f.controls.length,0);});
