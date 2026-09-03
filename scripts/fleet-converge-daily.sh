#!/bin/bash
# Daily dev-worker fleet converge — GitOps-true: runs from a PRISTINE, self-updating clone
# (~/.ailab-converge/repo in WSL), never from the operator's working checkout. Born from the
# 2026-09-03 stale-checkout incident: a mystery ~06:00 job converged the fleet from a
# checkout stale at Aug 31 (and later found sitting on a dirty WIP branch), silently
# REVERTING merged work every morning. This script is scheduled AFTER that window (06:35 via
# Windows Task Scheduler task "ailab-fleet-converge") so current main always wins the
# morning; when the stale job is found and removed, this becomes the only converge.
# Logs: ~/.ailab-converge/converge.log (last 14 runs kept). Serialized via flock.
set -eu
BASE="$HOME/.ailab-converge"
REPO="$BASE/repo"
LOG="$BASE/converge.log"

exec 9>"$BASE/.lock"
flock -n 9 || { echo "another converge is running; skipping" >>"$LOG"; exit 0; }

{
  echo "=== converge $(date -Is)"
  git -C "$REPO" fetch --quiet origin main
  git -C "$REPO" reset --hard --quiet origin/main
  git -C "$REPO" log -1 --format='source: %h %ci %s'
  cd "$REPO/ansible"
  export ANSIBLE_CONFIG="$PWD/ansible.cfg"
  export SOPS_AGE_KEY_FILE=/mnt/c/Users/chifo/work/home/ailab/kubernetes/infra/_out/age.agekey
  export PATH="$HOME/.local/bin:$PATH"
  # dw6's herdr takeover stays operator-scheduled (pane-killing); everything else full-role.
  ansible-playbook dev-workers.yml --limit 'dev_workers:!dev-worker-6' 2>&1 | tail -10
  ansible-playbook dev-workers.yml --limit dev-worker-6 --skip-tags herdr 2>&1 | tail -4
  echo "=== done $(date -Is)"
} >>"$LOG" 2>&1

# keep the log bounded
tail -n 400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
