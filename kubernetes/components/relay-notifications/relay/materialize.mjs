// Projected Secret files are symlinks owned by root. Copy only validated input
// into this execution user's private tmpfs before starting Relay. Never log input.
import { constants } from 'node:fs';
import { chmod, mkdir, open, rename, rm } from 'node:fs/promises';

async function bounded(path, max) {
  // Deliberately follow the kubelet's projected-volume symlink on INPUT only.
  const f = await open(path, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    const s = await f.stat();
    if (!s.isFile() || s.size > max) throw new Error('invalid input');
    const bytes = Buffer.alloc(max + 1);
    const { bytesRead } = await f.read(bytes, 0, bytes.length, 0);
    if (bytesRead > max) throw new Error('oversized input');
    return bytes.subarray(0, bytesRead).toString('utf8').trim();
  } finally { await f.close(); }
}

async function write(name, value) {
  const path = `/output/private/${name}`;
  // A killed init container can leave a partial temporary file. No application
  // container runs until this single init writer succeeds.
  await rm(`${path}.new`, { force: true });
  const f = await open(`${path}.new`, 'wx', 0o600);
  try { await f.writeFile(value); await f.sync(); } finally { await f.close(); }
  await rename(`${path}.new`, path);
}

try {
  const tenant = await bounded('/input/tenant-id', 128);
  const token = await bounded('/input/publisher-token', 128);
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(tenant)
      || !/^tk_[A-Za-z0-9]{29}$/.test(token)) throw new Error('invalid input');
  await mkdir('/output/private', { mode: 0o700, recursive: true });
  await chmod('/output/private', 0o700);
  await write('publisher-token', token);
  await write('destinations.json', JSON.stringify([{
    tenantId: tenant,
    url: 'https://ntfy.chifor.me/relay-actions',
    tokenFile: '/run/relay-notifications/private/publisher-token',
  }]) + '\n');
} catch {
  // Node exceptions can contain file contents; never surface raw errors here.
  console.error('Relay notification file setup failed; inspect Secret shape and volume permissions.');
  process.exitCode = 1;
}
