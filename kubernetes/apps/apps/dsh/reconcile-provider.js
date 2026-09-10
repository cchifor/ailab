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
// ROBUSTNESS. This parses a file ANOTHER COMPONENT OWNS AND REWRITES, so every rule below exists
// because a review found the earlier draft could corrupt it:
//
//   1. DIRECT CHILDREN ONLY, never arbitrary descendants. The root must sit at column 0, and each
//      later step must match at exactly its parent's child indent. Accepting any deeper match let
//      a nested `llm-pi-ai.metadata.providers.litellm` be selected and overwritten while the real
//      provider stayed stale -- the exact failure this script exists to prevent. Requiring the
//      exact depth also skips nested subtrees for free, since they are deeper.
//   2. INDENT IS DERIVED ON READ, AND APPLIED ON WRITE. Keys are matched by regex with the live
//      file's own indentation, because the UI may re-serialise. The seed block is then RE-INDENTED
//      to the live block's depth before splicing: without that, a live file at 2-space and a seed
//      at 4-space would splice `litellm:` at the depth of its own parent, moving it out of the
//      mapping and invalidating the sibling providers.
//   3. AN UNWALKABLE LIVE FILE IS A WARNING, NOT A FATAL. This runs in an initContainer under
//      `set -eu`, so a non-zero exit puts dsh into CrashLoopBackOff -- a harder outage than the
//      stale-model bug being fixed. The SEED check stays fatal: that file is ours and
//      version-controlled, so a malformed one is our error and must fail on rollout rather than
//      silently skip the reconcile and reintroduce the bug.
const fs = require('fs');

// Overridable purely so this can be exercised against a copy of a real settings.yaml before it is
// trusted with the live one; the defaults are what the initContainer mounts.
const LIVE = process.env.DSH_SETTINGS || '/dsh-home/settings.yaml';
const SEED = process.env.DSH_SEED || '/seed/settings.seed.yaml';

// WHICH provider block to reconcile. Defaults to `litellm`, so an invocation with no env is
// byte-for-byte the behaviour this script has always had. `codex` (architecture B, the harness
// running on the Codex model route) is reconciled by a SECOND invocation with DSH_PROVIDER set,
// for the reason the seed alone is not enough: settings.yaml is only SEEDED when it is absent,
// and it has existed on this deployment since 2026-09-07 -- so a provider added to the seed file
// would never reach the live volume. That is exactly how #613's AGENTS.md shipped inert.
//
// Plain identifiers by construction (letters and hyphens only), so they need no regex escaping.
// Validated rather than trusted: this string is interpolated into a RegExp below, and a value
// carrying regex metacharacters would silently match the wrong block.
const PROVIDER = process.env.DSH_PROVIDER || 'litellm';
// REMOVE mode. A provider this repo once wrote and now must UNWRITE cannot simply
// be dropped from the seed: settings.yaml lives on the PVC and reconcile only
// rewrites the block it is told to, so a stale block survives every boot.
//
// This exists because of a real outage. A `codex` provider was seeded with
// `api: openai-codex-responses`, which is NOT an accepted value -- the schema
// takes only openai-completions | openai-responses | anthropic-messages. The
// whole `providers` dict then failed validation, so NO provider resolved,
// including litellm, and the composer went dead with no models. Removing it from
// the seed would have fixed new volumes and left every existing one broken.
const REMOVE = process.env.DSH_PROVIDER_REMOVE === '1';
if (!/^[A-Za-z][A-Za-z0-9-]*$/.test(PROVIDER)) {
  console.warn(`WARNING: DSH_PROVIDER ${JSON.stringify(PROVIDER)} is not a plain identifier;`);
  console.warn('         leaving settings.yaml untouched.');
  process.exit(0);
}
const PATH = ['llm-pi-ai', 'providers', PROVIDER];

const indentOf = (l) => l.length - l.trimStart().length;
const isComment = (l) => l.trimStart().startsWith('#');
const isBlank = (l) => l.trim() === '';

