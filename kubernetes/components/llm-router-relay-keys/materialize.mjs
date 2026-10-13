import { constants } from 'node:fs';
import { open, mkdir, chmod, rename, rm } from 'node:fs/promises';

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

async function write(value) {
  const path = '/output/trust.json';
  await rm(`${path}.new`, { force: true });
  const file = await open(`${path}.new`, 'wx', 0o600);
  try { await file.writeFile(value); await file.sync(); } finally { await file.close(); }
  await rename(`${path}.new`, path);
}

try {
  const text = await read('trust.json', 65536);
  const value = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.version !== 1 ||
      !Array.isArray(value.issuers) || value.issuers.length < 1 || value.issuers.length > 100)
    throw new Error('invalid trust schema');
  // The router performs the authoritative schema, key-type and duplicate check
  // at startup. This init gate ensures the mounted object is bounded JSON and
  // cannot be replaced through a symlink or partial write.
  await mkdir('/output', { recursive: true, mode: 0o700 });
  await chmod('/output', 0o700);
  await write(JSON.stringify(value) + '\n');
} catch {
  console.error('llm-router Relay trust setup failed; input values withheld.');
  process.exitCode = 1;
}
