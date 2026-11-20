#!/bin/bash
# The dev_worker fstrim converge cases. Runs INSIDE the test container (systemd as PID 1), started by
# ../test-fstrim-converge-docker.sh, which mounts the repo at /repo and this directory at /t. /workspace is a
# REAL ext4 filesystem on a loop device whose backing file is sparse, so a trim that reaches it punches holes
# the test can measure. Exit 1 if any case fails.
set -u
export PATH=/opt/ansible/bin:$PATH ANSIBLE_ROLES_PATH=/work/ansible/roles ANSIBLE_NOCOLOR=1
export ANSIBLE_STDOUT_CALLBACK=default ANSIBLE_CALLBACK_RESULT_FORMAT=yaml ANSIBLE_LOCALHOST_WARNING=False
rm -rf /work && mkdir -p /work/kubernetes/apps && cp -r /repo/ansible /work/ && cp -r /repo/kubernetes/apps/node-fstrim /work/kubernetes/apps/

TF=/var/lib/prometheus/node-exporter
fail=0
ok() { echo "PASS: $*"; }
bad() {
  echo "FAIL: $*"
  fail=1
}
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: expected '$2', got '$3'"; fi; }
exists() { [ -e "$1" ] && echo present || echo absent; }
run() { # $1 label, then ansible-playbook args; leaves the PLAY RECAP line in RECAP
  local label=$1 rc
  shift
  ansible-playbook -i localhost, /t/play.yml -e @/t/vars.yml "$@" > "/tmp/run-$label.log" 2>&1
  rc=$?
  RECAP=$(grep -E '^localhost +:' "/tmp/run-$label.log" | tail -1)
  echo "== $label: rc=$rc  $RECAP"
  return $rc
}
show() { tail -40 "/tmp/run-$1.log"; }
changed() { sed -nE 's/.*changed=([0-9]+).*/\1/p' <<< "$RECAP"; }
metric() { sed -nE "s|^$1 (.*)$|\1|p" "$TF/node_fstrim.prom" 2>/dev/null; }
alloc_mib() { echo $(( $(stat -c %b /ws/workspace.img) * $(stat -c %B /ws/workspace.img) / 1048576 )); }

# ---- a dev worker in miniature: the cloud image's root fstab line, /workspace on a loop device ----
printf 'LABEL=cloudimg-rootfs\t/\t ext4\tdiscard,commit=30,errors=remount-ro\t0 1\nLABEL=UEFI\t/boot/efi\tvfat\tumask=0077\t0 1\nLABEL=workspace /workspace ext4 defaults,nofail,noatime 0 2\n' > /etc/fstab
FSTAB0="$(cat /etc/fstab)"
mkdir -p /ws /workspace && mount -t tmpfs -o size=2g tmpfs /ws
truncate -s 1G /ws/workspace.img && mkfs.ext4 -q -F -L workspace /ws/workspace.img
LOOP=$(losetup -f --show /ws/workspace.img) || { echo "no loop device"; exit 1; }
mount -o noatime "$LOOP" /workspace
systemctl enable --now fstrim.timer > /dev/null 2>&1 # the Ubuntu default the role takes over from
eq "precondition: the stock weekly timer is on" enabled "$(systemctl is-enabled fstrim.timer)"

# ---- a first --check changes nothing ----
run check-fresh --check --diff || { bad "fresh --check failed"; show check-fresh; }
eq "fresh --check wrote no unit" absent "$(exists /etc/systemd/system/dev-worker-fstrim.timer)"
eq "fresh --check left fstab alone" "$FSTAB0" "$(cat /etc/fstab)"
eq "fresh --check left the stock timer on" enabled "$(systemctl is-enabled fstrim.timer)"

