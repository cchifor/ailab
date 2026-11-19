#!/bin/bash
# The pve_ec_fan converge cases. Runs INSIDE the test container (systemd as PID 1), started by
# ../test-converge-docker.sh, which mounts the repo at /repo, a clone of the driver repo at /src and this
# directory at /t. Exit 1 if any case fails.
set -u
export PATH=/opt/ansible/bin:$PATH ANSIBLE_ROLES_PATH=/work/ansible/roles ANSIBLE_NOCOLOR=1
export ANSIBLE_STDOUT_CALLBACK=default ANSIBLE_CALLBACK_RESULT_FORMAT=yaml ANSIBLE_LOCALHOST_WARNING=False
rm -rf /work && mkdir -p /work && cp -r /repo/ansible /work/

K=7.0.2-6-pve # = pve_ec_fan_kernel in vars.yml and PVE_KERNEL in the Dockerfile
PIN=$(sed -n 's/^pve_ec_fan_commit: //p' /work/ansible/roles/pve_ec_fan/defaults/main.yml)
V="1.0+${PIN:0:7}"
OLDPIN=$(git -C /src rev-parse "$PIN~1")
OLDV="1.0+${OLDPIN:0:7}"
F=/fake/ec_su_axb35
P=/var/lib/prometheus/node-exporter/ec_fan.prom

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
versions() { dkms status -m ec_su_axb35 | cut -d, -f1 | cut -d: -f1 | sort -u | tr '\n' ' ' | sed 's/ $//'; }

