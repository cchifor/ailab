"use strict";
const net=require('node:net');
// Credential-less fixed gate client; intentionally no callback/module/host URL.
function createFinalizationObserver({socketPath,qualificationSha256}) {
  if(typeof socketPath!=='string' || !socketPath.startsWith('/') || socketPath.includes('\0') ||
     !/^[a-f0-9]{64}$/.test(qualificationSha256)) throw Error('Finalization observer unavailable');
  return Object.freeze({qualificationSha256, observe(context) {
    const {signal,...body}=context;
    const keys=['version','stage','reportPins','decisionSha256','sources','scope','launches','markers','originalIdle','notBefore','deadline'];
    if(Object.keys(body).sort().join()!==keys.sort().join() || !['preflight','renderer'].includes(body.stage) ||
       !Number.isSafeInteger(body.deadline) || body.deadline<=Date.now() || body.deadline>Date.now()+120000 || signal?.aborted)
      return Promise.reject(Error('Finalization observer request refused'));
    const raw=Buffer.from(JSON.stringify({v:1,call:'checkpoint',kind:'finalization',deadline:body.deadline,context:body})+'\n');
    if(raw.length>65536) return Promise.reject(Error('Finalization observer request too large'));
    return new Promise((resolve,reject)=>{
      const socket=net.createConnection(socketPath),chunks=[];let size=0,done=false;
      const finish=(error,value)=>{if(done)return;done=true;clearTimeout(timer);signal?.removeEventListener('abort',abort);
        socket.destroy();error?reject(Error(error)):resolve(value);};
      const abort=()=>finish('Finalization observer aborted');
      const timer=setTimeout(()=>finish('Finalization observer deadline'),body.deadline-Date.now());
      signal?.addEventListener('abort',abort,{once:true});
      if(signal?.aborted) return abort();
      socket.on('error',()=>finish('Finalization observer unavailable'));
      socket.on('end',()=>finish('Finalization observer closed'));
      socket.on('connect',()=>socket.write(raw));
      socket.on('data',b=>{
        size+=b.length;if(size>524288)return finish('Finalization observer overflow');chunks.push(b);
        const bytes=Buffer.concat(chunks),nl=bytes.indexOf(10);if(nl<0)return;
        if(nl!==bytes.length-1)return finish('Finalization observer framing');
        try {
          const text=new TextDecoder('utf-8',{fatal:true}).decode(bytes.subarray(0,nl)),v=JSON.parse(text);
          if(v.v!==1 || v.ok!==true || !Object.hasOwn(v,'value'))return finish('Finalization observer refused');
          finish(null,v.value);
        } catch {finish('Finalization observer framing');}
      });
    });
  }});
}
module.exports={createFinalizationObserver};
