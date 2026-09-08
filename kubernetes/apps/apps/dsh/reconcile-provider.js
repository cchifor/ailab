// Reconcile the `litellm` PROVIDER block in $DSH_HOME/settings.yaml from the seed, on every boot.
//
// WHY THIS EXISTS. settings.yaml is seeded ONCE and then owned by dsh, because the Settings UI
// writes it. That is right for user preferences and wrong for the provider block: baseURL, api,
// apiKeyEnv and the model list are infrastructure this repo owns. When #547 renamed the local
// routes to their `-cloud` names, the seed in git was updated and the PVC copy was not, so dsh
// went on advertising four model ids the gateway no longer served -- qwen3.8-27b-vllm,
// qwen3.8-27b-vllm-fast, qwen3-coder-30b-a3b and qwen3.5-122b, every one of them a 404. Nothing
// surfaced it: dsh lists whatever settings.yaml names, and it only fails when a user picks a
// model. Verified against /v1/models on 2026-09-08 -- all four missing, while the seed in git had
// the correct names all along. Any future rename would break it again, so this reconciles rather
// than patching the volume once.
//
// WHAT IT TOUCHES. Only the block at llm-pi-ai > providers > litellm. Everything else -- other
// providers, UI preferences, anything the Settings UI wrote -- is preserved byte-for-byte.
// Credentials are untouched by construction, not by care: they are not in this file at all. dsh
// keeps them in $DSH_HOME/.credentials.yaml, and `apiKeyEnv` only names an environment variable.
//
// Line-based, not a YAML round-trip, deliberately: a parse/serialise cycle would strip every
// explanatory comment in the file, and there is no YAML parser in this init container -- it is a
// bare node:22-alpine with only /seed and /dsh-home mounted.
//
// THREE ROBUSTNESS PROPERTIES, each one a review finding against an earlier draft:
//
//   1. The path is WALKED, not string-matched. Searching the whole file for the first four-space
//      `litellm:` would rewrite an unrelated section that happened to carry that key, while
//      leaving the real provider stale -- the exact opposite of the point. Each level below is
//      scoped to its parent's block.
//   2. Keys are matched by regex with the indent DERIVED, not asserted. The Settings UI owns this
//      file and may re-serialise it; hinging the reconcile on byte-exact leading whitespace and no
//      trailing space is a needless single point of failure.
//   3. A live file we cannot understand is a WARNING, not a fatal. This runs in an initContainer
//      under `set -eu`, so a non-zero exit puts dsh into CrashLoopBackOff -- a harder outage than
//      the stale-model bug being fixed. If the UI re-serialises settings.yaml into a shape this
//      cannot walk, the right move is to leave it alone and let dsh boot. The SEED check stays
//      fatal: that file is ours and version-controlled, so a malformed one is our error and must
//      fail loudly on rollout rather than silently skipping the reconcile and reintroducing the
//      very bug this exists to prevent.
const fs = require('fs');

// Overridable purely so this can be exercised against a copy of a real settings.yaml before it is
// trusted with the live one; the defaults are what the initContainer mounts.
const LIVE = process.env.DSH_SETTINGS || '/dsh-home/settings.yaml';
const SEED = process.env.DSH_SEED || '/seed/settings.seed.yaml';

// Plain identifiers by construction (letters and hyphens only), so they need no regex escaping.
const PATH = ['llm-pi-ai', 'providers', 'litellm'];

const indentOf = (l) => l.length - l.trimStart().length;
const isComment = (l) => l.trimStart().startsWith('#');

// First line in [from, to) that is `<indent><key>:` with indent strictly deeper than the parent.
function findKey(lines, key, parentDepth, from, to) {
  const re = new RegExp('^( *)' + key + ':[ \\t]*$');
  for (let i = from; i < to; i++) {
    const m = re.exec(lines[i]);
    if (m && m[1].length > parentDepth) return { index: i, depth: m[1].length };
  }
  return null;
}

