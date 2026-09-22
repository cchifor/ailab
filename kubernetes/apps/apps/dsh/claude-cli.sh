#!/bin/sh
# The `claude-cli` provider's entry point: hand the PINNED Claude Code binary the subscription
# credential, a scrubbed environment and a tool policy, then exec it. Installed to
# /dsh-home/.claude-cli/bin/claude-cli by seed-settings; named as the provider's `command`.
#
# WHY THE POLICY LIVES HERE AND NOT ONLY IN THE ROW'S argv. The dsh agent has a shell in this
# container, so anything reachable is reachable by model-authored code too. With the deny flags in
# the adapter's argv alone, they applied to ONE call site: `claude-cli -p '...'` from the agent's
# own Bash would have started a FULLY-TOOLED Claude Code on the subscription, and the "text-only
# tier" would have been a property of how dsh happened to call it rather than of the route. Now
# every invocation gets the same policy, whoever makes it. (Found in review, 2026-09-22.)
#
# NOT ON PATH, for the same reason: /dsh-home/.local/bin is the FIRST PATH entry of this container,
# so a wrapper there is one word away from the model. This one has to be named by absolute path.
# That is friction, not a boundary -- see below.
#
# IT CONFINES NOTHING BY ITSELF, and must not be described as if it does. The agent runs in THIS
# container as THIS uid and can already read /dsh-credentials directly -- deployment.yaml says so
# at the volume. What this file buys is that the CLI cannot be turned into a tool-running agent by
# calling it differently, and that the child does not inherit the estate's other credentials.
# Containment is the OpenBao ACL on af/dsh/credentials and the pod boundary.
#
# WHAT IS RUNNING. The unmodified binary Anthropic publishes, pinned by version and verified by the
# install Job against the sha256 in Anthropic's own signed release manifest, on the subscription
# owner's own `claude setup-token`. Nothing here alters the binary, its identity or its system
# prompt. ADR 0030 records why that shape is permitted where ADR 0029's was not; an edit that
# starts injecting identity to make some other client authenticate is the thing ADR 0029 forbids.
set -u

BIN="/app/tools/claude-code/${CLAUDE_CODE_VERSION:-unset}/claude"
TOKEN_FILE=/dsh-credentials/DSH_CLAUDE_CODE_OAUTH_TOKEN
STATE_DIR=/dsh-home/.claude-cli

if [ "${CLAUDE_CODE_VERSION:-}" = "" ]; then
  echo "claude-cli: CLAUDE_CODE_VERSION is not set in the dsh container; deployment.yaml must" >&2
  echo "claude-cli: carry it (kustomize derives it from the install Job -- see kustomization.yaml)." >&2
  exit 78
fi
if [ ! -x "$BIN" ]; then
  echo "claude-cli: $BIN is missing or not executable. The install Job stages it and is" >&2
  echo "claude-cli: deliberately NON-FATAL, so a failed download leaves this route absent rather" >&2
  echo "claude-cli: than the harness dead. Check: kubectl -n dsh logs job/dsh-install-<name>" >&2
  exit 78
fi

# ---- the child's environment ------------------------------------------------------------------
# SCRUB FIRST, then add back exactly one credential. Upstream spawns the child with the whole of
# process.env, so without this the Claude Code process would inherit LITELLM_API_KEY,
# CODEX_API_KEY and every DSH_* variable -- which contradicts the posture this estate states
# twice, that credential-shaped variables are stripped before a child sees them
# (kustomization.yaml on dsh-subprocess, cordis.patch.yml on the codex provider). Found in review.
#
# AN ALLOWLIST, NOT A PATTERN. The obvious implementation is dsh-subprocess's own denylist --
# KEY/PASSWORD/SECRET/TOKEN-shaped names plus DSH_* -- and it was written that way first. It leaks:
# exercised in this pod on 2026-09-22 with planted variables, `GITEA_PAT` survived every one of
# those patterns. A denylist has to enumerate every shape a credential name might take, and this
# estate already has one that fits none of them.
#
# So: keep the handful the CLI actually needs and drop everything else. MEASURED in this pod the
# same day -- the CLI runs, resolves TLS and reaches Anthropic under exactly this set (`env -i`
# with these names), reporting its version and an empty tool list.
#
# The name list is captured BEFORE the loop so unsetting cannot disturb the iteration.
_names=$(env 2>/dev/null | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p')
for _n in $_names; do
  case "$_n" in
    PATH|HOME|LANG|LC_ALL|TERM|TMPDIR|CLAUDE_CODE_VERSION) ;;   # keep
    *) unset "$_n" 2>/dev/null || true ;;
  esac
done
unset _names _n

