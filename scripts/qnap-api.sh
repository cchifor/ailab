#!/usr/bin/env bash
# Read-only QNAP inventory. Two modes:
#   qnap-api.sh ssh        # most reliable: firmware, zpool/zfs, disks, net (needs SSH enabled)
#   qnap-api.sh inventory  # HTTP: unauth quick info, + auth sysinfo if QNAP_USER/QNAP_PASS set
# Nothing here changes state.
set -uo pipefail

QNAP_HOST="${QNAP_HOST:-ai-storage}"
QNAP_PORT="${QNAP_PORT:-8080}"
BASE="http://${QNAP_HOST}:${QNAP_PORT}"
cmd="${1:-inventory}"

case "$cmd" in
  ssh)
    QNAP_SSH_HOST="${QNAP_SSH_HOST:-192.168.1.225}"
    QNAP_SSH_USER="${QNAP_SSH_USER:-admin}"
    echo "### QNAP ssh ${QNAP_SSH_USER}@${QNAP_SSH_HOST}  $(date -u +%FT%TZ)"
    # shellcheck disable=SC2016
    ssh -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new \
      "${QNAP_SSH_USER}@${QNAP_SSH_HOST}" '
        echo "== model/fw =="; getcfg System "Internal Model" -f /etc/config/uLinux.conf 2>/dev/null;
        getcfg System Version -f /etc/config/uLinux.conf 2>/dev/null;
        getcfg System "Build Number" -f /etc/config/uLinux.conf 2>/dev/null;
        echo "== mem =="; free -m 2>/dev/null | head -2;
        echo "== zpool =="; zpool list 2>/dev/null; zpool status 2>/dev/null;
        echo "== zfs =="; zfs list 2>/dev/null;
        echo "== disks =="; qcli_storage -d 2>/dev/null || true;
        echo "== net =="; (ip -br addr 2>/dev/null || ifconfig -a 2>/dev/null);
        echo "== nfs exports =="; cat /etc/exports 2>/dev/null || true;
      '
    ;;
  inventory)
    echo "### QNAP http $BASE  $(date -u +%FT%TZ)"
    echo "== unauth quick info (authLogin.cgi) =="
    curl -s --max-time 8 "$BASE/cgi-bin/authLogin.cgi" || true
    echo
    if [ -n "${QNAP_USER:-}" ] && [ -n "${QNAP_PASS:-}" ]; then
      echo "== authenticated sysinfo =="
      pw=$(printf '%s' "$QNAP_PASS" | base64)
      sid=$(curl -s --max-time 10 "$BASE/cgi-bin/authLogin.cgi" \
              --data-urlencode "user=$QNAP_USER" --data-urlencode "pwd=$pw" \
            | sed -n 's:.*<authSid><!\[CDATA\[\(.*\)\]\]></authSid>.*:\1:p')
      if [ -n "${sid:-}" ]; then
        echo "(sid acquired) — endpoint paths vary by firmware; prefer the 'ssh' mode for disks/zfs."
        curl -s --max-time 10 "$BASE/cgi-bin/sysinfoReq.cgi?subfunc=sysinfo&sid=$sid" || true
      else
        echo "auth failed (wrong creds, or 2FA/MFA enabled). Use 'ssh' mode."
      fi
    else
      echo "(set QNAP_USER and QNAP_PASS for authenticated info, or use 'ssh' mode)"
    fi
    ;;
  *)
    echo "usage: qnap-api.sh [ssh|inventory]" >&2; exit 1 ;;
esac
