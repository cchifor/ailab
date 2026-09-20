import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {ORIGINAL,REPLACEMENT,VERSION,BUILD,PACKAGE,rewriteActivation,validateIdentity,readBounded,publish,rebuild,sha256} from '../../kubernetes/apps/apps/dsh/build-command-ui-fix.mjs';
const fixture=`const view={${ORIGINAL}};`;
function temporary(fn){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'dsh-ui-fix-test-'));try{return fn(dir);}finally{fs.rmSync(dir,{recursive:true,force:true});}}
test('one targeted rewrite preserves surrounding bytes and parses',()=>{
 assert.equal(rewriteActivation(fixture),`const view={${REPLACEMENT}};`);
});
test('missing or duplicate predicate fails',()=>{
 assert.throws(()=>rewriteActivation('const view={};'),/observed=0, expected=1/);
 assert.throws(()=>rewriteActivation(fixture+fixture),/observed=2, expected=1/);
});
test('malformed rebuilt JavaScript fails syntax checking without execution',()=>{
 assert.throws(()=>rewriteActivation(fixture+' syntax ???'),SyntaxError);
});
test('commands and errors activate without needing a model turn; empty does not',()=>{
 const predicate=new Function(`return ({${REPLACEMENT}}).isActive`)();
 for(const kind of ['command','user','assistant','system','tool'])assert.equal(predicate({order:['a'],nodes:{get:()=>({kind})}}),true);
 assert.equal(predicate({order:[],nodes:{get:()=>undefined}}),false);
 assert.equal(predicate({order:['missing'],nodes:{get:()=>undefined}}),false);
});
test('identity pins cannot silently follow upgrades or another package',()=>{
 validateIdentity(VERSION,BUILD,{name:PACKAGE,version:VERSION});
 for(const args of [['new',BUILD,{name:PACKAGE,version:VERSION}],[VERSION,'other',{name:PACKAGE,version:VERSION}],[VERSION,BUILD,{name:'other',version:VERSION}],[VERSION,BUILD,{name:PACKAGE,version:'new'}]])assert.throws(()=>validateIdentity(...args),/observed=.*expected=/);
});
test('bounded reads reject directories, oversize files and symlinks',()=>temporary(dir=>{
 const file=path.join(dir,'source');fs.writeFileSync(file,'abcd');
 assert.equal(readBounded(file,4).toString(),'abcd');
 assert.throws(()=>readBounded(file,3),/bounded/);
 assert.throws(()=>readBounded(dir),/regular/);
 fs.symlinkSync(file,path.join(dir,'link'));assert.throws(()=>readBounded(path.join(dir,'link')));
}));
test('publication is atomic, read-only and idempotent',()=>temporary(dir=>{
 const file=path.join(dir,'client.js');const bytes=Buffer.from('verified fixture');
 publish(file,bytes);const before=fs.statSync(file);
 publish(file,bytes);assert.equal(fs.statSync(file).ino,before.ino);
 assert.equal(fs.statSync(file).mode&0o777,0o444);
 assert.equal(sha256(readBounded(file)),sha256(bytes));
 assert.equal(fs.existsSync(file+'.new'),false);
 assert.throws(()=>publish(file,Buffer.from('unexpected')),/existing output SHA256/);
}));
test('publication refuses live and dangling output symlinks',()=>temporary(dir=>{
 const file=path.join(dir,'client.js');const other=path.join(dir,'other');
 fs.writeFileSync(other,'original');fs.symlinkSync(other,file);
 assert.throws(()=>publish(file,Buffer.from('x')));
 fs.unlinkSync(file);fs.symlinkSync(path.join(dir,'missing'),file);
 assert.throws(()=>publish(file,Buffer.from('x')));
 assert.equal(fs.readFileSync(other,'utf8'),'original');
}));
test('stale temporary file is recovered but a temporary symlink is refused',()=>temporary(dir=>{
 const file=path.join(dir,'client.js');fs.writeFileSync(file+'.new','partial');
 publish(file,Buffer.from('verified'));assert.equal(fs.readFileSync(file,'utf8'),'verified');
 const second=path.join(dir,'second');fs.symlinkSync(file,second+'.new');
 assert.throws(()=>publish(second,Buffer.from('x')),/Unsafe stale/);
}));
test('CLI entrypoint executes through a ConfigMap-style symlink instead of silently exiting',()=>temporary(dir=>{
 const script=fileURLToPath(new URL('../../kubernetes/apps/apps/dsh/build-command-ui-fix.mjs',import.meta.url));
 const link=path.join(dir,'projected.mjs');fs.symlinkSync(script,link);
 const result=spawnSync(process.execPath,[link,dir,path.join(dir,'output')],{encoding:'utf8',env:{...process.env,DSH_VERSION:'unsupported',DSH_BUILD:BUILD}});
 assert.equal(result.status,1);assert.match(result.stderr,/DSH_VERSION: observed=unsupported/);
 assert.equal(fs.existsSync(path.join(dir,'output')),false);
}));
test('CLI pipeline rejects wrong installed source before publishing',()=>temporary(dir=>{
 const tree=path.join(dir,`${VERSION}-${BUILD}`);const root=path.join(tree,'node_modules',PACKAGE);
 fs.mkdirSync(path.join(root,'lib'),{recursive:true});fs.writeFileSync(path.join(tree,'.installed'),'ready');
 fs.writeFileSync(path.join(root,'lib/client.js'),fixture);
 const output=path.join(os.tmpdir(),path.basename(dir)+'-never-published');
 assert.throws(()=>rebuild(dir,output,VERSION,BUILD),/source artifact SHA256: observed=.*expected=/);
 assert.equal(fs.existsSync(output),false);
 assert.throws(()=>rebuild(dir,path.join(dir,'forbidden'),VERSION,BUILD),/must not be inside/);
 assert.throws(()=>rebuild(dir,output,'future',BUILD),/DSH_VERSION/);
}));
