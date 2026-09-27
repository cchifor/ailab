import {mkdirSync,copyFileSync,existsSync} from 'node:fs';
import {randomBytes} from 'node:crypto';
import {createRuntime} from '/tmp/router-release/dist/packages/runtime/cordis.js';
import {createHttpServer} from '/tmp/router-release/dist/packages/plugins/http-api.js';
const snapshot='/releases/.backups/20260927-pre-admin-bridge';
mkdirSync('/tmp/router-smoke',{recursive:true});
for(const file of ['router.sqlite','router.secrets.key','plugins.yml'])if(existsSync(`${snapshot}/${file}`))copyFileSync(`${snapshot}/${file}`,`/tmp/router-smoke/${file}`);
const runtime=await createRuntime({database:'/tmp/router-smoke/router.sqlite',quiet:true,pluginConfig:JSON.parse(process.env.ROUTER_PLUGIN_CONFIG)});
let app;
try {
  const token=randomBytes(32).toString('hex');
  app=await createHttpServer(runtime,{webRoot:'/tmp/router-release/dist/web',keys:[{token,principal:{id:'release-smoke',namespace:'default',role:'admin',access:['local']}}]});
  const base=await app.listen({host:'127.0.0.1',port:0});
  if(!runtime.plugins.list().some(p=>p.id==='admin-bridge'&&p.state==='active'))throw new Error('Admin bridge is not active');
  for(const path of ['/health','/ready','/']) {const response=await fetch(base+path);if(!response.ok)throw new Error(`${path}: ${response.status}`);}
  const denied=await fetch(base+'/register/admin-bridge',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action:'capabilities'})});
  if(denied.status!==403)throw new Error(`Bridge must reject unsigned requests: ${denied.status}`);
  console.info('Production snapshot smoke passed: readiness, SPA and signed bridge gate.');
} finally {if(app)await app.close();await runtime.close();}