// Match `<exactly depth spaces><key>:` within [from, to). Exact depth is what confines the walk
// to direct children (rule 1); anything nested is deeper and therefore skipped.
//
// A trailing inline comment is VALID YAML for a block header (`litellm: # custom provider`) and
// must match, or resolution reports the key absent and the insertion branch splices a SECOND
// `litellm` key beside the real one -- duplicate mapping keys, which either fail to parse or
// leave the stale provider effective. Anything else after the colon (a value, an anchor, a flow
// mapping) is a shape this script does not handle; KEY_PRESENT below detects that case so the
// caller can decline rather than duplicate.
//
// A key may also be QUOTED (`"litellm":`, `'litellm':`) or carry whitespace before the colon.
// Those are the SAME YAML key, so both patterns must recognise them: if the presence check does
// not, an existing quoted provider is reported absent and an unquoted duplicate is spliced beside
// it -- duplicate mapping keys in a file that was valid before this script touched it.
const KEYPAT = (key) => '(?:' + key + '|"' + key + '"|\'' + key + '\')';
const KEY_HEADER = (key, depth) =>
  new RegExp('^ {' + depth + '}' + KEYPAT(key) + '[ \\t]*:[ \\t]*(#.*)?$');
const KEY_PRESENT = (key, depth) =>
  new RegExp('^ {' + depth + '}' + KEYPAT(key) + '[ \\t]*:');

function findKeyAt(lines, key, depth, from, to) {
  const re = KEY_HEADER(key, depth);
  for (let i = from; i < to; i++) if (re.test(lines[i])) return i;
  return -1;
}

// True if the key is present at this depth in ANY form -- including one findKeyAt cannot parse.
function keyPresentAt(lines, key, depth, from, to) {
  const re = KEY_PRESENT(key, depth);
  for (let i = from; i < to; i++) if (re.test(lines[i])) return true;
  return false;
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
    if (isBlank(l)) { end++; continue; }
    if (indentOf(l) > depth) { end++; continue; }
    if (isComment(l)) {
      let k = end;
      while (k < to && (isBlank(lines[k]) || isComment(lines[k]))) k++;
      if (k < to && indentOf(lines[k]) > depth) { end = k; continue; }
    }
    break;
  }
  while (end > start + 1 && isBlank(lines[end - 1])) end--;
  return end;
}

// Indent of the first real (non-blank, non-comment) child inside a block, or null if it has none.
function childIndent(lines, start, end) {
  for (let i = start + 1; i < end; i++) {
    if (isBlank(lines[i]) || isComment(lines[i])) continue;
    return indentOf(lines[i]);
  }
  return null;
}

// Walks PATH as DIRECT children. Returns { range, depth, failedAt }.
function resolve(lines, path) {
  let from = 0, to = lines.length, depth = 0, range = null;
  for (let d = 0; d < path.length; d++) {
    const at = findKeyAt(lines, path[d], depth, from, to);
    if (at === -1) return { range: null, depth: null, failedAt: path.slice(0, d + 1).join(' > ') };
    const end = blockEnd(lines, at, depth, to);
    range = [at, end];
    if (d < path.length - 1) {
      const ci = childIndent(lines, at, end);
      if (ci === null || ci <= depth) {
        return { range: null, depth: null, failedAt: path.slice(0, d + 2).join(' > ') };
      }
      from = at + 1; to = end; depth = ci;
    }
  }
  return { range, depth, failedAt: null };
}

// Shift a block's indentation by `delta` columns (rule 2). Blank lines stay blank.
function reindent(block, delta) {
  if (delta === 0) return block.slice();
  return block.map((l) => {
    if (isBlank(l)) return l;
    if (delta > 0) return ' '.repeat(delta) + l;
    const strip = Math.min(-delta, indentOf(l));
    return l.slice(strip);
  });
}

if (!fs.existsSync(LIVE)) {
  console.log('settings.yaml absent; the seeding branch handles it');
  process.exit(0);
}

const liveLines = fs.readFileSync(LIVE, 'utf8').split('\n');
const seedLines = fs.readFileSync(SEED, 'utf8').split('\n');

// FATAL by design: the seed is ours, and silently skipping would reintroduce the stale bug.
if (REMOVE) {
  const live = resolve(liveLines, PATH);
  if (!live.range) {
    console.log(`${PROVIDER} provider is not present; nothing to remove`);
    process.exit(0);
  }
  const out = [...liveLines.slice(0, live.range[0]), ...liveLines.slice(live.range[1])];
  fs.writeFileSync(LIVE, out.join('\n'));
  console.log(`removed the ${PROVIDER} provider block from settings.yaml`);
  process.exit(0);
}

