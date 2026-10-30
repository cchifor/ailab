#!/usr/bin/env bash
# Revoke the retired playground capability kid from ONE broker kid registry (plans/2026-10-30-retire-
# playground-plan.md, Phase 3). The work runs INSIDE the agentforge-provisioner pod, with its own OpenBao
# identity and the deployed image's _cas_remove_from_registry: see scripts/af_revoke_legacy_kid.py.
#
#   scripts/af-revoke-legacy-kid.sh <provider>/<account>            # dry run: pre-checks + key names
#   scripts/af-revoke-legacy-kid.sh <provider>/<account> --execute  # remove it
#
# One aud at a time (anthropic/claude-max-1, then anthropic/claude-max-2, then openai/codex-pro), and
# poll every broker replica's /internal/kid/<kid> until it reports served:false before the next one.
#
# Every run first does a DRY pass and saves the kid's PUBLIC record (the ROLLBACK line; public-key
# material only) under ${AF_REVOKE_ROLLBACK_DIR:-$HOME/.af-revoke}, outside the repo, and verifies the
# file before --execute is allowed to write. The execute pass's full output, including the provisioner
# audit record, is saved beside it whatever the outcome. Rollback = re-publish that record with
# agentforge's _cas_publish_to_registry.
set -euo pipefail

aud=${1:?usage: $0 <provider>/<account> [--execute]}
shift
execute=0
for a in "$@"; do
  case "$a" in
    --execute) execute=1 ;;
    *) echo "unknown argument '$a'" >&2; exit 2 ;;
  esac
done
case "$aud" in
  anthropic/claude-max-1|anthropic/claude-max-2|openai/codex-pro) ;;
  *) echo "refusing aud '$aud': tz-213512e80fde7c84 was granted only on claude-max-1, claude-max-2, codex-pro" >&2; exit 2 ;;
esac

# AF_REVOKE_KUBECTL exists only so scripts/tests can substitute a fake kubectl.
KUBECTL=("${AF_REVOKE_KUBECTL:-kubectl}" --context admin@ai -n openbao)
PY=$(command -v python || command -v python3)
here=$(cd "$(dirname "$0")" && pwd)
rollback_dir=${AF_REVOKE_ROLLBACK_DIR:-$HOME/.af-revoke}
stamp="$(date -u +%Y%m%dT%H%M%SZ)-${aud//\//_}"

run() {  # run the driver in the provisioner pod; extra args are appended
  "${KUBECTL[@]}" exec -i deploy/agentforge-provisioner -c provisioner -- python -u - \
    --aud "$aud" \
    --kid tz-213512e80fde7c84 \
    --expect-pk b864a0136bbc8d39 \
    --expect-tenant tenant-zero \
    --expect-workspace playground \
    --keep-kid cap-b5663179affeeaba5366c3ffd232681d \
    --keep-pk db3a6d0ec8e1d6a9 \
    "$@" < "$here/af_revoke_legacy_kid.py"
}

# 1. DRY pass: pre-checks, and the rollback record BEFORE anything can be written.
dry=$(run) || { rc=$?; printf '%s\n' "$dry"; exit "$rc"; }
printf '%s\n' "$dry" | grep -v '^ROLLBACK '
if ! printf '%s\n' "$dry" | grep -q '^ROLLBACK '; then
  exit 0  # already absent: nothing to save, nothing to do
fi
(umask 077; mkdir -p "$rollback_dir")
rb="$rollback_dir/$stamp.rollback.json"
(umask 077; printf '%s\n' "$dry" | sed -n 's/^ROLLBACK //p' > "$rb")
"$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['aud']==sys.argv[2] and d['kid']=='tz-213512e80fde7c84' and d['record']['public_key']" "$rb" "$aud" \
  || { echo "ABORT: rollback record $rb did not verify; nothing was written" >&2; exit 3; }
echo "rollback record (public key material only) saved and verified: $rb"
[ "$execute" = 1 ] || exit 0

# 2. EXECUTE pass. Its output is STREAMED into the log as it arrives (python -u in the pod, tee here),
# so an interrupted session still leaves every line it saw, including the audit record; kubectl's own
# exit status is kept through the pipe.
log="$rollback_dir/$stamp.execute.log"
(umask 077; : > "$log")
set +e
run --execute 2>&1 | tee -a "$log" | grep --line-buffered -v '^ROLLBACK '
rc=${PIPESTATUS[0]}
set -e
echo "execute output saved: $log (exit $rc)"
exit "$rc"
