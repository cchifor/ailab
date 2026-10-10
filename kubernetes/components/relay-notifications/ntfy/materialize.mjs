// Keep account names, roles, topic and ACLs in reviewed code. Input is limited to
// password verifiers and opaque tokens; malformed values never reach ntfy's
// parser, whose validation errors can include the supplied credential.
import { constants } from 'node:fs';
import { open, rename, rm } from 'node:fs/promises';

async function bounded(path, max) {
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

try {
  const base = await bounded('/base/server.yml', 65536);
  // This component owns ntfy's declarative provisioning set. Do not silently
  // override another component's accounts, or remove them during reconciliation.
  if (/^\s*auth-(users|access|tokens)\s*:/m.test(base)) throw new Error('already provisioned');
  if (!/^auth-default-access: "deny-all"$/m.test(base)) throw new Error('deny-all required');
  const users = [], tokens = [];
  const seen = new Set();
  for (const role of ['publisher', 'subscriber']) {
    const hash = await bounded(`/input/${role}-hash`, 128);
    const token = await bounded(`/input/${role}-token`, 128);
    if (!/^\$2[aby]\$(10|11|12)\$[./A-Za-z0-9]{53}$/.test(hash)
        || !/^tk_[A-Za-z0-9]{29}$/.test(token) || seen.has(token)) throw new Error('invalid input');
    seen.add(token);
    users.push(`relay-${role}:${hash}:user`);
    tokens.push(`relay-${role}:${token}:relay-${role}`);
  }
  const value = `${base}\nauth-users: ${JSON.stringify(users)}\nauth-access: ${JSON.stringify([
    'relay-publisher:*:deny-all', 'relay-publisher:relay-actions:write-only',
    'relay-subscriber:*:deny-all', 'relay-subscriber:relay-actions:read-only',
  ])}\nauth-tokens: ${JSON.stringify(tokens)}\n`;
  await rm('/output/server.yml.new', { force: true });
  const f = await open('/output/server.yml.new', 'wx', 0o600);
  try { await f.writeFile(value); await f.sync(); } finally { await f.close(); }
  await rename('/output/server.yml.new', '/output/server.yml');
} catch {
  console.error('ntfy Relay configuration failed; inspect Secret shape, base configuration and volume permissions.');
  process.exitCode = 1;
}
