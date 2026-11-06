// tasks/graders.mjs - graders by name. A grader gets the task's grader spec and the run's evidence ({ texts, calls,
// workspace, ctx }) and returns { pass, detail }. Workspace graders run their tests in docker, never on a host's own
// toolchain, so a task's tests cannot touch the host.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const docker = (dir, image, cmd) => spawnSync('docker', ['run', '--rm', '-v', `${dir}:/w`, '-w', '/w', image, 'sh', '-c', cmd],
  { encoding: 'utf8', timeout: 180_000, env: { ...process.env, MSYS_NO_PATHCONV: '1' } });
const untouched = (dir, paths) => spawnSync('git', ['diff', '--quiet', 'HEAD', '--', ...paths], { cwd: dir }).status === 0;
const winPath = (p) => (process.platform === 'win32' ? spawnSync('cygpath', ['-w', p], { encoding: 'utf8' }).stdout.trim() || p : p);

export const GRADERS = {
  'regex-equals': ({ pattern, expect }, { texts }) => {
    const m = new RegExp(pattern).exec(texts.at(-1) ?? '');
    return { pass: !!m && m[1] === expect, detail: m ? m[1] : (texts.at(-1) ?? '').slice(0, 40) };
  },
  'tool-call': ({ tool, args }, { calls }) => {
    const c = calls[0];
    if (!c) return { pass: false, detail: 'no tool call' };
    const name = c.name.replace(/^mcp__\w+__/, '');
    const a = typeof c.arguments === 'string' ? JSON.parse(c.arguments || '{}') : c.arguments;
    const ok = name === tool && Object.entries(args).every(([k, v]) => String(a[k]).toUpperCase() === String(v).toUpperCase());
    return { pass: ok, detail: `${name} ${JSON.stringify(a)}` };
  },
  'secret-sum': (_spec, { texts }) => {
    const s = /SUM\s*=\s*(\d+)/.exec(texts[0] ?? ''), n = /NUM\s*=\s*(\d{6}|UNKNOWN)/.exec(texts[1] ?? '');
    const num = n?.[1];
    return { pass: !!(s && num && num !== 'UNKNOWN' && [...num].reduce((a, d) => a + Number(d), 0) === Number(s[1])), detail: `${s?.[1]}/${num}` };
  },
  'python-unittest': ({ module, protect }, { workspace }) => {
    const r = docker(winPath(workspace), 'python:3.12-slim', `python -m unittest -q ${module} 2>&1 | tail -3`);
    const ok = r.status === 0 && untouched(workspace, protect);
    return { pass: ok, detail: (r.stdout || '').trim().split('\n').at(-1) };
  },
  'node-hidden-tests': ({ protect }, { workspace, task, fixtures }) => {
    const copy = fs.mkdtempSync(path.join(path.dirname(workspace), 'grade-'));
    fs.cpSync(workspace, copy, { recursive: true, filter: (p) => !p.includes(`${path.sep}.git`) });
    if (task.hidden) fs.cpSync(path.join(fixtures, task.hidden), path.join(copy, 'hidden'), { recursive: true });
    const r = docker(winPath(copy), 'node:22-slim', `node --test tests/*.test.mjs ${task.hidden ? 'hidden/*.test.mjs' : ''} 2>&1 | grep -E "^# (pass|fail)"`);
    fs.rmSync(copy, { recursive: true, force: true });
    const fail = /# fail (\d+)/.exec(r.stdout || ''), pass = /# pass (\d+)/.exec(r.stdout || '');
    return { pass: !!fail && fail[1] === '0' && untouched(workspace, protect), detail: `${pass?.[1]} pass, ${fail?.[1]} fail` };
  },
};

export function grade(task, evidence) {
  const g = GRADERS[task.grader.name];
  if (!g) throw new Error(`unknown grader ${task.grader.name} for task ${task.id}`);
  return g(task.grader, { ...evidence, task });
}