# The driver's sysfs as plain files, with the driver's default curves; fan3's files get an old mtime.
mkfake() {
  rm -rf /fake
  mkdir -p $F/fan1 $F/fan2 $F/fan3 $F/temp1 $F/apu
  for n in 1 2 3; do
    echo auto > $F/fan$n/mode
    echo 1 > $F/fan$n/level
    echo 60,70,83,95,97 > $F/fan$n/rampup_curve
    echo 40,50,80,94,96 > $F/fan$n/rampdown_curve
  done
  echo 1540 > $F/fan1/rpm
  echo 1560 > $F/fan2/rpm
  echo 0 > $F/fan3/rpm
  echo 20,60,83,95,97 > $F/fan3/rampup_curve
  echo 0,50,80,94,96 > $F/fan3/rampdown_curve
  echo 52 > $F/temp1/temp
  echo balanced > $F/apu/power_mode
  touch -d 2020-01-01 $F/fan3/*
}
fan3_state() { echo "$(cat $F/fan3/mode) $(cat $F/fan3/rampup_curve) $(cat $F/fan3/rampdown_curve) newer=$(find $F/fan3 -type f -newermt 2020-01-02 | wc -l)"; }
FAN3_PRISTINE="auto 20,60,83,95,97 0,50,80,94,96 newer=0"

# The image has the kernel's headers but not its modules. DKMS skips depmod without a modules.dep and
# deletes an empty one, so give the kernel one real module, built against the same headers and
# xz-compressed like a packaged kernel's: DKMS then installs ec_su_axb35 compressed as well.
if [ ! -e /lib/modules/$K/kernel/dummy/ecfan_dummy.ko.xz ]; then
  mkdir -p /tmp/dummy /lib/modules/$K/kernel/dummy
  printf '#include <linux/module.h>\nMODULE_LICENSE("GPL");\n' > /tmp/dummy/ecfan_dummy.c
  echo 'obj-m += ecfan_dummy.o' > /tmp/dummy/Kbuild
  make -s -C /lib/modules/$K/build M=/tmp/dummy modules > /tmp/dummy.log 2>&1 || {
    cat /tmp/dummy.log
    exit 1
  }
  xz -c /tmp/dummy/ecfan_dummy.ko > /lib/modules/$K/kernel/dummy/ecfan_dummy.ko.xz
  depmod $K
fi
mkfake

# ---- a first --check changes nothing it cannot preview ----
run check-fresh --check --diff -e pve_ec_fan_enabled=true || { bad "fresh --check failed"; show check-fresh; }
eq "fresh --check wrote no unit" absent "$(exists /etc/systemd/system/ec-fan-profile.service)"
eq "fresh --check cloned nothing" absent "$(exists /usr/src/ec_su_axb35-$V)"

# ---- apply ----
run apply -e pve_ec_fan_enabled=true || { bad "apply failed"; show apply; }
eq "DKMS installed for $K" 1 "$(dkms status -m ec_su_axb35 -v "$V" -k $K | grep -c ': installed')"
eq "modprobe resolves the DKMS build" /lib/modules/$K/updates/dkms/ec_su_axb35.ko.xz "$(modinfo -k $K -n ec_su_axb35)"
eq "checkout at the pin" "$PIN" "$(git -C /usr/src/ec_su_axb35-$V rev-parse HEAD)"
eq "modules-load.d" ec_su_axb35 "$(grep -v '^#' /etc/modules-load.d/ec_su_axb35.conf)"
for n in 1 2; do
  eq "fan$n rampup" 35,50,60,68,95 "$(cat $F/fan$n/rampup_curve)"
  eq "fan$n rampdown" 30,45,55,63,90 "$(cat $F/fan$n/rampdown_curve)"
  eq "fan$n mode" curve "$(cat $F/fan$n/mode)"
done
eq "fan3 never written" "$FAN3_PRISTINE" "$(fan3_state)"
eq "first apply: the profile ran once" 1 "$(journalctl -u ec-fan-profile --no-pager | grep -c 'fan1/mode <- curve')"
eq "first apply: no auto in between" 0 "$(journalctl -u ec-fan-profile --no-pager | grep -c 'fan1/mode <- auto')"
eq "profile active" active "$(systemctl is-active ec-fan-profile.service)"
eq "profile enabled" enabled "$(systemctl is-enabled ec-fan-profile.service)"
eq "timer active" active "$(systemctl is-active ec-fan-textfile.timer)"
eq "units verify" ok "$(systemd-analyze verify /etc/systemd/system/ec-fan-profile.service \
  /etc/systemd/system/ec-fan-textfile.service /etc/systemd/system/ec-fan-textfile.timer > /tmp/verify.log 2>&1 && echo ok || cat /tmp/verify.log)"
sleep 3
eq "textfile fan1 rpm" 1 "$(grep -c '^ec_fan_rpm{fan="1"} 1540$' $P)"
eq "textfile fan3 rpm" 1 "$(grep -c '^ec_fan_rpm{fan="3"} 0$' $P)"
eq "textfile fan1 mode" 1 "$(grep -c '^ec_fan_mode_info{fan="1",mode="curve"} 1$' $P)"
eq "textfile temp" 1 "$(grep -c '^ec_temp_celsius 52$' $P)"
eq "textfile driver loaded" 1 "$(grep -c '^ec_fan_driver_loaded 1$' $P)"

# node_exporter, the Debian package the hosts run, reads the file without a scrape error.
prometheus-node-exporter --collector.disable-defaults --collector.textfile \
  --collector.textfile.directory=/var/lib/prometheus/node-exporter --web.listen-address=127.0.0.1:9199 > /tmp/ne.log 2>&1 &
NE=$!
sleep 2
curl -s 127.0.0.1:9199/metrics > /tmp/ne-metrics.txt
kill $NE
eq "node_exporter: no textfile scrape error" 1 "$(grep -c '^node_textfile_scrape_error 0$' /tmp/ne-metrics.txt)"
eq "node_exporter: three ec_fan_rpm series" 3 "$(grep -c '^ec_fan_rpm{' /tmp/ne-metrics.txt)"

# ---- idempotence ----
run apply-again -e pve_ec_fan_enabled=true || bad "second apply failed"
eq "second apply changed" 0 "$(changed)"
run check-after -e pve_ec_fan_enabled=true --check --diff || bad "--check after apply failed"
eq "--check after apply changed" 0 "$(changed)"

# ---- a kernel installed before the module was added (the next one, waiting for a reboot) gets it too ----
# proxmox-default-headers, installed by the apply, brought the newest headers; a /boot/vmlinuz makes that
# kernel "installed".
OTHER=$(for d in /lib/modules/*/; do k=$(basename "$d"); [ "$k" != $K ] && [ -e "${d}build/Makefile" ] && echo "$k"; done | head -1)
if [ -n "$OTHER" ]; then
  touch "/boot/vmlinuz-$OTHER"
  run other-kernel -e pve_ec_fan_enabled=true || { bad "converge with a second kernel failed"; show other-kernel; }
  eq "second kernel $OTHER: installed" 1 "$(dkms status -m ec_su_axb35 -v "$V" -k "$OTHER" | grep -c ': installed')"
  run other-kernel-again -e pve_ec_fan_enabled=true || bad "second converge with a second kernel failed"
  eq "second kernel: next run changed" 0 "$(changed)"
  rm -f "/boot/vmlinuz-$OTHER"
else
  bad "no second kernel's headers to test with (proxmox-default-headers should have brought them)"
fi

# ---- a current host converges without the network: the checkout is not fetched again ----
run offline -e pve_ec_fan_enabled=true -e pve_ec_fan_repo_url=/nonexistent || { bad "converge without the repo failed"; show offline; }
eq "offline: changed" 0 "$(changed)"

