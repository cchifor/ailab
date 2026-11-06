// providers/index.mjs - provider discovery. Every top-level module in providers/ (except this one) is a provider; adding
// a provider is adding one file. Each default-exports an object implementing the interface below; the core validates
// it and never knows a provider by name.
//
// interface Provider {
//   name: string                      // registry key; tasks name it in `providers`
//   describe: string                  // one line for the report
//   kinds: string[]                   // task kinds it can run (tasks/registry.mjs)
//   setup?(ctx): Promise<void>        // deploy what it needs (worker kit, temp homes, the tap); idempotent
//   layerA(ctx): Promise<Check[]>     // deterministic transparency checks: { name, pass, detail }
//   run(ctx, task, arm, runId): Promise<RunMetrics>   // one run, arm 'direct' | 'router' (core/margins.mjs)
//   teardown?(ctx): Promise<void>     // provider-specific cleanup (the core always deletes the run's temp dirs)
// }
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const REQUIRED = { name: 'string', describe: 'string', kinds: 'object', layerA: 'function', run: 'function' };

export function validate(p, file) {
  for (const [k, t] of Object.entries(REQUIRED)) if (typeof p?.[k] !== t) throw new Error(`${file}: provider must define ${k} (${t})`);
  return p;
}

export async function loadProviders(dir = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'))) {
  const out = [];
  for (const f of fs.readdirSync(dir).sort()) {
    if (!f.endsWith('.mjs') || f === 'index.mjs') continue;
    const mod = await import(pathToFileURL(path.join(dir, f)).href);
    out.push(validate(mod.default, f));
  }
  return out;
}
