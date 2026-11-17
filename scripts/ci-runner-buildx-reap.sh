#!/bin/bash
# Operator-run: reap leaked buildx builders on ONE idle CI runner (docs/runbooks/ci-runners.md,
# Troubleshooting, "Leaked buildx builders"; alert CIRunnerLeakedBuildxBuilders). Nothing runs it
# automatically. It is the script used by hand on ci-runner-5/-6/-9/-10 on 2026-11-17 (owner-approved;
# 64 GB freed), hardened after review: anything it cannot read is skipped, never removed.
#   ssh ubuntu@<ip> 'sudo bash -s list'   < scripts/ci-runner-buildx-reap.sh   # read-only, always first
#   ssh ubuntu@<ip> 'sudo bash -s remove' < scripts/ci-runner-buildx-reap.sh
# Removes builder containers (exact name buildx_buildkit_<name>) 6h or older, then every builder state
# volume (exact name buildx_buildkit_<name>_state) no container uses. Refuses unless gitea-act-runner has
# no child processes across 3 reads, re-reads that before each removal, and treats a probe it cannot read
# as busy.
# LIMITS: that idle signal reads false between a job's steps and cannot see a docker-executor job or a
# detached compose stack; a builder a later job reuses by name can be in use however old it is (read the
# list first); the docker calls have no timeouts. The next build on a reaped builder is cold.
MODE="${1:-list}"; set -u
CTR_RE='^buildx_buildkit_[A-Za-z0-9][A-Za-z0-9_.-]*$'          # the cleanup metric's shapes
VOL_RE='^buildx_buildkit_[A-Za-z0-9][A-Za-z0-9_.-]*_state$'
# 0 = busy, or unknown; 1 = idle. pgrep exits 1 for "no children" and 2+ for an error: only 1 is idle.
busy() {
  local mp rc=0
  mp=$(systemctl show -p MainPID --value gitea-act-runner.service 2>/dev/null) || return 0
  case "$mp" in ''|*[!0-9]*) return 0;; esac
  [ "$mp" -gt 0 ] || return 1 # the runner is stopped: no job of ours
  pgrep -P "$mp" >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 1 ] && return 1
  return 0
}
for _ in 1 2 3; do busy &&{ echo "$(hostname): BUSY (job running, or the probe failed) -> nothing done"; exit 3; }; sleep 1; done
now=$(date +%s); echo "$(hostname): idle confirmed; mode=$MODE; disk before: $(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"
for id in $(docker ps -aq --filter name=buildx_buildkit); do
  if ! meta=$(docker inspect -f '{{.Name}}|{{.State.Status}}|{{.Created}}' "$id" 2>/dev/null); then
    echo "  SKIP container $id: docker inspect failed"; continue
  fi
  IFS='|' read -r name st created <<< "$meta"; name="${name#/}"
  if ! grep -qE "$CTR_RE" <<< "$name"; then echo "  SKIP container $id ($name): not a builder's name"; continue; fi
  # `date -d ''` is today's midnight: an empty or unparseable time skips the container.
  if [ -z "$created" ] || ! t=$(date -d "$created" +%s 2>/dev/null) || [ -z "$t" ]; then
    echo "  SKIP builder container $name: unreadable creation time '$created'"; continue
  fi
  age_h=$(( (now - t) / 3600 ))
  echo "  builder container $name state=$st age=${age_h}h"
  if [ "$MODE" = remove ] && [ "$age_h" -ge 6 ]; then busy && { echo "  BUSY now -> stop"; exit 3; }; docker rm -f "$id" >/dev/null && echo "    REMOVED container $name"; fi
done
for v in $(docker volume ls -q --filter name=buildx_buildkit); do
  if ! grep -qE "$VOL_RE" <<< "$v"; then echo "  SKIP volume $v: not a builder state volume's name"; continue; fi
  if ! users=$(docker ps -aq --filter volume="$v" 2>/dev/null); then echo "  SKIP volume $v: cannot read its users"; continue; fi
  n=0; [ -z "$users" ] || n=$(printf '%s\n' "$users" | wc -l)
  sz=$(du -sh "/var/lib/docker/volumes/$v" 2>/dev/null | cut -f1)
  echo "  builder volume $v size=$sz users=$n"
  if [ "$MODE" = remove ] && [ "$n" -eq 0 ]; then busy && { echo "  BUSY now -> stop"; exit 3; }; docker volume rm "$v" >/dev/null && echo "    REMOVED volume $v ($sz)"; fi
done
echo "  disk after: $(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"
