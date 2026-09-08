#!/usr/bin/env bash
# Install/refresh the versitygw supervisor on the QNAP (W3 of the USB failure-domain plan).
#
# versitygw is NOT managed by Flux or OpenTofu -- it is a binary on a USB disk on the NAS, started
# from root's crontab. That is a recorded residual risk in plans/2026-09-08-usb-failure-domain-plan.md.
# This script is how the estate's 100%-IaC rule is honoured for it anyway: the watchdog's content
# lives in git (scripts/qnap-versitygw-watchdog.sh) and this installer is the only supported way to
# put it on the NAS. Do not hand-edit the deployed copy -- re-run this instead.
#
# Idempotent: safe to re-run. It re-copies the script (verifying the checksum), reconciles exactly
# one cron line, and leaves everything else alone.
#
#   bash scripts/qnap-versitygw-install.sh            # apply
#   DRY_RUN=1 bash scripts/qnap-versitygw-install.sh  # show what would change, touch nothing
#
# Prereqs: .env filled (QNAP_SSH_USER / QNAP_ADMIN_PASSWORD), SSH enabled on the QNAP.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="${VGW_SUPERVISOR_BASE:-/share/ZFS2_DATA/.versitygw-supervisor}"
VGW_DIR="${VGW_DIR:-/share/external/DEV3302_2/versitygw}"
SCHEDULE="${SCHEDULE:-*/3 * * * *}"
DRY_RUN="${DRY_RUN:-0}"

SRC=scripts/qnap-versitygw-watchdog.sh
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }
bash -n "$SRC" || { echo "$SRC does not parse" >&2; exit 1; }

B64=$(base64 -w0 < "$SRC")
SUM=$(md5sum < "$SRC" | awk '{print $1}')
CRON_LINE="$SCHEDULE /bin/bash $BASE/watchdog.sh >/dev/null 2>&1"

echo "installing $SRC -> $BASE/watchdog.sh (md5 $SUM)"
echo "cron line:  $CRON_LINE"
[ "$DRY_RUN" = 1 ] && echo "== DRY RUN: nothing will be changed =="
echo

