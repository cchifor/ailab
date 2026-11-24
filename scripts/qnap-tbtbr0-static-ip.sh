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
# What this does NOT replace: S99thunderbolt.sh (~261 s) deletes tbtbr0 and recreates it with
# brctl, outside QNAP's network manager and with NO address. The cron reconciler puts 10.55.0.254
# on that second bridge, so it stays. The iSCSI portal watchdog
# (scripts/qnap-iscsi-portal-watchdog.sh) stays too, as the backstop.
#
# This WRITES CONFIG ONLY, through QNAP's network CLI (`nacli setcfg`). Nothing is applied to the
# live bridge; the value takes effect at the next NAS boot. It is not known whether QNAP's network
# daemon re-applies a written value live (and a live re-apply could flush 10.55.0.254, the address
# that carries the control-plane disks over NFS). So APPLY refuses while any iSCSI session is
# logged in, i.e. while the cluster is up: run it inside the planned window
# (plans/2026-11-24-qnap-tbtbr0-static-ip-plan.md), after every Talos node is shut down.
#
#   bash scripts/qnap-tbtbr0-static-ip.sh            # read-only: current vs wanted (the default)
#   APPLY=1 bash scripts/qnap-tbtbr0-static-ip.sh    # write STATIC 10.55.0.254/24 (window only)
#   REVERT=1 bash scripts/qnap-tbtbr0-static-ip.sh   # back to QNAP's default (DHCP, link-local)
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
APPLY="${APPLY:-0}"
REVERT="${REVERT:-0}"
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

echo "== tbtbr0 in \$CONF =="
echo "  now:  ip4_type=\$(get ip4_type) ip=\$(get ip) subnet_mask=\$(get subnet_mask)"
echo "  want: ip4_type=$WANT_TYPE ip=$WANT_IP subnet_mask=$WANT_MASK"
echo "  live: \$(live)"
if [ "\$(get ip4_type)|\$(get ip)|\$(get subnet_mask)" = "$WANT_TYPE|$WANT_IP|$WANT_MASK" ]; then
  echo "  already as wanted; nothing to write"
  exit 0
fi
if [ "$MODE" = show ]; then
  echo "  read-only. APPLY=1 writes it, inside the window, with the cluster shut down."
  exit 0
fi

# Interlock (live config only): zero logged-in iSCSI sessions NAS-wide, i.e. "the cluster is down".
# A failed query or a target record without a parseable sess_cnt is "unknown" and refuses. A
# successful query with no target records at all (a fresh NAS, before Trident made any LUN) counts as 0.
if [ "\$CONF" = "$LIVE_CONF" ]; then
  out=\$(iscsiadmin get_target 2>/dev/null) && rc=0 || rc=\$?
  if [ "\$rc" != 0 ]; then
    sessions=unknown
  else
    sessions=\$(printf '%s\n' "\$out" | awk '/^target = / { n++; if (match(\$0, /sess_cnt = [0-9]+/)) { s += substr(\$0, RSTART + 11, RLENGTH - 11); k++ } } END { if (k == n) print s + 0; else print "unknown" }')
  fi
  echo "== interlock: NAS-wide iSCSI sessions=\$sessions =="
  if [ "\$sessions" != 0 ]; then
    echo "FATAL: sessions=\$sessions. Run this inside the window, after every Talos node is shut down."
    exit 1
  fi
else
  echo "== rehearsal on a scratch copy (\$CONF): no interlock, the live config is not touched =="
fi

# Every write keeps its own pre-image. From the first write until the read-back is verified, an EXIT
# trap puts it back on ANY abort (a failed setcfg, a failed getcfg under set -e, a mismatch): the
# window reboots the NAS right after this step, so a half-written section must never survive.
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
[ "\$CONF" = "$LIVE_CONF" ] && echo "  live: \$(live) (unchanged is expected: it takes effect at the next NAS boot)"
exit 0
EOF
)
printf '%s' "$REMOTE" | python scripts/qnap-ssh.py --sudo
