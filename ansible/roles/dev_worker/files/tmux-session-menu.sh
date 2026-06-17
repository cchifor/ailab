#!/bin/bash
# Managed by ansible (role: dev_worker). Flat session picker for the status-left mouse binding.
set -euo pipefail
ARGS=()
while IFS= read -r name; do
  ARGS+=("$name" "" "switch-client -t '$name'")
done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)
[ "${#ARGS[@]}" -eq 0 ] && exit 0
exec tmux display-menu -T '#[align=centre]Sessions' -x M -y W "${ARGS[@]}"
