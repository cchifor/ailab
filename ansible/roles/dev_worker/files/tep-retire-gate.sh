#!/usr/bin/env bash
# tep-retire-gate.sh [agent.hcl] — may tasks/tep.yml delete ~/.tep and the agent's tep template yet?
# (ADR 0037). Prints exactly one word on stdout and exits 0:
#   safe                  no agent.hcl, or it has no tep stanza and the running agent (if any) was
#                         started AFTER that config was written — i.e. it is running it
#   unsafe-config         agent.hcl still renders ~/.tep/kubeconfig
#   unsafe-stale-process  the running agent started before (or at the same instant as) the config it
#                         should be running, so it may still hold the old tep stanza
#   unsafe-unknown        a timestamp could not be read; fail closed
# Deleting a template's destination directory under a live stanza is a render failure, and the
# agent's exit_on_retry_failure turns that into a dead agent (and no ~/.git-credentials).
#
# Timestamps are compared at NANOSECOND precision (stat %.9Y, systemd --timestamp=us+utc). Whole
# seconds are not enough: on the normal path the handler restarts the agent within the same second
# as the template write, and a restart that happened just BEFORE the write in that same second must
# read as stale (reviewer-codex on ailab#1217).
set -uo pipefail

hcl="${1:-/etc/openbao-agent/agent.hcl}"

if [ ! -f "$hcl" ]; then echo safe; exit 0; fi
if grep -q 'tep-kubeconfig' "$hcl"; then echo unsafe-config; exit 0; fi
if [ "$(systemctl is-active openbao-agent 2>/dev/null)" != active ]; then echo safe; exit 0; fi

started_raw=$(systemctl show openbao-agent -p ExecMainStartTimestamp --timestamp=us+utc --value 2>/dev/null)
# `date -d ""` means today's midnight, not an error, so an empty value must be caught here.
[ -n "$started_raw" ] || { echo unsafe-unknown; exit 0; }
started=$(date -u -d "$started_raw" +%s%N 2>/dev/null) || { echo unsafe-unknown; exit 0; }
written=$(stat -c %.9Y "$hcl" 2>/dev/null | tr -d .) || { echo unsafe-unknown; exit 0; }
case "$started$written" in *[!0-9]*|'') echo unsafe-unknown; exit 0 ;; esac

if [ "$started" -gt "$written" ]; then echo safe; else echo unsafe-stale-process; fi