# UNSET, not blanked. Both outrank CLAUDE_CODE_OAUTH_TOKEN in the CLI's documented credential
# precedence (ANTHROPIC_AUTH_TOKEN 2nd, ANTHROPIC_API_KEY 3rd, the OAuth token 5th), and an empty
# value still counts as set on at least one precedence path. The scrub above already removes both;
# these two lines are kept so the intent survives a future narrowing of that pattern.
unset ANTHROPIC_API_KEY
unset ANTHROPIC_AUTH_TOKEN

# The credential, read fresh on EVERY turn rather than captured once: the dsh-credentials
# ExternalSecret refreshes every 5 minutes and kubelet updates the mounted volume in place, so a
# rotated token is picked up by the next turn with no pod roll. Reopening by pathname is also why
# that volume is mounted whole rather than with subPath (see deployment.yaml).
#
# ABSENT OR EMPTY IS NOT AN ERROR. Until the operator writes the field the CLI answers
# `Not logged in - Please run /login` as a well-formed result with is_error: true (measured in this
# pod 2026-09-22), and dsh surfaces that as a failed turn naming the cause -- a better outcome than
# this script guessing.
if [ -r "$TOKEN_FILE" ]; then
  _t=$(cat "$TOKEN_FILE" 2>/dev/null) || _t=''
  if [ -n "$_t" ]; then
    CLAUDE_CODE_OAUTH_TOKEN=$_t
    export CLAUDE_CODE_OAUTH_TOKEN
  fi
  unset _t
fi

# State on the home PVC, NOT $HOME/.claude: HOME is /dsh-home, so the default would scatter CLI
# state through the directory holding settings.yaml and .credentials.yaml.
CLAUDE_CONFIG_DIR="$STATE_DIR/config"
export CLAUDE_CONFIG_DIR
mkdir -p "$CLAUDE_CONFIG_DIR" "$STATE_DIR/cwd" 2>/dev/null || true

# BOUNDED, because nothing else bounds it. The CLI writes a transcript per turn and this route
# replays the whole conversation every turn, so an N-turn thread writes O(N^2) bytes -- onto a 5 Gi
# RWO local-path volume that also holds dsh's own settings and cookie secret. Seven days is far
# more history than a one-shot route can use; the files are never read back by anything here.
find "$CLAUDE_CONFIG_DIR/projects" -type f -mtime +7 -delete 2>/dev/null || true

DISABLE_AUTOUPDATER=1                      # the binary is on a read-only mount; the updater cannot win
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 # matches DSH_TELEMETRY_DISABLED for the harness
export DISABLE_AUTOUPDATER CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC

# ---- the tool policy, appended to whatever argv we were given ------------------------------------
# MEASURED IN THIS POD, 2026-09-22, by reading the tool list out of the CLI's own init event:
#
#   no flags .............................................. 22 tools (Task, Bash, Edit, Read, ...)
#   --strict-mcp-config --mcp-config '{"mcpServers":{}}' ... 22 tools  <-- MCP only; built-ins stay
#   --disallowed-tools '*' ................................. 0 tools
#   --tools "" ............................................. 0 tools
#   all of the above ....................................... 0 tools, no settings files loaded
#
# The middle row is the trap: the upstream README calls --strict-mcp-config "stripping Claude's
# tooling", which is true of MCP servers and false of Bash, Read and Edit -- in a pod whose
# ServiceAccount is cluster-admin. An explicit deny LIST is not equivalent either: the same
# measurement left 14 tools standing, because the list cannot name tools a future release adds.
#
# --tools "" is the positive form (an empty allowlist) and --disallowed-tools '*' the negative one.
# Both measured to zero on their own; both are sent because they fail differently.
#
# --setting-sources "" stops the CLI loading user, project and local settings files. Without it the
# CLI reads .claude/settings.json from its cwd, which can carry PreToolUse HOOKS -- arbitrary shell
# that would run inside a process holding the subscription token. The CLI has its own second line
# of defence (an untrusted workspace has its permissions.allow ignored, observed in the same
# measurement), but this route should not be relying on a trust dialog nobody can answer.
#
# THE MCP FLAGS ARRIVE TWICE and that is fine. The cordis row keeps isolateTools: true, so the
# adapter sends --strict-mcp-config --mcp-config too, and "$@" carries them in ahead of these.
# Verified in this pod 2026-09-22: the duplicated argv is accepted, "tools":[] and
# "mcp_servers":[]. They are repeated here rather than left to the adapter because a direct
# invocation -- the case this whole block exists for -- would not carry the adapter's copy.
exec "$BIN" "$@" \
  --tools "" \
  --disallowed-tools "*" \
  --setting-sources "" \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}'

# NEVER add --bare. It skips discovery and starts faster, which looks like an easy win, but bare
# mode does NOT read CLAUDE_CODE_OAUTH_TOKEN -- it would de-authenticate this route silently.
