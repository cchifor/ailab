#!/bin/sh
# Does the experimental Agent Teams pair install and compose in THIS estate's layout?
#
#   docker run --rm -v "$PWD/scripts:/s:ro" mirror.gcr.io/library/node:22 sh /s/spike-dsh-agent-teams.sh
#
# Result: plans/2026-09-10-dsh-experimental-agent-teams-spike.md (GO).
#
# The two packages that the original agent-teams analysis MISSED:
#   @deepseek-ai/dsh-experimental-agent-team       roster, durable peer mailbox, shared task DAG
#   @deepseek-ai/dsh-experimental-tool-agent-team  the nine model-facing tools
# Neither declares dsh.bundle, so they mount as ordinary composition rows rather than joining
# dsh.profile.bundles -- which this script asserts, because getting that wrong would either
# crash-loop the pod or silently mount nothing.
set -eu
V=0.1.5-alpha.2; B=glibc
export DSH_HOME=/dsh-home npm_config_cache=/app/.npm
D="/app/${V}-${B}"; P="$DSH_HOME/profiles/web"
ok(){ printf '  PASS  %s\n' "$1"; }
no(){ printf '  FAIL  %s\n' "$1"; FAILED=1; }
FAILED=0

echo "== install dsh + init profile =="
mkdir -p "$D" "$DSH_HOME"
npm install --prefix "$D" --no-fund --no-audit "@deepseek-ai/dsh@${V}" >/dev/null 2>&1
corepack enable pnpm >/dev/null 2>&1 || true
(cd "$D" && ./node_modules/.bin/dsh --profile web --dump-config >/dev/null 2>&1) || true

echo "== install the team packages into the PROFILE (the path a row name resolves from) =="
# NO koffi HANDLING, deliberately. An earlier revision of this script appended an
# `onlyBuiltDependencies: [koffi]` block here, and a comment claiming dsh's
# initProfile already writes that key. BOTH WERE WRONG. Verified against a fresh
# profile: initProfile writes only `packages`, `nodeLinker: hoisted` and
# `autoInstallPeers: false` -- no allowlist at all. The key I saw was this
# script's own append, and the "duplicate mapping key" failure came from state
# left by a previous run.
#
# It is moot anyway: koffi arrives transitively via dsh-session-persistence-jsonl,
# which this profile does NOT need -- the web composition already ships it. With
# only the two team packages, koffi never appears, which the run below confirms
# by printing an empty "who needs koffi".
(cd "$P" && pnpm add \
  "@deepseek-ai/dsh-experimental-agent-team@${V}" \
  "@deepseek-ai/dsh-experimental-tool-agent-team@${V}" >/tmp/pn.log 2>&1) || true
# pnpm 12 raises ERR_PNPM_IGNORED_BUILDS AFTER completing the install, so a
# non-zero exit here does not mean the packages are absent. Judge by the tree.
grep -q ERR_PNPM_IGNORED_BUILDS /tmp/pn.log && echo "  note: ERR_PNPM_IGNORED_BUILDS (koffi native build skipped)"
for pkg in dsh-experimental-agent-team dsh-experimental-tool-agent-team; do
  [ -f "$P/node_modules/@deepseek-ai/$pkg/package.json" ] || { echo "  MISSING: $pkg"; exit 1; }
done
echo "  all three packages present despite the build gate"
echo "  installed: $(ls "$P/node_modules" | wc -l) top-level entries"
echo "  who needs koffi: $(cd "$P" && pnpm why koffi 2>/dev/null | grep -oE '@deepseek-ai/[a-z-]+' | sort -u | head -3 | tr '\n' ' ')"

echo "== did they join dsh.profile.bundles? (they should NOT -- no dsh.bundle) =="
node -e '
const m=require("/dsh-home/profiles/web/package.json");
const b=m.dsh.profile.bundles;
console.log("  bundles:", JSON.stringify(b));
console.log("  team pkg in bundles:", b.some(x=>x.includes("agent-team")));'

echo "== mount them via the USER PATCH (stand-in for a preset row) =="
cat > "$P/cordis.patch.yml" <<'PATCH'
# session-persistence-jsonl is ALREADY in the web composition (dsh-web-app ships
# it), so inserting it again is a duplicate loader entry id and fails the boot.
# The README's "smallest setup" assumes a bare composition; this one is not bare.
- insert:
    - id: agent-team
      name: '@deepseek-ai/dsh-experimental-agent-team'
      config:
        maxMembers: 4
        maxTasks: 64
    - id: tool-agent-team
      name: '@deepseek-ai/dsh-experimental-tool-agent-team'
      config:
        freshProvider: spawn
        forkProvider: fork
PATCH

echo "== resolve =="
cd "$D"
./node_modules/.bin/dsh --profile web --dump-config >/tmp/t.yml 2>/tmp/t.err && ok "dump-config exit 0" || no "dump-config failed"
[ -s /tmp/t.err ] && { echo "  stderr:"; head -5 /tmp/t.err; no "dump-config stderr non-empty"; } || ok "dump-config stderr EMPTY"
grep -q "agent-team" /tmp/t.yml && ok "team rows present in the resolved tree" || no "team rows absent"

echo "== BOOT =="
T0=$(date +%s); ST=0
timeout 40 ./node_modules/.bin/dsh web >/tmp/w.out 2>/tmp/w.err || ST=$?
EL=$(( $(date +%s) - T0 ))
if [ "$ST" = 124 ] && [ "$EL" -ge 38 ] && grep -q "dsh web: http" /tmp/w.out && [ ! -s /tmp/w.err ]; then
  ok "host booted and stayed up ${EL}s with Agent Teams mounted, stderr empty"
else
  no "boot: exit=$ST elapsed=${EL}s"; head -8 /tmp/w.err
fi
echo
# EXIT NON-ZERO ON FAILURE. Both branches used to end in a successful echo, so a
# failed resolve or boot still exited 0 and the documented docker invocation
# looked like a pass. A gate that cannot fail verifies nothing.
if [ "$FAILED" = 0 ]; then
  echo "TEAM SPIKE: GO"
else
  echo "TEAM SPIKE: NO-GO"
fi
exit "$FAILED"
