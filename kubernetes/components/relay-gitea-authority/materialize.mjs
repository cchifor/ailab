// Input is the kubelet's projected Secret. Output is private, regular tmpfs files.
import { constants } from 'node:fs';
import { open, mkdir, chmod, rename, rm } from 'node:fs/promises';
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const repo = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;
const branch = v => typeof v === 'string' && /^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/.test(v) && !v.includes('..') && !v.includes('//') && !v.endsWith('/') && !v.endsWith('.') && v.split('/').every(p => !p.startsWith('.') && !p.endsWith('.lock'));
const require = value => { if (!value) throw Error('invalid input'); };
async function read(name, maximum) {
  const file = await open('/input/' + name, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    const stat = await file.stat(); require(stat.isFile() && stat.size <= maximum);
    const bytes = Buffer.alloc(maximum + 1), {bytesRead} = await file.read(bytes, 0, bytes.length, 0);
    require(bytesRead <= maximum); return bytes.subarray(0, bytesRead).toString('utf8').trim();
  } finally { await file.close(); }
}
async function write(name, value) {
  const path = '/output/private/' + name;
  await rm(path + '.new', {force: true});
  const file = await open(path + '.new', 'wx', 0o600);
  try { await file.writeFile(value); await file.sync(); } finally { await file.close(); }
  await rename(path + '.new', path);
}
try {
  const inputs = JSON.parse(await read('deployments.json', 32768));
  require(Array.isArray(inputs) && inputs.length <= 20);
  const ids = new Set(), result = [], tokens = [];
  for (const item of inputs) {
    require(item && typeof item === 'object' && !Array.isArray(item));
    require(Object.keys(item).every(k => ['id','tenants','origin','repositories','enabled','pullRequests','ssh'].includes(k)));
    require(uuid.test(item.id) && !ids.has(item.id)); ids.add(item.id);
    require(Array.isArray(item.tenants) && item.tenants.length >= 1 && item.tenants.length <= 100 && item.tenants.every(v => typeof v === 'string' && uuid.test(v)));
    require(Array.isArray(item.repositories) && item.repositories.length >= 1 && item.repositories.length <= 100 && item.repositories.every(v => typeof v === 'string' && repo.test(v)));
    const origin = new URL(item.origin);
    require(origin.protocol === 'https:' && origin.origin === item.origin && !origin.username && !origin.password);
    require(item.enabled === undefined || typeof item.enabled === 'boolean');
    if (item.pullRequests !== undefined) {
      require(item.pullRequests && typeof item.pullRequests === 'object' && !Array.isArray(item.pullRequests));
      require(Object.entries(item.pullRequests).every(([r,b]) => item.repositories.includes(r) && branch(b)));
    }
    if (item.ssh !== undefined) {
      const s = item.ssh;
      require(s && Object.keys(s).sort().join(',') === 'host,hostKey,port,user');
      require(typeof s.host === 'string' && s.host.length <= 253 && /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(s.host));
      require(Number.isInteger(s.port) && s.port >= 1 && s.port <= 65535 && s.user === 'git');
      require(typeof s.hostKey === 'string' && /^ssh-ed25519 [A-Za-z0-9+/]+={0,2}$/.test(s.hostKey));
      const encoded = s.hostKey.split(' ')[1], raw = Buffer.from(encoded, 'base64');
      require(raw.length === 51 && raw.toString('base64') === encoded && raw.subarray(0,19).toString('hex') === '0000000b7373682d6564323535313900000020');
    }
    const name = 'issuer-' + item.id, token = await read(name, 4096);
    require(/^[A-Za-z0-9_-]{20,4096}$/.test(token)); tokens.push([name, token]);
    result.push({...item, enabled: item.enabled ?? false, tokenFile: '/run/relay-gitea/private/' + name});
  }
  require(Buffer.byteLength(JSON.stringify(result)) <= 32768);
  await mkdir('/output/private', {recursive: true, mode: 0o700}); await chmod('/output/private', 0o700);
  for (const [name, token] of tokens) await write(name, token);
  await write('deployments.json', JSON.stringify(result) + '\n');
} catch {
  console.error('Relay Gitea private-file setup failed; input values withheld.');
  process.exitCode = 1;
}
