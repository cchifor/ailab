#!/usr/bin/env bash
# Tests for files/agent-dirs-migrate and templates/profile.d-02-dev-worker-agent-tmp.sh.j2 (tasks/agent_dirs.yml,
# W4 of plans/2026-10-09-dev-worker-disk-hardening-plan.md). Hermetic: temp dirs only, no root needed.
set -euo pipefail

role=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
migrate="$role/files/agent-dirs-migrate"
tmpl="$role/templates/profile.d-02-dev-worker-agent-tmp.sh.j2"
t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
export DW_AGENT_DIRS_BUSY_CHECK=0
# A docker that reports no containers, so the result never depends on the machine running the test
# (a real docker whose socket this user cannot open now counts as busy, by design).
mkdir -p "$t/nodocker"
printf '#!/bin/bash\nexit 0\n' >"$t/nodocker/docker"
chmod +x "$t/nodocker/docker"
export PATH="$t/nodocker:$PATH"

# 1. A real ~/.cache (Playwright browsers + a uv cache) and ~/.npm become symlinks onto the workspace;
#    the browsers are carried over, everything else repopulates.
home=$t/home
root=$t/ws/u
mkdir -p "$home/.cache/ms-playwright/chromium-1" "$home/.cache/uv/x" "$home/.npm/_cacache" "$root"
echo bin >"$home/.cache/ms-playwright/chromium-1/chrome"
echo y >"$home/.cache/uv/x/f"
echo z >"$home/.npm/_cacache/c"
out=$(bash "$migrate" "$home" "$root")
[ -L "$home/.cache" ] || fail "~/.cache is not a symlink"
[ "$(readlink -f "$home/.cache")" = "$(readlink -f "$root/.cache")" ] || fail "~/.cache points elsewhere"
[ -L "$home/.npm" ] || fail "~/.npm is not a symlink"
[ "$(cat "$root/.cache/ms-playwright/chromium-1/chrome")" = bin ] || fail "Playwright browsers not carried over"
[ ! -e "$root/.cache/uv/x/f" ] || fail "the uv cache was copied; caches repopulate"
[ ! -e "$home/.cache.pre-workspace" ] && [ ! -e "$home/.npm.pre-workspace" ] || fail "old dirs left behind"
grep -q "moved $home/.cache" <<<"$out" || fail "no 'moved' line: $out"

# 2. Idempotent: a second run changes nothing and says nothing.
out=$(bash "$migrate" "$home" "$root")
[ -z "$out" ] || fail "second run was not a no-op: $out"

# 3. A symlink to somewhere else is reported and left alone.
mkdir -p "$t/home2" "$t/elsewhere"
ln -s "$t/elsewhere" "$t/home2/.cache"
out=$(bash "$migrate" "$t/home2" "$root")
grep -q "skip $t/home2/.cache" <<<"$out" || fail "a foreign symlink was not reported: $out"
[ "$(readlink "$t/home2/.cache")" = "$t/elsewhere" ] || fail "a foreign symlink was changed"

# 4. No ~/.cache yet: just the symlink.
mkdir -p "$t/home3"
bash "$migrate" "$t/home3" "$root" >/dev/null
[ -L "$t/home3/.cache" ] || fail "a fresh home got no symlink"

# 5. No workspace directory for the user: nothing happens.
mkdir -p "$t/home4/.cache"
out=$(bash "$migrate" "$t/home4" "$t/nope")
grep -q "does not exist" <<<"$out" || fail "a missing workspace root was not reported: $out"
[ -d "$t/home4/.cache" ] && [ ! -L "$t/home4/.cache" ] || fail "~/.cache was touched without a workspace root"

# 6. Something of this user is using the caches, however it was started: the run is skipped.
#    By name (npm, `python -m pip`, Playwright's node CLI) and by use (a cwd inside ~/.cache).
busy_case() { # NAME ARGV...: run ARGV in the background, expect the migration of $BUSY_HOME to skip
	local name=$1 pid out h=${BUSY_HOME:-$t/home5}
	shift
	mkdir -p "$h/.cache/uv"
	"$@" &
	pid=$!
	sleep 0.3
	out=$(DW_AGENT_DIRS_BUSY_CHECK=1 bash "$migrate" "$h" "$root")
	kill "$pid" 2>/dev/null || :
	wait "$pid" 2>/dev/null || :
	grep -q "skip: busy" <<<"$out" || fail "$name did not make it skip: $out"
	[ ! -L "$h/.cache" ] || fail "moved while $name was running"
}
mkdir -p "$t/bin" "$t/proj/node_modules/playwright"
printf '#!/bin/bash\nsleep 30\n' >"$t/bin/python3"
printf '#!/bin/bash\nsleep 30\n' >"$t/bin/node"
chmod +x "$t/bin/python3" "$t/bin/node"
if command -v pgrep >/dev/null; then
	busy_case "npm" bash -c 'exec -a npm sleep 30'
	busy_case "python -m pip" "$t/bin/python3" -m pip install x
	busy_case "node playwright/cli.js" "$t/bin/node" "$t/proj/node_modules/playwright/cli.js" install
