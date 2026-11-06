// core/context.mjs - the run context every provider gets: where things run, how to reach them, and the cleanup list.
//
// Hosts: the gate runs on the operator workstation (kubectl context admin@ai, ssh to the worker). The direct arms that
// need a host's own logins (the ChatGPT seat, the Claude seat) and the LAN model servers run on the worker; the router
// arms that need a route the worker key does not carry run on the workstation (docker) or inside the router pod.
//
// Secrets: never printed or written. The worker reads its own logins in place. The router admin bearer is read from its
// Kubernetes Secret into a child process's environment only (the runbook's operator retrieval), or used inside the
// router pod from the pod's own environment.
//
// Recordings: everything recorded (request bodies, streams) lives under one temp directory per gate run, on the worker
// and on the workstation, and is deleted at teardown, whatever the outcome.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const HERE = path.dirname(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')));

export function createContext(opts) {
  const id = `rab-${new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)}-${process.pid}`;
  // forward slashes: Git for Windows' tar (first on PATH there) misreads a C:\ path; Windows' own tools accept both
  const local = fs.mkdtempSync(path.join(os.tmpdir(), `${id}-`)).split(path.sep).join('/');
  const ctx = {
    id, opts, local, worker: opts.worker ?? 'c4@192.168.0.8', remote: `/tmp/${id}`, kube: opts.kubeContext ?? 'admin@ai',
    log: (...a) => console.error(`[${new Date().toISOString().slice(11, 19)}]`, ...a),
    /** Runs a command on the worker over ssh (stdin closed); returns { status, stdout, stderr }. */
    ssh(cmd, { timeout = 900_000 } = {}) {
      return spawnSync('ssh', ['-o', 'BatchMode=yes', ctx.worker, cmd], { encoding: 'utf8', timeout, input: '' });
    },
    /** Copies a local directory's content to a path on the worker (tar over ssh: keeps dotfiles and modes). */
    push(localDir, remoteDir) {
      const tar = spawnSync('tar', ['-C', localDir, '-cf', '-', '.'], { maxBuffer: 1 << 28 });
      return spawnSync('ssh', ['-o', 'BatchMode=yes', ctx.worker, `mkdir -p ${remoteDir} && tar -C ${remoteDir} -xf -`], { input: tar.stdout, encoding: 'utf8' });
    },
    /** Runs a node script inside the router pod with its own admin bearer (stdin = the script). */
    inPod(scriptPath, args = [], { timeout = 600_000 } = {}) {
      const kc = ['--context', ctx.kube, '-n', 'llm-router'];
      const dest = `/tmp/${ctx.id}-${path.basename(scriptPath)}`;
      spawnSync('kubectl', [...kc, 'exec', '-i', 'deploy/llm-router', '-c', 'router', '--', 'sh', '-c', `cat > ${dest}`], { input: fs.readFileSync(scriptPath), env: { ...process.env, MSYS_NO_PATHCONV: '1' } });
      const r = spawnSync('kubectl', [...kc, 'exec', 'deploy/llm-router', '-c', 'router', '--', 'node', dest, ...args], { encoding: 'utf8', timeout, env: { ...process.env, MSYS_NO_PATHCONV: '1' } });
      spawnSync('kubectl', [...kc, 'exec', 'deploy/llm-router', '-c', 'router', '--', 'rm', '-f', dest], { env: { ...process.env, MSYS_NO_PATHCONV: '1' } });
      return r;
    },
    /** A fresh local directory for one run (forward slashes, see above). */
    localDir(name) {
      const d = `${local}/${name}`;
      fs.mkdirSync(d, { recursive: true });
      return d;
    },
    /** The router admin bearer, for one child process's environment only. */
    adminToken() {
      const r = spawnSync('kubectl', ['--context', ctx.kube, '-n', 'llm-router', 'get', 'secret', 'llm-router-auth', '-o', 'jsonpath={.data.admin-token}'], { encoding: 'utf8', env: { ...process.env, MSYS_NO_PATHCONV: '1' } });
      if (r.status !== 0 || !r.stdout) throw new Error('cannot read the router admin token (kubectl context?)');
      return Buffer.from(r.stdout, 'base64').toString('utf8');
    },
    /** The live router image digest (for the report). */
    image() {
      const r = spawnSync('kubectl', ['--context', ctx.kube, '-n', 'llm-router', 'get', 'pods', '-l', 'app=llm-router', '-o', 'jsonpath={.items[0].status.containerStatuses[0].imageID}'], { encoding: 'utf8', env: { ...process.env, MSYS_NO_PATHCONV: '1' } });
      return (r.stdout || '').split('@')[1] ?? null;
    },
    teardown() {
      ctx.ssh(`rm -rf ${ctx.remote}; for p in $(pgrep -f "node ${ctx.remote}/kit/tap[.]mjs"); do kill $p; done`, { timeout: 60_000 });
      fs.rmSync(local, { recursive: true, force: true });
    },
  };
  return ctx;
}