# Build the remote command in a VARIABLE and feed it on STDIN rather than as an argument.
# The base64 payload is ~25 KB and Windows caps a process command line at 32767 characters, so
# passing it in argv fails with "Argument list too long" before the SSH connection is even made.
# qnap-ssh.py reads its command from stdin when none is given in argv, and `printf` is a bash
# builtin, so nothing here is bounded by argv limits.
REMOTE_CMD="
  set -eu
  DRY=$DRY_RUN

  echo '== preconditions =='
  [ -d '$VGW_DIR' ] || { echo \"FATAL: $VGW_DIR not found -- is the USB disk mounted?\"; exit 1; }
  # The whole point of W3 is that the supervisor does not live on the disk it watches. Prove it
  # here too, so a mis-set BASE fails the INSTALL rather than silently re-arming the original bug.
  case '$BASE' in /share/external/*) echo 'FATAL: BASE is on the external disk'; exit 1 ;; esac
  [ \"\$DRY\" = 1 ] || mkdir -p '$BASE/state'
  # Resolve the device via the deepest EXISTING ancestor, so a dry run stays genuinely read-only
  # (df on a non-existent path fails, and creating the dir just to answer it would be a mutation).
  probe='$BASE'
  while [ ! -d \"\$probe\" ] && [ \"\$probe\" != / ]; do probe=\$(dirname \"\$probe\"); done
  bdev=\$(df -P \"\$probe\" | awk 'NR==2{print \$1}')
  vdev=\$(df -P '$VGW_DIR' | awk 'NR==2{print \$1}')
  [ \"\$bdev\" != \"\$vdev\" ] || { echo \"FATAL: BASE and gateway are both on \$bdev\"; exit 1; }
  echo \"  supervisor base on \$bdev, gateway on \$vdev -- separate devices, good\"

  echo
  echo '== 1. install the watchdog =='
  if [ \"\$DRY\" = 1 ]; then
    echo '  (dry run) would write $BASE/watchdog.sh'
  else
    # printf, NOT echo: the QNAP's base64 treats echo's trailing newline as invalid input -- it
    # still decodes correctly but exits 1, which under 'set -e' aborts a step that actually worked.
    printf '%s' '$B64' | base64 -d > '$BASE/watchdog.sh.new'
    got=\$(md5sum < '$BASE/watchdog.sh.new' | awk '{print \$1}')
    [ \"\$got\" = '$SUM' ] || { echo \"FATAL: checksum mismatch (\$got != $SUM)\"; rm -f '$BASE/watchdog.sh.new'; exit 1; }
    chmod 755 '$BASE/watchdog.sh.new'
    mv -f '$BASE/watchdog.sh.new' '$BASE/watchdog.sh'
    echo \"  installed, md5 \$got verified\"
  fi

  echo
  echo '== 2. retire the old on-USB watchdog =='
  # Disable the OLD script BEFORE touching cron: while the old glob-based line is still live, a
  # renamed target makes it a no-op ([ -f ] fails), so there is no window in which both the old and
  # the new watchdog run against the same gateway.
  if [ -f '$VGW_DIR/watchdog.sh' ]; then
    if [ \"\$DRY\" = 1 ]; then
      echo '  (dry run) would rename $VGW_DIR/watchdog.sh -> watchdog.sh.retired'
    else
      mv -f '$VGW_DIR/watchdog.sh' '$VGW_DIR/watchdog.sh.retired'
      echo '  renamed to watchdog.sh.retired (kept for reference; the old cron glob no longer matches)'
    fi
  else
    echo '  already retired'
  fi

  echo
  echo '== 3. reconcile the cron line =='
  CT=/etc/config/crontab
  before=\$(grep -c 'versitygw' \"\$CT\" || true)
  echo \"  existing versitygw cron lines: \$before\"
  grep -n 'versitygw' \"\$CT\" | sed 's/^/    old: /' || true
  if [ \"\$DRY\" = 1 ]; then
    echo '  (dry run) would replace them with the single line above'
  else
    cp -f \"\$CT\" \"\$CT.bak-\$(date +%Y%m%d%H%M%S)\"
    grep -v 'versitygw' \"\$CT\" > \"\$CT.new\"
    printf '%s\n' '$CRON_LINE' >> \"\$CT.new\"
    mv -f \"\$CT.new\" \"\$CT\"
    # QNAP keeps the durable copy in /etc/config; the live table must be loaded from it explicitly.
    crontab \"\$CT\"
    /etc/init.d/crond.sh restart >/dev/null 2>&1 || true
    echo \"  now: \$(grep -c 'versitygw' \"\$CT\") line(s)\"
    grep -n 'versitygw' \"\$CT\" | sed 's/^/    new: /'
  fi

  echo
  echo '== 4. move versitygw stdout off the USB =='
  # The gateway's own log had grown to 478 MB unrotated ON THE USB, so the gateway blocked on its
  # own logging the moment the disk stalled. New starts go to \$BASE/versitygw.log (rotated by the
  # watchdog). The RUNNING process still holds an fd on the old file -- it moves on next restart --
  # so keep the recent tail as evidence and truncate in place to reclaim the space.
  OLD='$VGW_DIR/versitygw.log'
  if [ -f \"\$OLD\" ]; then
    sz=\$(wc -c < \"\$OLD\")
    echo \"  old log: \$sz bytes\"
    if [ \"\$DRY\" = 1 ]; then
      echo '  (dry run) would keep the last 5000 lines and truncate'
    elif [ \"\$sz\" -gt 20971520 ]; then
      tail -5000 \"\$OLD\" > '$BASE/versitygw.log.pre-w3' 2>/dev/null || true
      : > \"\$OLD\"
      echo \"  kept last 5000 lines at $BASE/versitygw.log.pre-w3, truncated the original\"
    else
      echo '  under the rotation threshold, left alone'
    fi
  fi

  echo
  echo '== 5. verify =='
  if [ \"\$DRY\" = 1 ]; then
    echo '  (dry run) skipped'
  else
    /bin/bash '$BASE/watchdog.sh' || echo \"  watchdog exited \$?\"
    echo '  --- status ---'
    cat '$BASE/state/status' 2>/dev/null || echo '  (no status written)'
    echo '  --- watchdog.log tail ---'
    tail -5 '$BASE/watchdog.log' 2>/dev/null || true
  fi
"
printf '%s' "$REMOTE_CMD" | python scripts/qnap-ssh.py --sudo
echo
echo "done."
