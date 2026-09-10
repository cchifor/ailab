import Provider from './openbao-credentials.mjs';
import { mkdtempSync, writeFileSync, mkdirSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const results = [];
const check = (name, cond, detail = '') => results.push({ name, ok: !!cond, detail });
const dir = mkdtempSync(join(tmpdir(), 'baomount-'));
writeFileSync(join(dir, 'LITELLM_API_KEY'), 'bao-value\n');
writeFileSync(join(dir, 'JSON_DOC'), JSON.stringify({ value: 'json-value', other: 'x' }));
writeFileSync(join(dir, 'JSON_NO_FIELD'), JSON.stringify({ other: 'x' }));
writeFileSync(join(dir, 'EMPTY'), '   \n');
const denied = join(dir, 'DENIED');
writeFileSync(denied, 'secret'); chmodSync(denied, 0o000);

const make = (extra = {}) => new Provider({}, { dir, __env: {}, __file: {}, __records: {}, ...extra });

// 1. mount hit
let p = make();
let r = await p.resolve('LITELLM_API_KEY');
check('mount hit resolves from openbao', r?.value === 'bao-value' && r?.source === 'openbao', JSON.stringify(r));

// 2. env WINS over the mount
p = make({ __env: { LITELLM_API_KEY: 'env-value' } });
r = await p.resolve('LITELLM_API_KEY');
check('env layer still wins', r?.value === 'env-value' && r?.source === 'env', JSON.stringify(r));

// 3. absent in mount -> falls through to the local file layer
p = make({ __file: { OTHER_KEY: 'file-value' } });
r = await p.resolve('OTHER_KEY');
check('absent in mount falls through to file', r?.value === 'file-value' && r?.source === 'file', JSON.stringify(r));

// 4. UNREADABLE mount entry must THROW, never fall through
p = make({ __file: { DENIED: 'stale-local' } });
let threw = false;
try { await p.resolve('DENIED'); } catch { threw = true; }
check('unreadable entry fails the operation (no stale fallback)', threw);

// 5. JSON document -> configured field extracted
p = make();
r = await p.resolve('JSON_DOC');
check('json document yields its field', r?.value === 'json-value', JSON.stringify(r));

// 6. JSON document missing the field is a configuration error
p = make({ __file: { JSON_NO_FIELD: 'stale' } });
threw = false;
try { await p.resolve('JSON_NO_FIELD'); } catch { threw = true; }
check('json without the field throws rather than shadowing', threw);

// 7. empty entry is absence
p = make({ __file: { EMPTY: 'file-value' } });
r = await p.resolve('EMPTY');
check('empty entry is absence', r?.value === 'file-value' && r?.source === 'file', JSON.stringify(r));

// 8. describe reports openbao and NOT writable
p = make();
let d = await p.describe('LITELLM_API_KEY');
check('describe: openbao source, not writable', d.configured === true && d.source === 'openbao' && d.writable === false, JSON.stringify(d));

// 9. a reference outside the grammar cannot escape the directory
p = make({ __file: {} });
r = await p.resolve('../../etc/passwd');
check('non-conforming ref never reaches the mount', r === undefined, JSON.stringify(r));

// 10. set/unset rejected for an OpenBao-held reference
p = make();
threw = false; try { await p.set('LITELLM_API_KEY', 'x'); } catch { threw = true; }
check('set rejected for an openbao-held ref', threw);
threw = false; try { await p.unset('LITELLM_API_KEY'); } catch { threw = true; }
check('unset rejected for an openbao-held ref', threw);

// 11. set STILL ALLOWED for a reference OpenBao does not hold
p = make();
let ok = true; try { await p.set('LOCAL_ONLY', 'v'); } catch { ok = false; }
check('set still allowed for a ref openbao does not hold', ok && (await p.resolve('LOCAL_ONLY'))?.value === 'v');

// 12. record half inherited untouched, including the undefined = leave-unchanged contract
p = make({ __records: { 'client-connection/browser-session': { kind: 'api-key', payload: { secret: 's' } } } });
const kept = await p.modifyRecord('client-connection/browser-session', async () => undefined);
check('modifyRecord(undefined) preserves the browser-session record', kept?.payload?.secret === 's', JSON.stringify(kept));
check('listRecords still enumerates records, not refs', (await p.listRecords()).map((e) => e.key).join(',') === 'client-connection/browser-session');

// 13. a missing mount directory entirely is absence, not an error
p = new Provider({}, { dir: join(dir, 'nope'), __file: { X: 'v' } });
r = await p.resolve('X');
check('absent mount directory degrades to the lower layer', r?.value === 'v', JSON.stringify(r));

let failed = 0;
for (const r2 of results) { if (!r2.ok) failed++; console.log(`${r2.ok ? 'ok  ' : 'FAIL'} ${r2.name}${r2.ok ? '' : '  <- ' + r2.detail}`); }
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed === 0 ? 0 : 1);
