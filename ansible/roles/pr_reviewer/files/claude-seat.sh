#!/bin/sh
# reviewbot's claude entry point (ansible/roles/pr_reviewer -> /usr/local/lib/reviewbot/).
#
# A seat's long-lived OAuth token has to reach the CLI through the ENVIRONMENT: reviewbot runs
# the model as the seat's own OS user via `sudo -n -u <user> HOME=<home> ...`, and anything
# passed on that command line is visible in `ps` to every process on the host. sudo resets the
# environment, so the only place the token can come from is inside the seat's HOME - which is
# exactly the isolation the seat exists for (one credential per user, none reachable from a
# prompt-injected diff running as another).
#
# No token file -> nothing is exported and the ordinary ~/.claude/.credentials.json login
# applies, so a single-seat host runs unchanged through this same file.
#
# REVIEWBOT_CLAUDE_BIN exists for the unit test (scripts/tests/test_reviewbot.py) and nothing
# else; the role never sets it.
t="$HOME/.claude/oauth-token"
if [ -r "$t" ]; then
  CLAUDE_CODE_OAUTH_TOKEN=$(cat "$t")
  export CLAUDE_CODE_OAUTH_TOKEN
fi
exec "${REVIEWBOT_CLAUDE_BIN:-/usr/bin/claude}" "$@"