// End (exclusive) of the block headed at `start`. A deeper-indented line continues the block.
//
// A COMMENT at or above the head's indent is AMBIGUOUS -- it may be an interior note written at a
// shallow column, or the banner introducing the NEXT section -- so look past it: if deeper content
// follows, the comments were interior and the block continues. Without this a single column-0 `#`
// inside the block truncates it, and a truncated seedBlock would write a partial provider while
// leaving the live file's stale tail behind: corrupt YAML, worse than the bug being fixed.
function blockEnd(lines, start, depth, to) {
  let end = start + 1;
  while (end < to) {
    const l = lines[end];
    if (l.trim() === '') { end++; continue; }
    if (indentOf(l) > depth) { end++; continue; }
    if (isComment(l)) {
      let k = end;
      while (k < to && (lines[k].trim() === '' || isComment(lines[k]))) k++;
      if (k < to && indentOf(lines[k]) > depth) { end = k; continue; }
    }
    break;
  }
  while (end > start + 1 && lines[end - 1].trim() === '') end--;
  return end;
}

// Walks PATH from the file root, each level bounded by its parent's block.
function resolve(lines, path) {
  let from = 0, to = lines.length, parentDepth = -1, range = null;
  for (let d = 0; d < path.length; d++) {
    const hit = findKey(lines, path[d], parentDepth, from, to);
    if (!hit) return { range: null, failedAt: path.slice(0, d + 1).join(' > ') };
    const end = blockEnd(lines, hit.index, hit.depth, to);
    range = [hit.index, end];
    from = hit.index + 1; to = end; parentDepth = hit.depth;
  }
  return { range, failedAt: null };
}

if (!fs.existsSync(LIVE)) {
  console.log('settings.yaml absent; the seeding branch handles it');
  process.exit(0);
}

const liveLines = fs.readFileSync(LIVE, 'utf8').split('\n');
const seedLines = fs.readFileSync(SEED, 'utf8').split('\n');

// FATAL by design: the seed is ours, and silently skipping would reintroduce the stale bug.
const seed = resolve(seedLines, PATH);
if (!seed.range) {
  console.error('FATAL: seed has no ' + seed.failedAt + ' -- refusing to guess');
  process.exit(1);
}
const seedBlock = seedLines.slice(seed.range[0], seed.range[1]);

const live = resolve(liveLines, PATH);
let out;
if (live.range) {
  const liveBlock = liveLines.slice(live.range[0], live.range[1]);
  if (liveBlock.join('\n') === seedBlock.join('\n')) {
    console.log('litellm provider already matches the seed');
    process.exit(0);
  }
  const ids = (b) => b.filter((l) => /^\s*- id:/.test(l)).map((l) => l.trim().replace(/^- id:\s*/, ''));
  console.log('reconciling litellm provider from the seed');
  console.log('  was:', ids(liveBlock).join(', ') || '(none)');
  console.log('  now:', ids(seedBlock).join(', ') || '(none)');
  out = [...liveLines.slice(0, live.range[0]), ...seedBlock, ...liveLines.slice(live.range[1])];
} else {
  // No litellm provider. Insert under the CORRECT providers block -- the one inside llm-pi-ai.
  const parent = resolve(liveLines, PATH.slice(0, -1));
  if (!parent.range) {
    // WARN, not fatal: see robustness note 3. dsh boots with whatever it already has.
    console.warn('WARNING: settings.yaml has no ' + parent.failedAt + '; leaving it untouched.');
    console.warn('         The litellm provider was NOT reconciled. If dsh lists stale models,');
    console.warn('         this file has a shape this script cannot walk -- fix it by hand.');
    process.exit(0);
  }
  console.log('litellm provider missing from llm-pi-ai.providers; inserting it from the seed');
  const at = parent.range[0];
  out = [...liveLines.slice(0, at + 1), ...seedBlock, ...liveLines.slice(at + 1)];
}

fs.writeFileSync(LIVE, out.join('\n'));
console.log('settings.yaml updated');
