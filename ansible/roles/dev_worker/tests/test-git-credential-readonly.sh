#!/usr/bin/env bash
# Drives real git against files/git-credential-store-readonly in a throwaway HOME: `fill` returns
# the stored line, while `reject` (what git runs on a 401) and `approve` (what it runs on success)
# leave ~/.git-credentials byte-identical. A control run with the stock `store` helper and the SAME
# reject input must erase the line, which proves this test can see the bug it guards. Then the
# task's own migration script (cut out of tasks/git_credential_helper.yml): `store` or nothing is
# replaced, a re-run changes nothing, and a hand-added helper stops it. Then the wiring.
# CI: .gitea/workflows/dev-worker-scripts.yaml.
set -euo pipefail
ROLE="$(cd "$(dirname "$0")/.." && pwd)"
TASKS="$ROLE/tasks"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
fails=0
bad() { echo "FAIL: $*" >&2; fails=$((fails + 1)); }

# Nothing from the caller's git setup may reach these runs: no system config, no config passed in
# the environment, no askpass, and no repository around the working directory.
unset GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM GIT_ASKPASS SSH_ASKPASS \
      GIT_DIR GIT_WORK_TREE
export HOME="$work/home" XDG_CONFIG_HOME="$work/home/.config" GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
mkdir -p "$HOME"
cd "$work"

# A copy with the exec bit, as the copy task installs it: git runs an absolute-path helper directly,
# and the repo stores role files 0644.
HELPER="$work/bin/git-credential-store-readonly"
mkdir -p "$work/bin"
cp "$ROLE/files/git-credential-store-readonly" "$HELPER"
chmod 0755 "$HELPER"

creds="$HOME/.git-credentials"
seed() { printf '%s\n' 'https://dev-worker-bot:tok_first@git.example' > "$creds"; cp "$creds" "$work/expected"; }
use() { git config --global --replace-all credential.helper "$1"; }
query() { printf 'protocol=https\nhost=git.example\n\n'; }
# What git sends on reject/approve: the full credential, including the password it used.
cred() { printf 'protocol=https\nhost=git.example\nusername=dev-worker-bot\npassword=%s\n\n' "$1"; }
fill() { query | git credential fill 2>/dev/null; }

seed; use store
cred tok_first | git credential reject
[ ! -s "$creds" ] || bad "control: 'store' did not erase on reject, so this test cannot see the bug"

seed; use "$HELPER"
out="$(fill)" || bad "fill failed with the read-only helper"
printf '%s\n' "$out" | grep -qx 'username=dev-worker-bot' || bad "fill returned no username"
printf '%s\n' "$out" | grep -qx 'password=tok_first' || bad "fill returned the wrong password"

cred tok_first | git credential reject || bad "reject exited non-zero"
cmp -s "$creds" "$work/expected" || bad "reject changed ~/.git-credentials"

cred tok_other | git credential approve || bad "approve exited non-zero"
cmp -s "$creds" "$work/expected" || bad "approve changed ~/.git-credentials"

out="$(fill)" || bad "fill failed after reject + approve"
printf '%s\n' "$out" | grep -qx 'password=tok_first' || bad "fill after reject + approve returned the wrong password"

# A host with no stored line: fill fails cleanly instead of prompting or inventing a password.
if printf 'protocol=https\nhost=elsewhere.example\n\n' | git credential fill >"$work/none" 2>/dev/null; then
  bad "fill for an unknown host succeeded"
fi
if grep -q '^password=' "$work/none"; then bad "fill for an unknown host printed a password"; fi

# The task's migration script, run as written. The block scalar sits between the shell line and
# `args:`, indented four spaces.
sed -n '/ansible.builtin.shell: |/,/^  args:/p' "$TASKS/git_credential_helper.yml" | sed '1d;$d' | sed 's/^    //' > "$work/migrate.sh"
want="$(sed -n 's/^want=\(.*\)$/\1/p' "$work/migrate.sh")"
[ -n "$want" ] || bad "could not cut the migration script out of git_credential_helper.yml"
migrate() { sh "$work/migrate.sh" >"$work/m.out" 2>"$work/m.err"; }
helpers() { git config --global --get-all credential.helper || true; }

git config --global --unset-all credential.helper || true
migrate || bad "migrate: no helper set must succeed"
[ "$(helpers)" = "$want" ] && grep -qx changed "$work/m.out" || bad "migrate: no helper set was not replaced and reported changed"

use store
migrate || bad "migrate: 'store' must succeed"
[ "$(helpers)" = "$want" ] && grep -qx changed "$work/m.out" || bad "migrate: 'store' was not replaced and reported changed"

migrate || bad "migrate: a re-run must succeed"
if [ -s "$work/m.out" ]; then bad "migrate: a re-run reported a change"; fi

git config --global --add credential.helper cache
if migrate; then bad "migrate: a hand-added second helper must stop the task"; fi
grep -q 'unexpected global credential.helper' "$work/m.err" || bad "migrate: the failure does not name the helper"
[ "$(helpers | wc -l)" -eq 2 ] || bad "migrate: a refused run changed the helper list"

use cache
if migrate; then bad "migrate: a hand-set helper must stop the task"; fi
[ "$(helpers)" = cache ] || bad "migrate: a refused run replaced a hand-set helper"

# Wiring. The path the task configures is the path it installs, from this file.
inst="$(sed -n 's/^ *dest: \(.*\)$/\1/p' "$TASKS/git_credential_helper.yml")"
[ "$inst" = "$want" ] || bad "installed path '$inst' != configured path '$want'"
grep -q '^ *src: git-credential-store-readonly$' "$TASKS/git_credential_helper.yml" || bad "the task does not install files/git-credential-store-readonly"
for f in git_forge.yml openbao.yml; do
  grep -q 'import_tasks: git_credential_helper.yml' "$TASKS/$f" || bad "$f does not import git_credential_helper.yml"
done
if grep -rn 'credential\.helper store' "$TASKS"; then bad "a task still configures the erasing 'store' helper"; fi

if [ "$fails" -gt 0 ]; then echo "$fails failure(s)" >&2; exit 1; fi
echo "git-credential-store-readonly: all checks passed"
