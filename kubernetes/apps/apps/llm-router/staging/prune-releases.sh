#!/bin/sh
# Prunes router-releases, run INSIDE the staging pod (router-stage.yaml):
#   kubectl -n llm-router cp prune-releases.sh router-stage:/tmp/prune.sh
#   kubectl -n llm-router exec router-stage -- sh /tmp/prune.sh <live-release> <rollback-release> [--apply]
#
# Keeps the two named release directories and the newest KEEP_BACKUPS database backups; removes every other
# router-* release directory and garbage-collects the pnpm store (packages no kept release uses). Dry run unless
# --apply is given: it lists what it would remove first. The live release can never be removed because it must be
# named as one of the two kept; read it from the Deployment first:
#   kubectl -n llm-router get deploy llm-router -o jsonpath='{.spec.template.spec.containers[0].volumeMounts[?(@.name=="releases")].subPath}'
set -eu
USAGE="usage: prune-releases.sh <live-release> <rollback-release> [--apply]"
# The kept names are compared exactly with the directory names below, so they are normalised and checked first:
# a trailing slash (tab completion) or a path would otherwise pass an existence check yet match nothing, and
# --apply would delete the very release meant to be kept.
keep_name() {
  n=$(printf '%s' "$1" | sed 's#/*$##')
  case "$n" in
    router-*) ;;
    *) echo "refusing: '$1' is not a release directory name (router-...)" >&2; exit 1 ;;
  esac
  case "$n" in */*|*..*|*' '*) echo "refusing: '$1' must be a bare directory name, not a path" >&2; exit 1 ;; esac
  printf '%s' "$n"
}
LIVE=$(keep_name "${1:?$USAGE}")
ROLLBACK=$(keep_name "${2:?$USAGE}")
APPLY="${3:-}"
case "$APPLY" in ""|--apply) ;; *) echo "$USAGE" >&2; exit 1 ;; esac
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"
ROOT=/releases
cd "$ROOT"
for keep in "$LIVE" "$ROLLBACK"; do
  [ -d "$ROOT/$keep" ] || { echo "refusing: $ROOT/$keep does not exist (typo?)" >&2; exit 1; }
done

remove=""
for d in router-*; do
  [ -d "$d" ] || continue
  [ "$d" = "$LIVE" ] || [ "$d" = "$ROLLBACK" ] || remove="$remove $d"
done
# Belt and braces: both kept releases must still be present after the selection.
for keep in "$LIVE" "$ROLLBACK"; do
  case " $remove " in *" $keep "*) echo "refusing: $keep was selected for removal" >&2; exit 1 ;; esac
done
old_backups=""
if [ -d .backups ]; then
  old_backups=$(ls -1t .backups | tail -n +"$((KEEP_BACKUPS + 1))" | sed 's#^#.backups/#' | tr '\n' ' ')
fi

echo "keep:    $LIVE $ROLLBACK (and the newest $KEEP_BACKUPS backups)"
echo "remove: ${remove:- (no releases)} ${old_backups:-(no backups)}"
du -sh "$ROOT" 2>/dev/null | sed 's/^/before: /'
if [ "$APPLY" != "--apply" ]; then echo "dry run: add --apply to remove"; exit 0; fi

for d in $remove $old_backups; do rm -rf "${ROOT:?}/$d"; done
# Drop store packages that no kept release links to.
npx -y pnpm@10.32.1 store prune --store-dir "$ROOT/.pnpm-store" >/dev/null 2>&1 || echo "pnpm store prune failed (the releases are still pruned)" >&2
du -sh "$ROOT" 2>/dev/null | sed 's/^/after:  /'
