#!/bin/sh
# Reproduce the WP-3 profile-bundle spike end to end, in one container.
#
#   docker run --rm -v "$PWD/scripts:/s" mirror.gcr.io/library/node:22 sh /s/spike-dsh-profile-bundles.sh
#
# Result: plans/2026-09-10-wp3-profile-bundle-spike.md. This reproduces the layout
# dsh runs in production -- /app/<version>-<build> as the install tree (RWX nfs there),
# $DSH_HOME/profiles/web as the profile (RWO local-path there) -- and answers the
# question WP-3 turns on: can a bundle closure be built on the volume that HAS egress
# and projected onto the volume the profile lives on?
set -eu
DSH_VERSION=${DSH_VERSION:-0.1.5-alpha.2}
DSH_BUILD=${DSH_BUILD:-glibc}
export DSH_HOME=${DSH_HOME:-/dsh-home}
export npm_config_cache=/app/.npm
D="/app/${DSH_VERSION}-${DSH_BUILD}"
PROF="$DSH_HOME/profiles/web"
STAGE="/app/profiles/${DSH_VERSION}-${DSH_BUILD}-ps1/web"
BUNDLES="@deepseek-ai/dsh-subagent-codex@${DSH_VERSION} @deepseek-ai/dsh-subagent-claude-code@${DSH_VERSION}"
ok() { printf '  PASS  %s\n' "$1"; }
no() { printf '  FAIL  %s\n' "$1"; FAILED=1; }
FAILED=0

echo "== 1. install dsh and init the web profile =="
mkdir -p "$D" "$DSH_HOME"
npm install --prefix "$D" --no-fund --no-audit "@deepseek-ai/dsh@${DSH_VERSION}" >/tmp/npm.log 2>&1 \
  || { tail -20 /tmp/npm.log; exit 1; }
corepack enable pnpm >/dev/null 2>&1 || true
(cd "$D" && ./node_modules/.bin/dsh --profile web --dump-config >/dev/null 2>&1) || true
test -f "$PROF/package.json" || { echo "profile never initialised"; exit 1; }
echo "  dsh $("$D/node_modules/.bin/dsh" --version 2>&1 | head -1), pnpm $(pnpm --version)"

echo "== 2. F3a: does a declared-but-absent bundle crash the host? =="
cp "$PROF/package.json" /tmp/pkg.bak
node -e 'const f=require("fs"),p=process.argv[1],j=JSON.parse(f.readFileSync(p,"utf8"));
j.dsh.profile.bundles.push("@deepseek-ai/dsh-subagent-codex");f.writeFileSync(p,JSON.stringify(j,null,2));' "$PROF/package.json"
if (cd "$D" && timeout 25 ./node_modules/.bin/dsh web >/dev/null 2>/tmp/f3a.err); then
  no "a missing bundle did NOT crash the host -- WP-3's rules 1-5 rest on it doing so"
else
  grep -q "cannot resolve profile bundle" /tmp/f3a.err \
    && ok "missing bundle crashes at boot (unhandled throw in resolveBundleDir)" \
    || no "host failed, but not for the expected reason: $(head -2 /tmp/f3a.err)"
fi
cp /tmp/pkg.bak "$PROF/package.json"

echo "== 3. Option B: build the closure on /app, project it onto the profile =="
rm -rf "$PROF/node_modules" "$PROF/pnpm-lock.yaml" "$STAGE"
mkdir -p "$STAGE"
cp "$PROF/package.json" "$STAGE/package.json"
# VERBATIM: this file carries nodeLinker: hoisted AND minimumReleaseAgeExclude, which
# pnpm needs to accept the -alpha bundles. Regenerating it instead works today and
# breaks on the next alpha bump.
cp "$PROF/pnpm-workspace.yaml" "$STAGE/pnpm-workspace.yaml"
# shellcheck disable=SC2086
(cd "$STAGE" && pnpm add $BUNDLES >/tmp/pnpm.log 2>&1) || { tail -20 /tmp/pnpm.log; exit 1; }
cp -a "$STAGE/node_modules" "$PROF/node_modules"
# DECLARATIVE (WP-3 rule 4): set, never merge; only names verified present in the closure.
node -e '
const f=require("fs"),p=process.argv[1],nm=process.argv[2];
const want=["@deepseek-ai/dsh-subagent-codex","@deepseek-ai/dsh-subagent-claude-code"];
const have=want.filter(n=>f.existsSync(nm+"/"+n+"/package.json"));
const j=JSON.parse(f.readFileSync(p,"utf8"));
j.dsh.profile.bundles=["@deepseek-ai/dsh-base","@deepseek-ai/dsh-web-app"].concat(have);
f.writeFileSync(p,JSON.stringify(j,null,2));
console.log("  declared "+want.length+", present "+have.length);' "$PROF/package.json" "$PROF/node_modules"
echo "  closure: $(ls "$PROF/node_modules" | wc -l) entries, $(du -sh "$PROF/node_modules" | cut -f1)"

echo "== 4. assertions =="
(cd "$D" && ./node_modules/.bin/dsh --profile web --dump-config >/tmp/dump.yml 2>/tmp/dump.err) \
  && [ ! -s /tmp/dump.err ] \
  && ok "dump-config exit 0 with EMPTY stderr (loader errors go to stderr while exiting 0)" \
  || no "dump-config: exit/stderr not clean -- $(head -3 /tmp/dump.err)"
grep -q 'subagent-codex' /tmp/dump.yml && grep -q 'subagent-claude-code' /tmp/dump.yml \
  && ok "both provider rows resolved" || no "provider rows missing from the resolved tree"
if (cd "$D" && timeout 30 ./node_modules/.bin/dsh web >/tmp/web.out 2>/tmp/web.err); then
  no "host exited on its own -- it should stay up"
else
  [ "$?" = 124 ] || true
  grep -q "dsh web: http" /tmp/web.out && [ ! -s /tmp/web.err ] \
    && ok "host BOOTED and stayed up, stderr empty" \
    || no "host did not boot cleanly: $(head -3 /tmp/web.err)"
fi
DUPES=0
for pkg in @deepseek-ai/cordis @deepseek-ai/dsh-subagent @deepseek-ai/dsh-llm @deepseek-ai/dsh-session; do
  n=$(find "$D/node_modules/$pkg" "$PROF/node_modules/$pkg" -maxdepth 0 2>/dev/null | wc -l)
  [ "$n" -gt 1 ] && { DUPES=1; echo "    $pkg has $n copies"; }
done
[ "$DUPES" = 0 ] && ok "single on-disk instance of every shared framework peer" \
                 || no "a shared peer is duplicated -- a provider may register into a registry nobody reads"
CODEX=$(find "$PROF/node_modules/@openai" -type f -name codex -perm -u+x 2>/dev/null | head -1)
CLAUDE="$PROF/node_modules/@anthropic-ai/claude-agent-sdk-linux-x64/claude"
[ -n "$CODEX" ] && timeout 20 "$CODEX" --version >/dev/null 2>&1 \
  && ok "codex payload executes ($(timeout 20 "$CODEX" --version 2>&1 | head -1))" \
  || no "codex payload missing or not executable"
[ -x "$CLAUDE" ] && timeout 30 "$CLAUDE" --version >/dev/null 2>&1 \
  && ok "claude payload executes ($(timeout 30 "$CLAUDE" --version 2>&1 | head -1))" \
  || no "claude payload missing or not executable"

echo
[ "$FAILED" = 0 ] && echo "SPIKE: GO" || echo "SPIKE: NO-GO"
exit "$FAILED"
