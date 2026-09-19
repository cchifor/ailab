// Human UI command only. No model-facing tool and no task on plugin activation.
export const name='conductor-command';
const usage='/conductor status | start <task> | pause <id> | resume <id> | resume-ack <id>';
const idPattern=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function apply(ctx){
 ctx.inject(['commands','teamConductor'],child=>{
  const service=child.get('teamConductor');
  const key=Symbol.for('dsh-conductor.production-initial-admission');
  const admission=service[key]??(service[key]={creating:false});
  const summary=run=>({id:run.id,state:run.state,branch:run.branch,pr:run.pr,budget:service.budget(run.id)});
  child.commands.register({name:'conductor',description:'Control the bounded production conductor (human only)',input:{hint:'status | start <task> | pause/resume/resume-ack <id>'},handler:async invocation=>{
   const fail=text=>({kind:'error',text});
   if(invocation.agent.session.header.parentSession)return fail('Use a top-level human session.');
   if(invocation.signal?.aborted)return fail('Command cancelled before admission.');
   const input=invocation.rawInput.trim(),space=input.search(/\s/),verb=space<0?input:input.slice(0,space),arg=space<0?'':input.slice(space).trim();
   if(!input||verb==='status')return {kind:'success',text:JSON.stringify({repository:'cchifor/dsh-team-conductor',allowance:'One production run, at most 40 calls / 40M reserved tokens; no monetary ceiling',creating:admission.creating,runs:service.status().map(summary)},null,2)};
   if(verb==='start'){
    if(!arg||arg.length>8000)return fail('Provide a task between 1 and 8000 characters.');
    // v0.1.0's ledger ceiling is PER RUN. This initial authorized allowance is
    // therefore limited to ONE persisted run, including failed/completed runs.
    // History is never deleted/refunded to admit another run; concurrent starts
    // remain excluded until the underlying create settles even after UI abort.
    if(service.config?.enabled!==true||service.config.admission?.maxCalls!==40||service.config.admission?.maxReservedTokens!==40000000)return fail('Production configuration does not match the authorized allowance.');
    if(admission.creating||service.status().length!==0)return fail('The initial production allowance is already assigned. Use status; another run needs new operator authorization.');
    admission.creating=true;
    try{const run=await service.createTask(arg);return {kind:'success',text:JSON.stringify(summary(run),null,2)};}
    catch{return fail('Task creation did not confirm success. Inspect status and retained state before retrying.');}
    finally{admission.creating=false;}
   }
   if(!['pause','resume','resume-ack'].includes(verb)||!idPattern.test(arg))return fail(usage);
   try{
    const run=verb==='pause'?service.pause(arg):service.resume(arg,verb==='resume-ack');
    return {kind:'success',text:JSON.stringify(summary(run),null,2)};
   }catch{return fail('Control refused. Inspect status and reconcile uncertain effects; resume-ack explicitly acknowledges an interrupted stage and never refunds reservations.');}
  }});
  child.provide('conductorControlsReady',{command:'conductor',maxRuns:1,maxCalls:40,maxReservedTokens:40000000});
 });
}
export default {name,apply};
