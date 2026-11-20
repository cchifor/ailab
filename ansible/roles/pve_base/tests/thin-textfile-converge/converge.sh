#!/bin/bash
# The pve_base thin_textfile converge cases. Runs INSIDE the test container (systemd as PID 1), started by
# ../test-thin-textfile-converge-docker.sh, which mounts the repo at /repo and this directory at /t.
# Docker Desktop's kernel has no dm-thin-pool, so the pool is two-part:
#   1. REAL LVM: a VG `pve` on a loop device with a linear `root`. The real `lvs` is run the way the script runs
#      it, so its output format (field order, separator, plain byte counts) is checked, and the unit is run
#      under its own hardening against real LVM locking (the pool is missing there: one read error, by design).
#   2. A stub `lvs` (/usr/local/sbin, first on the unit's PATH) printing a Proxmox-like thin listing in that
#      same format, for the pool and volume numbers.
# Exit 1 if any case fails.
set -u
export PATH=/opt/ansible/bin:$PATH ANSIBLE_ROLES_PATH=/work/ansible/roles ANSIBLE_NOCOLOR=1
export ANSIBLE_STDOUT_CALLBACK=default ANSIBLE_CALLBACK_RESULT_FORMAT=yaml ANSIBLE_LOCALHOST_WARNING=False
rm -rf /work && mkdir -p /work && cp -r /repo/ansible /work/

TF=/var/lib/prometheus/node-exporter
LOOP=""
teardown() {
  vgremove -q -f pve > /dev/null 2>&1
  [ -n "$LOOP" ] && { pvremove -q -ff -y "$LOOP" > /dev/null 2>&1; losetup -d "$LOOP"; }
}
trap teardown EXIT
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
  ansible-playbook -i localhost, /t/play.yml "$@" > "/tmp/run-$label.log" 2>&1
  rc=$?
  RECAP=$(grep -E '^localhost +:' "/tmp/run-$label.log" | tail -1)
  echo "== $label: rc=$rc  $RECAP"
  return $rc
}
show() { tail -40 "/tmp/run-$1.log"; }
changed() { sed -nE 's/.*changed=([0-9]+).*/\1/p' <<< "$RECAP"; }
metric() { sed -nE "s|^$1 (.*)$|\1|p" "$TF/pve_thin.prom" 2> /dev/null; }

# ---- 1. real LVM: VG pve with a linear root ----
mkdir -p /pv && mount -t tmpfs -o size=600m tmpfs /pv && truncate -s 500M /pv/pv.img
LOOP=$(losetup -f --show /pv/pv.img) || { echo "no loop device"; exit 1; }
{ pvcreate -q "$LOOP" && vgcreate -q pve "$LOOP" && lvcreate -q -y -an -Zn -L 64M -n root pve; } > /tmp/lvm.log 2>&1 \
  || { cat /tmp/lvm.log; echo "cannot build the test VG"; exit 1; }
real="$(lvs --noheadings --units b --nosuffix --separator '|' \
  -o lv_name,pool_lv,segtype,lv_size,data_percent,metadata_percent,lv_metadata_size pve)"
eq "real lvs: the line the script parses" "  root||linear|67108864|||" "$real"

# ---- --check, apply, idempotence ----
run check-fresh --check --diff || { bad "fresh --check failed"; show check-fresh; }
eq "fresh --check wrote no unit" absent "$(exists /etc/systemd/system/pve-thin-textfile.timer)"
run apply || { bad "apply failed"; show apply; }
eq "the script is the role's" "" "$(cmp /work/ansible/roles/pve_base/files/pve-thin-textfile.sh /usr/local/sbin/pve-thin-textfile)"
eq "timer enabled and active" "enabled active" "$(systemctl is-enabled pve-thin-textfile.timer) $(systemctl is-active pve-thin-textfile.timer)"
run again || { bad "second apply failed"; show again; }
eq "second apply changes nothing" 0 "$(changed)"
run check-after --check --diff || { bad "--check after apply failed"; show check-after; }
eq "--check after apply changes nothing" 0 "$(changed)"

# ---- the unit under its hardening, against the real lvs (and LVM's locking) ----
systemctl start pve-thin-textfile.service
eq "the unit succeeded with the real lvs" "success 0" "$(systemctl show -p Result --value pve-thin-textfile.service) $(systemctl show -p ExecMainStatus --value pve-thin-textfile.service)"
eq "the missing pool is one read error" 1 "$(metric pve_thin_textfile_read_errors)"
[ -n "$(metric pve_thin_textfile_timestamp_seconds)" ] && ok "the file was written" || bad "no timestamp"
eq "no lvs complaint in the journal" 0 "$(journalctl -u pve-thin-textfile --no-pager | grep -ciE 'lock|permission|read-only')"

# ---- 2. a Proxmox-like thin listing, in the format the real lvs printed above ----
cat > /usr/local/sbin/lvs << 'STUB'
#!/bin/bash
printf '  data||thin-pool|1884114714624|61.23|2.29|16106127360\n  root||linear|103079215104|||\n  vm-5002-disk-1|data|thin|171798691840|80.50||\n  base-9000-disk-0|data|thin|3758096384|||\n'
STUB
chmod +x /usr/local/sbin/lvs
systemctl start pve-thin-textfile.service
eq "no read errors" 0 "$(metric pve_thin_textfile_read_errors)"
eq "pool data%" 61.23 "$(metric 'pve_thin_pool_data_percent\{pool="pve/data"\}')"
eq "pool metadata%" 2.29 "$(metric 'pve_thin_pool_metadata_percent\{pool="pve/data"\}')"
eq "the volume's allocation (171798691840 x 80.50 %)" 138297946931 "$(metric 'pve_thin_lv_allocated_bytes\{pool="pve/data",lv="vm-5002-disk-1",vmid="5002"\}')"
eq "the inactive template volume is left out" 0 "$(grep -c base-9000 "$TF/pve_thin.prom")"
eq "readable by node_exporter" 644 "$(stat -c %a "$TF/pve_thin.prom")"
rm -f /usr/local/sbin/lvs

# ---- disable ----
run disable -e pve_thin_textfile_enabled=false || { bad "disable failed"; show disable; }
eq "timer gone" absent "$(exists /etc/systemd/system/pve-thin-textfile.timer)"
eq "metrics gone" absent "$(exists "$TF/pve_thin.prom")"
eq "script gone" absent "$(exists /usr/local/sbin/pve-thin-textfile)"
run disable-again -e pve_thin_textfile_enabled=false || { bad "second disable failed"; show disable-again; }
eq "a second disable changes nothing" 0 "$(changed)"

[ "$fail" -eq 0 ] && echo "thin_textfile converge: all cases passed" || echo "thin_textfile converge: FAILURES"
exit "$fail"
