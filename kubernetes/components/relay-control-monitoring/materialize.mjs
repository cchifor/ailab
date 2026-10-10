// Read kubelet-projected verifier metadata; Relay never receives the scrape token.
import { constants } from 'node:fs';
import { chmod, mkdir, open, rename, rm } from 'node:fs/promises';

async function bounded(name, max) {
  const file = await open(`/input/${name}`, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > max) throw Error();
    const buffer = Buffer.alloc(max + 1);
    const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
    if (bytesRead > max) throw Error();
    return buffer.subarray(0, bytesRead).toString('utf8').trim();
  } finally { await file.close(); }
}
try {
  const tenantId = await bounded('tenant-id', 128);
  const tokenSha256 = await bounded('token-sha256', 128);
  const expiresAt = await bounded('expires-at', 128);
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(tenantId) ||
      !/^[a-f0-9]{64}$/.test(tokenSha256) ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(expiresAt) ||
      !Number.isFinite(Date.parse(expiresAt))) throw Error();
  // Reject dates that Date.parse normalizes, such as February 30.
  if (new Date(expiresAt).toISOString().slice(0, 19) !== expiresAt.slice(0, 19)) throw Error();
  await mkdir('/output/private', { recursive: true, mode: 0o700 });
  await chmod('/output/private', 0o700);
  const target = '/output/private/config.json';
  await rm(target + '.new', { force: true });
  const file = await open(target + '.new', 'wx', 0o600);
  try {
    await file.writeFile(JSON.stringify({version: 1, tenantId, tokenSha256, expiresAt}) + '\n');
    await file.sync();
  } finally { await file.close(); }
  await rename(target + '.new', target);
} catch {
  // Never print raw exceptions, input values or parser diagnostics.
  console.error('Relay control metrics configuration is unavailable or invalid.');
  process.exitCode = 1;
}
