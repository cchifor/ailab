#!/usr/bin/env bash
# test-env-reaper.sh — the env-reaper's shell helpers (kubernetes/apps/infrastructure/testpool/
# env-reaper.yaml, ConfigMap key reap.sh) run under the DaemonSet's OWN image (busybox ash in
# docker.io/alpine/k8s, digest-pinned below = the manifest's) against a synthetic /proc tree:
#   - procs_of / still_ours: the identity chain (cgroup + exe + cmdline) matches a sandbox's
#     cloud-hypervisor and virtiofsd and nothing else; still_ours goes false when the exe changes
#     or the pid disappears (PID reuse between evidence collection and SIGKILL)
#   - evidence: one process line, one line per thread, kernel stack only for D/T threads,
#     '?' for unreadable entries, truncation after EVIDENCE_MAX_THREADS, and the timeout path
#     (a FIFO with no writer as /proc/<pid>/stack) ends in an "incomplete" line, never a hang
#   - reap: an identity that changed after evidence is skipped with a skip-kill line
# FAIL CLOSED like scripts/manifest-lint.sh: set -euo pipefail, no soft skips; docker required.
# Run: bash scripts/tests/test-env-reaper.sh   (CI: .gitea/workflows/manifests.yaml)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/kubernetes/apps/infrastructure/testpool/env-reaper.yaml"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }

IMAGE="$($PY - "$MANIFEST" <<'EOF'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1], encoding="utf-8")) if d]
ds = [d for d in docs if d["kind"] == "DaemonSet"][0]
print(ds["spec"]["template"]["spec"]["containers"][0]["image"])
EOF
)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
$PY - "$MANIFEST" "$WORK/reap.sh" <<'EOF'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1], encoding="utf-8")) if d]
cm = [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "env-reaper"][0]
open(sys.argv[2], "w", newline="\n").write(cm["data"]["reap.sh"])
EOF

cat > "$WORK/inner.sh" <<'EOF'
#!/bin/sh
# Runs INSIDE the reaper image. Builds a fake /proc and exercises the sourced helpers.
set -u
fail() { echo "FAIL: $*" >&2; exit 1; }
P=/tmp/fakeproc; rm -rf "$P"; mkdir -p "$P"
SB=a73661aed709877460c8ec29908bffe1
UID_=727c4446-34db-4763-a4ec-2f9ee360c266
mkproc() { # pid cgroup exe cmdline state threads
  d="$P/$1"; mkdir -p "$d/task"
  printf '0::%s\n' "$2" > "$d/cgroup"
  ln -s "$3" "$d/exe"
  printf '%s' "$4" | tr ' ' '\0' > "$d/cmdline"
  printf 'Name:\tx\nState:\t%s\nThreads:\t%s\n' "$5" "$6" > "$d/status"
  printf 'do_wait\n' > "$d/wchan"
  printf '[<0>] a\n[<0>] b\n' > "$d/stack"
  i=0; while [ "$i" -lt "$6" ]; do
    t="$d/task/$(( $1 + i ))"; mkdir -p "$t"
    st=S; [ "$i" -eq 1 ] && st=D
    printf 'State:\t%s\n' "$st" > "$t/status"; printf 'x_wait_%s\n' "$i" > "$t/wchan"
    printf '%s (x) %s 1 1 1 0 -1\n' "$(( $1 + i ))" "$st" > "$t/stat"; printf '[<0>] f1\n[<0>] f2\n' > "$t/stack"
    i=$(( i + 1 ))
  done
}
mkproc 1001 "/kubepods/burstable/pod${UID_}/kata_${SB}" /usr/local/bin/cloud-hypervisor "/usr/local/bin/cloud-hypervisor --api-socket /run/vc/vm/${SB}/clh.sock" "S (sleeping)" 3
mkproc 1002 "/kata_overhead/${SB}" /usr/local/libexec/virtiofsd "/usr/local/libexec/virtiofsd --shared-dir /run/kata-containers/shared/sandboxes/${SB}/shared" "D (disk sleep)" 2
mkproc 1003 "/kata_overhead/${SB}" /usr/local/bin/containerd-shim-kata-v2 "/usr/local/bin/containerd-shim-kata-v2 -namespace k8s.io -id ${SB} -address /run/containerd/containerd.sock" "S (sleeping)" 1
mkproc 1004 "/kubepods/burstable/podOTHER/kata_otherid" /usr/local/bin/cloud-hypervisor "/usr/local/bin/cloud-hypervisor --api-socket /run/vc/vm/otherid/clh.sock" "S (sleeping)" 1
mkproc 1005 "/kubepods/burstable/pod${UID_}/kata_${SB}" /usr/bin/umount "umount /x" "S (sleeping)" 1   # same cgroup, wrong exe
mkproc 1006 "/kata_overhead/${SB}" /usr/local/libexec/virtiofsd "/usr/local/libexec/virtiofsd --shared-dir /run/kata-containers/shared/sandboxes/OTHER/shared" "S (sleeping)" 1  # right exe, wrong sandbox in cmdline

export NODE_NAME=test PROC_ROOT="$P" REAP_LIB_ONLY=1 REAP_SCRIPT=/t/reap.sh EVIDENCE_MAX_THREADS=2 EVIDENCE_TIMEOUT_SECONDS=2
. /t/reap.sh
[ "$(type sandbox_of 2>/dev/null | head -1)" != "" ] || fail "library did not load"

# --- sandbox_of: from the live VMM cgroup ---
[ "$(sandbox_of "$UID_")" = "$SB" ] || fail "sandbox_of should find $SB, got '$(sandbox_of "$UID_")'"

