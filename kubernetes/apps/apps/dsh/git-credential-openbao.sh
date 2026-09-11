#!/bin/sh
# git credential helper for the dsh agent: answers `get` for git.chifor.me from the OpenBao
# credentials that External Secrets projects into /dsh-credentials (openbao-eso.yaml).
#
# WHY THIS EXISTS. openbao-credentials.mjs teaches dsh's own `credentials` SERVICE to read that
# mount, and that is the seam `apiKeyEnv:` and plugins resolve through. It is NOT the agent's shell.
# `git` runs in a Landlock-confined bash whose environment @deepseek-ai/dsh-subprocess builds with
# scrubbedParentEnv(): every variable matching /KEY|PASSWORD|SECRET|TOKEN/i and every DSH_* variable
# is dropped before the shell starts. So nothing the mount carries reaches git by itself, and an env
# var never could -- observed 2026-09-11 in session 3d381759, which ended on
#     fatal: could not read Username for 'https://git.chifor.me': No such device or address
# This file is the bridge: gitconfig.seed names it (by absolute path) as the credential helper for
# https://git.chifor.me, and seed-settings installs both on every pod creation.
#
# READ AT USE TIME, NOT AT BOOT. The files are reopened by pathname on every `get`, never cached:
# kubelet swaps an atomic symlink (`..data`) when the Secret changes, so a PAT rotated in OpenBao
# reaches the next git invocation after ESO and kubelet propagate it -- no pod restart, no manifest
# change, which is the property the OpenBao credential store exists to provide.
#
# ABSENCE AND MALFORMATION ARE NOT ERRORS, AND NEVER LEAK. When either field is missing, empty,
# unreadable, or not a single opaque word, this prints ONE stderr line that never contains the
# value, emits nothing on stdout, and exits 0 -- so git falls through to exactly the failure it has
# today, with a cause attached. A wrapped or CR-terminated value is refused rather than repaired:
# the credential protocol is line-based and git 2.39 echoes malformed lines in a warning, which
# would put fragments of a token into the agent's transcript.
#
# HOST SCOPING IS NOT DONE HERE. gitconfig.seed attaches this helper to `https://git.chifor.me`
# only; git consults it for nothing else. Keeping that in git's own config, rather than re-checking
# the request here, means there is one place to read to learn where the credential goes.
#
# NOT `set -e` ON THE READS: a failing `cat` must become the stderr hint, not a silent exit.
set -u

# Env-overridable so scripts/tests/test_dsh_git_credential_helper.py can drive the real script
# against a fixture directory, the way tests/test-cred-helper.sh drives the dev-worker `cred`.
DIR="${GIT_CREDENTIAL_OPENBAO_DIR:-/dsh-credentials}"
USER_FILE="$DIR/GITEA_USER"
PAT_FILE="$DIR/GITEA_PAT"

case "${1:-}" in
  get) ;;
  # `store` (after a successful auth), `erase` (after a rejection) and any operation added to the
  # protocol later: gitcredentials(7) says a helper must ignore what it does not handle.
  *) exit 0 ;;
esac

# git writes the request description to stdin and closes it; drain it so a fast exit never races
# the writer. The description is not consulted: scoping lives in gitconfig.seed (see above).
cat >/dev/null

# One line, no values, exit 0. The path named is the OpenBao one because that is where the fix is.
refuse() {
  echo "git-credential-openbao: $1 -- the operator provisions GITEA_USER and GITEA_PAT in OpenBao at af/dsh/credentials (docs/runbooks/dsh.md, 'Git access to the forge')" >&2
  exit 0
}

[ -e "$USER_FILE" ] && [ -e "$PAT_FILE" ] || refuse "no GITEA_USER/GITEA_PAT under $DIR"

# Command substitution strips trailing newlines and nothing else: a value written with a trailing
# newline is accepted, everything else about the bytes is preserved.
user=$(cat "$USER_FILE" 2>/dev/null) || refuse "cannot read $USER_FILE"
pat=$(cat "$PAT_FILE" 2>/dev/null) || refuse "cannot read $PAT_FILE"

# A single opaque word, or nothing: no whitespace and no control characters anywhere in either
# value. This is what stops a wrapped PAT from becoming extra protocol lines.
case "$user" in
  '') refuse "GITEA_USER is empty" ;;
  *[[:space:][:cntrl:]]*) refuse "GITEA_USER is not a single word" ;;
esac
case "$pat" in
  '') refuse "GITEA_PAT is empty" ;;
  *[[:space:][:cntrl:]]*) refuse "GITEA_PAT is not a single word" ;;
esac

printf 'username=%s\npassword=%s\n' "$user" "$pat"
