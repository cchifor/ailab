#!/usr/bin/env bash
# Put the Thunderbolt bridge's service IP (tbtbr0, 10.55.0.254/24) into QNAP's OWN network config,
# so the address exists when the iSCSI target binds its portals at boot.
#
# WHY (2026-11-24; runbook qnap-storage-setup.md sections 2 and 12). QNAP's config had tbtbr0 as
# DHCP. Nothing serves DHCP on the Thunderbolt link, so at boot S45network (uptime ~86 s) gave the
# bridge an avahi link-local 169.254.x.x, and S46iSCSItrgt (~116-191 s) bound every target's
# allowed portal to it. 10.55.0.254 arrived later, from the cron reconciler
# /etc/config/tb-storage-ip.sh (crond is S70). After a cold boot every iSCSI login was refused
# (2026-10-30, 2026-11-24). With ip4_type=STATIC, S45network puts 10.55.0.254 on the bridge
# before S46 runs.
#
# S99thunderbolt.sh does NOT rebuild the bridge on a normal boot: it logs "Remove tbtbr0" every
# time, but deletes only when /tmp/nasconfig_tmp/tbtbr_disable exists, and creates only when
# QNAP's network manager is absent. So the address set at S45 stays. The cron reconciler is kept
# until a boot proves this config (runbook §2), and the iSCSI portal watchdog
# (scripts/qnap-iscsi-portal-watchdog.sh) stays as the backstop.
#
# This WRITES CONFIG ONLY, through QNAP's network CLI (`nacli setcfg`). Nothing is applied to the
# live bridge; the value takes effect at the next NAS boot. Measured 2026-11-24: QNAP's own log of
# a setcfg call shows load through ncaas (the network config store), change one key, save, exit.
# Applying is a separate command (`nacli setup_by_config` / `reconfig`). No process holds an inotify
# watch on nm.conf that belongs to the network stack (only hda_monitor and qmmsd watch the config
# directory), and nm.conf is not rewritten after boot.
#
# Still, a live re-apply would flush 10.55.0.254, the address that carries the control-plane disks
# over NFS. So a write while iSCSI sessions are logged in needs the explicit LIVE_WRITE=1. A live
# write is then followed by a 30 s check: the bridge must keep the address (it is re-added at once
# and the run fails if it goes). A lower session count is reported, not failed on.
#
#   bash scripts/qnap-tbtbr0-static-ip.sh                      # read-only: current vs wanted (default)
#   APPLY=1 LIVE_WRITE=1 bash scripts/qnap-tbtbr0-static-ip.sh  # write STATIC 10.55.0.254/24 now
#   REVERT=1 LIVE_WRITE=1 bash scripts/qnap-tbtbr0-static-ip.sh # back to QNAP's default (DHCP)
# (with the cluster down, i.e. sessions=0, LIVE_WRITE is not needed)
#
# Rehearsal on a scratch copy (no interlock; the copy is not what the network daemon reads):
#   python scripts/qnap-ssh.py --sudo "cp /etc/config/nm.conf /tmp/nm.conf.rehearse"
#   NM_CONF=/tmp/nm.conf.rehearse APPLY=1 bash scripts/qnap-tbtbr0-static-ip.sh
#   NM_CONF=/tmp/nm.conf.rehearse REVERT=1 bash scripts/qnap-tbtbr0-static-ip.sh
#
# Every write first saves a timestamped pre-image next to the file, and restores it on any failed write
# or read-back mismatch.
#
# Prereqs: .env filled (QNAP_SSH_USER / QNAP_ADMIN_PASSWORD), SSH enabled on the QNAP.
set -euo pipefail
cd "$(dirname "$0")/.."

