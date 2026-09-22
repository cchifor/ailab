#!/bin/sh
# The `claude-cli` provider's entry point: supply the subscription credential and exec the PINNED
# Claude Code binary. Installed to /dsh-home/.local/bin/claude-cli by the seed-settings
# initContainer; named as the provider's `command` in cordis.patch.yml.
#
# WHY A WRAPPER AND NOT THE BINARY DIRECTLY. Three things have to be true on every turn and none of
# them belong in the vendored adapter, which runs INSIDE the process that executes model-authored
# tool calls: the credential has to come off the ESO mount (so a rotation is picked up without a
# pod roll), two higher-precedence variables have to be out of the way, and the CLI has to be told
# it is headless. Keeping all three here means claude-cli-provider.mjs carries no token logic at
# all -- it spawns a command and reads NDJSON.
#
# This is the same shape as ansible/roles/pr_reviewer/files/claude-seat.sh, which has fed
# reviewer-1's Claude seats a token through the environment since 2026-09-18.
#
# IT CONFINES NOTHING, and must not be described as if it does. The dsh agent runs in THIS
# container as THIS uid and can already read /dsh-credentials directly -- deployment.yaml says so
# at the volume itself. This is a credential PATH, not a boundary; containment is the OpenBao ACL
# on the af/dsh/credentials document, which is why that document must hold only what dsh is meant
# to have.
#
# WHAT IS RUNNING. The unmodified binary Anthropic publishes, pinned by version and verified
# against the sha256 in Anthropic's own signed release manifest by the install Job, on the
# subscription owner's own `claude setup-token`. Nothing here alters the binary, and nothing
# rewrites its identity or its system prompt -- doing either is what ADR 0029 rejected, and ADR
# 0030 records why this shape is a different thing. If a future edit here starts injecting
# identity headers or a system message to make some other client authenticate, that edit is the
# thing ADR 0029 forbids; do not make it.
set -u

BIN="/app/tools/claude-code/${CLAUDE_CODE_VERSION:-unset}/claude"
TOKEN_FILE=/dsh-credentials/DSH_CLAUDE_CODE_OAUTH_TOKEN

if [ "${CLAUDE_CODE_VERSION:-}" = "" ]; then
  echo "claude-cli: CLAUDE_CODE_VERSION is not set in the dsh container; deployment.yaml must" >&2
  echo "claude-cli: carry it (kustomize derives it from the install Job -- see kustomization.yaml)." >&2
  exit 78
fi
if [ ! -x "$BIN" ]; then
  echo "claude-cli: $BIN is missing or not executable. The install Job stages it and is" >&2
  echo "claude-cli: deliberately NON-FATAL, so a failed download leaves this route absent rather" >&2
  echo "claude-cli: than the harness dead. Check: kubectl -n dsh logs job/dsh-install-<ver>" >&2
  exit 78
fi

# UNSET, not blanked. Both of these outrank CLAUDE_CODE_OAUTH_TOKEN in the CLI's documented
# credential precedence (ANTHROPIC_AUTH_TOKEN is 2nd, ANTHROPIC_API_KEY 3rd, the OAuth token 5th),
# and the platform documentation states that an empty value still counts as set on at least one
# precedence path. Upstream blanks them; blanking leaves a question that unsetting does not.
# Neither is set in this container today -- this is here so that a future variable added for some
# other route cannot silently move Claude turns onto a metered key.
unset ANTHROPIC_API_KEY
unset ANTHROPIC_AUTH_TOKEN

# The credential, read fresh on EVERY turn rather than captured once: the dsh-credentials
# ExternalSecret refreshes every 5 minutes and kubelet updates the mounted volume in place, so a
# rotated token is picked up by the next turn with no pod roll. Reopening by pathname is also why
# the volume is mounted whole rather than with subPath (see deployment.yaml).
#
# ABSENT OR EMPTY IS NOT AN ERROR HERE. Until the operator writes the field, the CLI answers
# `Not logged in - Please run /login` as a well-formed result with is_error: true -- measured in
# this pod 2026-09-22. dsh surfaces that as a failed turn naming the cause, which is a far better
# outcome than this script guessing.
if [ -r "$TOKEN_FILE" ]; then
  _t=$(cat "$TOKEN_FILE" 2>/dev/null) || _t=''
  if [ -n "$_t" ]; then
    CLAUDE_CODE_OAUTH_TOKEN=$_t
    export CLAUDE_CODE_OAUTH_TOKEN
  fi
  unset _t
fi

# Config/state directory on the home PVC, NOT the default $HOME/.claude: HOME is /dsh-home here,
# so the default would scatter CLI state through the directory dsh keeps settings.yaml and
# .credentials.yaml in. The CLI writes session transcripts under here keyed by cwd
# (<dir>/projects/-workspace/), so it GROWS with use -- see docs/runbooks/dsh.md for pruning.
CLAUDE_CONFIG_DIR=/dsh-home/.claude-cli
export CLAUDE_CONFIG_DIR
mkdir -p "$CLAUDE_CONFIG_DIR" 2>/dev/null || true

# The binary lives on a READ-ONLY mount, so the background updater could never succeed; without
# this it retries and logs on every turn. Bumping the pin is a two-value edit in install-job.yaml.
DISABLE_AUTOUPDATER=1
export DISABLE_AUTOUPDATER
# No analytics or product-feedback traffic from a private deployment, matching DSH_TELEMETRY_DISABLED.
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC

# NEVER add --bare here or in the row's extraArgs. It skips discovery and starts faster, which
# looks like an easy win, but bare mode does NOT read CLAUDE_CODE_OAUTH_TOKEN -- it would
# de-authenticate this route silently, turning every turn into "Not logged in".
exec "$BIN" "$@"
