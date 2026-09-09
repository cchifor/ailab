// Repoint the chat UI's file-open action at an HTTP route the browser can actually reach.
//
// WHY THIS PATCH EXISTS. Clicking a produced file in the transcript raised
//     path open failed: path open failed: spawn xdg-open ENOENT
// and no configuration could stop it. The chain is:
//
//   dsh-client-ui-deliverables  producedFileMentions() -> openFile(path)   [UNGATED]
//   dsh-client-ui-chat          openFile -> remote.session.openWorkspacePath()
//   dsh-api-session-controller  openWorkspacePath() -> openNativePath()    [UNGATED]
//   dsh-native-command          openNativePath() -> spawn("xdg-open")
//
// dsh does carry a capability probe, `canOpenWorkspacePath()`, and the "Produced" chip row honours
// it (`const canOpenPath = isLoopback && hostCanOpenPath`). The INLINE filename mention in the
// message text does not: producedFileMentions calls openFile unconditionally. Neither does the
// server's openWorkspacePath, which calls this.openPath() without ever consulting this.canOpenPath().
// So the `nativeOpen: false` setting reaches only the gate the OTHER rendering uses.
//
// Installing xdg-utils would not have helped either. openNativePath runs SERVER-SIDE, inside this
// pod. Even a working desktop opener would open the file on a machine in the cluster, never in the
// browser of whoever clicked. For a remotely-reached deployment the click has to become an HTTP
// fetch, which is what this rewrite does -- the relay serves the bytes (see relay.js).
//
// FAILS LOUDLY, NEVER SILENTLY. If the anchor stops matching -- an upstream refactor, a minified
// build -- this exits non-zero and the install Job fails, rather than leaving a pod whose file links
// quietly do nothing. Re-running it on an already-patched tree is a no-op.
const fs = require('fs');
const path = require('path');

const dest = process.argv[2];
if (dest === undefined || dest === '') {
  console.error('patch-open-file: usage: node patch-open-file.js <install-prefix>');
  process.exit(2);
}

const target = path.join(dest, 'node_modules', '@deepseek-ai', 'dsh-client-ui-chat', 'lib', 'client.js');
if (!fs.existsSync(target)) {
  console.error(`patch-open-file: ${target} does not exist`);
  process.exit(1);
}

const MARK = '__dsh-file';
let source = fs.readFileSync(target, 'utf8');
if (source.includes(MARK)) {
  console.log('patch-open-file: already applied');
  process.exit(0);
}

// The bundle is tab-indented and unminified. Matched as an exact string rather than a regex so a
// changed body cannot be partially rewritten into something that still parses.
const T = '\t'.repeat(7);
const B = '\t'.repeat(8);
const before = [
  `${T}openFile: async (path) => {`,
  `${B}const cwd = ctx.sessions.list.getSnapshot().byId[sessionId]?.cwd;`,
  `${B}const result = await ctx.remote.session.openWorkspacePath({ path: resolveWorkspacePath(cwd, path) });`,
  // single-quoted, so the ${...} below stay literal characters of the bundle being matched
  B + 'if (!result.ok) throw new Error(`path open failed: ${result.error.message}`);',
  `${T}},`,
].join('\n');

const after = [
  `${T}openFile: async (path) => {`,
  `${B}// PATCHED (kubernetes/apps/apps/dsh/patch-open-file.js). Upstream RPCs to a server-side`,
  `${B}// xdg-open, which cannot reach the browser of a remote user. relay.js serves the bytes.`,
  `${B}const cwd = ctx.sessions.list.getSnapshot().byId[sessionId]?.cwd;`,
  `${B}const target = resolveWorkspacePath(cwd, path);`,
  `${B}window.open("/${MARK}?path=" + encodeURIComponent(target), "_blank", "noopener,noreferrer");`,
  `${T}},`,
].join('\n');

if (!source.includes(before)) {
  console.error('patch-open-file: ANCHOR NOT FOUND in dsh-client-ui-chat/lib/client.js.');
  console.error('patch-open-file: upstream changed the openFile body. Re-read it and update this');
  console.error('patch-open-file: patch; failing rather than shipping dead file links.');
  process.exit(1);
}

source = source.replace(before, after);
fs.writeFileSync(target, source);

// A .map alongside a rewritten bundle now lies about the source. Drop it rather than ship a
// mapping that points at code which is no longer there.
const map = `${target}.map`;
if (fs.existsSync(map)) fs.rmSync(map);

console.log('patch-open-file: openFile now opens /' + MARK + ' in a new tab');
