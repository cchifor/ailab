// Normalise the web profile's patchReload before dsh boots.
//
// dsh writes this manifest itself on first boot with patchReload: "live", and "live" makes
// profile-boot dynamically create a cordis-plugin-hmr entry when no hmr service is registered
// (dsh-base ships that row disabled). That plugin throws
//     --expose-internals is required for HMR service
// which kills the process -- an unrecoverable crash-loop on a fresh volume.
//
// "startup" is the only other value the manifest loader accepts; "off" is rejected with
//     patchReload must be "live" or "startup"
// It is also the correct one for a pod: live reload watches a developer's file edits, and a
// config change in a container means a new pod anyway.
const fs = require('fs');
const p = '/dsh-home/profiles/web/package.json';

if (!fs.existsSync(p)) {
  // Pre-create it so dsh never gets to write "live", rather than letting it crash once first.
  // Bundles match what dsh itself writes for the web profile.
  fs.writeFileSync(p, JSON.stringify({
    name: 'dsh-profile-web',
    private: true,
    dependencies: {},
    dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'],
                      patchReload: 'startup' } },
  }, null, 2));
  console.log('pre-created web profile manifest with patchReload=startup');
} else {
  const j = JSON.parse(fs.readFileSync(p, 'utf8'));
  const cur = j && j.dsh && j.dsh.profile ? j.dsh.profile.patchReload : undefined;
  if (cur === 'live') {
    j.dsh.profile.patchReload = 'startup';       // rewrite ONLY "live"; leave other edits alone
    fs.writeFileSync(p, JSON.stringify(j, null, 2));
    console.log('patchReload live -> startup');
  } else {
    console.log('patchReload already:', cur);
  }
}
