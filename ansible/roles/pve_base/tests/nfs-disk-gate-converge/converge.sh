#!/bin/bash
# The pve_base nfs_disk_gate converge cases. Runs INSIDE the test container (systemd as PID 1), started by
# ../test-nfs-disk-gate-converge-docker.sh, which mounts the repo at /repo and this directory at /t.
# Proxmox is not installed, so this puts stand-ins in place:
#   - pve-guests.service in /usr/lib/systemd/system with the stock unit's shape: oneshot, RemainAfterExit, a
#     "-" ExecStartPre (pve-startall-delay), then ExecStart (startall), which here records when it ran;
#   - /etc/pve/storage.cfg with qnap-nfs at /mnt/pve/qnap-nfs, and this node's VM configs: 4001 with its disk
#     on qnap-nfs, 4101 on local-lvm;
#   - a tmpfs at /mnt/pve/qnap-nfs as the share (a test drop-in sets GATE_FSTYPES=tmpfs), and a `pvesm` stub that
#     "activates" it (mounts the tmpfs with the disk image) once /run/nas-up exists, as PVE would mount the NFS.
# Exit 1 if any case fails.
set -u
export PATH=/opt/ansible/bin:$PATH ANSIBLE_ROLES_PATH=/work/ansible/roles ANSIBLE_NOCOLOR=1
export ANSIBLE_STDOUT_CALLBACK=default ANSIBLE_CALLBACK_RESULT_FORMAT=yaml ANSIBLE_LOCALHOST_WARNING=False
rm -rf /work && mkdir -p /work && cp -r /repo/ansible /work/

NODE=$(hostname)
SHARE=/mnt/pve/qnap-nfs
DROPIN=/etc/systemd/system/pve-guests.service.d/10-ailab-nfs-disk-gate.conf
MARK=/run/fake-startall
BUDGET=30
fail=0
ok() { echo "PASS: $*"; }
bad() {
  echo "FAIL: $*"
  fail=1
}
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: expected '$2', got '$3'"; fi; }
exists() { [ -e "$1" ] && echo present || echo absent; }
run() {
  local label=$1 rc
  shift
  ansible-playbook -i localhost, /t/play.yml -e "pve_nfs_disk_gate_timeout_s=$BUDGET" "$@" > "/tmp/run-$label.log" 2>&1
  rc=$?
  RECAP=$(grep -E '^localhost +:' "/tmp/run-$label.log" | tail -1)
  echo "== $label: rc=$rc  $RECAP"
  return $rc
}
show() { tail -40 "/tmp/run-$1.log"; }
changed() { sed -nE 's/.*changed=([0-9]+).*/\1/p' <<< "$RECAP"; }
pre() { systemctl show -p ExecStartPre --value pve-guests.service; }
# Journal lines of pve-guests since a cursor file was written (a "boot" each).
since() { journalctl -u pve-guests --no-pager -o cat --after-cursor="$(cat /tmp/cursor)" "$@"; }
mark_boot() { journalctl -n 0 --show-cursor --no-pager | sed -n 's/^-- cursor: //p' > /tmp/cursor; }
share_up() {
  mountpoint -q "$SHARE" || mount -t tmpfs -o size=16m tmpfs "$SHARE"
  mkdir -p "$SHARE/images/4001" && head -c 8192 /dev/zero > "$SHARE/images/4001/vm-4001-disk-0.raw"
}
share_down() { if mountpoint -q "$SHARE"; then umount "$SHARE"; fi; }

# ---- the stand-ins ----
cat > /usr/lib/systemd/system/pve-guests.service << 'EOF'
# Test stand-in for pve-manager's pve-guests.service: the same Type, RemainAfterExit, TimeoutSec and the
# ExecStartPre-then-ExecStart shape. Its ExecStart says whether the gate's settings leaked into startall.
[Unit]
Description=PVE guests (test stand-in)

[Service]
Environment="PVE_LOG_ID=pve-guests"
ExecStartPre=-/usr/bin/true
ExecStart=/bin/sh -c 'date +%%s > /run/fake-startall; echo "startall: GATE_STORAGES=$${GATE_STORAGES:-unset} WAIT_S=$${WAIT_S:-unset}"'
ExecStop=/bin/rm -f /run/fake-startall
Type=oneshot
RemainAfterExit=yes
TimeoutSec=infinity

