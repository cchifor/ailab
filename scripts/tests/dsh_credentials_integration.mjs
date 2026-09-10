// Compatibility check against the REAL shipped closure: @deepseek-ai/dsh-credentials-local
// @0.1.5-alpha.2 and its real cordis, reached through ctx.credentials -- the actual service
// dispatch path, shadow receiver and all.
import { Context } from '@deepseek-ai/cordis';
import Provider from './openbao-credentials.mjs';
import { mkdtempSync, writeFileSync, readFileSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const mount = mkdtempSync(join(tmpdir(), 'mount-'));
const home = mkdtempSync(join(tmpdir(), 'home-'));
writeFileSync(join(mount, 'LITELLM_API_KEY'), 'from-openbao');
writeFileSync(join(mount, 'NEW_CREDENTIAL'), 'appeared-without-config');
// A pre-existing document holding exactly what the live pod holds: one record, no refs.
const doc = 'version: 1\nrecords:\n  client-connection/browser-session:\n    kind: grant\n    payload:\n      version: 1\n      secret: preexisting-secret\n';
writeFileSync(join(home, '.credentials.yaml'), doc);
// The base class REFUSES to start on a document readable beyond its owner. Verified, not assumed.
chmodSync(join(home, '.credentials.yaml'), 0o600);

const out = [];
const t = (n, ok, d = '') => out.push({ n, ok: !!ok, d });

const ctx = new Context();
ctx.plugin(Provider, { path: join(home, '.credentials.yaml'), dir: mount, watch: false });
await new Promise((r) => setTimeout(r, 200));

const creds = ctx.credentials;
t('the service registered under the singleton name', !!creds);

// The whole point of the shadow-receiver fix: these calls go through cordis dispatch.
let r = await creds.resolve('LITELLM_API_KEY');
t('resolves from the mount through ctx.credentials', r?.value === 'from-openbao' && r?.source === 'openbao', JSON.stringify(r));

r = await creds.resolve('NEW_CREDENTIAL');
t('a credential never named in config resolves', r?.value === 'appeared-without-config', JSON.stringify(r));

process.env.ENV_WINS = 'from-env';
writeFileSync(join(mount, 'ENV_WINS'), 'from-openbao');
r = await creds.resolve('ENV_WINS');
t('the inherited environment still wins', r?.value === 'from-env' && r?.source === 'env', JSON.stringify(r));

const d = await creds.describe('LITELLM_API_KEY');
t('describe reports openbao, not writable', d.configured && d.source === 'openbao' && d.writable === false, JSON.stringify(d));

// The record half must be untouched, and this is the real implementation, not a stub.
const kept = await creds.modifyRecord('client-connection/browser-session', async () => undefined);
t('modifyRecord(undefined) preserves the real browser-session record', kept?.payload?.secret === 'preexisting-secret', JSON.stringify(kept));
const listed = await creds.listRecords();
t('listRecords enumerates the real record', listed.map((e) => e.key).join(',') === 'client-connection/browser-session', JSON.stringify(listed));
t('the document on disk is unchanged', readFileSync(join(home, '.credentials.yaml'), 'utf8') === doc);

let threw = false;
try { await creds.set('LITELLM_API_KEY', 'x'); } catch { threw = true; }
t('set is rejected for an openbao-held reference', threw);

// Rotation: rewrite the mount and re-resolve, no restart.
writeFileSync(join(mount, 'LITELLM_API_KEY'), 'rotated');
r = await creds.resolve('LITELLM_API_KEY');
t('a rotation is visible to the next operation', r?.value === 'rotated', JSON.stringify(r));

let bad = 0;
for (const x of out) { if (!x.ok) bad++; console.log(`${x.ok ? 'ok  ' : 'FAIL'} ${x.n}${x.ok ? '' : '  <- ' + x.d}`); }
console.log(`\n${out.length - bad}/${out.length} passed against the real closure`);
process.exit(bad === 0 ? 0 : 1);
