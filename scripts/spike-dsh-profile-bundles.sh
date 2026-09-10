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
# The exit status IS the assertion: only 124 (killed by `timeout`) proves the host
# was still running when the clock ran out. Printing the launch line and then
# crashing with empty stderr looks identical in the output files, so an earlier
# version of this check -- which discarded the status with `|| true` -- reported
# "stayed up" for exactly the failure it existed to catch.
BOOT_STATUS=0
BOOT_T0=$(date +%s)
(cd "$D" && timeout 30 ./node_modules/.bin/dsh web >/tmp/web.out 2>/tmp/web.err) || BOOT_STATUS=$?
BOOT_ELAPSED=$(( $(date +%s) - BOOT_T0 ))
# 124 alone is NOT proof: `timeout` forwards a child's own exit status, so a host
# that printed its launch line and then called exit(124) is indistinguishable by
# status. Elapsed time is the independent evidence -- a process killed AT the
# deadline ran for the full window; one that exited early did not.
if [ "$BOOT_STATUS" = 124 ] && [ "$BOOT_ELAPSED" -ge 28 ] \
   && grep -q "dsh web: http" /tmp/web.out && [ ! -s /tmp/web.err ]; then
  ok "host BOOTED and stayed up ${BOOT_ELAPSED}s until killed by timeout, stderr empty"
elif [ "$BOOT_STATUS" = 0 ]; then
  no "host exited 0 on its own -- it should still be serving"
elif [ "$BOOT_STATUS" = 124 ]; then
  no "exit 124 after only ${BOOT_ELAPSED}s -- the host returned that itself, it was not killed"
else
  no "host did not stay up: exit=$BOOT_STATUS after ${BOOT_ELAPSED}s $(head -3 /tmp/web.err)"
fi
# RECURSIVE, and exactly-one. Two earlier bugs lived here. Naming the two
# top-level paths with -maxdepth 0 cannot see a nested node_modules, which is
# precisely where a hoisted tree puts a second copy of a conflicting version. And
# testing only `> 1` let ZERO pass -- a peer that resolved into neither tree was
# reported as "single instance". Realpaths are deduplicated because a hoisted
# tree still uses a few symlinks, and two names for one directory are one copy.
PEERS_OK=1
for pkg in @deepseek-ai/cordis @deepseek-ai/dsh-subagent @deepseek-ai/dsh-llm @deepseek-ai/dsh-session; do
  owner=$(basename "$pkg"); scope=$(dirname "$pkg")
  # -type d OR -type l: a hoisted tree still uses a few symlinks, and `-type d`
  # alone silently skips a peer reached through one -- which would count a real
  # duplicate as a single instance. readlink -f then collapses the two names for
  # one directory back to one entry, so a link and its target never inflate n.
  #
  # find's status is CHECKED rather than discarded: an unreadable subtree could
  # otherwise hide a second copy while the pipeline still reported success,
  # because a pipeline's status is `wc`'s and dash has no pipefail.
  if ! find "$D/node_modules" "$PROF/node_modules" \
        \( -type d -o -type l \) -path "*/$scope/$owner" \
        -exec readlink -f {} \; > /tmp/peer.raw 2>/tmp/peer.err; then
    PEERS_OK=0; echo "    $pkg: search FAILED -- $(head -1 /tmp/peer.err)"; continue
  fi
  sort -u /tmp/peer.raw > /tmp/peer.uniq
  n=$(wc -l < /tmp/peer.uniq)
  if [ "$n" -ne 1 ]; then
    PEERS_OK=0
    [ "$n" = 0 ] && echo "    $pkg: NOT FOUND in either tree" \
                 || { echo "    $pkg: $n distinct copies --"; sed "s/^/      /" /tmp/peer.uniq; }
  fi
done
[ "$PEERS_OK" = 1 ] && ok "exactly one on-disk instance of every shared framework peer" \
                    || no "a shared peer is missing or duplicated -- a provider may register into a registry nobody reads"
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
