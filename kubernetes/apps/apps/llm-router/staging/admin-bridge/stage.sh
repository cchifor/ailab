#!/bin/sh
set -eu
umask 0027
release=/releases/router-0.1.0-20260927-admin-bridge
if [ -f "$release/RELEASE.json" ]; then
  node -e 'const r=require(process.argv[1]);if(r.commit!=="a2a3b20e4dd7fa7f0e1a2350d5fe3a5ca495d48a"||!r.backup||!r.smokePassed)process.exit(1)' "$release/RELEASE.json"
  exit 0
fi
# Recreate has stopped the previous router before this init container runs. The
# snapshot is taken before new code can open the live database. Reuse on retry.
node /stage-scripts/backup.mjs
cd /stage/source
export npm_config_cache=/tmp/npm-cache CI=true
npx --yes pnpm@10.32.1 install --frozen-lockfile --ignore-scripts --store-dir /tmp/pnpm-store
npx --yes pnpm@10.32.1 build
mkdir -p /tmp/router-release
cp -a dist package.json pnpm-lock.yaml /tmp/router-release/
cd /tmp/router-release
npx --yes pnpm@10.32.1 install --prod --frozen-lockfile --ignore-scripts --store-dir /tmp/pnpm-store
node /stage-scripts/smoke.mjs
# The live subPath appears only after the isolated smoke test succeeds. A retry
# never rewrites a released directory or overwrites the retained rollback release.
staged=$(mktemp -d /releases/.admin-bridge-XXXXXXXX)
cp -a /tmp/router-release/. "$staged/"
node -e 'require("node:fs").writeFileSync(process.argv[1],JSON.stringify({commit:"a2a3b20e4dd7fa7f0e1a2350d5fe3a5ca495d48a",backup:"/releases/.backups/20260927-pre-admin-bridge",smokePassed:true})+"\n")' "$staged/RELEASE.json"
test ! -e "$release"
mv "$staged" "$release"
echo 'Router release staged; backup and isolated smoke passed.'
