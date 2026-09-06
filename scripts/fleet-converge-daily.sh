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
# pipefail so ansible's exit status SURVIVES `| tail`. Without it a failed converge is
# indistinguishable from a clean one — which is the same silent-drift failure this script's
# reviewers step was added to kill, one level up (reviewer finding, ailab#500).
set -o pipefail
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
  # Each converge records its own status and the run CONTINUES: a dev-worker failure must
  # not stop the reviewers converging (or vice versa). rc is checked at the end and returned
  # to the scheduler, so a persistently failing converge shows as a failed task rather than a
  # green one that quietly changed nothing.
  rc=0
  # dw6's herdr takeover stays operator-scheduled (pane-killing); everything else full-role.
  ansible-playbook dev-workers.yml --limit 'dev_workers:!dev-worker-6' 2>&1 | tail -10 || rc=$?
  ansible-playbook dev-workers.yml --limit dev-worker-6 --skip-tags herdr 2>&1 | tail -4 || rc=$?
  # The reviewer VMs converge here too. Until 2026-09-06 they converged NOWHERE: this script
  # only ran dev-workers.yml, so every reviewbot.py change merged to main sat undeployed
  # until somebody remembered to run the playbook by hand. That is not hypothetical — the
  # tolerant-diff-decode fix (e5e15d85, merged 09-05) went 22 hours undeployed while
  # platform#1074 failed on EVERY attempt on BOTH personas, 66 times, and the PR could not be
  # reviewed at all. A daily no-op is cheap; two days of silent drift is not.
  ansible-playbook reviewers.yml 2>&1 | tail -6 || rc=$?
  [ "$rc" -eq 0 ] || echo "!!! CONVERGE FAILED (rc=$rc) — hosts may now be DRIFTING from main"
  echo "=== done $(date -Is) rc=$rc"
} >>"$LOG" 2>&1

# keep the log bounded
tail -n 400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

# Surface the verdict to the scheduler: Task Scheduler shows a non-zero last-run result, which
# is the only signal outside this log file that anything went wrong.
exit "${rc:-0}"
