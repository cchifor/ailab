// Host-only observer: no coordinator creation, model calls, global veto or appExit.
import {mkdirSync,writeFileSync,renameSync,unlinkSync} from 'node:fs';
import {isAbsolute,dirname,resolve,relative} from 'node:path';
import {randomUUID} from 'node:crypto';
export const name='conductor-release-readiness';
// A required dependency on the profile entry itself makes optional-addon absence
// fatal in DSH's assertEntriesActivated. Keep only a nested observer pending.
export function apply(ctx,options={}){
 ctx.inject(['teamConductor','conductorControlsReady'],child=>observe(child,options).catch(()=>{
  // Readiness is evidence, not a reason to crash the existing GUI. Fail closed
  // by withholding the marker; never leak credential-bearing exception details.
  child.logger.warn('Conductor readiness unavailable; activation is not certified. Inspect operator preflight.');
 }));
}
async function observe(ctx,options={}){
 const {default:Runtime}=await import('dsh-team-conductor');
 const {default:pkg}=await import('dsh-team-conductor/package.json',{with:{type:'json'}});
 const {loadConfig}=await import('dsh-team-conductor/profile');
 const service=ctx.get('teamConductor'); // Cordis strict get requires ACTIVE provider.
 const controls=ctx.get('conductorControlsReady');
 const {configPath,markerPath,podUid}=options;
 const check=value=>{if(!value)throw new Error('Conductor release readiness refused');};
 check(pkg.version==='0.1.0'&&service instanceof Runtime);
 check(controls?.command==='conductor'&&controls.maxRuns===1&&controls.maxCalls===40&&controls.maxReservedTokens===40000000);
 check(typeof configPath==='string'&&isAbsolute(configPath)&&typeof markerPath==='string'&&isAbsolute(markerPath));
 check(typeof podUid==='string'&&/^[0-9a-f-]{36}$/.test(podUid));
 const config=loadConfig(configPath);
 check(typeof config.enabled==='boolean');
 // Deliberately pinned 0.1.0 diagnostics: these private fields are NOT a future SDK contract.
 check(service.config?.enabled===config.enabled&&service.config.database===config.database&&service.config.workspaceRoot===config.workspaceRoot);
 const outside=path=>{const r=relative(resolve(config.workspaceRoot),resolve(path));return r==='..'||r.startsWith('../')||isAbsolute(r);};
 check(outside(configPath)&&outside(config.database)&&outside(markerPath));
 const nonce=randomUUID();let alive=true;
 ctx.effect(()=>()=>{alive=false;try{if(loadConfig(markerPath).nonce===nonce)unlinkSync(markerPath);}catch{}});
 let timer;
 try{
  // Enabled providers have already awaited preflight in Service.init.
  await Promise.race([config.enabled?Promise.resolve():service.preflight(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Conductor readiness preflight deadline')),120000);})]);
 }finally{clearTimeout(timer);}
 if(!alive)return;
 const runs=service.status();check(Array.isArray(runs));
 const data={kind:'conductor-release-readiness',version:pkg.version,podUid,nonce,pid:process.pid,enabled:config.enabled,configurationMatched:true,humanCommand:controls.command,preflightPassed:true,runCount:runs.length,observedAt:new Date().toISOString()};
 mkdirSync(dirname(markerPath),{recursive:true});const temp=markerPath+'.'+nonce+'.tmp';
 try{writeFileSync(temp,JSON.stringify(data)+'\n',{flag:'wx',mode:0o600});renameSync(temp,markerPath);}finally{try{unlinkSync(temp);}catch{}}
 ctx.logger.info('Conductor 0.1.0 readiness verified; observer created no task');
}
export default {name,apply};
