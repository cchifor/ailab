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
# How often a unit's service was started by anything (its timer included): each start appends a line.
starts() { if [ -f "/run/$1.started" ]; then wc -l < "/run/$1.started"; else echo 0; fi; }
# How many trims have run (node-fstrim logs one `start:` per mount; one mount here). From the journal: the unit's
# own counters go when systemd unloads it, which a slot change does on purpose.
trims() { journalctl -q --no-pager -u dev-worker-fstrim.service | grep -c 'node-fstrim: start:'; }
epoch() { date -u -d "$1" +%s 2> /dev/null || echo 0; }
STAMP=/var/lib/systemd/timers/stamp-fstrim.timer
age_stamp() { touch -d '30 days ago' "$STAMP"; } # the stock timer masked for weeks, as on a worker
hhmm() { date -u -d "@$1" +%H:%M; }
next_minute() { echo $((($1 / 60 + 1) * 60)); } # the first whole minute after an epoch
wait_past() { while [ "$(date +%s)" -lt $(($1 + 2)) ]; do sleep 1; done; }
timer_cal() { systemctl show -p TimersCalendar --value dev-worker-fstrim.timer | sed -nE 's/.*OnCalendar=([^;]*[^ ;]) *;.*/\1/p'; }
next_elapse() { epoch "$(systemctl show -p NextElapseUSecRealtime --value dev-worker-fstrim.timer)"; }
# The scratch timer (below) moved to a new slot with a bare reload, as the role used to move its own.
ctl_slot() {
  sed -i "s/^OnCalendar=.*/OnCalendar=*-*-* $(hhmm "$1"):00 UTC/" /etc/systemd/system/ctl-slot.timer
  systemctl daemon-reload
  sleep 3
}

# ---- a dev worker in miniature: the cloud image's root fstab line, /workspace on a loop device ----
printf 'LABEL=cloudimg-rootfs\t/\t ext4\tdiscard,commit=30,errors=remount-ro\t0 1\nLABEL=UEFI\t/boot/efi\tvfat\tumask=0077\t0 1\nLABEL=workspace /workspace ext4 defaults,nofail,noatime 0 2\n' > /etc/fstab
FSTAB0="$(cat /etc/fstab)"
mkdir -p /ws /workspace && mount -t tmpfs -o size=2g tmpfs /ws
truncate -s 1G /ws/workspace.img && mkfs.ext4 -q -F -L workspace /ws/workspace.img
LOOP=$(losetup -f --show /ws/workspace.img) || { echo "no loop device"; exit 1; }
mount -o noatime "$LOOP" /workspace

# Ubuntu's fstrim.timer and fstrim.service carry ConditionVirtualization=!container, so here the timer would never
# go active and its catch-up could not be seen. Drop-ins (the test's, not the role's; they survive the mask) let it
# run, and make the service only record that it was started: no real fstrim in a test. systemd 255 schedules a
# Persistent timer's catch-up no earlier than its own start (boot) plus the random delay, so on a worker that has
# been up for more than RandomizedDelaySec=100min (+ AccuracySec=1h) the catch-up is already due and runs at once.
# This container's systemd started seconds ago: the drop-in zeroes both, to behave like that worker.
mkdir -p /etc/systemd/system/fstrim.timer.d /etc/systemd/system/fstrim.service.d /var/lib/systemd/timers
printf '[Unit]\nConditionVirtualization=\n[Timer]\nRandomizedDelaySec=0\nAccuracySec=1s\n' > /etc/systemd/system/fstrim.timer.d/test.conf
cat > /etc/systemd/system/fstrim.service.d/test.conf << 'EOF'
[Unit]
ConditionVirtualization=
[Service]
ExecStart=
ExecStart=/bin/sh -c 'echo started >> /run/fstrim.started'
EOF
systemctl daemon-reload

