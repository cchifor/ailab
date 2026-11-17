#!/bin/bash
# Operator-run: reap leaked buildx builders on ONE idle CI runner (docs/runbooks/ci-runners.md,
# Troubleshooting, "Leaked buildx builders"; alert CIRunnerLeakedBuildxBuilders). Used by hand on
# ci-runner-5/-6/-9/-10 on 2026-11-17 (owner-approved; 64 GB freed). Nothing runs it automatically.
#   ssh ubuntu@<ip> 'sudo bash -s list'   < scripts/ci-runner-buildx-reap.sh   # read-only, always first
#   ssh ubuntu@<ip> 'sudo bash -s remove' < scripts/ci-runner-buildx-reap.sh
# Removes builder containers 6h or older, then every builder volume no container uses. Refuses unless
# gitea-act-runner has no child processes across 3 reads, and re-reads that before each removal.
# LIMITS: that idle signal reads false between a job's steps and cannot see a docker-executor job or a
# detached compose stack; a builder a later job reuses by name can be in use however old it is (read the
# list first); the docker calls have no timeouts. The next build on a reaped builder is cold.
MODE="${1:-list}"; set -u
busy() { local mp; mp=$(systemctl show -p MainPID --value gitea-act-runner.service 2>/dev/null); case "$mp" in ''|*[!0-9]*) return 0;; esac; [ "$mp" -gt 0 ] && [ -n "$(pgrep -P "$mp" 2>/dev/null)" ]; }
for i in 1 2 3; do busy && { echo "$(hostname): BUSY (job running) -> nothing done"; exit 3; }; sleep 1; done
now=$(date +%s); echo "$(hostname): idle confirmed; mode=$MODE; disk before: $(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"
for id in $(docker ps -aq --filter name=buildx_buildkit); do
  name=$(docker inspect -f '{{.Name}}' "$id" | sed 's#^/##'); st=$(docker inspect -f '{{.State.Status}}' "$id")
  age_h=$(( (now - $(date -d "$(docker inspect -f '{{.Created}}' "$id")" +%s)) / 3600 ))
  echo "  builder container $name state=$st age=${age_h}h"
  if [ "$MODE" = remove ] && [ "$age_h" -ge 6 ]; then busy && { echo "  BUSY now -> stop"; exit 3; }; docker rm -f "$id" >/dev/null && echo "    REMOVED container $name"; fi
done
for v in $(docker volume ls -q --filter name=buildx_buildkit); do
  users=$(docker ps -aq --filter volume="$v" | wc -l); sz=$(du -sh "/var/lib/docker/volumes/$v" 2>/dev/null | cut -f1)
  echo "  builder volume $v size=$sz users=$users"
  if [ "$MODE" = remove ] && [ "$users" -eq 0 ]; then busy && { echo "  BUSY now -> stop"; exit 3; }; docker volume rm "$v" >/dev/null && echo "    REMOVED volume $v ($sz)"; fi
done
echo "  disk after: $(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"
