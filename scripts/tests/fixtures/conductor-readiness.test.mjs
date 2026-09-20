// Actual observer code with mocked package imports; not real-preflight evidence.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {registerHooks} from 'node:module';
import {mkdtempSync,mkdirSync,statSync,readdirSync,writeFileSync,readFileSync,existsSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
class Runtime{constructor(config){this.config={...config};this.calls=0;this.runs=[];}async preflight(){this.calls++;}status(){return this.runs;}}
globalThis.__conductorReadinessTestRuntime=Runtime;
const js=code=>'data:text/javascript,'+encodeURIComponent(code);
const mocks=new Map([
 ['dsh-team-conductor',js('export default globalThis.__conductorReadinessTestRuntime;')],
 ['dsh-team-conductor/package.json','data:application/json,'+encodeURIComponent('{"version":"0.1.0"}')],
 ['dsh-team-conductor/profile',js('import{readFileSync}from"node:fs";export function loadConfig(p){return JSON.parse(readFileSync(p,"utf8"));}')]
]);
registerHooks({resolve(spec,context,next){return mocks.has(spec)?{url:mocks.get(spec),shortCircuit:true}:next(spec,context);}});
const {apply}=await import('../../../kubernetes/apps/apps/dsh/conductor-readiness.mjs');
function fixture(t,enabled=false){
 const dir=mkdtempSync(join(tmpdir(),'conductor-observer-unit-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));
 const config={enabled,database:join(dir,'state.sqlite'),workspaceRoot:join(dir,'workspaces')};
 const configPath=join(dir,'config.json'),markerPath=join(dir,'ready.json');writeFileSync(configPath,JSON.stringify(config));
 const service=new Runtime(config),controls={command:'conductor',maxRuns:1,maxCalls:40,maxReservedTokens:40000000},disposers=[],warnings=[];let observer;
 const ctx={get:name=>name==='teamConductor'?service:controls,inject:(deps,fn)=>{assert.deepEqual(deps,['teamConductor','conductorControlsReady']);observer=fn;},effect:fn=>disposers.push(fn()),logger:{info(){},warn(message){warnings.push(message);}}};
 apply(ctx,{configPath,markerPath,podUid:'00000000-0000-0000-0000-000000000123'});
 return {warnings,dir,config,configPath,markerPath,service,controls,ctx,run:()=>observer(ctx),dispose:()=>disposers.forEach(f=>f()),read:()=>JSON.parse(readFileSync(markerPath,'utf8'))};
}
test('marker publication failure is contained rather than rejecting plugin activation',async t=>{const f=fixture(t,true);mkdirSync(f.markerPath);await assert.doesNotReject(f.run());assert.equal(f.warnings.length,1);assert.ok(statSync(f.markerPath).isDirectory());assert.ok(!readdirSync(f.dir).some(p=>p.endsWith('.tmp')));f.dispose();});
test('registration alone is inert even without available service',t=>{const f=fixture(t);assert.equal(f.service.calls,0);assert.equal(existsSync(f.markerPath),false);});
test('disabled observer runs preflight, writes and removes its own marker',async t=>{const f=fixture(t);await f.run();assert.equal(f.service.calls,1);assert.equal(f.read().enabled,false);assert.equal(f.read().runCount,0);f.dispose();assert.equal(existsSync(f.markerPath),false);});
test('enabled initialized service is observed without duplicate preflight',async t=>{const f=fixture(t,true);await f.run();assert.equal(f.service.calls,0);assert.equal(f.read().enabled,true);f.dispose();});
test('wrong class and mismatched configuration are refused',async t=>{const f=fixture(t);f.ctx.get=()=>({});await assert.doesNotReject(f.run());assert.ok(f.warnings.length);f.ctx.get=name=>name==='teamConductor'?f.service:f.controls;f.service.config.database+='different';await assert.doesNotReject(f.run());assert.ok(f.warnings.length);assert.equal(existsSync(f.markerPath),false);});
test('restart observation reports retained runs rather than creating or deleting them',async t=>{const f=fixture(t,true);f.service.runs=[{id:'retained'}];await f.run();assert.equal(f.read().runCount,1);assert.equal(f.service.runs.length,1);f.dispose();});
test('disposal during preflight never publishes stale readiness',async t=>{const f=fixture(t);let begin,finish;const started=new Promise(r=>begin=r),gate=new Promise(r=>finish=r);f.service.preflight=()=>{begin();return gate;};const pending=f.run();await started;f.dispose();finish();await pending;assert.equal(existsSync(f.markerPath),false);});
test('old disposer cannot remove another observer nonce',async t=>{const f=fixture(t);await f.run();const newer={...f.read(),nonce:'newer'};writeFileSync(f.markerPath,JSON.stringify(newer));f.dispose();assert.deepEqual(f.read(),newer);});
test('preflight rejection produces no readiness',async t=>{const f=fixture(t);f.service.preflight=async()=>{throw Error('test preflight failed');};await assert.doesNotReject(f.run());assert.equal(f.warnings.length,1);assert.ok(!f.warnings[0].includes('test preflight failed'));assert.equal(existsSync(f.markerPath),false);f.dispose();});
test('operator state inside model workspace is refused',async t=>{const f=fixture(t);f.config.workspaceRoot=f.dir;f.service.config.workspaceRoot=f.dir;writeFileSync(f.configPath,JSON.stringify(f.config));await assert.doesNotReject(f.run());assert.ok(f.warnings.length);assert.equal(existsSync(f.markerPath),false);});
