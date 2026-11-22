# Fixture for ansible/roles/gitea_runner/tests/test-fstrim-chunked.sh: CT 5004's hand-installed weekly trim unit,
# verbatim from docs/runbooks/registry-cache.md before 5004's decommission (2026-11-22). The test feeds edited copies of
# it to its schedule check to prove a dedicated registry trim that collides with a runner window is caught.

## Thin-volume trim (weekly, ai-node1)

```bash
cat > /etc/systemd/system/registry-lxc-fstrim.service <<'UNIT'
[Unit]
Description=Chunked, IO-pressure-guarded fstrim of the registry LXC data volume (ct 5004 mp0)
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lxc-fstrim-chunked.sh 5004 mp0
Nice=10
# The overall deadline. A oneshot is "activating" for its whole run, so RuntimeMaxSec= does nothing
# here (checked on systemd 255): TimeoutStartSec= is the limit. Latest start 03:46 Sunday (03:30 +
# 15 min RandomizedDelaySec + 1 min AccuracySec); SIGTERM by 23:31, done by 23:36 with the stop time
# below, so a run that keeps waiting out IO pressure never reaches Monday's ai-node1 runner windows.
TimeoutStartSec=19h45min
# On stop the script waits for the in-flight FITRIM (8 GiB at up to ~23 s/GiB is ~3 min), then
# unmounts (exit 143); leave it time to, and record that deliberate stop as clean rather than a
# failed unit. (A stop at the deadline above still shows as failed, Result=timeout: by design.)
TimeoutStopSec=300
SuccessExitStatus=143 130
UNIT
cat > /etc/systemd/system/registry-lxc-fstrim.timer <<'UNIT'
[Unit]
Description=Weekly registry LXC trim, CI-quiet hours (after the 00:00Z infra-pg dump and 02:00Z Velero)
[Timer]
OnCalendar=Sun *-*-* 03:30:00 UTC
RandomizedDelaySec=15min
[Install]
WantedBy=timers.target
UNIT
```
