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
//   optIn?: boolean                   // left out of a run without --providers; runs only when named
// }
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const REQUIRED = { name: 'string', describe: 'string', kinds: 'object', layerA: 'function', run: 'function' };

export function validate(p, file) {
  for (const [k, t] of Object.entries(REQUIRED)) if (typeof p?.[k] !== t) throw new Error(`${file}: provider must define ${k} (${t})`);
  if (p.optIn !== undefined && typeof p.optIn !== 'boolean') throw new Error(`${file}: optIn must be a boolean`);
  return p;
}

/** The providers a run uses: those named in --providers, or, with none named, every provider that is not opt-in. */
export function selectProviders(known, names) {
  return known.filter((p) => (names ? names.includes(p.name) : !p.optIn));
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