SVC_IP="${SVC_IP:-10.55.0.254}"
NETMASK="${NETMASK:-255.255.255.0}"
# Prefix length for the live re-add, derived from NETMASK (contiguous masks only).
PREFIX=0
ended=0   # after the first octet below 255, every later octet must be 0
IFS=. read -r o1 o2 o3 o4 <<< "$NETMASK"
for o in "$o1" "$o2" "$o3" "$o4"; do
  case "$o" in
    255|254|252|248|240|224|192|128|0) ;;
    *) echo "NETMASK $NETMASK is not a valid mask" >&2; exit 2 ;;
  esac
  if [ "$ended" = 1 ] && [ "$o" != 0 ]; then echo "NETMASK $NETMASK is not contiguous" >&2; exit 2; fi
  case "$o" in
    255) PREFIX=$((PREFIX + 8)) ;; 254) PREFIX=$((PREFIX + 7)) ;; 252) PREFIX=$((PREFIX + 6)) ;;
    248) PREFIX=$((PREFIX + 5)) ;; 240) PREFIX=$((PREFIX + 4)) ;; 224) PREFIX=$((PREFIX + 3)) ;;
    192) PREFIX=$((PREFIX + 2)) ;; 128) PREFIX=$((PREFIX + 1)) ;;
  esac
  [ "$o" = 255 ] || ended=1
