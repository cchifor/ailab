// Deployment-owned hotfix: rebuild ONLY the reviewed Web artifact into emptyDir.
// The shared installation is always mounted read-only; never patch it in place.
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {Script} from 'node:vm';
import {pathToFileURL} from 'node:url';

export const VERSION='0.1.5-alpha.2';
export const BUILD='glibc';
export const PACKAGE='@deepseek-ai/dsh-client-ui-chat';
export const SOURCE_SHA256='3891fc589652c50be24c6d8e68d35c5d4066ed9ae7b7d57a086d5a34ad20c0d9';
export const OUTPUT_SHA256='9f53814b335eaa3f892fd70e8a22f167b4ba6fef412c598309a17d001bcf847c';
export const ORIGINAL='isActive: (snapshot) => snapshot.order.some((key) => snapshot.nodes.get(key)?.kind !== "command")';
export const REPLACEMENT='isActive: (snapshot) => snapshot.order.some((key) => snapshot.nodes.get(key) !== void 0)';
export const sha256=bytes=>createHash('sha256').update(bytes).digest('hex');
function equal(actual,expected,label){if(actual!==expected)throw new Error(`${label}: observed=${actual}, expected=${expected}; remove or rebase the versioned UI hotfix`);}
export function readBounded(file,limit=1024*1024){
 const fd=fs.openSync(file,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
 try{
  const stat=fs.fstatSync(fd);
  if(!stat.isFile()||stat.size>limit)throw new Error(`Not a bounded regular file: ${file}`);
  const buffer=Buffer.alloc(limit+1);let size=0;
  while(size<buffer.length){const n=fs.readSync(fd,buffer,size,buffer.length-size,null);if(n===0)break;size+=n;}
  if(size>limit)throw new Error(`File exceeds ${limit} bytes: ${file}`);
  return buffer.subarray(0,size);
 }finally{fs.closeSync(fd);}
}
export function rewriteActivation(source){
 equal(source.split(ORIGINAL).length-1,1,'activation predicate matches');
 const output=source.replace(ORIGINAL,REPLACEMENT);
 new Script(output,{filename:'dsh-client-ui-chat/client.js'}); // compile only; no execution
 return output;
}
export function validateIdentity(version,build,meta){
 equal(version,VERSION,'DSH_VERSION');equal(build,BUILD,'DSH_BUILD');
 equal(meta.name,PACKAGE,'package name');equal(meta.version,VERSION,'package version');
}
function existsNoFollow(file){try{fs.lstatSync(file);return true;}catch(error){if(error.code==='ENOENT')return false;throw error;}}
// Caller validates pinned input/output before publication; this helper only owns atomic IO.
export function publish(outputFile,bytes){
 if(existsNoFollow(outputFile)){
  equal(sha256(readBounded(outputFile)),sha256(bytes),'existing output SHA256');
  return; // retry is idempotent; never replace an unknown output
 }
 const temporary=outputFile+'.new';
 // A previous killed init may leave an incomplete temp file in its OWN emptyDir.
 if(existsNoFollow(temporary)){
  if(!fs.lstatSync(temporary).isFile()||fs.lstatSync(temporary).isSymbolicLink())throw new Error('Unsafe stale temporary artifact');
  fs.unlinkSync(temporary);
 }
 fs.writeFileSync(temporary,bytes,{flag:'wx',mode:0o444});
 fs.renameSync(temporary,outputFile);
}
export function rebuild(appRoot,outputFile,version,build){
 equal(version,VERSION,'DSH_VERSION');equal(build,BUILD,'DSH_BUILD');
 const tree=path.join(appRoot,`${version}-${build}`);
 if(path.resolve(outputFile).startsWith(path.resolve(appRoot)+path.sep))throw new Error('Output must not be inside the installed app tree');
 readBounded(path.join(tree,'.installed'),64*1024);
 const packageRoot=path.join(tree,'node_modules',PACKAGE);
 const sourceFile=path.join(packageRoot,'lib/client.js');
 const source=readBounded(sourceFile);
 equal(sha256(source),SOURCE_SHA256,'source artifact SHA256');
 const meta=JSON.parse(readBounded(path.join(packageRoot,'package.json'),64*1024).toString('utf8'));
 validateIdentity(version,build,meta);
 if(fs.existsSync(sourceFile+'.map'))throw new Error('Unexpected source map; hotfix must be rebased with matching debug artifacts');
 const output=Buffer.from(rewriteActivation(source.toString('utf8')),'utf8');
 equal(sha256(output),OUTPUT_SHA256,'rebuilt artifact SHA256');
 publish(outputFile,output);
 return {kind:'dsh-command-ui-fix',version,build,sourceSha256:sha256(source),outputSha256:sha256(output),bytes:output.length};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(fs.realpathSync(process.argv[1])).href){
 try{
  const [appRoot,outputFile]=process.argv.slice(2);
  if(!appRoot||!outputFile)throw new Error('Usage: build-command-ui-fix.mjs APP_ROOT OUTPUT_FILE');
  console.log(JSON.stringify(rebuild(appRoot,outputFile,process.env.DSH_VERSION,process.env.DSH_BUILD)));
 }catch(error){console.error(`Command UI hotfix refused: ${error.message}`);process.exitCode=1;}
}
