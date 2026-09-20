// Operator validation against the INSTALLED DSH boot parser; no plugin boot,
// configuration mutation, expression evaluation, credentials read or inference.
import assert from 'node:assert/strict';
import {isAbsolute} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const boot=process.argv[2];
assert.ok(boot&&isAbsolute(boot),'Pass the installed @deepseek-ai/dsh-app-boot entry path');
const {loadOverlayPatches}=await import(pathToFileURL(boot).href);
const file=fileURLToPath(new URL('../kubernetes/apps/apps/dsh/cordis.patch.yml',import.meta.url));
const patches=loadOverlayPatches('dsh',file);
const rows=patches.flatMap(p=>p.insert??[]);
const readiness=rows.find(r=>r.id==='conductor-release-readiness');
const command=rows.find(r=>r.id==='conductor-command');
for(const row of [readiness,command])assert.equal(row.disabled.__jsExpr,'!process.env.DSH_TEAM_CONDUCTOR_CONFIG');
assert.equal(readiness.config.configPath.__jsExpr,'process.env.DSH_TEAM_CONDUCTOR_CONFIG');
assert.equal(readiness.config.podUid.__jsExpr,'process.env.DSH_POD_UID');
assert.equal(patches.find(p=>p.id==='connection').config.trustedHosts.__jsExpr,'ctx.webRuntime.trustedHosts');
assert.ok(rows.some(r=>r.id==='credentials-openbao'&&r.name.endsWith('/openbao-credentials.mjs')));
console.log(JSON.stringify({actualInstalledBootParser:true,completeAuthoritativePatchParsed:true,conductorExpressionNodesVerified:true,preExistingJsDialectVerified:true,credentialProviderRowPresent:true,modelCalls:0,pluginsBooted:false}));
