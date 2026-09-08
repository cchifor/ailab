// Reconcile the `litellm` PROVIDER block in $DSH_HOME/settings.yaml from the seed, on every boot.
//
// WHY THIS EXISTS. settings.yaml is seeded ONCE and then owned by dsh, because the Settings UI
// writes it. That is right for user preferences and wrong for the provider block: baseURL, api,
// apiKeyEnv and the model list are infrastructure this repo owns. The consequence was a real
// outage, twice. When #547 renamed the local routes to their `-cloud` names, the seed in git was
// updated and the PVC copy was not, so dsh went on advertising four model ids the gateway no
// longer served -- qwen3.8-27b-vllm, qwen3.8-27b-vllm-fast, qwen3-coder-30b-a3b and qwen3.5-122b,
// every one of them a 404. Nothing surfaced it: dsh lists whatever settings.yaml names, and the
// failure only appears when a user picks a model. Verified against /v1/models on 2026-09-08 --
// all four were missing while the seed in git had the correct names all along.
//
// Any model rename would break it again, so the fix is reconciliation rather than a one-off edit.
//
// WHAT IT TOUCHES. Only the `litellm:` block under llm-pi-ai.providers. Everything else in the
// file -- other providers, UI preferences, anything the Settings UI wrote -- is preserved
// byte-for-byte. Credentials are NOT here at all: dsh keeps them in $DSH_HOME/.credentials.yaml,
// so replacing this block cannot disturb the stored key. `apiKeyEnv` only names an env var.
//
// Line-based, not a YAML round-trip, and deliberately so: both files are ours, the structure is
// fixed, and a parse/serialise cycle would strip every explanatory comment in the file. There is
// no YAML parser in this init container either -- it is a bare node:22-alpine with /seed and
// /dsh-home mounted, nothing installed.
const fs = require('fs');

// Paths are overridable purely so this can be exercised against a copy of a real settings.yaml
// before it is trusted with the live one; the defaults are what the initContainer mounts.
const LIVE = process.env.DSH_SETTINGS || '/dsh-home/settings.yaml';
const SEED = process.env.DSH_SEED || '/seed/settings.seed.yaml';
const KEY = '    litellm:';          // 4-space indent: llm-pi-ai > providers > litellm
const PROVIDERS = '  providers:';

// Returns [startIndex, endIndex) of the block headed by `head`: the head line plus every
// following line that is blank or indented deeper than the head. Returns null if absent.
function blockRange(lines, head) {
  const start = lines.findIndex((l) => l === head);
  if (start === -1) return null;
  const depth = head.length - head.trimStart().length;
  let end = start + 1;
  while (end < lines.length) {
    const l = lines[end];
    if (l.trim() === '') { end++; continue; }
    const ind = l.length - l.trimStart().length;
    if (ind <= depth) break;
    end++;
  }
  // Do not swallow trailing blank lines that belong to whatever follows.
  while (end > start + 1 && lines[end - 1].trim() === '') end--;
  return [start, end];
}

if (!fs.existsSync(LIVE)) {
  // Nothing to reconcile -- the init container's `install` step seeds the whole file instead.
  console.log('settings.yaml absent; seeding path handles it');
  process.exit(0);
}

const liveLines = fs.readFileSync(LIVE, 'utf8').split('\n');
const seedLines = fs.readFileSync(SEED, 'utf8').split('\n');

const seedRange = blockRange(seedLines, KEY);
if (!seedRange) {
  // The seed is ours; if its shape ever changes this must fail loudly rather than silently
  // leave a stale provider in place, which is the exact failure mode this script exists to end.
  console.error('FATAL: no "' + KEY.trim() + '" block in the seed -- refusing to guess');
  process.exit(1);
}
const seedBlock = seedLines.slice(seedRange[0], seedRange[1]);

const liveRange = blockRange(liveLines, KEY);
let out;
if (liveRange) {
  const liveBlock = liveLines.slice(liveRange[0], liveRange[1]);
  if (liveBlock.join('\n') === seedBlock.join('\n')) {
    console.log('litellm provider already matches the seed');
    process.exit(0);
  }
  const before = liveBlock.filter((l) => /^\s*- id:/.test(l)).map((l) => l.trim());
  const after = seedBlock.filter((l) => /^\s*- id:/.test(l)).map((l) => l.trim());
  console.log('reconciling litellm provider from the seed');
  console.log('  was:', before.join(', ') || '(none)');
  console.log('  now:', after.join(', ') || '(none)');
  out = [...liveLines.slice(0, liveRange[0]), ...seedBlock, ...liveLines.slice(liveRange[1])];
} else {
  const at = liveLines.findIndex((l) => l === PROVIDERS);
  if (at === -1) {
    console.error('FATAL: settings.yaml has neither a litellm provider nor a "providers:" key');
    process.exit(1);
  }
  console.log('litellm provider missing from settings.yaml; inserting it from the seed');
  out = [...liveLines.slice(0, at + 1), ...seedBlock, ...liveLines.slice(at + 1)];
}

fs.writeFileSync(LIVE, out.join('\n'));
console.log('settings.yaml updated');