# CONTROL for the kill switch: on this systemd the stock timer, started with a stamp from weeks ago, catches up at
# once (Persistent=true). This is the run the role must not cause when it hands the disks back.
age_stamp
systemctl enable --now fstrim.timer > /dev/null 2>&1 # the Ubuntu default the role takes over from
sleep 3
eq "control: the stock timer started with a 30-day-old stamp runs at once" 1 "$(starts fstrim)"
eq "precondition: the stock weekly timer is on" "enabled active" "$(systemctl is-enabled fstrim.timer) $(systemctl is-active fstrim.timer)"

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
# It was running: stopped before the mask, it is inactive, not failed ("Unit to trigger vanished").
eq "the stock weekly timer is masked, and inactive rather than failed" "masked inactive" "$(systemctl is-enabled fstrim.timer) $(systemctl is-active fstrim.timer)"
eq "the slot" "*-*-* 00:30:00 UTC" "$(timer_cal)"
eq "TimeoutStartSec: 5 min + one 4 GiB chunk at 30 s/GiB + 1 min" 8min "$(systemctl show -p TimeoutStartUSec --value dev-worker-fstrim.service)"
eq "TimeoutStopSec: one chunk + 1 min" 3min "$(systemctl show -p TimeoutStopUSec --value dev-worker-fstrim.service)"
eq "ExecStart trims the configured mounts" "/usr/local/sbin/node-fstrim /workspace" "$(sed -n 's/^ExecStart=//p' /etc/systemd/system/dev-worker-fstrim.service)"
eq "the enabled marker" "dev_worker_fstrim_enabled 1" "$(grep -v '^#' "$TF/dev_worker_fstrim_enabled.prom")"

# A scratch timer started now, just after the role started its own, for the slot case's CONTROL below.
cat > /etc/systemd/system/ctl-slot.service << 'EOF'
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo started >> /run/ctl-slot.started'
EOF
printf '[Timer]\nOnCalendar=2099-01-01 00:00:00 UTC\nAccuracySec=1s\nPersistent=false\n' > /etc/systemd/system/ctl-slot.timer
systemctl daemon-reload && systemctl start ctl-slot.timer
CTL_ACTIVE=$(date +%s)

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

# ---- a new slot: it takes effect, and moving it never starts a trim ----
# systemd plans a loaded timer's next run from when it last fired (or was started, if it has not fired since), at
# every daemon-reload and every start, so a new slot between that time and now is due at once: on a worker,
# 00:30 -> 00:42 converged at 06:35. Two cases, each next to a CONTROL that makes the same change to the scratch
# timer with a bare reload (what the role did before) and sees its service start at once.
slot_case() { # <label> <slot epoch> <trim start before>: the role moves the slot; no trim, next due tomorrow
  run "$1" -e dev_worker_fstrim_start="$(hhmm "$2")" || { bad "$1: the slot change failed"; show "$1"; }
  sleep 3
  eq "$1: the unit has $(hhmm "$2")" "*-*-* $(hhmm "$2"):00 UTC" "$(timer_cal)"
  eq "$1: next due tomorrow at $(hhmm "$2"), not today" "$(($2 + 86400))" "$(next_elapse)"
  eq "$1: the change started no trim" "$3" "$(trims)"
  grep -A2 'Stop and disable the timer before its unit file changes' "/tmp/run-$1.log" | grep -q '^changed' \
    && ok "$1: the timer was stopped and disabled before its file changed" || { bad "$1: the timer was not stopped first"; show "$1"; }
}
# (1) The timer has not fired since it was started (a converge soon after a boot, or after enabling).
S1=$(next_minute "$CTL_ACTIVE")
wait_past "$S1"
ctl_slot "$S1"
eq "control 1 (not fired yet): a bare reload starts the scratch timer's service at once" 1 "$(starts ctl-slot)"
slot_case slot1 "$S1" "$(trims)"
# (2) The timer has fired at its slot (a worker the morning after its night). S2 leaves the converge time to finish.
S2=$(next_minute $(($(date +%s) + 30)))
ctl_slot "$S2" # not due yet: the scratch timer fires at S2 as well
t0=$(trims)
run slot2 -e dev_worker_fstrim_start="$(hhmm "$S2")" || { bad "slot2: the slot change failed"; show slot2; }
eq "slot2: a slot still ahead starts no trim at the change" "$t0" "$(trims)"
wait_past "$S2"
sleep 3
fired=$(epoch "$(systemctl show -p ExecMainStartTimestamp --value dev-worker-fstrim.service)")
{ [ "$fired" -ge "$S2" ] && [ "$fired" -lt $((S2 + 10)) ]; } && ok "slot2: the new slot took effect: the trim ran at $(hhmm "$S2")" \
  || bad "slot2: no trim at $(hhmm "$S2") (last start $(systemctl show -p ExecMainStartTimestamp --value dev-worker-fstrim.service))"