fi
busy_case "a shell inside ~/.cache" bash -c "cd '$t/home5/.cache/uv' && sleep 30"
# ...and an unrelated process of the same user does not block it.
(cd "$t" && sleep 30) &
idle_pid=$!
out=$(DW_AGENT_DIRS_BUSY_CHECK=1 bash "$migrate" "$t/home5" "$root")
kill "$idle_pid" 2>/dev/null || :
grep -q "moved $t/home5/.cache" <<<"$out" || fail "an unrelated process blocked the move: $out"

# 6b. A running container that bind-mounts the cache blocks the move; one mounting elsewhere does not.
#     (Processes INSIDE containers run in another mount namespace and are ignored by the use/name
#     checks: their /home/appuser/.cache is not this ~/.cache.) A stub `docker` stands in.
mkdir -p "$t/stub" "$t/home6/.cache"
cat >"$t/stub/docker" <<STUB
#!/bin/bash
case "\$1" in
ps) echo abc123 ;;
inspect) printf '%s\n' "\$DW_STUB_MOUNT" ;;
esac
STUB
chmod +x "$t/stub/docker"
out=$(PATH="$t/stub:$PATH" DW_STUB_MOUNT="$t/home6/.cache/uv" DW_AGENT_DIRS_BUSY_CHECK=1 bash "$migrate" "$t/home6" "$root")
grep -q "skip: busy (a running container bind-mounts" <<<"$out" || fail "a container mounting the cache did not block: $out"
[ ! -L "$t/home6/.cache" ] || fail "moved under a container's bind mount"
for src in "$t/home6" "$t" "/"; do # an ANCESTOR of the cache mounted into a container also blocks
	out=$(PATH="$t/stub:$PATH" DW_STUB_MOUNT="$src" DW_AGENT_DIRS_BUSY_CHECK=1 bash "$migrate" "$t/home6" "$root")
	grep -q "skip: busy (a running container bind-mounts" <<<"$out" || fail "a container mounting $src did not block: $out"
done
mkdir -p "$t/home6x/.cache"
out=$(PATH="$t/stub:$PATH" DW_STUB_MOUNT="$t/home6x/.cachex" DW_AGENT_DIRS_BUSY_CHECK=1 bash "$migrate" "$t/home6x" "$root")
grep -q "moved $t/home6x/.cache" <<<"$out" || fail "a sibling with the cache's name as prefix blocked: $out"
out=$(PATH="$t/stub:$PATH" DW_STUB_MOUNT="/srv/data" DW_AGENT_DIRS_BUSY_CHECK=1 bash "$migrate" "$t/home6" "$root")
grep -q "moved $t/home6/.cache" <<<"$out" || fail "an unrelated container mount blocked the move: $out"

# A docker that cannot be asked (daemon down, socket denied) is busy, never "no containers".
cat >"$t/stub/docker" <<'STUB'
#!/bin/bash
exit 1
STUB
mkdir -p "$t/home6b/.cache"
out=$(PATH="$t/stub:$PATH" DW_AGENT_DIRS_BUSY_CHECK=1 bash "$migrate" "$t/home6b" "$root")
grep -q "skip: busy (cannot ask docker" <<<"$out" || fail "a failing docker did not count as busy: $out"
[ ! -L "$t/home6b/.cache" ] || fail "moved although docker could not be asked"

# 6c. A process in ANOTHER mount namespace that still shares the host cache (unshare -m) blocks it:
#     another namespace is not proof of isolation. Needs unprivileged user+mount namespaces.
if unshare -rm true 2>/dev/null; then
	mkdir -p "$t/home7/.cache/uv"
	BUSY_HOME="$t/home7" busy_case "unshare -m inside ~/.cache" unshare -rm bash -c "cd '$t/home7/.cache/uv' && sleep 30"
fi

# 7. The TMPDIR hook: set only for an existing, real, writable directory.
hook=$t/hook.sh
sed "s#{{ dev_worker_workspace_mount }}#$t/ws#g" "$tmpl" >"$hook"
mkdir -p "$t/ws/u/.tmp"
got=$(USER=u bash -c ". '$hook'; echo \"\${TMPDIR:-unset}|\${__dw_tmp:-gone}\"")
[ "$got" = "$t/ws/u/.tmp|gone" ] || fail "TMPDIR not set for an existing dir (or helper leaked): $got"
got=$(USER=nobody-here bash -c ". '$hook'; echo \"\${TMPDIR:-unset}\"")
[ "$got" = unset ] || fail "TMPDIR set for a missing dir: $got"
mkdir -p "$t/ws/v" "$t/target"
ln -s "$t/target" "$t/ws/v/.tmp"
got=$(USER=v bash -c ". '$hook'; echo \"\${TMPDIR:-unset}\"")
[ "$got" = unset ] || fail "TMPDIR set for a symlinked dir: $got"

echo "agent-dirs: all tests passed"