# ---- apply ----
run apply || { bad "apply failed"; show apply; }
eq "fstab: only the root entry's discard went" "LABEL=cloudimg-rootfs	/	 ext4	commit=30,errors=remount-ro	0 1" "$(head -1 /etc/fstab)"
eq "fstab: the other lines are untouched" "$(sed -n 2,3p <<< "$FSTAB0")" "$(sed -n 2,3p /etc/fstab)"
[ "$(ls /etc/fstab.*~ 2> /dev/null | wc -l)" -ge 1 ] && ok "fstab backed up" || bad "no fstab backup"
eq "/ (overlay here, no discard) was not remounted" 1 "$(grep -c 'Remount / nodiscard' /tmp/run-apply.log)"
grep -A2 'Remount / nodiscard' /tmp/run-apply.log | grep -q skipping && ok "the remount was skipped" || bad "the remount ran on an overlay root"
eq "node-fstrim is the repo's script" "" "$(cmp /work/kubernetes/apps/node-fstrim/node-fstrim.sh /usr/local/sbin/node-fstrim)"
eq "timer enabled" enabled "$(systemctl is-enabled dev-worker-fstrim.timer)"
eq "timer active" active "$(systemctl is-active dev-worker-fstrim.timer)"
eq "the converge did not start a trim" inactive "$(systemctl is-active dev-worker-fstrim.service)"
eq "the stock weekly timer is masked" masked "$(systemctl is-enabled fstrim.timer)"
eq "the slot" "*-*-* 00:30:00 UTC" "$(systemctl show -p TimersCalendar --value dev-worker-fstrim.timer | sed -nE 's/.*OnCalendar=([^;]*[^ ;]) *;.*/\1/p')"
eq "TimeoutStartSec: 5 min + one 4 GiB chunk at 30 s/GiB + 1 min" 8min "$(systemctl show -p TimeoutStartUSec --value dev-worker-fstrim.service)"
eq "TimeoutStopSec: one chunk + 1 min" 3min "$(systemctl show -p TimeoutStopUSec --value dev-worker-fstrim.service)"
eq "ExecStart trims the configured mounts" "/usr/local/sbin/node-fstrim /workspace" "$(sed -n 's/^ExecStart=//p' /etc/systemd/system/dev-worker-fstrim.service)"
eq "the enabled marker" "dev_worker_fstrim_enabled 1" "$(grep -v '^#' "$TF/dev_worker_fstrim_enabled.prom")"

# ---- idempotence, and --check after apply ----
run again || { bad "second apply failed"; show again; }
eq "second apply changes nothing" 0 "$(changed)"
run check-after --check --diff || { bad "--check after apply failed"; show check-after; }
eq "--check after apply changes nothing" 0 "$(changed)"

# ---- the unit end to end: a real trim reaches the volume's backing file ----
dd if=/dev/urandom of=/workspace/blob bs=1M count=400 status=none && sync
rm /workspace/blob && sync && sleep 6
before=$(alloc_mib)
systemctl start dev-worker-fstrim.service
eq "the trim succeeded" "success 0" "$(systemctl show -p Result --value dev-worker-fstrim.service) $(systemctl show -p ExecMainStatus --value dev-worker-fstrim.service)"
after=$(alloc_mib)
[ $((before - after)) -ge 350 ] && ok "the freed 400 MiB came back ($before -> $after MiB allocated)" \
  || bad "the freed space came back: $before -> $after MiB allocated"
eq "beacon: done" 1 "$(metric 'node_fstrim_last_run_reason\{reason="done"\}')"
eq "beacon: /workspace complete" 1 "$(metric 'node_fstrim_complete\{mountpoint="/workspace"\}')"
[ -n "$(metric node_fstrim_last_success_timestamp_seconds)" ] && ok "a success time" || bad "no success time"
eq "the beacon is readable by node_exporter" 644 "$(stat -c %a "$TF/node_fstrim.prom")"
[ -s /var/lib/node-fstrim/_workspace.complete ] && ok "the state dir (StateDirectory) holds the complete pass" \
  || bad "no /var/lib/node-fstrim/_workspace.complete: $(ls -la /var/lib/node-fstrim 2>&1)"

# ---- refused settings ----
run badstart -e dev_worker_fstrim_start=0:30 && bad "an unpadded start was accepted" || ok "an unpadded start is refused"
run badmount -e '{"dev_worker_fstrim_mounts": ["workspace"]}' && bad "a relative mount was accepted" || ok "a relative mount is refused"
run badpsi -e dev_worker_fstrim_psi_max=45% && bad "psi_max 45% was accepted" || ok "psi_max 45% is refused"
run badpsi2 -e dev_worker_fstrim_psi_max=101 && bad "psi_max 101 was accepted" || ok "psi_max 101 is refused"

