#!/usr/bin/env bash
# Install/refresh the QNAP storage reconciler (see scripts/qnap-tb-storage-ip.sh for the why). Same
# pattern as scripts/qnap-tbnet-offload-install.sh: the script's content lives in git, this installer
# is the only supported way to put it on the NAS, and it reconciles exactly one cron line. It touches
# nothing else (scripts/qnap-setup.sh, which also re-applies eth1 and the share, calls it).
#
# Idempotent: safe to re-run.
#   bash scripts/qnap-tb-storage-ip-install.sh            # apply, run once, assert the address
#   DRY_RUN=1 bash scripts/qnap-tb-storage-ip-install.sh  # show what would change, touch nothing
#
# Prereqs: .env filled (QNAP_SSH_USER / QNAP_ADMIN_PASSWORD), SSH enabled on the QNAP.
set -euo pipefail
cd "$(dirname "$0")/.."

DEST=/etc/config/tb-storage-ip.sh
SCHEDULE="${SCHEDULE:-* * * * *}"
DRY_RUN="${DRY_RUN:-0}"

SRC=scripts/qnap-tb-storage-ip.sh
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }
sh -n "$SRC" || { echo "$SRC does not parse" >&2; exit 1; }
# A CRLF copy would break every line on the NAS's busybox sh; .gitattributes forces LF, assert it.
if grep -q $'\r' "$SRC"; then echo "$SRC has CRLF line endings" >&2; exit 1; fi

B64=$(base64 -w0 < "$SRC")
SUM=$(md5sum < "$SRC" | awk '{print $1}')
CRON_LINE="$SCHEDULE $DEST >/dev/null 2>&1"

echo "installing $SRC -> $DEST (md5 $SUM)"
echo "cron line:  $CRON_LINE"
[ "$DRY_RUN" = 1 ] && echo "== DRY RUN: nothing will be changed =="
echo

# Fed on STDIN (not argv) for the same reason as the other QNAP installers: Windows argv limits.
REMOTE_CMD="
  set -eu
  DRY=$DRY_RUN
  echo '== 1. install the reconciler =='
  if [ -f '$DEST' ]; then echo \"  current md5 \$(md5sum < '$DEST' | awk '{print \$1}')\"; fi
  if [ \"\$DRY\" = 1 ]; then
    echo '  (dry run) would write $DEST'
  else
    printf '%s' '$B64' | base64 -d > '$DEST.new'
    got=\$(md5sum < '$DEST.new' | awk '{print \$1}')
    [ \"\$got\" = '$SUM' ] || { echo \"FATAL: checksum mismatch (\$got != $SUM)\"; rm -f '$DEST.new'; exit 1; }
    chmod 755 '$DEST.new'
    mv -f '$DEST.new' '$DEST'
    echo \"  installed, md5 \$got verified\"
  fi

  echo
  echo '== 2. reconcile the cron line =='
  CT=/etc/config/crontab
  grep -n 'tb-storage-ip' \"\$CT\" | sed 's/^/    old: /' || true
  if [ \"\$DRY\" = 1 ]; then
    echo '  (dry run) would replace them with the single line above'
  elif [ \"\$(grep -c 'tb-storage-ip' \"\$CT\")\" = 1 ] && grep -qxF '$CRON_LINE' \"\$CT\"; then
    echo '  already the single line above'
  else
    cp -f \"\$CT\" \"\$CT.bak-tb-storage-ip\"
    grep -v 'tb-storage-ip' \"\$CT\" > \"\$CT.new\" || true
    printf '%s\n' '$CRON_LINE' >> \"\$CT.new\"
    mv -f \"\$CT.new\" \"\$CT\"
    crontab \"\$CT\"
    /etc/init.d/crond.sh restart >/dev/null 2>&1 || true
    echo \"  now: \$(grep -c 'tb-storage-ip' \"\$CT\") line(s)\"
  fi

  echo
  echo '== 3. run once now, and ASSERT the result =='
  [ \"\$DRY\" = 1 ] || /bin/sh '$DEST'
  echo \"  tbtbr0: \$(ip -4 -o addr show dev tbtbr0 2>/dev/null | awk '{print \$4}' | tr '\n' ' ')\"
  echo \"  rw export entries: Thunderbolt subnet \$(exportfs -v 2>/dev/null | grep -c '10\.55\.0\.0/24(.*rw'), 10GbE subnet \$(exportfs -v 2>/dev/null | grep -c '10\.55\.1\.0/24(.*rw')\"
  if [ \"\$DRY\" != 1 ] && ! ip -4 -o addr show dev tbtbr0 2>/dev/null | grep -q ' 10\.55\.0\.254/24'; then
    echo 'FATAL: 10.55.0.254/24 is not on tbtbr0'; exit 1
  fi
  echo '  OK'
"
printf '%s' "$REMOTE_CMD" | python scripts/qnap-ssh.py --sudo
