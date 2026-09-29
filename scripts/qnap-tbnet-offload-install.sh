#!/usr/bin/env bash
# Install/refresh the Thunderbolt offload enforcer on the QNAP (see scripts/qnap-tbnet-offload.sh for
# the why). Same pattern as scripts/qnap-versitygw-install.sh: the script's content lives in git, this
# installer is the only supported way to put it on the NAS, and it reconciles exactly one cron line.
#
# Idempotent: safe to re-run.
#   bash scripts/qnap-tbnet-offload-install.sh            # apply (also enforces once, immediately)
#   DRY_RUN=1 bash scripts/qnap-tbnet-offload-install.sh  # show what would change, touch nothing
#
# Prereqs: .env filled (QNAP_SSH_USER / QNAP_ADMIN_PASSWORD), SSH enabled on the QNAP.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="${TBNET_BASE:-/share/ZFS2_DATA/.tbnet-offload}"
SCHEDULE="${SCHEDULE:-* * * * *}"
DRY_RUN="${DRY_RUN:-0}"

SRC=scripts/qnap-tbnet-offload.sh
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }
sh -n "$SRC" || { echo "$SRC does not parse" >&2; exit 1; }

B64=$(base64 -w0 < "$SRC")
SUM=$(md5sum < "$SRC" | awk '{print $1}')
CRON_LINE="$SCHEDULE /bin/sh $BASE/enforce.sh >/dev/null 2>&1 # tbnet-offload"

echo "installing $SRC -> $BASE/enforce.sh (md5 $SUM)"
echo "cron line:  $CRON_LINE"
[ "$DRY_RUN" = 1 ] && echo "== DRY RUN: nothing will be changed =="
echo

# Fed on STDIN (not argv) for the same reason as the versitygw installer: Windows argv limits.
REMOTE_CMD="
  set -eu
  DRY=$DRY_RUN
  echo '== 1. install the enforcer =='
  if [ \"\$DRY\" = 1 ]; then
    echo '  (dry run) would write $BASE/enforce.sh'
  else
    mkdir -p '$BASE'
    printf '%s' '$B64' | base64 -d > '$BASE/enforce.sh.new'
    got=\$(md5sum < '$BASE/enforce.sh.new' | awk '{print \$1}')
    [ \"\$got\" = '$SUM' ] || { echo \"FATAL: checksum mismatch (\$got != $SUM)\"; rm -f '$BASE/enforce.sh.new'; exit 1; }
    chmod 755 '$BASE/enforce.sh.new'
    mv -f '$BASE/enforce.sh.new' '$BASE/enforce.sh'
    echo \"  installed, md5 \$got verified\"
  fi

  echo
  echo '== 2. reconcile the cron line =='
  CT=/etc/config/crontab
  grep -n 'tbnet-offload' \"\$CT\" | sed 's/^/    old: /' || true
  if [ \"\$DRY\" = 1 ]; then
    echo '  (dry run) would replace them with the single line above'
  else
    cp -f \"\$CT\" \"\$CT.bak-\$(date +%Y%m%d%H%M%S)\"
    grep -v 'tbnet-offload' \"\$CT\" > \"\$CT.new\" || true
    printf '%s\n' '$CRON_LINE' >> \"\$CT.new\"
    mv -f \"\$CT.new\" \"\$CT\"
    crontab \"\$CT\"
    /etc/init.d/crond.sh restart >/dev/null 2>&1 || true
    echo \"  now: \$(grep -c 'tbnet-offload' \"\$CT\") line(s)\"
  fi

  echo
  echo '== 3. enforce once now, and show the result =='
  [ \"\$DRY\" = 1 ] || /bin/sh '$BASE/enforce.sh'
  for i in tbtbr0 \$(ls /sys/class/net | grep -E '^tbtnet[0-9]+p[0-9]+\$'); do
    printf '  %s: ' \"\$i\"; ethtool -k \"\$i\" | grep -E '^(tcp-segmentation-offload|generic-segmentation-offload):' | tr '\n' ' '; echo
  done
"
printf '%s' "$REMOTE_CMD" | python scripts/qnap-ssh.py --sudo