[Install]
WantedBy=multi-user.target
EOF
mkdir -p /etc/systemd/system/pve-guests.service.d "/etc/pve/nodes/$NODE/qemu-server" "$SHARE"
cat > /etc/systemd/system/pve-guests.service.d/99-test.conf << 'EOF'
[Service]
Environment=GATE_FSTYPES=tmpfs STEP_S=1
EOF
cat > /etc/pve/storage.cfg << 'EOF'
lvmthin: local-lvm
	thinpool data
	vgname pve
	content images,rootdir

nfs: qnap-nfs
	export /pve-nfs
	path /mnt/pve/qnap-nfs
	server 10.55.0.254
	content vztmpl,import,iso,images,backup
	options vers=4.0
EOF
printf '%s\n' "ide2: local-lvm:vm-4001-cloudinit,media=cdrom" "name: talos-cp1" "onboot: 1" \
  "scsi0: qnap-nfs:4001/vm-4001-disk-0.raw,aio=io_uring,backup=1,cache=none,discard=on,iothread=1,size=80G" \
  > "/etc/pve/nodes/$NODE/qemu-server/4001.conf"
printf '%s\n' "name: ci-runner-1" "onboot: 1" "scsi0: local-lvm:vm-4101-disk-0,size=250G" \
  > "/etc/pve/nodes/$NODE/qemu-server/4101.conf"
cat > /usr/local/sbin/pvesm << 'EOF'
#!/bin/bash
echo "$*" >> /run/pvesm-calls
if [ -e /run/nas-up ] && ! mountpoint -q /mnt/pve/qnap-nfs; then
  mount -t tmpfs -o size=16m tmpfs /mnt/pve/qnap-nfs
  mkdir -p /mnt/pve/qnap-nfs/images/4001 && head -c 8192 /dev/zero > /mnt/pve/qnap-nfs/images/4001/vm-4001-disk-0.raw
fi
EOF
chmod +x /usr/local/sbin/pvesm
systemctl daemon-reload

# ---- --check, apply, idempotence ----
run check-fresh --check --diff || { bad "fresh --check failed"; show check-fresh; }
eq "fresh --check wrote no drop-in" absent "$(exists "$DROPIN")"
eq "fresh --check wrote no script" absent "$(exists /usr/local/sbin/pve-nfs-disk-gate)"
run apply || { bad "apply failed"; show apply; }
eq "the script is the role's" "" "$(cmp /work/ansible/roles/pve_base/files/pve-nfs-disk-gate.sh /usr/local/sbin/pve-nfs-disk-gate)"
eq "the script is executable" 755 "$(stat -c %a /usr/local/sbin/pve-nfs-disk-gate)"
echo "systemctl show -p ExecStartPre --value pve-guests.service:"
pre | sed 's/^/    /'
eq "two ExecStartPre entries, the stock one first, then the gate" "/usr/bin/true /usr/bin/env" \
  "$(pre | sed -nE 's/^\{ path=([^ ]+) .*/\1/p' | tr '\n' ' ' | sed 's/ $//')"
pre | grep -q "argv\[\]=/usr/bin/env GATE_STORAGES=qnap-nfs WAIT_S=$BUDGET /usr/local/sbin/pve-nfs-disk-gate ; ignore_errors=yes" \
  && ok "the gate runs with the role's settings and its status ignored" || bad "the gate's ExecStartPre is not as expected"
eq "ExecStart is still startall's" 1 "$(systemctl show -p ExecStart --value pve-guests.service | grep -c 'fake-startall')"
run again || { bad "second apply failed"; show again; }
eq "second apply changes nothing" 0 "$(changed)"
run check-after --check --diff || { bad "--check after apply failed"; show check-after; }
eq "--check after apply changes nothing" 0 "$(changed)"

# ---- boot A: the share is ready ----
share_up
mark_boot
t0=$(date +%s)
systemctl start pve-guests.service
eq "A: pve-guests active" active "$(systemctl is-active pve-guests.service)"
eq "A: startall ran" present "$(exists $MARK)"
[ "$(($(date +%s) - t0))" -le 5 ] && ok "A: no wait with the share ready" || bad "A: took $(($(date +%s) - t0)) s"
since | grep -q "qnap-nfs ready after [01]s: mounted, writable, serving the disks of VM 4001$" \
  && ok "A: the gate said ready" || { bad "A: no ready line"; since; }
since | grep -q "startall: GATE_STORAGES=unset WAIT_S=unset" && ok "A: the gate's settings did not reach startall" \
  || bad "A: the gate's env leaked into startall"
eq "A: this node's probe file" present "$(exists "$SHARE/.pve-nfs-disk-gate/$NODE")"