# --- procs_of stage 1: exactly the VMM + the sandbox's virtiofsd ---
got=$(procs_of "$SB" 1 | sort | tr '\n' ';')
[ "$got" = "1001 /usr/local/bin/cloud-hypervisor;1002 /usr/local/libexec/virtiofsd;" ] || fail "procs_of stage 1: '$got'"
got=$(procs_of "$SB" 2 | tr '\n' ';')
[ "$got" = "1003 /usr/local/bin/containerd-shim-kata-v2;" ] || fail "procs_of stage 2: '$got'"

# --- still_ours: true, then false on a changed exe, false when the pid disappears ---
still_ours "$SB" 1 1002 /usr/local/libexec/virtiofsd || fail "still_ours should accept 1002"
still_ours "$SB" 1 1005 /usr/bin/umount && fail "still_ours accepted a non-kata exe"
still_ours "$SB" 1 1006 /usr/local/libexec/virtiofsd && fail "still_ours accepted a virtiofsd of another sandbox"
rm "$P/1002/exe"; ln -s /usr/bin/sleep "$P/1002/exe"
still_ours "$SB" 1 1002 /usr/local/libexec/virtiofsd && fail "still_ours accepted a changed exe (PID reuse)"
rm "$P/1002/exe"; ln -s /usr/local/libexec/virtiofsd "$P/1002/exe"
mv "$P/1001" "$P/1001.gone"
still_ours "$SB" 1 1001 /usr/local/bin/cloud-hypervisor && fail "still_ours accepted a vanished pid"
mv "$P/1001.gone" "$P/1001"

# --- evidence: process line + per-thread lines, D-thread stack, truncation at EVIDENCE_MAX_THREADS ---
out=$(evidence pod-x "$UID_" "$SB" 1001 /usr/local/bin/cloud-hypervisor)
echo "$out" | grep -q "evidence stage=1 pod=pod-x uid=${UID_} sandbox=${SB} exe=/usr/local/bin/cloud-hypervisor pid=1001 state=S wchan=do_wait threads=3 tstates=D:1,S:2" || fail "process evidence line: $out"
echo "$out" | grep -q "evidence-thread stage=1 pod=pod-x sandbox=${SB} pid=1001 tid=1001 state=S wchan=x_wait_0" || fail "thread line tid=1001: $out"
echo "$out" | grep -q "evidence-thread .* tid=1002 state=D wchan=x_wait_1" || fail "thread line tid=1002 (D): $out"
echo "$out" | grep -q "evidence-stack stage=1 pod=pod-x sandbox=${SB} pid=1001 tid=1002 stack=\[<0>\] f1|\[<0>\] f2|" || fail "D-thread stack: $out"
echo "$out" | grep -q "truncated after 2 threads" || fail "thread truncation: $out"
[ "$(echo "$out" | grep -c 'evidence-stack')" = "1" ] || fail "stack only for the D thread: $out"

# --- evidence: unreadable entries read as '?' ---
chmod 000 "$P/1003/status" "$P/1003/wchan" 2>/dev/null || true
rm -f "$P/1003/status" "$P/1003/wchan"
out=$(evidence pod-x "$UID_" "$SB" 1003 /usr/local/bin/containerd-shim-kata-v2)
echo "$out" | grep -q "pid=1003 state=? wchan=? threads=?" || fail "unreadable status/wchan should read '?': $out"

# --- evidence: a stack that never returns ends in an 'incomplete' line within the timeout ---
rm -f "$P/1002/task/1003/stack"; mkfifo "$P/1002/task/1003/stack"   # tid 1003 of pid 1002 is the D thread
start=$(date +%s)
out=$(evidence pod-x "$UID_" "$SB" 1002 /usr/local/libexec/virtiofsd)
el=$(( $(date +%s) - start ))
echo "$out" | grep -q "evidence stage=1 pod=pod-x sandbox=${SB} pid=1002 incomplete (timeout 2s or read error)" || fail "timeout should log incomplete: $out"
[ "$el" -le 5 ] || fail "evidence took ${el}s, timeout not enforced"
rm -f "$P/1002/task/1003/stack"

# --- reap: identity re-check right before the kill (exe changed after matching) ---
# 1007 matches at scan time; the evidence() hook (called per pid, AFTER procs_of listed it) swaps
# its exe — the PID-reuse shape — so still_ours must refuse and reap must log skip-kill, not reap.
# (The hook acts only for 1007: procs_of runs concurrently as the pipeline producer, so flipping
# on an earlier pid would race the scan and 1007 would simply never be listed.)
mkproc 1007 "/kata_overhead/${SB}" /usr/local/libexec/virtiofsd "/usr/local/libexec/virtiofsd --shared-dir /run/kata-containers/shared/sandboxes/${SB}/shared" "S (sleeping)" 1
evidence() { if [ "$4" = 1007 ]; then rm "$P/1007/exe"; ln -s /usr/bin/sleep "$P/1007/exe"; fi; return 0; }
out=$(reap pod-x "$UID_" "$SB" 1 150 2>&1)
echo "$out" | grep -q "skip-kill stage=1 pod=pod-x sandbox=${SB} pid=1007 exe=/usr/local/libexec/virtiofsd reason=identity-changed" || fail "changed identity must be skipped: $out"
echo "$out" | grep -q "reap stage=1 .* pid=1007 " && fail "a changed identity was killed: $out"
echo "ALL REAPER HELPER TESTS PASSED"
EOF

docker run --rm -v "$WORK:/t:ro" --user 0 "$IMAGE" sh /t/inner.sh