done
APPLY="${APPLY:-0}"
REVERT="${REVERT:-0}"
LIVE_WRITE="${LIVE_WRITE:-0}"
LIVE_CONF=/etc/config/nm.conf
NM_CONF="${NM_CONF:-$LIVE_CONF}"
if [ "$APPLY" = 1 ] && [ "$REVERT" = 1 ]; then echo "APPLY and REVERT are exclusive" >&2; exit 2; fi
case "$NM_CONF" in
  "$LIVE_CONF"|/tmp/*) ;;
  *) echo "NM_CONF must be $LIVE_CONF or a scratch copy under /tmp/" >&2; exit 2 ;;
esac

if [ "$REVERT" = 1 ]; then
  MODE=write; WANT_TYPE=DHCP;   WANT_IP="";      WANT_MASK=255.255.0.0   # QNAP's own default for tbtbr0
elif [ "$APPLY" = 1 ]; then
  MODE=write; WANT_TYPE=STATIC; WANT_IP=$SVC_IP; WANT_MASK=$NETMASK
else
  MODE=show;  WANT_TYPE=STATIC; WANT_IP=$SVC_IP; WANT_MASK=$NETMASK
fi

# Fed on STDIN (not argv), like the other QNAP installers. `\$` is evaluated on the NAS.
REMOTE=$(cat <<EOF
set -eu
CONF="$NM_CONF"
NACLI=/usr/local/bin/nacli
[ -f "\$CONF" ] || { echo "FATAL: \$CONF does not exist"; exit 1; }
[ -x "\$NACLI" ] || { echo "FATAL: \$NACLI is missing (did a QuTS update move it?)"; exit 1; }
grep -q '^\[tbtbr0\]' "\$CONF" || { echo "FATAL: no [tbtbr0] section in \$CONF"; exit 1; }
get() { "\$NACLI" getcfg -f "\$CONF" tbtbr0 "\$1" 2>/dev/null; }
live() { ip -4 -o addr show tbtbr0 2>/dev/null | awk '{ print \$4 }' | tr '\n' ' '; }
# NAS-wide logged-in iSCSI sessions. A failed query or a target record without a parseable sess_cnt
# is "unknown". A successful query with no target records (a fresh NAS) is 0.
count_sessions() {
  out=\$(iscsiadmin get_target 2>/dev/null) && rc=0 || rc=\$?
  if [ "\$rc" != 0 ]; then echo unknown; return 0; fi
  printf '%s\n' "\$out" | awk '/^target = / { n++; if (match(\$0, /sess_cnt = [0-9]+/)) { s += substr(\$0, RSTART + 11, RLENGTH - 11); k++ } } END { if (k == n) print s + 0; else print "unknown" }'
}

echo "== tbtbr0 in \$CONF =="
echo "  now:  ip4_type=\$(get ip4_type) ip=\$(get ip) subnet_mask=\$(get subnet_mask)"
echo "  want: ip4_type=$WANT_TYPE ip=$WANT_IP subnet_mask=$WANT_MASK"
echo "  live: \$(live)"
if [ "\$(get ip4_type)|\$(get ip)|\$(get subnet_mask)" = "$WANT_TYPE|$WANT_IP|$WANT_MASK" ]; then
  echo "  already as wanted; nothing to write"
  exit 0
fi
if [ "$MODE" = show ]; then
  echo "  read-only. APPLY=1 writes it (plus LIVE_WRITE=1 while the cluster is up)."
  exit 0
fi

# Interlock (live config only). With sessions logged in, a write needs LIVE_WRITE=1. An unknown count
# always refuses: the post-write check could not tell a dropped session from a parse failure.
if [ "\$CONF" = "$LIVE_CONF" ]; then
  sessions=\$(count_sessions)
  echo "== interlock: NAS-wide iSCSI sessions=\$sessions =="
  if [ "\$sessions" = unknown ]; then
    echo "FATAL: the session count is unknown; not writing."
    exit 1
  fi
  if [ "\$sessions" != 0 ] && [ "$LIVE_WRITE" != 1 ]; then
    echo "FATAL: sessions=\$sessions. Pass LIVE_WRITE=1 to write while the cluster is up (config-only; checked for 30 s after)."
    exit 1
  fi
else
  echo "== rehearsal on a scratch copy (\$CONF): no interlock, the live config is not touched =="
fi

# Every write keeps its own pre-image. From the first write until the read-back is verified, an EXIT
# trap puts it back on ANY abort (a failed setcfg, a failed getcfg under set -e, a mismatch): the
# next boot applies this file, so a half-written section must never survive.
BAK="\$CONF.bak-tbtbr0-\$(date +%Y%m%d-%H%M%S)"
cp -f "\$CONF" "\$BAK"
echo "  pre-image: \$BAK"
trap 'cp -f "\$BAK" "\$CONF" && echo "  ABORTED: restored \$CONF from \$BAK"' EXIT
for kv in "ip4_type|$WANT_TYPE" "ip|$WANT_IP" "subnet_mask|$WANT_MASK"; do
  k=\${kv%%|*}; v=\${kv#*|}
  "\$NACLI" setcfg -f "\$CONF" tbtbr0 "\$k" "\$v" || { echo "FATAL: nacli setcfg \$k failed"; exit 1; }
done
got="\$(get ip4_type)|\$(get ip)|\$(get subnet_mask)"
if [ "\$got" != "$WANT_TYPE|$WANT_IP|$WANT_MASK" ]; then
  echo "FATAL: read-back is \$got"
  exit 1
fi
trap - EXIT
echo "  written and read back: \$got"
[ "\$CONF" = "$LIVE_CONF" ] || exit 0

# Live check: the write must not have touched the running bridge. The address is the guard: a live
# re-apply would flush it at once, while target-side session counts lag (sessions linger until their
# timeouts) and also move for unrelated reasons (a pod moving logs a session out). So a missing
# address fails and is put back at once (the cron reconciler would within a minute); a lower session
# count is only reported. Checked immediately, then every 5 s for 30 s.
echo "== live check, 30 s: tbtbr0 must keep $SVC_IP (sessions before the write: \$sessions) =="
bad=0
for t in 0 5 10 15 20 25 30; do
  [ "\$t" = 0 ] || sleep 5
  l=\$(live)
  case " \$l " in
    *" $SVC_IP/"*) ;;
    *) echo "  !! t+\${t}s: $SVC_IP missing (live: \$l); re-adding it now"
       ip addr add $SVC_IP/$PREFIX dev tbtbr0 2>/dev/null || true
       bad=1 ;;
  esac
  s2=\$(count_sessions)
  note=""
  if [ "\$s2" = unknown ] || [ "\$s2" -lt "\$sessions" ]; then note=" (lower than before: noted, not a failure)"; fi
  echo "  t+\${t}s live: \$l sessions=\$s2\$note"
done
if [ "\$bad" != 0 ]; then
  echo "FAIL: the live bridge lost $SVC_IP after the write (re-added above). The STATIC config was KEPT in \$CONF;"
  echo "      to undo it: REVERT=1 LIVE_WRITE=1 bash scripts/qnap-tbtbr0-static-ip.sh, or cp \$BAK \$CONF."
  exit 1
fi
echo "  OK: the live bridge kept its address. The static address takes effect at the next NAS boot."
exit 0
EOF
)
printf '%s' "$REMOTE" | python scripts/qnap-ssh.py --sudo
