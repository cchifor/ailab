#!/bin/bash
# The pve_base lxc_fstrim converge cases. Runs INSIDE the test container (systemd as PID 1), started by
# ../test-lxc-fstrim-converge-docker.sh, which mounts the repo at /repo and this directory at /t.
# `pct`, `pvesm` and `lvs` are stubs; the container volume is a REAL ext4 filesystem on a loop device
# whose backing file is sparse, so a trim that reaches it punches holes the test can measure. Exit 1 if
# any case fails.
set -u
export PATH=/opt/ansible/bin:$PATH ANSIBLE_ROLES_PATH=/work/ansible/roles ANSIBLE_NOCOLOR=1
export ANSIBLE_STDOUT_CALLBACK=default ANSIBLE_CALLBACK_RESULT_FORMAT=yaml ANSIBLE_LOCALHOST_WARNING=False
rm -rf /work && mkdir -p /work && cp -r /repo/ansible /repo/scripts /work/

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
metric() { sed -nE "s/^$1 (.*)$/\1/p" "$TF/pve_lxc_fstrim.prom" 2>/dev/null; }
alloc_mib() { echo $(( $(stat -c %b /ct/vm-5002-disk-1.img) * $(stat -c %B /ct/vm-5002-disk-1.img) / 1048576 )); }

# ---- a Proxmox host in miniature: container 5002 running, its mp1 a real ext4 volume ----
mkdir -p /ct /ctmnt && mount -t tmpfs -o size=2g tmpfs /ct
truncate -s 1G /ct/vm-5002-disk-1.img
mkfs.ext4 -q -F /ct/vm-5002-disk-1.img
LOOP=$(losetup -f --show /ct/vm-5002-disk-1.img) || { echo "no loop device"; exit 1; }
# The container's own mount of the volume. The sweep's host-side mount shares its superblock, as on a
# Proxmox host, so ext4's per-group "trimmed" flags last from one night to the next.
mount "$LOOP" /ctmnt
cat > /usr/local/bin/pct << 'STUB'
#!/bin/bash
case "$1" in
  list) printf 'VMID       Status     Lock         Name\n5002       running                 ai-llm-2\n5003       stopped                 ai-llm-3\n' ;;
  config) [ "$2" = 5002 ] && printf 'hostname: ai-llm-2\nmp0: /mnt/pve/qnap-nfs/models,mp=/models\nmp1: local-lvm:vm-5002-disk-1,mp=/models-local,size=1G\n' ;;
  status) [ "$2" = 5002 ] && echo "status: running" || echo "status: stopped" ;;
esac
STUB
printf '#!/bin/bash\n[ "$1" = path ] && [ "$2" = local-lvm:vm-5002-disk-1 ] && echo %s\n' "$LOOP" > /usr/local/bin/pvesm
printf '#!/bin/bash\necho "  50.00"\n' > /usr/local/bin/lvs
chmod +x /usr/local/bin/pct /usr/local/bin/pvesm /usr/local/bin/lvs

# ---- a first --check changes nothing ----
run check-fresh --check --diff || { bad "fresh --check failed"; show check-fresh; }
eq "fresh --check wrote no unit" absent "$(exists /etc/systemd/system/pve-lxc-fstrim.timer)"
eq "fresh --check installed no sweep" absent "$(exists /usr/local/sbin/pve-lxc-fstrim)"

# ---- apply ----
run apply || { bad "apply failed"; show apply; }
eq "the chunked trim is the repo's script" "" "$(cmp /work/scripts/lxc-fstrim-chunked.sh /usr/local/sbin/lxc-fstrim-chunked.sh)"
eq "the sweep is installed" "" "$(cmp /work/ansible/roles/pve_base/files/pve-lxc-fstrim.sh /usr/local/sbin/pve-lxc-fstrim)"
eq "timer enabled" enabled "$(systemctl is-enabled pve-lxc-fstrim.timer)"
eq "timer active" active "$(systemctl is-active pve-lxc-fstrim.timer)"
eq "the converge did not start a trim" inactive "$(systemctl is-active pve-lxc-fstrim.service)"
eq "the slot" "*-*-* 04:30:00 UTC" "$(systemctl show -p TimersCalendar --value pve-lxc-fstrim.timer | sed -nE 's/.*OnCalendar=([^;]*[^ ;]) *;.*/\1/p')"
eq "TimeoutStartSec: 13 min + one 4 GiB chunk at 30 s/GiB + 1 min" 16min "$(systemctl show -p TimeoutStartUSec --value pve-lxc-fstrim.service)"
eq "TimeoutStopSec: one chunk + 1 min" 3min "$(systemctl show -p TimeoutStopUSec --value pve-lxc-fstrim.service)"
eq "the enabled marker" "pve_lxc_fstrim_enabled 1" "$(grep -v '^#' "$TF/pve_lxc_fstrim_enabled.prom")"
eq "the settings" 'PVE_LXC_FSTRIM_RUN_MAX_S=780 CHUNK_GB=4 PAUSE_IDLE_S=1' \
  "$(grep -E '^(PVE_LXC_FSTRIM_RUN_MAX_S|CHUNK_GB|PAUSE_IDLE_S)=' /etc/default/pve-lxc-fstrim | tr '\n' ' ' | sed 's/ $//')"

