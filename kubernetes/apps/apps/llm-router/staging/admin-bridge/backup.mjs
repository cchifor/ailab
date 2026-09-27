import {DatabaseSync, backup} from 'node:sqlite';
import {existsSync, mkdirSync, copyFileSync, writeFileSync, chmodSync} from 'node:fs';
const dest='/releases/.backups/20260927-pre-admin-bridge';
if (!existsSync(`${dest}/COMPLETE`)) {
  mkdirSync(dest,{recursive:true,mode:0o700});
  const db=new DatabaseSync('/data/router.sqlite',{readOnly:true});
  try {await backup(db,`${dest}/router.sqlite`);} finally {db.close();}
  for(const file of ['router.secrets.key','plugins.yml']) if(existsSync(`/data/${file}`)) {
    copyFileSync(`/data/${file}`,`${dest}/${file}`);chmodSync(`${dest}/${file}`,0o600);
  }
  const check=new DatabaseSync(`${dest}/router.sqlite`,{readOnly:true});
  try {if(check.prepare('PRAGMA quick_check').get().quick_check!=='ok')throw new Error('Backup integrity check failed');}finally{check.close();}
  writeFileSync(`${dest}/COMPLETE`,'Pre-admin-bridge snapshot\n',{mode:0o600});
}
console.info('Consistent router snapshot retained.');
