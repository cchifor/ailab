// Compatibility check against the REAL shipped closure: @deepseek-ai/dsh-credentials-local
// @0.1.5-alpha.2 and its real cordis, reached through ctx.credentials -- the actual service
// dispatch path, shadow receiver and all.
import { Context } from '@deepseek-ai/cordis';
import { createLaunchEnvironmentSnapshot } from '@deepseek-ai/dsh-launch-environment';
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

// --- the .env fallback layers, against the REAL launch-environment snapshot ------------------
// The stub suite asserts that a stale `.env` value does not block OpenBao. That assertion is only
// worth anything if the real base class really does report those layers as 'user-env'/'project-env'
// rather than 'env' -- so it is checked here against the shipped snapshot implementation.
{
  const h = mkdtempSync(join(tmpdir(), 'dotenv-home-'));
  writeFileSync(join(h, '.credentials.yaml'), 'version: 1\n');
  chmodSync(join(h, '.credentials.yaml'), 0o600);
  writeFileSync(join(mount, 'DOTENV_KEY'), 'from-openbao');
  const c2 = new Context();
  c2.provide(
    'launchEnvironment',
    createLaunchEnvironmentSnapshot([
      { source: 'process', values: {} },
      { source: 'user-env', path: '/x/.env', values: { DOTENV_KEY: 'stale-dotenv' } },
    ]),
  );
  c2.plugin(Provider, { path: join(h, '.credentials.yaml'), dir: mount, watch: false });
  await new Promise((r) => setTimeout(r, 200));
  const got = await c2.credentials.resolve('DOTENV_KEY');
  t('openbao wins over a stale .env fallback', got?.value === 'from-openbao', JSON.stringify(got));
}

// --- the EXACT production config -------------------------------------------------------------
// The cordis insert supplies only `dir`; `path` and `dshHome` are absent, so the document is
// resolved from $DSH_HOME. Every case above passes `path` explicitly, which means the config shape
// the pod actually runs was never exercised. It is now, watcher and all (watch defaults true).
{
  const h = mkdtempSync(join(tmpdir(), 'prod-home-'));
  writeFileSync(join(h, '.credentials.yaml'), doc);
  chmodSync(join(h, '.credentials.yaml'), 0o600);
  process.env.DSH_HOME = h;
  const c3 = new Context();
  c3.plugin(Provider, { dir: mount });
  await new Promise((r) => setTimeout(r, 300));
  t('the production config registers the service', !!c3.credentials);
  const got = await c3.credentials?.resolve('NEW_CREDENTIAL');
  t('the production config resolves from the mount', got?.value === 'appeared-without-config', JSON.stringify(got));
  const rec = await c3.credentials?.readRecord('client-connection/browser-session');
  t('the production config finds the document under $DSH_HOME', rec?.payload?.secret === 'preexisting-secret', JSON.stringify(rec));
}

let bad = 0;
for (const x of out) { if (!x.ok) bad++; console.log(`${x.ok ? 'ok  ' : 'FAIL'} ${x.n}${x.ok ? '' : '  <- ' + x.d}`); }
console.log(`\n${out.length - bad}/${out.length} passed against the real closure`);
process.exit(bad === 0 ? 0 : 1);