const seed = resolve(seedLines, PATH);
// A provider the seed does not carry is not an error: this script is invoked once per provider
// this repo owns, and one may legitimately be absent from a given seed revision.
//
// ONLY THE LEAF. An earlier version exited on any `!seed.range`, which also swallowed a seed
// whose `llm-pi-ai` or `providers` block is missing or unwalkable -- previously FATAL, and
// rightly so: that is a broken seed, and treating it as "provider not offered" would silently
// preserve stale live configuration for `litellm` too. resolve() reports the full path it
// failed at, so the two cases are distinguishable.
if (!seed.range) {
  if (seed.failedAt === PATH.join(' > ')) {
    console.log(`${PROVIDER} provider is absent from the seed; nothing to reconcile`);
    process.exit(0);
  }
  // fall through to the pre-existing fatal handling for a broken ancestor
}
if (!seed.range) {
  console.error('FATAL: seed has no ' + seed.failedAt + ' -- refusing to guess');
  process.exit(1);
}
const seedBlock = seedLines.slice(seed.range[0], seed.range[1]);
const ids = (b) => b.filter((l) => /^\s*- id:/.test(l)).map((l) => l.trim().replace(/^- id:\s*/, ''));

const live = resolve(liveLines, PATH);
let out;
if (live.range) {
  const block = reindent(seedBlock, live.depth - seed.depth);
  const liveBlock = liveLines.slice(live.range[0], live.range[1]);
  if (liveBlock.join('\n') === block.join('\n')) {
    console.log(`${PROVIDER} provider already matches the seed`);
    process.exit(0);
  }
  console.log(`reconciling ${PROVIDER} provider from the seed`);
  console.log('  was:', ids(liveBlock).join(', ') || '(none)');
  console.log('  now:', ids(block).join(', ') || '(none)');
  if (live.depth !== seed.depth) console.log('  re-indented seed block by', live.depth - seed.depth);
  out = [...liveLines.slice(0, live.range[0]), ...block, ...liveLines.slice(live.range[1])];
} else {
  // No such provider. Insert under the CORRECT providers block -- the one inside llm-pi-ai --
  // at that block's own child indent, not the seed's.
  const parent = resolve(liveLines, PATH.slice(0, -1));
  if (!parent.range) {
    // WARN, not fatal: see rule 3. dsh boots with whatever it already has.
    console.warn('WARNING: settings.yaml has no ' + parent.failedAt + '; leaving it untouched.');
    console.warn(`         The ${PROVIDER} provider was NOT reconciled. If dsh lists stale models,`);
    console.warn('         this file has a shape this script cannot walk -- fix it by hand.');
    process.exit(0);
  }
  // Prefer an existing sibling's indent; with no siblings, step in by the file's own step size.
  const sib = childIndent(liveLines, parent.range[0], parent.range[1]);
  const step = parent.depth > 0 ? parent.depth : 2;
  const target = sib !== null && sib > parent.depth ? sib : parent.depth + step;

  // NEVER splice a second copy of a key that is already there. resolve() only recognises a plain
  // block header, so a provider written in a shape it cannot walk would otherwise be reported
  // missing and duplicated -- and duplicate mapping keys either fail to parse or silently leave
  // the stale provider in effect, both worse than the stale-model bug. Declining is safe: dsh
  // keeps whatever it has, and the warning says what to fix.
  if (keyPresentAt(liveLines, PROVIDER, target, parent.range[0], parent.range[1])) {
    console.warn(`WARNING: a ${PROVIDER} key already exists under llm-pi-ai.providers in a form this`);
    console.warn('         script cannot parse (a value, anchor or flow mapping on the header');
    console.warn('         line). Leaving settings.yaml untouched rather than inserting a');
    console.warn(`         duplicate key. Rewrite it as a plain \`${PROVIDER}:\` block to re-enable`);
    console.warn('         reconciliation.');
    process.exit(0);
  }
  const block = reindent(seedBlock, target - seed.depth);
  console.log(`${PROVIDER} provider missing from llm-pi-ai.providers; inserting it from the seed`);
  console.log('  at indent', target);
  const at = parent.range[0];
  out = [...liveLines.slice(0, at + 1), ...block, ...liveLines.slice(at + 1)];
}

fs.writeFileSync(LIVE, out.join('\n'));
console.log('settings.yaml updated');