# ---- a root entry the regexes do not know (UUID=) that still has discard is refused, not silently kept ----
cp /etc/fstab /tmp/fstab.good
sed -i '1s/.*/UUID=0b6f8d2c-1111-2222-3333-444455556666 \/ ext4 discard,errors=remount-ro 0 1/' /etc/fstab
run uuidroot && bad "a UUID= root entry with discard was accepted" || ok "a UUID= root entry with discard is refused"
grep -q "still has .discard." /tmp/run-uuidroot.log && ok "and the play says why" || { bad "no reason given"; show uuidroot; }
eq "that entry is left as it was" "UUID=0b6f8d2c-1111-2222-3333-444455556666 / ext4 discard,errors=remount-ro 0 1" "$(head -1 /etc/fstab)"
cp /tmp/fstab.good /etc/fstab

# ---- a takedown interrupted after the units went (the stock timer still masked) is finished by the next one ----
systemctl disable --now dev-worker-fstrim.timer > /dev/null 2>&1
rm -f /etc/systemd/system/dev-worker-fstrim.timer /etc/systemd/system/dev-worker-fstrim.service /usr/local/sbin/node-fstrim
systemctl daemon-reload
eq "precondition: the units are gone, the stock timer masked, the settings left" "masked present" \
  "$(systemctl is-enabled fstrim.timer) $(exists /etc/default/dev-worker-fstrim)"

# ---- disable: the kill switch gives the disks back to the stock weekly timer ----
run disable -e dev_worker_fstrim_enabled=false -e dev_worker_root_nodiscard=false || { bad "disable failed"; show disable; }
eq "timer gone" absent "$(exists /etc/systemd/system/dev-worker-fstrim.timer)"
eq "service gone" absent "$(exists /etc/systemd/system/dev-worker-fstrim.service)"
eq "settings gone (the ownership marker, last)" absent "$(exists /etc/default/dev-worker-fstrim)"
eq "state gone" absent "$(exists /var/lib/node-fstrim)"
eq "script gone" absent "$(exists /usr/local/sbin/node-fstrim)"
eq "marker gone" absent "$(exists "$TF/dev_worker_fstrim_enabled.prom")"
eq "beacon gone" absent "$(exists "$TF/node_fstrim.prom")"
# Ubuntu's fstrim.timer has ConditionVirtualization=!container: started, it stays inactive in a container
# (ConditionResult=no) and goes active on a VM. Either way the role unmasked, enabled and started it.
eq "the stock weekly timer is back (enabled; started, its container condition unmet)" "enabled no" \
  "$(systemctl is-enabled fstrim.timer) $(systemctl show -p ConditionResult --value fstrim.timer)"
grep -A3 'Unmask and start the stock weekly fstrim.timer again' /tmp/run-disable.log | grep -q '^changed' && ok "the interrupted takedown was finished: the role restored it" || { bad "the stock timer was not restored"; show disable; }
eq "fstab keeps nodiscard (off does not put discard back)" "LABEL=cloudimg-rootfs	/	 ext4	commit=30,errors=remount-ro	0 1" "$(head -1 /etc/fstab)"
run disable-again -e dev_worker_fstrim_enabled=false -e dev_worker_root_nodiscard=false || { bad "second disable failed"; show disable-again; }
eq "a second disable changes nothing" 0 "$(changed)"

# ---- on again, then a clean takedown ----
run reapply || { bad "re-apply failed"; show reapply; }
eq "re-apply: the stock timer masked again" masked "$(systemctl is-enabled fstrim.timer)"
run disable2 -e dev_worker_fstrim_enabled=false -e dev_worker_root_nodiscard=false || { bad "clean disable failed"; show disable2; }
eq "clean takedown: units, settings and state gone; the stock timer enabled" "absent absent absent enabled" \
  "$(exists /etc/systemd/system/dev-worker-fstrim.timer) $(exists /etc/default/dev-worker-fstrim) $(exists /var/lib/node-fstrim) $(systemctl is-enabled fstrim.timer)"

umount /workspace; losetup -d "$LOOP"
[ "$fail" -eq 0 ] && echo "dev_worker fstrim converge: all cases passed" || echo "dev_worker fstrim converge: FAILURES"
exit "$fail"