eq "slot2: exactly one trim, at the new slot" "$((t0 + 1))" "$(trims)"
eq "slot2: the scratch timer fired at $(hhmm "$S2") too" 2 "$(starts ctl-slot)"
S3=$((S2 + 60))
wait_past "$S3"
ctl_slot "$S3"
eq "control 2 (fired since): a bare reload, the new slot between that run and now, starts the service at once" 3 "$(starts ctl-slot)"
slot_case slot3 "$S3" "$(trims)"
t0=$(trims)
run slot-back || { bad "the slot change back failed"; show slot-back; }
sleep 2
eq "back to the slot in vars.yml: no trim either" "$t0" "$(trims)"
eq "and the timer is enabled and active again" "enabled active" "$(systemctl is-enabled dev-worker-fstrim.timer) $(systemctl is-active dev-worker-fstrim.timer)"
run slot-same || { bad "a converge after the slot change failed"; show slot-same; }
eq "an unchanged slot changes nothing (the timer is not stopped)" 0 "$(changed)"

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

# ---- disable: the kill switch gives the disks back to the stock weekly timer, without a catch-up run ----
# kill_switch_ok <label> <stock starts before> <time before>: the stock timer is back, did not run at once, and plans
# its next run from the stamp the role set (now): the next weekly boundary, Monday 00:00 (no random delay here).
kill_switch_ok() {
  sleep 3
  eq "$1: the stock timer is enabled and active again" "enabled active" "$(systemctl is-enabled fstrim.timer) $(systemctl is-active fstrim.timer)"
  eq "$1: it did not catch up the masked weeks at once" "$2" "$(starts fstrim)"
  [ "$(stat -c %Y "$STAMP")" -ge "$3" ] && ok "$1: its stamp is from this converge" || bad "$1: its stamp is $(stat -c %y "$STAMP")"
  eq "$1: its next run is the next weekly boundary" "$(date -d 'next monday 00:00' +%s)" \
    "$(epoch "$(systemctl show -p NextElapseUSecRealtime --value fstrim.timer)")"
}
age_stamp
s0=$(starts fstrim) t0=$(date +%s)
run disable -e dev_worker_fstrim_enabled=false -e dev_worker_root_nodiscard=false || { bad "disable failed"; show disable; }
kill_switch_ok "kill switch (interrupted takedown)" "$s0" "$t0"
eq "timer gone" absent "$(exists /etc/systemd/system/dev-worker-fstrim.timer)"
eq "service gone" absent "$(exists /etc/systemd/system/dev-worker-fstrim.service)"
eq "settings gone (the ownership marker, last)" absent "$(exists /etc/default/dev-worker-fstrim)"
eq "state gone" absent "$(exists /var/lib/node-fstrim)"
eq "script gone" absent "$(exists /usr/local/sbin/node-fstrim)"
eq "marker gone" absent "$(exists "$TF/dev_worker_fstrim_enabled.prom")"
eq "beacon gone" absent "$(exists "$TF/node_fstrim.prom")"
grep -A3 'Unmask and start the stock weekly fstrim.timer again' /tmp/run-disable.log | grep -q '^changed' && ok "the interrupted takedown was finished: the role restored it" || { bad "the stock timer was not restored"; show disable; }
eq "fstab keeps nodiscard (off does not put discard back)" "LABEL=cloudimg-rootfs	/	 ext4	commit=30,errors=remount-ro	0 1" "$(head -1 /etc/fstab)"
run disable-again -e dev_worker_fstrim_enabled=false -e dev_worker_root_nodiscard=false || { bad "second disable failed"; show disable-again; }
eq "a second disable changes nothing" 0 "$(changed)"

# ---- on again, then a clean takedown ----
run reapply || { bad "re-apply failed"; show reapply; }
eq "re-apply: the stock timer masked again" "masked inactive" "$(systemctl is-enabled fstrim.timer) $(systemctl is-active fstrim.timer)"
age_stamp
s0=$(starts fstrim) t0=$(date +%s)
run disable2 -e dev_worker_fstrim_enabled=false -e dev_worker_root_nodiscard=false || { bad "clean disable failed"; show disable2; }
eq "clean takedown: units, settings and state gone" "absent absent absent" \
  "$(exists /etc/systemd/system/dev-worker-fstrim.timer) $(exists /etc/default/dev-worker-fstrim) $(exists /var/lib/node-fstrim)"
kill_switch_ok "kill switch (clean takedown)" "$s0" "$t0"

umount /workspace; losetup -d "$LOOP"
[ "$fail" -eq 0 ] && echo "dev_worker fstrim converge: all cases passed" || echo "dev_worker fstrim converge: FAILURES"
exit "$fail"
