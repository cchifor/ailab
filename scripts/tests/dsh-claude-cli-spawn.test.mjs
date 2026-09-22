/**
 * Behavioural test for the one failure that takes the whole harness down.
 *
 * WHY THIS IS NOT A STATIC GATE. Every other check on this provider
 * (test_dsh_claude_cli_provider.py) reads the manifests. This one cannot: the defect it covers is
 * a missing event listener, and whether an unhandled 'error' event terminates the process is a
 * property of Node, not of the text. Both PR reviewers raised it twice each, and it reproduced on
 * the first try -- with only the stdin listener attached, a spawn failure printed an uncaught
 * ENOENT and exited. On a 1-replica Recreate Deployment that is the web UI going down, when the
 * correct outcome is one failed turn.
 *
 * TWO REAL PATHS, both measured in the dsh pod on 2026-09-22:
 *   1. the wrapper missing or not executable -- a partial seed-settings state;
 *   2. the configured `cwd` absent. This one is subtle and was INTRODUCED by the fix for an
 *      earlier review finding: spawn applies cwd BEFORE the wrapper executes, so the `mkdir` in
 *      claude-cli.sh can never rescue its own working directory. The agent shares this uid and
 *      can remove that directory.
 * EAGAIN/ENOMEM under the concurrent-children fan-out ADR 0030 records as unbounded is the third,
 * and is not reproducible on demand.
 *
 * THE STUB BASE CLASS IS FOR THIS TEST ONLY. claude-cli-provider.mjs carries a warning against
 * vendoring a stub of @deepseek-ai/dsh-llm -- that is about the runtime profile, where a stub
 * silently drops inherited methods. Here nothing inherited is exercised: the test drives
 * `stream()` directly, which is where the listeners live.
 *
 * Run:  node --test scripts/tests/dsh-claude-cli-spawn.test.mjs
 */
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, copyFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DSH = join(HERE, '..', '..', 'kubernetes', 'apps', 'apps', 'dsh');
const FILES = ['claude-cli-provider.mjs', 'claude-cli-translate.mjs', 'claude-cli-images.mjs'];

let root;
let mod;

before(async () => {
  root = await mkdtemp(join(tmpdir(), 'dsh-claude-cli-test-'));
  const stub = join(root, 'node_modules', '@deepseek-ai', 'dsh-llm');
  await mkdir(stub, { recursive: true });
  await writeFile(join(stub, 'package.json'),
    JSON.stringify({ name: '@deepseek-ai/dsh-llm', version: '0.0.0-stub', type: 'module', main: 'index.js' }));
  await writeFile(join(stub, 'index.js'),
    'export class LlmAdapter {}\n' +
    'export class LlmError extends Error { constructor(m, code) { super(m); this.code = code; } }\n');
  for (const f of FILES) await copyFile(join(DSH, f), join(root, f));
  mod = await import(pathToFileURL(join(root, 'claude-cli-provider.mjs')).href);
});

after(async () => { if (root) await rm(root, { recursive: true, force: true }); });

/** Drive stream() to completion and report how it ended, without letting a throw escape. */
async function runTurn(config) {
  const options = mod.resolveOptions(config);
  const adapter = new mod.ClaudeCliAdapter(options, {});
  const chunks = [];
  try {
    for await (const c of adapter.stream({ model: 'claude-sonnet-5', messages: [{ role: 'user', content: [{ type: 'text', text: 'hi' }] }] })) {
      chunks.push(c);
    }
    return { ok: true, chunks };
  } catch (err) {
    return { ok: false, error: err, chunks };
  }
}

/**
 * The assertion that matters is not the message but the SURVIVAL: an unhandled 'error' event is a
 * process-level throw that no try/catch in the generator can intercept, so if the listener is
 * missing this test crashes the runner rather than failing.
 */
test('a missing wrapper fails the turn instead of terminating the process', async () => {
  const uncaught = [];
  const onUncaught = (e) => uncaught.push(e);
  process.on('uncaughtException', onUncaught);
  try {
    const r = await runTurn({ command: join(root, 'no-such-wrapper'), timeoutMs: 5000 });
    assert.equal(r.ok, false, 'the turn must fail');
    assert.match(r.error.message, /could not be started/,
      'the error must name the cause; "exited null" names neither the cause nor the fix');
    assert.match(r.error.message, /ENOENT/);
  } finally {
    process.off('uncaughtException', onUncaught);
  }
  assert.deepEqual(uncaught, [], 'a spawn failure must not reach uncaughtException');
});

test('a missing working directory fails the turn instead of terminating the process', async () => {
  // spawn applies cwd before the wrapper runs, so claude-cli.sh cannot create its own cwd.
  const uncaught = [];
  const onUncaught = (e) => uncaught.push(e);
  process.on('uncaughtException', onUncaught);
  try {
    const r = await runTurn({ command: process.execPath, cwd: join(root, 'no-such-cwd'), timeoutMs: 5000 });
    assert.equal(r.ok, false);
    assert.match(r.error.message, /could not be started/);
    assert.match(r.error.message, /no-such-cwd/, 'the message must name the directory that is missing');
  } finally {
    process.off('uncaughtException', onUncaught);
  }
  assert.deepEqual(uncaught, []);
});

test('a failed spawn does not hang the turn', async () => {
  // A child that never starts may never emit 'close'. Awaiting that alone would trade a crash for
  // a wedged turn, which on a single-replica deployment is barely an improvement.
  const started = Date.now();
  const r = await runTurn({ command: join(root, 'no-such-wrapper'), timeoutMs: 600000 });
  assert.equal(r.ok, false);
  assert.ok(Date.now() - started < 5000,
    'the turn must settle immediately, not wait out the idle timeout');
});

test('a wrapper that runs and exits non-zero is reported as an exit, not a spawn failure', async () => {
  // The degraded path claude-cli.sh actually takes (exit 78 when the pinned binary is missing)
  // must stay distinguishable from a child that never started, or "the Job did not finish" and
  // "seed-settings did not finish" read identically to whoever is on the end of it.
  //
  // process.execPath, not /bin/sh: this suite has to run on the maintainer's Windows host as well
  // as in CI, and a POSIX path there is itself a spawn failure -- which is how this test first
  // passed for the wrong reason. Node given the adapter's argv starts, rejects the flags and
  // exits non-zero, which is exactly the shape under test.
  const r = await runTurn({ command: process.execPath, timeoutMs: 5000 });
  assert.equal(r.ok, false);
  assert.doesNotMatch(r.error.message, /could not be started/,
    'a child that ran and exited is not a spawn failure');
  assert.match(r.error.message, /exited/, 'it should report the exit');
});