# ---- a curve change is applied even when a build for ANOTHER kernel then fails ----
# The handler flush comes before those builds, so the failure there cannot hold back the profile restart.
BROKEN=9.9.9-ecfan-broken
CURVE2='{"pve_ec_fan_rampup_curve": [40, 52, 62, 70, 95]}'
mkdir -p /lib/modules/$BROKEN/build
printf 'modules:\n\tfalse\n' > /lib/modules/$BROKEN/build/Makefile
touch /boot/vmlinuz-$BROKEN
if run broken-kernel -e pve_ec_fan_enabled=true -e "$CURVE2"; then bad "a failing build for another kernel did not fail the play"; else ok "a failing build for another kernel fails the play"; fi
eq "broken kernel: it failed at that build" "TASK [pve_ec_fan : Build and install the module for those kernels too]" \
  "$(grep -E '^TASK' /tmp/run-broken-kernel.log | tail -1 | sed 's/ \**$//')"
if grep -q 'RUNNING HANDLER \[pve_ec_fan : Restart ec-fan-profile\]' /tmp/run-broken-kernel.log; then ok "broken kernel: the restart handler ran first"; else bad "broken kernel: the restart handler did not run"; fi
eq "broken kernel: fan1 rampup" 40,52,62,70,95 "$(cat $F/fan1/rampup_curve)"
eq "broken kernel: fan2 rampup" 40,52,62,70,95 "$(cat $F/fan2/rampup_curve)"
eq "broken kernel: fan2 mode" curve "$(cat $F/fan2/mode)"
rm -rf /lib/modules/$BROKEN /boot/vmlinuz-$BROKEN /var/lib/dkms/ec_su_axb35/$V/$BROKEN
run broken-gone -e pve_ec_fan_enabled=true -e "$CURVE2" || { bad "converge after removing the broken kernel failed"; show broken-gone; }
eq "broken kernel gone: changed" 0 "$(changed)"

# ---- a fan dropped from the list goes back to auto (ExecStop reads the new config) ----
run drop-fan2 -e pve_ec_fan_enabled=true -e "$CURVE2" -e '{"pve_ec_fan_fans": [1]}' || { bad "dropping fan2 failed"; show drop-fan2; }
eq "drop fan2: fan1 mode" curve "$(cat $F/fan1/mode)"
eq "drop fan2: fan2 mode" auto "$(cat $F/fan2/mode)"
eq "drop fan2: fan3 still never written" "$FAN3_PRISTINE" "$(fan3_state)"
systemctl start ec-fan-textfile.service
eq "drop fan2: textfile says fan2 is not managed" 1 "$(grep -c '^ec_fan_managed{fan="2"} 0$' $P)"
run drop-fan2-again -e pve_ec_fan_enabled=true -e "$CURVE2" -e '{"pve_ec_fan_fans": [1]}' || bad "second converge with fan2 dropped failed"
eq "drop fan2: next run changed" 0 "$(changed)"
run fan2-back -e pve_ec_fan_enabled=true -e "$CURVE2" || { bad "adding fan2 back failed"; show fan2-back; }
eq "fan2 back: fan2 mode" curve "$(cat $F/fan2/mode)"
eq "fan2 back: fan2 rampup" 40,52,62,70,95 "$(cat $F/fan2/rampup_curve)"

# ---- recovery: the module reloaded (rmmod; modprobe) under the still-active service ----
# A container cannot load the module, so this makes the state a fresh load leaves: every fan on auto with
# the driver's own curves, while ec-fan-profile is still active (exited). `state: started` alone would leave
# it so; the converge must restart the profile.
eq "recovery: profile active before" active "$(systemctl is-active ec-fan-profile.service)"
mkfake
run recovery -e pve_ec_fan_enabled=true -e "$CURVE2" || { bad "recovery converge failed"; show recovery; }
eq "recovery: fan1 mode" curve "$(cat $F/fan1/mode)"
eq "recovery: fan2 mode" curve "$(cat $F/fan2/mode)"
eq "recovery: fan1 rampup" 40,52,62,70,95 "$(cat $F/fan1/rampup_curve)"
eq "recovery: fan3 never written" "$FAN3_PRISTINE" "$(fan3_state)"
run recovery-again -e pve_ec_fan_enabled=true -e "$CURVE2" || bad "converge after recovery failed"
eq "recovery: next run changed" 0 "$(changed)"

# ---- a bad profile is refused before any change ----
if run fan3 -e pve_ec_fan_enabled=true -e '{"pve_ec_fan_fans": [1, 2, 3]}'; then bad "fans [1,2,3] accepted"; else ok "fans [1,2,3] refused"; fi
if run hysteresis -e pve_ec_fan_enabled=true -e '{"pve_ec_fan_rampdown_curve": [30, 45, 60, 63, 90]}'; then bad "rampdown = rampup accepted"; else ok "rampdown = rampup refused"; fi
if run not-rising -e pve_ec_fan_enabled=true -e '{"pve_ec_fan_rampup_curve": [35, 60, 50, 68, 95]}'; then bad "non-rising curve accepted"; else ok "non-rising curve refused"; fi
if run strings -e pve_ec_fan_enabled=true -e '{"pve_ec_fan_rampup_curve": ["35", "50", "60", "68", "95"]}'; then bad "string curve accepted"; else ok "string curve refused"; fi
eq "refusals left the curve alone" 40,52,62,70,95 "$(cat $F/fan1/rampup_curve)"
eq "fan3 still never written" "$FAN3_PRISTINE" "$(fan3_state)"

