// kit/tools.mjs - the four workspace tools of the agentic Claude tasks, ONE implementation shared by both arms
// (the direct arm's MCP server and the router arm's harness). Paths are confined to the workspace root.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

export const SYSTEM = 'You are a coding agent working in a small JavaScript repository. Use the provided tools to inspect and modify files and to run the tests. Keep working until the task is complete and the tests pass, then reply with a one-paragraph summary of what you changed.';
export const PROMPT = 'Complete the task described in TASK.md in the workspace.';
export const TOOLS = [
  { name: 'list_files', description: 'List every file in the workspace (recursively), one path per line.', parameters: { type: 'object', properties: {}, additionalProperties: false } },
  { name: 'read_file', description: 'Return the full text of one file in the workspace.', parameters: { type: 'object', properties: { path: { type: 'string', description: 'Path relative to the workspace root' } }, required: ['path'], additionalProperties: false } },
  { name: 'write_file', description: 'Create or overwrite one file in the workspace with the given full content.', parameters: { type: 'object', properties: { path: { type: 'string', description: 'Path relative to the workspace root' }, content: { type: 'string', description: 'The complete new file content' } }, required: ['path', 'content'], additionalProperties: false } },
  { name: 'run_tests', description: 'Run the visible test suite (node --test on every tests/*.test.mjs) and return its exit code and output.', parameters: { type: 'object', properties: {}, additionalProperties: false } },
];

const within = (root, p) => {
  const r = path.resolve(root, p);
  if (r !== root && !r.startsWith(root + path.sep)) throw new Error('path outside the workspace');
  return r;
};
const walk = (root, d = root) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) =>
  e.isDirectory() ? (e.name === 'node_modules' || e.name === '.git' ? [] : walk(root, path.join(d, e.name))) : [path.relative(root, path.join(d, e.name))]);

// A task may restrict the tools with a `.tools.json` in its workspace: { "hidden": ["tests/"], "bail": true }.
// Hidden paths cannot be read or written; .tools.json cannot be written; bail reports only the first failing test.
const options = (root) => { try { return JSON.parse(fs.readFileSync(path.join(root, '.tools.json'), 'utf8')); } catch { return {}; } };
const guarded = (root, p, opts) => {
  const rel = path.relative(root, within(root, p)).split(path.sep).join('/');
  if (rel === '.tools.json' || (opts.hidden ?? []).some((h) => rel === h.replace(/\/$/, '') || rel.startsWith(h))) throw new Error(`${p} is not accessible`);
  return within(root, p);
};
/** The first failing test of a TAP stream, plus the pass count (the bail option). */
const firstFailure = (tap) => {
  const lines = tap.split('\n');
  const pass = (tap.match(/^# pass (\d+)/m) ?? [])[1], tests = (tap.match(/^# tests (\d+)/m) ?? [])[1];
  const i = lines.findIndex((l) => /^not ok /.test(l));
  if (i < 0) return `all ${tests} tests pass`;
  const block = [lines[i]];
  for (let k = i + 1; k < lines.length && !/^(not ok|ok) /.test(lines[k]) && !/^# (tests|pass)/.test(lines[k]); k++) block.push(lines[k]);
  return `${pass} of ${tests} tests pass. First failure:\n` + block.join('\n').slice(0, 2000);
};

/** Executes one tool call. Returns { ok, output }; an unknown tool or invalid arguments is ok:false (a rejected call). */
export function execute(root, name, args) {
  const opts = options(root);
  try {
    if (typeof args === 'string') args = args ? JSON.parse(args) : {};
    if (!args || typeof args !== 'object') throw new Error('arguments must be an object');
    switch (name) {
      case 'list_files':
        return { ok: true, output: walk(root).sort().join('\n') };
      case 'read_file':
        if (typeof args.path !== 'string') throw new Error('path is required');
        return { ok: true, output: fs.readFileSync(guarded(root, args.path, opts), 'utf8').slice(0, 20000) };
      case 'write_file': {
        if (typeof args.path !== 'string' || typeof args.content !== 'string') throw new Error('path and content are required');
        const f = guarded(root, args.path, opts);
        fs.mkdirSync(path.dirname(f), { recursive: true });
        fs.writeFileSync(f, args.content);
        return { ok: true, output: `wrote ${args.content.length} characters to ${args.path}` };
      }
      case 'run_tests': {
        // node 22 takes test FILES (a bare directory argument is loaded as a module and fails), so list them
        const files = fs.readdirSync(path.join(root, 'tests')).filter((f) => f.endsWith('.test.mjs')).map((f) => path.join('tests', f));
        const r = spawnSync(process.execPath, ['--test', ...files], { cwd: root, encoding: 'utf8', timeout: 30000 });
        const all = (r.stdout || '') + (r.stderr || '');
        return { ok: true, output: `exit code ${r.status}\n` + (opts.bail ? firstFailure(all) : all.slice(-3000)) };
      }
      default:
        throw new Error(`unknown tool ${name}`);
    }
  } catch (e) {
    return { ok: false, output: `error: ${e.message}` };
  }
}
