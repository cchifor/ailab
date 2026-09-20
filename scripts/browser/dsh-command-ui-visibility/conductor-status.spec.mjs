import {test,expect} from '@playwright/test';
import {mkdtempSync,readdirSync,rmdirSync} from 'node:fs';
import path from 'node:path';
import {openAuthenticatedUI,baseURL} from './support.mjs';

test('first command is visible without a chat turn and survives reload',async({},testInfo)=>{
 const {browser,page}=await openAuthenticatedUI();
 const cwd=mkdtempSync('/workspace/dsh-status-e2e-');
 const workspaceName=path.basename(cwd);
 const results=[];const events=[];const responseErrors=[];
 page.on('response',async response=>{
  if(new URL(response.url()).pathname==='/api/commands/execute'){
   try{results.push({status:response.status(),reply:await response.json()});}
   catch{responseErrors.push('Could not read command response');}
  }
 });
 page.on('websocket',ws=>{
  ws.on('framereceived',frame=>{
   const text=String(frame.payload);
   const markers=['command/run','command/done'].filter(marker=>text.includes(marker));
   if(markers.length)events.push({time:new Date().toISOString(),markers});
  });
 });
 try{
  await page.goto(baseURL,{waitUntil:'domcontentloaded'});
  // Finish restored-session autofocus before opening a modal, so focus is not stolen.
  await expect(page.locator('[data-composer-input="true"]')).toBeFocused({timeout:15000});
  await page.getByRole('button',{name:'Add workspace',exact:true}).click();
  await page.getByRole('button',{name:'Edit path',exact:true}).click();
  const input=page.locator('input:not([type="file"]):not([placeholder="Search sessions..."])');
  await input.fill('');await input.pressSequentially(cwd,{delay:2});
  await expect(input).toHaveValue(cwd);await input.press('Enter');
  await page.getByRole('navigation').getByRole('button',{name:workspaceName,exact:true}).waitFor();
  await page.getByRole('button',{name:'Open',exact:true}).click();
  await expect(page.getByRole('button',{name:'Choose workspace',exact:true})).toHaveText(workspaceName);
  await page.locator('button[title="Agent preset for the session you are about to start"]').click();
  await page.getByText('Team Conductor (0.1.0)',{exact:true}).click();
  const composer=page.locator('[data-composer-input="true"]');
  await composer.fill('/conductor status');await composer.press('Enter');
  await expect(page.getByText(/cchifor\/dsh-team-conductor/).first()).toBeVisible({timeout:12000});
  await expect(page.locator('body')).toContainText('"runs": []');
  await expect.poll(()=>results.length).toBe(1);
  expect(results[0].status).toBe(200);
  expect(results[0].reply.result.value.result.kind).toBe('success');
  await page.reload({waitUntil:'domcontentloaded'});
  await expect(page.getByText(/cchifor\/dsh-team-conductor/).first()).toBeVisible({timeout:12000});
  await composer.fill('/conductor ');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await expect.poll(()=>results.length).toBe(2);
  const alias=results[1].reply.result.value.result;
  expect(results[1].status).toBe(200);expect(alias.kind).toBe('success');
  const status=JSON.parse(alias.text);
  expect(status.repository).toBe('cchifor/dsh-team-conductor');expect(status.runs).toEqual([]);
  await expect(page.getByText(/cchifor\/dsh-team-conductor/).last()).toBeVisible();
  await composer.fill('/conductor __e2e_unknown__');await composer.press('Enter');
  await expect.poll(()=>results.length).toBe(3);
  expect(results[2].reply.result.value.result.kind).toBe('error');
  await expect(page.getByText('/conductor status | start <task> | pause <id> | resume <id> | resume-ack <id>',{exact:true}).first()).toBeVisible();
  expect(responseErrors).toEqual([]);
 }finally{
  try{
   await testInfo.attach('safe-command-evidence',{body:JSON.stringify({cwd,url:page.url(),interception:false,events,results,responseErrors},null,2),contentType:'application/json'});
   await page.screenshot({path:testInfo.outputPath('command-status.png')});
  }finally{
   try{
    // User explicitly requested cleanup: remove only this test's unique registration.
    // UI delete preserves session audit logs; never remove other workspaces/histories.
    const row=page.getByRole('treeitem',{name:workspaceName,exact:true});
    if(await row.count()){
     await row.hover();
     await page.getByRole('button',{name:'Workspace actions for '+workspaceName,exact:true}).click();
     await page.getByText('Delete workspace',{exact:true}).click();
     const dialog=page.getByRole('dialog');await expect(dialog).toContainText('“'+workspaceName+'”');
     await dialog.getByRole('button',{name:'Delete workspace',exact:true}).click();
     await expect(row).toHaveCount(0);await expect(dialog).toHaveCount(0);
    }
    await page.reload({waitUntil:'domcontentloaded'});
    await page.getByRole('tree',{name:'Sessions',exact:true}).waitFor();
    await expect(page.getByRole('treeitem',{name:workspaceName,exact:true})).toHaveCount(0);
    if(readdirSync(cwd).length===0)rmdirSync(cwd);
    console.log(JSON.stringify({workspace:workspaceName,removed:true,verifiedAfterRefresh:true}));
   }finally{await browser.close();}
  }
 }
});