# ---- boot B: the share comes late (PVE activation mounts it once the NAS answers) ----
systemctl stop pve-guests.service
share_down
rm -f /run/pvesm-calls /run/nas-up
mark_boot
t0=$(date +%s)
systemctl start --no-block pve-guests.service
sleep 4
eq "B: pve-guests is held in start-pre" activating "$(systemctl is-active pve-guests.service)"
eq "B: startall has not run" absent "$(exists $MARK)"
touch /run/nas-up
up=$(date +%s)
for _ in $(seq 1 20); do [ "$(systemctl is-active pve-guests.service)" = active ] && break; sleep 1; done
eq "B: pve-guests active once the share is up" active "$(systemctl is-active pve-guests.service)"
[ -e $MARK ] && [ "$(cat $MARK)" -ge "$up" ] && ok "B: startall ran after the share came up" \
  || bad "B: startall at $(cat $MARK 2> /dev/null), the share at $up"
since | grep -qE "qnap-nfs ready after ([4-9]|[1-9][0-9])s" && ok "B: the gate waited, then said ready" \
  || { bad "B: no ready line after a wait"; since; }
grep -qx "status --storage qnap-nfs" /run/pvesm-calls && ok "B: the gate asked PVE to activate the storage" \
  || bad "B: no pvesm status call"

# ---- boot C: the share never comes ----
systemctl stop pve-guests.service
share_down
rm -f /run/nas-up
mark_boot
t0=$(date +%s)
systemctl start pve-guests.service
took=$(($(date +%s) - t0))
eq "C: pve-guests active after the budget" active "$(systemctl is-active pve-guests.service)"
eq "C: startall ran" present "$(exists $MARK)"
[ "$took" -ge "$BUDGET" ] && [ "$took" -le $((BUDGET + 10)) ] && ok "C: held for the budget only (${took}s)" \
  || bad "C: held ${took}s for a ${BUDGET}s budget"
since -p warning | grep -qE "WARNING: qnap-nfs not ready after ($BUDGET|$((BUDGET + 1)))s: not mounted at $SHARE. Handing over to pve-guests anyway" \
  && ok "C: the WARNING is in the journal at priority warning" || { bad "C: no WARNING at priority warning"; since; }
pre | grep -E "pve-nfs-disk-gate ; ignore_errors=yes ; .* code=exited ; status=1 }" > /dev/null \
  && ok "C: the gate's exit 1 is recorded and ignored" || { bad "C: the gate's status is not 1, ignored"; pre; }

# ---- boot D: the share is mounted but reading the disk hangs (a FIFO: dd blocks in open, killable) ----
systemctl stop pve-guests.service
share_up
rm -f "$SHARE/images/4001/vm-4001-disk-0.raw" && mkfifo "$SHARE/images/4001/vm-4001-disk-0.raw"
mark_boot
t0=$(date +%s)
systemctl start pve-guests.service
took=$(($(date +%s) - t0))
eq "D: pve-guests active after the budget" active "$(systemctl is-active pve-guests.service)"
[ "$took" -ge "$BUDGET" ] && [ "$took" -le $((BUDGET + 10)) ] && ok "D: held for the budget only (${took}s)" \
  || bad "D: held ${took}s for a ${BUDGET}s budget"
since -p warning | grep -q "vm-4001-disk-0.raw timed out" && ok "D: the WARNING names the hung read" \
  || { bad "D: no 'timed out' WARNING"; since; }
sleep 1
eq "D: no dd from a timed-out probe survives" 0 "$(pgrep -c -f 'vm-4001-disk-0.raw' || true)"
eq "D: no shell job notice or job-control complaint in the journal" 0 "$(since | grep -ciE 'killed|job control')"
share_down

# ---- disable ----
systemctl stop pve-guests.service
run disable -e pve_nfs_disk_gate_enabled=false || { bad "disable failed"; show disable; }
eq "drop-in gone" absent "$(exists "$DROPIN")"
eq "script gone" absent "$(exists /usr/local/sbin/pve-nfs-disk-gate)"
eq "pve-guests no longer runs the gate" 0 "$(pre | grep -c pve-nfs-disk-gate)"
run disable-again -e pve_nfs_disk_gate_enabled=false || { bad "second disable failed"; show disable-again; }
eq "a second disable changes nothing" 0 "$(changed)"

[ "$fail" -eq 0 ] && echo "nfs_disk_gate converge: all cases passed" || echo "nfs_disk_gate converge: FAILURES"
exit "$fail"
