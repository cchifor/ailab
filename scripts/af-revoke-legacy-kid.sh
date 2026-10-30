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
# On --execute the removed PUBLIC record is saved under ${AF_REVOKE_ROLLBACK_DIR:-$HOME/.af-revoke},
# outside the repo; it is the input to a rollback via _cas_publish_to_registry.
set -euo pipefail

aud=${1:?usage: $0 <provider>/<account> [--execute]}
shift
case "$aud" in
  anthropic/claude-max-1|anthropic/claude-max-2|openai/codex-pro) ;;
  *) echo "refusing aud '$aud': tz-213512e80fde7c84 was granted only on claude-max-1, claude-max-2, codex-pro" >&2; exit 2 ;;
esac

KUBECTL=(kubectl --context admin@ai -n openbao)
here=$(cd "$(dirname "$0")" && pwd)
rollback_dir=${AF_REVOKE_ROLLBACK_DIR:-$HOME/.af-revoke}

out=$("${KUBECTL[@]}" exec -i deploy/agentforge-provisioner -c provisioner -- python - \
  --aud "$aud" \
  --kid tz-213512e80fde7c84 \
  --expect-pk b864a0136bbc8d39 \
  --expect-tenant tenant-zero \
  --expect-workspace playground \
  --keep-kid cap-b5663179affeeaba5366c3ffd232681d \
  --keep-pk db3a6d0ec8e1d6a9 \
  "$@" < "$here/af_revoke_legacy_kid.py") || { rc=$?; printf '%s\n' "${out:-}" | grep -v '^ROLLBACK ' || true; exit "$rc"; }

if printf '%s\n' "$out" | grep -q '^ROLLBACK '; then
  (umask 077; mkdir -p "$rollback_dir")
  f="$rollback_dir/$(date -u +%Y%m%dT%H%M%SZ)-${aud//\//_}.json"
  (umask 077; printf '%s\n' "$out" | sed -n 's/^ROLLBACK //p' > "$f")
  echo "rollback record (public key material only) saved to $f"
fi
printf '%s\n' "$out" | grep -v '^ROLLBACK '
