#!/usr/bin/env node
/** Operator-only Linux/Node24 artifact staging. No network or package execution.
 * Source may be a projected ConfigMap symlink. Destination ancestry must be
 * operator-owned: component checks are not an openat sandbox against hostile
 * concurrent directory renames. File publication is atomic and no-clobber;
 * crash durability depends on the underlying filesystem. A killed process may
 * leave an exclusive temporary file; it is never treated as a published asset.
 */
import * as fs from 'node:fs';
import {createHash,randomUUID} from 'node:crypto';
import {resolve,join,parse} from 'node:path';
import {fileURLToPath} from 'node:url';
export const MAX_SIZE=1048576, PACKAGE_NAME='dsh-team-conductor-0.1.0.tgz';
const C=fs.constants;
const codes=new Set(['INVALID_ARGS','INVALID_HASH_FORMAT','SOURCE_NOT_REGULAR','SOURCE_TOO_LARGE','SOURCE_READ_FAILED','HASH_MISMATCH','DEST_DIR_CREATE_FAILED','DEST_IS_SYMLINK','DEST_NOT_REGULAR','DEST_HARDLINKED','DEST_HASH_MISMATCH','TEMP_CREATE_FAILED','WRITE_FAILED','SYNC_FAILED','LINK_FAILED','UNLINK_FAILED']);
const fail=code=>{throw new Error(code);};
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
function boundedRead(path,source){
 let fd;
 try{
  // Numeric flags are supported by Node; NONBLOCK prevents FIFO-open hangs.
  fd=fs.openSync(path,C.O_RDONLY|C.O_NONBLOCK|(source?0:C.O_NOFOLLOW));
  const stat=fs.fstatSync(fd);
  if(!stat.isFile())fail(source?'SOURCE_NOT_REGULAR':'DEST_NOT_REGULAR');
  if(!source&&stat.nlink!==1)fail('DEST_HARDLINKED');
  if(stat.size>MAX_SIZE)fail(source?'SOURCE_TOO_LARGE':'DEST_HASH_MISMATCH');
  const buffer=Buffer.alloc(MAX_SIZE+1);let used=0;
  while(used<buffer.length){const n=fs.readSync(fd,buffer,used,buffer.length-used,null);if(!n)break;used+=n;}
  if(used>MAX_SIZE)fail(source?'SOURCE_TOO_LARGE':'DEST_HASH_MISMATCH');
  if(!used)fail(source?'SOURCE_READ_FAILED':'DEST_HASH_MISMATCH');
  return buffer.subarray(0,used);
 }catch(error){
  if(codes.has(error.message))throw error;
  fail(source?'SOURCE_NOT_REGULAR':'DEST_NOT_REGULAR');
 }finally{if(fd!==undefined)fs.closeSync(fd);}
}
function directories(path){
 let current=parse(path).root;
 for(const part of path.slice(current.length).split('/').filter(Boolean)){
  current=join(current,part);let stat;
  try{stat=fs.lstatSync(current);}catch(error){
   if(error.code!=='ENOENT')fail('DEST_DIR_CREATE_FAILED');
   try{fs.mkdirSync(current,{mode:0o755});}catch(e){if(e.code!=='EEXIST')fail('DEST_DIR_CREATE_FAILED');}
   stat=fs.lstatSync(current);
  }
  if(stat.isSymbolicLink()||!stat.isDirectory())fail('DEST_DIR_CREATE_FAILED');
 }
}
export function stagePrivateConductor(sourcePath,expectedHash,destinationRoot){
 let temp,fd;
 try{
  if(arguments.length!==3||![sourcePath,destinationRoot].every(v=>typeof v==='string'&&v.length>0&&v.length<=4096))fail('INVALID_ARGS');
  if(typeof expectedHash!=='string'||!/^[a-f0-9]{64}$/.test(expectedHash))fail('INVALID_HASH_FORMAT');
  const bytes=boundedRead(resolve(sourcePath),true);
  if(hash(bytes)!==expectedHash)fail('HASH_MISMATCH');
  // No destination writes occur until the exact publication buffer is verified.
  const dir=join(resolve(destinationRoot),expectedHash),destination=join(dir,PACKAGE_NAME);
  directories(dir);let existing;
  try{existing=fs.lstatSync(destination);}catch(e){if(e.code!=='ENOENT')fail('DEST_NOT_REGULAR');}
  if(existing){
   if(existing.isSymbolicLink())fail('DEST_IS_SYMLINK');
   if(hash(boundedRead(destination,false))!==expectedHash)fail('DEST_HASH_MISMATCH');
   return {success:true,destination,hash:expectedHash};
  }
  const candidate=join(dir,'.artifact-'+randomUUID()+'.tmp');
  try{fd=fs.openSync(candidate,C.O_WRONLY|C.O_CREAT|C.O_EXCL|C.O_NOFOLLOW,0o644);temp=candidate;}catch{fail('TEMP_CREATE_FAILED');}
  try{fs.writeFileSync(fd,bytes);}catch{fail('WRITE_FAILED');}
  try{fs.fsyncSync(fd);}catch{fail('SYNC_FAILED');}
  fs.closeSync(fd);fd=undefined;
  // link is exclusive: an existing regular file OR dangling link is not replaced.
  try{fs.linkSync(temp,destination);}catch{fail('LINK_FAILED');}
  try{fs.unlinkSync(temp);temp=undefined;}catch{fail('UNLINK_FAILED');}
  return {success:true,destination,hash:expectedHash};
 }catch(error){return {success:false,error:codes.has(error.message)?error.message:'WRITE_FAILED'};}
 finally{if(fd!==undefined){try{fs.closeSync(fd);}catch{}}if(temp){try{fs.unlinkSync(temp);}catch{}}}
}
// Resolve the entry realpath so a ConfigMap-projected script is a working CLI.
let main=false;try{main=!!process.argv[1]&&fs.realpathSync(process.argv[1])===fileURLToPath(import.meta.url);}catch{}
if(main){const args=process.argv.slice(2);const result=args.length===3?stagePrivateConductor(...args):{success:false,error:'INVALID_ARGS'};console.log(JSON.stringify(result));process.exitCode=result.success?0:1;}
