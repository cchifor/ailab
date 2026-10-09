#!/usr/bin/env bash
# Drives real git against files/git-credential-store-readonly in a throwaway HOME: `fill` returns
# the stored line and nothing from ~/.config/git/credentials, while `reject` (what git runs when a
# server refuses the credential it sent) and `approve` (what it runs on success) leave
# ~/.git-credentials byte-identical. A control run with the stock `store` helper and the SAME
# reject input must erase the line, which proves this test can see the bug it guards. Then the
# task's own migration script (cut out of tasks/git_credential_helper.yml): `store` or nothing is
# replaced, a re-run changes nothing, and a hand-added helper, a helper in the XDG config, an empty
# entry, an unreadable config or a failed write stops it with nothing reported as changed. Then the
# wiring.
# CI: .gitea/workflows/dev-worker-scripts.yaml. Linux only: Git Bash on Windows rewrites the
# /usr/local/bin/... argument into C:/Program Files/Git/usr/local/bin/... before git stores it.
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

# A host with no line in ~/.git-credentials: fill fails cleanly instead of prompting or inventing a
# password, and does not fall back to the XDG credentials file that stock `store` also reads.
mkdir -p "$XDG_CONFIG_HOME/git"
printf '%s
' 'https://other:tok_xdg@elsewhere.example' > "$XDG_CONFIG_HOME/git/credentials"
if printf 'protocol=https\nhost=elsewhere.example\n\n' | git credential fill >"$work/none" 2>/dev/null; then
  bad "fill for an unknown host succeeded"
fi
if grep -q '^password=' "$work/none"; then bad "fill for an unknown host printed a password"; fi
rm -f "$XDG_CONFIG_HOME/git/credentials"

# The task's migration script, run as written. The block scalar sits between the shell line and
# `args:`, indented four spaces.
sed -n '/ansible.builtin.shell: |/,/^  args:/p' "$TASKS/git_credential_helper.yml" | sed '1d;$d' | sed 's/^    //' > "$work/migrate.sh"
want="$(sed -n 's/^want=\(.*\)$/\1/p' "$work/migrate.sh")"
[ -n "$want" ] || bad "could not cut the migration script out of git_credential_helper.yml"
migrate() { sh "$work/migrate.sh" >"$work/m.out" 2>"$work/m.err"; }
helpers() { git config --file "$HOME/.gitconfig" --get-all credential.helper || true; }
# A refused run: non-zero, nothing reported as changed, and the stderr names $1.
refused() {
  if migrate; then bad "migrate: $2 must stop the task"; fi
  if [ -s "$work/m.out" ]; then bad "migrate: $2 reported a change"; fi
  grep -qF -- "$1" "$work/m.err" || bad "migrate: the failure for $2 does not name '$1': $(cat "$work/m.err")"
}

git config --global --unset-all credential.helper || true
migrate || bad "migrate: no helper set must succeed"
[ "$(helpers)" = "$want" ] && grep -qx changed "$work/m.out" || bad "migrate: no helper set was not replaced and reported changed"

use store
migrate || bad "migrate: 'store' must succeed"
[ "$(helpers)" = "$want" ] && grep -qx changed "$work/m.out" || bad "migrate: 'store' was not replaced and reported changed"

migrate || bad "migrate: a re-run must succeed"
if [ -s "$work/m.out" ]; then bad "migrate: a re-run reported a change"; fi

git config --global --add credential.helper cache
refused cache "a hand-added second helper"
[ "$(helpers)" = "$(printf '%s
cache' "$want")" ] || bad "migrate: a refused run changed the helper list"

use cache
refused cache "a hand-set helper"
[ "$(helpers)" = cache ] || bad "migrate: a refused run replaced a hand-set helper"

# An empty entry after the wanted one resets git's helper list to nothing; it must not read as done.
use "$want"; git config --global --add credential.helper ''
refused "2 entries" "an empty trailing entry"

# A helper in ~/.config/git/config: `git config --global` reads it but would write ~/.gitconfig.
use store; mkdir -p "$XDG_CONFIG_HOME/git"
git config --file "$XDG_CONFIG_HOME/git/config" credential.helper store
refused "$XDG_CONFIG_HOME/git/config" "a helper in the XDG config"
[ "$(helpers)" = store ] || bad "migrate: a refused run changed ~/.gitconfig"
rm -f "$XDG_CONFIG_HOME/git/config"

# An unreadable ~/.gitconfig is not "no helper".
cp "$HOME/.gitconfig" "$work/gitconfig.good"
printf '[credential
' >> "$HOME/.gitconfig"
refused "cannot read" "an unreadable ~/.gitconfig"
cp "$work/gitconfig.good" "$HOME/.gitconfig"

# A write that fails (a held lock) must fail the task, not report a change.
touch "$HOME/.gitconfig.lock"
refused "could not write" "a failed write"
[ "$(helpers)" = store ] || bad "migrate: a failed write changed the helper"
rm -f "$HOME/.gitconfig.lock"
migrate && [ "$(helpers)" = "$want" ] || bad "migrate: 'store' was not replaced once the lock was gone"

# Wiring. The path the task configures is the path it installs, from this file.
inst="$(sed -n 's/^ *dest: \(.*\)$/\1/p' "$TASKS/git_credential_helper.yml")"
[ "$inst" = "$want" ] || bad "installed path '$inst' != configured path '$want'"
grep -q '^ *src: git-credential-store-readonly$' "$TASKS/git_credential_helper.yml" || bad "the task does not install files/git-credential-store-readonly"
# git runs an absolute-path helper directly, so a non-executable install breaks every forge call.
grep -q '^ *mode: "0755"$' "$TASKS/git_credential_helper.yml" || bad "the helper is not installed 0755"
for f in git_forge.yml openbao.yml; do
  grep -q 'import_tasks: git_credential_helper.yml' "$TASKS/$f" || bad "$f does not import git_credential_helper.yml"
done
if grep -rn 'credential\.helper store' "$TASKS"; then bad "a task still configures the erasing 'store' helper"; fi

if [ "$fails" -gt 0 ]; then echo "$fails failure(s)" >&2; exit 1; fi
echo "git-credential-store-readonly: all checks passed"
