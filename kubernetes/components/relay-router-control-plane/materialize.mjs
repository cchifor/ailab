import { createPrivateKey as parsePrivateKey } from 'node:crypto';
import { constants } from 'node:fs';
import { open, mkdir, chmod, rename, rm } from 'node:fs/promises';

const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const ident = /^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$/;
const mount = /^[A-Za-z0-9_-]+$/;
const prefix = /^[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*$/;

async function read(name, maximum) {
  const file = await open(`/input/${name}`, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > maximum) throw new Error('invalid input');
    const bytes = Buffer.alloc(maximum + 1);
    const { bytesRead } = await file.read(bytes, 0, bytes.length, 0);
    if (bytesRead > maximum) throw new Error('oversized input');
    return bytes.subarray(0, bytesRead).toString('utf8').trim();
  } finally { await file.close(); }
}

async function write(name, value, mode = 0o600) {
  const path = `/output/private/${name}`;
  await rm(`${path}.new`, { force: true });
  const file = await open(`${path}.new`, 'wx', mode);
  try { await file.writeFile(value); await file.sync(); } finally { await file.close(); }
  await rename(`${path}.new`, path);
}

function origin(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.origin !== value || url.pathname !== '/' || url.username || url.password || url.search || url.hash)
    throw new Error('invalid origin');
  return url.origin;
}

try {
  const metadata = JSON.parse(await read('metadata.json', 16384));
  const expected = ['baoMount', 'baoOrigin', 'baoPrefix', 'id', 'issuer', 'kid', 'name', 'origin', 'reconcilerKid', 'tenants'];
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata) || Object.keys(metadata).sort().join(',') !== expected.join(',')) throw new Error('invalid metadata');
  if (!uuid.test(metadata.id) || !ident.test(metadata.issuer) || !ident.test(metadata.kid) || !ident.test(metadata.reconcilerKid) || typeof metadata.name !== 'string' || metadata.name.length < 1 || metadata.name.length > 80) throw new Error('invalid metadata');
  if (!Array.isArray(metadata.tenants) || metadata.tenants.length < 1 || metadata.tenants.length > 100 || metadata.tenants.some(value => typeof value !== 'string' || !uuid.test(value))) throw new Error('invalid tenants');
  const routerOrigin = origin(metadata.origin), baoOrigin = origin(metadata.baoOrigin);
  if (!mount.test(metadata.baoMount) || !prefix.test(metadata.baoPrefix)) throw new Error('invalid OpenBao path');
  const admin = await read('admin-key.pem', 8192), reconciler = await read('reconciler-key.pem', 8192), token = await read('bao-token', 8192);
  if (!token || /\s/.test(token) || token.length < 20) throw new Error('invalid OpenBao token');
  for (const key of [admin, reconciler]) {
    const parsed = parsePrivateKey(key);
    if (parsed.asymmetricKeyType !== 'ed25519') throw new Error('invalid signing key');
  }
  await mkdir('/output/private', { recursive: true, mode: 0o700 });
  await chmod('/output/private', 0o700);
  await write('admin-key.pem', admin);
  await write('reconciler-key.pem', reconciler);
  await write('bao-token', token);
  await write('connections.json', JSON.stringify([{
    id: metadata.id,
    name: metadata.name,
    tenants: metadata.tenants,
    origin: routerOrigin,
    issuer: metadata.issuer,
    kid: metadata.kid,
    signingKeyFile: '/run/relay-router/private/admin-key.pem',
    reconcilerKid: metadata.reconcilerKid,
    reconcilerKeyFile: '/run/relay-router/private/reconciler-key.pem',
    baoOrigin,
    baoTokenFile: '/run/relay-router/private/bao-token',
    baoMount: metadata.baoMount,
    baoPrefix: metadata.baoPrefix,
  }], null, 2) + '\n');
} catch (error) {
  const code = /^E[A-Z]{1,20}$/.test(error?.code ?? '') ? error.code : 'INVALID_INPUT';
  console.error(`Relay router private-file setup failed (${code}); input values withheld.`);
  process.exitCode = 1;
}
