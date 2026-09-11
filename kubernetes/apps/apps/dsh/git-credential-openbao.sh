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
# unreadable, or not a single ASCII word (one trailing newline tolerated), this prints ONE stderr
# line that never contains the value, emits nothing on stdout, and exits 0 -- so git falls through
# to exactly the failure it has today, with a cause attached. A wrapped, CR-terminated or
# NUL-containing value is refused rather than repaired: the credential protocol is line-based and
# git 2.39 echoes malformed lines in a warning, which would put fragments of a token into the
# agent's transcript. The bytes are checked BEFORE command substitution, which is lossy (see below).
#
# HOST SCOPING IS DONE TWICE, ON PURPOSE. gitconfig.seed attaches this helper to
# `https://git.chifor.me` only, so git consults it for nothing else -- that is the layer an operator
# reads to learn where the credential goes. This file ALSO parses the request git writes to stdin
# and answers only for protocol=https + host=git.chifor.me (silently otherwise: a wrong authority is
# not a provisioning problem, so it gets no hint). Defense in depth against a future mis-scoped
# gitconfig or a URL-matching quirk handing the PAT to another authority. The two must agree;
# scripts/tests/test_dsh_git_credential_helper.py pins them to each other.
#
# NOT `set -e` ON THE READS: a failing `cat` must become the stderr hint, not a silent exit.
set -u

# Env-overridable so scripts/tests/test_dsh_git_credential_helper.py can drive the real script
# against a fixture directory, the way tests/test-cred-helper.sh drives the dev-worker `cred`.
DIR="${GIT_CREDENTIAL_OPENBAO_DIR:-/dsh-credentials}"
USER_FILE="$DIR/GITEA_USER"
PAT_FILE="$DIR/GITEA_PAT"
# The ONE authority this helper answers for. Must equal the section gitconfig.seed scopes to.
AUTHORITY_PROTOCOL=https
AUTHORITY_HOST=git.chifor.me

case "${1:-}" in
  get) ;;
  # `store` (after a successful auth), `erase` (after a rejection) and any operation added to the
  # protocol later: gitcredentials(7) says a helper must ignore what it does not handle.
  *) exit 0 ;;
esac

# git writes the request description to stdin (key=value lines, blank line, EOF). Read it all --
# so a fast exit never races the writer -- and keep the two keys that name the authority. git puts
# a port in `host=` only when it is not the protocol's default, so `git.chifor.me:8443` is,
# correctly, not this authority.
req_protocol=
req_host=
while IFS= read -r line; do
  case "$line" in
    protocol=*) req_protocol=${line#protocol=} ;;
    host=*)     req_host=${line#host=} ;;
    '')         break ;;
  esac
done
cat >/dev/null
[ "$req_protocol" = "$AUTHORITY_PROTOCOL" ] && [ "$req_host" = "$AUTHORITY_HOST" ] || exit 0

# One line, no values, exit 0. The path named is the OpenBao one because that is where the fix is.
refuse() {
  echo "git-credential-openbao: $1 -- the operator provisions GITEA_USER and GITEA_PAT in OpenBao at af/dsh/credentials (docs/runbooks/dsh.md, 'Git access to the forge')" >&2
  exit 0
}

[ -e "$USER_FILE" ] && [ -e "$PAT_FILE" ] || refuse "no GITEA_USER/GITEA_PAT under $DIR"

# Resolve the kubelet symlinks ONCE and use the resolved files for both the check and the read.
# Otherwise a rotation landing between the two (kubelet swaps `..data` atomically) would validate
# the old bytes and emit the new, unvalidated ones. Resolving pins one version for this call; if
# kubelet removes that version before the read, the read fails and is refused -- closed, not open.
# `readlink -f` on a regular file is the file itself, so a non-symlink mount works the same way.
USER_REAL=$(readlink -f "$USER_FILE" 2>/dev/null) || refuse "cannot resolve $USER_FILE"
PAT_REAL=$(readlink -f "$PAT_FILE" 2>/dev/null) || refuse "cannot resolve $PAT_FILE"
[ -n "$USER_REAL" ] && [ -n "$PAT_REAL" ] || refuse "cannot resolve GITEA_USER/GITEA_PAT under $DIR"

# The BYTES are judged before any shell expansion touches them, because `$(cat ...)` is lossy:
# dash discards NUL bytes inside a command substitution, so a value like `abc<NUL>def` would arrive
# as `abcdef` and sail past a check on the expanded string. The rule: every byte is ASCII printable
# and non-blank ([:graph:] in the C locale), except that ONE trailing newline is tolerated -- that is
# what `bao kv patch KEY=@file` stores when the file was written with echo. Empty is refused here
# too (zero bytes, or a lone newline). Non-ASCII is refused as a consequence and that is fine: a
# forge username and a PAT are ASCII words.
single_word_file() { # <file>
  bad=$(LC_ALL=C tr -d '[:graph:]' < "$1" 2>/dev/null | wc -c | tr -d ' ') || return 1
  case "$bad" in
    0) [ -s "$1" ] ;;
    # exactly one non-graph byte, and it is the LAST byte, and it is a newline
    1) [ "$(tail -c 1 "$1" 2>/dev/null | tr -d '\n' | wc -c | tr -d ' ')" = 0 ] && [ "$(wc -c < "$1" | tr -d ' ')" != 1 ] ;;
    *) return 1 ;;
  esac
}
[ -r "$USER_REAL" ] || refuse "cannot read $USER_FILE"
[ -r "$PAT_REAL" ] || refuse "cannot read $PAT_FILE"
single_word_file "$USER_REAL" || refuse "GITEA_USER is empty or not a single ASCII word"
single_word_file "$PAT_REAL" || refuse "GITEA_PAT is empty or not a single ASCII word"

# Now the expansion is lossless for what is left (only the tolerated trailing newline is stripped),
# and it reads the SAME file the check just judged.
user=$(cat "$USER_REAL" 2>/dev/null) || refuse "cannot read $USER_FILE"
pat=$(cat "$PAT_REAL" 2>/dev/null) || refuse "cannot read $PAT_FILE"

# Belt and braces on the expanded strings: nothing above should let these fire, and if a future
# edit ever does, the refusal is still value-free.
case "$user" in
  '' | *[[:space:][:cntrl:]]*) refuse "GITEA_USER failed the post-expansion check" ;;
esac
case "$pat" in
  '' | *[[:space:][:cntrl:]]*) refuse "GITEA_PAT failed the post-expansion check" ;;
esac

printf 'username=%s\npassword=%s\n' "$user" "$pat"
