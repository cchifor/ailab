import { afterEach, expect, it, vi } from 'vitest';
import { createHash, generateKeyPairSync, randomBytes, randomUUID, sign } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createRuntime } from '../packages/runtime/cordis.js';
import { createHttpServer } from '../packages/kernel/index.js';
import { AUDIENCE, binding, digest, Input, PATH } from '../packages/plugins/relay-keys/contracts.js';

// Cross-repository qualification: real B Controller, signed HTTP, service and
// SQLite. Only issuer authority and terminal/drain observations are synthetic.
// No provider requests, Pi, native drain or live authority are qualified here.
const SOURCE = '0bc835ab72fb9ea03b2b3ea51bba7a9789a5e171';
const HELPER_SHA = 'beff49f0285479257c5cf0bc92a183c7668acc0a494bcbbdcb3ab08554708bf2';
const cleanup: Array<() => Promise<unknown>> = [];
afterEach(async () => {
  for (const close of cleanup.splice(0).reverse()) await close();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

async function fixture(name: string, corruptArm = false) {
  const helperPath = process.env.B_FENCE_HELPER;
  if (!helperPath) throw Error('B_FENCE_HELPER must name the frozen actual helper');
  expect(digest(await readFile(helperPath))).toBe(HELPER_SHA);
  const helper = createRequire(import.meta.url)(resolve(helperPath));
  const dir = await mkdtemp(join(tmpdir(), 'b-fence-protocol-'));
  cleanup.push(() => rm(dir, { recursive: true, force: true }));
  const signing = generateKeyPairSync('ed25519');
  const tenant = randomUUID();
  const trustPath = join(dir, 'trust.json');
  await writeFile(trustPath, JSON.stringify({ version: 1, issuers: [{
    issuer: 'b-controller-fixture',
    keys: [{ kid: 'admin', roles: ['admin'], publicKey: signing.publicKey.export({ type: 'spki', format: 'pem' }) }],
    tenants: [{ tenant, namespace: 'default', routes: ['canary-pi-claude'], access: ['subscription'], maxLifetimeSeconds: 3600, maxKeys: 2 }],
  }] }), { mode: 0o600 });
  vi.stubEnv('ROUTER_ADMIN_TOKEN', 'synthetic-test-admin');
  vi.stubEnv('RELAY_KEYS_TRUST_FILE', trustPath);
  const runtime = await createRuntime({ database: join(dir, 'router.sqlite'), quiet: true, noStartupProbe: true });
  cleanup.push(() => runtime.close());
  const secret = await runtime.secrets.create('sk-ant-oat01-fixture', 'synthetic fixture only');
  const cfg = runtime.config();
  Object.assign(cfg.accounts[0], {
    provider: 'claude', access: 'subscription', credentialOwner: 'llm-router', secretRef: secret.ref, allowedModels: ['haiku'],
    capabilities: { tools: true, vision: false, structuredOutput: false, maxOutputTokens: true },
  });
  Object.assign(cfg.policies[0], { alias: 'canary-pi-claude', placement: 'conversation', access: ['subscription'], maxAttempts: 1, fallbackAliases: [] });
  cfg.policies[0].deployments[0].model = 'haiku';
  await runtime.updateConfig(cfg, cfg.revision, 'test');
  const providerGeneration = runtime.ctx.routerProviders.generation('claude');
  expect(Number.isSafeInteger(providerGeneration)).toBe(true);
  const lease = runtime.ctx.routerProviders.acquire('claude');
  expect(lease.generation).toBe(providerGeneration);
  const providerOpen = vi.spyOn(lease.provider, 'open');
  lease.release();
  const app = await createHttpServer(runtime, { keys: [] });
  cleanup.push(() => app.close());
  const origin = await app.listen({ host: '127.0.0.1', port: 0 });
  const wire: any[] = [];
  const snapshots: any[] = [];
  const state: any = { operations: [], phases: [] };
  const mutations = new Map<string, { raw: string; bodySha256: string; requestDigest: string }>();

  async function http(raw: string, signal = AbortSignal.timeout(15000)) {
    const body = Input.parse(JSON.parse(raw));
    const clock = Math.floor(Date.now() / 1000);
    const header = Buffer.from(JSON.stringify({ alg: 'EdDSA', typ: 'JWT', kid: 'admin' })).toString('base64url');
    const claims = Buffer.from(JSON.stringify({ iss: 'b-controller-fixture', aud: AUDIENCE, sub: 'synthetic-owner', tenant, role: 'admin',
      action: body.action, operationId: body.operationId, iat: clock, exp: clock + 60, nonce: randomBytes(24).toString('base64url'), binding: binding(raw),
    })).toString('base64url');
    const jwt = `${header}.${claims}.${sign(null, Buffer.from(`${header}.${claims}`), signing.privateKey).toString('base64url')}`;
    const response = await fetch(origin + PATH, { method: 'POST', redirect: 'error',
      headers: { authorization: 'Bearer ' + jwt, 'content-type': 'application/json' }, body: raw, signal });
    const value = await response.json() as any;
    // JWT, synthetic secret and one-time key token are never written to evidence.
    const { token: _token, ...sanitized } = value;
    wire.push({ body, raw, bodySha256: digest(raw), assertionBinding: binding(raw), status: response.status, response: sanitized });
    expect(response.status, `${body.action}: ${JSON.stringify(sanitized)}`).toBe(200);
    return value;
  }

  const createId = randomUUID();
  const created = await http(JSON.stringify({ version: 1, action: 'create', operationId: createId, name: 'b-controller-protocol', routes: ['canary-pi-claude'], access: ['subscription'], lifetimeSeconds: 3600 }));
  expect(created.token).toMatch(/^lrk_/);
  await http(JSON.stringify({ version: 1, action: 'delivery-ack', operationId: randomUUID(), targetOperationId: createId, keyId: created.key.id }));

  let lostReply = false;
  async function dispatch(method: string, request: any) {
    const body = JSON.parse(request.raw);
    expect(request.bodySha256).toBe(digest(request.raw));
    expect(request.requestDigest).toBe(binding(request.raw));
    expect(request.bodySha256).not.toBe(request.requestDigest);
    if (['arm', 'advance', 'close'].includes(method)) {
      // Inspect the saved clone, not mutable Controller state: intent precedes
      // signing/HTTP, including the request whose committed response is lost.
      const saved = snapshots.at(-1)?.operations.at(-1);
      expect(saved).toMatchObject({ raw: request.raw, bodySha256: request.bodySha256, requestDigest: request.requestDigest, status: 'sending' });
      expect(mutations.has(body.operationId)).toBe(false);
      mutations.set(body.operationId, { raw: request.raw, bodySha256: request.bodySha256, requestDigest: request.requestDigest });
    }
    const actual = await http(request.raw, request.signal);
    if (['arm', 'advance', 'close', 'readOperation'].includes(method)) {
      const original = mutations.get(method === 'readOperation' ? body.targetOperationId : body.operationId)!;
      expect(original).toBeDefined();
      expect(actual.requestDigest).toBe(original.bodySha256);
      expect(actual.requestDigest).not.toBe(original.requestDigest);
      expect(actual.operationId).toBe(JSON.parse(original.raw).operationId);
      if (method === 'readOperation') {
        expect(actual.operationId).not.toBe(body.operationId);
        expect(actual.requestDigest).not.toBe(request.bodySha256);
        expect(actual.action).toBe('attempt-fence-advance');
      }
    }
    if (!corruptArm && method === 'advance' && !lostReply) {
      lostReply = true;
      throw Error('Synthetic lost reply AFTER actual HTTP commit');
    }
    const delivered = corruptArm && method === 'arm' ? { ...actual, requestDigest: request.requestDigest } : actual;
    return { routerSource: SOURCE, observedAt: Date.now(), projectionSha256: helper.digest(delivered), response: delivered };
  }
  const transport = Object.fromEntries(['inspect', 'arm', 'advance', 'close', 'readOperation'].map(method => [method, (request: any) => dispatch(method, request)]));
  const deadline = Date.now() + 120000;
  const controller = new helper.Controller({ transport, source: SOURCE, now: Date.now, randomUUID,
    save: () => snapshots.push(structuredClone(state)), state,
    check: () => { if (Date.now() >= deadline) throw Error('fixture deadline'); }, deadline,
    scope: { keyId: created.key.id, principalId: 'key:' + created.key.id, namespace: 'default', tenantId: tenant, holderId: randomUUID(), journalId: randomUUID() },
    selection: { route: 'canary-pi-claude', accountId: cfg.accounts[0].id, deploymentId: cfg.policies[0].deployments[0].id, model: 'haiku', provider: 'claude', providerGeneration },
  });
  const terminal = () => ({ observedAt: Date.now(), syntheticTerminalDigest: '1'.repeat(64) });
  const run = (effort: boolean) => ({ runId: randomUUID(), effort, deadline, lastSnapshot: { totalAttempts: 0 } });
  const saveEvidence = async () => {
    expect(providerOpen).not.toHaveBeenCalled();
    const evidence = process.env.B_FENCE_EVIDENCE;
    if (!evidence) throw Error('B_FENCE_EVIDENCE required');
    await mkdir(evidence, { recursive: true });
    const output = JSON.stringify({ source: SOURCE, helperSha256: HELPER_SHA, name, providerGeneration, providerOpens: providerOpen.mock.calls.length, wire, snapshots, state }, null, 2) + '\n';
    expect(output).not.toContain(created.token);
    expect(output).not.toContain('Bearer ');
    await writeFile(join(evidence, name + '.json'), output, { flag: 'wx', mode: 0o600 });
  };
  return { controller, helper, state, wire, mutations, snapshots, terminal, run, saveEvidence };
}

it('real B Controller accepts signed HTTP body-SHA receipts and recovers a lost mutation reply without resend', async () => {
  const f = await fixture('positive');
  await f.controller.initialize();
  for (const effort of [false, true]) {
    const run = f.run(effort);
    const rows = f.helper.ROWS[effort ? 'effort' : 'lifecycle'];
    for (const [row] of rows) {
      await f.controller.row(run, row, f.terminal());
      await f.controller.settle(run, 0, f.terminal());
    }
    await f.controller.close(f.terminal());
    expect((await f.controller.inspect()).fence.closedAt).toBeGreaterThan(0);
  }
  expect(f.state.phases).toHaveLength(2);
  expect(f.state.phases.map((phase: any) => phase.rows.length)).toEqual([8, 2]);
  expect(f.state.phases.every((phase: any) => phase.closed && phase.rows.every((row: any) => row.settled && row.attempts === 0))).toBe(true);
  expect(f.state.operations).toHaveLength(12); // 2 arms + 8 advances + 2 closes.
  expect(f.state.operations.every((operation: any) => operation.status === 'committed')).toBe(true);
  expect(f.mutations.size).toBe(12);
  expect(f.wire.filter((entry: any) => entry.body.action === 'operation')).toHaveLength(1);
  expect(f.snapshots.some((state: any) => state.operations.some((operation: any) => operation.status === 'uncertain'))).toBe(true);
  const [firstArm, secondArm] = f.state.operations.filter((operation: any) => JSON.parse(operation.raw).action === 'attempt-fence-arm');
  expect(JSON.parse(secondArm.raw).predecessorFenceId).toBe(firstArm.receipt.fence.plan.fenceId);
  expect(JSON.parse(secondArm.raw).expectedFenceRevision).toBe(9);
  expect(f.state.current.revision).toBe(12);
  await f.saveEvidence();
}, 30000);

it('real B Controller refuses JWT binding substituted into an actual HTTP receipt and never replays the arm', async () => {
  const f = await fixture('wrong-receipt-digest', true);
  await f.controller.initialize();
  const run = f.run(false);
  await expect(f.controller.row(run, 'ready', null)).rejects.toThrow('mutation receipt/digest/count differs');
  expect(f.state.operations).toHaveLength(1);
  expect(f.state.operations[0].status).toBe('invalid-receipt');
  expect(f.controller.uncertain).toBe(true);
  const before = f.wire.length;
  await expect(f.controller.row(run, 'ready', null)).rejects.toThrow('resolved operation missing');
  expect(f.wire).toHaveLength(before);
  expect(f.wire.filter((entry: any) => entry.body.action === 'attempt-fence-arm')).toHaveLength(1);
  expect(f.wire.filter((entry: any) => entry.body.action === 'operation')).toHaveLength(0);
  // The wire's unmodified receipt remains independently readable and body-bound.
  const arm = f.wire.find((entry: any) => entry.body.action === 'attempt-fence-arm');
  expect(arm.response.requestDigest).toBe(arm.bodySha256);
  expect(arm.response.requestDigest).not.toBe(arm.assertionBinding);
  await f.saveEvidence();
}, 30000);