# ---- disabling hands the fans back to auto and removes all but the DKMS build ----
# ... even after an apply that stopped between the profile unit and the textfile units: those are missing.
systemctl stop ec-fan-textfile.timer ec-fan-textfile.service
rm -f /etc/systemd/system/ec-fan-textfile.timer /etc/systemd/system/ec-fan-textfile.service
systemctl daemon-reload
run disable -e pve_ec_fan_enabled=false || { bad "disable failed"; show disable; }
eq "disable: fan1 mode" auto "$(cat $F/fan1/mode)"
eq "disable: fan2 mode" auto "$(cat $F/fan2/mode)"
eq "disable: fan3 still never written" "$FAN3_PRISTINE" "$(fan3_state)"
for p in /etc/systemd/system/ec-fan-profile.service /etc/systemd/system/ec-fan-textfile.timer \
  /etc/systemd/system/ec-fan-textfile.service /etc/default/ec-fan-profile /usr/local/sbin/ec-fan-profile \
  /usr/local/sbin/ec-fan-textfile /etc/modules-load.d/ec_su_axb35.conf $P; do
  eq "disable removed $p" absent "$(exists $p)"
done
eq "disable kept the DKMS build" 1 "$(dkms status -m ec_su_axb35 -k $K | grep -c ': installed')"
run disable-again -e pve_ec_fan_enabled=false || bad "second disable failed"
eq "second disable changed" 0 "$(changed)"

run reenable -e pve_ec_fan_enabled=true || { bad "re-enable failed"; show reenable; }
if grep -A2 'TASK \[pve_ec_fan : Build the module' /tmp/run-reenable.log | grep -q skipping; then ok "re-enable did not rebuild"; else bad "re-enable rebuilt"; fi
eq "re-enable: fan1 mode" curve "$(cat $F/fan1/mode)"

# ---- a new pin is a new DKMS version; the old one leaves DKMS and /usr/src ----
run repin -e pve_ec_fan_enabled=true -e "pve_ec_fan_commit=$OLDPIN" || { bad "re-pin failed"; show repin; }
eq "re-pin: only the new version in DKMS" "ec_su_axb35/$OLDV" "$(versions)"
eq "re-pin: old source removed" absent "$(exists /usr/src/ec_su_axb35-$V)"
run pin-back -e pve_ec_fan_enabled=true || { bad "pin back failed"; show pin-back; }
eq "pin back: only the pin" "ec_su_axb35/$V" "$(versions)"

# ---- an edited checkout is reset to the pin and rebuilt ----
echo '// local edit' >> /usr/src/ec_su_axb35-$V/src/ec_su_axb35.c
run tamper -e pve_ec_fan_enabled=true || { bad "converge after an edit failed"; show tamper; }
eq "edit: checkout clean again" "" "$(git -C /usr/src/ec_su_axb35-$V status --porcelain --untracked-files=no)"
if grep -A2 'TASK \[pve_ec_fan : Drop a build whose source' /tmp/run-tamper.log | grep -q 'changed:'; then ok "edit: stale build dropped and rebuilt"; else bad "edit: no rebuild"; fi
run tamper-again -e pve_ec_fan_enabled=true || bad "converge after the rebuild failed"
eq "after the rebuild, the next run changed" 0 "$(changed)"

# ---- kernel lockdown (Secure Boot) is refused before any change ----
mount -t tmpfs none /sys/kernel/security
echo 'none [integrity] confidentiality' > /sys/kernel/security/lockdown
if run lockdown -e pve_ec_fan_enabled=true; then bad "lockdown host accepted"; else ok "lockdown host refused"; fi
eq "lockdown: refused at the lockdown check" yes "$(grep -q 'lockdown=integrity, sig_enforce=absent, secureboot=absent' /tmp/run-lockdown.log && echo yes || echo no)"
eq "lockdown: nothing changed" 0 "$(changed)"
echo '[none] integrity confidentiality' > /sys/kernel/security/lockdown
run lockdown-none -e pve_ec_fan_enabled=true || { bad "lockdown none refused"; show lockdown-none; }
umount /sys/kernel/security

echo
if [ $fail -eq 0 ]; then
  echo "converge: ALL PASS"
else
  echo "converge: FAILURES"
  exit 1
fi
