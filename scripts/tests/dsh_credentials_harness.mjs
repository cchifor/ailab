// Behaviour suite for openbao-credentials.mjs, run under node against stub base classes.
//
// EVERY CASE RUNS TWICE: once on the raw instance, and once through a Proxy. The Proxy is not
// decoration -- cordis hands a service call a SHADOW RECEIVER (createShadowMethod substitutes
// thisArg for a Proxy over the instance before applying the method), and a native `#private`
// member is branded to the instance, so `this.#anything` inside a method reached through
// `ctx.credentials` throws "Cannot read private member ... from an object whose class did not
// declare it". A suite that only calls the raw instance cannot see that, and an earlier revision
// of the provider shipped exactly that bug.
//
// The stubs reproduce the base-class behaviour this subclass DEPENDS on -- env precedence reported
// as source 'env', a local reference map reported as source 'file', and record methods including
// the "callback returns undefined means leave unchanged" contract. They are not a faithful
// LocalCredentialProvider: they do not reject env-shadowed writes, and they do not validate record
// shapes. Passing here establishes this file's own logic, NOT production compatibility.
import Provider from './openbao-credentials.mjs';
import { mkdtempSync, writeFileSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const results = [];
const check = (name, cond, detail = '') => results.push({ name, ok: !!cond, detail });

const dir = mkdtempSync(join(tmpdir(), 'baomount-'));
writeFileSync(join(dir, 'LITELLM_API_KEY'), 'bao-value');
// A credential whose bytes must survive verbatim: leading/trailing whitespace is significant in
// an opaque secret, and a value that merely begins with `{` is not an envelope.
writeFileSync(join(dir, 'AWKWARD'), '  {"value":"not-an-envelope"}  \n');
writeFileSync(join(dir, 'EMPTY'), '');
const denied = join(dir, 'DENIED');
writeFileSync(denied, 'secret');
chmodSync(denied, 0o000);
// A path whose PARENT is a regular file: readFile gives ENOTDIR, which is broken configuration
// rather than absence and must not fall through.
const notdir = join(dir, 'NOTDIR');
writeFileSync(notdir, 'x');

const RECORD_KEY = 'client-connection/browser-session';

/** Build the provider twice over identical state: raw, and behind a Proxy. */
function pair(extra = {}) {
  const cfg = { dir, __env: {}, __file: {}, __records: {}, ...extra };
  return [
    ['raw', new Provider({}, structuredClone(cfg))],
    ['shadowed', new Proxy(new Provider({}, structuredClone(cfg)), {})],
  ];
}

// --- cases -----------------------------------------------------------------------------------
// Each case names what must hold, then asserts it for both receivers.
const cases = [
  {
    name: 'mount hit resolves from openbao',
    extra: {},
    run: (p) => p.resolve('LITELLM_API_KEY'),
    want: (r) => r?.value === 'bao-value' && r?.source === 'openbao',
  },
  {
    name: 'env layer still wins',
    extra: { __env: { LITELLM_API_KEY: 'env-value' } },
    run: (p) => p.resolve('LITELLM_API_KEY'),
    want: (r) => r?.value === 'env-value' && r?.source === 'env',
  },
  {
    // The precedence that matters most for rotation: a stale value in <cwd>/.env or
    // $DSH_HOME/.env must NOT block OpenBao. Only the INHERITED PROCESS environment outranks it,
    // and the base distinguishes them by source name -- 'env' for process, 'user-env'/'project-env'
    // for the dotenv fallbacks. Keying the yield on `source === 'env'` alone is therefore correct,
    // and this case is what stops that from silently becoming a precedence inversion.
    name: 'a stale .env fallback does not block openbao',
    extra: { __dotenv: { LITELLM_API_KEY: 'stale-dotenv' } },
    run: (p) => p.resolve('LITELLM_API_KEY'),
    want: (r) => r?.value === 'bao-value' && r?.source === 'openbao',
  },
  {
    name: 'a .env fallback still answers when openbao carries nothing',
    extra: { __dotenv: { NOT_IN_MOUNT: 'from-dotenv' } },
    run: (p) => p.resolve('NOT_IN_MOUNT'),
    want: (r) => r?.value === 'from-dotenv' && r?.source === 'user-env',
  },
  {
    name: 'absent in mount falls through to the file layer',
    extra: { __file: { OTHER_KEY: 'file-value' } },
    run: (p) => p.resolve('OTHER_KEY'),
    want: (r) => r?.value === 'file-value' && r?.source === 'file',
  },
  {
    name: 'unreadable entry fails the operation, no stale fallback',
    extra: { __file: { DENIED: 'stale-local' } },
    run: (p) => p.resolve('DENIED'),
    wantThrow: true,
  },
  {
    // `dir` points THROUGH a regular file, so readFile raises ENOTDIR rather than ENOENT. That is
    // broken configuration, and swallowing it would silently serve the stale local value forever.
    name: 'ENOTDIR is broken configuration, not absence',
    extra: { dir: notdir, __file: { X: 'stale-local' } },
    run: (p) => p.resolve('X'),
    wantThrow: true,
  },
  {
    // The companion case: a reference carrying a separator is not a reference at all, and the
    // grammar refuses it before the filesystem is ever consulted.
    name: 'a reference carrying a separator never reaches the mount',
    extra: { __file: { 'NOTDIR/X': 'file-value' } },
    run: (p) => p.resolve('NOTDIR/X'),
    want: (r) => r?.value === 'file-value' && r?.source === 'file',
  },
  {
    name: 'value is returned verbatim, never trimmed or JSON-unwrapped',
    extra: {},
    run: (p) => p.resolve('AWKWARD'),
    want: (r) => r?.value === '  {"value":"not-an-envelope"}  \n',
  },
  {
    name: 'empty entry is absence',
    extra: { __file: { EMPTY: 'file-value' } },
    run: (p) => p.resolve('EMPTY'),
    want: (r) => r?.value === 'file-value' && r?.source === 'file',
  },
  {
    name: 'describe reports openbao and not writable',
    extra: {},
    run: (p) => p.describe('LITELLM_API_KEY'),
    want: (d) => d.configured === true && d.source === 'openbao' && d.writable === false,
  },
  {
    name: 'a reference outside the grammar never reaches the mount',
    extra: {},
    run: (p) => p.resolve('../../etc/passwd'),
    want: (r) => r === undefined,
  },
  {
    name: 'set is rejected for an openbao-held reference',
    extra: {},
    run: (p) => p.set('LITELLM_API_KEY', 'x'),
    wantThrow: true,
  },
  {
    name: 'unset is rejected for an openbao-held reference',
    extra: {},
    run: (p) => p.unset('LITELLM_API_KEY'),
    wantThrow: true,
  },
  {
    name: 'set is still allowed for a reference openbao does not carry',
    extra: {},
    run: async (p) => {
      await p.set('LOCAL_ONLY', 'v');
      return p.resolve('LOCAL_ONLY');
    },
    want: (r) => r?.value === 'v',
  },
  {
    name: 'modifyRecord(undefined) preserves the browser-session record',
    extra: { __records: { [RECORD_KEY]: { kind: 'grant', payload: { secret: 's' } } } },
    run: (p) => p.modifyRecord(RECORD_KEY, async () => undefined),
    want: (r) => r?.payload?.secret === 's',
  },
  {
    name: 'listRecords enumerates records, not references',
    extra: { __records: { [RECORD_KEY]: { kind: 'grant', payload: { secret: 's' } } } },
    run: async (p) => (await p.listRecords()).map((e) => e.key).join(','),
    want: (r) => r === RECORD_KEY,
  },
  {
    name: 'an absent mount directory degrades to the lower layer',
    extra: { dir: join(dir, 'nope'), __file: { X: 'v' } },
    run: (p) => p.resolve('X'),
    want: (r) => r?.value === 'v',
  },
];

for (const c of cases) {
  for (const [kind, p] of pair(c.extra)) {
    let value;
    let error;
    try {
      value = await c.run(p);
    } catch (e) {
      error = e;
    }
    const ok = c.wantThrow ? error !== undefined : error === undefined && c.want(value);
    check(
      `${c.name} [${kind}]`,
      ok,
      error ? `${error.constructor.name}: ${error.message}` : JSON.stringify(value),
    );
  }
}

let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.name}${r.ok ? '' : '  <- ' + r.detail}`);
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed === 0 ? 0 : 1);
