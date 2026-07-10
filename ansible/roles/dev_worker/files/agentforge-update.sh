#!/bin/bash
# Managed by ansible (role: dev_worker). AgentForge atomic self-update (ADR 0018).
#
# Fired every 2 min by agentforge-update.timer (as root). Contract (plans/2026-07-10-agentforge-plan.md):
#   flock singleton -> read the `release` + `release_sha256` pin from the config repo's
#   agentforge.json -> no-op if `current` already serves the pin -> download the release tarball
#   from the Gitea generic package registry -> sha256 verify -> extract to releases/<ver> ->
#   `uv sync --frozen` -> flip the `current` symlink ATOMICALLY -> restart agentforge -> poll
#   /healthz (<=60s) requiring version == pin AND a changed MainPID (the endpoint serves the
#   RUNNING build's version, so the check can never pass against the old in-memory process) ->
#   on ANY failure flip the symlink back, restart onto the previous release, verify IT healthy,
#   beacon + exit 1. Keep the last 3 releases.
#
# A failed pin is re-attempted on every tick until the operator reverts it (deliberate: transient
# failures self-heal; a truly broken pin is loud — one rollback beacon per tick — and `current`
# never breaks). uv is on PATH via the unit's Environment= (the agent user's pipx ~/.local/bin).
set -euo pipefail

ENV_FILE=/etc/agentforge/agentforge.env
BASE_DIR=/opt/agentforge
RELEASES_DIR="$BASE_DIR/releases"
CURRENT_LINK="$BASE_DIR/current"
LOCK_FILE=/run/lock/agentforge-update.lock
KEEP_RELEASES=3
HEALTH_TIMEOUT=60

log() { echo "agentforge-update: $*"; }
beacon() { logger -t agentforge-update -- "$*"; log "$*"; }

# ---- singleton (the timer can tick while a slow update runs) ----
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another update is in flight; skipping this tick"
  exit 0
fi

# ---- config (secrets + endpoints from the SOPS-fed env file) ----
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a
: "${AF_GITEA_URL:?}" "${AF_CONFIG_REPO:?}" "${AF_PORT:?}" "${AF_BOT_TOKEN_PLANNER:?}"
AUTH=(-H "Authorization: token $AF_BOT_TOKEN_PLANNER") # any bot PAT reads the config repo
OWNER="${AF_CONFIG_REPO%%/*}"

# ---- read the pin ----
config_json="$(curl -fsSL --max-time 30 "${AUTH[@]}" \
  "$AF_GITEA_URL/api/v1/repos/$AF_CONFIG_REPO/raw/agentforge.json")"
pinned="$(jq -r '.release // empty' <<<"$config_json")"
pinned_sha="$(jq -r '.release_sha256 // empty' <<<"$config_json")"
if [ -z "$pinned" ] || [ -z "$pinned_sha" ]; then
  beacon "config agentforge.json has no release/release_sha256 pin; nothing to do"
  exit 0
fi

current=""
if [ -L "$CURRENT_LINK" ]; then
  current="$(basename "$(readlink -f "$CURRENT_LINK")")"
fi
if [ "$pinned" = "$current" ]; then
  exit 0
fi
log "update: '$current' -> '$pinned'"

# ---- download + verify (Gitea generic package registry; releases are immutable) ----
tmp="$(mktemp -d /tmp/agentforge-update.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT
tarball="$tmp/agentforge-$pinned.tar.gz"
curl -fsSL --max-time 300 "${AUTH[@]}" -o "$tarball" \
  "$AF_GITEA_URL/api/packages/$OWNER/generic/agentforge/$pinned/agentforge-$pinned.tar.gz"
echo "$pinned_sha  $tarball" | sha256sum -c --quiet

# ---- extract + build the venv in the versioned dir ----
dest="$RELEASES_DIR/$pinned"
rm -rf "$dest" # clear any half-extracted leftover from a previously failed attempt
mkdir -p "$dest"
# release.yml packs the repo under a single top-level agentforge-<ver>/ prefix
tar -xzf "$tarball" -C "$dest" --strip-components=1
(cd "$dest" && uv sync --frozen)
# the updater runs as root; the service runs as the agent user (owner of /opt/agentforge)
chown -R "$(stat -c '%U:%G' "$BASE_DIR")" "$dest"

# ---- health check helper: version must match AND the process must be the restarted one ----
poll_healthy() { # poll_healthy <expected-version> <old-main-pid>
  local expected="$1" old_pid="$2" waited=0 pid version
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    sleep 2
    waited=$((waited + 2))
    pid="$(systemctl show -p MainPID --value agentforge.service)"
    if [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "$old_pid" ]; then
      version="$(curl -fsS --max-time 5 "http://localhost:$AF_PORT/healthz" | jq -r '.version // empty' || true)"
      if [ "$version" = "$expected" ]; then
        return 0
      fi
    fi
  done
  return 1
}

flip_current() { # flip_current <target-dir> — atomic via rename over the existing symlink
  ln -sfn "$1" "$CURRENT_LINK.tmp"
  mv -Tf "$CURRENT_LINK.tmp" "$CURRENT_LINK"
}

# ---- flip + restart + verify; roll back onto the previous release on any failure ----
prev_target=""
if [ -n "$current" ]; then
  prev_target="$RELEASES_DIR/$current"
fi
old_pid="$(systemctl show -p MainPID --value agentforge.service)"
flip_current "$dest"
systemctl restart agentforge.service || true
if poll_healthy "$pinned" "$old_pid"; then
  log "release $pinned healthy (version-matched, new MainPID)"
else
  if [ -n "$prev_target" ]; then
    failed_pid="$(systemctl show -p MainPID --value agentforge.service)"
    flip_current "$prev_target"
    systemctl restart agentforge.service || true
    if poll_healthy "$current" "$failed_pid"; then
      beacon "ROLLBACK: release $pinned failed its health check; reverted to $current (healthy)"
    else
      beacon "ROLLBACK FAILED: release $pinned unhealthy AND previous $current did not come back — operator attention required"
    fi
  else
    # first install failed: nothing to roll back to — remove the link + stop the crash loop so
    # the next tick retries cleanly
    rm -f "$CURRENT_LINK"
    systemctl stop agentforge.service || true
    beacon "FIRST INSTALL FAILED: release $pinned failed its health check; no previous release to roll back to"
  fi
  exit 1
fi

# ---- prune: keep the newest KEEP_RELEASES, never the one `current` points at ----
n=0
while IFS= read -r rel; do
  n=$((n + 1))
  [ "$n" -le "$KEEP_RELEASES" ] && continue
  [ "$rel" = "$pinned" ] && continue
  rm -rf "${RELEASES_DIR:?}/${rel}"
  log "pruned old release $rel"
done < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@\t%f\n' | sort -rn | cut -f2-)

exit 0
