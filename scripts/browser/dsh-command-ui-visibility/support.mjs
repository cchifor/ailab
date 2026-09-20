import {chromium} from '@playwright/test';
import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
export const baseURL='http://127.0.0.1:3080';
export async function openAuthenticatedUI(){
 assert.equal(process.env.DSH_RUN_LIVE_UI_TEST,'1','Set DSH_RUN_LIVE_UI_TEST=1 explicitly; this test creates then removes an isolated workspace');
 const pods=JSON.parse(execFileSync('kubectl',['get','pods','-n','dsh','-l','app=dsh','-o','json'],{encoding:'utf8',timeout:15000})).items;
 const active=pods.filter(p=>!p.metadata.deletionTimestamp&&p.status.phase==='Running');
 assert.equal(active.length,1,'Exactly one current DSH pod is required');
 // Normal operator sign-in only. No secret files, saved cookies, raw tracing,
 // auth weakening or synthetic authorization. Never surface kubectl log bodies.
 let logs;
 try{logs=execFileSync('kubectl',['logs','-n','dsh',active[0].metadata.name,'-c','dsh','--tail=1000'],{encoding:'utf8',maxBuffer:4*1024*1024,timeout:15000,stdio:['ignore','pipe','pipe']});}
 catch{throw new Error('Could not obtain the normal operator launch URL; sensitive diagnostics omitted');}
 const matches=[...logs.matchAll(/http:\/\/127\.0\.0\.1:3080\/\?token=([A-Za-z0-9_-]+)/g)];
 assert.ok(matches.length,'Normal launch URL unavailable; no alternate authentication attempted');
 let response;
 try{response=await fetch(baseURL+'/?token='+encodeURIComponent(matches.at(-1)[1]),{redirect:'manual',signal:AbortSignal.timeout(15000)});}
 catch{throw new Error('Normal local sign-in failed; sensitive URL omitted');}
 assert.ok([200,302,303].includes(response.status),'Normal sign-in must succeed');
 const headers=response.headers.getSetCookie();assert.ok(headers.length,'Sign-in must issue a cookie');
 const cookies=headers.map(header=>{const [pair,...attributes]=header.split(';');const at=pair.indexOf('=');const site=attributes.map(a=>a.trim().toLowerCase()).find(a=>a.startsWith('samesite='))?.slice(9);return {name:pair.slice(0,at),value:pair.slice(at+1),url:baseURL,httpOnly:attributes.some(a=>a.trim().toLowerCase()==='httponly'),secure:attributes.some(a=>a.trim().toLowerCase()==='secure'),sameSite:site==='strict'?'Strict':site==='none'?'None':'Lax'};});
 const env={...process.env};
 if(process.env.DSH_BROWSER_LIBRARY_PATH)env.LD_LIBRARY_PATH=process.env.DSH_BROWSER_LIBRARY_PATH;
 const browser=await chromium.launch({headless:true,env});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  await context.addCookies(cookies);
  const page=await context.newPage();page.setDefaultTimeout(10000);page.setDefaultNavigationTimeout(15000);
  return {browser,page}; // Deliberately no route/interception or candidate-patch option.
 }catch(error){await browser.close();throw error;}
}
