// Repoint `agent-default-model` in $DSH_HOME/settings.yaml when it names a RETIRED provider.
//
// WHY THIS EXISTS. agent-default-model is the route a NEW session starts on. It is a user setting
// (the model picker saves it), so unlike the provider blocks it is NOT reconciled on every boot.
// But when this repo retires a provider -- 2026-10-19, every model moved to llm-router (ADR 0039)
// and `litellm`, `openai-codex`, `openai-codex-realjaynesage` and the shipped `deepseek-official`
// went away -- a saved default that still names one would start every new session on a route that
// no longer exists. So this rewrites the block ONLY in that case, and leaves any default the user
// picked from the current providers alone.
//
// WHAT IT TOUCHES. Only the top-level `agent-default-model:` block, replaced by the seed's block of
// the same name. Line-based, for the reasons in reconcile-provider.js's header: no YAML parser in
// this init container, and a round-trip would strip every comment in a file dsh owns.
//
// NEVER FATAL ON THE LIVE FILE (that script's rule 3): an initContainer under `set -eu` that exits
// non-zero crash-loops dsh, a harder outage than a stale default. A broken SEED is fatal -- it is
// ours and version-controlled.
//
//   DSH_RETIRED_PROVIDERS  comma-separated provider names whose default must be repointed
//   DSH_SETTINGS, DSH_SEED overridable so this can be exercised against a copy (as the other script)
const fs = require('fs');

const LIVE = process.env.DSH_SETTINGS || '/dsh-home/settings.yaml';
const SEED = process.env.DSH_SEED || '/seed/settings.seed.yaml';
const KEY = 'agent-default-model';
const RETIRED = (process.env.DSH_RETIRED_PROVIDERS || '')
  .split(',').map((s) => s.trim()).filter((s) => /^[A-Za-z][A-Za-z0-9-]*$/.test(s));

// The key at column 0, bare or quoted, with nothing after the colon but an optional value or comment.
const HEADER = new RegExp(`^(?:${KEY}|"${KEY}"|'${KEY}')[ \\t]*:`);
const isBlank = (l) => l.trim() === '';
const isComment = (l) => l.trimStart().startsWith('#');
const indentOf = (l) => l.length - l.trimStart().length;

// [start, end) of the top-level block, or null. Continuation = any deeper-indented line, blank
// lines, and comments that are followed by more deeper content (an interior note at column 0).
function findBlock(lines) {
  const hits = [];
  for (let i = 0; i < lines.length; i++) if (HEADER.test(lines[i])) hits.push(i);
  if (hits.length !== 1) return hits.length ? { duplicate: true } : null;
  const start = hits[0];
  let end = start + 1;
  while (end < lines.length) {
    const l = lines[end];
    if (isBlank(l) || indentOf(l) > 0) { end++; continue; }
    if (isComment(l)) {
      let k = end;
      while (k < lines.length && (isBlank(lines[k]) || isComment(lines[k]))) k++;
      if (k < lines.length && indentOf(lines[k]) > 0) { end = k; continue; }
    }
    break;
  }
  while (end > start + 1 && isBlank(lines[end - 1])) end--;
  return { start, end };
}

// The provider a block names, read from the ACTIVE mapping only -- never from a comment. A
// whole-block text search would take `# previous provider: litellm` above the real key as the
// setting, and then overwrite a valid default the user picked (or skip a retired one). Two shapes:
//   block form  `agent-default-model:` then a DIRECT child `provider: x` (comments skipped; the
//               child indent is the first real line's, so deeper keys never match)
//   flow form   `agent-default-model: { provider: x, model: y }` on the header line itself
// Anything else (a scalar value, an anchor, a multi-line flow mapping) is null: unreadable, and
// the caller then leaves the file alone.
const PROVIDER_VALUE = `(['"]?)([A-Za-z0-9._-]+)\\1`;
function providerOf(blockLines) {
  const header = blockLines[0].replace(HEADER, '').trim();
  if (header !== '' && !header.startsWith('#')) {
    const flow = header.match(/^\{([^{}#]*)\}[ \t]*(#.*)?$/);
    if (!flow) return null;
    for (const pair of flow[1].split(',')) {
      const m = pair.trim().match(new RegExp(`^(?:provider|"provider"|'provider')[ \\t]*:[ \\t]*${PROVIDER_VALUE}$`));
      if (m) return m[2];
    }
    return null;
  }
  const children = blockLines.slice(1).filter((l) => !isBlank(l) && !isComment(l));
  if (!children.length) return null;
  const depth = indentOf(children[0]);
  const key = new RegExp(`^ {${depth}}(?:provider|"provider"|'provider')[ \\t]*:[ \\t]*${PROVIDER_VALUE}[ \\t]*(#.*)?$`);
  for (const l of children) {
    if (indentOf(l) !== depth) continue;
    const m = l.match(key);
    if (m) return m[2];
  }
  return null;
}

// Split on LF and keep each line's own CR off the matching; rejoin with the file's own ending.
const read = (p) => {
  const text = fs.readFileSync(p, 'utf8');
  const eol = text.includes('\r\n') ? '\r\n' : '\n';
  return { eol, lines: text.split(/\r?\n/) };
};

if (!RETIRED.length) {
  console.log('no DSH_RETIRED_PROVIDERS given; nothing to do');
  process.exit(0);
}
if (!fs.existsSync(LIVE)) {
  console.log('settings.yaml absent; the seeding branch handles it');
  process.exit(0);
}

const seed = read(SEED);
const seedBlock = findBlock(seed.lines);
if (!seedBlock || seedBlock.duplicate || !providerOf(seed.lines.slice(seedBlock.start, seedBlock.end))) {
  console.error(`FATAL: the seed has no single plain \`${KEY}:\` block naming a provider -- refusing to guess`);
  process.exit(1);
}
const replacement = seed.lines.slice(seedBlock.start, seedBlock.end);
if (RETIRED.includes(providerOf(replacement))) {
  console.error(`FATAL: the seed's ${KEY} names a retired provider (${providerOf(replacement)})`);
  process.exit(1);
}

const live = read(LIVE);
const block = findBlock(live.lines);
if (!block) {
  // No saved default: the composition's agent-default-model row (cordis.patch.yml) applies.
  console.log(`no ${KEY} in settings.yaml; the composition default applies`);
  process.exit(0);
}
if (block.duplicate) {
  console.warn(`WARNING: settings.yaml has more than one ${KEY} key; leaving it untouched.`);
  process.exit(0);
}
const current = providerOf(live.lines.slice(block.start, block.end));
if (current === null) {
  console.warn(`WARNING: settings.yaml's ${KEY} names no provider this script can read; leaving it untouched.`);
  process.exit(0);
}
if (!RETIRED.includes(current)) {
  console.log(`${KEY} names ${current}, which is not retired; keeping the user's default`);
  process.exit(0);
}
const out = [...live.lines.slice(0, block.start), ...replacement, ...live.lines.slice(block.end)];
fs.writeFileSync(LIVE, out.join(live.eol));
console.log(`${KEY} named the retired provider ${current}; repointed to ${providerOf(replacement)} from the seed`);