# ---- idempotence, and --check after apply ----
run again || { bad "second apply failed"; show again; }
eq "second apply changes nothing" 0 "$(changed)"
run check-after --check --diff || { bad "--check after apply failed"; show check-after; }
eq "--check after apply changes nothing" 0 "$(changed)"

# ---- the unit end to end: a real trim reaches the volume's backing file ----
dd if=/dev/urandom of=/ctmnt/blob bs=1M count=400 status=none && sync
rm /ctmnt/blob && sync && sleep 6 # past ext4's commit, so the blocks are free
before=$(alloc_mib)
systemctl start pve-lxc-fstrim.service
eq "the sweep succeeded" "success 0" "$(systemctl show -p Result --value pve-lxc-fstrim.service) $(systemctl show -p ExecMainStatus --value pve-lxc-fstrim.service)"
after=$(alloc_mib)
[ $((before - after)) -ge 350 ] && ok "the trim gave back the freed 400 MiB ($before -> $after MiB allocated)" \
  || bad "the trim gave back the freed space: $before -> $after MiB allocated"
eq "reason done" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="done"\}')"
eq "the volume's exit code" 0 "$(metric 'pve_lxc_fstrim_volume_exit_code\{ctid="5002",volume="mp1"\}')"
bytes=$(metric 'pve_lxc_fstrim_volume_trimmed_bytes\{ctid="5002",volume="mp1"\}')
[ "${bytes:-0}" -gt 0 ] && ok "ext4 reported $bytes bytes trimmed" || bad "no bytes reported trimmed"
[ -n "$(metric pve_lxc_fstrim_last_success_timestamp_seconds)" ] && ok "a success time" || bad "no success time"
eq "the bind mount was skipped" 1 "$(journalctl -u pve-lxc-fstrim --no-pager | grep -c 'mp0: bind mount')"
eq "the host-side mount was released" 0 "$(findmnt -rn -o TARGET | grep -c lxc-fstrim)"
eq "the container's own mount is untouched" /ctmnt "$(findmnt -rn -o TARGET "$LOOP")"

# ---- the next night: ext4 remembers what it trimmed (shared superblock), so nothing is sent again ----
systemctl start pve-lxc-fstrim.service
eq "second night: 0 bytes" 0 "$(metric 'pve_lxc_fstrim_volume_trimmed_bytes\{ctid="5002",volume="mp1"\}')"
eq "second night: reason done" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="done"\}')"

# ---- a new slot ----
run newslot -e pve_lxc_fstrim_start=04:45 || { bad "new slot failed"; show newslot; }
eq "the timer moved" "*-*-* 04:45:00 UTC" "$(systemctl show -p TimersCalendar --value pve-lxc-fstrim.timer | sed -nE 's/.*OnCalendar=([^;]*[^ ;]) *;.*/\1/p')"

# ---- refused settings ----
run badstart -e pve_lxc_fstrim_start=4:30 && bad "an unpadded start was accepted" || ok "an unpadded start is refused"
run badexclude -e '{"pve_lxc_fstrim_exclude": ["abc"]}' && bad "a non-numeric exclude was accepted" || ok "a non-numeric exclude is refused"

# ---- disable: the kill switch ----
run disable -e pve_lxc_fstrim_enabled=false || { bad "disable failed"; show disable; }
eq "timer gone" absent "$(exists /etc/systemd/system/pve-lxc-fstrim.timer)"
eq "service gone" absent "$(exists /etc/systemd/system/pve-lxc-fstrim.service)"
eq "settings gone" absent "$(exists /etc/default/pve-lxc-fstrim)"
eq "sweep gone" absent "$(exists /usr/local/sbin/pve-lxc-fstrim)"
eq "marker gone" absent "$(exists "$TF/pve_lxc_fstrim_enabled.prom")"
eq "beacon gone" absent "$(exists "$TF/pve_lxc_fstrim.prom")"
eq "the chunked trim stays (the registry unit runs it)" present "$(exists /usr/local/sbin/lxc-fstrim-chunked.sh)"
eq "systemd no longer knows the timer" "" "$(systemctl list-unit-files pve-lxc-fstrim.timer --no-legend)"
run disable-again -e pve_lxc_fstrim_enabled=false || { bad "second disable failed"; show disable-again; }
eq "a second disable changes nothing" 0 "$(changed)"

umount /ctmnt; losetup -d "$LOOP"
[ "$fail" -eq 0 ] && echo "lxc_fstrim converge: all cases passed" || echo "lxc_fstrim converge: FAILURES"
exit "$fail"
