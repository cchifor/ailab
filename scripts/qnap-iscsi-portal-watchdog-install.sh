#!/usr/bin/env bash
# Install/refresh the iSCSI portal watchdog on the QNAP (see scripts/qnap-iscsi-portal-watchdog.sh
# for the why). Same pattern as scripts/qnap-tbnet-offload-install.sh: the script's content lives in
# git, this installer is the only supported way to put it on the NAS, and it reconciles exactly one
# cron line.
#
# Idempotent: safe to re-run. It never restarts the iSCSI service itself.
#   bash scripts/qnap-iscsi-portal-watchdog-install.sh            # apply, then assert `--check` = ok
#   DRY_RUN=1 bash scripts/qnap-iscsi-portal-watchdog-install.sh  # show what would change, touch nothing
#
# Prereqs: .env filled (QNAP_SSH_USER / QNAP_ADMIN_PASSWORD), SSH enabled on the QNAP.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="${ISCSI_WD_BASE:-/share/ZFS2_DATA/.iscsi-portal-watchdog}"
SCHEDULE="${SCHEDULE:-* * * * *}"
DRY_RUN="${DRY_RUN:-0}"

SRC=scripts/qnap-iscsi-portal-watchdog.sh
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }
sh -n "$SRC" || { echo "$SRC does not parse" >&2; exit 1; }
# A CRLF copy would break every line on the NAS's busybox sh; .gitattributes forces LF, assert it.
if grep -q $'\r' "$SRC"; then echo "$SRC has CRLF line endings" >&2; exit 1; fi

B64=$(base64 -w0 < "$SRC")
SUM=$(md5sum < "$SRC" | awk '{print $1}')
CRON_LINE="$SCHEDULE /bin/sh $BASE/watchdog.sh >/dev/null 2>&1 # iscsi-portal-watchdog"

echo "installing $SRC -> $BASE/watchdog.sh (md5 $SUM)"
echo "cron line:  $CRON_LINE"
[ "$DRY_RUN" = 1 ] && echo "== DRY RUN: nothing will be changed =="
echo

# Fed on STDIN (not argv) for the same reason as the other QNAP installers: Windows argv limits.
REMOTE_CMD="
  set -eu
  DRY=$DRY_RUN
  echo '== 1. the current state, judged by the new script before anything changes =='
  mkdir -p /tmp/iscsi-portal-watchdog-install
  printf '%s' '$B64' | base64 -d > /tmp/iscsi-portal-watchdog-install/watchdog.sh
  got=\$(md5sum < /tmp/iscsi-portal-watchdog-install/watchdog.sh | awk '{print \$1}')
  [ \"\$got\" = '$SUM' ] || { echo \"FATAL: checksum mismatch (\$got != $SUM)\"; exit 1; }
  rc=0; /bin/sh /tmp/iscsi-portal-watchdog-install/watchdog.sh --check || rc=\$?
  rm -rf /tmp/iscsi-portal-watchdog-install

  echo
  echo '== 2. install the watchdog =='
  if [ \"\$DRY\" = 1 ]; then
    echo '  (dry run) would write $BASE/watchdog.sh'
  else
    mkdir -p '$BASE'
    printf '%s' '$B64' | base64 -d > '$BASE/watchdog.sh.new'
    got=\$(md5sum < '$BASE/watchdog.sh.new' | awk '{print \$1}')
    [ \"\$got\" = '$SUM' ] || { echo \"FATAL: checksum mismatch (\$got != $SUM)\"; rm -f '$BASE/watchdog.sh.new'; exit 1; }
    chmod 755 '$BASE/watchdog.sh.new'
    mv -f '$BASE/watchdog.sh.new' '$BASE/watchdog.sh'
    echo \"  installed, md5 \$got verified\"
  fi

  echo
  echo '== 3. reconcile the cron line =='
  CT=/etc/config/crontab
  grep -n 'iscsi-portal-watchdog' \"\$CT\" | sed 's/^/    old: /' || true
  if [ \"\$DRY\" = 1 ]; then
    echo '  (dry run) would replace them with the single line above'
  else
    # One rolling backup, as the tbnet installer does: timestamped copies would pile up on the
    # QNAP's small flash-backed config partition.
    cp -f \"\$CT\" \"\$CT.bak-iscsi-portal-watchdog\"
    grep -v 'iscsi-portal-watchdog' \"\$CT\" > \"\$CT.new\" || true
    printf '%s\n' '$CRON_LINE' >> \"\$CT.new\"
    mv -f \"\$CT.new\" \"\$CT\"
    crontab \"\$CT\"
    /etc/init.d/crond.sh restart >/dev/null 2>&1 || true
    echo \"  now: \$(grep -c 'iscsi-portal-watchdog' \"\$CT\") line(s)\"
    crontab -l | grep -q 'iscsi-portal-watchdog' || { echo 'FATAL: cron line not active'; exit 1; }
  fi

  echo
  echo '== 4. assert =='
  # The only supported deploy path must fail loudly, not print a bad state and exit 0. A STALE
  # verdict is left to the cron job (it debounces, then checks for live sessions); this installer
  # never restarts the service.
  if [ \"\$rc\" != 0 ]; then echo \"FATAL: --check exited \$rc (1 = stale bindings, 2 = cannot judge)\"; exit 1; fi
  echo '  OK'
"
printf '%s' "$REMOTE_CMD" | python scripts/qnap-ssh.py --sudo
