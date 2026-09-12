#!/usr/bin/env bash
# Prove codex actually WORKS on every dev worker and the codex reviewer — from the control node,
# over the ansible inventory, one round-trip per host. Ships validate-codex-host.sh to each host
# (ansible `script` module, as root) and prints its one-line verdict per host:
#
#   dev-worker-1 OK   user=c4 projection(no-refresh-token) access_token_left=8.9d
#   reviewer-2   FAIL user=codexrun HAS-REFRESH-TOKEN(hand-copied) access_token_left=-2.1d rc=1 ...
#
# "projection(no-refresh-token)" is the OpenBao-rendered shared login (the target state —
# docs/runbooks/openbao-dev-workers.md § "The shared codex login"); "HAS-REFRESH-TOKEN" means the
# host still runs on a hand-copied auth.json that the ansible rollout has not replaced yet. Exit 1
# if any host FAILs or is unreachable.
#
#   scripts/validate-codex-fleet.sh                  # all six workers + reviewer-2
#   scripts/validate-codex-fleet.sh dev-worker-3     # one host (any inventory pattern)
#
# Needs the ansible inventory + SSH that dev-workers.yml / reviewers.yml already use (run it from
# the same control node). Nothing is left on the hosts; the smoke prompt costs a few hundred tokens
# per host.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PATTERN="${1:-dev_workers,reviewer-2}"

cd "$REPO/ansible"
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
# `minimal` callback (built into every ansible-core; `json` moved out of core in 2.19), -b = root
# (the host script sudo's down to the codex user itself). The host script prints exactly one
# "<host> OK|FAIL ..." line, which is what is kept; an unreachable host is reported the same way.
# Exit 1 if any host is not OK.
raw=$(ANSIBLE_STDOUT_CALLBACK=minimal ANSIBLE_DEPRECATION_WARNINGS=False \
      ansible "$PATTERN" -b -m script -a "$REPO/scripts/validate-codex-host.sh" 2>/dev/null || true)
# minimal prints each host's stdout indented under "stdout: |-"; the verdict line is the one that
# starts with the hostname followed by OK/FAIL.
{
  printf '%s\n' "$raw" | sed -nE 's/^[[:space:]]+([A-Za-z0-9._-]+ (OK|FAIL) .*)$/\1/p'
  printf '%s\n' "$raw" | sed -nE 's/^([^ |]+) \| UNREACHABLE!.*/\1 FAIL unreachable/p'
} | sort
if printf '%s\n' "$raw" | grep -qE '^[[:space:]]+[A-Za-z0-9._-]+ FAIL |UNREACHABLE!'; then exit 1; fi
if ! printf '%s\n' "$raw" | grep -qE '^[[:space:]]+[A-Za-z0-9._-]+ OK '; then echo "no host reported OK (inventory pattern '$PATTERN' matched nothing?)" >&2; exit 1; fi
