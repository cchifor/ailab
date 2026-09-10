// Set $DSH_HOME/profiles/web/package.json's dsh.profile.bundles to exactly the
// bundles that are actually present in the profile's node_modules.
//
// WHY THIS EXISTS AT ALL. dsh ships the supported way to do this --
// `dsh plugin --profile web add <pkg>`, a pnpm forwarder that reconciles the
// list afterwards. It cannot be used here: it runs pnpm with cwd = the profile
// directory, which lives on dsh-home (RWO, local-path), and the container that
// HAS egress to a registry is the install Job, which deliberately does not
// mount that volume (mounting it could pin the Job to a different node than the
// Deployment and deadlock the volume -- see install-job.yaml's header). So the
// Job stages the closure on the RWX volume and this script does the reconcile
// half against the projected copy.
//
// WHY IT IS DECLARATIVE, NOT A MERGE. loadProfileDirectory() in dsh-app-boot
// maps over dsh.profile.bundles and calls resolveBundleDir + readFileSync on
// each, unguarded. A name whose package is absent throws:
//
//   Error: dsh: cannot resolve profile bundle "@deepseek-ai/dsh-subagent-codex"
//   from the dsh installation or /dsh-home/profiles/web
//       at resolveBundleDir (.../dsh-app-boot/lib/index.js:831:8)
//
// That is an unhandled throw at boot. On a 1-replica Recreate Deployment it is
// an outage, not a missing feature -- reproduced in a container before this was
// written. A merge would leave a stale name behind after a git revert and do
// exactly that, so the list is SET from disk every boot, never appended to.
//
// The membership test matches the CLI's: a package joins the layer stack when
// its own manifest declares `dsh.bundle`. Reconciling by installed state rather
// than by a hand-written list means a package that gains its declaration in a
// newer version activates on the next roll without another edit here.
const fs = require('fs');
const path = require('path');

const PROFILE = '/dsh-home/profiles/web';
const MANIFEST = path.join(PROFILE, 'package.json');
const MODULES = path.join(PROFILE, 'node_modules');

// The bundles the profile TEMPLATE ships. These resolve from the dsh install
// tree, not from the profile's node_modules, so they are always valid and must
// never be dropped -- without them the web profile composes nothing at all.
const SHIPPED = ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'];

// A package joins the layer stack only if it is BOOTABLE, which is a stronger test than
// "declares dsh.bundle". loadProfileDirectory reads the declared patch file itself:
//
//     const declared = JSON.parse(readFileSync(join(packageDir, 'package.json'))).dsh?.bundle?.patch;
//     const patchPath = join(packageDir, declared);
//     patches: loadOverlayPatches(binName, patchPath)
//
// so a package with an intact manifest and a MISSING or unreadable patch file passes a
// declaration-only check, gets published in dsh.profile.bundles, and then throws at boot -- the
// same crash-loop as a wholly absent package, but reached through a half-written closure that
// every other guard accepts. Checking the file is the difference between "the manifest says it
// is a bundle" and "this will actually load".
function declaresBundle(dir) {
  try {
    const m = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'));
    const declared = m && m.dsh && m.dsh.bundle && m.dsh.bundle.patch;
    if (typeof declared !== 'string' || declared === '') return false;
    // Resolve exactly as the loader does, and prove it is readable rather than merely present.
    fs.accessSync(path.join(dir, declared), fs.constants.R_OK);
    return true;
  } catch {
    return false;
  }
}

// Scan one level of scopes plus bare names, the shape a hoisted node_modules has.
function installedBundles() {
  const found = [];
  let entries;
  try {
    entries = fs.readdirSync(MODULES, { withFileTypes: true });
  } catch {
    return found;                     // no closure projected: shipped bundles only
  }
  for (const e of entries) {
    if (e.name.startsWith('.')) continue;
    if (e.name.startsWith('@')) {
      let scoped = [];
      try {
        scoped = fs.readdirSync(path.join(MODULES, e.name), { withFileTypes: true });
      } catch { continue; }
      for (const s of scoped) {
        const name = `${e.name}/${s.name}`;
        if (declaresBundle(path.join(MODULES, e.name, s.name))) found.push(name);
      }
    } else if (declaresBundle(path.join(MODULES, e.name))) {
      found.push(e.name);
    }
  }
  return found.sort();
}

if (!fs.existsSync(MANIFEST)) {
  console.log('reconcile-bundles: no profile manifest yet -- fix-profile.js will create it');
  process.exit(0);
}

const manifest = JSON.parse(fs.readFileSync(MANIFEST, 'utf8'));
const present = installedBundles();
// SHIPPED first: the shipped root's layers must apply before any deployment
// bundle patches on top of them, which is the order the CLI produces too.
const bundles = SHIPPED.concat(present.filter((n) => !SHIPPED.includes(n)));

const before = JSON.stringify((manifest.dsh || {}).profile ? manifest.dsh.profile.bundles : undefined);
manifest.dsh = { ...manifest.dsh, profile: { ...((manifest.dsh || {}).profile || {}), bundles } };
fs.writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2));

console.log(`reconcile-bundles: ${before} -> ${JSON.stringify(bundles)}`);
if (present.length === 0) {
  console.log('reconcile-bundles: no deployment bundles present (shipped only)');
}
